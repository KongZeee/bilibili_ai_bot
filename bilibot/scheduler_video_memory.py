"""_build_video_detail_digest, _archive_required, _event_is_full_video_watch, _has_full_video_observation, _has_completed_proactive_video, _load_existing_video_detail, _compose_video_content_for_prompt, _pause_for_memory_failure, _recall_for_proactive_video, _recent_failed_video_event_ids, _archive_video_web_reference extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from bilibot.models.proactive_video_context import ProactiveVideoContext

logger = logging.getLogger("bilibot.scheduler_video_memory")


async def build_video_detail_digest(
    self,
    *,
    title: str,
    owner: str,
    behavior_log: str,
    extra_context: str = "",
    max_attempts: int = 2,
    require_llm: bool = True,
) -> str:
    """Compress audiovisual log into ≤2000 chars for later recall/comment.

    Retries LLM summarization on failure/empty/raw-log dumps. When
    ``require_llm`` is True (default for proactive watch), heuristic
    truncation is NOT accepted as success so the caller can skip the video.
    """
    from bilibot.memory_brain.gateway import MemoryModelGateway

    log = str(behavior_log or "").strip()
    if not log:
        return ""

    gateway = None
    brain = getattr(self, "memory_brain", None)
    if brain is not None and getattr(brain, "gateway", None) is not None:
        gateway = brain.gateway
    elif getattr(self, "llm", None) is not None:
        gateway = MemoryModelGateway(chat_provider=self.llm, embedding_provider=None)
    else:
        gateway = MemoryModelGateway()

    attempts = max(1, int(max_attempts))
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            detail = await gateway.summarize_video_detail(
                title=title,
                owner=owner,
                behavior_log=log,
                extra_context=extra_context,
                max_chars=2000,
                allow_heuristic=not require_llm,
            )
        except Exception as exc:
            last_err = type(exc).__name__
            logger.warning(
                "视频详细内容摘要失败 attempt=%s/%s: %s",
                attempt,
                attempts,
                last_err,
            )
            detail = ""
        detail = str(detail or "").strip()
        if detail and not MemoryModelGateway.looks_like_heuristic_video_detail(detail):
            logger.info(
                "视频详细内容摘要完成: %s 字 (attempt=%s/%s)",
                len(detail),
                attempt,
                attempts,
            )
            return detail[:2000]
        last_err = last_err or "empty_or_heuristic"
        if attempt < attempts:
            # brief backoff before retrying the same video
            await asyncio.sleep(min(2.0 * attempt, 4.0))

    if require_llm:
        logger.warning(
            "视频详细内容摘要在 %s 次尝试后仍失败（%s），将换视频",
            attempts,
            last_err or "unknown",
        )
        return ""

    # Non-strict path: accept heuristic as last resort.
    detail = MemoryModelGateway.heuristic_video_detail(
        title=title, owner=owner, behavior_log=log, max_chars=2000
    )
    return str(detail or "").strip()[:2000]

async def archive_required(self, envelope, *, treat_idempotent_as_ready: bool = False):
    """Archive a raw observation before any irreversible business action.

    Store semantics:
    - same idempotency_key + same content_hash → soft success (no exception)
    - same key + different hash → IdempotencyConflictError

    ``treat_idempotent_as_ready`` only absorbs conflict when the existing
    event is already a full video watch for the same bvid (digest non-
    determinism on retry). It must NOT silently accept a different video
    bound to a reused key.
    """
    brain = getattr(self, "memory_brain", None)
    if brain is None and not getattr(self, "_memory_brain_required", False):
        # Compatibility for direct legacy/minimal Scheduler construction.
        # Production AccountInstance always injects the V6 service.
        return None
    if brain is None:
        raise RuntimeError("V6 memory brain is not initialized")
    try:
        result = await brain.archive_observation_async(envelope)
        if result is None:
            raise RuntimeError("V6 archive returned no commit result")
        if getattr(result, "source_committed", True) is False:
            raise RuntimeError("V6 archive source commit was not confirmed")
        return result
    except Exception as exc:
        # IdempotencyConflictError / ReingestBlockedError 是良性条件
        # （数据已存在或已被删除 tombstone），不是存储故障，不应暂停账号。
        from bilibot.memory_brain.models import (
            IdempotencyConflictError,
            ReingestBlockedError,
        )
        if isinstance(exc, IdempotencyConflictError) and treat_idempotent_as_ready:
            # 仅当库里已是「同 bvid 的完整观看或已完成闭环」时才视为就绪。
            # content_hash 不同通常来自 digest / experience 非确定性；绝不能在
            # key 复用导致「新片撞旧片」时继续评价。
            meta = getattr(envelope, "metadata", None) or {}
            if not isinstance(meta, dict):
                meta = {}
            bvid = str(meta.get("bvid") or "").strip()
            ready = False
            if bvid:
                if await self._has_full_video_observation(bvid):
                    ready = True
                elif await self._has_completed_proactive_video(bvid):
                    ready = True
            if ready:
                logger.info(
                    "memory archive conflict treated as ready: "
                    "account=%s key=%s bvid=%s",
                    self.account_id,
                    getattr(envelope, "idempotency_key", ""),
                    bvid,
                )
                return {
                    "source_committed": True,
                    "idempotent_hit": True,
                    "content_mismatch": True,
                }
            logger.warning(
                "memory archive idempotency conflict (not ready): "
                "account=%s key=%s bvid=%s",
                self.account_id,
                getattr(envelope, "idempotency_key", ""),
                bvid,
            )
            raise
        if isinstance(exc, (IdempotencyConflictError, ReingestBlockedError)):
            logger.warning(
                "memory archive skipped (idempotency conflict or blocked): "
                "account=%s key=%s",
                self.account_id,
                getattr(envelope, "idempotency_key", ""),
            )
            raise
        self._pause_for_memory_failure()
        logger.error(
            "required memory archive failed; account actions paused: account=%s",
            self.account_id,
            exc_info=True,
        )
        raise

def event_is_full_video_watch(self, event) -> bool:
    """True only for a real audiovisual watch/digest — not metadata/like/etc."""
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("event_type") or "")
    source_type = str(event.get("source_type") or "")
    if event_type != "video_observation" and source_type != "video":
        return False
    meta = event.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("has_video_detail"):
        return True
    for source in event.get("sources") or []:
        if not isinstance(source, dict):
            continue
        if source.get("source_type") not in self._FULL_VIDEO_SOURCE_TYPES:
            continue
        if str(source.get("full_text") or source.get("text") or "").strip():
            return True
    return False

async def has_full_video_observation(self, bvid: str) -> bool:
    """True if account brain already has a full audiovisual watch for bvid."""
    brain = getattr(self, "memory_brain", None)
    bvid_value = str(bvid or "").strip()
    if not brain or not bvid_value:
        return False
    try:
        hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid_value], 20)
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            hit_meta = hit.get("metadata") or {}
            if not isinstance(hit_meta, dict):
                hit_meta = {}
            # Prefer exact bvid match on metadata; fall through to load full event.
            meta_bvid = str(hit_meta.get("bvid") or "").strip()
            if meta_bvid and meta_bvid != bvid_value:
                continue
            # Lightweight path: metadata already flags a digest-backed watch.
            if meta_bvid == bvid_value and hit_meta.get("has_video_detail"):
                if str(hit.get("event_type") or "") == "video_observation" or str(
                    hit.get("source_type") or ""
                ) == "video":
                    return True
            event_id = str(hit.get("event_id") or hit.get("id") or "")
            if not event_id:
                continue
            event = await asyncio.to_thread(brain.get_event, event_id, None)
            if not event:
                continue
            event_meta = event.get("metadata") or {}
            if not isinstance(event_meta, dict):
                event_meta = {}
            if str(event_meta.get("bvid") or "").strip() not in {"", bvid_value}:
                continue
            if str(event_meta.get("bvid") or "").strip() != bvid_value:
                # Accept via source external_id when metadata.bvid missing.
                if not any(
                    isinstance(s, dict)
                    and str(s.get("external_id") or "").strip() == bvid_value
                    for s in (event.get("sources") or [])
                ):
                    continue
            if self._event_is_full_video_watch(event):
                return True
        return False
    except Exception as exc:
        logger.debug(
            "full video observation check failed bvid=%s: %s",
            bvid_value,
            type(exc).__name__,
        )
        return False

async def has_completed_proactive_video(self, bvid: str) -> bool:
    """Skip candidate only after the evaluate/interact cycle archived experience.

    Full video_observation alone is NOT enough: archive happens before evaluate.
    If evaluate/interact fails after archive, retry must be allowed to finish
    the cycle (idempotent archive + policy dedupe protect side effects).
    """
    brain = getattr(self, "memory_brain", None)
    bvid_value = str(bvid or "").strip()
    if not brain or not bvid_value:
        return False
    try:
        hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid_value], 30)
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            event_type = str(hit.get("event_type") or "")
            source_type = str(hit.get("source_type") or "")
            hit_meta = hit.get("metadata") or {}
            if not isinstance(hit_meta, dict):
                hit_meta = {}
            meta_bvid = str(hit_meta.get("bvid") or "").strip()
            if meta_bvid and meta_bvid != bvid_value:
                continue
            if event_type == "bot_experience" or source_type in {
                "video_experience",
                "bot_experience",
            }:
                if meta_bvid == bvid_value:
                    return True
                # metadata may be unparsed on list rows; load full event
                event_id = str(hit.get("event_id") or hit.get("id") or "")
                if not event_id:
                    continue
                event = await asyncio.to_thread(brain.get_event, event_id, None)
                if not event:
                    continue
                em = event.get("metadata") or {}
                if isinstance(em, dict) and str(em.get("bvid") or "").strip() == bvid_value:
                    if str(event.get("event_type") or "") == "bot_experience" or str(
                        event.get("source_type") or ""
                    ) in {"video_experience", "bot_experience"}:
                        return True
        return False
    except Exception as exc:
        logger.debug(
            "completed proactive video check failed bvid=%s: %s",
            bvid_value,
            type(exc).__name__,
        )
        return False

async def load_existing_video_detail(self, bvid: str) -> str:
    """Load archived video_detail / summary for a bvid if a full watch exists."""
    brain = getattr(self, "memory_brain", None)
    bvid_value = str(bvid or "").strip()
    if not brain or not bvid_value:
        return ""
    try:
        hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid_value], 20)
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            hit_meta = hit.get("metadata") or {}
            if not isinstance(hit_meta, dict):
                hit_meta = {}
            meta_bvid = str(hit_meta.get("bvid") or "").strip()
            if meta_bvid and meta_bvid != bvid_value:
                continue
            event_id = str(hit.get("event_id") or hit.get("id") or "")
            if not event_id:
                continue
            event = await asyncio.to_thread(brain.get_event, event_id, None)
            if not event or not self._event_is_full_video_watch(event):
                continue
            event_meta = event.get("metadata") or {}
            if not isinstance(event_meta, dict):
                event_meta = {}
            if str(event_meta.get("bvid") or "").strip() not in {"", bvid_value}:
                continue
            if str(event_meta.get("bvid") or "").strip() != bvid_value:
                if not any(
                    isinstance(s, dict)
                    and str(s.get("external_id") or "").strip() == bvid_value
                    for s in (event.get("sources") or [])
                ):
                    continue
            # Prefer dedicated video_detail source text.
            for source in event.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                if source.get("source_type") == "video_detail":
                    text = str(source.get("full_text") or "").strip()
                    if text:
                        return text[:2000]
            summary = str(event.get("summary") or "").strip()
            if summary and len(summary) >= 40:
                return summary[:2000]
        return ""
    except Exception as exc:
        logger.debug(
            "load existing video_detail failed bvid=%s: %s",
            bvid_value,
            type(exc).__name__,
        )
        return ""

def compose_video_content_for_prompt(
    self,
    ctx: "ProactiveVideoContext",
    *,
    video_detail: str = "",
) -> str:
    """Build evaluate/comment input: digest first, keep untrusted search ref.

    If ``ctx`` already carries memory/companion (set before compose), append
    them so digest path does not drop cross-scene evidence. Callers may still
    pass memory_evidence kwargs to evaluate/comment for dual coverage.
    """
    parts: list[str] = []
    detail = str(video_detail or "").strip()
    if detail:
        parts.append(detail)
        search_block = ctx.format_search_reference()
        if search_block:
            parts.append(search_block)
    else:
        # to_prompt_sections already includes search + memory/companion if set.
        return ctx.to_prompt_sections(
            include_metadata=False, include_hot_comments=False,
        )
    mem = str(getattr(ctx, "memory_evidence", "") or "").strip()
    if mem:
        parts.append("【相关记忆/近期经历】\n" + mem[:1800])
    life = str(getattr(ctx, "companion_context", "") or "").strip()
    if life:
        parts.append("【你今天的状态与念头】\n" + life[:500])
    return "\n\n".join(parts)

def pause_for_memory_failure(self) -> None:
    """Pause account when irreversible observation would be lost.

    Recovery: fix storage/brain health, then
    ``safety_checker.resume_account(account_id)``. Status exposes
    ``memory_archive=true`` and ``resume_hint``.
    """
    safety = getattr(self, "safety_checker", None)
    account_id = str(getattr(self, "account_id", "") or "")
    if safety is None or not account_id:
        logger.error(
            "memory archive failure without safety_checker/account_id; "
            "cannot pause account=%s",
            account_id or "-",
        )
        return
    reason = "memory_archive_failed"
    try:
        pause = getattr(safety, "pause_account", None)
        if callable(pause):
            pause(account_id, reason=reason)
        logger.error(
            "account paused for memory integrity: account=%s reason=%s "
            "(resume after brain is writable via resume_account)",
            account_id,
            reason,
        )
    except Exception as exc:
        logger.error(
            "failed to pause account after memory archive failure: account=%s err=%s",
            account_id,
            type(exc).__name__,
        )

async def recall_for_proactive_video(
    self,
    *,
    title: str = "",
    owner: str = "",
    tags: Optional[List[str]] = None,
    bvid: str = "",
    oid: str = "",
    desc: str = "",
) -> Dict[str, Any]:
    """Account-scoped hybrid recall for evaluate / proactive-comment.

    Pulls related video/bangumi/diary/comment experiences so the model can
    ground comments beyond the current clip + companion surface alone.
    Failures degrade to empty evidence (do not block watching/archiving).
    """
    empty = {"memory_evidence": "", "memory_event_ids": [], "event_count": 0}
    brain = getattr(self, "memory_brain", None)
    if brain is None or not callable(getattr(brain, "recall", None)):
        return empty
    tags_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
    query_parts = [
        "最近看的视频、番剧、日记、评论、心情",
        str(title or "").strip(),
        f"UP主 {owner}" if owner else "",
        " ".join(tags_list[:6]),
        str(desc or "")[:120],
    ]
    query_text = " ".join(p for p in query_parts if p).strip()
    if not query_text:
        query_text = "最近观看与生活经历"
    try:
        from bilibot.memory_brain import RecallQuery

        result = await brain.recall(
            RecallQuery(
                current_message=query_text,
                account_id=self.account_id or "",
                title=str(title or "").strip(),
                bvid=str(bvid or "").strip(),
                oid=str(oid or "").strip(),
                scene="proactive_video",
            )
        )
    except Exception as exc:
        logger.debug(
            "proactive video memory recall failed: %s", type(exc).__name__
        )
        return empty
    evidence = ""
    event_ids: List[str] = []
    events = ()
    if result is not None:
        evidence = str(getattr(result, "prompt_evidence", "") or "")
        events = getattr(result, "events", ()) or ()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id") or ev.get("event_id") or "").strip()
            if eid and eid not in event_ids:
                event_ids.append(eid)
    if evidence:
        logger.info(
            "主动视频混合召回: events=%s evidence_chars=%s bvid=%s",
            len(events),
            len(evidence),
            bvid or "-",
        )
    return {
        "memory_evidence": evidence,
        "memory_event_ids": event_ids[:20],
        "event_count": len(events),
    }

async def recent_failed_video_event_ids(self, bvid: str, limit: int = 12) -> List[str]:
    brain = getattr(self, "memory_brain", None)
    if not brain or not bvid:
        return []
    try:
        hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid], 40)
    except Exception:
        return []
    result: List[str] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        meta = hit.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        if str(meta.get("bvid") or "") != str(bvid):
            continue
        if str(meta.get("action_state") or "").casefold() != "failed":
            continue
        event_id = str(hit.get("event_id") or hit.get("id") or "")
        if event_id and event_id not in result:
            result.append(event_id)
        if len(result) >= max(1, int(limit)):
            break
    return result

async def archive_video_web_reference(
    self,
    *,
    bvid: str,
    oid: str,
    title: str,
    query: str,
    result: Any,
) -> str:
    """Archive a compact search observation independently of watch success."""
    from bilibot.memory_brain.ingestion import text_observation

    compact: Dict[str, Any] = {"query": self._clean_platform_text(query, 240)}
    if isinstance(result, dict):
        for key in ("answer", "content"):
            if result.get(key):
                compact[key] = self._clean_platform_text(result.get(key), 1500)
        rows = result.get("items") or result.get("results") or []
        compact["items"] = [
            {
                "title": self._clean_platform_text(row.get("title"), 160),
                "snippet": self._clean_platform_text(
                    row.get("snippet") or row.get("content"), 500
                ),
                "url": self._clean_platform_text(row.get("url"), 500),
            }
            for row in rows[:6]
            if isinstance(row, dict)
        ]
    else:
        compact["content"] = self._clean_platform_text(result, 2500)
    body = json.dumps(compact, ensure_ascii=False, sort_keys=True, indent=2)
    digest = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()[:16]
    archived = await self._archive_required(
        text_observation(
            account_id=self.account_id or "default",
            idempotency_key=f"video_web:{bvid or oid}:{digest}",
            source_type="web_reference",
            event_type="web_observation",
            text=body,
            title=f"视频搜索参考：{self._clean_platform_text(title, 60)}",
            persona_id=self._get_current_persona_id(),
            scene="proactive_video",
            metadata={
                "bvid": str(bvid or ""),
                "oid": str(oid or ""),
                "query": compact["query"],
                "canonical_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "canonical_chars": len(body),
            },
            importance=0.3,
        )
    )
    return str(getattr(archived, "event_id", "") or "")
