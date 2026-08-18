"""_collect_related_titles_for_dynamic, _notify_companion_dynamic_posted, _notify_companion_private_message_replied, _notify_companion_comment_replied, _begin_activity_context, _archive_bot_action, _clean_platform_text, _archive_proactive_video_failure, _bot_aliases, _is_bot_speaker, _bot_already_replied_to_source, _comment_memory_title, _dynamic_card_context, _redact_private_message_runtime, _bounded_recent_turns, _archive_comment_thread_context, _private_message_text, _archive_pm_recent_history extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional


logger = logging.getLogger("bilibot.scheduler_archiving")


def collect_related_titles_for_dynamic(self, brain, *, limit: int = 5) -> List[str]:
    """Titles from recent watch/experience/bangumi events for dynamic grounding.

    ``list_events`` only filters one source_type at a time; merge several
    relevant types and de-dupe by title.
    """
    if brain is None or not hasattr(brain, "list_events"):
        return []
    source_types = (
        "video_experience",
        "video",
        "bangumi",
        "diary",
        "dream",
        "bot_action",
    )
    per = max(5, int(limit) * 2)
    seen: set[str] = set()
    titles: List[str] = []

    def _take_from(events) -> None:
        for event in events or []:
            if not isinstance(event, dict):
                continue
            title = str(event.get("title") or event.get("event_title") or "").strip()
            if not title or title in seen:
                continue
            # Skip pure system action labels without content signal
            st = str(event.get("source_type") or "")
            if st == "bot_action" and not title.startswith("《"):
                # Prefer event summary snippet for dynamic_post bot_actions
                summary = str(
                    event.get("summary") or event.get("event_summary") or ""
                ).strip()
                if summary and summary not in seen:
                    label = summary[:40]
                    seen.add(summary)
                    titles.append(label)
                continue
            seen.add(title)
            titles.append(f"《{title}》" if not title.startswith("《") else title)
            if len(titles) >= limit:
                return

    try:
        for st in source_types:
            if len(titles) >= limit:
                break
            try:
                events = brain.list_events(limit=per, source_type=st)
            except TypeError:
                events = brain.list_events(limit=per)
            except Exception:
                continue
            _take_from(events)
        if len(titles) < limit:
            try:
                _take_from(brain.list_events(limit=per))
            except Exception:
                pass
    except Exception:
        return titles[:limit]
    return titles[:limit]

def notify_companion_dynamic_posted(
    self,
    *,
    content: str = "",
    topic: str = "",
    draft_id: str = "",
    dynamic_id: str = "",
    task_id: str = "",
) -> None:
    companion = getattr(self, "companion", None)
    if companion is None or not getattr(companion, "enabled", False):
        return
    try:
        if not hasattr(companion, "on_dynamic_posted"):
            return
        companion.on_dynamic_posted(
            content=content or "",
            topic=topic or "",
            draft_id=draft_id or "",
            dynamic_id=dynamic_id or "",
            task_id=task_id or "",
        )
    except TypeError:
        # Older signature without correlation kwargs
        try:
            companion.on_dynamic_posted(content=content or "", topic=topic or "")
        except Exception as e:
            logger.debug("companion dynamic feedback failed: %s", e)
    except Exception as e:
        logger.debug("companion dynamic feedback failed: %s", e)

def notify_companion_private_message_replied(
    self,
    *,
    actor_label: str = "",
) -> None:
    """Push continuous-self feedback after a real PM send (no body text)."""
    companion = getattr(self, "companion", None)
    if companion is None or not getattr(companion, "enabled", False):
        return
    on_pm = getattr(companion, "on_private_message_replied", None)
    if not callable(on_pm):
        return
    try:
        on_pm(preview="", actor_label=str(actor_label or "")[:24])
    except Exception:
        logger.debug("companion PM feedback failed", exc_info=True)

def notify_companion_comment_replied(
    self,
    *,
    title: str = "",
    preview: str = "",
    proactive: bool = False,
) -> None:
    companion = getattr(self, "companion", None)
    if companion is None or not getattr(companion, "enabled", False):
        return
    on_cmt = getattr(companion, "on_comment_replied", None)
    if not callable(on_cmt):
        return
    try:
        on_cmt(
            title=str(title or "")[:40],
            preview=str(preview or "")[:80],
            proactive=bool(proactive),
        )
    except Exception:
        logger.debug("companion comment feedback failed", exc_info=True)

async def begin_activity_context(
    self,
    *,
    action_key: str,
    action_type: str,
    current_activity: str,
    query: str = "",
    scene: str = "system",
    title: str = "",
    bvid: str = "",
    oid: str = "",
    metadata: Optional[Dict[str, Any]] = None,
):
    """Create durable current intent and return its cross-scene memory."""
    brain = getattr(self, "memory_brain", None)
    if brain is None:
        if getattr(self, "_memory_brain_required", False):
            self._pause_for_memory_failure()
            raise RuntimeError("V6 memory brain is not initialized")
        return None
    begin = getattr(brain, "begin_activity", None)
    if not callable(begin) or not callable(
        getattr(type(brain), "begin_activity", None)
    ):
        # Compatibility for isolated legacy/unit constructions. Production
        # always receives MemoryBrainService, which implements this method.
        if getattr(self, "_memory_brain_required", False):
            self._pause_for_memory_failure()
            raise RuntimeError("V6 activity memory is not available")
        return None
    try:
        context = await begin(
            action_key=action_key,
            action_type=action_type,
            current_activity=current_activity,
            query=query,
            scene=scene,
            title=title,
            bvid=bvid,
            oid=oid,
            persona_id=self._get_current_persona_id(),
            metadata=metadata or {},
        )
    except Exception:
        self._pause_for_memory_failure()
        logger.error(
            "activity memory initialization failed: account=%s action=%s",
            self.account_id,
            action_key,
            exc_info=True,
        )
        raise
    if not str(getattr(context, "prompt_text", "") or "").strip():
        self._pause_for_memory_failure()
        raise RuntimeError("V6 activity memory returned an empty context")
    return context

async def archive_bot_action(
    self,
    *,
    action_key: str,
    action_type: str,
    text: str,
    published: bool,
    title: str = "",
    scene: str = "system",
    metadata: Optional[Dict[str, Any]] = None,
    importance: float = 0.6,
    status: str = "",
):
    """Archive a bot action intent or terminal outcome.

    Semantics for ``status`` / ``published``:
    - ``status=""`` + ``published=False`` → **intent** (open activity; not finish)
    - ``status=""`` + ``published=True`` → **completed**
    - explicit terminal (failed / result_unknown / rejected / drafted / …) → finish
    - explicit ``status="intent"`` → intent archive (not finish)

    Prefer ``finish_activity`` only for terminal states so intent rows stay distinct.
    """
    from bilibot.memory_brain.ingestion import bot_action_observation

    brain = getattr(self, "memory_brain", None)
    finish = getattr(brain, "finish_activity", None) if brain is not None else None
    status_s = str(status or "").strip().casefold()

    # Open intent: empty status + not published, or explicit intent.
    is_intent = (not status_s and not published) or status_s == "intent"
    if is_intent:
        return await self._archive_required(
            bot_action_observation(
                account_id=self.account_id or "default",
                action_key=action_key,
                action_type=action_type,
                text=text,
                published=False,
                persona_id=self._get_current_persona_id(),
                title=title,
                scene=scene,
                metadata=metadata or {},
                importance=importance,
                state="intent",
            )
        )

    terminal = status_s or ("completed" if published else "failed")
    if callable(finish) and callable(getattr(type(brain), "finish_activity", None)):
        try:
            return await finish(
                action_key=action_key,
                action_type=action_type,
                result_text=text,
                state=terminal if terminal != "rejected" else "rejected",
                scene=scene,
                title=title,
                persona_id=self._get_current_persona_id(),
                metadata=metadata or {},
            )
        except Exception:
            logger.warning(
                "finish_activity failed, falling back to archive: action=%s",
                action_key,
                exc_info=True,
            )

    return await self._archive_required(
        bot_action_observation(
            account_id=self.account_id or "default",
            action_key=action_key,
            action_type=action_type,
            text=text,
            published=published,
            persona_id=self._get_current_persona_id(),
            title=title,
            scene=scene,
            metadata=metadata or {},
            importance=importance,
            state=terminal,
        )
    )

def clean_platform_text(value: Any, limit: int = 160) -> str:
    from bilibot.scheduler_text import clean_platform_text

    return clean_platform_text(value, limit)

async def archive_proactive_video_failure(
    self,
    *,
    bvid: str = "",
    oid: str = "",
    title: str = "",
    owner: str = "",
    reason: str,
    task_id: str = "",
    tags: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    partial_evidence: str = "",
) -> str:
    """Persist one honest failed watch attempt and feed it back to SelfState."""
    from bilibot.memory_brain.ingestion import (
        bot_action_observation,
        text_observation,
    )

    clean_title = self._clean_platform_text(title or "未知视频", 120)
    clean_owner = self._clean_platform_text(owner, 60)
    reason_s = self._clean_platform_text(reason or "UNKNOWN", 160)
    attempt_key = (
        f"proactive_video_failure:{bvid or oid or 'feed'}:"
        f"{task_id or time.time_ns()}:{time.time_ns()}"
    )
    metadata: Dict[str, Any] = {
        "bvid": str(bvid or ""),
        "oid": str(oid or ""),
        "owner": clean_owner,
        "tags": [self._clean_platform_text(x, 40) for x in (tags or [])[:12]],
        "reason_code": reason_s.split(":", 1)[0],
        "failure_detail": reason_s,
        "task_id": str(task_id or ""),
        "watch_state": "attempted_not_completed",
    }
    metadata.update(dict(extra or {}))
    text = f"想看视频《{clean_title}》"
    if clean_owner:
        text += f"（UP主 {clean_owner}）"
    text += f"，但这次没有看成：{reason_s}。"
    partial_evidence_s = str(partial_evidence or "").strip()[:4000]

    # When a concrete video is known, preserve a compact, explicitly
    # metadata-only perception in addition to the action outcome.  This is
    # evidence that the bot encountered the item, never evidence that it
    # watched or understood it.
    failure_observation_event_id = ""
    if bvid or oid:
        observation_metadata = {
            **metadata,
            "observation_state": "failed",
            "metadata_only": True,
            "watched": False,
            "has_video_detail": False,
            "partial_evidence": bool(partial_evidence_s),
            "partial_evidence_chars": len(partial_evidence_s),
        }
        observation_text = text
        if partial_evidence_s:
            observation_text += (
                "\n\n【部分视听证据（提取未完成，不代表已看完）】\n"
                + partial_evidence_s
            )
        observation_result = await self._archive_required(
            text_observation(
                account_id=self.account_id or "default",
                idempotency_key=f"failed_video:{attempt_key}",
                source_type="video_metadata",
                event_type="video_metadata_observation",
                text=observation_text,
                title=clean_title,
                persona_id=self._get_current_persona_id(),
                scene="proactive_video",
                metadata=observation_metadata,
                importance=0.35,
                # A failed metadata sighting is already fully structured;
                # do not feed it into expensive enrichment/vision work.
                job_types=(),
            )
        )
        failure_observation_event_id = str(
            getattr(observation_result, "event_id", "") or ""
        )
    if failure_observation_event_id:
        metadata["failure_observation_event_id"] = failure_observation_event_id

    result = await self._archive_required(
        bot_action_observation(
            account_id=self.account_id or "default",
            action_key=attempt_key,
            action_type="watch_proactive_video",
            text=text,
            published=False,
            persona_id=self._get_current_persona_id(),
            title=clean_title,
            scene="proactive_video",
            metadata=metadata,
            importance=0.45,
            state="failed",
            job_types=(),
        )
    )
    event_id = str(getattr(result, "event_id", "") or "")
    brain_store = getattr(getattr(self, "memory_brain", None), "store", None)
    if brain_store is not None:
        try:
            if failure_observation_event_id:
                await asyncio.to_thread(
                    brain_store.set_event_index_status,
                    failure_observation_event_id,
                    "degraded",
                )
            if event_id:
                await asyncio.to_thread(
                    brain_store.set_event_index_status,
                    event_id,
                    "ready",
                )
        except Exception:
            logger.debug("failed video index status patch skipped", exc_info=True)
    if event_id and failure_observation_event_id and brain_store is not None:
        try:
            await asyncio.to_thread(
                brain_store.upsert_links,
                event_id,
                [
                    {
                        "target_event_id": failure_observation_event_id,
                        "relation_type": "is_about",
                        "weight": 1.0,
                        "evidence_ids": [event_id, failure_observation_event_id],
                    }
                ],
            )
        except Exception:
            logger.debug("failed video evidence link skipped", exc_info=True)
    companion = getattr(self, "companion", None)
    feedback = getattr(companion, "on_proactive_video_failed", None)
    exhausted = getattr(companion, "on_proactive_video_candidates_exhausted", None)
    if (bvid or oid) and callable(feedback):
        try:
            feedback(
                title=clean_title,
                bvid=str(bvid or ""),
                reason=reason_s,
                memory_event_id=event_id,
            )
        except Exception:
            logger.debug("companion video failure feedback skipped", exc_info=True)
    elif not (bvid or oid) and callable(exhausted):
        try:
            exhausted(reason=reason_s)
        except Exception:
            logger.debug("companion feed exhaustion feedback skipped", exc_info=True)
    return event_id

def bot_aliases(self) -> set[str]:
    from bilibot.scheduler_identity import build_bot_aliases

    bot_name = str(getattr(self, "_bot_name", "") or "").strip()
    try:
        persona_id = str(self._get_current_persona_id() or "").strip()
    except Exception:
        persona_id = ""
    return build_bot_aliases(bot_name, persona_id)

def is_bot_speaker(self, actor_id: str | int = "", username: str = "", text: str = "") -> bool:
    from bilibot.scheduler_identity import is_bot_speaker

    try:
        persona_id = str(self._get_current_persona_id() or "").strip()
    except Exception:
        persona_id = ""
    return is_bot_speaker(
        bot_uid=str(getattr(self, "_bot_uid", "") or ""),
        bot_name=str(getattr(self, "_bot_name", "") or ""),
        persona_id=persona_id,
        actor_id=actor_id,
        username=username,
        text=text,
    )

def bot_already_replied_to_source(
    self,
    replies: list,
    *,
    source_rpid: str,
    expected_text: str = "",
) -> bool:
    """楼中楼幂等：仅当 bot 已回复该 source_rpid（parent 匹配）才算已回。"""
    from bilibot.scheduler_identity import bot_already_replied_to_source

    return bot_already_replied_to_source(
        bot_uid=str(getattr(self, "_bot_uid", "") or ""),
        replies=replies,
        source_rpid=source_rpid,
        expected_text=expected_text,
    )

def comment_memory_title(self, username: str, text: str, reply_id: str | int = "") -> str:
    from bilibot.scheduler_text import comment_memory_title

    return comment_memory_title(username, text, reply_id)

def dynamic_card_context(self, card: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a small target context for comments under Bot's own dynamics."""
    from bilibot.scheduler_cards import dynamic_card_context

    return dynamic_card_context(card)

