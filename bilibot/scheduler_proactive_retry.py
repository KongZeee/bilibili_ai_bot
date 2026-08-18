"""Proactive-comment retry processing extracted from scheduler.py."""

from __future__ import annotations

import logging

logger = logging.getLogger("bilibot.scheduler_proactive_retry")


async def process_retryable_proactive_comments(self):
    """PRD-V5 §10.2 COM-501：重试 retry_wait 的主动评论

    - 只复用已生成的文本（不重新生成），保证内容一致性
    - mark_publishing（retry_wait → publishing）→ 调 API → published / retry_wait / result_unknown
    - 已 published / failed / result_unknown 的不重试
    """
    if not self.bili:
        return
    # P3-10：rest 高分时已排队的主动评论也不应照常发布，
    # 与 _check_proactive_tasks 共用同一 scheduler-tick 的动机快照。
    from bilibot.scheduler_motive import get_tick_motive

    motive = get_tick_motive(self)
    if motive is not None and motive.rest_gated:
        return
    try:
        pending = self.proactive_comment_store.list_pending_retry(
            account_id=self.account_id or "_default",
        )
        if not pending:
            return
        logger.info(f"发现 {len(pending)} 条待重试主动评论")
        for action in pending:
            if self.proactive_comment_store.has_published(
                action.account_id, action.bvid,
            ):
                # 已发布（可能另一 worker 成功）→ 跳过
                continue
            reply_text = action.generation_text or ""
            if not reply_text:
                self.proactive_comment_store.mark_failed(
                    action.action_id, "NO_GEN_TEXT", "重试时无生成文本",
                )
                continue
            # 校验 hash 完整性
            from bilibot.services.proactive_comment_store import (
                compute_generation_hash,
            )
            if action.generation_hash:
                expected = compute_generation_hash(reply_text)
                if expected != action.generation_hash:
                    logger.warning(
                        f"主动评论重试文本 hash 不匹配 action={action.action_id}"
                    )
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "GEN_HASH_MISMATCH",
                        "重试文本 hash 不匹配",
                    )
                    continue
            # oid 反查（在策略/安全检查前完成）
            oid = await self._resolve_oid_from_bvid(action.bvid)
            if not oid:
                self.proactive_comment_store.mark_failed(
                    action.action_id, "NO_OID",
                    f"无法解析 bvid={action.bvid} 的 oid",
                )
                continue

            # COM-601：先完成策略/安全，再 mark_publishing，避免检查失败后卡在 publishing
            allowed, policy_reason, _ = await self.comment_policy.check_async(
                bvid=action.bvid, oid=str(oid), content=reply_text,
                persona_id=action.persona_id or "", max_per_video=1,
            )
            if not allowed:
                logger.warning(f"重试主动评论策略拒绝: {policy_reason}")
                self.proactive_comment_store.mark_failed(
                    action.action_id, "POLICY_REJECTED", policy_reason,
                )
                continue
            if self.safety_checker is None:
                logger.error(
                    "safety_checker 未初始化，拒绝重试主动评论（fail-closed）: action=%s",
                    action.action_id,
                )
                self.proactive_comment_store.mark_failed(
                    action.action_id, "NO_SAFETY_CHECKER",
                    "safety_checker not initialized",
                )
                continue
            rate_reserved = False
            try:
                ok, sreason = await self.safety_checker.check_content(
                    reply_text, scene="proactive_comment",
                    persona_id=action.persona_id or "",
                    account_id=self.account_id,
                )
                if not ok:
                    logger.warning(f"重试主动评论安全检查未通过: {sreason}")
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "SAFETY_REJECTED", sreason,
                    )
                    continue
                rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                    scene="proactive_comment", account_id=self.account_id,
                )
                if not rate_ok:
                    logger.warning("重试主动评论频率限制触发: %s", rate_reason)
                    # 保持 retry_wait 且不烧 attempt，避免限流导致永久 failed
                    self.proactive_comment_store.mark_retry_wait(
                        action.action_id, "RATE_LIMITED",
                        "proactive_comment rate limited",
                        increment_attempt=False,
                    )
                    continue
                rate_reserved = True
            except Exception as se:
                logger.error(f"重试主动评论安全检查异常（拒绝发布）: {se}", exc_info=True)
                self.proactive_comment_store.mark_failed(
                    action.action_id, "SAFETY_CHECK_ERROR", str(se),
                )
                continue

            # retry_wait → publishing（检查通过后再 claim）
            if not self.proactive_comment_store.mark_publishing(action.action_id):
                if rate_reserved and self.safety_checker is not None:
                    try:
                        self.safety_checker.refund_publish(
                            scene="proactive_comment", account_id=self.account_id,
                        )
                    except Exception:
                        pass
                continue

            audit_id = await self._record_proactive_comment_audit(
                persona_id=action.persona_id or "default",
                comment_text=reply_text,
                bvid=action.bvid,
                oid=oid,
                input_summary=f"主动评论重试 · bvid={action.bvid}",
                context_summary=f"主动评论重试发布 bvid={action.bvid}",
                prompt_preview=f"retry bvid={action.bvid}",
                status="publishing",
            )

            try:
                success = await self.bili.post_comment(
                    oid=oid, content=reply_text,
                    comment_type=1, rpid=0, parent=0,
                )
            except Exception as e:
                logger.error(f"重试主动评论异常 action={action.action_id}: {e}")
                # 结果不确定时不退配额（可能已发出）
                self.proactive_comment_store.mark_result_unknown(
                    action.action_id, "RETRY_PUBLISH_EXCEPTION", str(e),
                )
                try:
                    await self._archive_bot_action(
                        action_key=(
                            f"proactive_comment:{action.action_id}:"
                            f"retry:{action.attempt + 1}"
                        ),
                        action_type="proactive_comment",
                        text=reply_text,
                        published=False,
                        status="result_unknown",
                        title=action.bvid,
                        scene="proactive_video",
                        metadata={
                            "bvid": action.bvid,
                            "reason_code": "RETRY_PUBLISH_EXCEPTION",
                        },
                    )
                except Exception:
                    logger.error(
                        "unknown retried proactive comment result could not be archived: action=%s",
                        action.action_id,
                    )
                self._finalize_proactive_comment_audit(
                    audit_id,
                    published=False,
                    failure_reason=str(e),
                    status="result_unknown",
                )
                continue

            if success is None:
                logger.error(
                    "重试主动评论结果不确定（不自动重发）: action=%s",
                    action.action_id,
                )
                self.proactive_comment_store.mark_result_unknown(
                    action.action_id, "RESULT_UNKNOWN",
                    "post_comment transport uncertainty",
                )
                try:
                    await self._archive_bot_action(
                        action_key=(
                            f"proactive_comment:{action.action_id}:"
                            f"retry:{action.attempt + 1}"
                        ),
                        action_type="proactive_comment",
                        text=reply_text,
                        published=False,
                        status="result_unknown",
                        title=action.bvid,
                        scene="proactive_video",
                        metadata={
                            "bvid": action.bvid,
                            "oid": str(oid),
                            "reason_code": "RESULT_UNKNOWN",
                        },
                    )
                except Exception:
                    logger.error(
                        "unknown retried proactive comment result could not be archived: action=%s",
                        action.action_id,
                    )
                self._finalize_proactive_comment_audit(
                    audit_id,
                    published=False,
                    failure_reason="post_comment transport uncertainty",
                    status="result_unknown",
                )
                continue

            if success:
                if not self.proactive_comment_store.mark_published(action.action_id):
                    logger.warning(
                        f"Task 4: 重试主动评论 mark_published 失败 action={action.action_id}（状态可能已变更）"
                    )
                logger.info(f"主动评论重试成功 action={action.action_id}")
                try:
                    await self.comment_policy.record_async(
                        bvid=action.bvid, oid=str(oid), content=reply_text,
                        persona_id=action.persona_id or "",
                        published=True,
                    )
                    await self.interaction_policy.record_result_async(
                        "comment", action.bvid, str(oid), "success",
                        content_hash=self.comment_policy.content_hash(reply_text),
                    )
                except Exception:
                    pass
                if audit_id:
                    self._finalize_proactive_comment_audit(audit_id, published=True)
                else:
                    await self._record_proactive_comment_audit(
                        persona_id=action.persona_id or "default",
                        comment_text=reply_text,
                        bvid=action.bvid,
                        oid=oid,
                        input_summary=f"主动评论重试 · bvid={action.bvid}",
                        context_summary=f"主动评论重试发布 bvid={action.bvid}",
                        prompt_preview=f"retry bvid={action.bvid}",
                        published=True,
                        status="published",
                    )
                try:
                    await self._archive_bot_action(
                        action_key=f"proactive_comment:{action.action_id}",
                        action_type="proactive_comment",
                        text=reply_text,
                        published=True,
                        title=action.bvid,
                        scene="proactive_video",
                        metadata={"bvid": action.bvid, "oid": str(oid)},
                    )
                except Exception:
                    logger.error(
                        "retried proactive comment result could not be archived: action=%s",
                        action.action_id,
                    )
                else:
                    self._notify_companion_comment_replied(
                        title=str(action.bvid or "")[:40],
                        preview=str(reply_text or "")[:80],
                        proactive=True,
                    )
            else:
                self.proactive_comment_store.mark_retry_wait(
                    action.action_id, "RETRY_PUBLISH_FAILED",
                    "重试发布失败：bili.post_comment 返回 False",
                )
                if rate_reserved and self.safety_checker is not None:
                    try:
                        self.safety_checker.refund_publish(
                            scene="proactive_comment", account_id=self.account_id,
                        )
                    except Exception:
                        pass
                try:
                    await self._archive_bot_action(
                        action_key=(
                            f"proactive_comment:{action.action_id}:"
                            f"retry:{action.attempt + 1}"
                        ),
                        action_type="proactive_comment",
                        text=reply_text,
                        published=False,
                        status="failed",
                        title=action.bvid,
                        scene="proactive_video",
                        metadata={
                            "bvid": action.bvid,
                            "oid": str(oid),
                            "reason_code": "RETRY_PUBLISH_FAILED",
                        },
                    )
                except Exception:
                    logger.error(
                        "failed retried proactive comment result could not be archived: action=%s",
                        action.action_id,
                    )
                self._finalize_proactive_comment_audit(
                    audit_id,
                    published=False,
                    failure_reason="retry_bili_api_error",
                )
                logger.warning(f"主动评论重试失败 action={action.action_id}")

    except Exception as e:
        logger.error(f"处理重试主动评论失败: {e}", exc_info=True)
