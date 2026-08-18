"""Validated Bilibili video identifiers and reply-link safeguards."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$", re.IGNORECASE)
_TRUSTED_BVID_URL_RE = re.compile(
    r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})(?:[/?#\s]|$)",
    re.IGNORECASE,
)
_AID_RE = re.compile(r"^[0-9]+$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_VIDEO_SOURCES = frozenset({"video", "video_experience", "video_metadata", "bot_action"})
_LINK_REQUEST_MARKERS = (
    "\u94fe\u63a5",
    "\u7f51\u5740",
    "url",
    "link",
    "bvid",
    "bv\u53f7",
)
_TRAILING_URL_PUNCTUATION = ".,!?;:)]}>\u3002\uff0c\uff01\uff1f\uff1b\uff1a\u3009\u300b\u300d\u300f"


def normalize_bvid(value: Any) -> str:
    """Return a BVID only when the value is an exact valid identifier."""

    text = str(value or "").strip()
    if not _BVID_RE.fullmatch(text):
        return ""
    return "BV" + text[2:]


def canonical_video_url(*, bvid: Any = "", aid: Any = "") -> str:
    """Build one canonical public video URL from a validated BVID or numeric aid."""

    normalized = normalize_bvid(bvid)
    if normalized:
        return f"https://www.bilibili.com/video/{normalized}"
    numeric_aid = str(aid or "").strip()
    if _AID_RE.fullmatch(numeric_aid):
        return f"https://www.bilibili.com/video/av{numeric_aid}"
    return ""


def _parse_structured(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _metadata_rows(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("metadata", "metadata_json", "meta", "data", "data_json"):
        value = _parse_structured(event.get(key))
        if isinstance(value, Mapping):
            rows.append(value)
    sources = event.get("sources") or event.get("source") or ()
    if isinstance(sources, Mapping):
        sources = [sources]
    if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            for key in ("metadata", "metadata_json", "meta", "data", "data_json"):
                value = _parse_structured(source.get(key))
                if isinstance(value, Mapping):
                    rows.append(value)
    return rows


def video_url_from_event(event: Mapping[str, Any]) -> str:
    """Extract only an explicitly stored, validated URL for a video event.

    Arbitrary source metadata is never copied into prompts. Only exact BVIDs are
    accepted, and numeric aids are accepted for the three video archive sources.
    """

    if not isinstance(event, Mapping):
        return ""
    source_type = str(event.get("source_type") or "").strip().casefold()
    if not source_type:
        sources = event.get("sources") or event.get("source") or ()
        if isinstance(sources, Mapping):
            sources = [sources]
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
            for source in sources:
                if isinstance(source, Mapping):
                    source_type = str(
                        source.get("source_type") or source.get("type") or ""
                    ).strip().casefold()
                    if source_type:
                        break
    if source_type not in _VIDEO_SOURCES:
        return ""
    for metadata in _metadata_rows(event):
        link = canonical_video_url(
            bvid=metadata.get("bvid") or metadata.get("video_bvid"),
            aid=metadata.get("aid") or metadata.get("video_aid") or metadata.get("oid"),
        )
        if link:
            return link
    return ""


def first_video_bvid(value: Any) -> str:
    """Return the first BVID from an explicitly rendered canonical video URL."""

    match = _TRUSTED_BVID_URL_RE.search(str(value or ""))
    return normalize_bvid(match.group(1)) if match else ""


def is_video_link_request(value: Any) -> bool:
    """Recognize a request for a video URL without relying on model intent."""

    text = str(value or "").casefold()
    return any(marker in text for marker in _LINK_REQUEST_MARKERS)


def _is_bilibili_video_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.rstrip(_TRAILING_URL_PUNCTUATION))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if host in {"b23.tv", "www.b23.tv"}:
        return True
    return host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"} and "/video/" in (parsed.path or "").casefold()


def ensure_video_link(
    text: Any,
    *,
    query: Any = "",
    bvid: Any = "",
    aid: Any = "",
    max_chars: int = 0,
) -> str:
    """Force a requested video link to the validated canonical target.

    This changes text only when the user asked for a link and the caller has a
    trusted BVID/aid. Existing Bilibili video URLs and short links are replaced;
    unrelated URLs are preserved. When a length limit is supplied, the link is
    kept intact and the prose before it is shortened.
    """

    result = str(text or "").strip()
    if not result or not is_video_link_request(query):
        return result
    link = canonical_video_url(bvid=bvid, aid=aid)
    if not link:
        return result
    found_video_url = False

    def replace(match: re.Match[str]) -> str:
        nonlocal found_video_url
        raw = match.group(0)
        trailing = ""
        core = raw
        while core and core[-1] in _TRAILING_URL_PUNCTUATION:
            trailing = core[-1] + trailing
            core = core[:-1]
        if not _is_bilibili_video_url(core):
            return raw
        found_video_url = True
        return link + trailing

    result = _URL_RE.sub(replace, result)
    if not found_video_url:
        result = f"{result}\n\u89c6\u9891\u94fe\u63a5：{link}"
    result = result.strip()
    if max_chars and len(result) > max_chars:
        link_pos = result.find(link)
        if link_pos >= 0:
            prefix = result[:link_pos].rstrip()
            suffix = result[link_pos + len(link):].strip()
            suffix = suffix if suffix and suffix not in _TRAILING_URL_PUNCTUATION else ""
            tail = f"{link}{suffix}"
            separator = "\n" if prefix else ""
            budget = max_chars - len(separator) - len(tail)
            prefix = prefix[: max(0, budget)].rstrip()
            result = f"{prefix}{separator}{tail}".strip()
        else:
            result = result[:max_chars].rstrip()
    return result


__all__ = [
    "canonical_video_url",
    "ensure_video_link",
    "first_video_bvid",
    "is_video_link_request",
    "normalize_bvid",
    "video_url_from_event",
]
