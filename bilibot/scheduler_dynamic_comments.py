"""_collect_own_dynamic_comment_items, _extract_dynamic_comment_target, _reply_to_notification_items, _get_proactive_comment_max_attempts, _record_proactive_comment_audit, _finalize_proactive_comment_audit, _get_forbidden_phrases extracted from scheduler.py."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("bilibot.scheduler_dynamic_comments")


async def collect_own_dynamic_comment_items(self, config: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
    """补扫 Bot 自己动态下的评论，并转换成通知中心 item 形状。

    B站通知中心有时不会把“别人评论了我的动态”稳定返回到
    /x/msgfeed/reply，尤其是新动态接口发布的内容。这里只拉取最近几条
    自己动态的评论区，后续仍复用 _check_new_comments 的归档、过滤、
    生成、安全和发布流程。
    """
    if not self.bili or not hasattr(self.bili, "get_user_dynamics"):
        return []
    if not hasattr(self.bili, "get_replies"):
        return []

    reply_cfg = config.get("reply", {}) if isinstance(config, dict) else {}
    if reply_cfg.get("poll_own_dynamics", True) is False:
        return []

    bot_uid = self._bot_uid
    if not bot_uid:
        try:
            bot_uid = getattr(self.bili.config.bilibili, "dede_user_id", 0)
        except Exception:
            bot_uid = 0
    try:
        bot_uid_int = int(bot_uid or 0)
    except Exception:
        bot_uid_int = 0
    if bot_uid_int <= 0:
        return []

    dynamics_limit = int(reply_cfg.get("own_dynamic_poll_limit", 5) or 5)
    replies_ps = int(reply_cfg.get("own_dynamic_reply_ps", 20) or 20)
    dynamics_data = await self.bili.get_user_dynamics(bot_uid_int, limit=dynamics_limit)
    if not dynamics_data or dynamics_data.get("code") != 0:
        return []

    cards = (dynamics_data.get("data") or {}).get("items") or []
    if not cards and (dynamics_data.get("data") or {}).get("cards"):
        cards = (dynamics_data.get("data") or {}).get("cards") or []

    found: List[Dict[str, Any]] = []
    for card in cards[:dynamics_limit]:
        oid, comment_type = self._extract_dynamic_comment_target(card)
        if not oid:
            continue
        replies_data = await self.bili.get_replies(
            oid=int(oid),
            comment_type=int(comment_type or 17),
            pn=1,
            ps=replies_ps,
            sort=0,
        )
        if not replies_data or replies_data.get("code") != 0:
            continue
        replies = (replies_data.get("data") or {}).get("replies") or []
        target_context = self._dynamic_card_context(card)
        for reply in replies:
            found.extend(
                self._reply_to_notification_items(
                    reply,
                    oid,
                    int(comment_type or 17),
                    root_id=0,
                    target_context=target_context,
                )
            )
            if len(found) >= limit:
                return found[:limit]
    return found[:limit]

def extract_dynamic_comment_target(self, card: Dict[str, Any]) -> Tuple[int, int]:
    """Return (comment_oid, comment_type) for a dynamic feed card."""
    basic = card.get("basic") or {}
    oid = (
        basic.get("comment_id_str")
        or basic.get("comment_id")
        or basic.get("rid_str")
        or basic.get("rid")
    )
    comment_type = basic.get("comment_type") or 17
    if oid:
        try:
            return int(oid), int(comment_type or 17)
        except Exception:
            return 0, 17

    desc = card.get("desc") or {}
    oid = (
        desc.get("dynamic_id")
        or desc.get("dynamic_id_str")
        or desc.get("rid")
        or desc.get("rid_str")
    )
    comment_type = desc.get("type") or 17
    if oid:
        try:
            return int(oid), int(comment_type or 17)
        except Exception:
            return 0, 17
    return 0, 17

def reply_to_notification_items(
    self,
    reply: Dict[str, Any],
    oid: int,
    comment_type: int,
    *,
    root_id: int = 0,
    target_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Convert a B站 reply object and its floor replies to notification-like items."""
    items: List[Dict[str, Any]] = []

    def _one(reply_obj: Dict[str, Any], root: int) -> Optional[Dict[str, Any]]:
        rpid = reply_obj.get("rpid") or reply_obj.get("rpid_str") or reply_obj.get("id") or 0
        try:
            rpid_int = int(rpid)
        except Exception:
            rpid_int = 0
        if not rpid_int:
            return None
        content = (reply_obj.get("content") or {}).get("message") or ""
        if not content:
            return None
        member = reply_obj.get("member") or {}
        mid = str(member.get("mid") or "")
        uname = member.get("uname") or member.get("name") or "未知用户"
        if self._is_bot_speaker(mid, uname, content):
            return None
        return {
            "id": str(rpid_int),
            "user": {"mid": mid, "nickname": uname},
            "item": {
                "subject_id": int(oid),
                "business_id": int(comment_type),
                "root_id": int(root or 0),
                "source_id": int(rpid_int),
                "source_content": content,
                "target_context": dict(target_context or {}),
            },
            "source": "own_dynamic_poll",
        }

    top = _one(reply, root_id)
    if top:
        items.append(top)
    top_rpid = int((reply.get("rpid") or reply.get("rpid_str") or 0) or 0)
    for child in reply.get("replies") or []:
        child_item = _one(child, top_rpid)
        if child_item:
            items.append(child_item)
    return items

