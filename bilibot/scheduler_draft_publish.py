"""Approved dynamic-draft publishing extracted from scheduler.py."""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger("bilibot.scheduler_draft_publish")


async def do_publish_approved_draft(self, task_id: str, draft_id: str):
    """PRD-V5 §4.1 DYN-501：审核通过后的独立发布任务

    - start → mark_publishing（approved/retry_wait → publishing，原子 claim）
    - 上传图片（如有 base64 引用）+ 调用 post_dynamic_text
    - 成功 → mark_published；失败 → mark_retry_wait
    """
    # DYN-604：safety_checker None → fail-closed（与 _do_post_dynamic 一致）
    if self.safety_checker is None:
        self._fail_task(task_id, "SAFETY_CHECKER_MISSING", "safety_checker not initialized, fail-closed", retryable=False)
        return
    if not self.bili:
        self._fail_task(task_id, "NO_BILI", "bili API 未初始化", retryable=False)
        return
    # PRD-V5 §7：claim → start
    if not self.task_store.start(task_id):
        logger.warning(f"publish draft TaskRun {task_id} start 失败（可能已被处理）")
        return

    try:
        # DYN-602：暂停检查（fail-closed）。在 mark_publishing 之前暂停时草稿仍为 approved，
        # mark_retry_wait 只接受 publishing 来源，这里不要调用（会是 no-op）。
        # 仅失败 TaskRun（retryable），下次重试会再次从 approved claim。
        if self.safety_checker is not None:
            if self.safety_checker.is_paused():
                self._fail_task(task_id, "GLOBAL_PAUSED", "全局暂停状态", retryable=True)
                return
            if self.safety_checker.is_account_paused(self.account_id):
                self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
                return

        store = self._get_draft_store()
        draft = store.get(draft_id)
        if draft is None:
            self._fail_task(task_id, "DRAFT_NOT_FOUND",
                            f"草稿不存在: {draft_id}", retryable=False)
            return

        # 多账号隔离：草稿归属必须与当前 scheduler 账号一致（先于 claim）
        # 空 draft_acc 或空 self.account_id 也不得放行跨账号（对齐 task API 空 account 403）
        draft_acc = str(getattr(draft, "account_id", "") or "")
        self_acc = str(self.account_id or "")
        if self_acc and draft_acc != self_acc:
            logger.error(
                "草稿账号不匹配，拒绝发布 draft=%s draft_acc=%s self=%s",
                draft_id, draft_acc, self_acc,
            )
            self._fail_task(
                task_id, "DRAFT_ACCOUNT_MISMATCH",
                f"草稿账号 {draft_acc or '(empty)'} 与当前账号 {self_acc} 不匹配",
                retryable=False,
            )
            return

        # 原子 claim：approved/retry_wait → publishing
        if not store.mark_publishing(draft_id):
            self._fail_task(task_id, "DRAFT_NOT_PUBLISHABLE",
                            f"草稿状态 {draft.status} 不可发布", retryable=False)
            return

        content = draft.content
        image_refs = draft.image_refs
        # PRD V6：不单独记录 intent，仅在最终结果时归档，避免同一动态两条记忆。

        # 上传图片（如有 base64 引用）
        image_list = []
        if image_refs:
            import base64 as _b64
            _img_failed = False
            for ref in image_refs:
                try:
                    img_bytes = _b64.b64decode(ref)
                    img_info = await self.bili.upload_dynamic_image(img_bytes)
                    if img_info:
                        image_list.append(img_info)
                    else:
                        logger.warning("草稿配图上传失败")
                        _img_failed = True
                except Exception as e:
                    logger.warning(f"草稿配图上传异常: {e}")
                    _img_failed = True
            # DYN-607：审核模式草稿（有 image_refs）图片上传失败不应降级为纯文字
            if _img_failed or not image_list:
                store.mark_retry_wait(
                    draft_id, "草稿配图上传失败，等待重试"
                )
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}:image_upload",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="deferred",
                    title="已审核动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "reason_code": "IMAGE_UPLOAD_FAILED",
                    },
                )
                self._fail_task(task_id, "IMAGE_UPLOAD_FAILED",
                                "草稿配图上传失败", retryable=True)
                return

        # DYN-602：发布前原子预占频率配额
        rate_reserved = False
        # 发布前再次内容检查 + 限流（与直发路径一致；审核后内容可能被编辑）
        if self.safety_checker is None:
            try:
                store.mark_retry_wait(draft_id, "safety_checker missing")
            except Exception:
                pass
            self._fail_task(
                task_id, "NO_SAFETY_CHECKER",
                "safety_checker 未初始化", retryable=False,
            )
            return
        try:
            passed, reason = await self.safety_checker.check_content(
                content,
                scene="dynamic_post",
                persona_id=getattr(draft, "persona_id", "") or "",
                account_id=self.account_id,
            )
        except Exception as e:
            logger.error(
                "草稿真发安全检查异常（拒绝发布）: %s", e, exc_info=True,
            )
            try:
                store.mark_retry_wait(
                    draft_id, f"safety_check_exception: {type(e).__name__}",
                )
            except Exception:
                pass
            self._fail_task(
                task_id, "SAFETY_CHECK_ERROR",
                f"safety_check_exception: {type(e).__name__}",
                retryable=True,
            )
            return
        if not passed:
            logger.warning("草稿真发内容安全检查未通过: %s", reason)
            try:
                store.mark_retry_wait(
                    draft_id, f"safety_check: {reason}",
                )
            except Exception:
                pass
            self._fail_task(
                task_id, "SAFETY_REJECTED",
                f"safety_check: {reason}", retryable=True,
            )
            await self._archive_bot_action(
                action_key=f"dynamic_draft:{draft_id}:safety",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="rejected",
                title="已审核动态草稿",
                scene="dynamic_post",
                metadata={
                    "draft_id": draft_id,
                    "task_id": task_id,
                    "reason_code": "SAFETY_REJECTED",
                    "reason": str(reason)[:200],
                },
            )
            return

        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
            scene="dynamic_post", account_id=self.account_id,
        )
        if not rate_ok:
            try:
                store.mark_retry_wait(
                    draft_id, "dynamic_post rate limited"
                )
            except Exception as _e:
                logger.warning(f"Task 11.2: 限流回退草稿状态失败: {_e}")
            self._fail_task(task_id, "RATE_LIMITED",
                            "dynamic_post rate limited", retryable=True)
            await self._archive_bot_action(
                action_key=f"dynamic_draft:{draft_id}:publish_rate",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="deferred",
                title="已审核动态草稿",
                scene="dynamic_post",
                metadata={
                    "draft_id": draft_id,
                    "task_id": task_id,
                    "reason_code": "RATE_LIMITED",
                },
            )
            return
        rate_reserved = True

        # 发布
        try:
            success = await self.bili.post_dynamic_text(
                content, images=image_list if image_list else None
            )
        except Exception as publish_exc:
            # 结果不确定：不退配额
            error_name = type(publish_exc).__name__
            store.mark_result_unknown(draft_id, error_name)
            self._mark_task_result_unknown(
                task_id, f"dynamic draft publish exception: {error_name}"
            )
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="result_unknown",
                    title="已审核动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "reason_code": error_name,
                    },
                )
            except Exception:
                logger.error(
                    "unknown dynamic draft publish result could not be archived"
                )
            return
        if success is None:
            logger.error("草稿动态发布结果不确定（不自动重发） draft=%s", draft_id)
            try:
                store.mark_result_unknown(draft_id, "post_dynamic transport uncertainty")
            except Exception:
                pass
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="result_unknown",
                    title="已审核动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "reason_code": "RESULT_UNKNOWN",
                    },
                )
            except Exception:
                logger.error("unknown draft dynamic result could not be archived")
            self._mark_task_result_unknown(
                task_id, "post_dynamic_text transport uncertainty"
            )
            return

        if success is False:
            self._check_bili_risk_control("dynamic_post")
            if rate_reserved and self.safety_checker is not None:
                try:
                    self.safety_checker.refund_publish(
                        scene="dynamic_post", account_id=self.account_id,
                    )
                except Exception:
                    pass

        if success:
            store.mark_published(draft_id)
            logger.info(f"动态草稿发布成功: {draft_id}")
            if self.safety_checker is not None:
                try:
                    self.safety_checker.record_content(content, account_id=self.account_id)
                except Exception:
                    pass
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}",
                    action_type="dynamic_post",
                    text=content,
                    published=True,
                    title="已发布动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "has_image": bool(image_list),
                    },
                )
            except Exception:
                logger.error("published dynamic draft result could not be archived")
                self._mark_task_result_unknown(
                    task_id, "dynamic draft published but V6 result archive failed"
                )
                return
            # 审核通过真发：生活面回写 + 与 draft_id 关联（脑归档已在上方完成）
            self._notify_companion_dynamic_posted(
                content=content or "",
                topic="",
                draft_id=draft_id,
                task_id=task_id,
            )
            self._succeed_task(task_id, {
                "success": True,
                "summary": f"动态草稿发布成功: {draft_id}",
                "draft_id": draft_id,
                "published_at": datetime.now().isoformat(),
                "platform": "bilibili",
                "kind": "dynamic",
            })
        else:
            store.mark_retry_wait(
                draft_id, "bili.post_dynamic_text 返回 False"
            )
            await self._archive_bot_action(
                action_key=f"dynamic_draft:{draft_id}",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="failed",
                title="已审核动态草稿",
                scene="dynamic_post",
                metadata={
                    "draft_id": draft_id,
                    "task_id": task_id,
                    "reason_code": "BILI_API_FALSE",
                },
            )
            self._fail_task(task_id, "DYNAMIC_PUBLISH_FAILED",
                            "bili.post_dynamic_text 返回 False", retryable=True)
    except Exception as e:
        # After mark_publishing / post_dynamic, unexpected errors are result-unknown
        # to avoid auto re-publish. Pre-publish failures already returned above.
        logger.error(f"发布动态草稿失败: {e}", exc_info=True)
        try:
            self._get_draft_store().mark_result_unknown(draft_id, str(e))
        except Exception:
            try:
                self._get_draft_store().mark_retry_wait(draft_id, str(e))
            except Exception:
                pass
        self._mark_task_result_unknown(task_id, f"DYNAMIC_ERROR: {type(e).__name__}")
