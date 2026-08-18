"""Pure scheduler helpers extracted from scheduler.py.

No imports from scheduler.py — these helpers are dependency-free so they can be
unit tested without constructing the 9k-line Scheduler.
"""

from __future__ import annotations

from typing import Any, Dict, List


def dynamic_card_context(card: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a small target context for comments under Bot's own dynamics."""
    basic = card.get("basic") or {}
    modules = card.get("modules") or {}
    dynamic = modules.get("module_dynamic") or {}
    desc = card.get("desc") or {}
    item = card.get("item") or {}

    text_candidates: List[str] = []
    major = dynamic.get("major") if isinstance(dynamic, dict) else {}
    opus = major.get("opus") if isinstance(major, dict) else {}
    opus_summary = opus.get("summary") if isinstance(opus, dict) else {}
    dynamic_desc = dynamic.get("desc") if isinstance(dynamic, dict) else {}
    for value in (
        dynamic_desc.get("text") if isinstance(dynamic_desc, dict) else "",
        opus_summary.get("text") if isinstance(opus_summary, dict) else "",
        item.get("description"),
        item.get("content"),
        item.get("title"),
        card.get("title"),
    ):
        if isinstance(value, str) and value.strip():
            text_candidates.append(value.strip())

    return {
        "kind": "own_dynamic",
        "dynamic_id": str(
            card.get("id_str")
            or card.get("id")
            or desc.get("dynamic_id_str")
            or desc.get("dynamic_id")
            or ""
        ),
        "comment_oid": str(
            basic.get("comment_id_str")
            or basic.get("comment_id")
            or basic.get("rid_str")
            or basic.get("rid")
            or ""
        ),
        "dynamic_text": (text_candidates[0] if text_candidates else "")[:500],
    }