def get_proactive_comment_max_attempts(self) -> int:
    """从 config 读取主动评论 max_attempts"""
    from bilibot.services.proactive_comment_store import DEFAULT_MAX_ATTEMPTS
    try:
        prov = self.config_loader.get_raw_config().get("proactive", {})
        scenes_cfg = prov.get("scenes", {}) or {}
        scene_cfg = scenes_cfg.get("proactive_comment", {}) or {}
        return int(scene_cfg.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
    except Exception:
        return DEFAULT_MAX_ATTEMPTS

async def record_proactive_comment_audit(
    self,
    *,
    persona_id: str,
    comment_text: str,
    bvid: str,
    oid: Any,
    title: str = "",
    owner: str = "",
    input_summary: str = "",
    context_summary: str = "",
    prompt_preview: str = "",
    published: bool = False,
    status: str = "generated",
    failure_reason: str = "",
    extra_target: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """写入主动评论审计（评论页数据源）。失败仅记日志，不抛出。"""
    if self.audit_store is None:
        return None
    target: Dict[str, Any] = {
        "bvid": bvid,
        "oid": oid,
        "kind": "proactive_comment",
        "account_id": self.account_id or "",
    }
    if title:
        target["video_title"] = title
    if owner:
        target["owner"] = owner
    if failure_reason:
        target["failure_reason"] = failure_reason
    if extra_target:
        try:
            target.update(extra_target)
        except Exception:
            pass
    try:
        return await self.audit_store.record_async(
            scene="proactive_comment",
            persona_id=persona_id or "default",
            input_summary=input_summary or (
                f"主动评论 · 《{title}》 · UP {owner}" if title else f"主动评论 · bvid={bvid}"
            ),
            context_summary=context_summary or f"主动看视频后发表评论 bvid={bvid}",
            prompt_preview=prompt_preview or (
                f"视频: {title} | UP: {owner}" if title else f"bvid={bvid}"
            ),
            output=comment_text,
            published=published,
            status=status,
            target=target,
        )
    except Exception as e:
        logger.warning(f"主动评论审计记录失败 bvid={bvid}: {e}")
        return None

def finalize_proactive_comment_audit(
    self,
    audit_id: Optional[str],
    *,
    published: bool = False,
    failure_reason: str = "",
    status: Optional[str] = None,
    target: Optional[Dict[str, Any]] = None,
) -> None:
    """收口主动评论审计终态，避免评论页长期卡在 generated/pending。"""
    if not audit_id or self.audit_store is None:
        return
    try:
        self.audit_store.mark_published(
            audit_id,
            published=published,
            failure_reason=failure_reason or None,
            status=status,
            target=target,
        )
    except Exception as e:
        logger.warning(f"主动评论审计终态更新失败 audit_id={audit_id}: {e}")

def get_forbidden_phrases(self) -> Tuple[List[str], List["re.Pattern"]]:
    """读取禁用短语配置（Task 26）

    从 config 的 proactive 段读取：
    - forbidden_phrases：大小写不敏感子串匹配列表
    - forbidden_phrase_patterns：正则模式字符串列表（IGNORECASE 编译）

    未配置时回退到 _DEFAULT_FORBIDDEN_PHRASES / _DEFAULT_FORBIDDEN_PATTERNS。

    Returns:
        (phrases, compiled_patterns)
    """
    try:
        prov = self.config_loader.get_raw_config().get("proactive", {}) or {}
        phrases = prov.get("forbidden_phrases", self._DEFAULT_FORBIDDEN_PHRASES)
        pattern_strs = prov.get(
            "forbidden_phrase_patterns", self._DEFAULT_FORBIDDEN_PATTERNS
        )
    except Exception:
        phrases = self._DEFAULT_FORBIDDEN_PHRASES
        pattern_strs = self._DEFAULT_FORBIDDEN_PATTERNS
    try:
        patterns = [re.compile(p, re.IGNORECASE) for p in pattern_strs]
    except Exception as e:
        logger.warning(f"编译 forbidden_phrase_patterns 失败，回退默认: {e}")
        patterns = [re.compile(p, re.IGNORECASE) for p in self._DEFAULT_FORBIDDEN_PATTERNS]
    return phrases, patterns
