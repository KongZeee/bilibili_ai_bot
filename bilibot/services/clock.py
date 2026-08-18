"""Bilibili 产品时区时钟。

所有「人类视角」的调度边界（日报/日记/动态槽位/周总结/搜索预算日切）
统一使用 Asia/Shanghai；持久化时间戳仍按项目约定为 UTC epoch/ISO。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Shanghai"
_FALLBACK_TIMEZONE = timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)


def _timezone():
    try:
        return ZoneInfo(DEFAULT_TIMEZONE)
    except Exception:
        # Windows test environments may not ship the IANA tzdata package.
        # Keep product day boundaries in China Standard Time instead of
        # falling through to a second ZoneInfo lookup that also fails.
        return _FALLBACK_TIMEZONE


def now_cn() -> datetime:
    return datetime.now(_timezone())


def today_cn():
    return now_cn().date()