def redact_private_message_runtime(
    self, text: str, *, actor_id: str | int, username: str = ""
):
    """Return a PM-safe value even on the legacy direct-construction path."""
    from bilibot.scheduler_identity import redact_private_message_runtime

    return redact_private_message_runtime(
        getattr(self, "memory_brain", None),
        text,
        actor_id=actor_id,
        username=username,
        account_id=getattr(self, "account_id", ""),
    )

def bounded_recent_turns(
    turns: List[str], *, max_turns: int = 6, max_chars: int = 1200
) -> List[str]:
    from bilibot.scheduler_text import bounded_recent_turns

    return bounded_recent_turns(turns, max_turns=max_turns, max_chars=max_chars)

async def archive_comment_thread_context(
    self,
    replies: List[Dict[str, Any]],
    *,
    oid: str | int,
    comment_type: int,
    thread_key: str | int,
) -> List[str]:
    rows: List[Dict[str, Any]] = []
    prompt_lines: List[str] = []
    bot_name = str(getattr(self, "_bot_name", "") or "Bot")
    for reply in replies or []:
        member = reply.get("member", {}) or {}
        content = reply.get("content", {}) or {}
        text = str(content.get("message") or "")
        if not text:
            continue
        actor_id = str(reply.get("mid") or member.get("mid") or "")
        username = str(member.get("uname") or "?")
        is_bot = self._is_bot_speaker(actor_id, username, text)
        speaker = bot_name if is_bot else username
        prompt_lines.append(f"{speaker}: {text}")
        stored_actor = (
            "self" if is_bot else self._pseudonymize_actor_id(actor_id)
        )
        rows.append(
            {
                "external_id": str(reply.get("rpid") or reply.get("id") or len(rows)),
                "actor_id": stored_actor,
                "username": username,
                "text": text,
                "is_bot": is_bot,
                "occurred_at": reply.get("ctime") or reply.get("timestamp"),
            }
        )
    if rows:
        from bilibot.memory_brain.ingestion import comment_thread_observation

        await self._archive_required(
            comment_thread_observation(
                account_id=getattr(self, "account_id", "") or "default",
                thread_key=str(thread_key),
                rows=rows,
                oid=str(oid),
                comment_type=comment_type,
                persona_id=self._get_current_persona_id(),
            )
        )
    return prompt_lines

