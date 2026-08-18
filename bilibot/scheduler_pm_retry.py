"""Private-message retry processing extracted from scheduler.py."""

from __future__ import annotations

import logging

logger = logging.getLogger("bilibot.scheduler_pm_retry")


async def _repair_pending_pm_archives(self) -> bool:
    """Repair outgoing PM memory observations without resending messages."""
    pending = self.pm_state_store.list_memory_archive_pending(
        account_id=self.account_id
    )
    if not pending:
        return True
    brain = getattr(self, "memory_brain", None)
    if brain is None:
        logger.error("待修复私信归档缺少 memory_brain，保持暂停")
        return False
    for pm_state in pending:
        try:
            safe = self._redact_private_message_runtime(
                pm_state.generation_text,
                actor_id="self",
            )
            _, archive_result = await brain.archive_private_message(
                platform_message_id=pm_state.platform_message_id,
                text=pm_state.generation_text,
                actor_id="self",
                direction="outgoing",
                persona_id=self._get_current_persona_id(),
                redacted=safe,
            )
            if (
                archive_result is None
                or getattr(archive_result, "source_committed", True) is False
            ):
                raise RuntimeError("PM outgoing source commit was not confirmed")
            self.pm_state_store.clear_memory_archive_pending(pm_state.id)
            logger.info(
                "已修复已发送私信的记忆归档: msg_id=%s",
                pm_state.platform_message_id,
            )
        except Exception as exc:
            self._pause_for_memory_failure()
            logger.error(
                "已发送私信记忆归档修复失败: msg_id=%s error=%s",
                pm_state.platform_message_id,
                type(exc).__name__,
                exc_info=True,
            )
            return False
    return True


