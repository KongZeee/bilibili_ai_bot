"""start extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any, Dict

from bilibot.runtime_health import consolidation_schedule_reached
from bilibot.scheduler_motive import prepare_tick_motive
from bilibot.services.clock import now_cn


logger = logging.getLogger("bilibot.scheduler_start")

_CONSOLIDATION_STATE_FILE = "consolidation_state.json"


def _log_companion_tick(account_id: str, actions: Any) -> None:
    """Keep stable motive/rest ticks out of the INFO log stream."""
    normalized = [str(action) for action in (actions or ()) if str(action)]
    if not normalized:
        return
    stable = all(
        action == "rest" or action.startswith("motive:")
        for action in normalized
    )
    if stable:
        logger.debug(
            "[%s] companion tick stable: %s",
            account_id or "-",
            ",".join(normalized),
        )
        return
    meaningful = [
        action
        for action in normalized
        if action != "rest" and not action.startswith("motive:")
    ]
    logger.info(
        "[%s] companion tick actions: %s",
        account_id or "-",
        ",".join(meaningful or normalized),
    )


def _load_consolidation_attempt_date(self) -> date | None:
    if not self.ds:
        return None
    state = self.ds.load_json(_CONSOLIDATION_STATE_FILE, {}) or {}
    if not isinstance(state, dict):
        return None
    raw = str(state.get("last_attempt_date") or "")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _record_consolidation_attempt(
    self,
    day: date,
    *,
    outcome: str = "started",
    reflection_count: int = 0,
    error_kind: str = "",
) -> None:
    if not self.ds:
        return
    state = {
        "last_attempt_date": day.isoformat(),
        "attempted_at": now_cn().isoformat(),
        "outcome": str(outcome or "started"),
        "reflection_count": max(0, int(reflection_count or 0)),
        "error_kind": str(error_kind or ""),
    }
    try:
        if not self.ds.save_json(_CONSOLIDATION_STATE_FILE, state):
            logger.warning("夜间记忆巩固尝试状态保存失败")
    except Exception:
        logger.warning("夜间记忆巩固尝试状态保存异常", exc_info=True)


async def scheduler_start(self):
    """启动调度器"""
    self.running = True
    logger.info("BiliBot 调度器启动")

    # 加载已回复记录
    self._load_replied_state()

    # PRD V3 §4.8：加载评论失败计数器（重启后恢复阈值保护）
    self._load_fail_counts()

    # A failed soft consolidation must not be retried repeatedly merely because
    # the process restarts during the configured consolidation hour.
    persisted_consolidation_day = _load_consolidation_attempt_date(self)
    if persisted_consolidation_day is not None:
        self._last_consolidation_date = persisted_consolidation_day

    # V6 memories never expire or auto-prune. Durable jobs resume from SQLite.

    # PRD-V5 §7.2 / §6.3：启动时恢复未完成的 TaskRun
    # claimed/running → interrupted（重启前在运行，需按场景恢复）
    try:
        recovered = self.task_store.recover_interrupted()
        if recovered:
            logger.info(f"启动恢复：{recovered} 个 TaskRun 由 claimed/running 转为 interrupted")
        # 标记过期任务（超过 grace_window 的 scheduled）
        expired = self.task_store.expire_overdue()
        if expired:
            logger.info(f"启动过期清理：{expired} 个 TaskRun 标记为 expired")
        reconciled = self.task_store.clear_succeeded_errors()
        if reconciled:
            logger.info(f"启动状态对账：清理 {reconciled} 个成功 TaskRun 的残留错误")
    except Exception as e:
        logger.warning(f"TaskRun 启动恢复失败: {e}")

    # 先清理 video_temp 孤儿文件，再 spawn 恢复任务，避免误删/与新下载竞态
    try:
        from bilibot.video_understanding.cleanup import cleanup_orphaned_video_temp
        import os as _os
        video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
        cleaned = cleanup_orphaned_video_temp(video_temp_dir)
        if cleaned:
            logger.info(f"启动清理：{cleaned} 个 video_temp 孤儿文件/目录已清理")
    except Exception as e:
        logger.warning(f"启动清理 video_temp 失败: {e}")

    # Task 5：启动时恢复 interrupted 状态的 TaskRun
    # 按场景重新入队（retry → scheduled）或立即重跑（retry → claim → dispatch）
    try:
        interrupted = self.task_store.list_interrupted()
        if interrupted:
            logger.info(f"Task 5: 发现 {len(interrupted)} 个 interrupted TaskRun，开始恢复")
            for task in interrupted:
                scene = task.scene
                # dynamic_post is the canonical scene; keep "dynamic" for legacy rows.
                if scene in ("proactive_video", "dynamic", "dynamic_post", "bangumi"):
                    # 立即重跑：retry → _dispatch_task_run（claim + scene/kind 分流统一）
                    if not self.task_store.retry(task.task_id):
                        logger.warning(f"Task 5: TaskRun {task.task_id} retry 失败")
                        continue
                    if not self._dispatch_task_run(
                        task.task_id,
                        tag_prefix="recovery",
                        claim_if_scheduled=True,
                    ):
                        logger.warning(
                            f"Task 5: TaskRun {task.task_id} dispatch 失败 scene={scene}"
                        )
                        continue
                    _recovery_bvid = ""
                    if scene == "proactive_video":
                        try:
                            _r_input = json.loads(task.input_json) if task.input_json else {}
                            _recovery_bvid = str(_r_input.get("bvid") or "")
                        except Exception:
                            pass
                    logger.info(
                        f"Task 5: interrupted TaskRun {task.task_id} 已派发（scene={scene}）"
                        + (f"，将优先重试 bvid={_recovery_bvid}" if _recovery_bvid else "")
                    )
                else:
                    # 其他场景：重新入队（转 scheduled，由各自调度机制拾取）
                    if self.task_store.retry(task.task_id):
                        logger.info(f"Task 5: TaskRun {task.task_id} 重新入队（scene={scene}）")
    except Exception as e:
        logger.warning(f"Task 5: interrupted 恢复失败: {e}")

    # Task 4：启动时恢复卡在 publishing 状态的主动评论（崩溃前 mark_publishing 后未完成）
    try:
        stuck = self.proactive_comment_store.recover_stuck_publishing()
        if stuck:
            logger.warning(
                f"Task 4: 启动恢复 {stuck} 个卡在 publishing 的主动评论 → result_unknown"
            )
    except Exception as e:
        logger.warning(f"Task 4: 启动恢复 publishing 卡死失败: {e}")

    # C6：启动时恢复卡在 claimed 的主动评论（生成前崩溃永久锁 bvid）
    try:
        recover_claimed = getattr(
            self.proactive_comment_store, "recover_stuck_claimed", None
        )
        if callable(recover_claimed):
            stuck_claimed = recover_claimed()
            if stuck_claimed:
                logger.warning(
                    f"C6: 启动恢复 {stuck_claimed} 个卡在 claimed 的主动评论"
                )
    except Exception as e:
        logger.warning(f"C6: 启动恢复 claimed 卡死失败: {e}")

    # Task 42：启动时恢复卡住的动态草稿（approved 超期 / publishing 超阈值）
    try:
        stuck_drafts = self._get_draft_store().recover_stuck_drafts()
        if stuck_drafts.get("approved_expired") or stuck_drafts.get("publishing_stuck"):
            logger.warning(
                f"Task 42: 启动恢复动态草稿: approved_expired={stuck_drafts.get('approved_expired', 0)}, "
                f"publishing_stuck={stuck_drafts.get('publishing_stuck', 0)}"
            )
    except Exception as e:
        logger.warning(f"Task 42: 启动恢复动态草稿卡死失败: {e}", exc_info=True)

    # 生成今日调度计划
    self._generate_daily_schedule()
    self._schedule_date = now_cn().date()

    # 跳过已过期的计划
    self._mark_overdue_as_triggered()

    # 检查登录状态
    config = self.config_loader.get_raw_config()
    if not self.bili or not config.get("bilibili", {}).get("sessdata"):
        logger.warning("B站未登录！请配置SESSDATA和bili_jct")

    # 获取 Bot 自身昵称（优先用配置，其次从 B站 API 获取）
    self._bot_name = config.get("personality", {}).get("bot_name", "") or ""
    self._bot_uid = str(config.get("bilibili", {}).get("dede_user_id", "") or "")
    identity_cache: Dict[str, Any] = {}
    if self.ds is not None:
        try:
            loaded_identity = self.ds.load_json("bili_identity_cache.json", {}) or {}
            if isinstance(loaded_identity, dict):
                identity_cache = loaded_identity
        except Exception:
            identity_cache = {}
    # 任一字段缺失都尝试从 nav API 补全
    if (not self._bot_name or not self._bot_uid) and self.bili:
        try:
            nav = await self.bili.get_nav()
            if nav and nav.get("code") == 0:
                data = nav.get("data", {})
                self._bot_name = data.get("uname", "")
                if not self._bot_uid:
                    self._bot_uid = str(data.get("mid", ""))
                if self._bot_name:
                    logger.info(f"Bot 昵称: {self._bot_name} (UID: {self._bot_uid})")
                    # 回填到配置，让 personality 系统也能用
                    config.setdefault("personality", {})["bot_name"] = self._bot_name
                    if self.ds is not None:
                        self.ds.save_json(
                            "bili_identity_cache.json",
                            {
                                "uname": self._bot_name,
                                "mid": self._bot_uid,
                                "updated_at": now_cn().isoformat(),
                            },
                        )
        except Exception as e:
            logger.warning(f"获取 Bot 昵称失败: {e}")
    if not self._bot_name and identity_cache.get("uname"):
        self._bot_name = str(identity_cache.get("uname") or "")
        if not self._bot_uid:
            self._bot_uid = str(identity_cache.get("mid") or "")
        logger.warning(
            "B站 nav 暂不可用，沿用上次成功昵称: %s (UID: %s)",
            self._bot_name,
            self._bot_uid or "-",
        )
    if not self._bot_name:
        self._bot_name = "Bot"
        logger.warning("未获取到 Bot 昵称，使用默认值 'Bot'")

    # 主循环
    while self.running:
        try:
            now = now_cn()
            current_time = f"{now.hour:02d}:{now.minute:02d}"

            # 日期变更检测：跨天时重置调度计划（PRD 3.3）
            if self._schedule_date and now.date() != self._schedule_date:
                logger.info(f"日期变更：{self._schedule_date} → {now.date()}，重新生成调度计划")
                self._schedule_date = now.date()
                self._proactive_triggered.clear()
                self._dynamic_triggered.clear()
                self._last_consolidation_date = None
                self._generate_daily_schedule()
                self._mark_overdue_as_triggered()

            # Cookie 自动刷新（对齐 AstrBot 插件：默认每 6 小时检查）
            # 间隔优先 features.cookie_check_interval_hours，
            # 其次 bilibili 段（账号级 ConfigLoader 会覆盖 bilibili 凭据字段）
            if self.bili is not None:
                try:
                    raw_cfg = self.config_loader.get_raw_config() or {}
                    features = raw_cfg.get("features") or {}
                    bili_sec = raw_cfg.get("bilibili") or {}
                    interval_h = features.get("cookie_check_interval_hours")
                    if interval_h is None:
                        interval_h = bili_sec.get("cookie_check_interval_hours", 6)
                    interval_h = float(interval_h)
                except Exception:
                    interval_h = 6.0
                try:
                    ok, msg = await self.bili.maybe_refresh_cookie(
                        interval_hours=interval_h,
                    )
                    if not ok and msg not in ("skip", "未登录"):
                        logger.warning("Cookie 检查/刷新: %s", msg)
                except Exception as e:
                    logger.warning(
                        "Cookie 自动刷新异常: %s", type(e).__name__,
                    )

            # 日终记忆清算（PRD 3.4，默认 03:00）
            consolidation_hour = 3
            consolidation_minute = 0
            try:
                consolidation_config = (
                    self.config_loader.get_raw_config()
                    .get("memory", {})
                    .get("consolidation", {})
                )
                consolidation_hour = int(consolidation_config.get("hour", 3))
                consolidation_minute = int(consolidation_config.get("minute", 0))
            except Exception:
                pass
            # Run once after today's scheduled time, including a late process
            # start or a loop that was paused during the exact hour.
            if (
                consolidation_schedule_reached(
                    now,
                    hour=consolidation_hour,
                    minute=consolidation_minute,
                )
                and now.date() != self._last_consolidation_date
            ):
                self._last_consolidation_date = now.date()
                _record_consolidation_attempt(self, now.date(), outcome="started")
                try:
                    brain = getattr(self, "memory_brain", None)
                    if brain:
                        await brain.run_jobs_until_idle(max_jobs=1000)
                        reflections = await brain.consolidate_recent(
                            now.date().isoformat()
                        )
                        outcome = getattr(
                            brain, "get_last_consolidation_outcome", lambda: {}
                        )()
                        if not isinstance(outcome, dict):
                            outcome = {}
                        outcome_name = str(outcome.get("outcome") or "success")
                        _record_consolidation_attempt(
                            self,
                            now.date(),
                            outcome=outcome_name,
                            reflection_count=reflections,
                            error_kind=str(outcome.get("error_kind") or ""),
                        )
                        if outcome_name in {
                            "invalid_model_output",
                            "exception",
                            "memory_unavailable",
                        }:
                            logger.warning(
                                "夜间记忆巩固结束但结果异常: outcome=%s reflections=%d",
                                outcome_name,
                                reflections,
                            )
                        else:
                            logger.info(
                                "夜间记忆巩固完成: outcome=%s 新增 %d 条反思",
                                outcome_name,
                                reflections,
                            )
                    else:
                        _record_consolidation_attempt(
                            self, now.date(), outcome="memory_unavailable"
                        )
                        logger.warning("夜间记忆巩固跳过: memory brain unavailable")
                except Exception as e:
                    _record_consolidation_attempt(
                        self,
                        now.date(),
                        outcome="exception",
                        error_kind=type(e).__name__,
                    )
                    logger.error("夜间记忆巩固失败: %s", type(e).__name__)

            # 番剧追番可能包含下载、视觉分析和多集观看，必须后台执行，
            # 否则会阻塞同账号的评论、私信、主动视频、动态和 companion tick。
            self._maybe_schedule_bangumi_check(now)

            # REP-602：恢复卡在中间态的评论（context_building/
            # generation_pending/safety_pending/publish_pending 超过 10 分钟
            # 未推进 → 转为 deferred，由后续 _process_retryable_comments 拾取）
            try:
                self.reply_state_store.recover_stuck_intermediate(timeout_minutes=10)
            except Exception as e:
                logger.warning(
                    f"REP-602: 恢复卡在中间态评论失败: {e}", exc_info=True
                )

            # PM-501：恢复卡住的私信（stale discovered/archive failure → deferred；publish_pending → result_unknown 防止重复发送）
            try:
                stuck_pm = self.pm_state_store.recover_stuck_intermediate(
                    account_id=self.account_id, timeout_minutes=10,
                )
                if stuck_pm:
                    logger.warning(f"PM-501: 恢复 {stuck_pm} 条卡在中间态的私信")
            except Exception as e:
                logger.warning(f"PM-501: 恢复卡在中间态私信失败: {e}", exc_info=True)

            # Task 4：周期性恢复卡在 publishing 状态的主动评论
            # （mark_publishing 后崩溃 → lease_until 超时 → result_unknown）
            try:
                stuck = self.proactive_comment_store.recover_stuck_publishing()
                if stuck:
                    logger.warning(
                        f"Task 4: 恢复 {stuck} 个卡在 publishing 的主动评论 → result_unknown"
                    )
            except Exception as e:
                logger.warning(f"Task 4: 恢复 publishing 卡死失败: {e}")

            # C6：周期性恢复卡在 claimed 的主动评论
            try:
                recover_claimed = getattr(
                    self.proactive_comment_store, "recover_stuck_claimed", None
                )
                if callable(recover_claimed):
                    stuck_claimed = recover_claimed()
                    if stuck_claimed:
                        logger.warning(
                            f"C6: 恢复 {stuck_claimed} 个卡在 claimed 的主动评论"
                        )
            except Exception as e:
                logger.warning(f"C6: 恢复 claimed 卡死失败: {e}")

            # Task 42：恢复卡住的动态草稿（approved 超期 / publishing 超阈值）
            try:
                stuck_drafts = self._get_draft_store().recover_stuck_drafts()
                if stuck_drafts.get("approved_expired") or stuck_drafts.get("publishing_stuck"):
                    logger.warning(
                        f"Task 42: 动态草稿恢复: approved_expired={stuck_drafts.get('approved_expired', 0)}, "
                        f"publishing_stuck={stuck_drafts.get('publishing_stuck', 0)}"
                    )
            except Exception as e:
                logger.warning(f"Task 42: 动态草稿卡死恢复失败: {e}", exc_info=True)

            # 1. 检查评论回复
            await self._check_new_comments()

            # 1.2 PRD V4 REP-005：重试 deferred/retry_wait 的评论
            await self._process_retryable_comments()

            # Prepare one companion motive decision for both proactive gates.
            prepare_tick_motive(self)

            # 1.25 PRD-V5 §10.2 COM-501：重试 retry_wait 的主动评论
            await self._process_retryable_proactive_comments()

            # 1.3 Task 5：重试 retry_wait 的 TaskRun（主动视频/动态等到期自动重试）
            await self._process_retryable_tasks()

            # 1.35 TaskRun watchdog：回收租约过期且协程已死的 running 行
            await self._recover_orphaned_task_runs()

            # 1.5 检查私信
            await self._check_new_messages()

            # 1.6 PRD-V5 §6.3 / PM-501：重试 retry_wait 的私信（独立退避）
            await self._process_retryable_pms()

            # 2. 主动行为
            await self._check_proactive_tasks(current_time)

            # 2.5 陪伴生活层 tick（日程/日记/探索/创作；默认关闭）
            if self.companion is not None and getattr(self.companion, "enabled", False):
                try:
                    # 保持 web_search / draft 引用新鲜（热重载后可能换实例）
                    if getattr(self.companion, "web_search", None) is None:
                        self.companion.web_search = self.web_search
                    if getattr(self.companion, "draft_store", None) is None:
                        try:
                            self.companion.draft_store = self.get_draft_store()
                        except Exception:
                            pass
                    c_result = await self.companion.tick(now)
                    _log_companion_tick(
                        self.account_id,
                        (c_result or {}).get("actions") or [],
                    )
                except Exception as e:
                    logger.warning(
                        "companion tick 失败: %s", type(e).__name__, exc_info=True
                    )

            # 3. 周总结
            await self._check_weekly_summary()

            # 4. 每分钟检查一次
            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logger.info("调度器被取消")
            break
        except Exception as e:
            logger.error(f"主循环异常: {e}", exc_info=True)
            await asyncio.sleep(60)

    # 清理资源
    await self.cleanup()
