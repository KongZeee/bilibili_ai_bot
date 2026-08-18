"""Private-message polling extracted from scheduler.py."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("bilibot.scheduler_pm")


def _defer_after_memory_archive_failure(
    self,
    pm_state,
    *,
    stage: str,
    exc: BaseException,
    platform_message_id: str = "",
) -> None:
    """Pause the account and durably defer a PM after archive failure.

    Archive failures are fail-closed for publishing, but the failure handler
    itself must never strand the PM. Keep the exception class in the state
    ledger and log a traceback without logging message content.
    """
    error_type = type(exc).__name__
    reason = f"{stage}:{error_type}"
    try:
        current = self.pm_state_store.get_by_id(pm_state.id) or pm_state
        current_status = str(getattr(current, "status", "") or "")
    except Exception:
        # A state lookup failure means we cannot prove the PM is terminal, so
        # preserve fail-closed behavior and surface the diagnostic below.
        current_status = ""
        logger.error(
            "记忆归档失败后读取私信状态异常: state_id=%s",
            getattr(pm_state, "id", "-"),
            exc_info=True,
        )
    if current_status in {"published", "ignored", "rejected", "failed", "result_unknown"}:
        # A concurrent worker already completed or made the platform result
        # uncertain; do not pause the account or reopen the finished PM.
        logger.warning(
            "记忆归档失败但保留已处理私信状态: status=%s state_id=%s",
            current_status,
            pm_state.id,
        )
        return
    try:
        self._pause_for_memory_failure()
    except Exception:
        logger.error(
            "记忆归档失败后暂停账号异常: stage=%s msg_id=%s",
            stage,
            platform_message_id or "-",
            exc_info=True,
        )
    try:
        if current_status == "publish_pending":
            # The platform may have accepted the send; force manual
            # reconciliation instead of allowing a duplicate retry.
            self.pm_state_store.mark_result_unknown(
                pm_state.id,
                error_code="MEMORY_ARCHIVE_FAILED",
                error=reason,
            )
        else:
            self.pm_state_store.mark_deferred(
                pm_state.id,
                reason=reason,
                error_code="MEMORY_ARCHIVE_FAILED",
            )
        self.pm_state_store.update_metadata(
            pm_state.id,
            {
                "memory_archive_stage": stage,
                "memory_archive_error": error_type,
                "memory_archive_pending": True,
            },
        )
    except Exception:
        # Do not hide either the original archive failure or a state-machine
        # regression. The next scheduler tick can recover stale PM states.
        logger.error(
            "记忆归档失败后私信状态无法 deferred: stage=%s state_id=%s msg_id=%s",
            stage,
            getattr(pm_state, "id", "-"),
            platform_message_id or "-",
            exc_info=True,
        )


async def _archive_pm_memory_for_retry(self, pm_state) -> None:
    """Re-confirm PM memory sources before deferred regeneration.

    The retry path must not generate/send a PM that was deferred because its
    observation was not durably committed. Archive is idempotent, so replaying
    the same platform message ID is safe.
    """
    from bilibot.services.pm_state_store import extract_platform_message_id

    brain = getattr(self, "memory_brain", None)
    if brain is None:
        raise RuntimeError("MEMORY_BRAIN_MISSING")
    metadata = getattr(pm_state, "metadata", None) or {}
    incoming = str(metadata.get("incoming_text") or "").strip()
    if not incoming:
        raise RuntimeError("PM_NO_INCOMING")
    talker_id = str(getattr(pm_state, "talker_id", "") or "")
    safe = self._redact_private_message_runtime(
        incoming,
        actor_id=talker_id or "unknown",
        username="私信用户",
    )
    _, result = await brain.archive_private_message(
        platform_message_id=pm_state.platform_message_id,
        text=incoming,
        actor_id=talker_id or "unknown",
        username="私信用户",
        direction="incoming",
        persona_id=self._get_current_persona_id(),
        redacted=safe,
    )
    if result is None or getattr(result, "source_committed", True) is False:
        raise RuntimeError("PM source commit was not confirmed")

    stage = str(metadata.get("memory_archive_stage") or "")
    if not stage.startswith("pm_history_archive_failed"):
        return

    try:
        talker_int = int(talker_id or 0)
    except (TypeError, ValueError):
        talker_int = 0
    if not talker_int:
        raise RuntimeError("PM_HISTORY_NO_TALKER")
    config = self.config_loader.get_raw_config()
    try:
        my_uid = int(
            str(getattr(self, "_bot_uid", "") or "").strip()
            or config.get("bilibili", {}).get("dede_user_id", 0)
            or 0
        )
    except (TypeError, ValueError):
        my_uid = 0
    if not my_uid:
        raise RuntimeError("PM_HISTORY_NO_BOT_UID")

    history = []
    fetcher = getattr(self.bili, "get_session_messages", None)
    if callable(fetcher):
        response = await fetcher(
            talker_id=talker_int,
            session_type=1,
            size=20,
            sender_uid=talker_int,
            receiver_uid=my_uid,
            limit=20,
        )
        if isinstance(response, dict) and response.get("code") == 0:
            history = (response.get("data") or {}).get("messages") or []
    if not history:
        sessions = await self.bili.get_private_sessions(limit=20)
        for session in ((sessions or {}).get("data") or {}).get("session_list", []):
            if str(session.get("talker_id") or "") != str(talker_int):
                continue
            history = (
                session.get("messages")
                or session.get("message_list")
                or session.get("session_messages")
                or []
            )
            if history:
                break
    if not history:
        raise RuntimeError("PM_HISTORY_UNAVAILABLE")
    current_id = extract_platform_message_id({"msg_id": pm_state.platform_message_id})
    await self._archive_pm_recent_history(
        history,
        current_message_id=current_id or pm_state.platform_message_id,
        talker_id=talker_int,
        talker_name="私信用户",
        my_uid=my_uid,
    )


async def check_new_messages(self):
    """检查新私信并自动回复"""
    if not self.reply_gen or not self.bili:
        return
    if not self._authenticated_poll_allowed():
        return

    # 全局暂停 / 账号风险暂停 / 无 checker → fail-closed
    if self.safety_checker is None:
        logger.error("safety_checker 未初始化，跳过私信检查（fail-closed）")
        return
    if (
        self.safety_checker.is_paused()
        or self.safety_checker.is_account_paused(self.account_id)
    ):
        return

    config = self.config_loader.get_raw_config()
    if not config.get("features", {}).get("private_message", False):
        return

    try:
        sessions_resp = await self.bili.get_private_sessions(limit=20)
        if not sessions_resp:
            logger.info("私信会话 API 返回空")
            return
        if sessions_resp.get("code") != 0:
            logger.info(f"私信会话 API 返回错误: code={sessions_resp.get('code')} msg={sessions_resp.get('message')}")
            return

        session_list = sessions_resp.get("data", {}).get("session_list", [])
        if not session_list:
            logger.info("无私信会话")
            return

        # P1-1：与评论路径统一 bot 身份；优先 _bot_uid（nav 可补全）
        try:
            my_uid = int(
                str(getattr(self, "_bot_uid", "") or "").strip()
                or config.get("bilibili", {}).get("dede_user_id", 0)
                or 0
            )
        except (TypeError, ValueError):
            my_uid = 0
        if not my_uid:
            logger.error(
                "_bot_uid/dede_user_id 为空，跳过私信检查（fail-closed，防止身份错乱）"
            )
            return
        # PRD V6：会话数变化时才打 INFO，否则降级 DEBUG
        _sess_count = len(session_list)
        if _sess_count != self._last_dm_session_count:
            logger.info("检查私信: %d 个会话", _sess_count)
            self._last_dm_session_count = _sess_count
        else:
            logger.debug("检查私信: %d 个会话（无变化）", _sess_count)

        for session in session_list:
            try:
                # 只处理单聊（session_type=1）
                if session.get("session_type", 1) != 1:
                    continue

                # 检查是否有未读消息
                unread = session.get("unread_count", 0)
                try:
                    unread = int(unread or 0)
                except (TypeError, ValueError):
                    unread = 0
                if not unread:
                    continue

                # 获取对方信息
                talker_id_raw = session.get("talker_id", 0)
                try:
                    talker_id = int(talker_id_raw)
                except (TypeError, ValueError):
                    talker_id = 0
                if not talker_id or talker_id == my_uid:
                    continue

                # 获取对方用户名
                talker_name = "用户"
                talker_info = session.get("talker_info") or {}
                if isinstance(talker_info, dict):
                    talker_name = talker_info.get("uname") or talker_info.get("name") or "用户"

                from bilibot.services.pm_state_store import (
                    extract_platform_message_id,
                    TERMINAL_STATUSES as PM_TERMINAL,
                )

                # S6：unread>1 时拉最近 N 条按 platform_message_id 幂等处理，避免只回 last_msg 漏回
                history_messages = (
                    session.get("messages")
                    or session.get("message_list")
                    or session.get("session_messages")
                    or []
                )
                if not isinstance(history_messages, list):
                    history_messages = []

                fetch_limit = max(5, min(int(unread) + 2, 20))
                need_fetch = (
                    unread > 1
                    or not (session.get("last_msg") or {}).get("content")
                    or not history_messages
                )
                if need_fetch and hasattr(self.bili, "get_session_messages"):
                    try:
                        msgs_resp = await self.bili.get_session_messages(
                            talker_id=talker_id,
                            session_type=1,
                            size=fetch_limit,
                            sender_uid=talker_id,
                            receiver_uid=my_uid,
                            limit=fetch_limit,
                        )
                        if msgs_resp and msgs_resp.get("code") == 0:
                            fetched = (
                                (msgs_resp.get("data") or {}).get("messages")
                                or []
                            )
                            if isinstance(fetched, list) and fetched:
                                history_messages = fetched
                    except Exception as fetch_exc:
                        logger.warning(
                            "拉取会话消息列表失败 talker=%s: %s",
                            talker_id, type(fetch_exc).__name__,
                        )

                # 候选：对方发来的、有内容、可提取 platform_message_id 的消息
                # 优先处理历史中的未读条；无列表时退回 last_msg 单条
                candidate_msgs: List[Dict[str, Any]] = []
                if history_messages:
                    for m in history_messages:
                        if not isinstance(m, dict):
                            continue
                        try:
                            s_uid = int(m.get("sender_uid") or 0)
                        except (TypeError, ValueError):
                            s_uid = 0
                        if s_uid and s_uid == my_uid:
                            continue
                        if not self._private_message_text(m):
                            continue
                        if not extract_platform_message_id(m):
                            continue
                        candidate_msgs.append(m)
                if not candidate_msgs:
                    last_msg = session.get("last_msg") or {}
                    if isinstance(last_msg, dict) and last_msg:
                        try:
                            s_uid = int(last_msg.get("sender_uid") or 0)
                        except (TypeError, ValueError):
                            s_uid = 0
                        if not (s_uid and s_uid == my_uid):
                            if self._private_message_text(last_msg) and extract_platform_message_id(last_msg):
                                candidate_msgs = [last_msg]

                if not candidate_msgs:
                    continue

                # 每会话最多处理 batch 条，避免一次拉太多触发限流；按时间序（旧→新）
                def _msg_ts(m: Dict[str, Any]) -> float:
                    for k in ("timestamp", "msg_timestamp", "msg_seqno", "seqno"):
                        v = m.get(k)
                        if v is not None:
                            try:
                                return float(v)
                            except (TypeError, ValueError):
                                pass
                    return 0.0

                candidate_msgs = sorted(candidate_msgs, key=_msg_ts)
                # P0-A：本轮最多主动处理 5 条（限流），但其余候选必须至少
                # ensure_discovered 入库，且未处理完禁止整会话 ack。
                process_batch = candidate_msgs[:5]
                overflow_msgs = candidate_msgs[5:]
                for m in overflow_msgs:
                    pid = extract_platform_message_id(m)
                    if not pid:
                        continue
                    try:
                        self.pm_state_store.ensure_discovered(
                            account_id=self.account_id,
                            platform_message_id=pid,
                            talker_id=str(talker_id),
                            incoming_text=self._private_message_text(m)[:2000],
                        )
                    except Exception:
                        pass

                # 仅当「全部候选（含 overflow）均已离开未完结态」且无 overflow
                # 需要后续轮次处理时，才允许 ack。overflow 存在 → 永不本轮 ack。
                session_ack_ok = not overflow_msgs
                max_ack_seqno = 0

                def _msg_seq(m: Dict[str, Any]) -> int:
                    for k in ("msg_seqno", "seqno", "msg_seq", "seq_id"):
                        v = m.get(k)
                        if v is not None:
                            try:
                                return int(v)
                            except (TypeError, ValueError):
                                pass
                    return 0

                for last_msg in process_batch:
                    try:
                        msg_content = self._private_message_text(last_msg)
                        if not msg_content:
                            continue

                        # PRD-V5 §6.3 / PM-501：私信幂等键使用平台消息 ID
                        platform_msg_id = extract_platform_message_id(last_msg)
                        if not platform_msg_id:
                            logger.warning("无法提取私信平台消息 ID，跳过未归档消息")
                            continue

                        pm_state = self.pm_state_store.ensure_discovered(
                            account_id=self.account_id,
                            platform_message_id=platform_msg_id,
                            talker_id=str(talker_id),
                        )

                        # V6 privacy boundary: redact and pseudonymize before brain,
                        # logs, recall, audit or model prompts.
                        try:
                            safe_pm = self._redact_private_message_runtime(
                                msg_content,
                                actor_id=str(talker_id),
                                username=talker_name,
                            )
                            # Persist redacted incoming text for deferred regen
                            try:
                                self.pm_state_store.ensure_discovered(
                                    account_id=self.account_id,
                                    platform_message_id=platform_msg_id,
                                    talker_id=str(talker_id),
                                    incoming_text=safe_pm.text or "",
                                )
                                # refresh state after metadata fill
                                pm_state = self.pm_state_store.get_by_message_id(
                                    self.account_id, platform_msg_id
                                ) or pm_state
                            except Exception:
                                pass

                            # Do not re-archive already processed messages just
                            # because Bilibili keeps them in the session list.
                            # Their current payload may differ from the original
                            # redacted payload and trigger a false idempotency
                            # conflict before the terminal-state check below.
                            if pm_state.status in PM_TERMINAL:
                                logger.debug(
                                    "私信已处于终态 %s，跳过重复归档 msg_id=%s",
                                    pm_state.status,
                                    platform_msg_id,
                                )
                                continue
                            if pm_state.status != "discovered":
                                logger.debug(
                                    "私信状态 %s 非 discovered，跳过归档 msg_id=%s",
                                    pm_state.status,
                                    platform_msg_id,
                                )
                                continue

                            brain = getattr(self, "memory_brain", None)
                            if brain is not None:
                                safe_pm, archive_result = await brain.archive_private_message(
                                    platform_message_id=platform_msg_id,
                                    text=msg_content,
                                    actor_id=str(talker_id),
                                    username=talker_name,
                                    direction="incoming",
                                    persona_id=self._get_current_persona_id(),
                                    redacted=safe_pm,
                                )
                                if (
                                    archive_result is None
                                    or getattr(archive_result, "source_committed", True) is False
                                ):
                                    raise RuntimeError(
                                        "PM source commit was not confirmed"
                                    )
                        except Exception as archive_exc:
                            logger.error(
                                "私信入站记忆归档失败: msg_id=%s error=%s",
                                platform_msg_id,
                                type(archive_exc).__name__,
                                exc_info=True,
                            )
                            _defer_after_memory_archive_failure(
                                self,
                                pm_state,
                                stage="memory_archive_failed",
                                exc=archive_exc,
                                platform_message_id=platform_msg_id,
                            )
                            continue

                        try:
                            pm_recent_turns = await self._archive_pm_recent_history(
                                history_messages,
                                current_message_id=platform_msg_id,
                                talker_id=talker_id,
                                talker_name=talker_name,
                                my_uid=my_uid,
                            )
                        except Exception as history_exc:
                            logger.error(
                                "私信历史记忆归档失败: msg_id=%s error=%s",
                                platform_msg_id,
                                type(history_exc).__name__,
                                exc_info=True,
                            )
                            _defer_after_memory_archive_failure(
                                self,
                                pm_state,
                                stage="pm_history_archive_failed",
                                exc=history_exc,
                                platform_message_id=platform_msg_id,
                            )
                            continue

                        logger.info("发现新私信: actor=%s msg_id=%s", safe_pm.actor_pseudonym, platform_msg_id)

                        # Ignored/blacklisted messages remain archived observations.
                        if self.safety_checker is not None and self.safety_checker.is_blacklisted(str(talker_id)):
                            logger.info("私信 actor=%s 命中黑名单，跳过", safe_pm.actor_pseudonym)
                            self.pm_state_store.mark_ignored(pm_state.id, rule="blacklist")
                            continue

                        # 幂等：终态或进行中则跳过（由 _process_retryable_pms 处理 retry_wait）
                        if pm_state.status in PM_TERMINAL:
                            logger.debug(
                                f"私信已处于终态 {pm_state.status}，跳过: "
                                f"actor={safe_pm.actor_pseudonym}"
                            )
                            continue
                        if pm_state.status not in ("discovered",):
                            # retry_wait / deferred 由独立重试循环处理；中间态跳过避免并发
                            logger.debug(
                                f"私信状态 {pm_state.status} 非 discovered，跳过: "
                                f"actor={safe_pm.actor_pseudonym}"
                            )
                            continue

                        # 推进：discovered → generation_pending
                        pm_state = self.pm_state_store.update_status(
                            pm_state.id, "generation_pending",
                        )

                        # 用 reply_gen 生成回复（复用评论回复逻辑）
                        # PRD-V5 §4.3 SEA-501：私信场景须传 scene=private_message
                        # P3-7：与 reply 层共用同一活动键（private_reply:pm_<actor>），
                        # 不再在 scheduler 层另开一个 intent。
                        pm_action_key = f"private_reply:pm_{safe_pm.actor_pseudonym}"
                        pm_action_type = "private_reply"
                        pm_activity_started = True
                        memory_evidence = ""
                        try:
                            from bilibot.memory_brain import RecallQuery

                            brain = getattr(self, "memory_brain", None)
                            if brain is not None:
                                recall_result = await brain.recall(
                                    RecallQuery(
                                        current_message=safe_pm.text,
                                        recent_turns=tuple(pm_recent_turns),
                                        account_id=self.account_id,
                                        speaker_actor_id=safe_pm.actor_pseudonym,
                                        scene="private_message",
                                    )
                                )
                                memory_evidence = recall_result.prompt_evidence
                        except Exception as exc:
                            logger.warning("私信记忆召回降级为空: %s", type(exc).__name__)

                        from bilibot.models import ReplyContext
                        # 直接调用 _generate_reply_impl 以获取 GenerationOutcome，
                        # 不再通过 generate_reply 包装器（它把 skip/retryable/permanent 全折叠成 None）
                        try:
                            outcome = await self.reply_gen._generate_reply_impl(
                                user_id=safe_pm.actor_pseudonym,
                                username="私信用户",
                                comment=safe_pm.text,
                                thread_id=f"pm_{safe_pm.actor_pseudonym}",
                                oid="",
                                comment_type=0,
                                reply_context=ReplyContext(memory_evidence=memory_evidence),
                                scene="private_message",
                                private_redaction_username=talker_name,
                            )
                        except Exception as llm_err:
                            logger.error(
                                "私信 LLM 生成失败，deferred actor=%s: %s",
                                safe_pm.actor_pseudonym, llm_err,
                            )
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text=f"私信生成失败: {type(llm_err).__name__}",
                                        published=False,
                                        status="failed",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "LLM_ERROR",
                                        },
                                    )
                                except Exception:
                                    pass
                            self.pm_state_store.mark_deferred(
                                pm_state.id,
                                reason=f"llm_error: {llm_err}",
                                error_code="LLM_ERROR",
                            )
                            continue

                        if outcome.is_skip:
                            logger.debug("LLM 决定跳过私信回复 actor=%s", safe_pm.actor_pseudonym)
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text="LLM 决定跳过私信回复",
                                        published=False,
                                        status="skipped",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": outcome.error_code or "llm_no_reply",
                                        },
                                    )
                                except Exception:
                                    pass
                            self.pm_state_store.mark_ignored(
                                pm_state.id, rule=outcome.error_code or "llm_no_reply",
                            )
                            continue
                        if not outcome.is_generated:
                            if getattr(outcome, "is_permanent_error", False):
                                logger.error(
                                    "私信 LLM 永久失败 (code=%s)，标记 failed actor=%s",
                                    outcome.error_code, safe_pm.actor_pseudonym,
                                )
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=f"私信生成永久失败: {outcome.error_code}",
                                            published=False,
                                            status="failed",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": outcome.error_code or "PM_GEN_PERMANENT",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_failed(
                                    pm_state.id,
                                    error_code=outcome.error_code or "PM_GEN_PERMANENT",
                                    error=f"generation_permanent: {outcome.error_code}",
                                )
                                continue
                            logger.warning(
                                "私信 LLM 生成未成功 (status=%s, code=%s)，deferred actor=%s",
                                outcome.status, outcome.error_code, safe_pm.actor_pseudonym,
                            )
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text=f"私信生成未成功: {outcome.status}",
                                        published=False,
                                        status="failed",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": outcome.error_code or "GEN_FAILED",
                                        },
                                    )
                                except Exception:
                                    pass
                            self.pm_state_store.mark_deferred(
                                pm_state.id,
                                reason=f"generation_{outcome.status}: {outcome.error_code}",
                                error_code=outcome.error_code or "GEN_FAILED",
                            )
                            continue

                        reply_text = outcome.text
                        audit_id = outcome.audit_id  # PRD 4.16：私信审计
                        safe_reply = self._redact_private_message_runtime(
                            reply_text,
                            actor_id=str(talker_id),
                            username=talker_name,
                        )
                        safe_reply_text = safe_reply.text

                        # PRD-V5 §6.3 / PM-501：生成文本持久化（安全检查之前）
                        pm_state = self.pm_state_store.save_generation_result(
                            pm_state.id,
                            text=safe_reply_text,
                            persona_id=self._get_current_persona_id(),
                        )

                        # 推进：generation_pending → safety_pending
                        pm_state = self.pm_state_store.update_status(
                            pm_state.id, "safety_pending",
                        )

                        # 安全检查（fail-closed：无 checker 禁止发送私信）
                        if self.safety_checker is None:
                            logger.error(
                                "safety_checker 未初始化，拒绝发送私信（fail-closed）: actor=%s",
                                safe_pm.actor_pseudonym,
                            )
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text="私信安全检查器未初始化，拒绝发送",
                                        published=False,
                                        status="deferred",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "NO_SAFETY_CHECKER",
                                        },
                                    )
                                except Exception:
                                    pass
                            self.pm_state_store.mark_deferred(
                                pm_state.id, reason="safety_checker_missing",
                                error_code="NO_SAFETY_CHECKER",
                            )
                            continue
                        rate_reserved = False
                        passed, reason = await self.safety_checker.check_content(
                            safe_reply_text, scene="private_message",
                            persona_id=self._get_current_persona_id(),
                            account_id=self.account_id,
                        )
                        if not passed:
                            logger.warning(f"私信回复安全检查未通过: {reason}")
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text=f"私信安全检查未通过: {reason}",
                                        published=False,
                                        status="rejected",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "safety_check_failed",
                                        },
                                    )
                                except Exception:
                                    pass
                            self.pm_state_store.mark_rejected(
                                pm_state.id, reason=reason or "safety_check_failed",
                            )
                            continue
                        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                            scene="private_message", account_id=self.account_id,
                        )
                        if not rate_ok:
                            logger.warning("私信频率限制触发: %s", rate_reason)
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text=f"私信频率限制: {rate_reason}",
                                        published=False,
                                        status="deferred",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "PM_RATE_LIMITED",
                                        },
                                    )
                                except Exception:
                                    pass
                            self.pm_state_store.mark_deferred(
                                pm_state.id, reason="rate_limited",
                                error_code="PM_RATE_LIMITED",
                            )
                            continue
                        rate_reserved = True

                        # 推进：safety_pending → publish_pending
                        pm_state = self.pm_state_store.update_status(
                            pm_state.id, "publish_pending",
                        )

                        # 发送私信
                        try:
                            success = await self.bili.send_private_message(
                                receiver_id=talker_id,
                                msg=safe_reply_text,
                            )
                        except Exception as send_exc:
                            # 本地异常：平台结果不确定 → result_unknown（不自动重发）
                            # 不确定结果不退配额（可能已发出）；仅明确 False 时退
                            send_error = type(send_exc).__name__
                            logger.error(
                                "私信发送抛异常（平台结果不确定）: actor=%s error=%s",
                                safe_pm.actor_pseudonym,
                                send_error,
                            )
                            self.pm_state_store.mark_result_unknown(
                                pm_state.id,
                                error_code="PM_SEND_EXCEPTION",
                                error=send_error,
                            )
                            try:
                                await self._archive_bot_action(
                                    action_key=f"private_message:{platform_msg_id}:send",
                                    action_type="private_message",
                                    text=safe_reply_text,
                                    published=False,
                                    status="result_unknown",
                                    title="私信回复",
                                    scene="private_message",
                                    metadata={
                                        "actor": safe_pm.actor_pseudonym,
                                        "reason_code": "PM_SEND_EXCEPTION",
                                    },
                                )
                            except Exception:
                                logger.error(
                                    "unknown PM result could not be archived: actor=%s",
                                    safe_pm.actor_pseudonym,
                                )
                            # 审计记录失败
                            if audit_id and self.audit_store:
                                try:
                                    self.audit_store.mark_published(
                                        audit_id, published=False,
                                        failure_reason=f"send_exception: {send_error}",
                                    )
                                except Exception:
                                    pass
                            continue

                        if success is None:
                            # Transport uncertainty: no refund, no auto-resend
                            logger.error(
                                "私信结果不确定（不自动重发）: actor=%s",
                                safe_pm.actor_pseudonym,
                            )
                            self.pm_state_store.mark_result_unknown(
                                pm_state.id,
                                error_code="RESULT_UNKNOWN",
                                error="send_private_message transport uncertainty",
                            )
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text=str(safe_reply_text or "")[:200]
                                        or "私信发送结果不确定",
                                        published=False,
                                        status="result_unknown",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "RESULT_UNKNOWN",
                                        },
                                    )
                                except Exception:
                                    logger.debug(
                                        "PM result_unknown finish failed",
                                        exc_info=True,
                                    )
                            continue

                        # PRD 4.16：私信审计记录
                        if audit_id and self.audit_store:
                            try:
                                if success:
                                    self.audit_store.mark_published(
                                        audit_id, published=True,
                                        target={"kind": "private_message", "actor": safe_pm.actor_pseudonym},
                                    )
                                else:
                                    self.audit_store.mark_published(
                                        audit_id, published=False, failure_reason="send_private_message failed",
                                    )
                            except Exception:
                                pass

                        if success:
                            logger.info("已回复私信 actor=%s msg_id=%s", safe_pm.actor_pseudonym, platform_msg_id)
                            # PRD-V5 §6.3 / PM-501：标记已发布（终态）
                            self.pm_state_store.mark_published(pm_state.id)
                            try:
                                brain = getattr(self, "memory_brain", None)
                                if brain is not None:
                                    _, archive_result = await brain.archive_private_message(
                                        platform_message_id=platform_msg_id,
                                        text=safe_reply_text,
                                        actor_id=str(talker_id),
                                        username=talker_name,
                                        direction="outgoing",
                                        persona_id=self._get_current_persona_id(),
                                        redacted=safe_reply,
                                    )
                                    if (
                                        archive_result is None
                                        or getattr(archive_result, "source_committed", True) is False
                                    ):
                                        raise RuntimeError(
                                            "PM outgoing source commit was not confirmed"
                                        )
                            except Exception as outgoing_archive_exc:
                                self._pause_for_memory_failure()
                                try:
                                    self.pm_state_store.mark_memory_archive_pending(
                                        pm_state.id,
                                        stage="outgoing",
                                        error_type=type(outgoing_archive_exc).__name__,
                                    )
                                except Exception:
                                    logger.error(
                                        "published PM archive repair marker failed: actor=%s",
                                        safe_pm.actor_pseudonym,
                                        exc_info=True,
                                    )
                                logger.error(
                                    "published PM result could not be archived: actor=%s error=%s",
                                    safe_pm.actor_pseudonym,
                                    type(outgoing_archive_exc).__name__,
                                    exc_info=True,
                                )
                            # Always close activity + continuous self after a real send,
                            # even if durable PM observation archive failed.
                            if pm_activity_started:
                                try:
                                    await self._archive_bot_action(
                                        action_key=pm_action_key,
                                        action_type=pm_action_type,
                                        text=safe_reply_text,
                                        published=True,
                                        status="completed",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "platform_message_id": platform_msg_id,
                                        },
                                    )
                                except Exception:
                                    logger.debug(
                                        "PM success finish_activity failed",
                                        exc_info=True,
                                    )
                            self._notify_companion_private_message_replied(
                                actor_label=str(safe_pm.actor_pseudonym or "")[:24],
                            )
                            if self.safety_checker is not None:
                                self.safety_checker.record_content(
                                    safe_reply_text, account_id=self.account_id
                                )
                        else:
                            # PRD-V5 §6.3 / PM-501：发布失败 → retry_wait（独立退避）
                            # 平台明确返回失败（非本地异常），按 retry_wait 处理
                            if rate_reserved and self.safety_checker is not None:
                                try:
                                    self.safety_checker.refund_publish(
                                        scene="private_message", account_id=self.account_id,
                                    )
                                except Exception:
                                    pass
                            logger.warning("私信发送失败 actor=%s", safe_pm.actor_pseudonym)
                            self.pm_state_store.mark_retry_wait(
                                pm_state.id,
                                error_code="PM_PUBLISH_FAILED",
                                error="send_private_message returned False",
                            )
                            try:
                                await self._archive_bot_action(
                                    action_key=f"private_message:{platform_msg_id}:send",
                                    action_type="private_message",
                                    text=safe_reply_text,
                                    published=False,
                                    status="failed",
                                    title="私信回复",
                                    scene="private_message",
                                    metadata={
                                        "actor": safe_pm.actor_pseudonym,
                                        "reason_code": "PM_PUBLISH_FAILED",
                                    },
                                )
                            except Exception:
                                logger.error(
                                    "failed PM result could not be archived: actor=%s",
                                    safe_pm.actor_pseudonym,
                                )
                    except Exception as msg_exc:
                        session_ack_ok = False
                        logger.warning(
                            "处理单条私信异常 talker=%s: %s",
                            talker_id, type(msg_exc).__name__,
                        )
                    else:
                        try:
                            max_ack_seqno = max(max_ack_seqno, _msg_seq(last_msg))
                        except Exception:
                            pass

                # P0-A：仅当无 overflow、本轮处理无异常、且全部候选（含 overflow
                # 入库的）均已离开 discovered/中间态 时才 ack；并带真实 ack_seqno。
                if session_ack_ok and candidate_msgs and not overflow_msgs:
                    try:
                        all_settled = True
                        for m in candidate_msgs:
                            pid = extract_platform_message_id(m)
                            if not pid:
                                all_settled = False
                                break
                            st = self.pm_state_store.get_by_message_id(
                                self.account_id, pid
                            )
                            if st is None or st.status in (
                                "discovered",
                                "generation_pending",
                                "safety_pending",
                                "publish_pending",
                            ):
                                all_settled = False
                                break
                            try:
                                max_ack_seqno = max(max_ack_seqno, _msg_seq(m))
                            except Exception:
                                pass
                        if all_settled:
                            await self.bili.ack_session(
                                talker_id,
                                session_type=1,
                                ack_seqno=int(max_ack_seqno or 0),
                            )
                    except Exception:
                        pass

            except Exception as e:
                logger.warning("处理私信会话异常: %s", type(e).__name__)

    except Exception as e:
        logger.error("私信检查异常: %s", type(e).__name__)
