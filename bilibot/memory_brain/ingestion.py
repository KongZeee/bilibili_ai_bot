"""Domain-to-envelope adapters for V6 lossless memory ingestion."""

from __future__ import annotations

import json
import hashlib
import time
from typing import Any, Mapping, Sequence

from .models import Observation, ObservationEnvelope, SourceDocument


_MEDIA_KEYS = frozenset(
    {
        "audio_path",
        "file_path",
        "frame_path",
        "image_path",
        "keyframe",
        "keyframes",
        "media_path",
        "video_path",
        "work_dir",
    }
)


def _safe_structured(value: Any) -> Any:
    """Remove media/binary artifacts while retaining extracted structured text."""
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _MEDIA_KEYS or normalized.endswith("_base64"):
                continue
            result[str(key)] = _safe_structured(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_structured(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[binary omitted]"
    return value


def _json_text(value: Any) -> str:
    return json.dumps(
        _safe_structured(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    )


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        _safe_structured(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _joined_observation_text(rows: Sequence[Mapping[str, Any]], field: str) -> str:
    return "\n".join(str(row.get(field) or "") for row in rows if row.get(field))


def comment_observation(
    *,
    account_id: str,
    comment_type: int | str,
    reply_id: str,
    text: str,
    actor_id: str,
    username: str = "",
    oid: str = "",
    title: str = "",
    context: Mapping[str, Any] | None = None,
    persona_id: str = "",
    observed_at: float | None = None,
) -> ObservationEnvelope:
    safe_context = _safe_structured(dict(context or {}))
    metadata = {
        "oid": str(oid),
        "reply_id": str(reply_id),
        "comment_type": str(comment_type),
    }
    metadata.update({f"target_{k}": v for k, v in safe_context.items() if v})
    return ObservationEnvelope(
        idempotency_key=f"comment:{account_id}:{comment_type}:{reply_id}:incoming",
        account_id=str(account_id),
        source_type="comment",
        event_type="conversation_message",
        event_title=title or f"评论 {reply_id}",
        speaker_actor_id=str(actor_id),
        persona_id=persona_id,
        scene="reply_comment",
        importance=0.35,
        occurred_at=observed_at or time.time(),
        metadata=metadata,
        sources=(
            SourceDocument(
                source_type="comment",
                external_id=str(reply_id),
                full_text=str(text),
                data={
                    "username": username,
                    "oid": str(oid),
                    "comment_text": str(text),
                    "target": safe_context,
                    "note": "This source text is the comment body, not a platform title.",
                },
                observations=(
                    Observation(
                        text=str(text),
                        modality="comment",
                        actor_id=str(actor_id),
                        external_id=str(reply_id),
                        occurred_at=observed_at,
                    ),
                ),
            ),
        ),
    )


def comment_thread_observation(
    *,
    account_id: str,
    thread_key: str,
    rows: Sequence[Mapping[str, Any]],
    oid: str = "",
    comment_type: int | str = 1,
    persona_id: str = "",
) -> ObservationEnvelope:
    """Archive every extracted turn from one comment thread without truncation."""
    safe_rows = tuple(_safe_structured(dict(row)) for row in rows)
    digest = _stable_hash(safe_rows)
    observations = []
    lines = []
    # occurred_at 必须稳定（基于 rows 内容），不能用 time.time()，
    # 否则同内容重试时 content_hash 不同 → IdempotencyConflictError → 账号风险暂停
    thread_occurred_at = 0.0
    for index, row in enumerate(safe_rows):
        text = str(row.get("text") or row.get("content") or "")
        if not text:
            continue
        username = str(row.get("username") or row.get("uname") or "")
        display_name = "\u4e9a\u6258\u8389" if bool(row.get("is_bot", False)) else username
        lines.append(f"{display_name}: {text}" if display_name else text)
        occurred = row.get("occurred_at") or row.get("ctime") or row.get("timestamp")
        try:
            occurred_at = float(occurred) if occurred is not None else None
        except (TypeError, ValueError):
            occurred_at = None
        if occurred_at is not None and occurred_at > thread_occurred_at:
            thread_occurred_at = occurred_at
        observations.append(
            Observation(
                text=text,
                modality="comment_turn",
                actor_id=str(row.get("actor_id") or row.get("mid") or ""),
                external_id=str(row.get("external_id") or row.get("rpid") or index),
                occurred_at=occurred_at,
                data={
                    "username": username,
                    "is_bot": bool(row.get("is_bot", False)),
                },
            )
        )
    # \u4e0d\u8981\u7528\u56fa\u5b9a\u6807\u9898\u300c\u8bc4\u8bba\u5bf9\u8bdd\u4e0a\u4e0b\u6587\u300d\uff1afind_events_by_identifiers \u4f1a\u6309 e.title
    # \u7cbe\u786e\u5339\u914d\uff0c\u56fa\u5b9a\u6807\u9898\u4f1a\u8ba9 title_entity \u901a\u9053\u628a\u6240\u6709\u8bc4\u8bba\u7ebf\u7a0b\u4e00\u8d77\u635e\u4e0a\u6765\uff0c
    # \u4e5f\u4f1a\u88ab\u9519\u8bef\u590d\u7528\u6210\u89c6\u9891\u6807\u9898\u3002\u6539\u6210\u5e26 thread_key \u7684\u53ef\u533a\u5206\u6807\u9898\u3002
    first_line = lines[0] if lines else ""
    if len(first_line) > 40:
        first_line = first_line[:40].rstrip() + "\u2026"
    event_title = (
        f"\u8bc4\u8bba\u7ebf\u7a0b {thread_key}"
        + (f"\uff1a{first_line}" if first_line else "")
    )
    return ObservationEnvelope(
        idempotency_key=(
            f"comment_thread:{account_id}:{comment_type}:{oid}:{thread_key}:{digest}"
        ),
        source_type="comment_thread",
        event_type="conversation_context",
        event_title=event_title,
        persona_id=persona_id,
        scene="reply_comment",
        importance=0.3,
        occurred_at=thread_occurred_at,
        metadata={
            "oid": str(oid),
            "thread_key": str(thread_key),
            "comment_type": str(comment_type),
            "turn_count": len(observations),
        },
        sources=(
            SourceDocument(
                source_type="comment_thread",
                external_id=str(thread_key),
                full_text="\n".join(lines),
                data={"turns": safe_rows},
                observations=tuple(observations),
            ),
        ),
    )

def video_metadata_observation(
    *,
    account_id: str,
    oid: str,
    metadata: Mapping[str, Any],
    persona_id: str = "",
    scene: str = "reply_comment",
) -> ObservationEnvelope:
    """Archive complete video metadata fetched for a model context."""
    safe_metadata = _safe_structured(dict(metadata))
    digest = _stable_hash(safe_metadata)
    bvid = str(safe_metadata.get("bvid") or "")
    title = str(safe_metadata.get("title") or "")
    owner = safe_metadata.get("owner") or {}
    owner_name = str(owner.get("name") or "") if isinstance(owner, Mapping) else ""
    return ObservationEnvelope(
        idempotency_key=f"video_metadata:{account_id}:{oid or bvid}:{digest}",
        source_type="video_metadata",
        event_type="video_metadata_observation",
        event_title=title or f"视频元数据 {oid or bvid}",
        event_summary=(
            f"获取了视频《{title}》的元数据，UP主 {owner_name}".strip()
        ),
        persona_id=persona_id,
        scene=scene,
        importance=0.35,
        occurred_at=time.time(),
        metadata={"oid": str(oid), "bvid": bvid, "owner": owner_name},
        sources=(
            SourceDocument(
                source_type="video_metadata",
                external_id=bvid or str(oid),
                full_text=_json_text(safe_metadata),
                data=safe_metadata,
                observations=(),
            ),
        ),
    )


def bot_action_observation(
    *,
    account_id: str,
    action_key: str,
    action_type: str,
    text: str,
    published: bool,
    persona_id: str = "",
    title: str = "",
    scene: str = "system",
    metadata: Mapping[str, Any] | None = None,
    importance: float = 0.6,
    state: str = "",
) -> ObservationEnvelope:
    action_state = str(state or ("completed" if published else "intent")).strip().casefold()
    allowed_states = {
        "intent",
        "completed",
        "failed",
        "result_unknown",
        "rejected",
        "deferred",
        "drafted",
        "skipped",
    }
    if action_state not in allowed_states:
        raise ValueError(f"unsupported bot action state: {action_state}")
    safe_metadata = _safe_structured(dict(metadata or {}))
    # Correlation helpers for dynamic posts (draft_id / task_id / dynamic_id) stay
    # in metadata so companion life feedback and list/find_by_identifiers can join.
    for corr_key in ("draft_id", "task_id", "dynamic_id", "topic"):
        if corr_key in safe_metadata and safe_metadata[corr_key] is not None:
            safe_metadata[corr_key] = str(safe_metadata[corr_key])
    safe_metadata.update(
        {
            "action_type": action_type,
            "action_state": action_state,
            "published": bool(published),
        }
    )
    external_id = str(
        safe_metadata.get("dynamic_id")
        or safe_metadata.get("draft_id")
        or action_key
    )
    return ObservationEnvelope(
        idempotency_key=f"bot_action:{account_id}:{action_key}:{action_state}",
        account_id=str(account_id),
        source_type="bot_action",
        event_type=(
            "bot_action"
            if action_state == "completed"
            else "action_intent"
            if action_state == "intent"
            else "action_outcome"
        ),
        event_title=title or action_type,
        event_summary=str(text),
        speaker_actor_id="self",
        persona_id=persona_id,
        scene=scene,
        importance=max(0.0, min(1.0, float(importance))),
        occurred_at=time.time(),
        metadata=safe_metadata,
        sources=(
            SourceDocument(
                source_type="bot_action",
                external_id=external_id,
                full_text=str(text),
                data=safe_metadata,
                observations=(
                    Observation(text=str(text), modality="bot_action", actor_id="self"),
                ),
            ),
        ),
    )


def video_observation(
    *,
    account_id: str,
    observation_key: str,
    bvid: str,
    oid: str,
    title: str,
    owner: str,
    context: Mapping[str, Any],
    tags: Sequence[str] = (),
    persona_id: str = "",
    video_detail: str = "",
) -> ObservationEnvelope:
    safe_context = _safe_structured(context)
    metadata = safe_context.get("metadata") or {}
    hot_comments = safe_context.get("hot_comments") or []
    search_reference = safe_context.get("search_reference") or {}
    audiovisual = safe_context.get("audiovisual") or {}
    sources: list[SourceDocument] = []

    # Prefer a compact ≤2000-char "video_detail" note as the primary recall surface.
    # Raw audiovisual sources remain for deep retrieval / re-summarization.
    detail_text = str(video_detail or "").strip()
    if not detail_text:
        # Allow callers to stash it on context as well.
        detail_text = str(safe_context.get("video_detail") or "").strip()
    if detail_text:
        if len(detail_text) > 2000:
            detail_text = detail_text[:2000].rstrip()
        sources.append(
            SourceDocument(
                source_type="video_detail",
                external_id=bvid or oid,
                full_text=detail_text,
                data={"max_chars": 2000, "kind": "audiovisual_digest"},
                observations=(
                    Observation(
                        text=detail_text,
                        modality="video_detail",
                        actor_id="self",
                    ),
                ),
            )
        )

    if metadata:
        sources.append(
            SourceDocument(
                source_type="video_metadata",
                external_id=bvid or oid,
                full_text=_json_text(metadata),
                data=metadata,
                observations=(),
            )
        )
    if hot_comments:
        observations = []
        for index, row in enumerate(hot_comments):
            if isinstance(row, Mapping):
                text = str(
                    row.get("content") or row.get("message") or row.get("text") or ""
                )
                actor = str(row.get("mid") or row.get("user_id") or "")
                external_id = str(row.get("rpid") or row.get("id") or index)
            else:
                text, actor, external_id = str(row), "", str(index)
            if text:
                observations.append(
                    Observation(
                        text=text,
                        modality="hot_comment",
                        actor_id=actor,
                        external_id=external_id,
                    )
                )
        sources.append(
            SourceDocument(
                source_type="video_hot_comments",
                external_id=bvid or oid,
                full_text=_json_text(hot_comments),
                data={"count": len(hot_comments)},
                observations=tuple(observations),
            )
        )
    if search_reference:
        sources.append(
            SourceDocument(
                source_type="web_reference",
                external_id=bvid or oid,
                full_text=_json_text(search_reference),
                data=search_reference,
                observations=(),
            )
        )

    audio_rows = audiovisual.get("audio_observations") or []
    if audio_rows:
        audio_observations = tuple(
            Observation(
                text=str(row.get("text") or ""),
                modality=str(row.get("source") or "asr"),
                start_ms=int(float(row.get("start") or 0) * 1000),
                end_ms=int(float(row.get("end") or 0) * 1000),
                data=row,
            )
            for row in audio_rows
            if isinstance(row, Mapping) and row.get("text")
        )
        sources.append(
            SourceDocument(
                source_type="subtitle" if any(
                    str(row.get("source")) == "subtitle" for row in audio_rows if isinstance(row, Mapping)
                ) else "asr",
                external_id=bvid or oid,
                full_text=_joined_observation_text(audio_rows, "text"),
                data={
                    "segments": audio_rows,
                    "quality": audiovisual.get("audio_status") or {},
                },
                observations=audio_observations,
            )
        )

    visual_rows = audiovisual.get("visual_observations") or []
    if visual_rows:
        visual_observations: list[Observation] = []
        ocr_observations: list[Observation] = []
        for row in visual_rows:
            if not isinstance(row, Mapping):
                continue
            timestamp_ms = int(float(row.get("timestamp") or 0) * 1000)
            frame_data = {"frame_number": row.get("frame_number")}
            description = str(row.get("description") or "")
            if description:
                visual_observations.append(
                    Observation(
                        text=description,
                        modality="visual_description",
                        start_ms=timestamp_ms,
                        end_ms=timestamp_ms,
                        data=frame_data,
                    )
                )
            ocr_text = str(row.get("ocr_text") or row.get("ocr") or "")
            if ocr_text:
                ocr_observations.append(
                    Observation(
                        text=ocr_text,
                        modality="ocr",
                        start_ms=timestamp_ms,
                        end_ms=timestamp_ms,
                        data=frame_data,
                    )
                )
        if visual_observations:
            sources.append(
                SourceDocument(
                    source_type="visual_description",
                    external_id=bvid or oid,
                    full_text="\n".join(item.text for item in visual_observations),
                    data={"observations": visual_rows},
                    observations=tuple(visual_observations),
                )
            )
        if ocr_observations:
            sources.append(
                SourceDocument(
                    source_type="ocr",
                    external_id=bvid or oid,
                    full_text="\n".join(item.text for item in ocr_observations),
                    data={"observations": visual_rows},
                    observations=tuple(ocr_observations),
                )
            )

    behavior_log = str(audiovisual.get("behavior_log") or "")
    if behavior_log:
        sources.append(
            SourceDocument(
                source_type="behavior_log",
                external_id=bvid or oid,
                full_text=behavior_log,
                data={"timeline": audiovisual.get("timeline_observations") or []},
            )
        )

    if not sources:
        sources.append(
            SourceDocument(
                source_type="video_metadata",
                external_id=bvid or oid,
                full_text=f"视频《{title}》，UP主 {owner}",
                data={"title": title, "owner": owner},
                observations=(),
            )
        )

    return ObservationEnvelope(
        idempotency_key=f"video:{account_id}:{observation_key}",
        account_id=str(account_id),
        source_type="video",
        event_type="video_observation",
        event_title=title,
        # Prefer the detailed digest as event.summary so title/id hits already
        # carry "what the video is about" without needing chunk re-ranking.
        event_summary=(
            detail_text
            if detail_text
            else f"观察了视频《{title}》，UP主 {owner}"
        ),
        speaker_actor_id="self",
        persona_id=persona_id,
        scene="proactive_video",
        importance=0.55,
        occurred_at=time.time(),
        metadata={
            "bvid": bvid,
            "oid": str(oid),
            "owner": owner,
            "tags": list(tags),
            "has_video_detail": bool(detail_text),
        },
        sources=tuple(sources),
    )


def bangumi_episode_observation(
    *,
    account_id: str,
    observation_key: str,
    season_id: str | int,
    episode_id: str | int,
    season_title: str,
    episode_title: str,
    episode_index: str,
    analysis_result: Mapping[str, Any],
    subtitle_segments: Sequence[Mapping[str, Any]] = (),
    persona_id: str = "",
    evaluation: Mapping[str, Any] | None = None,
) -> ObservationEnvelope:
    """Archive one bangumi episode into the unified account brain.

    Stored as ``source_type=bangumi`` / ``event_type=bangumi_episode`` so
    cross-scene recall (comment/dynamic/diary) can find 番名/集数/评价 via the
    same FTS/vector space as proactive video — not only ``bangumi_watch_state``.
    """
    safe_eval = _safe_structured(dict(evaluation or {}))
    context = {
        "metadata": {
            "season_id": str(season_id),
            "episode_id": str(episode_id),
            "title": season_title,
            "episode_title": episode_title,
            "episode_index": episode_index,
            "kind": "bangumi",
        },
        "audiovisual": _safe_structured(analysis_result),
    }
    if subtitle_segments and not context["audiovisual"].get("audio_observations"):
        context["audiovisual"]["audio_observations"] = [
            {
                "start": item.get("from", 0),
                "end": item.get("to", 0),
                "text": item.get("content", ""),
                "source": "subtitle",
            }
            for item in subtitle_segments
            if item.get("content")
        ]
    title = f"{season_title} 第{episode_index}话 {episode_title}".strip()
    review_bit = str(
        safe_eval.get("review") or safe_eval.get("comment") or ""
    ).strip()
    score_bit = safe_eval.get("score")
    summary_parts = [f"看了番剧《{season_title}》第{episode_index}话"]
    if episode_title:
        summary_parts[0] += f"「{episode_title}」"
    if score_bit is not None and str(score_bit) != "":
        summary_parts.append(f"评分{score_bit}/10")
    if review_bit:
        summary_parts.append(review_bit[:200])
    event_summary = "，".join(summary_parts)

    envelope = video_observation(
        account_id=account_id,
        observation_key=observation_key,
        bvid="",
        oid=str(episode_id),
        title=title,
        owner="番剧",
        context=context,
        persona_id=persona_id,
    )
    # Prefer a short narrative summary over raw AV digest so list/recall surfaces
    # show 番名/集数/评价 without needing chunk re-ranking.
    meta = {
        **dict(envelope.metadata),
        "season_id": str(season_id),
        "episode_id": str(episode_id),
        "episode_index": str(episode_index),
        "season_title": str(season_title),
        "episode_title": str(episode_title),
        "kind": "bangumi",
    }
    if safe_eval:
        meta["evaluation"] = safe_eval
    return ObservationEnvelope(
        **{
            **envelope.__dict__,
            "idempotency_key": f"bangumi:{account_id}:{observation_key}",
            "account_id": str(account_id),
            "source_type": "bangumi",
            "event_type": "bangumi_episode",
            "event_title": title,
            "event_summary": event_summary,
            "scene": "bangumi",
            "importance": 0.6,
            "metadata": meta,
        }
    )


def text_observation(
    *,
    account_id: str,
    idempotency_key: str,
    source_type: str,
    event_type: str,
    text: str,
    title: str = "",
    persona_id: str = "",
    scene: str = "system",
    metadata: Mapping[str, Any] | None = None,
    importance: float = 0.5,
) -> ObservationEnvelope:
    return ObservationEnvelope(
        idempotency_key=f"{source_type}:{account_id}:{idempotency_key}",
        account_id=str(account_id),
        source_type=source_type,
        event_type=event_type,
        event_title=title,
        event_summary=str(text) if len(str(text)) <= 500 else "",
        speaker_actor_id=(
            "self"
            if source_type in {"bot_action", "dynamic", "summary", "weekly_summary"}
            else ""
        ),
        persona_id=persona_id,
        scene=scene,
        importance=max(0.0, min(1.0, float(importance))),
        occurred_at=time.time(),
        metadata=_safe_structured(dict(metadata or {})),
        sources=(
            SourceDocument(
                source_type=source_type,
                external_id=idempotency_key,
                full_text=str(text),
                data=_safe_structured(dict(metadata or {})),
            ),
        ),
    )


__all__ = [
    "bangumi_episode_observation",
    "bot_action_observation",
    "comment_observation",
    "comment_thread_observation",
    "text_observation",
    "video_metadata_observation",
    "video_observation",
]
