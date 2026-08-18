"""Proactive-comment publishing extracted from scheduler.py."""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger("bilibot.scheduler_proactive_publish")


async def do_proactive_comment_publish(
    self,
    bvid: str,
    oid: int,
    title: str,
    owner: str,
    desc: str,
    tags_list: list,
    review: str,
    mood: str,
    video_content: str,
    evaluation: dict,
    llm_ok: bool,
    task_id: str = "",
    memory_evidence: str = "",
    companion_context: str = "",
    memory_event_ids: Optional[List[str]] = None,
) -> str:
    """PRD-V5 §10.2 COM-501：主动评论原子幂等发布流程

    流程：
    1. claim — 同账号同视频只能一个 worker 进入；失败则跳过
    2. 生成文本 → save_generation
    3. CommentPolicy 检查 — 拒绝则 mark_failed(policy_rejected)
    4. 安全检查 — 拒绝则 mark_failed(safety_rejected/rate_limited)
    5. mark_publishing
    6. 调用 B站 send_comment API
    7. 成功 → mark_published
    8. 异常（HTTP 超时/解析失败）→ mark_result_unknown（不自动重发）
    9. API 返回 False → mark_retry_wait（达到 max_attempts 自动转 failed）

    Returns:
        成功发表的评论文本；未发表返回空字符串
    """
    from bilibot.services.proactive_comment_store import (
        default_idempotency_key,
    )

    max_attempts = self._get_proactive_comment_max_attempts()
    persona_id_for_policy = self._get_current_persona_id()
    idem_key = default_idempotency_key(self.account_id or "_default", bvid)

    # 1. 原子 claim
    action = self.proactive_comment_store.claim(
        account_id=self.account_id or "_default",
        bvid=bvid,
        task_id=task_id,
        idempotency_key=idem_key,
        persona_id=persona_id_for_policy,
        max_attempts=max_attempts,
    )
    if action is None:
        logger.info(f"主动评论已由其他 worker claim（account={self.account_id}, bvid={bvid}），跳过")
        return ""
    action_id = action.action_id

    activity_context = await self._begin_activity_context(
        action_key=f"proactive_comment:{action_id}",
        action_type="proactive_comment",
        current_activity=(
            "正在为刚看过的视频准备一条主动评论，必须结合视频内容、刚才的评价和最近经历再决定怎么说。"
        ),
        query=" ".join(
            item
            for item in (
                str(title or ""),
                str(owner or ""),
                str(review or "")[:500],
                str(mood or ""),
                " ".join(str(tag) for tag in (tags_list or [])[:8]),
            )
            if item
        ),
        scene="proactive_video",
        title=title,
        bvid=bvid,
        oid=str(oid),
        metadata={"bvid": bvid, "oid": str(oid), "task_id": task_id},
    )

    # 2. 生成评论（评价阶段已有 short comment 时可复用；否则再生成并混入记忆）
    comment_text = evaluation.get("comment", "") if llm_ok else ""
    mem_ev = str(memory_evidence or "").strip()
    activity_prompt = str(
        getattr(activity_context, "prompt_text", "") or ""
    ).strip()
    if activity_prompt and activity_prompt not in mem_ev:
        mem_ev = "\n\n".join(item for item in (mem_ev, activity_prompt) if item)
    if activity_context is not None:
        for event_id in list(getattr(activity_context, "event_ids", ()) or ()):
            if event_id and event_id not in (memory_event_ids or []):
                if memory_event_ids is None:
                    memory_event_ids = []
                memory_event_ids.append(event_id)
    life_ctx = str(companion_context or "").strip()
    if not life_ctx:
        companion = getattr(self, "companion", None)
        if companion is not None and getattr(companion, "enabled", False):
            try:
                if hasattr(companion, "build_proactive_context_block"):
                    life_ctx = companion.build_proactive_context_block() or ""
            except Exception:
                life_ctx = ""
    if not mem_ev:
        try:
            bundle = await self._recall_for_proactive_video(
                title=title,
                owner=owner,
                tags=tags_list,
                bvid=bvid,
                oid=str(oid),
                desc=desc,
            )
            mem_ev = str(bundle.get("memory_evidence") or "")
            if not memory_event_ids:
                memory_event_ids = list(bundle.get("memory_event_ids") or [])
        except Exception:
            mem_ev = ""

    if not comment_text or len(comment_text) < 5:
        try:
            try:
                comment_text = await self.comment_generator.generate_proactive_comment(
                    title=title,
                    owner=owner,
                    desc=desc,
                    tags=tags_list,
                    review=review,
                    mood=mood,
                    video_content=video_content,
                    companion_context=life_ctx,
                    memory_evidence=mem_ev,
                )
            except TypeError:
                try:
                    comment_text = await self.comment_generator.generate_proactive_comment(
                        title=title,
                        owner=owner,
                        desc=desc,
                        tags=tags_list,
                        review=review,
                        mood=mood,
                        video_content=video_content,
                        companion_context=life_ctx,
                    )
                except TypeError:
                    comment_text = await self.comment_generator.generate_proactive_comment(
                        title=title,
                        owner=owner,
                        desc=desc,
                        tags=tags_list,
                        review=review,
                        mood=mood,
                        video_content=video_content,
                    )
        except Exception as e:
            logger.warning(f"评论生成失败: {e}")
            comment_text = ""

    # PRD V4 COM-003：禁止生成"我完整看完了"等与真实 watch_state 冲突的表达
    # Task 26 增强：可配置短语表 + 大小写不敏感 + 正则变体覆盖（如"完整.*看完"、"看完了?"）
    if comment_text:
        forbidden_phrases, forbidden_patterns = self._get_forbidden_phrases()
        comment_lower = comment_text.lower()
        hit = next(
            (p for p in forbidden_phrases if p.lower() in comment_lower), None
        )
        if hit is None:
            hit_pat = next(
                (p for p in forbidden_patterns if p.search(comment_text)), None
            )
            hit = hit_pat.pattern if hit_pat is not None else None
        if hit:
            logger.warning(f"评论包含与 watch_state 冲突的表达 '{hit}'，拒绝发布")
            comment_text = ""

    if not comment_text:
        self.proactive_comment_store.mark_failed(
            action_id, "NO_COMMENT_TEXT", "评论生成失败或为空",
        )
        return ""

    # 保存生成结果（便于 retry 时复用）；附带 memory event ids 便于审计
    try:
        self.proactive_comment_store.save_generation(action_id, comment_text)
    except TypeError:
        self.proactive_comment_store.save_generation(action_id, comment_text)
    # 将 memory_event_ids 挂到 action 元数据（若 store 支持 patch）
    try:
        patch = getattr(self.proactive_comment_store, "patch_metadata", None)
        if callable(patch) and memory_event_ids:
            patch(action_id, {"memory_event_ids": list(memory_event_ids)[:20]})
    except Exception:
        pass
    # PRD V6：不单独记录 intent，仅在最终结果时归档，避免同一行为两条记忆。

    # 3. PRD V4 COM-002：CommentPolicy 检查（去重 + 预算 + content_hash）
    allowed, policy_reason, policy_meta = await self.comment_policy.check_async(
        bvid=bvid, oid=str(oid), content=comment_text,
        persona_id=persona_id_for_policy, max_per_video=1,
    )
    if not allowed:
        logger.warning(f"主动评论策略拒绝: {policy_reason}")
        self.proactive_comment_store.mark_failed(
            action_id, "POLICY_REJECTED", policy_reason,
        )
        await self._record_proactive_comment_audit(
            persona_id=persona_id_for_policy or "default",
            comment_text=comment_text,
            bvid=bvid,
            oid=oid,
            title=title,
            owner=owner,
            published=False,
            status="failed",
            failure_reason=f"policy_rejected: {policy_reason}",
        )
        await self._archive_bot_action(
            action_key=f"proactive_comment:{action_id}",
            action_type="proactive_comment",
            text=comment_text,
            published=False,
            status="rejected",
            title=title,
            scene="proactive_video",
            metadata={
                "bvid": bvid,
                "oid": str(oid),
                "reason_code": "POLICY_REJECTED",
            },
        )
        return ""

    # 4. PRD §5.9：发布前内容检查 + 频率限制（fail-closed：无 checker 禁止发布）
    if self.safety_checker is None:
        logger.error(
            "safety_checker 未初始化，拒绝主动评论（fail-closed）: action=%s",
            action_id,
        )
        self.proactive_comment_store.mark_failed(
            action_id, "NO_SAFETY_CHECKER", "safety_checker not initialized",
        )
        return ""
    rate_reserved = False
    try:
        passed, reason = await self.safety_checker.check_content(
            comment_text, scene="proactive_comment",
            persona_id=persona_id_for_policy,
            account_id=self.account_id,
        )
        if not passed:
            logger.warning(f"主动评论安全检查未通过: {reason}")
            self.proactive_comment_store.mark_failed(
                action_id, "SAFETY_REJECTED", reason,
            )
            await self._record_proactive_comment_audit(
                persona_id=persona_id_for_policy or "default",
                comment_text=comment_text,
                bvid=bvid,
                oid=oid,
                title=title,
                owner=owner,
                published=False,
                status="failed",
                failure_reason=f"safety_rejected: {reason}",
            )
            await self._archive_bot_action(
                action_key=f"proactive_comment:{action_id}",
                action_type="proactive_comment",
                text=comment_text,
                published=False,
                status="rejected",
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "reason_code": "SAFETY_REJECTED",
                },
            )
            return ""
        # Task 21.1：改用 check_and_record_rate_limit 原子方法（避免竞态条件）
        # Task 21.2：原子方法为预扣减设计，post_comment 失败时需退回配额
        rate_passed, rate_reason = self.safety_checker.check_and_record_rate_limit(
            scene="proactive_comment", account_id=self.account_id,
        )
        if not rate_passed:
            logger.warning(f"主动评论频率限制触发，延后重试: {rate_reason}")
            # 限流是临时条件，不得消耗 attempt 预算 / 永久 failed
            self.proactive_comment_store.mark_retry_wait(
                action_id, "RATE_LIMITED", "proactive_comment rate limited",
                increment_attempt=False,
            )
            return ""
        rate_reserved = True
    except Exception as e:
        # PRD V4 DYN-003 / §4.2：fail-closed
        logger.error(f"主动评论安全检查异常（拒绝发布）: {e}", exc_info=True)
        self.proactive_comment_store.mark_failed(
            action_id, "SAFETY_CHECK_ERROR", str(e),
        )
        return ""

    # 5. mark_publishing（claimed → publishing）
    if not self.proactive_comment_store.mark_publishing(action_id):
        logger.warning(f"主动评论动作 {action_id} mark_publishing 失败")
        if rate_reserved and self.safety_checker is not None:
            try:
                self.safety_checker.refund_publish(
                    scene="proactive_comment", account_id=self.account_id,
                )
            except Exception:
                pass
        return ""

    # PRD 3.5 / COM-004：审计记录（评论页与 reply_comment 一并展示）
    # 必须在调用平台 API 前落库；若写失败，成功后仍会补写一条 published 审计。
    audit_id = await self._record_proactive_comment_audit(
        persona_id=persona_id_for_policy or "default",
        comment_text=comment_text,
        bvid=bvid,
        oid=oid,
        title=title,
        owner=owner,
        status="publishing",
    )

    # 6. 调用 B站 API 发布
    try:
        success = await self.bili.post_comment(
            oid=oid,
            content=comment_text,
            comment_type=1,
            rpid=0,
            parent=0,
        )
    except Exception as e:
        # PRD-V5 §10.2 COM-501：HTTP 异常 → 平台可能已收到 → result_unknown
        # 配额策略：结果不确定时不退还（可能已发出）
        logger.error(f"评论发表异常（平台可能已收到）: {e}", exc_info=True)
        self.proactive_comment_store.mark_result_unknown(
            action_id, "PUBLISH_EXCEPTION", str(e),
        )
        try:
            await self._archive_bot_action(
                action_key=f"proactive_comment:{action_id}",
                action_type="proactive_comment",
                text=comment_text,
                published=False,
                status="result_unknown",
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "reason_code": "PUBLISH_EXCEPTION",
                },
            )
        except Exception:
            logger.error(
                "unknown proactive comment result could not be archived: action=%s",
                action_id,
            )
        try:
            await self.comment_policy.record_async(
                bvid=bvid, oid=str(oid), content=comment_text,
                persona_id=persona_id_for_policy,
                published=False, failure_reason=str(e),
            )
            await self.interaction_policy.record_result_async(
                "comment", bvid, str(oid), "failed", failure_reason=str(e),
                content_hash=self.comment_policy.content_hash(comment_text),
            )
        except Exception:
            pass
        self._finalize_proactive_comment_audit(
            audit_id,
            published=False,
            failure_reason=str(e),
            status="result_unknown",
        )
        return ""

    if success is None:
        logger.error("主动评论结果不确定（不自动重发）: action=%s", action_id)
        self.proactive_comment_store.mark_result_unknown(
            action_id, "RESULT_UNKNOWN", "post_comment transport uncertainty",
        )
        try:
            await self._archive_bot_action(
                action_key=f"proactive_comment:{action_id}",
                action_type="proactive_comment",
                text=comment_text,
                published=False,
                status="result_unknown",
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "reason_code": "RESULT_UNKNOWN",
                },
            )
        except Exception:
            logger.error(
                "unknown proactive comment result could not be archived: action=%s",
                action_id,
            )
        self._finalize_proactive_comment_audit(
            audit_id,
            published=False,
            failure_reason="post_comment transport uncertainty",
            status="result_unknown",
        )
        return ""

    if success is False:
        self._check_bili_risk_control("proactive_comment")

    if success:
        # COM-603：先 mark_published，成功后再记录 policy（避免 mark_published 失败时 policy 已记录）
        # Task 4：检查 mark_published 返回值，失败时告警（可能需人工介入）
        if not self.proactive_comment_store.mark_published(action_id):
            logger.warning(
                f"Task 4: 主动评论 mark_published 失败 action={action_id}（状态可能已变更）"
            )
        logger.info(f"主动评论发表: {comment_text[:30]}")
        # 记录 CommentPolicy / InteractionPolicy
        try:
            await self.comment_policy.record_async(
                bvid=bvid, oid=str(oid), content=comment_text,
                persona_id=persona_id_for_policy,
                published=True,
            )
            await self.interaction_policy.record_result_async(
                "comment", bvid, str(oid), "success",
                api_code=getattr(self.bili, "last_api_code", None),
                content_hash=self.comment_policy.content_hash(comment_text),
            )
        except Exception:
            pass
        if self.safety_checker is not None:
            try:
                self.safety_checker.record_content(comment_text, account_id=self.account_id)
            except Exception:
                pass
        # 评论页依赖 audit：pre-publish 写入失败时在成功路径补写，避免“已发出但页面没有”
        if audit_id:
            self._finalize_proactive_comment_audit(audit_id, published=True)
        else:
            await self._record_proactive_comment_audit(
                persona_id=persona_id_for_policy or "default",
                comment_text=comment_text,
                bvid=bvid,
                oid=oid,
                title=title,
                owner=owner,
                published=True,
                status="published",
            )
        try:
            await self._archive_bot_action(
                action_key=f"proactive_comment:{action_id}",
                action_type="proactive_comment",
                text=comment_text,
                published=True,
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "memory_event_ids": list(memory_event_ids or [])[:20],
                    "memory_grounded": bool(mem_ev),
                },
            )
        except Exception:
            logger.error(
                "published proactive comment result could not be archived: action=%s",
                action_id,
            )
        else:
            self._notify_companion_comment_replied(
                title=str(title or "")[:40],
                preview=str(comment_text or "")[:80],
                proactive=True,
            )
        return comment_text
    else:
        # 9. API 返回 False → retry_wait（达 max_attempts 自动转 failed）
        logger.warning("主动评论发表失败")
        # 记录 CommentPolicy / InteractionPolicy
        try:
            await self.comment_policy.record_async(
                bvid=bvid, oid=str(oid), content=comment_text,
                persona_id=persona_id_for_policy,
                published=False,
                failure_reason="bili_api_false",
            )
            await self.interaction_policy.record_result_async(
                "comment", bvid, str(oid), "failed",
                api_code=getattr(self.bili, "last_api_code", None),
                failure_reason="bili_api_false",
                content_hash=self.comment_policy.content_hash(comment_text),
            )
        except Exception:
            pass
        self.proactive_comment_store.mark_retry_wait(
            action_id, "BILI_API_FALSE",
            f"bili.post_comment 返回 False (code={getattr(self.bili, 'last_api_code', None)})",
        )
        try:
            await self._archive_bot_action(
                action_key=f"proactive_comment:{action_id}",
                action_type="proactive_comment",
                text=comment_text,
                published=False,
                status="failed",
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "reason_code": "BILI_API_FALSE",
                    "memory_event_ids": list(memory_event_ids or [])[:20],
                },
            )
        except Exception:
            logger.error(
                "failed proactive comment result could not be archived: action=%s",
                action_id,
            )
        # Task 21.2：发布失败退回预占的频率配额（原子方法预扣减，失败时退回）
        if self.safety_checker is not None:
            try:
                self.safety_checker.refund_publish(
                    scene="proactive_comment", account_id=self.account_id,
                )
            except Exception:
                pass
        self._finalize_proactive_comment_audit(
            audit_id,
            published=False,
            failure_reason="bili_api_error",
        )
        return ""
