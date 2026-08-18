"""Weekly-summary generation extracted from scheduler.py.

The functions keep a ``self`` parameter bound to the Scheduler instance so
existing call sites and state access stay unchanged; scheduler.py now only
delegates here.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bilibot.services.clock import now_cn

logger = logging.getLogger("bilibot.scheduler_weekly")


def build_week_summary_from_sqlite(self, data_dir: str) -> str:
    """Build a bounded weekly prompt input from validated V6 events."""
    try:
        brain = getattr(self, "memory_brain", None)
        if not brain:
            return ""
        cutoff = time.time() - 7 * 86400
        events = brain.list_events(limit=500)
        lines = []
        total_chars = 0
        for event in reversed(events):
            if float(event.get("created_at") or 0) < cutoff:
                continue
            if event.get("source_type") == "weekly_summary":
                continue
            label = event.get("source_type") or event.get("event_type") or "经历"
            text = event.get("summary") or event.get("title") or ""
            if not text:
                continue
            line = f"- [{label}] {text}"
            if total_chars + len(line) > 12000:
                break
            lines.append(line)
            total_chars += len(line)
        return "\n".join(lines)
    except Exception as exc:
        logger.debug("从 V6 brain 读取周活动失败: %s", type(exc).__name__)
        return ""


async def check_weekly_summary(self) -> None:
    """检查并生成周总结

    PRD V3 §8.5 / §9.3 / PRD V4 SUM-001：
    - features.weekly_summary=false 时不执行检查和生成
    - 从 SQLite 读取本周活动（fallback 到 JSON）
    - 通过 orchestrator.build_weekly_summary_prompt 构建 prompt
    - 写入 audit + 账号级 V6 memory brain
    """
    # PRD V4 SUM-001：周总结开关
    features = self.config_loader.get_raw_config().get("features", {})
    if not features.get("weekly_summary", True):
        return

    cn_now = now_cn()
    if cn_now.weekday() != 0:  # 0=Monday，产品时区（Asia/Shanghai）
        return

    if not self.llm:
        return

    # DYN-605：task_id 在 try 块外初始化，便于 except 中 fail
    task_id = None
    try:
        this_week = cn_now.strftime("%G-W%V")
        brain = getattr(self, "memory_brain", None)
        if brain and await asyncio.to_thread(
            brain.has_identifier, this_week
        ):
            return

        logger.info("生成周总结...")

        # DYN-605：创建 TaskRun 跟踪周总结生命周期（可重试）
        try:
            from bilibot.services.task_store import (
                STATUS_CLAIMED,
                STATUS_RETRY_WAIT,
                STATUS_RUNNING,
                STATUS_SCHEDULED,
                STATUS_SUCCEEDED,
                TRIGGER_SCHEDULE,
            )
            _idem_key = f"{self.account_id or '_default'}:weekly_summary:{this_week}"
            _task = self.task_store.create_if_absent(
                account_id=self.account_id or "_default",
                scene="weekly_summary",
                idempotency_key=_idem_key,
                trigger_type=TRIGGER_SCHEDULE,
                scheduled_at=time.time(),
                input_data={"week": this_week},
            )
            if _task:
                task_id = _task.task_id
                if not self.task_store.claim(task_id):
                    logger.warning(f"周总结 TaskRun {task_id} claim 失败（可能已被处理）")
                    return
                if not self.task_store.start(task_id):
                    logger.warning(f"周总结 TaskRun {task_id} start 失败（可能已被处理）")
                    return
            else:
                # 已存在同周 TaskRun：不要无视其重试退避每分钟重新生成。
                existing = self.task_store.get_by_idempotency_key(_idem_key)
                if existing is not None:
                    if existing.status == STATUS_SUCCEEDED:
                        return
                    if existing.status == STATUS_SCHEDULED:
                        # 上次创建后未进入 running（如 crash 或修复前代码）：
                        # 补上 claim→start，继续受 TaskRun 生命周期保护。
                        if not self.task_store.claim(existing.task_id):
                            logger.warning(
                                "周总结 TaskRun %s claim 失败（可能已被处理）",
                                existing.task_id,
                            )
                            return
                        if not self.task_store.start(existing.task_id):
                            logger.warning(
                                "周总结 TaskRun %s start 失败（可能已被处理）",
                                existing.task_id,
                            )
                            return
                        task_id = existing.task_id
                    if existing.status in (
                        STATUS_RETRY_WAIT,
                        STATUS_RUNNING,
                        STATUS_CLAIMED,
                    ):
                        retry_at = float(existing.next_retry_at or 0)
                        if (
                            existing.status == STATUS_RETRY_WAIT
                            and retry_at > time.time()
                        ):
                            logger.debug(
                                "周总结 TaskRun 处于退避期，跳过: task=%s next=%.0fs",
                                existing.task_id,
                                retry_at - time.time(),
                            )
                            return
                        if existing.status in (STATUS_RUNNING, STATUS_CLAIMED):
                            logger.debug(
                                "周总结 TaskRun 已在进行中，跳过: task=%s status=%s",
                                existing.task_id,
                                existing.status,
                            )
                            return
                        if existing.status == STATUS_RETRY_WAIT:
                            if not self.task_store.claim(existing.task_id):
                                logger.warning(
                                    "周总结 TaskRun %s claim 失败（可能已被处理）",
                                    existing.task_id,
                                )
                                return
                            if not self.task_store.start(existing.task_id):
                                logger.warning(
                                    "周总结 TaskRun %s start 失败（可能已被处理）",
                                    existing.task_id,
                                )
                                return
                            task_id = existing.task_id
        except Exception as e:
            logger.warning(f"创建周总结 TaskRun 失败: {e}")

        # 1. 从 V6 account brain 读取本周活动。
        data_dir = self._get_data_dir()
        week_summary_text = build_week_summary_from_sqlite(self, data_dir)

        if not week_summary_text.strip() or week_summary_text == "无活动记录":
            logger.info("本周无活动记录，跳过周总结")
            if task_id:
                self._succeed_task(task_id, {
                    "success": True,
                    "summary": "本周无活动记录，跳过",
                    "week": this_week,
                })
            return

        weekly_activity = await self._begin_activity_context(
            action_key=f"weekly_summary:{this_week}",
            action_type="write_weekly_summary",
            current_activity=(
                "正在写本周总结，会回顾这一周已经做过、看过、发布过和写过的事情，再整理连续的自我感受。"
            ),
            query=week_summary_text[:3000],
            scene="weekly_summary",
            title=f"周总结 {this_week}",
            # Do not bind the intent hash to TaskRun creation; a retry may
            # acquire a task id after an earlier TaskStore failure.
            metadata={"week": this_week},
        )
        weekly_activity_prompt = str(
            getattr(weekly_activity, "prompt_text", "") or ""
        ).strip()

        # 2. 通过 orchestrator 构建 prompt
        persona_id = "unknown"
        persona = None
        if self.persona_store is not None:
            try:
                # PRD V4 ACC-002：使用账号绑定人格，避免多账号取全局人格
                persona = self.persona_store.get_persona_for_account(self.account_id)
                persona_id = persona.id if persona else "unknown"
            except Exception:
                persona = None

        system_prompt = ""
        user_prompt = ""
        if self.orchestrator is not None:
            try:
                prompt_dict = self.orchestrator.build_weekly_summary_prompt(
                    week_summary=week_summary_text,
                    persona=persona,
                )
                system_prompt = prompt_dict.get("system", "")
                user_prompt = prompt_dict.get("user", "")
            except Exception as e:
                logger.warning(f"orchestrator.build_weekly_summary_prompt 失败: {e}")

        if not system_prompt or not user_prompt:
            system_prompt = self.personality.get_system_prompt()
            user_prompt = (
                f"上周的活动记录：\n\n{week_summary_text}\n\n"
                "请用生动的语气写一份周报，200-350字。"
            )
        if weekly_activity_prompt:
            user_prompt = (
                f"{user_prompt}\n\n【当前活动与跨场景记忆】\n"
                f"{weekly_activity_prompt[:3500]}"
            )

        # 3. 调用 LLM
        from bilibot.services.token_usage import usage_context
        with usage_context(scene="weekly_summary", account_id=self.account_id or ""):
            summary = await self.llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=2000,
            )

        if not summary:
            logger.warning("LLM生成周总结失败")
            if task_id:
                self._fail_task(task_id, "LLM_GENERATE_FAILED",
                                "LLM生成周总结失败", retryable=True)
            return

        # 4. 写入 audit
        audit_id = None
        if self.audit_store:
            try:
                audit_id = await self.audit_store.record_async(
                    scene="weekly_summary",
                    persona_id=persona_id or "unknown",
                    input_summary=user_prompt[:200],
                    context_summary=week_summary_text[:500],
                    prompt_preview=system_prompt[:2000],
                    output=summary[:2000],
                    published=False,
                    target={"kind": "weekly_summary", "week": this_week},
                )
            except Exception:
                audit_id = None

        # 5. 新增反思事件，不覆盖或压缩底层经历。
        from bilibot.memory_brain.ingestion import text_observation

        await self._archive_required(
            text_observation(
                account_id=self.account_id or "default",
                idempotency_key=this_week,
                source_type="weekly_summary",
                event_type="reflection",
                text=summary,
                title=f"周总结 {this_week}",
                persona_id=persona_id,
                scene="weekly_summary",
                metadata={"week": this_week, "audit_id": audit_id},
                importance=0.8,
            )
        )
        await self._archive_bot_action(
            action_key=f"weekly_summary:{this_week}",
            action_type="write_weekly_summary",
            text=f"本周总结 {this_week} 已经写完并归档。",
            published=True,
            title=f"周总结 {this_week}",
            scene="weekly_summary",
            metadata={"week": this_week, "task_id": task_id or ""},
        )
        logger.info("周总结已生成")
        # DYN-605：标记 TaskRun 成功
        if task_id:
            self._succeed_task(task_id, {
                "success": True,
                "summary": f"周总结已生成: {this_week}",
                "week": this_week,
            })

    except Exception as e:
        logger.error(f"周总结失败: {e}", exc_info=True)
        # DYN-605：失败时标记 TaskRun（可重试）
        if task_id:
            self._fail_task(task_id, "WEEKLY_SUMMARY_ERROR", str(e), retryable=True)
