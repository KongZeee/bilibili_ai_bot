"""Comment polling extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

logger = logging.getLogger("bilibot.scheduler_comments")


async def check_new_comments(self):
    """检查 B站通知中心的新评论"""
    if not self.reply_gen:
        logger.info("reply_gen 未初始化，跳过评论检查")
        return
    if not self.bili:
        logger.info("bili API 未初始化，跳过评论检查")
        return
    if not self._authenticated_poll_allowed():
        return

    # PRD §5.9：全局暂停 / 账号风险暂停 / 无 checker → fail-closed 跳过自动回复
    if self.safety_checker is None:
        logger.error("safety_checker 未初始化，跳过评论检查（fail-closed）")
        return
    if (
        self.safety_checker.is_paused()
        or self.safety_checker.is_account_paused(self.account_id)
    ):
        logger.info(
            "跳过评论检查（全局暂停=%s, 账号暂停=%s）",
            self.safety_checker.is_paused(),
            self.safety_checker.is_account_paused(self.account_id),
        )
        return

    config = self.config_loader.get_raw_config()
    # PRD V4 REP-002 / CFG-003：统一使用 features.reply_comment
    # 旧 reply.auto_reply 已迁移，运行时不再消费
    features = config.get("features", {})
    if not features.get("reply_comment", True):
        logger.info("features.reply_comment=false，跳过评论检查")
        return

    # B站阿瓦隆 state=17 隐藏回复后进入发布冷却；冷却到期自动恢复。
    _risk_remaining = float(getattr(self, "_comment_risk_pause_remaining", lambda: 0.0)())
    if _risk_remaining > 0:
        logger.info(
            "评论回复处于风控暂停中，剩余 %.0f 秒，到期后自动恢复",
            _risk_remaining,
        )
        return
    _clear_pause = getattr(self, "_clear_comment_risk_pause", None)
    if callable(_clear_pause):
        _clear_pause()

    # S3：_bot_uid 为空 fail-closed，禁止自动回复（避免无法识别自己导致自回）
    if not str(getattr(self, "_bot_uid", "") or "").strip():
        logger.error(
            "_bot_uid 为空，跳过自动评论回复（fail-closed，防止自回）"
        )
        return

    try:
        items = []
        notifications = await self.bili.get_reply_notifications()
        if not notifications:
            logger.info("通知 API 返回空")
        elif notifications.get("code") != 0:
            logger.info(f"通知 API 返回错误: code={notifications.get('code')} msg={notifications.get('message')}")
        else:
            items = notifications.get("data", {}).get("items", []) or []
            # PRD V6：通知数变化时才打 INFO，否则降级 DEBUG
            _count = len(items)
            if _count != self._last_notify_count:
                logger.info(f"通知 API 返回 {_count} 条评论")
                self._last_notify_count = _count
            else:
                logger.debug(f"通知 API 返回 {_count} 条评论（无变化）")

        # 合并 @我的 通知（结构与 reply 一致，标记 _source="at" 以便后续区分）
        try:
            at_notifications = await self.bili.get_at_notifications()
            if at_notifications and at_notifications.get("code") == 0:
                at_items = at_notifications.get("data", {}).get("items", []) or []
                if at_items:
                    # 标记 at 来源，后续处理时优先看视频
                    for at_item in at_items:
                        at_item["_source"] = "at"
                    # 去重：与 reply 通知按 source_id 去重
                    existing_ids = {
                        str((it.get("item") or {}).get("source_id") or it.get("id") or "")
                        for it in items
                    }
                    for at_item in at_items:
                        at_id = str((at_item.get("item") or {}).get("source_id") or at_item.get("id") or "")
                        if at_id and at_id not in existing_ids:
                            items.append(at_item)
                            existing_ids.add(at_id)
                    # 与 reply 通知一致：数量无变化时降级 DEBUG，避免主循环每轮刷屏
                    _at_count = len(at_items)
                    _merged = len(items)
                    if (
                        _at_count != self._last_at_notify_count
                        or _merged != self._last_at_merged_count
                    ):
                        logger.info(
                            f"@我的 通知返回 {_at_count} 条，合并后候选共 {_merged} 条"
                        )
                        self._last_at_notify_count = _at_count
                        self._last_at_merged_count = _merged
                    else:
                        logger.debug(
                            f"@我的 通知返回 {_at_count} 条，合并后候选共 {_merged} 条（无变化）"
                        )
        except Exception as at_exc:
            logger.warning(f"获取@我的通知失败: {at_exc}")

        batch_size = config.get("reply", {}).get("batch_size", 10)
        try:
            own_dynamic_items = await self._collect_own_dynamic_comment_items(
                config=config,
                limit=batch_size,
            )
            if own_dynamic_items:
                existing_ids = {
                    str((it.get("item") or {}).get("source_id") or it.get("id") or "")
                    for it in items
                }
                for own_item in own_dynamic_items:
                    own_id = str((own_item.get("item") or {}).get("source_id") or own_item.get("id") or "")
                    if own_id and own_id not in existing_ids:
                        items.append(own_item)
                        existing_ids.add(own_id)
                _own_count = len(own_dynamic_items)
                # PRD V6：候选数变化时才打 INFO
                if _own_count != self._last_own_dynamic_count:
                    logger.info(f"自动态补扫发现 {_own_count} 条候选评论")
                    self._last_own_dynamic_count = _own_count
                else:
                    logger.debug(f"自动态补扫发现 {_own_count} 条候选评论（无变化）")
        except Exception as own_exc:
            logger.warning(f"自动态评论补扫失败: {own_exc}")

        if not items:
            logger.info("评论区无新通知")
            return

        new_items = []
        for item in items:
            # source_id 是触发通知的评论 rpid（用户的评论），用于去重
            item_detail = item.get("item", {})
            source_id = item_detail.get("source_id", 0)
            comment_type = item_detail.get("business_id", 1)
            # PRD 4.6：统一转 str，避免 int/str 类型不一致导致去重失败
            rpid = str(source_id or item.get("id") or "")
            # PRD V4 REP-001 / REP-601：使用 is_processed 跳过任何已有记录的评论
            # （终态 + 中间态 + deferred/retry_wait），防止进行中的评论被重复拉入队列。
            # deferred 与 retry_wait 由 _process_retryable_comments 单独处理。
            if rpid and not self.reply_state_store.is_processed(comment_type, rpid):
                new_items.append(item)
        new_items = new_items[:batch_size]

        if not new_items:
            # PRD V6：全部已回复时用指纹（数量+rpid 集合）判断是否变化
            _fp = f"{len(items)}:" + ",".join(sorted(
                str((it.get("item") or {}).get("source_id") or it.get("id") or "")
                for it in items
            ))
            if _fp != self._last_all_replied_fingerprint:
                logger.info(f"评论通知 {len(items)} 条，全部已回复过，跳过")
                self._last_all_replied_fingerprint = _fp
            else:
                logger.debug(f"评论通知 {len(items)} 条，全部已回复过，跳过（无变化）")
            return

        # 有新评论待回复时重置指纹，下次全回复时会重新打 INFO
        self._last_all_replied_fingerprint = None
        logger.info(f"发现 {len(new_items)} 条新评论待回复")

        # @回复会触发整段视频下载+视听分析，单条可达数分钟。若一批通知
        # 里有很多 @，会让私信/主动行为/番剧/重试在同一个调度循环里被
        # 饿死数小时。每轮最多完整预观看 2 个视频，其余降级为元数据回复。
        video_watch_quota = 2

        for item in new_items:
            try:
                item_detail = item.get("item", {})
                oid = item_detail.get("subject_id", 0)
                comment_type = item_detail.get("business_id", 1)
                # source_id 是触发通知的评论 rpid（用户的评论），root_id 是根评论 rpid
                root_id = item_detail.get("root_id", 0)
                source_id = item_detail.get("source_id", 0)
                # reply_id 用于去重，使用 source_id（用户的评论 rpid）
                # PRD 4.6：统一转 str
                reply_id = str(source_id or item.get("id") or "")
                user = item.get("user", {})
                user_id = str(user.get("mid", ""))
                username = user.get("nickname", "未知用户")
                comment_text = item_detail.get("source_content", "")

                # 调试日志
                logger.info(f"通知字段: reply_id={reply_id}, oid={oid}, type={comment_type}, "
                            f"root_id={root_id}, source_id={source_id}, username={username}")

                if not comment_text:
                    # S5：空 source_content 不得 silent skip，记 ignored 终态
                    logger.info(
                        "评论 source_content 为空，标记 ignored: reply_id=%s",
                        reply_id,
                    )
                    if reply_id:
                        self.reply_state_store.mark_ignored(
                            comment_type,
                            reply_id,
                            rule="empty_source_content",
                            notification=item,
                        )
                    continue

                # V6: every observed comment is archived before reply filters.
                try:
                    from bilibot.memory_brain.ingestion import comment_observation

                    await self._archive_required(
                        comment_observation(
                            account_id=self.account_id or "default",
                            comment_type=comment_type,
                            reply_id=reply_id,
                            text=comment_text,
                            actor_id=self._pseudonymize_actor_id(user_id),
                            username=username,
                            oid=str(oid),
                            title=self._comment_memory_title(username, comment_text, reply_id),
                            context=item_detail.get("target_context") or {},
                            persona_id=self._get_current_persona_id(),
                        )
                    )
                except Exception:
                    self.reply_state_store.mark_deferred(
                        comment_type,
                        reply_id,
                        reason="memory_archive_failed",
                        error_code="MEMORY_ARCHIVE_FAILED",
                        increment_attempt=False,
                    )
                    continue

                # PRD V4 REP-002：过滤规则真实生效
                reply_cfg = config.get("reply", {})
                features = config.get("features", {})

                # 过短评论 → ignored（终态）
                min_len = int(reply_cfg.get("min_comment_length", 2))
                if len(comment_text.strip()) < min_len:
                    logger.info(f"评论过短(<{min_len})，忽略: {comment_text[:20]}")
                    self.reply_state_store.mark_ignored(
                        comment_type, reply_id,
                        rule=f"min_comment_length({min_len})",
                        notification=item,
                    )
                    continue

                # 自己的评论 → ignored（终态）
                reply_own = reply_cfg.get("reply_own", False)
                if not reply_own and self._bot_uid and user_id == str(self._bot_uid):
                    logger.info("自己的评论，reply_own=false，忽略")
                    self.reply_state_store.mark_ignored(
                        comment_type, reply_id,
                        rule="reply_own=false",
                        notification=item,
                    )
                    continue

                # 输入黑名单 → ignored（终态）
                block_keywords = reply_cfg.get("block_keywords", [])
                # REP-605：兼容字符串配置，避免按字符迭代
                if isinstance(block_keywords, str):
                    block_keywords = [block_keywords]
                if block_keywords and isinstance(block_keywords, list):
                    _blocked = False
                    for kw in block_keywords:
                        if kw and kw in comment_text:
                            logger.info(f"评论命中黑名单关键词 '{kw}'，忽略")
                            self.reply_state_store.mark_ignored(
                                comment_type, reply_id,
                                rule=f"block_keyword:{kw}",
                                notification=item,
                            )
                            _blocked = True
                            break
                    if _blocked:
                        continue

                # 记录 discovered 状态（含原始通知，供后续审计）
                self.reply_state_store.upsert(
                    comment_type, reply_id, "context_building",
                    notification=item, persona_id=self._get_current_persona_id(),
                )

                # 获取评论上下文（楼中楼对话历史）
                comment_context = ""
                reply_replies = []
                context_root = root_id if root_id else source_id
                if root_id or source_id:
                    try:
                        replies_data = await self.bili.get_comment_replies(
                            oid=oid, root=context_root, comment_type=comment_type, ps=30,
                        )
                        if replies_data and replies_data.get("code") == 0:
                            reply_replies = replies_data.get("data", {}).get("replies", [])
                    except Exception as e:
                        logger.warning(f"获取评论上下文失败: {e}")
                # 通知自带的上下文字段：根评论 + 被回复评论（楼中楼接口不返回这些）
                # 这对 @ 通知尤其重要：bot 需要知道 @ 发生在什么对话语境下
                _notify_context_lines = []
                root_reply_content = (item_detail.get("root_reply_content") or "").strip()
                if root_reply_content:
                    _notify_context_lines.append(f"[根评论] {root_reply_content}")
                target_reply_content = (item_detail.get("target_reply_content") or "").strip()
                if target_reply_content:
                    _notify_context_lines.append(f"[被回复的评论] {target_reply_content}")
                if _notify_context_lines:
                    if comment_context:
                        comment_context = "\n".join(_notify_context_lines) + "\n" + comment_context
                    else:
                        comment_context = "\n".join(_notify_context_lines)
                    logger.info(f"通知自带上下文: 根评论={'有' if root_reply_content else '无'}, 被回复={'有' if target_reply_content else '无'}")
                if reply_replies:
                    try:
                        context_lines = await self._archive_comment_thread_context(
                            reply_replies,
                            oid=oid,
                            comment_type=comment_type,
                            thread_key=context_root,
                        )
                    except Exception:
                        self.reply_state_store.mark_deferred(
                            comment_type,
                            reply_id,
                            reason="comment_context_archive_failed",
                            error_code="MEMORY_ARCHIVE_FAILED",
                            increment_attempt=False,
                        )
                        continue
                    if context_lines:
                        if comment_context:
                            comment_context = comment_context + "\n" + "\n".join(context_lines)
                        else:
                            comment_context = "\n".join(context_lines)
                        logger.info(f"获取评论上下文: {len(context_lines)} 条对话")

                # PRD §5.9：黑名单过滤 → ignored（终态）
                if self.safety_checker is not None and self.safety_checker.is_blacklisted(user_id):
                    logger.info(f"用户 {username}({user_id}) 在黑名单中，跳过回复")
                    self.reply_state_store.mark_ignored(
                        comment_type, reply_id, rule="blacklist",
                    )
                    continue

                # @来源的视频评论：先看视频再回复，让回复基于真实观看内容
                if item.get("_source") == "at" and comment_type == 1 and oid:
                    if video_watch_quota > 0:
                        video_watch_quota -= 1
                        try:
                            watched = await self._watch_video_for_reply(bvid="", oid=oid)
                            if watched:
                                logger.info(f"@回复：已预观看视频 oid={oid}，回复将基于完整视频上下文")
                            else:
                                logger.info("@回复：预观看失败/降级，回复将基于元数据")
                        except Exception as watch_exc:
                            logger.warning(f"@回复：预观看异常，降级为元数据回复: {watch_exc}")
                    else:
                        logger.info(
                            "@回复：本轮视频预观看配额已满，降级为元数据回复 oid=%s",
                            oid,
                        )

                # PRD V4 §4.3.1：构建完整 ReplyContext
                reply_context = None
                if self.comment_context_service is not None:
                    try:
                        reply_context = await self.comment_context_service.build_context(
                            notification=item,
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
                                comment_type,
                                reply_id,
                                reason="video_context_archive_failed",
                                error_code="MEMORY_ARCHIVE_FAILED",
                                increment_attempt=False,
                            )
                            continue
                        logger.warning(f"构建评论上下文失败，降级处理: {e}")
                        reply_context = None

                # PRD V4 §9.1：状态 → generation_pending
                self.reply_state_store.upsert(comment_type, reply_id, "generation_pending")

                # 生成回复（传入 reply_context + 评论上下文）
                # BUG A-001：直接调用 _generate_reply_impl 以获取 GenerationOutcome，
                # 不再通过 generate_reply 包装器（它把 skip/retryable/permanent 全折叠成 None）
                try:
                    outcome = await self.reply_gen._generate_reply_impl(
                        user_id=user_id,
                        username=username,
                        comment=comment_text,
                        thread_id=str(reply_id),
                        oid=oid,
                        comment_type=comment_type,
                        reply_context=reply_context,
                        comment_context=comment_context,
                    )
                except Exception as llm_err:
                    # PRD V4 §9.1：LLM 失败 → deferred（非终态，可恢复）
                    logger.error(f"LLM 生成失败，deferred: {llm_err}")
                    _err_name = type(llm_err).__name__
                    _no_burn = (
                        _err_name == "RateLimitExhaustedError"
                        or "rate limit" in str(llm_err).lower()
                        or "rate-limited" in str(llm_err).lower()
                    )
                    self.reply_state_store.mark_deferred(
                        comment_type, reply_id,
                        reason=f"llm_error: {llm_err}",
                        error_code="LLM_RATE_LIMITED" if _no_burn else "LLM_ERROR",
                        increment_attempt=not _no_burn,
                    )
                    continue

                # BUG A-001：按 outcome.status 分派，不再用 None 判断
                if outcome.is_skip:
                    # LLM 明确决定不回复 → ignored（终态）
                    logger.debug(f"LLM 决定跳过回复 from {username}")
                    self.reply_state_store.mark_ignored(
                        comment_type, reply_id, rule="llm_no_reply",
                    )
                    continue

                if not outcome.is_generated:
                    # 永久错误（如 LLM 未配置）→ failed 终态，禁止 deferred 空转
                    if getattr(outcome, "is_permanent_error", False):
                        logger.error(
                            f"LLM 永久失败 (code={outcome.error_code})，标记 failed"
                        )
                        self.reply_state_store.mark_failed(
                            comment_type, reply_id,
                            reason=f"generation_permanent: {outcome.error_code}",
                            error_code=outcome.error_code or "GEN_PERMANENT",
                        )
                        continue
                    logger.warning(
                        f"LLM 生成未成功 (status={outcome.status}, code={outcome.error_code})，deferred"
                    )
                    _gen_code = outcome.error_code or "GEN_FAILED"
                    # 429 / 全 key 冷却：条件失败，不烧 attempt（与 RATE_LIMIT 一致）
                    _no_burn = _gen_code in (
                        "LLM_RATE_LIMITED",
                        "RATE_LIMIT",
                        "RATE_LIMITED",
                    )
                    self.reply_state_store.mark_deferred(
                        comment_type, reply_id,
                        reason=f"generation_{outcome.status}: {outcome.error_code}",
                        error_code=_gen_code,
                        increment_attempt=not _no_burn,
                    )
                    continue

                reply_text = outcome.text
                audit_id = outcome.audit_id
                context_meta = outcome.context_meta or {}

                # PRD-V5 §6.2 / REP-502：获取有效文本后、安全检查之前持久化生成结果
                # 确保即使安全检查或发布失败，重试时也能使用原始文本而非重新生成
                _gen_persona_id = (
                    context_meta.get("persona_id")
                    or self._get_current_persona_id()
                    or ""
                )
                self.reply_state_store.save_generation_result(
                    comment_type, reply_id,
                    text=reply_text,
                    persona_id=_gen_persona_id,
                    audit_id=audit_id,
                )

                # PRD V4 §9.1：状态 → safety_pending
                self.reply_state_store.upsert(comment_type, reply_id, "safety_pending")

                # PRD §5.9：发布前内容检查 + 频率限制（fail-closed：无 checker 禁止发布）
                if self.safety_checker is None:
                    logger.error(
                        "safety_checker 未初始化，拒绝发布评论（fail-closed）: reply_id=%s",
                        reply_id,
                    )
                    self.reply_state_store.mark_deferred(
                        comment_type, reply_id,
                        reason="safety_checker_missing", error_code="NO_SAFETY_CHECKER",
                        increment_attempt=False,
                    )
                    continue

                persona_id_for_check = self._get_current_persona_id()
                rate_reserved = False
                try:
                    passed, reason = await self.safety_checker.check_content(
                        reply_text, scene="reply_comment",
                        persona_id=persona_id_for_check,
                        account_id=self.account_id,
                    )
                    if not passed:
                        # PRD V4 §9.1：安全检查未通过 → rejected（终态）
                        logger.warning(f"回复内容安全检查未通过: {reason}")
                        if audit_id and self.audit_store:
                            try:
                                self.audit_store.mark_published(
                                    audit_id, published=False,
                                    target={
                                        "kind": "reply_comment",
                                        "rpid": str(reply_id),
                                        "source_rpid": str(reply_id),
                                        "comment_type": int(comment_type),
                                        "account_id": self.account_id or "",
                                    },
                                    failure_reason=f"safety_check: {reason}",
                                )
                            except Exception:
                                pass
                        self.reply_state_store.mark_rejected(
                            comment_type, reply_id, reason=f"safety_check: {reason}",
                        )
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{comment_type}:{reply_id}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=False,
                            status="rejected",
                            title=f"回复评论 {reply_id}",
                            scene="reply_comment",
                            metadata={
                                "reply_id": reply_id,
                                "oid": str(oid),
                                "reason_code": "SAFETY_REJECTED",
                            },
                        )
                        continue
                    rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                        scene="reply_comment", account_id=self.account_id,
                    )
                    if not rate_ok:
                        logger.warning("评论发布频率限制触发，deferred: %s", rate_reason)
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason="rate_limited", error_code="RATE_LIMIT",
                            increment_attempt=False,
                        )
                        continue
                    rate_reserved = True
                except Exception as e:
                    # PRD V4 §9.1：安全检查异常 → deferred（非终态，可恢复）
                    logger.error(f"安全检查异常，deferred: {e}", exc_info=True)
                    self.reply_state_store.mark_deferred(
                        comment_type, reply_id,
                        reason=f"safety_exception: {e}", error_code="SAFETY_ERROR",
                        increment_attempt=False,
                    )
                    continue

                # PRD V4 §9.1：状态 → publish_pending
                self.reply_state_store.upsert(comment_type, reply_id, "publish_pending")

                # 发表回复
                # root = 根评论rpid（一级评论时=source_id，二级评论时=root_id）
                # parent = 要回复的那条评论rpid（始终=source_id，即用户的评论）
                comment_root = root_id if root_id else source_id
                # 评论发布节流：默认 120s 最小间隔。低等级账号短时间连续评论会
                # 触发阿瓦隆把后续回复隐藏（state=17）。
                try:
                    _publish_interval = float(
                        reply_cfg.get("min_publish_interval_seconds", 120)
                    )
                except (TypeError, ValueError):
                    _publish_interval = 120.0
                _last_publish = float(
                    getattr(self, "_last_comment_publish_ts", 0.0) or 0.0
                )
                _publish_wait = _publish_interval - (time.time() - _last_publish)
                if _publish_wait > 0:
                    logger.info(
                        "评论发布节流，等待 %.0f 秒: reply_id=%s",
                        _publish_wait,
                        reply_id,
                    )
                    await asyncio.sleep(_publish_wait)
                try:
                    success = await self.bili.post_comment(
                        oid=oid,
                        content=reply_text,
                        comment_type=comment_type,
                        rpid=comment_root,
                        parent=source_id,
                    )
                except Exception as post_err:
                    # Exception after optional raise paths: treat as uncertain (no auto-retry, no refund)
                    logger.error(f"发表评论异常 reply_id={reply_id}: {post_err}")
                    self.reply_state_store.mark_result_unknown(
                        comment_type, reply_id,
                        reason=f"post_exception: {type(post_err).__name__}",
                        error_code="POST_EXCEPTION",
                    )
                    try:
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{comment_type}:{reply_id}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=False,
                            status="result_unknown",
                            title=f"回复评论 {reply_id}",
                            scene="reply_comment",
                            metadata={
                                "reply_id": reply_id,
                                "oid": str(oid),
                                "reason_code": "POST_EXCEPTION",
                            },
                        )
                    except Exception:
                        logger.error(
                            "unknown comment result could not be archived: reply_id=%s",
                            reply_id,
                        )
                    continue
                if success is None:
                    # Transport uncertainty (timeout/5xx/non-json): no refund, no auto-retry
                    logger.error(
                        "评论结果不确定（不自动重发）: reply_id=%s", reply_id,
                    )
                    self.reply_state_store.mark_result_unknown(
                        comment_type, reply_id,
                        reason="post_comment transport uncertainty",
                        error_code="RESULT_UNKNOWN",
                    )
                    try:
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{comment_type}:{reply_id}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=False,
                            status="result_unknown",
                            title=f"回复评论 {reply_id}",
                            scene="reply_comment",
                            metadata={
                                "reply_id": reply_id,
                                "oid": str(oid),
                                "reason_code": "RESULT_UNKNOWN",
                            },
                        )
                    except Exception:
                        logger.error(
                            "unknown comment result could not be archived: reply_id=%s",
                            reply_id,
                        )
                    continue
                if success is False:
                    self._check_bili_risk_control("reply_comment")
                    if rate_reserved and self.safety_checker is not None:
                        try:
                            self.safety_checker.refund_publish(
                                scene="reply_comment", account_id=self.account_id,
                            )
                        except Exception:
                            pass

                # PRD §5.9：发布成功后记录内容（频率已在预占时记录）
                if success and self.safety_checker is not None:
                    try:
                        self.safety_checker.record_content(reply_text, account_id=self.account_id)
                    except Exception:
                        pass

                # PRD V4 §4.5.3：发布结果同步到 audit
                if audit_id and self.audit_store:
                    try:
                        if success:
                            self.audit_store.mark_published(
                                audit_id, published=True,
                                target={
                                    "kind": "reply_comment",
                                    "rpid": str(reply_id),
                                    "source_rpid": str(reply_id),
                                    "comment_type": int(comment_type),
                                    "oid": str(oid),
                                    "account_id": self.account_id or "",
                                    "published_at": datetime.now().isoformat(),
                                },
                            )
                        else:
                            self.audit_store.mark_published(
                                audit_id, published=False,
                                target={
                                    "kind": "reply_comment",
                                    "rpid": str(reply_id),
                                    "source_rpid": str(reply_id),
                                    "comment_type": int(comment_type),
                                    "account_id": self.account_id or "",
                                },
                                failure_reason="bili.post_comment 返回 False",
                            )
                    except Exception as e:
                        logger.debug(f"audit mark_published 失败: {e}")

                if success:
                    self._last_comment_publish_ts = time.time()
                    logger.info(f"回复成功 -> {username}")
                    # PRD V4 REP-001：标记终态 published
                    self.reply_state_store.mark_published(comment_type, reply_id)
                    # 发布后可见性检查：发现阿瓦隆隐藏则删除该回复并进入冷却。
                    try:
                        visible = await self._verify_and_cleanup_reply_visibility(
                            oid=oid,
                            comment_root=comment_root,
                            reply_text=reply_text,
                            comment_type=comment_type,
                        )
                        if not visible:
                            logger.warning(
                                "评论被 B站风控隐藏，已清理并进入冷却: reply_id=%s",
                                reply_id,
                            )
                    except Exception as vis_exc:
                        logger.warning(
                            "评论可见性检查异常: reply_id=%s %s",
                            reply_id,
                            type(vis_exc).__name__,
                        )

                    try:
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{comment_type}:{reply_id}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=True,
                            title=f"已回复评论 {reply_id}",
                            scene="reply_comment",
                            metadata={"reply_id": reply_id, "oid": str(oid)},
                        )
                    except Exception:
                        # The platform action already happened. Pause subsequent
                        # actions and leave the durable intent as evidence.
                        logger.error(
                            "published comment result could not be archived: reply_id=%s",
                            reply_id,
                        )
                    else:
                        self._notify_companion_comment_replied(
                            title=str(oid or "")[:40],
                            preview=str(reply_text or "")[:80],
                            proactive=False,
                        )

                    # PRD V4 REP-006：好感度仅在发布成功后应用
                    features = config.get("features", {})
                    if features.get("affection", False) and self.knowledge_memory:
                        try:
                            if hasattr(self.knowledge_memory, 'update_user_affection'):
                                self.knowledge_memory.update_user_affection(user_id, delta=1)
                        except Exception as aff_err:
                            logger.warning(f"好感度更新失败（不影响回复）: {aff_err}")
                else:
                    # PRD V4 REP-005：发布失败 → retry_wait（非终态，指数退避重试）
                    # B站 12002（当前页面评论功能已关闭）是永久失败，重试只会
                    # 反复烧 API 请求并污染 retry 队列，直接终态 failed。
                    last_code = int(getattr(self.bili, "last_api_code", 0) or 0)
                    if last_code == 12002:
                        logger.warning(
                            "评论功能已关闭（12002），标记失败: reply_id=%s", reply_id
                        )
                        self.reply_state_store.mark_failed(
                            comment_type, reply_id,
                            reason="comment section closed (B站 12002)",
                            error_code="PERMANENT_PUBLISH_REJECTED",
                        )
                        reason_code = "PERMANENT_PUBLISH_REJECTED"
                    else:
                        logger.warning(f"回复发表失败，进入 retry_wait: {reply_text[:30]}...")
                        self.reply_state_store.mark_retry_wait(
                            comment_type, reply_id,
                            reason="bili.post_comment returned False",
                            error_code="PUBLISH_FAILED",
                        )
                        reason_code = "PUBLISH_FAILED"
                    try:
                        await self._archive_bot_action(
                            action_key=f"comment_reply:{comment_type}:{reply_id}",
                            action_type="reply_comment",
                            text=reply_text,
                            published=False,
                            status="failed",
                            title=f"回复评论 {reply_id}",
                            scene="reply_comment",
                            metadata={
                                "reply_id": reply_id,
                                "oid": str(oid),
                                "reason_code": reason_code,
                            },
                        )
                    except Exception:
                        logger.error(
                            "failed comment result could not be archived: reply_id=%s",
                            reply_id,
                        )

                await asyncio.sleep(2)

            except Exception as e:
                # S4：单条处理失败时若已知 comment_type/reply_id，记 deferred 可恢复
                logger.error(f"处理评论失败: {e}")
                try:
                    _ct = int(locals().get("comment_type") or 0) or int(
                        (locals().get("item_detail") or {}).get("business_id", 0) or 0
                    )
                    _rid = str(locals().get("reply_id") or "")
                    if not _rid:
                        _sid = (locals().get("item_detail") or {}).get("source_id", 0)
                        _rid = str(_sid or (locals().get("item") or {}).get("id") or "")
                    if _rid:
                        self.reply_state_store.mark_deferred(
                            _ct or 1,
                            _rid,
                            reason=f"unhandled: {type(e).__name__}: {e}",
                            error_code="UNHANDLED",
                            increment_attempt=False,
                        )
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"检查评论失败: {e}")