def private_message_text(message: Dict[str, Any]) -> str:
    raw = message.get("content", "") if isinstance(message, dict) else ""
    if not raw:
        return ""
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        value = raw
    if isinstance(value, dict):
        return str(value.get("content") or "")
    return str(value or "")

async def archive_pm_recent_history(
    self,
    messages: List[Dict[str, Any]],
    *,
    current_message_id: str,
    talker_id: int,
    talker_name: str,
    my_uid: int,
) -> List[str]:
    from bilibot.memory_brain.models import IdempotencyConflictError
    from bilibot.services.pm_state_store import extract_platform_message_id

    turns: List[str] = []
    brain = getattr(self, "memory_brain", None)
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        text = self._private_message_text(message)
        if not text:
            continue
        message_id = extract_platform_message_id(message)
        if not message_id:
            digest = hashlib.sha256(
                json.dumps(
                    message,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            message_id = f"history:{digest}"
        if str(message_id) == str(current_message_id):
            continue
        sender_id = str(message.get("sender_uid") or "")
        is_self = bool(my_uid and sender_id == str(my_uid))
        direction = "outgoing" if is_self else "incoming"
        safe = self._redact_private_message_runtime(
            text,
            actor_id="self" if is_self else str(talker_id),
            username=talker_name,
        )
        if brain is not None:
            try:
                _, result = await brain.archive_private_message(
                    platform_message_id=str(message_id),
                    text=text,
                    actor_id="self" if is_self else str(talker_id),
                    username=talker_name,
                    direction=direction,
                    persona_id=self._get_current_persona_id(),
                    redacted=safe,
                )
                if result is None or getattr(result, "source_committed", True) is False:
                    raise RuntimeError("PM history source commit was not confirmed")
            except IdempotencyConflictError:
                # History is contextual enrichment. If the platform returns a
                # changed representation for an already archived message, use
                # the existing memory version and keep the current PM gate
                # strict; do not pause the whole account for old context.
                logger.warning(
                    "私信历史幂等冲突，使用已归档版本: message_id=%s",
                    message_id,
                )
        speaker = "我" if is_self else "私信用户"
        turns.append(f"{speaker}: {safe.text}")
    return self._bounded_recent_turns(turns)
