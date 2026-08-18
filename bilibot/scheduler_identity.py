"""Bot-identity and PM-redaction helpers extracted from scheduler.py."""

from __future__ import annotations

import hashlib
from typing import Any, Set


def build_bot_aliases(bot_name: str, persona_id: str = "") -> Set[str]:
    """Case-folded names that identify the bot itself in comments/threads."""
    aliases = {"bot", "亚托莉", "亚托莉小姐", "atri", "ATRI"}
    name = str(bot_name or "").strip()
    if name:
        aliases.add(name)
    pid = str(persona_id or "").strip()
    if pid:
        aliases.add(pid)
    return {alias.casefold() for alias in aliases if alias}


def is_bot_speaker(
    *,
    bot_uid: str,
    bot_name: str = "",
    persona_id: str = "",
    actor_id: str | int = "",
    username: str = "",
    text: str = "",
) -> bool:
    uid = str(bot_uid or "")
    actor = str(actor_id or "")
    if uid and actor and actor == uid:
        return True
    aliases = build_bot_aliases(bot_name, persona_id)
    uname = str(username or "").strip().casefold()
    if uname and uname in aliases:
        return True
    lowered = str(text or "").casefold()
    return any(f"@{alias}" in lowered for alias in aliases if alias not in {"bot", "atri"})


def bot_already_replied_to_source(
    *,
    bot_uid: str,
    replies: list,
    source_rpid: str,
    expected_text: str = "",
) -> bool:
    """楼中楼幂等：仅当 bot 已回复该 source_rpid（parent 匹配）才算已回。

    匹配规则（任一命中即 True）：
    1. mid==bot 且 parent/parent_str == source_rpid
    2. mid==bot 且 content.message 与 expected_text 文本一致（生成文本对账）
    """
    uid = str(bot_uid or "")
    if not uid or not replies:
        return False
    source = str(source_rpid or "")
    expected = (expected_text or "").strip()
    for r in replies or []:
        if not isinstance(r, dict):
            continue
        member = r.get("member") or {}
        mid = str(member.get("mid") or r.get("mid") or "")
        if mid != uid:
            continue
        parent = str(
            r.get("parent")
            or r.get("parent_str")
            or (r.get("reply_control") or {}).get("parent")
            or ""
        )
        if source and parent and parent == source:
            return True
        if expected:
            content = r.get("content") or {}
            msg = str(
                content.get("message") if isinstance(content, dict) else content or ""
            ).strip()
            if msg and msg == expected:
                return True
    return False


def redact_private_message_runtime(
    brain: Any,
    text: str,
    *,
    actor_id: str | int,
    username: str = "",
    account_id: str = "",
):
    """PM-safe value even on the legacy direct-construction path."""
    if brain is not None:
        return brain.redact_private_message(
            text, actor_id=actor_id, username=username
        )
    from bilibot.memory_brain.redaction import redact_private_message

    salt = hashlib.sha256(
        f"bilibot:legacy-pm:{account_id}".encode("utf-8")
    ).digest()
    return redact_private_message(
        text,
        actor_id=actor_id,
        account_salt=salt,
        current_username=username,
    )
