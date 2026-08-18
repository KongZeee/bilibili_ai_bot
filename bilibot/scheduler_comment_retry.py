"""Comment retry processing extracted from scheduler.py."""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("bilibot.scheduler_comment_retry")


async def process_retryable_comments(self):
    """PRD V4 REP-005：重试 deferred/retry_wait 状态的评论

    - retry_wait：直接重新发布（已有生成结果）
    - deferred：重新走完整流程（LLM 可能已恢复）
    - 重试前查询本地终态，避免重复回复
    """
    if not self.bili or not self.reply_gen:
        return
    # S3：_bot_uid 为空 fail-closed，禁止重试发布（避免无法做幂等/自回识别）
    if not str(getattr(self, "_bot_uid", "") or "").strip():
        logger.error(
            "_bot_uid 为空，跳过评论重试（fail-closed，防止自回/幂等失效）"
        )
        return
    try:
        retryable = self.reply_state_store.get_retryable()
        if not retryable:
            return
        logger.info(f"发现 {len(retryable)} 条待重试评论")
        for item_state in retryable:
            ct = int(item_state.get("comment_type", 1))
            rpid = str(item_state.get("source_rpid", ""))
            state = item_state.get("state", "")
            if not rpid:
                continue
            # PRD REP-005：重试前查询本地终态，避免重复回复
            if self.reply_state_store.is_terminal(ct, rpid):
                continue
            # get_retryable 安全网可能返回卡在中间态的行；处理器只处理
            # retry_wait/deferred。有 generation_result 时转 retry_wait 复用原文
            # （与 recover_stuck_intermediate 一致），避免 deferred 重生导致双发。
            if state not in ("retry_wait", "deferred"):
                gen_existing = (item_state.get("generation_result") or "").strip()
                if gen_existing and state == "publish_pending":
                    logger.warning(
                        "retryable 中间态 %s 已有生成文本，转为 retry_wait: rpid=%s",
                        state, rpid,
                    )
                    self.reply_state_store.mark_retry_wait(
                        ct, rpid,
                        reason=f"normalize_from_{state}_keep_gen",
                        error_code="STUCK_NORMALIZE_RETRY",
                        increment_attempt=False,
                    )
                    state = "retry_wait"
                    item_state["state"] = "retry_wait"
                else:
                    logger.warning(
                        "retryable 含未处理中间态 %s，转为 deferred: rpid=%s",
                        state, rpid,
                    )
                    self.reply_state_store.mark_deferred(
                        ct, rpid,
                        reason=f"normalize_from_{state}",
                        error_code="STUCK_NORMALIZE",
                        increment_attempt=False,
                    )
                    state = "deferred"
                    item_state["state"] = "deferred"
            try:
                # 从状态记录恢复原始通知
                notif_json = item_state.get("notification_json", "")
                notif = json.loads(notif_json) if notif_json else None
                if not notif:
                    # 无原始通知，无法重试，标记失败
                    self.reply_state_store.upsert(ct, rpid, "failed",
                                                  error="no_notification_for_retry")
                    continue

                item_detail = notif.get("item", {})
                oid = item_detail.get("subject_id", 0)
                root_id = item_detail.get("root_id", 0)
                source_id = item_detail.get("source_id", 0)
                comment_text = item_detail.get("source_content", "")
                user = notif.get("user", {})
                user_id = str(user.get("mid", ""))
                username = user.get("nickname", "未知用户")

                if state == "retry_wait":
                    # PRD-V5 §6.2 / REP-502：使用原始生成文本重试，不重新生成
                    gen_result = item_state.get("generation_result", "")
                    gen_hash = item_state.get("generation_hash", "")
                    reply_text = gen_result if gen_result else ""
                    # 校验文本完整性：有文本且（无 hash 记录或 hash 匹配）才重试
                    text_valid = bool(reply_text)
                    if text_valid and gen_hash:
                        expected_hash = self.reply_state_store.compute_generation_hash(reply_text)
                        if expected_hash != gen_hash:
                            logger.warning(
                                f"重试文本 hash 不匹配，文本可能被篡改，转为 deferred: rpid={rpid}"
                            )
                            text_valid = False
                    if not text_valid:
                        # P1-14：无生成结果或 hash 失效 → deferred 重生，不烧 attempt
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason="no_generation_result",
                            error_code="RETRY_NO_GEN",
                            increment_attempt=False,
                        )
                        continue
                    # 幂等检查：按 parent/source_rpid 判断是否已回该条（避免楼中楼漏回）
                    comment_root = root_id if root_id else source_id
                    if self._bot_uid and self.bili:
                        try:
                            replies_data = await self.bili.get_comment_replies(
                                oid=oid, root=comment_root, comment_type=ct, ps=30,
                            )
                            existing = (
                                replies_data.get("data", {}).get("replies", [])
                                if replies_data and replies_data.get("code") == 0
                                else []
                            )
                            already_replied = self._bot_already_replied_to_source(
                                existing,
                                source_rpid=str(source_id or rpid),
                                expected_text=reply_text,
                            )
                            if already_replied:
                                logger.info(f"幂等检查：rpid={rpid} 已有 Bot 回复，跳过重试")
                                self.reply_state_store.mark_published(ct, rpid)
                                await self._archive_bot_action(
                                    action_key=f"comment_reply:{ct}:{rpid}",
                                    action_type="reply_comment",
                                    text=reply_text,
                                    published=True,
                                    title=f"已回复评论 {rpid}",
                                    scene="reply_comment",
                                    metadata={
                                        "reply_id": rpid,
                                        "oid": str(oid),
                                        "confirmed_by": "thread_lookup",
                                    },
                                )
                                self._notify_companion_comment_replied(
                                    title=str(oid or "")[:40],
                                    preview=str(reply_text or "")[:80],
                                    proactive=False,
                                )
                                continue
                        except Exception as e:
                            logger.warning(f"幂等检查失败，继续重试: {e}")
                    # Task 9：重试路径也需原子预占配额（与主路径一致）；无 checker 时 fail-closed
                    if self.safety_checker is None:
                        logger.error(
                            "safety_checker 未初始化，拒绝重试发布评论（fail-closed）: rpid=%s",
                            rpid,
                        )
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason="safety_checker_missing", error_code="NO_SAFETY_CHECKER",
                            increment_attempt=False,
                        )
                        continue
                    # 策略/规则可能在生成后变严，重试前重新内容安全检查
                    try:
                        passed, reason = await self.safety_checker.check_content(
                            reply_text, scene="reply_comment",
                            persona_id=self._get_current_persona_id(),
                            account_id=self.account_id,
                        )
                        if not passed:
                            logger.warning(f"重试路径评论安全检查未通过: {reason}")
                            self.reply_state_store.mark_rejected(
                                ct, rpid, reason=f"safety_check: {reason}",
                            )
                            continue
                    except Exception as se:
                        logger.error(f"重试路径安全检查异常: {se}", exc_info=True)
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason=f"safety_exception: {se}", error_code="SAFETY_ERROR",
                            increment_attempt=False,
                        )
                        continue
                    rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                        scene="reply_comment", account_id=self.account_id,
                    )
                    if not rate_ok:
                        logger.warning("重试路径评论发布频率限制触发，deferred: %s", rate_reason)
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason="rate_limited", error_code="RATE_LIMIT",
                            increment_attempt=False,
                        )
                        continue
                    rate_reserved = True
                    # 直接使用原始文本重新发布（不调用 generate_reply）
                    await self._archive_bot_action(
                        action_key=(
                            f"comment_reply:{ct}:{rpid}:"
                            f"retry_intent:{int(item_state.get('attempts') or 0) + 1}"
                        ),
                        action_type="reply_comment",
                        text=reply_text,
                        published=False,
                        status="intent",
                        title=f"回复评论 {rpid}",
                        scene="reply_comment",
                        metadata={"reply_id": rpid, "oid": str(oid)},
                    )
                    try:
                        success = await self.bili.post_comment(
                            oid=oid, content=reply_text, comment_type=ct,
                            rpid=comment_root, parent=source_id,
                        )
                    except Exception as post_err:
                        logger.error(f"重试发表评论异常 rpid={rpid}: {post_err}")
                        self.reply_state_store.mark_result_unknown(
                            ct, rpid,
                            reason=f"post_exception: {type(post_err).__name__}",
                            error_code="POST_EXCEPTION",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"comment_reply:{ct}:{rpid}:"
                                    f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                ),
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="result_unknown",
                                title=f"回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": rpid,
                                    "oid": str(oid),
                                    "reason_code": "POST_EXCEPTION",
                                },
                            )
                        except Exception:
                            logger.error(
                                "unknown retried comment result could not be archived: rpid=%s",
                                rpid,
                            )
                        continue
                    if success is None:
                        logger.error("重试评论结果不确定（不自动重发）: rpid=%s", rpid)
                        self.reply_state_store.mark_result_unknown(
                            ct, rpid,
                            reason="post_comment transport uncertainty",
                            error_code="RESULT_UNKNOWN",
                        )
                        continue
                    if success:
                        # PRD-V5 §6.2：发布成功保留同一 generation_hash
                        self.reply_state_store.mark_published(ct, rpid)
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{ct}:{rpid}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=True,
                            title=f"已回复评论 {rpid}",
                            scene="reply_comment",
                            metadata={"reply_id": rpid, "oid": str(oid)},
                        )
                        self._notify_companion_comment_replied(
                            title=str(oid or "")[:40],
                            preview=str(reply_text or "")[:80],
                            proactive=False,
                        )
                        logger.info(f"重试发布成功: rpid={rpid}")
                    else:
                        if rate_reserved and self.safety_checker is not None:
                            try:
                                self.safety_checker.refund_publish(
                                    scene="reply_comment", account_id=self.account_id,
                                )
                            except Exception:
                                pass
                        # 再次失败 → retry_wait（attempts 自动递增，超限转 failed）
                        # generation 字段由 upsert 自动保留。
                        # B站 12002（评论功能已关闭）是永久失败，直接终态 failed。
                        last_code = int(getattr(self.bili, "last_api_code", 0) or 0)
                        if last_code == 12002:
                            self.reply_state_store.mark_failed(
                                ct, rpid,
                                reason="comment section closed (B站 12002)",
                                error_code="PERMANENT_PUBLISH_REJECTED",
                            )
                            reason_code = "PERMANENT_PUBLISH_REJECTED"
                        else:
                            self.reply_state_store.mark_retry_wait(
                                ct, rpid, reason="retry_publish_failed", error_code="RETRY_PUBLISH_FAILED",
                            )
                            reason_code = "RETRY_PUBLISH_FAILED"
                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"comment_reply:{ct}:{rpid}:"
                                    f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                ),
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="failed",
                                title=f"回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": rpid,
                                    "oid": str(oid),
                                    "reason_code": reason_code,
                                },
                            )
                        except Exception:
                            logger.error(
                                "failed retried comment result could not be archived: rpid=%s",
                                rpid,
                            )
                        logger.warning(f"重试发布失败: rpid={rpid}")
                elif state == "deferred":
                    # BUG A-002：不再转 discovered（is_processed 会去重导致死状态），
                    # 而是在重试流程内直接走完整重生成→发帖/继续defer流程
                    logger.info(f"deferred 评论重试生成: rpid={rpid}")

                    # 获取评论上下文（楼中楼对话历史）
                    comment_context = ""
                    reply_replies = []
                    context_root = root_id if root_id else source_id
                    if root_id or source_id:
                        try:
                            replies_data = await self.bili.get_comment_replies(
                                oid=oid, root=context_root, comment_type=ct, ps=30,
                            )
                            if replies_data and replies_data.get("code") == 0:
                                reply_replies = replies_data.get("data", {}).get("replies", [])
                        except Exception as ctx_err:
                            logger.warning(f"deferred 重试获取上下文失败: {ctx_err}")
                    if reply_replies:
                        try:
                            context_lines = await self._archive_comment_thread_context(
                                reply_replies,
                                oid=oid,
                                comment_type=ct,
                                thread_key=context_root,
                            )
                        except Exception:
                            self.reply_state_store.mark_deferred(
                                ct,
                                rpid,
                                reason="comment_context_archive_failed",
                                error_code="MEMORY_ARCHIVE_FAILED",
                                increment_attempt=False,
                            )
                            continue
                        comment_context = "\n".join(context_lines)

                    # 构建 ReplyContext（用于 LLM 生成）
                    reply_context = None
                    if self.comment_context_service is not None:
                        try:
                            reply_context = await self.comment_context_service.build_context(
                                notification=notif,
                                current_user_id=user_id,
                                persona_id=self._get_current_persona_id(),
                                recent_turns=self._bounded_recent_turns(
                                    comment_context.splitlines()
                                ),
                            )
                        except Exception as e:
                            from bilibot.services.comment_context import ContextArchiveError

                            if isinstance(e, ContextArchiveError):
                                self._pause_for_memory_failure()
                                self.reply_state_store.mark_deferred(
                                    ct,
                                    rpid,
                                    reason="video_context_archive_failed",
                                    error_code="MEMORY_ARCHIVE_FAILED",
                                    increment_attempt=False,
                                )
                                continue
                            logger.warning(f"deferred 重试构建上下文失败，降级: {e}")

                    # BUG A-001 配套：直接调用 _generate_reply_impl 获取 GenerationOutcome
                    try:
                        outcome = await self.reply_gen._generate_reply_impl(
                            user_id=user_id,
                            username=username,
                            comment=comment_text,
                            thread_id=str(rpid),
                            oid=oid,
                            comment_type=ct,
                            reply_context=reply_context,
                            comment_context=comment_context,
                        )
                    except Exception as gen_err:
                        logger.error(f"deferred 重试生成异常: {gen_err}")
                        _err_name = type(gen_err).__name__
                        _no_burn = (
                            _err_name == "RateLimitExhaustedError"
                            or "rate limit" in str(gen_err).lower()
                            or "rate-limited" in str(gen_err).lower()
                        )
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason=f"retry_gen_exception: {gen_err}",
                            error_code=(
                                "LLM_RATE_LIMITED" if _no_burn else "RETRY_GEN_EXCEPTION"
                            ),
                            increment_attempt=not _no_burn,
                        )
                        continue

                    if outcome.is_skip:
                        # LLM 明确不回复 → ignored
                        self.reply_state_store.mark_ignored(
                            ct, rpid, rule="llm_no_reply_retry",
                        )
                        continue

                    if not outcome.is_generated:
                        if getattr(outcome, "is_permanent_error", False):
                            self.reply_state_store.mark_failed(
                                ct, rpid,
                                reason=f"retry_gen_permanent: {outcome.error_code}",
                                error_code=outcome.error_code or "GEN_PERMANENT",
                            )
                            continue
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason=f"retry_gen_{outcome.status}: {outcome.error_code}",
                            error_code=outcome.error_code or "RETRY_GEN_FAILED",
                            increment_attempt=(
                                (outcome.error_code or "")
                                not in ("LLM_RATE_LIMITED", "RATE_LIMIT", "RATE_LIMITED")
                            ),
                        )
                        continue

                    reply_text = outcome.text
                    audit_id = outcome.audit_id
                    context_meta = outcome.context_meta or {}

                    try:
                        await self._archive_bot_action(
                            action_key=(
                                f"comment_reply:{ct}:{rpid}:"
                                f"generation:{int(item_state.get('attempts') or 0) + 1}"
                            ),
                            action_type="reply_comment",
                            text=reply_text,
                            published=False,
                            status="intent",
                            title=f"回复评论 {rpid}",
                            scene="reply_comment",
                            metadata={"reply_id": rpid, "oid": str(oid)},
                        )
                    except Exception:
                        self.reply_state_store.mark_deferred(
                            ct,
                            rpid,
                            reason="memory_intent_archive_failed",
                            error_code="MEMORY_ARCHIVE_FAILED",
                            increment_attempt=False,
                        )
                        continue

                    # 持久化生成结果
                    self.reply_state_store.save_generation_result(
                        ct, rpid,
                        text=reply_text,
                        persona_id=context_meta.get("persona_id", "") or self._get_current_persona_id() or "",
                        audit_id=audit_id,
                    )

                    # 安全检查（fail-closed：无 checker 禁止发布）
                    if self.safety_checker is None:
                        logger.error(
                            "safety_checker 未初始化，拒绝 deferred 发布（fail-closed）: rpid=%s",
                            rpid,
                        )
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason="safety_checker_missing", error_code="NO_SAFETY_CHECKER",
                            increment_attempt=False,
                        )
                        continue
                    rate_reserved = False
                    try:
                        passed, reason = await self.safety_checker.check_content(
                            reply_text, scene="reply_comment",
                            persona_id=self._get_current_persona_id(),
                            account_id=self.account_id,
                        )
                        if not passed:
                            if audit_id and self.audit_store:
                                try:
                                    self.audit_store.mark_published(
                                        audit_id, published=False,
                                        target={
                                            "kind": "reply_comment",
                                            "rpid": str(rpid),
                                            "source_rpid": str(rpid),
                                            "comment_type": int(ct),
                                            "account_id": self.account_id or "",
                                        },
                                        failure_reason=f"safety_check: {reason}",
                                    )
                                except Exception:
                                    pass
                            self.reply_state_store.mark_rejected(
                                ct, rpid, reason=f"safety_check: {reason}",
                            )
                            continue
                        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                            scene="reply_comment", account_id=self.account_id,
                        )
                        if not rate_ok:
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason="rate_limited_retry", error_code="RATE_LIMIT",
                                increment_attempt=False,
                            )
                            continue
                        rate_reserved = True
                    except Exception as safety_err:
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason=f"safety_exception: {safety_err}",
                            error_code="SAFETY_ERROR",
                            increment_attempt=False,
                        )
                        continue

                    # 发布回复前幂等检查：按 parent/source_rpid 匹配，避免楼中楼漏回
                    comment_root = root_id if root_id else source_id
                    if self._bot_uid and self.bili:
                        try:
                            replies_data = await self.bili.get_comment_replies(
                                oid=oid, root=comment_root, comment_type=ct, ps=30,
                            )
                            existing = (
                                replies_data.get("data", {}).get("replies", [])
                                if replies_data and replies_data.get("code") == 0
                                else []
                            )
                            already_replied = self._bot_already_replied_to_source(
                                existing,
                                source_rpid=str(source_id or rpid),
                                expected_text=reply_text,
                            )
                            if already_replied:
                                logger.info(
                                    f"幂等检查：rpid={rpid} 已有 Bot 回复，跳过 deferred 重发"
                                )
                                if rate_reserved and self.safety_checker is not None:
                                    try:
                                        self.safety_checker.refund_publish(
                                            scene="reply_comment",
                                            account_id=self.account_id,
                                        )
                                    except Exception:
                                        pass
                                self.reply_state_store.mark_published(ct, rpid)
                                await self._archive_bot_action(
                                    action_key=f"comment_reply:{ct}:{rpid}",
                                    action_type="reply_comment",
                                    text=reply_text,
                                    published=True,
                                    title=f"已回复评论 {rpid}",
                                    scene="reply_comment",
                                    metadata={
                                        "reply_id": rpid,
                                        "oid": str(oid),
                                        "confirmed_by": "thread_lookup",
                                    },
                                )
                                self._notify_companion_comment_replied(
                                    title=str(oid or "")[:40],
                                    preview=str(reply_text or "")[:80],
                                    proactive=False,
                                )
                                continue
                        except Exception as e:
                            logger.warning(f"deferred 幂等检查失败，继续发布: {e}")
                    try:
                        success = await self.bili.post_comment(
                            oid=oid, content=reply_text, comment_type=ct,
                            rpid=comment_root, parent=source_id,
                        )
                    except Exception as post_err:
                        self.reply_state_store.mark_result_unknown(
                            ct, rpid,
                            reason=f"deferred_retry_post_exception: {type(post_err).__name__}",
                            error_code="POST_EXCEPTION",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"comment_reply:{ct}:{rpid}:"
                                    f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                ),
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="result_unknown",
                                title=f"回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": rpid,
                                    "oid": str(oid),
                                    "reason_code": "POST_EXCEPTION",
                                },
                            )
                        except Exception:
                            logger.error(
                                "unknown deferred comment result could not be archived: rpid=%s",
                                rpid,
                            )
                        continue
                    if success is None:
                        logger.error("deferred 评论结果不确定（不自动重发）: rpid=%s", rpid)
                        self.reply_state_store.mark_result_unknown(
                            ct, rpid,
                            reason="post_comment transport uncertainty",
                            error_code="RESULT_UNKNOWN",
                        )
                        continue
                    if success:
                        self.reply_state_store.mark_published(ct, rpid)
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{ct}:{rpid}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=True,
                            title=f"已回复评论 {rpid}",
                            scene="reply_comment",
                            metadata={"reply_id": rpid, "oid": str(oid)},
                        )
                        self._notify_companion_comment_replied(
                            title=str(oid or "")[:40],
                            preview=str(reply_text or "")[:80],
                            proactive=False,
                        )
                        if self.safety_checker is not None:
                            try:
                                self.safety_checker.record_content(
                                    reply_text, account_id=self.account_id
                                )
                            except Exception:
                                pass
                        if audit_id and self.audit_store:
                            try:
                                self.audit_store.mark_published(
                                    audit_id, published=True,
                                    target={
                                        "kind": "reply_comment",
                                        "rpid": str(rpid),
                                        "source_rpid": str(rpid),
                                        "comment_type": int(ct),
                                        "account_id": self.account_id or "",
                                    },
                                )
                            except Exception:
                                pass
                        logger.info(f"deferred 重试成功: rpid={rpid}")
                    else:
                        if rate_reserved and self.safety_checker is not None:
                            try:
                                self.safety_checker.refund_publish(
                                    scene="reply_comment", account_id=self.account_id,
                                )
                            except Exception:
                                pass
                        self.reply_state_store.mark_retry_wait(
                            ct, rpid,
                            reason="deferred_retry_publish_failed",
                            error_code="RETRY_PUBLISH_FAILED",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"comment_reply:{ct}:{rpid}:"
                                    f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                ),
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="failed",
                                title=f"回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": rpid,
                                    "oid": str(oid),
                                    "reason_code": "RETRY_PUBLISH_FAILED",
                                },
                            )
                        except Exception:
                            logger.error(
                                "failed deferred comment result could not be archived: rpid=%s",
                                rpid,
                            )
                        logger.warning(f"deferred 重试发布失败: rpid={rpid}")
            except Exception as e:
                logger.error(f"重试评论 {rpid} 失败: {e}")
    except Exception as e:
        logger.error(f"处理重试评论失败: {e}")

# ════════════════════════════════════════
#  主动评论原子幂等（PRD-V5 §10.2 COM-501）
# ════════════════════════════════════════
