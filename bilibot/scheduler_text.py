"""Small pure text helpers extracted from scheduler.py.

Kept dependency-free so platform-text normalization can be tested without
constructing the large Scheduler.
"""

from __future__ import annotations

import html
from typing import Any, List


def clean_platform_text(value: Any, limit: int = 160) -> str:
    """Normalize Bilibili text for prompt/archive boundaries."""
    text = html.unescape(str(value or "")).replace("\x00", " ")
    text = " ".join(text.split())
    return text[: max(1, int(limit))]


def comment_memory_title(
    username: str,
    text: str,
    reply_id: str | int = "",
) -> str:
    """Short stable comment title for memory events."""
    name = str(username or "未知用户").strip() or "未知用户"
    snippet = " ".join(str(text or "").split())
    if len(snippet) > 36:
        snippet = snippet[:36] + "…"
    return f"{name} 评论：{snippet}" if snippet else f"{name} 的评论 {reply_id}"


def bounded_recent_turns(
    turns: List[str],
    *,
    max_turns: int = 6,
    max_chars: int = 1200,
) -> List[str]:
    """Keep the newest turns that fit inside a character budget."""
    selected: List[str] = []
    remaining = max(0, int(max_chars))
    for value in reversed(list(turns or [])[-max_turns:]):
        text = str(value or "")
        if not text or remaining <= 0:
            continue
        if len(text) > remaining:
            # Keep the newest tail of an oversized turn, matching the original
            # scheduler behavior.
            text = text[-remaining:]
        selected.append(text)
        remaining -= len(text)
    selected.reverse()
    return selected
