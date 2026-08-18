"""Scheduler-facing video download guards.

Kept separate from scheduler.py so the video-understanding resource contract
(PRD-V5 §8.2) can be tested without constructing the 9k-line Scheduler.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def video_download_precheck(video_understanding: Any, video_info: Dict[str, Any]) -> str:
    """Refuse oversized / non-video targets BEFORE any bytes are pulled.

    Returns "" (ok) or a degradation reason string.
    """
    if not isinstance(video_info, dict):
        return "invalid_video_info"
    cid = video_info.get("cid") or 0
    if not cid:
        pages = video_info.get("pages") or []
        if isinstance(pages, list) and pages:
            first = pages[0] if isinstance(pages[0], dict) else {}
            cid = first.get("cid", 0)
    if not cid:
        # 专栏 / 音频 / 动态等 feed 内容可能混入：明确降级而不是
        # 把"缺 CID"伪装成下载失败。
        return "not_a_video"
    try:
        cfg = getattr(video_understanding, "cfg", None)
        max_duration = int(getattr(cfg, "max_duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if max_duration <= 0:
        return ""
    try:
        duration = float(video_info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        return ""
    if duration > max_duration:
        return "duration_exceeds_limit"
    return ""


def video_download_bounds(video_understanding: Any) -> Tuple[int, int]:
    """(max_bytes, timeout_seconds) for production video downloads."""
    try:
        cfg = getattr(video_understanding, "cfg", None)
        max_bytes = max(0, int(getattr(cfg, "max_download_bytes", 0) or 0))
        timeout = max(30, int(getattr(cfg, "download_timeout_seconds", 0) or 90))
        return max_bytes, timeout
    except (TypeError, ValueError):
        return 0, 600
