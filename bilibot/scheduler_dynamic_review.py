"""Dynamic review-mode handling extracted from scheduler.py."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bilibot.scheduler_dynamic_review")


async def handle_dynamic_review_mode(
    self,
    task_id: Optional[str],
    action_key: str,
    content: str,
    persona_id: str,
    audit_id: Optional[str],
    dp_cfg: Dict[str, Any],
):
    """PRD-V5 §4.1 DYN-501：审核模式处理

    - 执行安全检查（作为审核依据，不阻断草稿创建）
    - 生成配图（不上传 B站，存 base64 到草稿）
    - 安全通过 → awaiting_review；安全拒绝 → rejected
    - 不调用 post_dynamic_text / upload_dynamic_image
    """
    store = self._get_draft_store()

    # 安全检查（审核模式下作为审核依据快照）
    safety_passed = True
    safety_reason = ""
    safety_snapshot: Dict[str, Any] = {
        "scene": "dynamic_post",
        "persona_id": persona_id,
        "account_id": self.account_id,
        "checked_at": datetime.now().isoformat(),
    }
    if self.safety_checker is not None:
        try:
            passed, reason = await self.safety_checker.check_content(
                content, scene="dynamic_post",
                persona_id=persona_id,
                account_id=self.account_id,
            )
            safety_passed = passed
            safety_reason = reason
            safety_snapshot["passed"] = passed
            safety_snapshot["reason"] = reason
        except Exception as e:
            # DYN-003：安全检查异常 → 拒绝（不降级放行）
            safety_passed = False
            safety_reason = f"safety_check_exception: {e}"
            safety_snapshot["passed"] = False
            safety_snapshot["reason"] = safety_reason
            logger.error(f"审核模式安全检查异常（DYN-003）: {e}", exc_info=True)
    else:
        # DYN-604：safety_checker None → fail-closed（不降级放行）
        safety_passed = False
        safety_reason = "safety_checker unavailable"
        safety_snapshot["passed"] = False
        safety_snapshot["reason"] = safety_reason
        logger.error("审核模式 safety_checker 未初始化（DYN-604 fail-closed）")

    # 生成配图（不上传 B站，存 base64）
    image_refs: List[str] = []
    _img_provider = getattr(self, "image_provider", None)
    if (
        _img_provider
        and _img_provider.is_available()
        and dp_cfg.get("with_image", False)
    ):
        try:
            image_prompt = await self._generate_image_prompt(
                content,
                persona_id=persona_id,
            )
            if image_prompt:
                image_bytes = await _img_provider.generate(image_prompt)
                if image_bytes:
                    import base64 as _b64
                    image_refs = [_b64.b64encode(image_bytes).decode("ascii")]
                else:
                    # Task 13：配图生成返回 None，记录失败信息（不静默降级为纯文字）
                    safety_snapshot["image_generation_failed"] = True
                    safety_snapshot["image_failure_reason"] = (
                        "image_provider.generate returned None"
                    )
                    logger.warning("审核模式配图生成返回 None（with_image=True）")
            else:
                # Task 13：image_prompt 生成失败，记录失败信息
                safety_snapshot["image_generation_failed"] = True
                safety_snapshot["image_failure_reason"] = (
                    "image_prompt generation returned empty"
                )
                logger.warning("审核模式 image_prompt 生成返回空（with_image=True）")
        except Exception as e:
            # Task 13：配图生成异常，记录失败信息（不静默降级）
            safety_snapshot["image_generation_failed"] = True
            safety_snapshot["image_failure_reason"] = f"image_generation_exception: {e}"
            logger.warning(f"审核模式配图生成失败: {e}")

    # 计算过期时间
    draft_expiry = dp_cfg.get("draft_expiry_seconds", 86400)
    expires_at = time.time() + draft_expiry if draft_expiry else None

    draft_status = "awaiting_review" if safety_passed else "rejected"
    # task_id 可能为 None（非 TaskRun 调用路径），生成唯一占位 id
    draft_task_id = task_id or f"gen_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

    draft_id = store.create(
        account_id=self.account_id or "_default",
        persona_id=persona_id,
        task_id=draft_task_id,
        content=content,
        image_refs=image_refs,
        safety_snapshot=safety_snapshot,
        created_by="scheduler",
        status=draft_status,
        expires_at=expires_at,
    )

    await self._archive_bot_action(
        action_key=f"dynamic:{action_key}",
        action_type="dynamic_post",
        text=content,
        published=False,
        status="drafted" if safety_passed else "rejected",
        title="动态草稿",
        scene="dynamic_post",
        metadata={
            "draft_id": draft_id,
            "draft_status": draft_status,
            "task_id": task_id or draft_task_id,
            "reason_code": "" if safety_passed else "SAFETY_REJECTED",
        },
    )

    # 同步 audit 状态（OBS-501 语义化状态）
    if audit_id and self.audit_store:
        try:
            self.audit_store.set_status(audit_id, draft_status)
        except Exception:
            pass

    logger.info(f"动态草稿已创建，等待审核: {draft_id} status={draft_status}")
    if task_id:
        self._succeed_task(task_id, {
            "success": True,
            "summary": f"动态草稿已创建: {draft_id}",
            "draft_id": draft_id,
            "draft_status": draft_status,
            "review_mode": True,
        })