async def process_retryable_pms(self):
    """PRD-V5 §6.3 / PM-501：重试 retry_wait / deferred 状态的私信

    - retry_wait：使用已保存的 generation_text 重新发送（不重新生成）
    - deferred：有 gen text 则安全复检后发送；无 gen text 则重新生成
    - 超过 max_attempts → failed（由 mark_retry_wait 自动判定）
    - result_unknown 不自动重发（需人工对账）
    """
    if not self.bili or not self.reply_gen:
        return
    safety = getattr(self, "safety_checker", None)
    if safety is None:
        logger.error("safety_checker 未初始化，跳过私信重试（fail-closed）")
        return
    try:
        if safety.is_paused() or safety.is_account_paused(self.account_id):
            logger.debug("私信重试暂停：账号或全局安全暂停 account=%s", self.account_id)
            return
    except Exception:
        logger.error("检查私信重试暂停状态失败，fail-closed", exc_info=True)
        return
    try:
        if not await _repair_pending_pm_archives(self):
            return
        retryable = self.pm_state_store.list_retryable(account_id=self.account_id)
        if not retryable:
            return
        logger.info(f"发现 {len(retryable)} 条待重试私信")
        for pm_state in retryable:
            try:
                retry_actor = "actor_unknown"
                try:
                    retry_actor = self._redact_private_message_runtime(
                        "", actor_id=pm_state.talker_id or "unknown"
                    ).actor_pseudonym or retry_actor
                except Exception:
                    pass

                talker_id = 0
                try:
                    talker_id = int(pm_state.talker_id or 0)
                except (TypeError, ValueError):
                    talker_id = 0
                if not talker_id:
                    self.pm_state_store.mark_failed(
                        pm_state.id, error_code="PM_NO_TALKER",
                        error="no talker_id for retry",
                    )
                    continue

                reply_text = pm_state.generation_text or ""
                gen_hash = pm_state.generation_hash or ""
                status = pm_state.status or ""

                # deferred 且无文本：先重新确认入站/历史记忆归档，再生成。
                # 归档失败的 PM 不得绕过 memory gate 直接进入 LLM。
                if status == "deferred" and not reply_text:
                    from bilibot.scheduler_pm import (
                        _archive_pm_memory_for_retry,
                        _defer_after_memory_archive_failure,
                    )
                    try:
                        await _archive_pm_memory_for_retry(self, pm_state)
                        self.pm_state_store.update_metadata(
                            pm_state.id,
                            {
                                "memory_archive_pending": False,
                                "memory_archive_stage": "",
                            },
                        )
                    except Exception as archive_exc:
                        logger.error(
                            "deferred 私信记忆归档复核失败 actor=%s error=%s",
                            retry_actor,
                            type(archive_exc).__name__,
                            exc_info=True,
                        )
                        meta = getattr(pm_state, "metadata", None) or {}
                        stage = str(
                            meta.get("memory_archive_stage")
                            or "memory_archive_retry_failed"
                        )
                        _defer_after_memory_archive_failure(
                            self,
                            pm_state,
                            stage=stage,
                            exc=archive_exc,
                            platform_message_id=pm_state.platform_message_id,
                        )
                        continue
                    incoming = ""
                    try:
                        meta = getattr(pm_state, "metadata", None) or {}
                        if isinstance(meta, dict):
                            incoming = str(meta.get("incoming_text") or "")
                    except Exception:
                        incoming = ""
                    if not incoming.strip():
                        self.pm_state_store.mark_failed(
                            pm_state.id,
                            error_code="PM_NO_INCOMING",
                            error="deferred regen missing incoming_text",
                        )
                        continue
                    # 与首次一致：用脱敏伪名作 user_id，避免真实 UID 进入 prompt/记忆边界
                    regen_user_id = retry_actor if retry_actor != "actor_unknown" else str(talker_id)
                    try:
                        outcome = await self.reply_gen._generate_reply_impl(
                            user_id=regen_user_id,
                            username="私信用户",
                            comment=incoming,
                            thread_id=f"pm_{regen_user_id}",
                            oid="",
                            comment_type=0,
                            scene="private_message",
                        )
                    except Exception as gen_err:
                        logger.error(
                            "deferred 私信重生成异常 actor=%s: %s",
                            retry_actor, type(gen_err).__name__,
                        )
                        self.pm_state_store.mark_deferred(
                            pm_state.id,
                            reason=f"regen_exception: {type(gen_err).__name__}",
                            error_code="PM_REGEN_EXCEPTION",
                        )
                        continue
                    if outcome.is_skip:
                        # deferred → ignored（状态机已允许）
                        self.pm_state_store.mark_ignored(
                            pm_state.id, rule=outcome.error_code or "llm_no_reply",
                        )
                        continue
                    if not outcome.is_generated:
                        if getattr(outcome, "is_permanent_error", False):
                            self.pm_state_store.mark_failed(
                                pm_state.id,
                                error_code=outcome.error_code or "PM_REGEN_PERMANENT",
                                error=f"regen_permanent: {outcome.error_code}",
                            )
                            continue
                        self.pm_state_store.mark_deferred(
                            pm_state.id,
                            reason=f"regen_{outcome.status}: {outcome.error_code}",
                            error_code=outcome.error_code or "PM_REGEN_EMPTY",
                        )
                        continue
                    reply_text = outcome.text
                    safe_regen = self._redact_private_message_runtime(
                        reply_text, actor_id=str(talker_id),
                    )
                    reply_text = safe_regen.text
                    pm_state = self.pm_state_store.save_generation_result(
                        pm_state.id,
                        text=reply_text,
                        persona_id=self._get_current_persona_id(),
                    )
                    gen_hash = pm_state.generation_hash or ""

                if not reply_text:
                    self.pm_state_store.mark_deferred(
                        pm_state.id, reason="no_generation_text",
                        error_code="RETRY_NO_GEN",
                    )
                    continue
                if gen_hash:
                    expected = self.pm_state_store.compute_generation_hash(reply_text)
                    if expected != gen_hash:
                        # P1-2：hash 不匹配不再死循环 deferred，直接 failed
                        logger.warning("重试私信文本 hash 不匹配: actor=%s", retry_actor)
                        self.pm_state_store.mark_failed(
                            pm_state.id,
                            error_code="RETRY_HASH_MISMATCH",
                            error="hash_mismatch",
                        )
                        continue

                safe_retry = self._redact_private_message_runtime(
                    reply_text, actor_id=str(talker_id),
                )
                safe_retry_text = safe_retry.text

                # fail-closed 安全 + 限流（先进入 safety_pending，保证 mark_rejected 合法）
                if self.safety_checker is None:
                    logger.error(
                        "safety_checker 未初始化，拒绝重试私信（fail-closed）: actor=%s",
                        retry_actor,
                    )
                    self.pm_state_store.mark_deferred(
                        pm_state.id, reason="safety_checker_missing",
                        error_code="NO_SAFETY_CHECKER",
                    )
                    continue
                try:
                    if (pm_state.status or "") != "safety_pending":
                        pm_state = self.pm_state_store.update_status(
                            pm_state.id, "safety_pending",
                        )
                except ValueError as te:
                    logger.error(
                        "重试私信无法进入 safety_pending (status=%s): %s",
                        status, te,
                    )
                    self.pm_state_store.mark_deferred(
                        pm_state.id,
                        reason=f"enter_safety_pending_failed: {te}",
                        error_code="PM_STATE_ERROR",
                    )
                    continue
                rate_reserved = False
                try:
                    passed, reason = await self.safety_checker.check_content(
                        safe_retry_text, scene="private_message",
                        persona_id=self._get_current_persona_id(),
                        account_id=self.account_id,
                    )
                    if not passed:
                        logger.warning("重试私信安全检查未通过: %s", reason)
                        self.pm_state_store.mark_rejected(
                            pm_state.id, reason=reason or "safety_check_failed",
                        )
                        continue
                    rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                        scene="private_message", account_id=self.account_id,
                    )
                    if not rate_ok:
                        logger.warning("重试私信频率限制触发: %s", rate_reason)
                        self.pm_state_store.mark_deferred(
                            pm_state.id, reason="rate_limited",
                            error_code="PM_RATE_LIMITED",
                        )
                        continue
                    rate_reserved = True
                except Exception as se:
                    logger.error("重试私信安全检查异常: %s", se, exc_info=True)
                    self.pm_state_store.mark_deferred(
                        pm_state.id,
                        reason=f"safety_exception: {type(se).__name__}",
                        error_code="SAFETY_ERROR",
                    )
                    continue

                # 推进：safety_pending → publish_pending
                self.pm_state_store.update_status(pm_state.id, "publish_pending")

                try:
                    success = await self.bili.send_private_message(
                        receiver_id=talker_id, msg=safe_retry_text,
                    )
                except Exception as send_exc:
                    # Uncertain: no refund
                    send_error = type(send_exc).__name__
                    logger.error(
                        "重试私信发送抛异常: actor=%s error=%s",
                        retry_actor, send_error,
                    )
                    self.pm_state_store.mark_result_unknown(
                        pm_state.id,
                        error_code="PM_RETRY_SEND_EXCEPTION",
                        error=send_error,
                    )
                    try:
                        await self._archive_bot_action(
                            action_key=(
                                f"private_message:{pm_state.platform_message_id}:"
                                f"retry:{pm_state.attempt + 1}"
                            ),
                            action_type="private_message",
                            text=safe_retry_text,
                            published=False,
                            status="result_unknown",
                            title="私信回复重试",
                            scene="private_message",
                            metadata={
                                "actor": retry_actor,
                                "reason_code": "PM_RETRY_SEND_EXCEPTION",
                            },
                        )
                    except Exception:
                        logger.error(
                            "unknown retried PM result could not be archived: actor=%s",
                            retry_actor,
                        )
                    continue

                if success is None:
                    logger.error(
                        "重试私信结果不确定（不自动重发）: actor=%s", retry_actor,
                    )
                    self.pm_state_store.mark_result_unknown(
                        pm_state.id,
                        error_code="RESULT_UNKNOWN",
                        error="send_private_message transport uncertainty",
                    )
                    continue

                if success:
                    self.pm_state_store.mark_published(pm_state.id)
                    logger.info("重试私信发送成功: actor=%s", retry_actor)
                    try:
                        brain = getattr(self, "memory_brain", None)
                        if brain is not None:
                            _, archive_result = await brain.archive_private_message(
                                platform_message_id=pm_state.platform_message_id,
                                text=safe_retry_text,
                                actor_id=str(talker_id),
                                direction="outgoing",
                                persona_id=self._get_current_persona_id(),
                                redacted=safe_retry,
                            )
                            if (
                                archive_result is None
                                or getattr(archive_result, "source_committed", True) is False
                            ):
                                raise RuntimeError(
                                    "PM retry outgoing source commit was not confirmed"
                                )
                    except Exception as outgoing_archive_exc:
                        self._pause_for_memory_failure()
                        try:
                            self.pm_state_store.mark_memory_archive_pending(
                                pm_state.id,
                                stage="outgoing_retry",
                                error_type=type(outgoing_archive_exc).__name__,
                            )
                        except Exception:
                            logger.error(
                                "重试私信归档修复标记失败: actor=%s",
                                retry_actor,
                                exc_info=True,
                            )
                        logger.error(
                            "重试私信结果归档失败: actor=%s error=%s",
                            retry_actor,
                            type(outgoing_archive_exc).__name__,
                            exc_info=True,
                        )
                    self._notify_companion_private_message_replied(
                        actor_label=str(retry_actor or "")[:24],
                    )
                    if self.safety_checker is not None:
                        try:
                            self.safety_checker.record_content(
                                safe_retry_text, account_id=self.account_id,
                            )
                        except Exception:
                            pass
                    try:
                        await self.bili.ack_session(talker_id, session_type=1)
                    except Exception:
                        pass
                else:
                    if rate_reserved and self.safety_checker is not None:
                        try:
                            self.safety_checker.refund_publish(
                                scene="private_message", account_id=self.account_id,
                            )
                        except Exception:
                            pass
                    self.pm_state_store.mark_retry_wait(
                        pm_state.id,
                        error_code="PM_RETRY_PUBLISH_FAILED",
                        error="retry send_private_message returned False",
                    )
                    try:
                        await self._archive_bot_action(
                            action_key=(
                                f"private_message:{pm_state.platform_message_id}:"
                                f"retry:{pm_state.attempt + 1}"
                            ),
                            action_type="private_message",
                            text=safe_retry_text,
                            published=False,
                            status="failed",
                            title="私信回复重试",
                            scene="private_message",
                            metadata={
                                "actor": retry_actor,
                                "reason_code": "PM_RETRY_PUBLISH_FAILED",
                            },
                        )
                    except Exception:
                        logger.error(
                            "failed retried PM result could not be archived: actor=%s",
                            retry_actor,
                        )
                    logger.warning("重试私信发送失败: actor=%s", retry_actor)
            except Exception as e:
                logger.error(
                    "重试私信失败: actor=%s error=%s",
                    retry_actor, type(e).__name__,
                )
    except Exception as e:
        logger.error("处理重试私信失败: %s", type(e).__name__)

# ══════════════════════════════════════════
#  主动行为
# ══════════════════════════════════════════
