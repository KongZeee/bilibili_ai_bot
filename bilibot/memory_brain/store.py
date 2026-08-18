"""SQLite persistence for an account-scoped associative memory brain.

The database stores extracted text losslessly. Search documents, summaries and
embeddings are derived indexes and can always be rebuilt from the source rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
import sqlite3
import struct
import threading
import time
import unicodedata
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:
    import jieba
except ImportError:  # pragma: no cover - requirements include jieba
    jieba = None

from .models import (
    ArchiveResult,
    ClaimedJob,
    HealthReport,
    IdempotencyConflictError,
    Observation,
    ObservationEnvelope,
    ReingestBlockedError,
    SCHEMA_VERSION,
    SourceDocument,
    VectorDimensionError,
)

logger = logging.getLogger("bilibot.memory_brain.store")


_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_TOKEN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[A-Za-z0-9_]+|[^\s]",
    re.UNICODE,
)
_SENTENCE_RE = re.compile(r".+?(?:\r?\n+|[。！？!?；;]+|$)", re.DOTALL)
_TRACE_REDACTIONS = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(
        r"(?i)\b(?:cookie|token|authorization|api[_-]?key)\b\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"(?i)\b(?:uid|order(?:_id)?|订单号)\b\s*[:=：]?\s*[A-Za-z0-9_-]{5,}"),
)
_JOB_TYPES = {
    "summarize_event",
    "embed_event",
    "embed_chunks",
    "extract_entities",
    "link_associations",
}
_EVENT_EMBEDDING_TITLE_LIMIT = 256
_ENTITY_TYPE_TAXONOMY_VERSION = "1"
_HEALTH_REQUIRED_TABLES = frozenset(
    {
        "brain_info",
        "schema_migrations",
        "memory_events",
        "memory_sources",
        "memory_observations",
        "memory_chunks",
        "memory_event_fts",
        "memory_chunk_fts",
        "embedding_models",
        "memory_embeddings",
        "memory_entities",
        "memory_entity_aliases",
        "memory_entity_mentions",
        "memory_links",
        "brain_jobs",
        "recall_traces",
        "recall_candidates",
        "deletion_tombstones",
        "legacy_cleanup_log",
        "bangumi_watch_state",
    }
)


def normalize_entity_type(value: Any) -> str:
    """Collapse free-form model labels into a stable entity taxonomy."""

    raw = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    compact = re.sub(r"[\s_\-]+", " ", raw)
    if not compact:
        return "topic"
    rules = (
        ("character", ("character", "fictional", "角色")),
        (
            "person",
            (
                "person",
                "creator",
                "author",
                "up主",
                "up host",
                "historical figure",
                "handle",
                "user id",
                "user_id",
                "人物",
                "人名",
            ),
        ),
        (
            "organization",
            (
                "organization",
                "organisation",
                "company",
                "brand",
                "platform",
                "server",
                "group",
                "组织",
                "机构",
            ),
        ),
        (
            "location",
            (
                "location",
                "country",
                "city",
                "place",
                "geographic",
                "faction",
                "world",
                "地点",
                "地区",
                "国家",
            ),
        ),
        (
            "work",
            (
                "work",
                "video",
                "game",
                "song",
                "tv series",
                "media franchise",
                "channel/series",
                "title",
                "作品",
                "视频",
                "游戏",
                "歌曲",
            ),
        ),
        ("event", ("event", "challenge", "exam", "crime", "事件", "活动")),
        (
            "animal",
            (
                "animal",
                "organism",
                "species",
                "creature",
                "pet",
                "plant",
                "taxonomic",
                "动物",
                "生物",
                "植物",
            ),
        ),
        (
            "food",
            (
                "food",
                "dish",
                "cuisine",
                "ingredient",
                "condiment",
                "drink",
                "菜",
                "食物",
                "饮料",
                "原料",
            ),
        ),
        (
            "product",
            (
                "product",
                "object",
                "item",
                "prop",
                "artifact",
                "vehicle",
                "material",
                "outfit",
                "产品",
                "物品",
                "道具",
            ),
        ),
        (
            "activity",
            ("activity", "action", "dance", "行为", "动作"),
        ),
        (
            "time",
            ("date", "timeperiod", "geologicalperiod", "time period", "日期", "时间"),
        ),
        (
            "concept",
            (
                "concept",
                "subject",
                "ability",
                "skill",
                "technique",
                "law",
                "principle",
                "language",
                "meme",
                "phenomenon",
                "role",
                "class",
                "feature",
                "version",
                "hashtag",
                "slang",
                "概念",
                "技能",
                "话题",
            ),
        ),
    )
    for canonical, needles in rules:
        if any(needle in compact for needle in needles):
            return canonical
    return "topic"


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS brain_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    speaker_actor_id TEXT NOT NULL DEFAULT '',
    persona_id TEXT NOT NULL DEFAULT '',
    scene TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5 CHECK(importance >= 0 AND importance <= 1),
    occurred_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    index_status TEXT NOT NULL DEFAULT 'pending',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_events_created ON memory_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_events_speaker ON memory_events(speaker_actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_events_source ON memory_events(source_type, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_sources (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL DEFAULT 0,
    source_type TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    full_text TEXT NOT NULL,
    structured_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(event_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_memory_sources_external ON memory_sources(external_id);

CREATE TABLE IF NOT EXISTS memory_observations (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES memory_sources(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    modality TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    structured_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    occurred_at REAL,
    start_ms INTEGER,
    end_ms INTEGER,
    ignored INTEGER NOT NULL DEFAULT 0,
    ignore_reason TEXT NOT NULL DEFAULT '',
    extractor_version TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(event_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_memory_observations_source ON memory_observations(source_id, ordinal);

CREATE TABLE IF NOT EXISTS memory_chunks (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES memory_sources(id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL REFERENCES memory_observations(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    observation_ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    overlap_chars INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(event_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_observation ON memory_chunks(observation_id, observation_ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_event_fts USING fts5(
    event_id UNINDEXED,
    search_text,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunk_fts USING fts5(
    chunk_id UNINDEXED,
    event_id UNINDEXED,
    search_text,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS embedding_models (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    config_hash TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(provider, model, dimension, config_hash)
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('event', 'chunk')),
    target_id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    dimension INTEGER NOT NULL,
    vector_blob BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    norm REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(target_type, target_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_scan ON memory_embeddings(model_id, target_type, id);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_event ON memory_embeddings(event_id, target_type);

CREATE TABLE IF NOT EXISTS memory_entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    entity_type TEXT NOT NULL DEFAULT 'topic',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_entity_aliases (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_entity_mentions (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    observation_id TEXT REFERENCES memory_observations(id) ON DELETE CASCADE,
    chunk_id TEXT REFERENCES memory_chunks(id) ON DELETE CASCADE,
    surface_text TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    UNIQUE(entity_id, event_id, observation_id, chunk_id, surface_text)
);
CREATE INDEX IF NOT EXISTS idx_memory_mentions_event ON memory_entity_mentions(event_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_mentions_dedupe ON memory_entity_mentions(
    entity_id,event_id,ifnull(observation_id,''),ifnull(chunk_id,''),surface_text
);

CREATE TABLE IF NOT EXISTS memory_links (
    id TEXT PRIMARY KEY,
    source_event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    target_event_id TEXT NOT NULL REFERENCES memory_events(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0.5 CHECK(weight >= 0 AND weight <= 1),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK(source_event_id <> target_event_id),
    UNIQUE(source_event_id, target_event_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_target ON memory_links(target_event_id, weight DESC);

CREATE TABLE IF NOT EXISTS brain_jobs (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    job_type TEXT NOT NULL,
    event_id TEXT REFERENCES memory_events(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','retry','blocked','completed','dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 8,
    available_at REAL NOT NULL,
    lease_owner TEXT,
    leased_until REAL,
    last_error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_brain_jobs_ready ON brain_jobs(status, available_at, leased_until);
CREATE INDEX IF NOT EXISTS idx_brain_jobs_event ON brain_jobs(event_id, created_at, id);

CREATE TABLE IF NOT EXISTS recall_traces (
    id TEXT PRIMARY KEY,
    query_hash TEXT NOT NULL,
    query_text TEXT NOT NULL DEFAULT '',
    scene TEXT NOT NULL DEFAULT '',
    used_fallback INTEGER NOT NULL DEFAULT 0,
    rerank_status TEXT NOT NULL DEFAULT '',
    rerank_calls INTEGER NOT NULL DEFAULT 0,
    channel_errors_json TEXT NOT NULL DEFAULT '{}',
    latency_ms REAL NOT NULL DEFAULT 0,
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recall_candidates (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES recall_traces(id) ON DELETE CASCADE,
    event_id TEXT REFERENCES memory_events(id) ON DELETE SET NULL,
    channels_json TEXT NOT NULL DEFAULT '[]',
    channel_ranks_json TEXT NOT NULL DEFAULT '{}',
    rrf_score REAL NOT NULL DEFAULT 0,
    deterministic_score REAL NOT NULL DEFAULT 0,
    llm_score REAL,
    final_score REAL,
    decision TEXT NOT NULL DEFAULT '',
    threshold REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    injected INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deletion_tombstones (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    deleted_by TEXT NOT NULL DEFAULT 'admin',
    deleted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_cleanup_log (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('deleted','missing','failed')),
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    last_attempted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bangumi_watch_state (
    season_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    episode_title TEXT NOT NULL DEFAULT '',
    progress_ms INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    watched_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY(season_id, episode_id)
);
"""


_SCHEMA_V2_COLUMNS: Mapping[str, Sequence[tuple[str, str]]] = {
    "memory_observations": (
        ("ignored", "INTEGER NOT NULL DEFAULT 0"),
        ("ignore_reason", "TEXT NOT NULL DEFAULT ''"),
        ("extractor_version", "TEXT NOT NULL DEFAULT ''"),
    ),
    "recall_traces": (
        ("rerank_status", "TEXT NOT NULL DEFAULT ''"),
        ("rerank_calls", "INTEGER NOT NULL DEFAULT 0"),
        ("channel_errors_json", "TEXT NOT NULL DEFAULT '{}'"),
    ),
    "recall_candidates": (
        ("channel_ranks_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("rrf_score", "REAL NOT NULL DEFAULT 0"),
        ("threshold", "REAL NOT NULL DEFAULT 0"),
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("accepted", "INTEGER NOT NULL DEFAULT 0"),
    ),
}


def _upgrade_schema(conn: sqlite3.Connection) -> None:
    """Apply additive V6 schema upgrades to databases created by earlier builds."""

    for table, columns in _SCHEMA_V2_COLUMNS.items():
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _looks_like_platform_uid(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.isdigit()


def _pseudonymize_actor_payload(
    value: Any,
    pseudonymize,
    *,
    bot_actor_id: str = "",
) -> tuple[Any, bool]:
    """Recursively replace raw platform actor IDs in structured payloads."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            if key in {"mid", "user_id", "actor_id", "uid"} and _looks_like_platform_uid(item):
                out[key] = (
                    "self"
                    if bot_actor_id and str(item).strip() == str(bot_actor_id).strip()
                    else pseudonymize(str(item).strip())
                )
                changed = True
            else:
                child, child_changed = _pseudonymize_actor_payload(
                    item, pseudonymize, bot_actor_id=bot_actor_id
                )
                out[key] = child
                changed = changed or child_changed
        return out, changed
    if isinstance(value, list):
        out = []
        changed = False
        for item in value:
            child, child_changed = _pseudonymize_actor_payload(
                item, pseudonymize, bot_actor_id=bot_actor_id
            )
            out.append(child)
            changed = changed or child_changed
        return out, changed
    return value, False


def _legacy_event_has_raw_actor(conn: sqlite3.Connection, event_id: str) -> bool:
    row = conn.execute(
        "SELECT speaker_actor_id FROM memory_events WHERE id=?", (event_id,)
    ).fetchone()
    if row is not None and _looks_like_platform_uid(row["speaker_actor_id"]):
        return True
    obs = conn.execute(
        "SELECT 1 FROM memory_observations WHERE event_id=? AND actor_id "
        "GLOB '*[0-9]*'",
        (event_id,),
    ).fetchone()
    if obs is not None:
        return True
    for srow in conn.execute(
        "SELECT structured_json,full_text FROM memory_sources WHERE event_id=?",
        (event_id,),
    ).fetchall():
        for blob in (srow["structured_json"], srow["full_text"]):
            if not blob:
                continue
            try:
                payload = json.loads(blob)
            except (TypeError, json.JSONDecodeError):
                continue
            _, changed = _pseudonymize_actor_payload(
                payload,
                lambda value: value,
            )
            if changed:
                return True
    return False


def content_hash(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_search_text(text: str) -> str:
    """Normalize text without changing the archived source."""

    return unicodedata.normalize("NFKC", str(text)).lower()


def _sanitize_trace_query(text: str) -> str:
    value = str(text)
    labels = ("[REDACTED_EMAIL]", "[REDACTED_PHONE]", "[REDACTED_SECRET]", "[REDACTED_ID]")
    for pattern, label in zip(_TRACE_REDACTIONS, labels):
        value = pattern.sub(label, value)
    return value[:1200]


def estimate_tokens(text: str) -> int:
    """Conservative model-independent estimate used to enforce chunk limits."""

    return len(_TOKEN_RE.findall(text))


MAX_FTS_SOURCE_CHARS = 200_000


def build_fts_text(text: str, stable_ids: Iterable[str] = ()) -> str:
    """Build unicode61 input with Jieba search terms and explicit CJK bigrams."""

    normalized = normalize_search_text(text)
    # Long documents are recalled through their chunks; event-level FTS only
    # needs a bounded prefix. Unbounded ASR/behavior-log rows would otherwise
    # inflate the FTS table ~5x and make inserts pathologically slow.
    if len(normalized) > MAX_FTS_SOURCE_CHARS:
        normalized = normalized[:MAX_FTS_SOURCE_CHARS]
    terms: list[str] = [normalize_search_text(item) for item in stable_ids if item]
    # Single repeated character for tens of thousands of chars (OCR/ASR noise)
    # makes jieba and the bigram loop quadratic. Skip both and keep the raw
    # prefix only — such input has no useful search tokens.
    pathological_single_run = (
        bool(normalized) and len(normalized) > 10_000 and len(set(normalized)) == 1
    )
    if jieba is not None and normalized and not pathological_single_run:
        terms.extend(token.strip() for token in jieba.cut_for_search(normalized) if token.strip())
    if not pathological_single_run:
        for match in _CJK_RUN_RE.finditer(normalized):
            run = match.group(0)
            terms.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return " ".join([normalized, *terms]).strip()


def _fts_query_terms(query: str) -> list[str]:
    """Return the exact OR terms used by FTS in stable insertion order."""

    normalized = normalize_search_text(query)
    terms: list[str] = []
    if jieba is not None:
        terms.extend(token.strip() for token in jieba.cut_for_search(normalized) if token.strip())
    terms.extend(match.group(0) for match in re.finditer(r"[a-z0-9_:-]+", normalized))
    for match in _CJK_RUN_RE.finditer(normalized):
        run = match.group(0)
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[index : index + 2] for index in range(len(run) - 1))
    return list(dict.fromkeys(term for term in terms if term))[:64]


def _fts_coverage(
    search_text: str, query_terms: Sequence[str]
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    """Measure how much of a query a returned OR row actually covers.

    Single CJK characters and punctuation remain available to FTS candidate
    generation, but are intentionally not deterministic evidence.  The
    indexed text contains the Jieba/search-bigram terms separated by spaces,
    so exact token intersection mirrors what unicode61 can match without
    treating ASCII substrings as whole-token hits.
    """

    eligible = tuple(
        term
        for term in query_terms
        if re.fullmatch(r"[a-z0-9_:-]+", term)
        or (len(term) >= 2 and _CJK_RUN_RE.fullmatch(term))
    )
    if not eligible:
        return 0.0, (), ()
    document_terms = set(normalize_search_text(search_text).split())
    matched = tuple(term for term in eligible if term in document_terms)
    return len(matched) / len(eligible), matched, eligible


def normalize_vector(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("embedding vector must contain finite values")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding vector must have a non-zero norm")
    return [value / norm for value in values]


def encode_vector(vector: Sequence[float]) -> tuple[bytes, int]:
    values = normalize_vector(vector)
    return struct.pack(f"<{len(values)}f", *values), len(values)


def decode_vector(blob: bytes, dimension: int) -> list[float]:
    expected = int(dimension) * 4
    if len(blob) != expected:
        raise VectorDimensionError(
            f"vector BLOB has {len(blob)} bytes, expected {expected} for dimension {dimension}"
        )
    return list(struct.unpack(f"<{dimension}f", blob))


class _VectorCacheEntry:
    """Immutable, model-scoped matrix and the rows represented by it."""

    __slots__ = (
        "dimension",
        "matrix",
        "metadata",
        "embedding_ids",
        "target_ids",
        "event_ids",
        "query_template",
    )

    def __init__(
        self,
        *,
        dimension: int,
        matrix: Any,
        metadata: tuple[tuple[str, str, str], ...],
        embedding_ids: Any = None,
        target_ids: Any = None,
        event_ids: Any = None,
    ) -> None:
        self.dimension = int(dimension)
        self.matrix = matrix
        self.metadata = metadata
        # Parallel id arrays (optional) speed up top-k materialization.
        self.embedding_ids = embedding_ids
        self.target_ids = target_ids
        self.event_ids = event_ids
        # Reusable contiguous query buffer (filled under the cache lock).
        self.query_template = None


def _envelope_hash(envelope: ObservationEnvelope, sources: Sequence[SourceDocument]) -> str:
    # Hash only what archive_observation actually persists. Top-level legacy
    # envelope fields (source_text / source_data / observations / account_id /
    # occurred_at) are either redundant with normalized sources or store
    # boundaries, and must not make identical rows look like conflicts.
    payload = {
        "event_type": envelope.event_type,
        "event_title": envelope.event_title,
        "event_summary": envelope.event_summary,
        "speaker_actor_id": envelope.speaker_actor_id,
        "persona_id": envelope.persona_id,
        "scene": envelope.scene,
        "importance": envelope.importance,
        "metadata": envelope.metadata,
        "sources": [
            {
                **asdict(source),
                "observations": [
                    {k: v for k, v in asdict(item).items() if k != "occurred_at"}
                    for item in source.normalized_observations()
                ],
            }
            for source in sources
        ],
    }
    return content_hash(_json_dumps(payload))


def _source_row_hash(
    *,
    ordinal: int,
    source_type: str,
    external_id: str,
    full_text: str,
    structured_data: Mapping[str, Any],
) -> str:
    return content_hash(
        _json_dumps(
            {
                "ordinal": int(ordinal),
                "source_type": source_type,
                "external_id": external_id,
                "full_text": full_text,
                "structured_json": _json_dumps(structured_data),
            }
        )
    )


def _observation_row_hash(*, ordinal: int, observation: Any) -> str:
    return content_hash(
        _json_dumps(
            {
                "ordinal": int(ordinal),
                "modality": observation.modality,
                "actor_id": observation.actor_id,
                "external_id": observation.external_id,
                "text": observation.text,
                "structured_json": _json_dumps(observation.data),
                "occurred_at": observation.occurred_at,
                "start_ms": observation.start_ms,
                "end_ms": observation.end_ms,
                "ignored": bool(observation.ignored),
                "ignore_reason": observation.ignore_reason,
                "extractor_version": observation.extractor_version,
            }
        )
    )


def _fit_prefix(text: str, char_limit: int, token_limit: int) -> int:
    upper = min(len(text), char_limit)
    if estimate_tokens(text[:upper]) <= token_limit:
        return upper
    low, high = 1, upper
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= token_limit:
            low = middle
        else:
            high = middle - 1
    return max(1, low)


def _base_chunk_spans(
    text: str,
    *,
    target_chars: int = 600,
    hard_chars: int = 900,
    target_tokens: int = 450,
    hard_tokens: int = 700,
) -> list[tuple[int, int]]:
    """Return lossless non-overlapping spans around the configured targets."""

    if not text:
        return []
    pieces = [(match.start(), match.end()) for match in _SENTENCE_RE.finditer(text)]
    if not pieces:
        pieces = [(0, len(text))]
    spans: list[tuple[int, int]] = []
    current_start: int | None = None
    current_end = 0
    for piece_start, piece_end in pieces:
        cursor = piece_start
        while cursor < piece_end:
            if current_start is not None:
                candidate = text[current_start:piece_end]
                if len(candidate) <= target_chars and estimate_tokens(candidate) <= target_tokens:
                    current_end = piece_end
                    cursor = piece_end
                    continue
                spans.append((current_start, current_end))
                current_start = None
            remaining = text[cursor:piece_end]
            if len(remaining) <= hard_chars and estimate_tokens(remaining) <= hard_tokens:
                current_start, current_end = cursor, piece_end
                cursor = piece_end
                continue
            take = _fit_prefix(remaining, target_chars, target_tokens)
            spans.append((cursor, cursor + take))
            cursor += take
    if current_start is not None:
        spans.append((current_start, current_end))
    return spans


def chunk_text(
    text: str,
    *,
    previous_text: str = "",
    target_chars: int = 600,
    hard_chars: int = 900,
    target_tokens: int = 450,
    hard_tokens: int = 700,
    overlap_chars: int = 100,
) -> list[dict[str, Any]]:
    """Chunk one observation with bounded intra- and cross-observation context."""

    target_chars = max(1, int(target_chars))
    hard_chars = max(target_chars, int(hard_chars))
    target_tokens = max(1, int(target_tokens))
    hard_tokens = max(target_tokens, int(hard_tokens))
    overlap_limit = max(0, int(overlap_chars))

    chunks: list[dict[str, Any]] = []
    spans = _base_chunk_spans(
        text,
        target_chars=target_chars,
        hard_chars=hard_chars,
        target_tokens=target_tokens,
        hard_tokens=hard_tokens,
    )
    for ordinal, (base_start, end) in enumerate(spans):
        start = base_start
        prefix = ""
        cross_observation = ordinal == 0 and bool(previous_text)
        if cross_observation:
            payload_len = end - base_start
            overlap = min(overlap_limit, len(previous_text), max(0, payload_len // 5))
            prefix = previous_text[-overlap:] if overlap else ""
            while prefix and (
                len(prefix) + payload_len > hard_chars
                or estimate_tokens(prefix + text[base_start:end]) > hard_tokens
            ):
                prefix = prefix[1:]
        if ordinal:
            payload_len = end - base_start
            overlap = min(overlap_limit, base_start, max(0, payload_len // 5))
            start -= overlap
            while start < base_start and (
                end - start > hard_chars or estimate_tokens(text[start:end]) > hard_tokens
            ):
                start += 1
        overlap_count = len(prefix) if cross_observation else base_start - start
        value = prefix + text[start:end]
        chunks.append(
            {
                "text": value,
                "start_char": start,
                "end_char": end,
                "overlap_chars": overlap_count,
                "char_count": len(value),
                "token_count": estimate_tokens(value),
            }
        )
        if overlap_count * 5 > len(value):
            raise AssertionError("chunk overlap exceeded 20 percent")
    return chunks


class VectorScanTooLargeError(RuntimeError):
    """Full-scan vector search would exceed the configured row budget.

    Callers should degrade to FTS/deterministic channels instead of freezing
    the request path on a multi-GB sequential BLOB scan.

    Design decision: no ANN index (sqlite-vec / HNSW) is introduced.  This
    project keeps SQLite + numpy authoritative; the bounded scan plus the
    recall-layer candidate prefilter is the measured safety net, and an ANN
    extension would add a native dependency for a scale this single-account
    runtime has not demonstrated.  Raise ``vector_full_scan_row_limit`` only
    after benchmarking the concrete row count on the deployment host.

    Measured on a Windows dev host (2026-08, SQLite BLOB + numpy matmul):
    20,000 rows x 512-dim float32 ≈ 79.8 MB on disk (≈2x raw vector bytes);
    warm full-scan search ≈ 0.59-1.04 s (~19-34k rows/s) after the cache is
    loaded.  50,000 rows x 512-dim ≈ 199.3 MB, warm scan ≈ 1.29 s
    (~39k rows/s), and the row-budget guard rejects an over-limit scan in
    ~14 ms.  Extrapolating, a 100 GB raw-vector corpus (≈25M rows x 1024-dim)
    would take tens of minutes to full-scan, so production recall paths must
    go through FTS/candidate prefilter rather than raw full scans.
    """

    def __init__(self, *, rows: int, limit: int):
        self.rows = int(rows)
        self.limit = int(limit)
        super().__init__(
            f"vector scan rows {self.rows} exceed configured limit {self.limit}"
        )


class MemoryBrainStore:
    """Thread-safe short-connection store for exactly one account database."""

    def __init__(
        self,
        db_path: str | Path,
        account_id: str = "",
        *,
        chunk_target_chars: int = 600,
        chunk_hard_chars: int = 900,
        chunk_target_tokens: int = 450,
        chunk_hard_tokens: int = 700,
        chunk_overlap_chars: int = 100,
        job_max_attempts: int = 8,
        link_job_max_attempts: int = 3,
        vector_batch_size: int = 2048,
        vector_cache_limit: int = 50_000,
        vector_full_scan_row_limit: int = 50_000,
    ) -> None:
        self.db_path = Path(db_path)
        self.account_id = str(account_id)
        self._write_lock = threading.RLock()
        self._vector_cache_lock = threading.RLock()
        self._vector_cache: dict[tuple[str, str], _VectorCacheEntry] = {}
        # Total rows across ALL (model_id, target_type) cache entries so the
        # configured cache budget is a global memory bound, not a per-entry one.
        self._vector_cache_rows = 0
        self.configure_runtime(
            chunk_target_chars=chunk_target_chars,
            chunk_hard_chars=chunk_hard_chars,
            chunk_target_tokens=chunk_target_tokens,
            chunk_hard_tokens=chunk_hard_tokens,
            chunk_overlap_chars=chunk_overlap_chars,
            job_max_attempts=job_max_attempts,
            link_job_max_attempts=link_job_max_attempts,
            vector_batch_size=vector_batch_size,
            vector_cache_limit=vector_cache_limit,
            vector_full_scan_row_limit=vector_full_scan_row_limit,
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def configure_runtime(
        self,
        *,
        chunk_target_chars: int = 600,
        chunk_hard_chars: int = 900,
        chunk_target_tokens: int = 450,
        chunk_hard_tokens: int = 700,
        chunk_overlap_chars: int = 100,
        job_max_attempts: int = 8,
        link_job_max_attempts: int = 3,
        vector_batch_size: int = 2048,
        vector_cache_limit: int = 50_000,
        vector_full_scan_row_limit: int = 50_000,
    ) -> None:
        self.chunk_target_chars = max(1, int(chunk_target_chars))
        self.chunk_hard_chars = max(self.chunk_target_chars, int(chunk_hard_chars))
        self.chunk_target_tokens = max(1, int(chunk_target_tokens))
        self.chunk_hard_tokens = max(self.chunk_target_tokens, int(chunk_hard_tokens))
        self.chunk_overlap_chars = max(0, int(chunk_overlap_chars))
        self.job_max_attempts = max(1, int(job_max_attempts))
        self.link_job_max_attempts = max(
            1, min(self.job_max_attempts, int(link_job_max_attempts))
        )
        with self._vector_cache_lock:
            self.vector_batch_size = max(1, int(vector_batch_size))
            self.vector_cache_limit = max(0, int(vector_cache_limit))
            self.vector_full_scan_row_limit = max(0, int(vector_full_scan_row_limit))
            self._vector_cache.clear()
            self._vector_cache_rows = 0

    def _chunk_observation(self, text: str, previous_text: str = "") -> list[dict[str, Any]]:
        return chunk_text(
            text,
            previous_text=previous_text,
            target_chars=self.chunk_target_chars,
            hard_chars=self.chunk_hard_chars,
            target_tokens=self.chunk_target_tokens,
            hard_tokens=self.chunk_hard_tokens,
            overlap_chars=self.chunk_overlap_chars,
        )

    @classmethod
    def for_account(
        cls, data_root: str | Path, account_id: str, **kwargs: Any
    ) -> MemoryBrainStore:
        from .bootstrap import account_db_path

        return cls(account_db_path(data_root, account_id), account_id=account_id, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path), timeout=30, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA_SQL)
                _upgrade_schema(conn)
                now = time.time()
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                    (1, "v6_initial", now),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                    (2, "recall_trace_and_observation_metadata", now),
                )
                taxonomy = conn.execute(
                    "SELECT value FROM brain_info WHERE key='entity_type_taxonomy_version'"
                ).fetchone()
                if not taxonomy or str(taxonomy["value"] or "") != _ENTITY_TYPE_TAXONOMY_VERSION:
                    entity_rows = conn.execute(
                        "SELECT id,entity_type FROM memory_entities"
                    ).fetchall()
                    updates = []
                    for row in entity_rows:
                        normalized_type = normalize_entity_type(row["entity_type"])
                        if normalized_type != str(row["entity_type"] or ""):
                            updates.append((normalized_type, row["id"]))
                    if updates:
                        conn.executemany(
                            "UPDATE memory_entities SET entity_type=?,updated_at=? WHERE id=?",
                            [
                                (entity_type, now, entity_id)
                                for entity_type, entity_id in updates
                            ],
                        )
                    conn.execute(
                        "INSERT INTO brain_info(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        ("entity_type_taxonomy_version", _ENTITY_TYPE_TAXONOMY_VERSION, now),
                    )
                existing_account = conn.execute(
                    "SELECT value FROM brain_info WHERE key='account_id'"
                ).fetchone()
                if (
                    existing_account
                    and self.account_id
                    and existing_account["value"] != self.account_id
                ):
                    raise ValueError(
                        f"memory brain belongs to account {existing_account['value']!r}, "
                        f"not {self.account_id!r}"
                    )
                info = {
                    "schema_version": str(SCHEMA_VERSION),
                    "account_id": self.account_id or (existing_account["value"] if existing_account else ""),
                    "created_by": "memory_brain_v6",
                }
                for key, value in info.items():
                    if key == "schema_version":
                        conn.execute(
                            """INSERT INTO brain_info(key,value,updated_at) VALUES(?,?,?)
                               ON CONFLICT(key) DO UPDATE SET
                                 value=excluded.value,updated_at=excluded.updated_at""",
                            (key, value, now),
                        )
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO brain_info(key,value,updated_at) VALUES(?,?,?)",
                        (key, value, now),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO brain_info(key,value,updated_at) "
                    "VALUES('privacy_salt',?,?)",
                    (secrets.token_hex(32), now),
                )
                conn.commit()
                # Drain any WAL left behind by a previous unclean shutdown
                # before workers start writing. Otherwise the first writes can
                # trigger multi-second autocheckpoints and surface as
                # "database is locked" in the memory worker.
                try:
                    checkpoint_row = conn.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)"
                    ).fetchone()
                    if checkpoint_row and int(checkpoint_row[0]):
                        logger.warning(
                            "memory brain init: WAL TRUNCATE checkpoint busy: %s",
                            tuple(checkpoint_row),
                        )
                except sqlite3.OperationalError as exc:
                    logger.warning(
                        "memory brain init: WAL TRUNCATE checkpoint skipped: %s",
                        exc,
                    )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def archive_observation(self, envelope: ObservationEnvelope) -> ArchiveResult:
        if not isinstance(envelope, ObservationEnvelope):
            if isinstance(envelope, Mapping):
                envelope = ObservationEnvelope(**dict(envelope))
            else:
                raise TypeError("envelope must be an ObservationEnvelope or mapping")
        key = envelope.idempotency_key.strip()
        if not key:
            raise ValueError("idempotency_key is required")
        if envelope.account_id and envelope.account_id != self.account_id:
            raise ValueError("observation account_id does not match the bound memory store")
        sources = envelope.normalized_sources()
        if not sources:
            raise ValueError("at least one source is required")
        source_type = envelope.source_type.strip() or sources[0].source_type.strip()
        if not source_type:
            raise ValueError("source_type is required")
        if any(not source.source_type.strip() for source in sources):
            raise ValueError("every source must have a source_type")
        # Defense in depth: the redaction boundary lives in
        # MemoryBrainService.archive_private_message. Direct store callers must
        # not be able to persist unredacted PM bodies.
        if source_type == "private_message":
            meta = envelope.metadata if isinstance(envelope.metadata, Mapping) else {}
            if not bool(meta.get("redacted")):
                raise ValueError(
                    "private_message sources must be redacted before store archive"
                )
        digest = _envelope_hash(envelope, sources)
        now = time.time()

        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                tombstone = conn.execute(
                    "SELECT id FROM deletion_tombstones WHERE idempotency_key=?", (key,)
                ).fetchone()
                if tombstone:
                    raise ReingestBlockedError(
                        f"idempotency key {key!r} was explicitly deleted and cannot be re-ingested"
                    )
                existing = conn.execute(
                    "SELECT id,content_hash,index_status FROM memory_events WHERE idempotency_key=?", (key,)
                ).fetchone()
                if existing:
                    if existing["content_hash"] != digest:
                        raise IdempotencyConflictError(
                            f"idempotency key {key!r} already exists with different content"
                        )
                    result = self._archive_result(conn, existing["id"], digest, created=False)
                    # Replay repair: a "ready" event whose derived rows vanished
                    # must be rebuilt before we claim idempotent success.
                    if str(existing["index_status"] or "") == "ready":
                        chunk_count = conn.execute(
                            "SELECT COUNT(*) FROM memory_chunks WHERE event_id=?",
                            (existing["id"],),
                        ).fetchone()[0]
                        event_fts_count = conn.execute(
                            "SELECT COUNT(*) FROM memory_event_fts WHERE event_id=?",
                            (existing["id"],),
                        ).fetchone()[0]
                        observation_count = conn.execute(
                            "SELECT COUNT(*) FROM memory_observations WHERE event_id=?",
                            (existing["id"],),
                        ).fetchone()[0]
                        if event_fts_count == 0 or (chunk_count == 0 and observation_count > 0):
                            report = self._repair_event_derivations_tx(conn, existing["id"])
                            logger.warning(
                                "idempotent replay repaired missing derived rows "
                                "event=%s report=%s",
                                existing["id"],
                                report,
                            )
                        missing_jobs = self._missing_derivation_job_types_tx(
                            conn, existing["id"]
                        )
                        if missing_jobs:
                            requeued = self._requeue_event_jobs_tx(
                                conn, existing["id"], missing_jobs, now
                            )
                            logger.warning(
                                "idempotent replay detected missing enrichment rows "
                                "event=%s requeued_jobs=%s",
                                existing["id"],
                                requeued,
                            )
                    conn.commit()
                    return result

                event_id = _new_id("evt")
                source_id = _new_id("src")
                primary_source = sources[0]
                observations = primary_source.normalized_observations()
                source_text = primary_source.full_text or "\n".join(
                    item.text for item in observations
                )
                conn.execute(
                    """INSERT INTO memory_events(
                        id,idempotency_key,content_hash,event_type,source_type,title,summary,
                        speaker_actor_id,persona_id,scene,importance,occurred_at,metadata_json,
                        index_status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (
                        event_id,
                        key,
                        digest,
                        envelope.event_type,
                        source_type,
                        envelope.event_title,
                        envelope.event_summary,
                        envelope.speaker_actor_id,
                        envelope.persona_id,
                        envelope.scene,
                        max(0.0, min(1.0, float(envelope.importance))),
                        envelope.occurred_at,
                        _json_dumps(envelope.metadata),
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """INSERT INTO memory_sources(
                        id,event_id,ordinal,source_type,external_id,full_text,structured_json,
                        content_hash,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        source_id,
                        event_id,
                        0,
                        primary_source.source_type,
                        primary_source.external_id,
                        source_text,
                        _json_dumps(primary_source.data),
                        _source_row_hash(
                            ordinal=0,
                            source_type=primary_source.source_type,
                            external_id=primary_source.external_id,
                            full_text=source_text,
                            structured_data=primary_source.data,
                        ),
                        now,
                    ),
                )

                source_ids: list[str] = [source_id]
                observation_ids: list[str] = []
                chunk_ids: list[str] = []
                chunk_ordinal = 0
                fts_chunks: list[tuple[str, str, str]] = []
                previous_observation_text = ""
                for observation_ordinal, observation in enumerate(observations):
                    observation_id = _new_id("obs")
                    observation_ids.append(observation_id)
                    conn.execute(
                        """INSERT INTO memory_observations(
                            id,event_id,source_id,ordinal,modality,actor_id,external_id,text,
                            structured_json,content_hash,occurred_at,start_ms,end_ms,ignored,
                            ignore_reason,extractor_version,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            observation_id,
                            event_id,
                            source_id,
                            observation_ordinal,
                            observation.modality,
                            observation.actor_id,
                            observation.external_id,
                            observation.text,
                            _json_dumps(observation.data),
                            _observation_row_hash(
                                ordinal=observation_ordinal,
                                observation=observation,
                            ),
                            observation.occurred_at,
                            observation.start_ms,
                            observation.end_ms,
                            int(bool(observation.ignored)),
                            observation.ignore_reason,
                            observation.extractor_version,
                            now,
                        ),
                    )
                    for observation_chunk_ordinal, chunk in enumerate(
                        self._chunk_observation(observation.text, previous_observation_text)
                    ):
                        chunk_id = _new_id("chk")
                        chunk_ids.append(chunk_id)
                        conn.execute(
                            """INSERT INTO memory_chunks(
                                id,event_id,source_id,observation_id,ordinal,observation_ordinal,
                                text,content_hash,start_char,end_char,overlap_chars,char_count,
                                token_count,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                chunk_id,
                                event_id,
                                source_id,
                                observation_id,
                                chunk_ordinal,
                                observation_chunk_ordinal,
                                chunk["text"],
                                content_hash(chunk["text"]),
                                chunk["start_char"],
                                chunk["end_char"],
                                chunk["overlap_chars"],
                                chunk["char_count"],
                                chunk["token_count"],
                                now,
                            ),
                        )
                        fts_chunks.append(
                            (
                                chunk_id,
                                event_id,
                                build_fts_text(
                                    chunk["text"],
                                    (chunk_id, event_id, primary_source.external_id),
                                ),
                            )
                        )
                        chunk_ordinal += 1
                    previous_observation_text = observation.text

                additional_event_text: list[str] = []
                for source_ordinal, source in enumerate(sources[1:], start=1):
                    additional_source_id = _new_id("src")
                    source_ids.append(additional_source_id)
                    source_observations = source.normalized_observations()
                    full_text = source.full_text or "\n".join(
                        item.text for item in source_observations
                    )
                    additional_event_text.append(full_text)
                    conn.execute(
                        """INSERT INTO memory_sources(
                            id,event_id,ordinal,source_type,external_id,full_text,structured_json,
                            content_hash,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            additional_source_id,
                            event_id,
                            source_ordinal,
                            source.source_type,
                            source.external_id,
                            full_text,
                            _json_dumps(source.data),
                            _source_row_hash(
                                ordinal=source_ordinal,
                                source_type=source.source_type,
                                external_id=source.external_id,
                                full_text=full_text,
                                structured_data=source.data,
                            ),
                            now,
                        ),
                    )
                    previous_observation_text = ""
                    for observation in source_observations:
                        observation_id = _new_id("obs")
                        observation_ids.append(observation_id)
                        observation_ordinal = len(observation_ids) - 1
                        additional_event_text.append(observation.text)
                        conn.execute(
                            """INSERT INTO memory_observations(
                                id,event_id,source_id,ordinal,modality,actor_id,external_id,text,
                                structured_json,content_hash,occurred_at,start_ms,end_ms,ignored,
                                ignore_reason,extractor_version,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                observation_id,
                                event_id,
                                additional_source_id,
                                observation_ordinal,
                                observation.modality,
                                observation.actor_id,
                                observation.external_id,
                                observation.text,
                                _json_dumps(observation.data),
                                _observation_row_hash(
                                    ordinal=observation_ordinal,
                                    observation=observation,
                                ),
                                observation.occurred_at,
                                observation.start_ms,
                                observation.end_ms,
                                int(bool(observation.ignored)),
                                observation.ignore_reason,
                                observation.extractor_version,
                                now,
                            ),
                        )
                        for observation_chunk_ordinal, chunk in enumerate(
                            self._chunk_observation(observation.text, previous_observation_text)
                        ):
                            chunk_id = _new_id("chk")
                            chunk_ids.append(chunk_id)
                            conn.execute(
                                """INSERT INTO memory_chunks(
                                    id,event_id,source_id,observation_id,ordinal,observation_ordinal,
                                    text,content_hash,start_char,end_char,overlap_chars,char_count,
                                    token_count,created_at
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    chunk_id,
                                    event_id,
                                    additional_source_id,
                                    observation_id,
                                    chunk_ordinal,
                                    observation_chunk_ordinal,
                                    chunk["text"],
                                    content_hash(chunk["text"]),
                                    chunk["start_char"],
                                    chunk["end_char"],
                                    chunk["overlap_chars"],
                                    chunk["char_count"],
                                    chunk["token_count"],
                                    now,
                                ),
                            )
                            fts_chunks.append(
                                (
                                    chunk_id,
                                    event_id,
                                    build_fts_text(
                                        chunk["text"],
                                        (chunk_id, event_id, source.external_id),
                                    ),
                                )
                            )
                            chunk_ordinal += 1
                        previous_observation_text = observation.text

                event_text = "\n".join(
                    value
                    for value in (
                        envelope.event_title,
                        envelope.event_summary,
                        source_text,
                        *(item.text for item in observations),
                        *additional_event_text,
                    )
                    if value
                )
                conn.execute(
                    "INSERT INTO memory_event_fts(event_id,search_text) VALUES(?,?)",
                    (
                        event_id,
                        build_fts_text(
                            event_text,
                            (event_id, key, *(source.external_id for source in sources)),
                        ),
                    ),
                )
                conn.executemany(
                    "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) VALUES(?,?,?)",
                    fts_chunks,
                )

                job_ids: list[str] = []
                seen_jobs: set[str] = set()
                for job_type in envelope.job_types:
                    job_type = str(job_type).strip()
                    if not job_type or job_type in seen_jobs:
                        continue
                    if job_type not in _JOB_TYPES:
                        raise ValueError(f"unsupported memory job type: {job_type}")
                    seen_jobs.add(job_type)
                    job_id = _new_id("job")
                    job_ids.append(job_id)
                    conn.execute(
                        """INSERT INTO brain_jobs(
                            id,dedupe_key,job_type,event_id,payload_json,status,attempts,
                            max_attempts,available_at,created_at,updated_at
                        ) VALUES(?,?,?,?,?,'pending',0,?,?,?,?)""",
                        (
                            job_id,
                            f"{event_id}:{job_type}",
                            job_type,
                            event_id,
                            _json_dumps({"event_id": event_id}),
                            (
                                self.link_job_max_attempts
                                if job_type == "link_associations"
                                else self.job_max_attempts
                            ),
                            now,
                            now,
                            now,
                        ),
                    )
                if not job_ids:
                    # Raw source, observations, chunks and FTS are committed in
                    # this transaction. Events with no derived work are ready
                    # immediately instead of remaining permanently "pending".
                    conn.execute(
                        "UPDATE memory_events SET index_status='ready',updated_at=? WHERE id=?",
                        (now, event_id),
                    )
                conn.commit()
                return ArchiveResult(
                    event_id=event_id,
                    source_id=source_id,
                    source_ids=tuple(source_ids),
                    observation_ids=tuple(observation_ids),
                    chunk_ids=tuple(chunk_ids),
                    job_ids=tuple(job_ids),
                    content_hash=digest,
                    created=True,
                    source_committed=True,
                    fts_status="ready",
                )
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _archive_result(
        conn: sqlite3.Connection, event_id: str, digest: str, *, created: bool
    ) -> ArchiveResult:
        sources = conn.execute(
            "SELECT id FROM memory_sources WHERE event_id=? ORDER BY ordinal",
            (event_id,),
        ).fetchall()
        observations = conn.execute(
            "SELECT id FROM memory_observations WHERE event_id=? ORDER BY ordinal", (event_id,)
        ).fetchall()
        chunks = conn.execute(
            "SELECT id FROM memory_chunks WHERE event_id=? ORDER BY ordinal", (event_id,)
        ).fetchall()
        jobs = conn.execute(
            "SELECT id FROM brain_jobs WHERE event_id=? ORDER BY created_at,id", (event_id,)
        ).fetchall()
        return ArchiveResult(
            event_id=event_id,
            source_id=sources[0]["id"] if sources else "",
            source_ids=tuple(row["id"] for row in sources),
            observation_ids=tuple(row["id"] for row in observations),
            chunk_ids=tuple(row["id"] for row in chunks),
            job_ids=tuple(row["id"] for row in jobs),
            content_hash=digest,
            created=created,
            source_committed=True,
            fts_status="ready",
        )

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = _json_loads(item.pop("metadata_json", "{}"), {})
        item.setdefault("content", item.get("summary") or item.get("title") or "")
        item.setdefault("source", item.get("source_type", ""))
        item.setdefault("status", item.get("index_status", "pending"))
        return item

    def list_events(
        self,
        limit: int = 50,
        offset: int = 0,
        source_type: str | None = None,
        status: str | None = None,
        exclude_intents: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("source_type=?")
            params.append(source_type)
        if status:
            clauses.append("index_status=?")
            params.append(status)
        if exclude_intents:
            clauses.append(
                "coalesce(json_extract(metadata_json,'$.action_state'),'')!='intent'"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((max(1, min(int(limit), 500)), max(0, int(offset))))
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM memory_events {where} ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [self._event_row(row) for row in rows]
        finally:
            conn.close()

    def count_events(
        self,
        *,
        source_type: str | None = None,
        status: str | None = None,
        exclude_intents: bool = False,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if source_type:
            clauses.append("source_type=?")
            params.append(source_type)
        if status:
            clauses.append("index_status=?")
            params.append(status)
        if exclude_intents:
            clauses.append(
                "coalesce(json_extract(metadata_json,'$.action_state'),'')!='intent'"
            )
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    f"SELECT count(*) FROM memory_events {where}", params
                ).fetchone()[0]
                or 0
            )
        finally:
            conn.close()

    def get_event(
        self, event_id: str, chunks_per_event: int | None = None
    ) -> dict[str, Any] | None:
        items = self.get_events([event_id], chunks_per_event=chunks_per_event)
        return items[0] if items else None

    def get_events(
        self, event_ids: Sequence[str], chunks_per_event: int | None = 2
    ) -> list[dict[str, Any]]:
        ordered_ids = list(dict.fromkeys(str(item) for item in event_ids if item))
        if not ordered_ids:
            return []
        placeholders = ",".join("?" for _ in ordered_ids)
        conn = self._connect()
        try:
            events = {
                row["id"]: self._event_row(row)
                for row in conn.execute(
                    f"SELECT * FROM memory_events WHERE id IN ({placeholders})", ordered_ids
                ).fetchall()
            }
            source_rows = conn.execute(
                f"SELECT * FROM memory_sources WHERE event_id IN ({placeholders}) ORDER BY event_id,ordinal",
                ordered_ids,
            ).fetchall()
            observation_rows = conn.execute(
                f"SELECT * FROM memory_observations WHERE event_id IN ({placeholders}) "
                "ORDER BY event_id,ordinal",
                ordered_ids,
            ).fetchall()
            # Attach source_type so recall can prefer audiovisual evidence over
            # raw metadata JSON / web_reference blobs.
            chunk_rows = conn.execute(
                f"""SELECT c.*, s.source_type AS source_type
                    FROM memory_chunks c
                    LEFT JOIN memory_sources s ON s.id = c.source_id
                    WHERE c.event_id IN ({placeholders})
                    ORDER BY c.event_id, c.ordinal""",
                ordered_ids,
            ).fetchall()
            mention_rows = conn.execute(
                f"""SELECT m.event_id,m.surface_text,m.confidence,e.id AS entity_id,
                           e.canonical_name,e.entity_type
                    FROM memory_entity_mentions m JOIN memory_entities e ON e.id=m.entity_id
                    WHERE m.event_id IN ({placeholders})
                    ORDER BY m.event_id,e.canonical_name""",
                ordered_ids,
            ).fetchall()
            link_rows = conn.execute(
                f"""SELECT * FROM memory_links
                    WHERE source_event_id IN ({placeholders}) OR target_event_id IN ({placeholders})
                    ORDER BY weight DESC,created_at DESC""",
                [*ordered_ids, *ordered_ids],
            ).fetchall()

            for event in events.values():
                event["sources"] = []
                event["observations"] = []
                event["chunks"] = []
                event["entities"] = []
                event["links"] = []
            for row in source_rows:
                item = dict(row)
                item["structured_data"] = _json_loads(item.pop("structured_json"), {})
                events[row["event_id"]]["sources"].append(item)
            for row in observation_rows:
                item = dict(row)
                item["structured_data"] = _json_loads(item.pop("structured_json"), {})
                events[row["event_id"]]["observations"].append(item)
            chunk_counts: dict[str, int] = {}
            for row in chunk_rows:
                event_id = row["event_id"]
                count = chunk_counts.get(event_id, 0)
                if chunks_per_event is None or count < max(0, int(chunks_per_event)):
                    events[event_id]["chunks"].append(dict(row))
                chunk_counts[event_id] = count + 1
            for row in mention_rows:
                events[row["event_id"]]["entities"].append(dict(row))
            for row in link_rows:
                item = dict(row)
                item["evidence_ids"] = _json_loads(item.pop("evidence_json"), [])
                for event_id in ordered_ids:
                    if event_id in events and event_id in (
                        row["source_event_id"],
                        row["target_event_id"],
                    ):
                        events[event_id]["links"].append(item.copy())
            return [events[event_id] for event_id in ordered_ids if event_id in events]
        finally:
            conn.close()

    def list_open_activity_intents(self, limit: int = 500) -> list[dict[str, Any]]:
        """Durable begin_activity intents that have no terminal outcome yet.

        Used at boot to close intents orphaned by a previous process crash.
        """
        conn = self._connect()
        try:
            intent_rows = conn.execute(
                "SELECT id,event_type,title,summary,scene,occurred_at,metadata_json "
                "FROM memory_events WHERE event_type='action_intent' "
                "ORDER BY occurred_at ASC LIMIT ?",
                (max(1, min(int(limit), 2000)),),
            ).fetchall()
            if not intent_rows:
                return []
            intent_keys = {
                str(
                    _json_loads(row["metadata_json"], {})
                    .get("activity_key")
                    or _json_loads(row["metadata_json"], {})
                    .get("action_key")
                    or ""
                ).strip()
                for row in intent_rows
            }
            intent_keys.discard("")
            terminal_keys = set()
            if intent_keys:
                # Resolve terminal outcomes for exactly these intent keys, not
                # a global newest-N window: with >5000 terminal events an old
                # completed intent would otherwise be misclassified as open.
                placeholders = ",".join("?" for _ in intent_keys)
                params = list(intent_keys) + list(intent_keys)
                try:
                    terminal_rows = conn.execute(
                        f"""SELECT metadata_json FROM memory_events
                            WHERE event_type IN ('action_outcome','bot_action')
                              AND (
                                json_extract(metadata_json,'$.activity_key') IN ({placeholders})
                                OR json_extract(metadata_json,'$.action_key') IN ({placeholders})
                              )
                            LIMIT 10000""",
                        params,
                    ).fetchall()
                except sqlite3.OperationalError:
                    # JSON1 unavailable: bounded full scan fallback (still keyed
                    # by the requested intents after extraction).
                    terminal_rows = conn.execute(
                        "SELECT metadata_json FROM memory_events "
                        "WHERE event_type IN ('action_outcome','bot_action') "
                        "ORDER BY occurred_at DESC LIMIT 20000"
                    ).fetchall()
                for row in terminal_rows:
                    meta = _json_loads(row["metadata_json"], {})
                    key = str(meta.get("activity_key") or meta.get("action_key") or "").strip()
                    if key and key in intent_keys:
                        terminal_keys.add(key)
            result = []
            for row in intent_rows:
                meta = _json_loads(row["metadata_json"], {})
                key = str(meta.get("activity_key") or meta.get("action_key") or "").strip()
                if not key or key in terminal_keys:
                    continue
                result.append(
                    {
                        "event_id": row["id"],
                        "action_key": key,
                        "action_type": str(meta.get("action_type") or ""),
                        "scene": str(row["scene"] or ""),
                        "title": str(row["title"] or ""),
                        "summary": str(row["summary"] or ""),
                        "occurred_at": float(row["occurred_at"] or 0.0),
                    }
                )
            return result
        finally:
            conn.close()

    def stats(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            now = time.time()
            table_counts = {}
            for table in (
                "memory_events",
                "memory_sources",
                "memory_observations",
                "memory_chunks",
                "memory_embeddings",
                "memory_entities",
                "memory_links",
                "deletion_tombstones",
            ):
                table_counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            job_counts = {
                row["status"]: row["count"]
                for row in conn.execute(
                    "SELECT status,count(*) AS count FROM brain_jobs GROUP BY status"
                ).fetchall()
            }
            source_counts = {
                row["source_type"]: row["count"]
                for row in conn.execute(
                    "SELECT source_type,count(*) AS count FROM memory_events GROUP BY source_type"
                ).fetchall()
            }
            event_type_counts = {
                row["event_type"]: row["count"]
                for row in conn.execute(
                    "SELECT event_type,count(*) AS count FROM memory_events GROUP BY event_type"
                ).fetchall()
            }
            index_status_counts = {
                row["index_status"]: row["count"]
                for row in conn.execute(
                    "SELECT index_status,count(*) AS count FROM memory_events GROUP BY index_status"
                ).fetchall()
            }
            newest_event_at = float(
                conn.execute("SELECT coalesce(max(created_at),0) FROM memory_events").fetchone()[0]
                or 0.0
            )
            events_last_30m = int(
                conn.execute(
                    "SELECT count(*) FROM memory_events WHERE created_at>=?", (now - 1800.0,)
                ).fetchone()[0]
                or 0
            )
            active_job_row = conn.execute(
                """SELECT min(created_at) AS oldest_created_at,
                          min(available_at) AS oldest_available_at
                   FROM brain_jobs
                   WHERE status IN ('pending','processing','retry','blocked')"""
            ).fetchone()
            recall_rows = conn.execute(
                """SELECT used_fallback,rerank_status,channel_errors_json,
                          latency_ms,prompt_chars
                   FROM recall_traces WHERE created_at>=?
                   ORDER BY created_at""",
                (now - 1800.0,),
            ).fetchall()
            job_detail_rows = conn.execute(
                """SELECT job_type,status,last_error,created_at,updated_at
                   FROM brain_jobs"""
            ).fetchall()
            canonical_rows = conn.execute(
                """SELECT e.id,e.event_type,e.created_at,e.metadata_json,s.full_text
                   FROM memory_events e
                   LEFT JOIN memory_sources s ON s.event_id=e.id AND s.ordinal=0
                   WHERE e.event_type IN ('daily_plan','life_detail','diary','dream')
                   ORDER BY e.created_at DESC"""
            ).fetchall()

            def _median(values: Sequence[float]) -> float:
                ordered = sorted(float(value or 0.0) for value in values)
                count = len(ordered)
                if not count:
                    return 0.0
                middle = count // 2
                if count % 2:
                    return ordered[middle]
                return (ordered[middle - 1] + ordered[middle]) / 2.0

            def _percentile(values: Sequence[float], percentile: float) -> float:
                ordered = sorted(float(value or 0.0) for value in values)
                if not ordered:
                    return 0.0
                rank = max(0, math.ceil((len(ordered) * percentile) - 1))
                return ordered[min(rank, len(ordered) - 1)]

            def _error_kind(value: Any) -> str:
                text = str(value or "").strip()
                if not text:
                    return "none"
                head = text.split(":", 1)[0].strip()
                return head[:80] or "unknown"

            fallback_count = sum(int(row["used_fallback"] or 0) for row in recall_rows)
            recall_latencies = [float(row["latency_ms"] or 0.0) for row in recall_rows]
            timeout_count = 0
            error_count = 0
            rerank_statuses: dict[str, int] = {}
            for row in recall_rows:
                rerank_status = str(row["rerank_status"] or "unknown")
                rerank_statuses[rerank_status] = rerank_statuses.get(rerank_status, 0) + 1
                channel_errors = str(row["channel_errors_json"] or "{}")
                if "timeout" in rerank_status.casefold() or "timeout" in channel_errors.casefold():
                    timeout_count += 1
                if (
                    rerank_status.startswith("error:")
                    or channel_errors not in {"", "{}", "null"}
                ):
                    error_count += 1
            active_jobs_by_type: dict[str, int] = {}
            dead_jobs_by_type: dict[str, int] = {}
            failures_by_type: dict[str, dict[str, int]] = {}
            for row in job_detail_rows:
                job_type = str(row["job_type"] or "unknown")
                status = str(row["status"] or "unknown")
                if status in {"pending", "processing", "retry", "blocked"}:
                    active_jobs_by_type[job_type] = active_jobs_by_type.get(job_type, 0) + 1
                if status == "dead":
                    dead_jobs_by_type[job_type] = dead_jobs_by_type.get(job_type, 0) + 1
                if status in {"retry", "blocked", "dead"}:
                    kind = _error_kind(row["last_error"])
                    bucket = failures_by_type.setdefault(job_type, {})
                    bucket[kind] = bucket.get(kind, 0) + 1
            oldest_created_at = float(active_job_row["oldest_created_at"] or 0.0)
            latest_canonical: dict[str, dict[str, Any]] = {}
            for row in canonical_rows:
                kind = str(row["event_type"] or "")
                if not kind or kind in latest_canonical:
                    continue
                try:
                    metadata = json.loads(row["metadata_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                body = str(row["full_text"] or "")
                expected_hash = str(metadata.get("canonical_sha256") or "")
                expected_chars_raw = metadata.get("canonical_chars")
                try:
                    expected_chars = int(expected_chars_raw)
                except (TypeError, ValueError):
                    expected_chars = None
                actual_hash = hashlib.sha256(
                    body.encode("utf-8", errors="ignore")
                ).hexdigest()
                metadata_present = bool(expected_hash) and expected_chars is not None
                verified = bool(
                    metadata_present
                    and expected_hash == actual_hash
                    and expected_chars == len(body)
                )
                latest_canonical[kind] = {
                    "event_id": str(row["id"] or ""),
                    "created_at": float(row["created_at"] or 0.0),
                    "metadata_present": metadata_present,
                    "verified": verified,
                    "expected_chars": expected_chars,
                    "actual_chars": len(body),
                }
            canonical_checked = len(latest_canonical)
            canonical_verified = sum(
                1 for item in latest_canonical.values() if item["verified"]
            )
            canonical_missing_metadata = sum(
                1 for item in latest_canonical.values() if not item["metadata_present"]
            )
            return {
                "account_id": self.account_id,
                "db_path": str(self.db_path),
                "counts": table_counts,
                "jobs": job_counts,
                "sources": source_counts,
                "event_types": event_type_counts,
                "index_statuses": index_status_counts,
                "schema_version": SCHEMA_VERSION,
                "operations": {
                    "events_last_30m": events_last_30m,
                    "newest_event_at": newest_event_at,
                    "event_freshness_seconds": (
                        max(0.0, now - newest_event_at) if newest_event_at else None
                    ),
                    "active_jobs": sum(
                        int(job_counts.get(status) or 0)
                        for status in ("pending", "processing", "retry", "blocked")
                    ),
                    "active_jobs_by_type": active_jobs_by_type,
                    "dead_jobs_by_type": dead_jobs_by_type,
                    "job_failures_by_type": failures_by_type,
                    "oldest_active_job_at": oldest_created_at or None,
                    "oldest_active_job_age_seconds": (
                        max(0.0, now - oldest_created_at) if oldest_created_at else None
                    ),
                    "recall_last_30m": {
                        "count": len(recall_rows),
                        "fallback_count": fallback_count,
                        "fallback_rate": (
                            fallback_count / len(recall_rows) if recall_rows else 0.0
                        ),
                        "latency_ms_p50": _median(
                            recall_latencies
                        ),
                        "latency_ms_p95": _percentile(recall_latencies, 0.95),
                        "latency_ms_max": max(recall_latencies, default=0.0),
                        "timeout_count": timeout_count,
                        "timeout_rate": (
                            timeout_count / len(recall_rows) if recall_rows else 0.0
                        ),
                        "error_count": error_count,
                        "rerank_statuses": rerank_statuses,
                        "prompt_chars_p50": int(
                            round(
                                _median(
                                    [float(row["prompt_chars"] or 0.0) for row in recall_rows]
                                )
                            )
                        ),
                    },
                    "canonical_documents": {
                        "checked": canonical_checked,
                        "verified": canonical_verified,
                        "mismatch": canonical_checked - canonical_verified,
                        "missing_metadata": canonical_missing_metadata,
                        "latest": latest_canonical,
                    },
                },
            }
        finally:
            conn.close()

    @staticmethod
    def _fts_query(query: str) -> str:
        return " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"'
            for term in _fts_query_terms(query)
        )

    @staticmethod
    def _fts_rows_with_coverage(
        rows: Sequence[sqlite3.Row], query: str
    ) -> list[dict[str, Any]]:
        query_terms = _fts_query_terms(query)
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            search_text = str(item.pop("_fts_search_text", "") or "")
            coverage, matched, eligible = _fts_coverage(search_text, query_terms)
            item["lexical_coverage"] = coverage
            item["lexical_matched_terms"] = matched
            item["lexical_query_terms"] = eligible
            result.append(item)
        return result

    def search_events_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        match = self._fts_query(query)
        if not match:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT f.event_id,bm25(memory_event_fts) AS bm25,e.title,e.summary,
                          f.search_text AS _fts_search_text,
                          e.source_type,e.created_at
                   FROM memory_event_fts f JOIN memory_events e ON e.id=f.event_id
                   WHERE memory_event_fts MATCH ? ORDER BY bm25(memory_event_fts),e.created_at DESC
                   LIMIT ?""",
                (match, max(1, min(int(limit), 200))),
            ).fetchall()
            return self._fts_rows_with_coverage(rows, query)
        finally:
            conn.close()

    def search_chunks_fts(self, query: str, limit: int = 40) -> list[dict[str, Any]]:
        match = self._fts_query(query)
        if not match:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT f.chunk_id,f.event_id,bm25(memory_chunk_fts) AS bm25,c.text,
                          f.search_text AS _fts_search_text,
                          c.observation_id,c.ordinal,s.source_type AS source_type
                   FROM memory_chunk_fts f
                   JOIN memory_chunks c ON c.id=f.chunk_id
                   LEFT JOIN memory_sources s ON s.id=c.source_id
                   WHERE memory_chunk_fts MATCH ? ORDER BY bm25(memory_chunk_fts),c.ordinal
                   LIMIT ?""",
                (match, max(1, min(int(limit), 500))),
            ).fetchall()
            return self._fts_rows_with_coverage(rows, query)
        finally:
            conn.close()

    def find_events_by_identifiers(
        self, identifiers: Sequence[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        values = list(dict.fromkeys(str(value).strip() for value in identifiers if str(value).strip()))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        stable_metadata_keys = (
            "bvid",
            "oid",
            "reply_id",
            "rpid",
            "season_id",
            "episode_id",
            "session_id",
            "draft_id",
            "audit_id",
            "task_id",
            "message_id",
        )
        key_placeholders = ",".join("?" for _ in stable_metadata_keys)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""SELECT DISTINCT e.* FROM memory_events e
                    LEFT JOIN memory_sources s ON s.event_id=e.id
                    LEFT JOIN memory_entities n ON n.normalized_name IN ({placeholders})
                    LEFT JOIN memory_entity_mentions m ON m.entity_id=n.id AND m.event_id=e.id
                    WHERE e.id IN ({placeholders}) OR e.idempotency_key IN ({placeholders})
                       OR s.external_id IN ({placeholders}) OR e.title IN ({placeholders})
                       OR m.event_id IS NOT NULL
                       OR EXISTS (
                           SELECT 1 FROM json_each(e.metadata_json) AS stable_id
                           WHERE stable_id.key IN ({key_placeholders})
                             AND CAST(stable_id.value AS TEXT) IN ({placeholders})
                       )
                    ORDER BY e.created_at DESC LIMIT ?""",
                [
                    *values,
                    *values,
                    *values,
                    *values,
                    *values,
                    *stable_metadata_keys,
                    *values,
                    max(1, min(int(limit), 200)),
                ],
            ).fetchall()
            return [self._event_row(row) for row in rows]
        finally:
            conn.close()

    def recent_events(
        self, limit: int = 20, speaker_actor_id: str | None = None
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if speaker_actor_id is None:
                rows = conn.execute(
                    "SELECT * FROM memory_events ORDER BY created_at DESC,id LIMIT ?",
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memory_events WHERE speaker_actor_id=? "
                    "ORDER BY created_at DESC,id LIMIT ?",
                    (speaker_actor_id, max(1, min(int(limit), 200))),
                ).fetchall()
            return [self._event_row(row) for row in rows]
        finally:
            conn.close()

    def register_embedding_model(
        self,
        provider: str,
        model: str,
        dimension: int,
        config_hash: str = "",
    ) -> str:
        if int(dimension) <= 0:
            raise ValueError("embedding dimension must be positive")
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT id FROM embedding_models WHERE provider=? AND model=? "
                    "AND dimension=? AND config_hash=?",
                    (provider, model, int(dimension), config_hash),
                ).fetchone()
                if row:
                    conn.commit()
                    return row["id"]
                model_id = _new_id("embm")
                conn.execute(
                    "INSERT INTO embedding_models(id,provider,model,dimension,config_hash,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (model_id, provider, model, int(dimension), config_hash, now),
                )
                conn.commit()
                return model_id
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def upsert_embedding(
        self,
        target_type: str,
        target_id: str,
        vector: Sequence[float],
        *,
        provider: str,
        model: str,
        content_digest: str | None = None,
        config_hash: str = "",
    ) -> str:
        if target_type not in {"event", "chunk"}:
            raise ValueError("target_type must be 'event' or 'chunk'")
        blob, dimension = encode_vector(vector)
        # Pre-validate the target before registering a model row so a bad
        # target_id cannot leave an orphaned embedding_models entry.
        with self._write_lock:
            conn = self._connect()
            try:
                table = "memory_events" if target_type == "event" else "memory_chunks"
                target_exists = conn.execute(
                    f"SELECT 1 FROM {table} WHERE id=?", (target_id,)
                ).fetchone()
            finally:
                conn.close()
        if not target_exists:
            raise KeyError(f"unknown {target_type} target: {target_id}")
        model_id = self.register_embedding_model(provider, model, dimension, config_hash)
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if target_type == "event":
                    target = conn.execute(
                        "SELECT id,content_hash FROM memory_events WHERE id=?", (target_id,)
                    ).fetchone()
                    event_id = target_id
                else:
                    target = conn.execute(
                        "SELECT id,event_id,content_hash FROM memory_chunks WHERE id=?", (target_id,)
                    ).fetchone()
                    event_id = target["event_id"] if target else ""
                if not target:
                    raise KeyError(f"unknown {target_type} target: {target_id}")
                digest = content_digest or target["content_hash"]
                existing = conn.execute(
                    "SELECT id FROM memory_embeddings WHERE target_type=? AND target_id=? AND model_id=?",
                    (target_type, target_id, model_id),
                ).fetchone()
                embedding_id = existing["id"] if existing else _new_id("emb")
                conn.execute(
                    """INSERT INTO memory_embeddings(
                        id,target_type,target_id,event_id,model_id,dimension,vector_blob,
                        content_hash,norm,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,1.0,?,?)
                    ON CONFLICT(target_type,target_id,model_id) DO UPDATE SET
                        dimension=excluded.dimension,vector_blob=excluded.vector_blob,
                        content_hash=excluded.content_hash,norm=1.0,updated_at=excluded.updated_at""",
                    (
                        embedding_id,
                        target_type,
                        target_id,
                        event_id,
                        model_id,
                        dimension,
                        sqlite3.Binary(blob),
                        digest,
                        now,
                        now,
                    ),
                )
                conn.commit()
                self._invalidate_vector_cache(model_id=model_id, target_type=target_type)
                return embedding_id
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def _invalidate_vector_cache(
        self,
        *,
        model_id: str | None = None,
        target_type: str | None = None,
    ) -> None:
        with self._vector_cache_lock:
            if model_id is not None and target_type is not None:
                entry = self._vector_cache.pop((model_id, target_type), None)
                if entry is not None:
                    self._vector_cache_rows = max(
                        0,
                        self._vector_cache_rows
                        - int(getattr(entry.matrix, "shape", (0,))[0] or 0),
                    )
            else:
                self._vector_cache.clear()
                self._vector_cache_rows = 0

    @staticmethod
    def _vector_cache_entry_rows(entry: _VectorCacheEntry) -> int:
        try:
            return int(entry.matrix.shape[0])
        except Exception:
            return 0

    def _cache_entry_with_global_budget(
        self,
        cache_key: tuple[str, str],
        entry: _VectorCacheEntry,
    ) -> bool:
        """Insert cache entry while keeping the TOTAL cached rows ≤ limit."""
        rows = self._vector_cache_entry_rows(entry)
        if rows <= 0 or rows > self.vector_cache_limit:
            return False
        # Evict other entries (insertion-ordered ≈ least recently hit, because
        # hits re-insert their key at the end) until the new entry fits.
        while self._vector_cache_rows + rows > self.vector_cache_limit:
            victim = next(
                (key for key in self._vector_cache if key != cache_key), None
            )
            if victim is None:
                return False
            removed = self._vector_cache.pop(victim)
            self._vector_cache_rows = max(
                0, self._vector_cache_rows - self._vector_cache_entry_rows(removed)
            )
        self._vector_cache[cache_key] = entry
        self._vector_cache_rows += rows
        return True

    def _build_vector_cache_entry(
        self,
        conn: sqlite3.Connection,
        *,
        model_id: str,
        target_type: str,
        dimension: int,
        expected_count: int,
        np: Any,
    ) -> _VectorCacheEntry | None:
        if (
            self.vector_cache_limit <= 0
            or expected_count <= 0
            or expected_count > self.vector_cache_limit
        ):
            return None

        rows = conn.execute(
            """SELECT id,target_id,event_id,vector_blob FROM memory_embeddings
               WHERE model_id=? AND target_type=? ORDER BY id LIMIT ?""",
            (model_id, target_type, self.vector_cache_limit + 1),
        ).fetchall()
        if len(rows) > self.vector_cache_limit:
            return None

        little_float32 = np.dtype("<f4")
        matrix = np.empty((len(rows), dimension), dtype=little_float32)
        metadata: list[tuple[str, str, str]] = []
        expected_bytes = dimension * little_float32.itemsize
        valid_count = 0
        for row in rows:
            blob = row["vector_blob"]
            if len(blob) != expected_bytes:
                continue
            values = np.frombuffer(blob, dtype=little_float32, count=dimension)
            matrix[valid_count] = values
            metadata.append((row["id"], row["target_id"], row["event_id"]))
            valid_count += 1

        matrix = matrix[:valid_count]
        if valid_count:
            finite = np.isfinite(matrix).all(axis=1)
            if not bool(finite.all()):
                valid_indexes = np.flatnonzero(finite).tolist()
                matrix = np.ascontiguousarray(matrix[finite], dtype=little_float32)
                metadata = [metadata[index] for index in valid_indexes]
        matrix.setflags(write=False)
        meta_tuple = tuple(metadata)
        if meta_tuple:
            embedding_ids = np.array([row[0] for row in meta_tuple], dtype=object)
            target_ids = np.array([row[1] for row in meta_tuple], dtype=object)
            event_ids = np.array([row[2] for row in meta_tuple], dtype=object)
        else:
            embedding_ids = target_ids = event_ids = None
        return _VectorCacheEntry(
            dimension=dimension,
            matrix=matrix,
            metadata=meta_tuple,
            embedding_ids=embedding_ids,
            target_ids=target_ids,
            event_ids=event_ids,
        )

    @staticmethod
    def _rank_cached_vectors(
        entry: _VectorCacheEntry,
        query: Sequence[float],
        *,
        target_type: str,
        model_id: str,
        result_limit: int,
        batch_size: int,
        np: Any,
    ) -> list[dict[str, Any]]:
        # Full matvec + top-k partition: avoid per-row Python dicts and mid-scan sorts.
        # batch_size kept for API compatibility with callers; unused on the hot path.
        _ = batch_size
        if not entry.metadata:
            return []
        dtype = np.dtype("<f4")
        qbuf = entry.query_template
        if qbuf is None or int(qbuf.shape[0]) != int(entry.dimension):
            qbuf = np.empty(int(entry.dimension), dtype=dtype)
            entry.query_template = qbuf
        # Fill without allocating a new array when query is a plain list.
        if isinstance(query, np.ndarray) and query.dtype == dtype and query.flags["C_CONTIGUOUS"]:
            query_array = query
        else:
            qbuf[:] = query
            query_array = qbuf
        scores = entry.matrix @ query_array
        n = int(scores.shape[0])
        k = min(int(result_limit), n)
        if k <= 0:
            return []
        if k < n:
            candidate_idx = np.argpartition(scores, n - k)[n - k :]
        else:
            candidate_idx = np.arange(n)
        cand_scores = scores[candidate_idx]
        target_ids = entry.target_ids
        if target_ids is not None:
            cand_target_ids = target_ids[candidate_idx]
        else:
            cand_target_ids = np.array(
                [entry.metadata[int(i)][1] for i in candidate_idx.tolist()],
                dtype=object,
            )
        # lexsort: last key is primary. Sort by target_id asc, then score desc.
        order = np.lexsort((cand_target_ids, -cand_scores))
        ranked: list[dict[str, Any]] = []
        embedding_ids = entry.embedding_ids
        event_ids = entry.event_ids
        append = ranked.append
        for j in order.tolist():
            idx = int(candidate_idx[j])
            if embedding_ids is not None and event_ids is not None:
                embedding_id = embedding_ids[idx]
                target_id = target_ids[idx] if target_ids is not None else entry.metadata[idx][1]
                event_id = event_ids[idx]
            else:
                embedding_id, target_id, event_id = entry.metadata[idx]
            append(
                {
                    "embedding_id": embedding_id,
                    "target_id": target_id,
                    "event_id": event_id,
                    "target_type": target_type,
                    "model_id": model_id,
                    "score": float(cand_scores[j]),
                }
            )
        return ranked

    def search_embeddings(
        self,
        query_vector: Sequence[float],
        target_type: str = "chunk",
        model_id: str | None = None,
        limit: int = 40,
        batch_size: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        config_hash: str = "",
        event_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if target_type not in {"event", "chunk"}:
            raise ValueError("target_type must be 'event' or 'chunk'")
        query = normalize_vector(query_vector)
        # Optional candidate prefilter: rank vectors only for events already
        # surfaced by FTS / identifiers. This bounds a 100GB scan to a few
        # hundred rows at the cost of purely semantic recall.
        filter_ids: list[str] | None = None
        if event_ids:
            filter_ids = sorted(
                {str(item) for item in event_ids if str(item or "").strip()}
            )[:1000]
            if not filter_ids:
                return []
        exact_model = model_id is None and bool(provider and model)
        result_limit = max(1, min(int(limit), 1000))
        batch_size = self.vector_batch_size if batch_size is None else max(1, int(batch_size))
        try:
            import numpy as np
        except ImportError:  # pragma: no cover - optional acceleration
            np = None

        # Warm-cache short-circuit when model_id is known: no SQLite open.
        if (
            np is not None
            and self.vector_cache_limit > 0
            and model_id is not None
            and not exact_model
            and filter_ids is None
        ):
            cache_key = (model_id, target_type)
            with self._vector_cache_lock:
                entry = self._vector_cache.get(cache_key)
                if entry is not None:
                    if entry.dimension != len(query):
                        raise VectorDimensionError(
                            f"query dimension {len(query)} does not match model dimension {entry.dimension}"
                        )
                    # Touch: move key to the end so global-budget eviction keeps
                    # recently used entries first.
                    self._vector_cache.pop(cache_key, None)
                    self._vector_cache[cache_key] = entry
                    return self._rank_cached_vectors(
                        entry,
                        query,
                        target_type=target_type,
                        model_id=model_id,
                        result_limit=result_limit,
                        batch_size=batch_size,
                        np=np,
                    )

        conn = self._connect()
        try:
            if model_id is None:
                if exact_model:
                    row = conn.execute(
                        """SELECT m.id,m.dimension FROM embedding_models m
                           WHERE m.provider=? AND m.model=? AND m.dimension=? AND m.config_hash=?
                             AND EXISTS(SELECT 1 FROM memory_embeddings e
                                        WHERE e.model_id=m.id AND e.target_type=?)
                           LIMIT 1""",
                        (provider, model, len(query), config_hash, target_type),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """SELECT m.id,m.dimension FROM embedding_models m
                           WHERE m.dimension=? AND EXISTS(
                               SELECT 1 FROM memory_embeddings e
                               WHERE e.model_id=m.id AND e.target_type=?
                           ) ORDER BY m.created_at DESC LIMIT 1""",
                        (len(query), target_type),
                    ).fetchone()
                if not row:
                    return []
                model_id = row["id"]
                dimension = row["dimension"]
            else:
                row = conn.execute(
                    "SELECT dimension FROM embedding_models WHERE id=?", (model_id,)
                ).fetchone()
                if not row:
                    return []
                dimension = row["dimension"]
            if dimension != len(query):
                raise VectorDimensionError(
                    f"query dimension {len(query)} does not match model dimension {dimension}"
                )
            # Online recall searches only rows that already have embeddings.
            # Full-index completeness belongs in health reports, not recall gates —
            # partial embedding progress must still return partial hits.
            embedding_count: int | None = None
            filter_sql = ""
            filter_params: tuple = ()
            if filter_ids is not None:
                placeholders = ",".join("?" for _ in filter_ids)
                filter_sql = f" AND event_id IN ({placeholders})"
                filter_params = tuple(filter_ids)
            if exact_model:
                indexed = conn.execute(
                    f"SELECT count(*) FROM memory_embeddings "
                    f"WHERE model_id=? AND target_type=?{filter_sql}",
                    (model_id, target_type, *filter_params),
                ).fetchone()[0]
                if not indexed:
                    return []
                embedding_count = int(indexed)

            if (
                np is not None
                and self.vector_cache_limit > 0
                and filter_ids is None
            ):
                cache_key = (model_id, target_type)
                # Warm-cache fast path: skip COUNT(*) when entry already loaded.
                with self._vector_cache_lock:
                    entry = self._vector_cache.get(cache_key)
                    if entry is not None and entry.dimension != dimension:
                        self._vector_cache.pop(cache_key, None)
                        entry = None
                    if entry is not None:
                        return self._rank_cached_vectors(
                            entry,
                            query,
                            target_type=target_type,
                            model_id=model_id,
                            result_limit=result_limit,
                            batch_size=batch_size,
                            np=np,
                        )
                if embedding_count is None:
                    embedding_count = int(
                        conn.execute(
                            f"SELECT count(*) FROM memory_embeddings "
                            f"WHERE model_id=? AND target_type=?{filter_sql}",
                            (model_id, target_type, *filter_params),
                        ).fetchone()[0]
                    )
                scan_limit = max(0, int(getattr(self, "vector_full_scan_row_limit", 0) or 0))
                if scan_limit and embedding_count > scan_limit:
                    raise VectorScanTooLargeError(
                        rows=int(embedding_count), limit=scan_limit
                    )
                if embedding_count <= self.vector_cache_limit:
                    with self._vector_cache_lock:
                        entry = self._vector_cache.get(cache_key)
                        if entry is not None and entry.dimension != dimension:
                            self._vector_cache.pop(cache_key, None)
                            entry = None
                        if entry is None:
                            entry = self._build_vector_cache_entry(
                                conn,
                                model_id=model_id,
                                target_type=target_type,
                                dimension=dimension,
                                expected_count=embedding_count,
                                np=np,
                            )
                        if entry is not None:
                            if not self._cache_entry_with_global_budget(
                                cache_key, entry
                            ):
                                entry = None
                        if entry is not None:
                            return self._rank_cached_vectors(
                                entry,
                                query,
                                target_type=target_type,
                                model_id=model_id,
                                result_limit=result_limit,
                                batch_size=batch_size,
                                np=np,
                            )

            ranked: list[dict[str, Any]] = []
            last_id = ""
            while True:
                rows = conn.execute(
                    f"SELECT id,target_id,event_id,vector_blob FROM memory_embeddings "
                    f"WHERE model_id=? AND target_type=?{filter_sql} AND id>? "
                    f"ORDER BY id LIMIT ?",
                    (model_id, target_type, *filter_params, last_id, batch_size),
                ).fetchall()
                if not rows:
                    break
                last_id = rows[-1]["id"]
                valid: list[tuple[sqlite3.Row, list[float]]] = []
                for item in rows:
                    try:
                        valid.append((item, decode_vector(item["vector_blob"], dimension)))
                    except VectorDimensionError:
                        continue
                if np is not None and valid:
                    matrix = np.asarray([values for _, values in valid], dtype=np.float32)
                    scores = matrix @ np.asarray(query, dtype=np.float32)
                    for (item, _), score in zip(valid, scores.tolist()):
                        ranked.append(
                            {
                                "embedding_id": item["id"],
                                "target_id": item["target_id"],
                                "event_id": item["event_id"],
                                "target_type": target_type,
                                "model_id": model_id,
                                "score": float(score),
                            }
                        )
                else:
                    for item, values in valid:
                        ranked.append(
                            {
                                "embedding_id": item["id"],
                                "target_id": item["target_id"],
                                "event_id": item["event_id"],
                                "target_type": target_type,
                                "model_id": model_id,
                                "score": sum(a * b for a, b in zip(query, values)),
                            }
                        )
                if len(ranked) > result_limit * 4:
                    ranked.sort(key=lambda item: (-item["score"], item["target_id"]))
                    del ranked[result_limit:]
            ranked.sort(key=lambda item: (-item["score"], item["target_id"]))
            return ranked[:result_limit]
        finally:
            conn.close()

    def get_privacy_salt(self) -> bytes:
        """Return the per-account secret used for irreversible actor pseudonyms."""

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM brain_info WHERE key='privacy_salt'"
            ).fetchone()
            if not row:
                raise RuntimeError("memory brain privacy salt is missing")
            return bytes.fromhex(row["value"])
        finally:
            conn.close()

    def _rebuild_event_fts(self, conn: sqlite3.Connection, event_id: str) -> None:
        event = conn.execute(
            "SELECT id,idempotency_key,title,summary FROM memory_events WHERE id=?", (event_id,)
        ).fetchone()
        if not event:
            raise KeyError(f"unknown event: {event_id}")
        sources = conn.execute(
            "SELECT external_id,full_text FROM memory_sources WHERE event_id=? ORDER BY ordinal",
            (event_id,),
        ).fetchall()
        observations = conn.execute(
            "SELECT text FROM memory_observations WHERE event_id=? ORDER BY ordinal", (event_id,)
        ).fetchall()
        entity_rows = conn.execute(
            """SELECT DISTINCT e.id AS entity_id,e.canonical_name,a.alias
               FROM memory_entity_mentions m
               JOIN memory_entities e ON e.id=m.entity_id
               LEFT JOIN memory_entity_aliases a ON a.entity_id=e.id
               WHERE m.event_id=?
               ORDER BY e.canonical_name,a.alias""",
            (event_id,),
        ).fetchall()
        entity_terms = list(
            dict.fromkeys(
                value
                for row in entity_rows
                for value in (row["canonical_name"], row["alias"])
                if value
            )
        )
        entity_ids = list(dict.fromkeys(row["entity_id"] for row in entity_rows))
        text = "\n".join(
            value
            for value in (
                event["title"],
                event["summary"],
                *(row["full_text"] for row in sources),
                *(row["text"] for row in observations),
                *entity_terms,
            )
            if value
        )
        conn.execute("DELETE FROM memory_event_fts WHERE event_id=?", (event_id,))
        conn.execute(
            "INSERT INTO memory_event_fts(event_id,search_text) VALUES(?,?)",
            (
                event_id,
                build_fts_text(
                    text,
                    (
                        event_id,
                        event["idempotency_key"],
                        *(row["external_id"] for row in sources),
                        *entity_ids,
                    ),
                ),
            ),
        )

    def _missing_derivation_job_types_tx(
        self,
        conn: sqlite3.Connection,
        event_id: str,
    ) -> list[str]:
        """Return job types whose produced derived rows are now missing.

        Produced-row watermarks are read from live job payloads first and from
        the event's ``_v6_result_watermarks`` metadata second (copied there by
        ``prune_finished_jobs`` before terminal job rows were removed).  For
        legacy rows without any watermark only embedding jobs can be detected
        safely: a completed embed job always produced at least one vector,
        whereas zero extracted entities or zero suggested links are legitimate.
        """
        job_rows = conn.execute(
            "SELECT job_type,status,payload_json FROM brain_jobs "
            "WHERE event_id=? ORDER BY created_at,id",
            (event_id,),
        ).fetchall()
        chunk_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE event_id=?",
                (event_id,),
            ).fetchone()[0]
            or 0
        )
        current = {
            "event_embeddings": int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings "
                    "WHERE event_id=? AND target_type='event'",
                    (event_id,),
                ).fetchone()[0]
                or 0
            ),
            "chunk_embeddings": int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_embeddings "
                    "WHERE event_id=? AND target_type='chunk'",
                    (event_id,),
                ).fetchone()[0]
                or 0
            ),
            "entity_mentions": int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_entity_mentions WHERE event_id=?",
                    (event_id,),
                ).fetchone()[0]
                or 0
            ),
            "links": int(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_links WHERE source_event_id=?",
                    (event_id,),
                ).fetchone()[0]
                or 0
            ),
        }
        expected_by_type = {
            "embed_event": "event_embeddings",
            "embed_chunks": "chunk_embeddings",
            "extract_entities": "entity_mentions",
            "link_associations": "links",
        }
        # Job payload watermarks (authoritative while the job row exists).
        payload_watermarks: dict[str, dict[str, int]] = {}
        row_status: dict[str, str] = {}
        for row in job_rows:
            job_type = str(row["job_type"] or "")
            row_status[job_type] = str(row["status"] or "")
            payload = _json_loads(row["payload_json"], {})
            produced = payload.get("result_counts") if isinstance(payload, Mapping) else {}
            if isinstance(produced, Mapping):
                payload_watermarks[job_type] = {
                    str(key): int(value)
                    for key, value in produced.items()
                    if str(key) in current
                }
        # Metadata watermarks survive prune_finished_jobs.
        metadata_row = conn.execute(
            "SELECT metadata_json FROM memory_events WHERE id=?", (event_id,)
        ).fetchone()
        metadata_watermarks: dict[str, int] = {}
        if metadata_row is not None:
            meta = _json_loads(metadata_row["metadata_json"], {})
            if isinstance(meta, Mapping):
                stored = meta.get("_v6_result_watermarks")
                if isinstance(stored, Mapping):
                    metadata_watermarks = {
                        str(key): int(value)
                        for key, value in stored.items()
                        if str(key) in current
                    }
        missing: list[str] = []
        for job_type, produced_field in expected_by_type.items():
            expected: Optional[int] = None
            job_wm = payload_watermarks.get(job_type)
            if job_wm is not None and produced_field in job_wm:
                expected = job_wm[produced_field]
            elif produced_field in metadata_watermarks:
                expected = metadata_watermarks[produced_field]
            if expected is not None:
                if current[produced_field] < expected:
                    missing.append(job_type)
                continue
            if job_type == "embed_event" and row_status.get(job_type) == "completed":
                if current["event_embeddings"] == 0:
                    missing.append(job_type)
            elif job_type == "embed_chunks" and row_status.get(job_type) == "completed":
                if chunk_count > 0 and current["chunk_embeddings"] == 0:
                    missing.append(job_type)
        return list(dict.fromkeys(missing))

    def _requeue_event_jobs_tx(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        job_types: Sequence[str],
        now: float,
    ) -> list[str]:
        """Requeue or recreate enrichment job rows for one event (same tx)."""
        requeued: list[str] = []
        for job_type in job_types:
            job_type = str(job_type).strip()
            if not job_type:
                continue
            existing = conn.execute(
                "SELECT id,status FROM brain_jobs WHERE event_id=? AND job_type=?",
                (event_id, job_type),
            ).fetchone()
            if existing is None:
                job_id = _new_id("job")
                conn.execute(
                    """INSERT INTO brain_jobs(
                        id,dedupe_key,job_type,event_id,payload_json,status,attempts,
                        max_attempts,available_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'pending',0,?,?,?,?)""",
                    (
                        job_id,
                        f"{event_id}:{job_type}",
                        job_type,
                        event_id,
                        _json_dumps({"event_id": event_id}),
                        (
                            self.link_job_max_attempts
                            if job_type == "link_associations"
                            else self.job_max_attempts
                        ),
                        now,
                        now,
                        now,
                    ),
                )
                requeued.append(job_type)
            elif existing["status"] not in ("processing", "blocked", "dead"):
                changed = conn.execute(
                    """UPDATE brain_jobs SET status='pending',attempts=0,available_at=?,
                        lease_owner=NULL,leased_until=NULL,last_error='',completed_at=NULL,
                        updated_at=? WHERE id=? AND status NOT IN
                        ('processing','blocked','dead')""",
                    (now, now, existing["id"]),
                ).rowcount
                if changed:
                    requeued.append(job_type)
        if requeued:
            conn.execute(
                "UPDATE memory_events SET index_status='pending',updated_at=? WHERE id=?",
                (now, event_id),
            )
        return requeued

    def scan_derivation_repairs(
        self,
        *,
        limit: int = 500,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Scan ready events and requeue only evidence-backed missing derivations.

        Covers rows that never went through idempotent replay: a completed
        embed/entity/link job with ``result_counts`` watermarks (or the legacy
        completed-embed signal) is compared against the live derived rows and
        missing jobs are recreated.  Events with no evidence are left alone —
        zero extracted entities/links can be a legitimate outcome.
        """
        bounded = max(1, min(int(limit), 2000))
        now = time.time()
        repaired: dict[str, list[str]] = {}
        scanned = 0
        with self._write_lock:
            conn = self._connect()
            try:
                # dry_run only reads. A read-only scan must not hold a write
                # transaction: on a large legacy DB the old implementation
                # held BEGIN IMMEDIATE for ~190s and starved the durable worker
                # into "database is locked" crashes.
                if not dry_run:
                    conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT id FROM memory_events WHERE index_status='ready' "
                    "ORDER BY updated_at,id LIMIT ?",
                    (bounded,),
                ).fetchall()
                scanned = len(rows)
                for row in rows:
                    event_id = str(row["id"] or "")
                    missing = self._missing_derivation_job_types_tx(conn, event_id)
                    if not missing:
                        continue
                    if dry_run:
                        repaired[event_id] = missing
                        continue
                    requeued = self._requeue_event_jobs_tx(
                        conn, event_id, missing, now
                    )
                    if requeued:
                        repaired[event_id] = requeued
                if not dry_run:
                    conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()
        return {
            "scanned": scanned,
            "repaired_events": len(repaired),
            "dry_run": bool(dry_run),
            "details": repaired,
        }

    def _repair_event_derivations_tx(
        self,
        conn: sqlite3.Connection,
        event_id: str,
    ) -> dict[str, int]:
        """Rebuild chunks + both FTS tables for one event (same-transaction).

        Used when an idempotent replay finds a ``ready`` event whose derived
        rows were lost by external interference.
        """
        event = conn.execute(
            "SELECT id FROM memory_events WHERE id=?", (event_id,)
        ).fetchone()
        if not event:
            raise KeyError(f"unknown event: {event_id}")
        observations = conn.execute(
            "SELECT id,source_id,ordinal,text FROM memory_observations "
            "WHERE event_id=? ORDER BY ordinal",
            (event_id,),
        ).fetchall()
        source_external = {
            row["id"]: row["external_id"]
            for row in conn.execute(
                "SELECT id,external_id FROM memory_sources WHERE event_id=?",
                (event_id,),
            ).fetchall()
        }
        now = time.time()
        rebuilt_chunks = 0
        # Chunk rows are about to be recreated with new IDs; old chunk
        # embeddings reference deleted chunk IDs and would both leak in vector
        # search and make _missing_derivation_job_types_tx believe embed_chunks
        # is already done (leaving the new chunks vectorless).
        conn.execute(
            "DELETE FROM memory_embeddings WHERE event_id=? AND target_type='chunk'",
            (event_id,),
        )
        if observations:
            conn.execute("DELETE FROM memory_chunks WHERE event_id=?", (event_id,))
            conn.execute("DELETE FROM memory_chunk_fts WHERE event_id=?", (event_id,))
            chunk_ordinal = 0
            previous_text = ""
            for observation in observations:
                for chunk in self._chunk_observation(
                    observation["text"], previous_text
                ):
                    chunk_id = _new_id("chk")
                    conn.execute(
                        """INSERT INTO memory_chunks(
                            id,event_id,source_id,observation_id,ordinal,observation_ordinal,
                            text,content_hash,start_char,end_char,overlap_chars,char_count,
                            token_count,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            chunk_id,
                            event_id,
                            observation["source_id"],
                            observation["id"],
                            chunk_ordinal,
                            observation["ordinal"],
                            chunk["text"],
                            content_hash(chunk["text"]),
                            chunk["start_char"],
                            chunk["end_char"],
                            chunk["overlap_chars"],
                            chunk["char_count"],
                            chunk["token_count"],
                            now,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) VALUES(?,?,?)",
                        (
                            chunk_id,
                            event_id,
                            build_fts_text(
                                chunk["text"],
                                (
                                    chunk_id,
                                    event_id,
                                    source_external.get(observation["source_id"], ""),
                                ),
                            ),
                        ),
                    )
                    chunk_ordinal += 1
                    rebuilt_chunks += 1
                previous_text = observation["text"]
        self._rebuild_event_fts(conn, event_id)
        return {"chunks": rebuilt_chunks, "event_fts": 1, "chunk_fts": rebuilt_chunks}

    def repair_event_derivations(self, event_id: str) -> dict[str, int]:
        """Public wrapper for same-transaction derived-row repair."""
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                report = self._repair_event_derivations_tx(conn, event_id)
                conn.commit()
                return report
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def update_event_summary(self, event_id: str, summary: str) -> None:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute(
                    "UPDATE memory_events SET summary=?,updated_at=? WHERE id=?",
                    (summary, now, event_id),
                ).rowcount
                if not changed:
                    raise KeyError(f"unknown event: {event_id}")
                self._rebuild_event_fts(conn, event_id)
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def get_event_index_input(self, event_id: str) -> dict[str, Any]:
        event = self.get_event(event_id, chunks_per_event=None)
        if not event:
            raise KeyError(f"unknown event: {event_id}")
        title = str(event.get("title") or "").strip()[:_EVENT_EMBEDDING_TITLE_LIMIT]
        summary = str(event.get("summary") or "").strip()
        event["embedding_text"] = "\n".join(
            value for value in (title, summary) if value
        )
        return event

    def upsert_entities(
        self, event_id: str, entities: Sequence[str | Mapping[str, Any]]
    ) -> list[str]:
        now = time.time()
        entity_ids: list[str] = []
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not conn.execute("SELECT 1 FROM memory_events WHERE id=?", (event_id,)).fetchone():
                    raise KeyError(f"unknown event: {event_id}")
                for value in entities:
                    if isinstance(value, str):
                        item: Mapping[str, Any] = {"name": value}
                    else:
                        item = value
                    name = str(item.get("name") or item.get("canonical_name") or "").strip()
                    if not name:
                        continue
                    normalized = normalize_search_text(name).strip()
                    row = conn.execute(
                        "SELECT id FROM memory_entities WHERE normalized_name=?", (normalized,)
                    ).fetchone()
                    entity_id = row["id"] if row else _new_id("ent")
                    conn.execute(
                        """INSERT INTO memory_entities(
                            id,canonical_name,normalized_name,entity_type,metadata_json,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(normalized_name) DO UPDATE SET
                            canonical_name=excluded.canonical_name,
                            entity_type=excluded.entity_type,
                            metadata_json=excluded.metadata_json,
                            updated_at=excluded.updated_at""",
                        (
                            entity_id,
                            name,
                            normalized,
                            normalize_entity_type(
                                item.get("type") or item.get("entity_type") or "topic"
                            ),
                            _json_dumps(item.get("metadata") or {}),
                            now,
                            now,
                        ),
                    )
                    row = conn.execute(
                        "SELECT id FROM memory_entities WHERE normalized_name=?", (normalized,)
                    ).fetchone()
                    entity_id = row["id"]
                    entity_ids.append(entity_id)
                    aliases = item.get("aliases") or []
                    for alias in aliases:
                        alias = str(alias).strip()
                        if not alias:
                            continue
                        conn.execute(
                            """INSERT INTO memory_entity_aliases(
                                id,entity_id,alias,normalized_alias,created_at
                            ) VALUES(?,?,?,?,?) ON CONFLICT(normalized_alias) DO NOTHING""",
                            (_new_id("alias"), entity_id, alias, normalize_search_text(alias), now),
                        )
                    surface = str(item.get("surface_text") or name)
                    conn.execute(
                        """INSERT OR IGNORE INTO memory_entity_mentions(
                            id,entity_id,event_id,observation_id,chunk_id,surface_text,confidence,created_at
                        ) VALUES(?,?,?,NULL,NULL,?,?,?)""",
                        (
                            _new_id("mention"),
                            entity_id,
                            event_id,
                            surface,
                            max(0.0, min(1.0, float(item.get("confidence", 1.0)))),
                            now,
                        ),
                    )
                self._rebuild_event_fts(conn, event_id)
                conn.commit()
                return entity_ids
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def suggest_entity_links(
        self,
        event_id: str,
        candidate_event_ids: Sequence[str],
        *,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Deterministic ``related_to`` links from shared extracted entities.

        Used as an evidence-based fallback when the LLM linker returns an
        empty/None result: entity co-occurrence is exactly the kind of topical
        association ``related_to`` is allowed to express, and it never invents
        a target the brain has not already seen.
        """
        bounded = max(1, min(int(limit), 24))
        candidates = [str(item) for item in candidate_event_ids if item]
        candidates = list(dict.fromkeys(candidates))[:50]
        if not candidates:
            return []
        conn = self._connect()
        try:
            source_rows = conn.execute(
                "SELECT DISTINCT entity_id FROM memory_entity_mentions WHERE event_id=?",
                (event_id,),
            ).fetchall()
            source_entities = {str(row["entity_id"]) for row in source_rows}
            if not source_entities:
                return []
            placeholders = ",".join("?" for _ in source_entities)
            ranked: list[tuple[float, str]] = []
            for candidate_id in candidates:
                if candidate_id == event_id:
                    continue
                rows = conn.execute(
                    f"""SELECT DISTINCT entity_id FROM memory_entity_mentions
                        WHERE event_id=? AND entity_id IN ({placeholders})""",
                    [candidate_id, *source_entities],
                ).fetchall()
                overlap = len(rows)
                if overlap <= 0:
                    continue
                candidate_count = int(
                    conn.execute(
                        "SELECT COUNT(DISTINCT entity_id) FROM memory_entity_mentions WHERE event_id=?",
                        (candidate_id,),
                    ).fetchone()[0]
                    or 0
                )
                union = max(1, len(source_entities) + candidate_count - overlap)
                weight = min(0.95, max(0.3, overlap / union))
                ranked.append((weight, candidate_id))
            ranked.sort(reverse=True)
            return [
                {
                    "target_event_id": candidate_id,
                    "relation_type": "related_to",
                    "weight": round(weight, 4),
                    "evidence_ids": [event_id, candidate_id],
                }
                for weight, candidate_id in ranked[:bounded]
            ]
        finally:
            conn.close()

    def upsert_links(
        self,
        source_event_id: str,
        links: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        now = time.time()
        link_ids: list[str] = []
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                source_event = conn.execute(
                    "SELECT event_type,source_type FROM memory_events WHERE id=?",
                    (source_event_id,),
                ).fetchone()
                if not source_event:
                    raise KeyError(f"unknown event: {source_event_id}")
                for item in links:
                    target_id = str(item.get("target_event_id") or "")
                    relation = str(item.get("relation_type") or "related_to")
                    if not target_id or target_id == source_event_id:
                        continue
                    if not conn.execute(
                        "SELECT 1 FROM memory_events WHERE id=?", (target_id,)
                    ).fetchone():
                        continue
                    evidence_ids = self._validated_link_evidence_ids(
                        conn,
                        source_event_id,
                        target_id,
                        item.get("evidence_ids"),
                        require_target_evidence=(
                            source_event["event_type"] == "reflection"
                            or source_event["source_type"] == "reflection"
                        ),
                    )
                    if not evidence_ids:
                        continue
                    existing = conn.execute(
                        "SELECT id FROM memory_links WHERE source_event_id=? AND target_event_id=? "
                        "AND relation_type=?",
                        (source_event_id, target_id, relation),
                    ).fetchone()
                    link_id = existing["id"] if existing else _new_id("lnk")
                    weight = max(0.0, min(1.0, float(item.get("weight", 0.5))))
                    conn.execute(
                        """INSERT INTO memory_links(
                            id,source_event_id,target_event_id,relation_type,weight,evidence_json,
                            created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_event_id,target_event_id,relation_type) DO UPDATE SET
                            weight=excluded.weight,evidence_json=excluded.evidence_json,
                            updated_at=excluded.updated_at""",
                        (
                            link_id,
                            source_event_id,
                            target_id,
                            relation,
                            weight,
                            _json_dumps(evidence_ids),
                            now,
                            now,
                        ),
                    )
                    link_ids.append(link_id)
                conn.commit()
                return link_ids
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    @staticmethod
    def _validated_link_evidence_ids(
        conn: sqlite3.Connection,
        source_event_id: str,
        target_event_id: str,
        raw_evidence: Any,
        *,
        require_target_evidence: bool = False,
    ) -> list[str]:
        if not isinstance(raw_evidence, Sequence) or isinstance(
            raw_evidence, (str, bytes, bytearray)
        ):
            return []
        if not raw_evidence or any(not isinstance(value, str) for value in raw_evidence):
            return []
        evidence_ids = list(
            dict.fromkeys(value.strip() for value in raw_evidence if value.strip())
        )
        if not evidence_ids:
            return []

        placeholders = ",".join("?" for _ in evidence_ids)
        rows = conn.execute(
            f"""SELECT id AS evidence_id,id AS event_id FROM memory_events
                WHERE id IN ({placeholders})
                UNION ALL
                SELECT id AS evidence_id,event_id FROM memory_sources
                WHERE id IN ({placeholders})
                UNION ALL
                SELECT id AS evidence_id,event_id FROM memory_observations
                WHERE id IN ({placeholders})
                UNION ALL
                SELECT id AS evidence_id,event_id FROM memory_chunks
                WHERE id IN ({placeholders})""",
            [*evidence_ids, *evidence_ids, *evidence_ids, *evidence_ids],
        ).fetchall()
        owners: dict[str, set[str]] = {}
        for row in rows:
            owners.setdefault(str(row["evidence_id"]), set()).add(str(row["event_id"]))
        endpoints = {source_event_id, target_event_id}
        if any(
            evidence_id not in owners or not owners[evidence_id].issubset(endpoints)
            for evidence_id in evidence_ids
        ):
            return []
        if require_target_evidence and not any(
            target_event_id in owners[evidence_id] for evidence_id in evidence_ids
        ):
            return []
        return evidence_ids

    def related_events(
        self, event_ids: Sequence[str], limit: int = 20
    ) -> list[dict[str, Any]]:
        values = list(dict.fromkeys(str(value) for value in event_ids if value))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""SELECT l.*,
                    CASE WHEN l.source_event_id IN ({placeholders})
                         THEN l.target_event_id ELSE l.source_event_id END AS event_id
                    FROM memory_links l
                    WHERE l.source_event_id IN ({placeholders})
                       OR l.target_event_id IN ({placeholders})
                    ORDER BY l.weight DESC,l.updated_at DESC LIMIT ?""",
                [*values, *values, *values, max(1, min(int(limit), 200))],
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def reinforce_recall(
        self, event_ids: Sequence[str], link_ids: Sequence[str] = ()
    ) -> None:
        events = list(dict.fromkeys(str(value) for value in event_ids if value))
        links = list(dict.fromkeys(str(value) for value in link_ids if value))
        if not events and not links:
            return
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if events:
                    placeholders = ",".join("?" for _ in events)
                    conn.execute(
                        f"UPDATE memory_events SET recall_count=recall_count+1,last_recalled_at=? "
                        f"WHERE id IN ({placeholders})",
                        [now, *events],
                    )
                if links:
                    placeholders = ",".join("?" for _ in links)
                    conn.execute(
                        f"""UPDATE memory_links SET recall_count=recall_count+1,
                            last_recalled_at=?,weight=min(1.0,weight+0.01),updated_at=?
                            WHERE id IN ({placeholders})""",
                        [now, now, *links],
                    )
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def claim_jobs(
        self,
        worker_id: str,
        limit: int = 1,
        lease_seconds: float = 60.0,
        job_types: Sequence[str] | None = None,
        now: float | None = None,
    ) -> list[ClaimedJob]:
        if not worker_id:
            raise ValueError("worker_id is required")
        current = time.time() if now is None else float(now)
        leased_until = current + max(1.0, float(lease_seconds))
        clauses = [
            # pending/retry must still have attempt budget; expired processing
            # leases are always reclaimable so we can bump attempts or dead-letter.
            "((status IN ('pending','retry') AND available_at<=? AND attempts < max_attempts) "
            "OR (status='processing' AND leased_until<=?))",
        ]
        params: list[Any] = [current, current]
        if job_types:
            values = [str(item) for item in job_types]
            clauses.append(f"job_type IN ({','.join('?' for _ in values)})")
            params.extend(values)
        params.append(max(1, min(int(limit), 100)))
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    f"SELECT * FROM brain_jobs WHERE {' AND '.join(clauses)} "
                    "ORDER BY CASE job_type "
                    "WHEN 'embed_event' THEN 0 WHEN 'embed_chunks' THEN 1 "
                    "WHEN 'summarize_event' THEN 2 WHEN 'extract_entities' THEN 3 "
                    "WHEN 'link_associations' THEN 4 ELSE 5 END,"
                    "available_at,created_at,id LIMIT ?",
                    params,
                ).fetchall()
                jobs: list[ClaimedJob] = []
                for row in rows:
                    # attempts counts starts (claim / reclaim), not only fail_job.
                    # Worker crashes that never call fail_job still consume a slot and
                    # eventually hit max_attempts. fail_job must NOT +1 again.
                    next_attempts = int(row["attempts"]) + 1
                    max_attempts = int(row["max_attempts"])
                    if next_attempts > max_attempts:
                        # Lease expired after attempt budget already consumed — dead-letter.
                        conn.execute(
                            """UPDATE brain_jobs SET status='dead',lease_owner=NULL,leased_until=NULL,
                                last_error=CASE WHEN last_error='' OR last_error IS NULL
                                    THEN 'reclaim exceeded max_attempts'
                                    ELSE last_error END,
                                updated_at=? WHERE id=? AND status='processing' AND leased_until<=?""",
                            (current, row["id"], current),
                        )
                        continue
                    changed = conn.execute(
                        """UPDATE brain_jobs SET status='processing',lease_owner=?,leased_until=?,
                            attempts=?,updated_at=? WHERE id=? AND (
                              (status IN ('pending','retry') AND available_at<=?) OR
                              (status='processing' AND leased_until<=?)
                            )""",
                        (
                            worker_id,
                            leased_until,
                            next_attempts,
                            current,
                            row["id"],
                            current,
                            current,
                        ),
                    ).rowcount
                    if not changed:
                        continue
                    jobs.append(
                        ClaimedJob(
                            id=row["id"],
                            job_type=row["job_type"],
                            event_id=row["event_id"],
                            payload=_json_loads(row["payload_json"], {}),
                            attempts=next_attempts,
                            max_attempts=max_attempts,
                            lease_owner=worker_id,
                            leased_until=leased_until,
                        )
                    )
                conn.commit()
                return jobs
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def complete_job(
        self,
        job_id: str,
        worker_id: str,
        result_counts: Optional[Mapping[str, int]] = None,
    ) -> bool:
        """Mark a leased job completed; optionally persist produced-row counts.

        ``result_counts`` is the durability watermark used by idempotent replay
        repair to decide whether derived rows (embeddings/entities/links) were
        lost after the job completed.
        """
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                if result_counts:
                    row = conn.execute(
                        "SELECT payload_json FROM brain_jobs "
                        "WHERE id=? AND status='processing' AND lease_owner=?",
                        (job_id, worker_id),
                    ).fetchone()
                    if not row:
                        return False
                    payload = _json_loads(row["payload_json"], {})
                    payload["result_counts"] = {
                        str(key): int(value) for key, value in result_counts.items()
                    }
                    changed = conn.execute(
                        """UPDATE brain_jobs SET status='completed',lease_owner=NULL,
                            leased_until=NULL,last_error='',payload_json=?,
                            completed_at=?,updated_at=?
                            WHERE id=? AND status='processing' AND lease_owner=?""",
                        (_json_dumps(payload), now, now, job_id, worker_id),
                    ).rowcount
                else:
                    changed = conn.execute(
                        """UPDATE brain_jobs SET status='completed',lease_owner=NULL,leased_until=NULL,
                            last_error='',completed_at=?,updated_at=?
                            WHERE id=? AND status='processing' AND lease_owner=?""",
                        (now, now, job_id, worker_id),
                    ).rowcount
                return bool(changed)
            finally:
                conn.close()

    def prune_finished_jobs(self, *, older_than_seconds: float = 30 * 24 * 3600) -> int:
        """Remove terminal outbox rows (completed/dead) older than the window.

        ``brain_jobs`` is operational bookkeeping, not memory content: pruning
        terminal rows cannot lose events/chunks/vectors/FTS. Before deletion,
        produced-row watermarks (``result_counts``) are copied into the event's
        ``metadata_json`` under ``_v6_result_watermarks`` so a later idempotent
        replay can still detect derived rows lost after the job row is gone.
        Tombstones are intentionally never pruned — they are the privacy guard
        against re-ingesting explicitly deleted content.
        """
        now = time.time()
        cutoff = now - max(1.0, float(older_than_seconds))
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                doomed = conn.execute(
                    "SELECT job_type,event_id,payload_json FROM brain_jobs "
                    "WHERE status IN ('completed','dead') "
                    "AND COALESCE(completed_at, updated_at, 0) < ?",
                    (cutoff,),
                ).fetchall()
                changed = conn.execute(
                    "DELETE FROM brain_jobs WHERE status IN ('completed','dead') "
                    "AND COALESCE(completed_at, updated_at, 0) < ?",
                    (cutoff,),
                ).rowcount
                watermarks: dict[str, dict[str, int]] = {}
                for row in doomed:
                    payload = _json_loads(row["payload_json"], {})
                    counts = payload.get("result_counts") if isinstance(payload, Mapping) else {}
                    if not isinstance(counts, Mapping) or not counts:
                        continue
                    event_wm = watermarks.setdefault(str(row["event_id"] or ""), {})
                    for key, value in counts.items():
                        try:
                            event_wm[str(key)] = int(value)
                        except (TypeError, ValueError):
                            continue
                for event_id, produced in watermarks.items():
                    meta_row = conn.execute(
                        "SELECT metadata_json FROM memory_events WHERE id=?",
                        (event_id,),
                    ).fetchone()
                    if meta_row is None:
                        continue
                    meta = _json_loads(meta_row["metadata_json"], {})
                    if not isinstance(meta, dict):
                        meta = {}
                    prior = meta.get("_v6_result_watermarks")
                    merged = dict(prior) if isinstance(prior, Mapping) else {}
                    merged.update(produced)
                    meta["_v6_result_watermarks"] = merged
                    conn.execute(
                        "UPDATE memory_events SET metadata_json=?,updated_at=? WHERE id=?",
                        (_json_dumps(meta), now, event_id),
                    )
                conn.commit()
                return int(changed or 0)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def prune_recall_traces(
        self,
        *,
        older_than_seconds: float = 7 * 24 * 3600,
        keep_recent: int = 5000,
    ) -> int:
        """Bound operational recall diagnostics; never touches memory content.

        recall_traces / recall_candidates are debug bookkeeping only. Keep the
        newest ``keep_recent`` traces and drop the rest once older than the
        retention window (candidate rows cascade via FK).
        """
        now = time.time()
        cutoff = now - max(1.0, float(older_than_seconds))
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute(
                    """DELETE FROM recall_traces WHERE created_at < ?
                        AND id NOT IN (
                            SELECT id FROM recall_traces
                            ORDER BY created_at DESC, id DESC LIMIT ?
                        )""",
                    (cutoff, max(1, int(keep_recent))),
                ).rowcount
                conn.commit()
                return int(changed or 0)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def pseudonymize_legacy_actors(
        self,
        pseudonymize,
        *,
        bot_actor_id: str = "",
        dry_run: bool = False,
    ) -> int:
        """One-time remediation: replace legacy raw platform UIDs with pseudonyms.

        Older builds stored raw Bilibili mids as ``speaker_actor_id`` /
        ``observation.actor_id`` and inside structured source payloads
        (hot-comment/thread JSON). This rewrites those rows to the same
        account-salt pseudonym produced by ``PrivateMessageRedactor``, updates
        row hashes, and recomputes the event content hash so future idempotent
        replays match the pseudonymized envelopes emitted by current callers.

        ``dry_run=True`` only counts affected events and changes nothing.
        """
        if not callable(pseudonymize):
            raise ValueError("pseudonymize must be callable")

        def map_actor(value: Any) -> str:
            raw = str(value or "").strip()
            if not _looks_like_platform_uid(raw):
                return raw
            if bot_actor_id and raw == str(bot_actor_id).strip():
                return "self"
            try:
                return str(pseudonymize(raw))
            except Exception:
                # Fail-closed placeholder, never leave the raw UID behind.
                return "actor_" + content_hash("bilibot-legacy-actor:" + raw)[:24]

        with self._write_lock:
            conn = self._connect()
            try:
                event_ids = [
                    row[0]
                    for row in conn.execute("SELECT id FROM memory_events").fetchall()
                ]
                affected = 0
                if dry_run:
                    return sum(
                        1
                        for event_id in event_ids
                        if _legacy_event_has_raw_actor(conn, event_id)
                    )
                conn.execute("BEGIN IMMEDIATE")
                for event_id in event_ids:
                    if not _legacy_event_has_raw_actor(conn, event_id):
                        continue
                    event = conn.execute(
                        "SELECT event_type,title,summary,speaker_actor_id,"
                        "persona_id,scene,importance,metadata_json "
                        "FROM memory_events WHERE id=?",
                        (event_id,),
                    ).fetchone()
                    if event is None:
                        continue
                    new_speaker = map_actor(event["speaker_actor_id"])
                    source_rows = conn.execute(
                        "SELECT id,ordinal,source_type,external_id,full_text,"
                        "structured_json FROM memory_sources WHERE event_id=? "
                        "ORDER BY ordinal",
                        (event_id,),
                    ).fetchall()
                    sources: list[SourceDocument] = []
                    source_changed = False
                    for srow in source_rows:
                        data = _json_loads(srow["structured_json"], {})
                        new_data, data_changed = _pseudonymize_actor_payload(
                            data,
                            pseudonymize,
                            bot_actor_id=bot_actor_id,
                        )
                        full_text = str(srow["full_text"] or "")
                        new_full = full_text
                        full_changed = False
                        try:
                            parsed_full = json.loads(full_text)
                            sanitized_full, full_changed = _pseudonymize_actor_payload(
                                parsed_full,
                                pseudonymize,
                                bot_actor_id=bot_actor_id,
                            )
                            if full_changed:
                                new_full = json.dumps(
                                    sanitized_full,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    indent=2,
                                    default=_json_default,
                                )
                        except (TypeError, json.JSONDecodeError):
                            pass
                        observations: list[Observation] = []
                        obs_rows = conn.execute(
                            "SELECT id,ordinal,modality,actor_id,external_id,text,"
                            "structured_json,occurred_at,start_ms,end_ms,ignored,"
                            "ignore_reason,extractor_version FROM memory_observations "
                            "WHERE source_id=? ORDER BY ordinal",
                            (srow["id"],),
                        ).fetchall()
                        for orow in obs_rows:
                            new_actor = map_actor(orow["actor_id"])
                            odata = _json_loads(orow["structured_json"], {})
                            new_odata, odata_changed = _pseudonymize_actor_payload(
                                odata,
                                pseudonymize,
                                bot_actor_id=bot_actor_id,
                            )
                            new_text = orow["text"]
                            text_changed = False
                            if (
                                full_changed
                                and orow["modality"] == srow["source_type"]
                                and orow["text"] == full_text
                            ):
                                # The archive wrote a fallback observation that
                                # echoes the source full_text; sanitized
                                # full_text must propagate into that row too.
                                new_text = new_full
                                text_changed = True
                            observations.append(
                                Observation(
                                    text=new_text,
                                    modality=orow["modality"],
                                    actor_id=new_actor,
                                    occurred_at=orow["occurred_at"],
                                    start_ms=orow["start_ms"],
                                    end_ms=orow["end_ms"],
                                    external_id=orow["external_id"],
                                    data=new_odata,
                                    ignored=bool(orow["ignored"]),
                                    ignore_reason=orow["ignore_reason"] or "",
                                    extractor_version=orow["extractor_version"] or "",
                                )
                            )
                            if (
                                new_actor != orow["actor_id"]
                                or odata_changed
                                or text_changed
                            ):
                                source_changed = True
                                conn.execute(
                                    "UPDATE memory_observations SET actor_id=?,"
                                    "structured_json=?,content_hash=?,text=? WHERE id=?",
                                    (
                                        new_actor,
                                        _json_dumps(new_odata),
                                        _observation_row_hash(
                                            ordinal=orow["ordinal"],
                                            observation=observations[-1],
                                        ),
                                        new_text,
                                        orow["id"],
                                    ),
                                )
                        if data_changed or full_changed:
                            source_changed = True
                            conn.execute(
                                "UPDATE memory_sources SET structured_json=?,"
                                "full_text=?,content_hash=? WHERE id=?",
                                (
                                    _json_dumps(new_data),
                                    new_full,
                                    _source_row_hash(
                                        ordinal=srow["ordinal"],
                                        source_type=srow["source_type"],
                                        external_id=srow["external_id"],
                                        full_text=new_full,
                                        structured_data=new_data,
                                    ),
                                    srow["id"],
                                ),
                            )
                        sources.append(
                            SourceDocument(
                                source_type=srow["source_type"],
                                full_text=new_full,
                                external_id=srow["external_id"],
                                data=new_data,
                                observations=tuple(observations),
                            )
                        )
                    if (
                        new_speaker == event["speaker_actor_id"]
                        and not source_changed
                    ):
                        # Candidate check was a false positive (e.g. pseudonym
                        # containing digits); leave the event untouched.
                        continue
                    metadata = _json_loads(event["metadata_json"], {})
                    # ``summary`` is an enriched derived field, not part of the
                    # original envelope hash. Reconstruct the original
                    # event_summary for the ingestion adapters that can carry
                    # raw actor rows; future replays compute their hash against
                    # the original summary, not the enrichment.
                    event_summary = event["summary"]
                    event_type = str(event["event_type"] or "")
                    if event_type in {"conversation_message", "conversation_context"}:
                        event_summary = ""
                    elif event_type == "video_metadata_observation":
                        owner_name = ""
                        if isinstance(metadata, Mapping):
                            owner_name = str(metadata.get("owner") or "")
                        event_summary = (
                            f"获取了视频《{event['title']}》的元数据，UP主 {owner_name}"
                        ).strip()
                    elif event_type == "video_observation":
                        video_detail = next(
                            (
                                item.full_text
                                for item in sources
                                if item.source_type == "video_detail"
                            ),
                            "",
                        )
                        if video_detail:
                            event_summary = video_detail
                        else:
                            owner_name = ""
                            if isinstance(metadata, Mapping):
                                owner_name = str(metadata.get("owner") or "")
                            event_summary = (
                                f"观察了视频《{event['title']}》，UP主 {owner_name}"
                            )
                    envelope = ObservationEnvelope(
                        idempotency_key="legacy-pseudonymization",
                        event_type=event["event_type"],
                        event_title=event["title"],
                        event_summary=event_summary,
                        speaker_actor_id=new_speaker,
                        persona_id=event["persona_id"],
                        scene=event["scene"],
                        importance=float(event["importance"] or 0.5),
                        metadata=metadata,
                        sources=tuple(sources),
                    )
                    new_digest = _envelope_hash(envelope, sources)
                    if (
                        new_speaker != event["speaker_actor_id"]
                        or source_changed
                        or new_digest != conn.execute(
                            "SELECT content_hash FROM memory_events WHERE id=?",
                            (event_id,),
                        ).fetchone()[0]
                    ):
                        conn.execute(
                            "UPDATE memory_events SET speaker_actor_id=?,"
                            "content_hash=?,updated_at=? WHERE id=?",
                            (new_speaker, new_digest, time.time(), event_id),
                        )
                        if event_type == "video_metadata_observation":
                            # The adapter's idempotency key embeds the compact
                            # metadata digest; after pseudonymizing owner.mid
                            # the key must move with it or future replays would
                            # create a duplicate row.
                            from .ingestion import _stable_hash

                            safe_metadata = (
                                sources[0].data if sources else {}
                            )
                            oid_or_bvid = ""
                            if isinstance(metadata, Mapping):
                                oid_or_bvid = str(
                                    metadata.get("oid")
                                    or metadata.get("bvid")
                                    or ""
                                )
                            new_key = (
                                f"video_metadata:{self.account_id}:"
                                f"{oid_or_bvid}:{_stable_hash(safe_metadata)}"
                            )
                            conn.execute(
                                "UPDATE memory_events SET idempotency_key=? "
                                "WHERE id=?",
                                (new_key, event_id),
                            )
                        if source_changed:
                            self._rebuild_event_fts(conn, event_id)
                        affected += 1
                conn.commit()
                return affected
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def renew_job_lease(
        self, job_id: str, worker_id: str, lease_seconds: float = 60.0
    ) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                changed = conn.execute(
                    """UPDATE brain_jobs SET leased_until=?,updated_at=?
                        WHERE id=? AND status='processing' AND lease_owner=?""",
                    (now + max(1.0, float(lease_seconds)), now, job_id, worker_id),
                ).rowcount
                return bool(changed)
            finally:
                conn.close()

    def requeue_event_job(self, event_id: str, job_type: str) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                # Re-open ordinary downstream states after upstream enrichment.
                # Dead letters require an explicit operator retry; silently
                # resetting them here creates endless job generations/storms.
                changed = conn.execute(
                    """UPDATE brain_jobs SET status='pending',attempts=0,available_at=?,
                        lease_owner=NULL,leased_until=NULL,last_error='',completed_at=NULL,
                        updated_at=? WHERE event_id=? AND job_type=?
                        AND status NOT IN ('processing', 'blocked', 'dead')""",
                    (now, now, event_id, job_type),
                ).rowcount
                return bool(changed)
            finally:
                conn.close()

    def block_job(self, job_id: str, worker_id: str, error: str) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                changed = conn.execute(
                    """UPDATE brain_jobs SET status='blocked',lease_owner=NULL,leased_until=NULL,
                        last_error=?,updated_at=?
                        WHERE id=? AND status='processing' AND lease_owner=?""",
                    (str(error)[:4000], now, job_id, worker_id),
                ).rowcount
                return bool(changed)
            finally:
                conn.close()

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error: str,
        *,
        now: float | None = None,
    ) -> str | None:
        current = time.time() if now is None else float(now)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT attempts,max_attempts FROM brain_jobs "
                    "WHERE id=? AND status='processing' AND lease_owner=?",
                    (job_id, worker_id),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return None
                # attempts already bumped on claim_jobs; do not double-count here.
                attempts = int(row["attempts"])
                status = "dead" if attempts >= row["max_attempts"] else "retry"
                # PRD 9.1：min(30 * 2^(attempt-1), 3600)
                delay = 0.0 if status == "dead" else min(3600.0, 30.0 * (2 ** max(0, attempts - 1)))
                conn.execute(
                    """UPDATE brain_jobs SET status=?,available_at=?,lease_owner=NULL,
                        leased_until=NULL,last_error=?,updated_at=? WHERE id=?""",
                    (
                        status,
                        current + delay,
                        str(error)[:4000],
                        current,
                        job_id,
                    ),
                )
                conn.commit()
                return status
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def unblock_blocked_jobs(
        self,
        job_types: Sequence[str] | None = None,
        *,
        min_blocked_age_seconds: float = 0.0,
    ) -> int:
        """Re-open blocked jobs when a provider becomes available.

        P0-D: reset attempts to 0 on unblock. claim_jobs increments attempts on
        every start; without a reset, a job that was blocked after several
        claims can become pending forever (attempts >= max_attempts still
        blocks claim).

        The common case is zero blocked rows. Count first so the worker's
        1s poll does not start a no-op write transaction on a large brain
        (which can contend with WAL checkpoints and stall for the full SQLite
        busy timeout).

        ``min_blocked_age_seconds`` keeps recently blocked jobs in place.
        Without it, a provider that returns no result for one specific job is
        misdiagnosed as ProviderNotConfigured and the job loops
        claim→block→unblock forever, resetting attempts each cycle.
        """
        now = time.time()
        grace = max(0.0, float(min_blocked_age_seconds))
        params: list[Any] = []
        where = "status='blocked'"
        if grace > 0:
            where += " AND updated_at<=?"
            params.append(now - grace)
        if job_types:
            values = [str(value) for value in job_types]
            where += f" AND job_type IN ({','.join('?' for _ in values)})"
            params.extend(values)
        with self._write_lock:
            conn = self._connect()
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM brain_jobs WHERE {where}", params
                ).fetchone()[0]
                if not count:
                    return 0
                changed = conn.execute(
                    f"""UPDATE brain_jobs SET status='pending',attempts=0,available_at=?,
                        last_error='',lease_owner=NULL,leased_until=NULL,
                        updated_at=? WHERE {where}""",
                    [now, now, *params],
                ).rowcount
                return int(changed)
            finally:
                conn.close()

    def retry_dead_letter(self, job_id: str) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                changed = conn.execute(
                    """UPDATE brain_jobs SET status='pending',attempts=0,available_at=?,
                        lease_owner=NULL,leased_until=NULL,last_error='',completed_at=NULL,
                        updated_at=? WHERE id=? AND status='dead'""",
                    (now, now, job_id),
                ).rowcount
                return bool(changed)
            finally:
                conn.close()

    def retry_dead_letters(
        self,
        *,
        job_type: str | None = None,
        event_id: str | None = None,
        limit: int = 100,
    ) -> int:
        """Explicitly requeue a bounded dead-letter batch for operator recovery."""

        selected_type = str(job_type or "").strip()
        selected_event = str(event_id or "").strip()
        bounded_limit = max(1, min(int(limit), 500))
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                clauses = ["status='dead'"]
                params: list[Any] = []
                if selected_type:
                    clauses.append("job_type=?")
                    params.append(selected_type)
                if selected_event:
                    clauses.append("event_id=?")
                    params.append(selected_event)
                params.append(bounded_limit)
                rows = conn.execute(
                    f"SELECT id FROM brain_jobs WHERE {' AND '.join(clauses)} "
                    "ORDER BY updated_at,id LIMIT ?",
                    params,
                ).fetchall()
                ids = [str(row["id"]) for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"""UPDATE brain_jobs SET status='pending',attempts=0,available_at=?,
                            lease_owner=NULL,leased_until=NULL,last_error='',completed_at=NULL,
                            updated_at=? WHERE id IN ({placeholders}) AND status='dead'""",
                        [now, now, *ids],
                    )
                conn.commit()
                return len(ids)
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def dead_letter_report(self, *, limit: int = 20) -> dict[str, Any]:
        """Summarize durable enrichment failures without replaying them.

        Operators can first inspect common causes, then replay a bounded job type
        or one event instead of waking every historical dead letter at once.
        """

        bounded = max(1, min(int(limit), 100))
        conn = self._connect()
        try:
            where = "status IN ('dead','blocked','retry')"
            total_row = conn.execute(
                f"SELECT count(*) AS total,count(DISTINCT event_id) AS affected FROM brain_jobs WHERE {where}"
            ).fetchone()
            status_rows = conn.execute(
                f"SELECT status,count(*) AS count FROM brain_jobs WHERE {where} GROUP BY status"
            ).fetchall()
            type_rows = conn.execute(
                f"SELECT job_type,count(*) AS count FROM brain_jobs WHERE {where} GROUP BY job_type"
            ).fetchall()
            error_expression = (
                "CASE WHEN trim(coalesce(last_error,''))='' THEN 'unknown' "
                "WHEN instr(last_error,':')>0 THEN substr(last_error,1,instr(last_error,':')-1) "
                "ELSE last_error END"
            )
            error_rows = conn.execute(
                f"SELECT {error_expression} AS error_kind,count(*) AS count "
                f"FROM brain_jobs WHERE {where} GROUP BY {error_expression} "
                "ORDER BY count DESC,error_kind LIMIT 50"
            ).fetchall()
            sample_rows = conn.execute(
                f"""SELECT id,event_id,job_type,status,attempts,max_attempts,
                            last_error,created_at,updated_at
                     FROM brain_jobs WHERE {where}
                     ORDER BY CASE status WHEN 'dead' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
                              updated_at,id LIMIT ?""",
                (bounded,),
            ).fetchall()
            by_status = {
                str(row["status"] or "unknown"): int(row["count"] or 0)
                for row in status_rows
            }
            by_job_type = {
                str(row["job_type"] or "unknown"): int(row["count"] or 0)
                for row in type_rows
            }
            by_error = {
                str(row["error_kind"] or "unknown")[:80]: int(row["count"] or 0)
                for row in error_rows
            }
            return {
                "total": int(total_row["total"] or 0),
                "by_status": by_status,
                "by_job_type": by_job_type,
                "by_error": by_error,
                "affected_event_count": int(total_row["affected"] or 0),
                "samples": [dict(row) for row in sample_rows],
            }
        finally:
            conn.close()

    def list_jobs(
        self, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM brain_jobs WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status, max(1, min(int(limit), 500)), max(0, int(offset))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM brain_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (max(1, min(int(limit), 500)), max(0, int(offset))),
                ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["payload"] = _json_loads(item.pop("payload_json"), {})
                result.append(item)
            return result
        finally:
            conn.close()

    def refresh_event_index_status(self, event_id: str) -> str:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT job_type,status FROM brain_jobs WHERE event_id=?", (event_id,)
            ).fetchall()
        finally:
            conn.close()
        statuses = {row["job_type"]: row["status"] for row in rows}
        if not statuses or all(value == "completed" for value in statuses.values()):
            status = "ready"
        elif any(value == "dead" for value in statuses.values()):
            status = "degraded"
        elif any(
            statuses.get(job_type) == "blocked"
            for job_type in ("embed_event", "embed_chunks")
        ):
            status = "fts_only"
        elif any(value == "blocked" for value in statuses.values()):
            status = "enrichment_blocked"
        else:
            status = "pending"
        self.set_event_index_status(event_id, status)
        return status

    @staticmethod
    def _dependent_reflection_ids(
        conn: sqlite3.Connection, target_event_id: str
    ) -> list[str]:
        return [
            str(row["id"])
            for row in conn.execute(
                """SELECT DISTINCT source.id
                   FROM memory_links link
                   JOIN memory_events source ON source.id=link.source_event_id
                   WHERE link.target_event_id=?
                     AND (source.event_type='reflection' OR source.source_type='reflection')""",
                (target_event_id,),
            ).fetchall()
        ]

    def _reflection_has_valid_bottom_evidence(
        self, conn: sqlite3.Connection, reflection_event_id: str
    ) -> bool:
        links = conn.execute(
            """SELECT link.id,link.target_event_id,link.evidence_json
               FROM memory_links link
               JOIN memory_events target ON target.id=link.target_event_id
               WHERE link.source_event_id=?
                 AND target.event_type<>'reflection'
                 AND target.source_type<>'reflection'""",
            (reflection_event_id,),
        ).fetchall()
        has_evidence = False
        for link in links:
            evidence_ids = self._validated_link_evidence_ids(
                conn,
                reflection_event_id,
                str(link["target_event_id"]),
                _json_loads(link["evidence_json"], []),
                require_target_evidence=True,
            )
            if evidence_ids:
                has_evidence = True
            else:
                conn.execute("DELETE FROM memory_links WHERE id=?", (link["id"],))
        return has_evidence

    @staticmethod
    def _delete_event_rows(
        conn: sqlite3.Connection,
        event: sqlite3.Row,
        *,
        reason: str,
        deleted_by: str,
        deleted_at: float,
    ) -> None:
        event_id = str(event["id"])
        conn.execute("DELETE FROM memory_event_fts WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM memory_chunk_fts WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM memory_embeddings WHERE event_id=?", (event_id,))
        conn.execute(
            """INSERT INTO deletion_tombstones(
                id,event_id,idempotency_key,content_hash,source_type,reason,deleted_by,deleted_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                _new_id("del"),
                event_id,
                event["idempotency_key"],
                event["content_hash"],
                event["source_type"],
                reason,
                deleted_by,
                deleted_at,
            ),
        )
        conn.execute("DELETE FROM memory_events WHERE id=?", (event_id,))

    def hard_delete_event(
        self, event_id: str, reason: str = "", deleted_by: str = "admin"
    ) -> bool:
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                event = conn.execute(
                    """SELECT id,idempotency_key,content_hash,event_type,source_type
                       FROM memory_events WHERE id=?""",
                    (event_id,),
                ).fetchone()
                if not event:
                    conn.rollback()
                    return False

                pending_reflections = self._dependent_reflection_ids(conn, event_id)
                self._delete_event_rows(
                    conn,
                    event,
                    reason=reason,
                    deleted_by=deleted_by,
                    deleted_at=now,
                )

                checked: set[str] = set()
                while pending_reflections:
                    reflection_id = pending_reflections.pop()
                    if reflection_id in checked:
                        continue
                    checked.add(reflection_id)
                    reflection = conn.execute(
                        """SELECT id,idempotency_key,content_hash,event_type,source_type
                           FROM memory_events WHERE id=?""",
                        (reflection_id,),
                    ).fetchone()
                    if not reflection or (
                        reflection["event_type"] != "reflection"
                        and reflection["source_type"] != "reflection"
                    ):
                        continue
                    if self._reflection_has_valid_bottom_evidence(conn, reflection_id):
                        continue
                    pending_reflections.extend(
                        self._dependent_reflection_ids(conn, reflection_id)
                    )
                    self._delete_event_rows(
                        conn,
                        reflection,
                        reason=f"all evidence deleted after {event_id}",
                        deleted_by="system",
                        deleted_at=now,
                    )

                conn.execute(
                    "DELETE FROM memory_entities WHERE NOT EXISTS("
                    "SELECT 1 FROM memory_entity_mentions m WHERE m.entity_id=memory_entities.id)"
                )
                conn.commit()
                self._invalidate_vector_cache()
                return True
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def set_event_index_status(self, event_id: str, status: str) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE memory_events SET index_status=?,updated_at=? WHERE id=?",
                    (status, time.time(), event_id),
                )
                # Deleted-while-processing is a valid race for the durable
                # worker: the row is gone, there is nothing left to index.
            finally:
                conn.close()

    def rebuild_fts(self) -> dict[str, int]:
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM memory_event_fts")
                conn.execute("DELETE FROM memory_chunk_fts")
                event_ids = [
                    row["id"]
                    for row in conn.execute("SELECT id FROM memory_events ORDER BY id").fetchall()
                ]
                for event_id in event_ids:
                    self._rebuild_event_fts(conn, event_id)
                chunks = conn.execute(
                    """SELECT c.id,c.event_id,c.text,s.external_id
                       FROM memory_chunks c JOIN memory_sources s ON s.id=c.source_id
                       ORDER BY c.event_id,c.ordinal"""
                ).fetchall()
                conn.executemany(
                    "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) VALUES(?,?,?)",
                    (
                        (
                            row["id"],
                            row["event_id"],
                            build_fts_text(
                                row["text"],
                                (row["id"], row["event_id"], row["external_id"]),
                            ),
                        )
                        for row in chunks
                    ),
                )
                conn.commit()
                return {"events": len(event_ids), "chunks": len(chunks)}
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def reindex_all(self, *, clear_enrichment: bool = True) -> dict[str, int]:
        """Rebuild derived indexes and reset durable jobs without touching archives."""

        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                event_ids = [
                    row["id"]
                    for row in conn.execute("SELECT id FROM memory_events ORDER BY id").fetchall()
                ]
                conn.execute("DELETE FROM memory_event_fts")
                conn.execute("DELETE FROM memory_chunk_fts")
                for event_id in event_ids:
                    self._rebuild_event_fts(conn, event_id)
                chunks = conn.execute(
                    """SELECT c.id,c.event_id,c.text,s.external_id
                       FROM memory_chunks c JOIN memory_sources s ON s.id=c.source_id
                       ORDER BY c.event_id,c.ordinal"""
                ).fetchall()
                conn.executemany(
                    "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) VALUES(?,?,?)",
                    (
                        (
                            row["id"],
                            row["event_id"],
                            build_fts_text(
                                row["text"],
                                (row["id"], row["event_id"], row["external_id"]),
                            ),
                        )
                        for row in chunks
                    ),
                )
                if clear_enrichment:
                    conn.execute("DELETE FROM memory_embeddings")
                    conn.execute("DELETE FROM memory_links")
                    conn.execute("DELETE FROM memory_entity_mentions")
                    conn.execute("DELETE FROM memory_entities")
                for event_id in event_ids:
                    for job_type in _JOB_TYPES:
                        conn.execute(
                            """INSERT INTO brain_jobs(
                                id,dedupe_key,job_type,event_id,payload_json,status,attempts,
                                max_attempts,available_at,created_at,updated_at
                            ) VALUES(?,?,?,?,?,'pending',0,?,?,?,?)
                            ON CONFLICT(dedupe_key) DO UPDATE SET
                                status='pending',attempts=0,available_at=excluded.available_at,
                                max_attempts=excluded.max_attempts,
                                lease_owner=NULL,leased_until=NULL,last_error='',completed_at=NULL,
                                updated_at=excluded.updated_at""",
                            (
                                _new_id("job"),
                                f"{event_id}:{job_type}",
                                job_type,
                                event_id,
                                _json_dumps({"event_id": event_id}),
                                (
                                    self.link_job_max_attempts
                                    if job_type == "link_associations"
                                    else self.job_max_attempts
                                ),
                                now,
                                now,
                                now,
                            ),
                        )
                conn.execute(
                    "UPDATE memory_events SET index_status='pending',updated_at=?", (now,)
                )
                conn.commit()
                self._invalidate_vector_cache()
                return {
                    "events": len(event_ids),
                    "chunks": len(chunks),
                    "jobs": len(event_ids) * len(_JOB_TYPES),
                    "cleared_enrichment": int(bool(clear_enrichment)),
                }
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def checkpoint(self, mode: str = "PASSIVE") -> dict[str, int]:
        selected = str(mode).upper()
        if selected not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("invalid WAL checkpoint mode")
        with self._write_lock:
            conn = self._connect()
            try:
                row = conn.execute(f"PRAGMA wal_checkpoint({selected})").fetchone()
                return {
                    "busy": int(row[0]),
                    "log_frames": int(row[1]),
                    "checkpointed_frames": int(row[2]),
                }
            finally:
                conn.close()

    def save_recall_trace(
        self,
        *,
        query_hash: str,
        query_text: str = "",
        scene: str = "",
        used_fallback: bool = False,
        rerank_status: str = "",
        rerank_calls: int = 0,
        channel_errors: Mapping[str, Any] | None = None,
        latency_ms: float = 0.0,
        prompt_chars: int = 0,
        candidates: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        trace_id = _new_id("trace")
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """INSERT INTO recall_traces(
                        id,query_hash,query_text,scene,used_fallback,rerank_status,
                        rerank_calls,channel_errors_json,latency_ms,prompt_chars,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trace_id,
                        query_hash,
                        _sanitize_trace_query(query_text),
                        scene,
                        int(bool(used_fallback)),
                        str(rerank_status)[:120],
                        max(0, int(rerank_calls)),
                        _json_dumps(dict(channel_errors or {})),
                        max(0.0, float(latency_ms)),
                        max(0, int(prompt_chars)),
                        now,
                    ),
                )
                for item in candidates:
                    if not isinstance(item, Mapping):
                        item = vars(item) if hasattr(item, "__dict__") else {}
                    event_id = str(item.get("event_id") or item.get("candidate_id") or "") or None
                    if event_id and not conn.execute(
                        "SELECT 1 FROM memory_events WHERE id=?", (event_id,)
                    ).fetchone():
                        event_id = None
                    conn.execute(
                        """INSERT INTO recall_candidates(
                            id,trace_id,event_id,channels_json,channel_ranks_json,rrf_score,
                            deterministic_score,llm_score,final_score,decision,threshold,reason,
                            evidence_ids_json,injected,accepted,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            _new_id("cand"),
                            trace_id,
                            event_id,
                            _json_dumps(item.get("channels") or []),
                            _json_dumps(item.get("channel_ranks") or {}),
                            float(item.get("rrf_score") or 0.0),
                            float(item.get("deterministic_score") or 0.0),
                            item.get("llm_score"),
                            item.get("final_score"),
                            str(item.get("decision") or item.get("kind") or ""),
                            max(0.0, min(1.0, float(item.get("threshold") or 0.0))),
                            str(item.get("reason") or "")[:240],
                            _json_dumps(item.get("evidence_ids") or []),
                            int(bool(item.get("injected"))),
                            int(bool(item.get("accepted", item.get("injected")))),
                            now,
                        ),
                    )
                conn.commit()
                return trace_id
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

    def record_recall_trace(self, query: Any, trace: Any) -> str:
        """Compatibility entry point accepting serializable recall contracts."""

        if isinstance(query, str):
            query_text = query
            query_data: Mapping[str, Any] = {}
        elif isinstance(query, Mapping):
            query_data = query
            query_text = str(
                query.get("current_message")
                or query.get("message")
                or query.get("query")
                or ""
            )
        else:
            query_data = vars(query) if hasattr(query, "__dict__") else {}
            query_text = str(
                query_data.get("current_message")
                or query_data.get("message")
                or query_data.get("query")
                or ""
            )
        if isinstance(trace, Mapping):
            trace_data = trace
        elif hasattr(trace, "__dict__"):
            trace_data = vars(trace)
        else:
            raise TypeError("trace must be serializable")
        digest = str(trace_data.get("query_hash") or content_hash(_sanitize_trace_query(query_text)))
        return self.save_recall_trace(
            query_hash=digest,
            query_text=query_text,
            scene=str(trace_data.get("scene") or query_data.get("scene") or ""),
            used_fallback=bool(
                trace_data.get("used_fallback", getattr(trace, "used_fallback", False))
            ),
            rerank_status=str(trace_data.get("rerank_status") or ""),
            rerank_calls=int(trace_data.get("rerank_calls") or 0),
            channel_errors=trace_data.get("channel_errors") or {},
            latency_ms=float(trace_data.get("latency_ms") or 0.0),
            prompt_chars=int(trace_data.get("prompt_chars") or 0),
            candidates=trace_data.get("candidates") or (),
        )

    def list_recall_traces(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT t.*,count(c.id) AS candidate_count,
                          coalesce(sum(c.injected),0) AS injected_count
                   FROM recall_traces t LEFT JOIN recall_candidates c ON c.trace_id=t.id
                   GROUP BY t.id ORDER BY t.created_at DESC
                   LIMIT ? OFFSET ?""",
                (max(1, min(int(limit), 500)), max(0, int(offset))),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["channel_errors"] = _json_loads(
                    item.pop("channel_errors_json", "{}"), {}
                )
                result.append(item)
            return result
        finally:
            conn.close()

    def get_recall_trace(self, trace_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM recall_traces WHERE id=?", (trace_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["channel_errors"] = _json_loads(
                result.pop("channel_errors_json", "{}"), {}
            )
            candidates = []
            for candidate in conn.execute(
                """SELECT c.*,coalesce(e.title,'') AS title,
                          coalesce(e.summary,'') AS summary,
                          coalesce(e.source_type,'') AS source_type,
                          coalesce(e.event_type,'') AS event_type,
                          coalesce(e.index_status,'') AS index_status,
                          coalesce(json_extract(e.metadata_json,'$.action_state'),'') AS action_state,
                          coalesce(e.occurred_at,e.created_at,0) AS occurred_at
                   FROM recall_candidates c
                   LEFT JOIN memory_events e ON e.id=c.event_id
                   WHERE c.trace_id=? ORDER BY c.final_score DESC,c.id""",
                (trace_id,),
            ).fetchall():
                item = dict(candidate)
                item["channels"] = _json_loads(item.pop("channels_json"), [])
                item["channel_ranks"] = _json_loads(
                    item.pop("channel_ranks_json", "{}"), {}
                )
                item["evidence_ids"] = _json_loads(item.pop("evidence_ids_json"), [])
                candidates.append(item)
            result["candidates"] = candidates
            return result
        finally:
            conn.close()

    def upsert_bangumi_watch_state(
        self,
        season_id: str,
        episode_id: str,
        *,
        episode_title: str = "",
        progress_ms: int = 0,
        completed: bool = False,
        watched_at: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not season_id or not episode_id:
            raise ValueError("season_id and episode_id are required")
        now = time.time()
        watched = now if watched_at is None else float(watched_at)
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO bangumi_watch_state(
                        season_id,episode_id,episode_title,progress_ms,completed,watched_at,
                        metadata_json,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(season_id,episode_id) DO UPDATE SET
                        episode_title=CASE WHEN excluded.episode_title<>''
                                           THEN excluded.episode_title ELSE episode_title END,
                        progress_ms=max(progress_ms,excluded.progress_ms),
                        completed=max(completed,excluded.completed),
                        watched_at=max(watched_at,excluded.watched_at),
                        metadata_json=excluded.metadata_json,
                        updated_at=excluded.updated_at""",
                    (
                        str(season_id),
                        str(episode_id),
                        episode_title,
                        max(0, int(progress_ms)),
                        int(bool(completed)),
                        watched,
                        _json_dumps(metadata or {}),
                        now,
                    ),
                )
            finally:
                conn.close()

    def get_bangumi_watch_state(
        self, season_id: str, episode_id: str
    ) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM bangumi_watch_state WHERE season_id=? AND episode_id=?",
                (str(season_id), str(episode_id)),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["metadata"] = _json_loads(item.pop("metadata_json"), {})
            item["completed"] = bool(item["completed"])
            return item
        finally:
            conn.close()

    def list_bangumi_watch_state(
        self, season_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if season_id is None:
                rows = conn.execute(
                    "SELECT * FROM bangumi_watch_state ORDER BY watched_at DESC LIMIT ?",
                    (max(1, min(int(limit), 5000)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bangumi_watch_state WHERE season_id=? "
                    "ORDER BY watched_at DESC LIMIT ?",
                    (str(season_id), max(1, min(int(limit), 5000))),
                ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = _json_loads(item.pop("metadata_json"), {})
                item["completed"] = bool(item["completed"])
                result.append(item)
            return result
        finally:
            conn.close()

    def log_legacy_cleanup(self, path: str | Path, status: str, error: str = "") -> None:
        if status not in {"deleted", "missing", "failed"}:
            raise ValueError("invalid cleanup status")
        now = time.time()
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO legacy_cleanup_log(
                        id,path,status,error,attempts,last_attempted_at
                    ) VALUES(?,?,?,?,1,?)
                    ON CONFLICT(path) DO UPDATE SET status=excluded.status,error=excluded.error,
                        attempts=legacy_cleanup_log.attempts+1,
                        last_attempted_at=excluded.last_attempted_at""",
                    (_new_id("cleanup"), str(path), status, str(error)[:4000], now),
                )
            finally:
                conn.close()

    def health_check(self) -> HealthReport:
        errors: list[str] = []
        quick = "error"
        foreign_errors: list[Mapping[str, Any]] = []
        fts_ok = False
        vector_ok = False
        version = 0
        conn: sqlite3.Connection | None = None

        def _failure(label: str, exc: BaseException) -> str:
            return f"{label}: {type(exc).__name__}: {exc}"

        def _rollback(label: str, probe_errors: list[str]) -> None:
            if conn is None or not conn.in_transaction:
                return
            try:
                conn.rollback()
            except Exception as exc:  # pragma: no cover - a broken connection is rare
                probe_errors.append(_failure(f"{label} rollback failed", exc))

        with self._write_lock:
            try:
                conn = self._connect()
            except Exception as exc:
                errors.append(_failure("database connection failed", exc))

            if conn is not None:
                try:
                    try:
                        quick_rows = conn.execute("PRAGMA quick_check").fetchall()
                        quick_values = [str(row[0]) for row in quick_rows]
                        quick = "; ".join(quick_values) or "(no rows)"
                        if quick_values != ["ok"]:
                            errors.append(
                                "quick_check must return exactly one 'ok' row; "
                                f"received {quick!r}"
                            )
                    except Exception as exc:
                        errors.append(_failure("quick_check failed", exc))

                    try:
                        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                        foreign_errors = [dict(row) for row in fk_rows]
                        if foreign_errors:
                            errors.append(
                                "foreign_key_check must return zero rows; "
                                f"received {len(foreign_errors)}"
                            )
                    except Exception as exc:
                        errors.append(_failure("foreign_key_check failed", exc))

                    try:
                        table_rows = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                        table_names = {str(row["name"]) for row in table_rows}
                        missing_tables = sorted(_HEALTH_REQUIRED_TABLES - table_names)
                        if missing_tables:
                            errors.append(
                                "schema is missing required tables: " + ", ".join(missing_tables)
                            )

                        migration_rows = conn.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        ).fetchall()
                        migration_versions = [int(row["version"]) for row in migration_rows]
                        version = max(migration_versions, default=0)
                        expected_versions = list(range(1, SCHEMA_VERSION + 1))
                        if migration_versions != expected_versions:
                            errors.append(
                                "schema migrations are incomplete or unexpected: "
                                f"received {migration_versions}, expected {expected_versions}"
                            )
                        if version != SCHEMA_VERSION:
                            errors.append(
                                f"schema version is {version}, expected {SCHEMA_VERSION}"
                            )

                        info_row = conn.execute(
                            "SELECT value FROM brain_info WHERE key='schema_version'"
                        ).fetchone()
                        if info_row is None:
                            errors.append("brain_info.schema_version is missing")
                        else:
                            try:
                                info_version = int(info_row["value"])
                            except (TypeError, ValueError):
                                errors.append(
                                    "brain_info.schema_version is not an integer: "
                                    f"{info_row['value']!r}"
                                )
                            else:
                                if info_version != SCHEMA_VERSION:
                                    errors.append(
                                        "brain_info schema version is "
                                        f"{info_version}, expected {SCHEMA_VERSION}"
                                    )
                    except Exception as exc:
                        errors.append(_failure("schema validation failed", exc))

                    try:
                        account_row = conn.execute(
                            "SELECT value FROM brain_info WHERE key='account_id'"
                        ).fetchone()
                        stored_account = str(account_row["value"]) if account_row else ""
                        if not account_row:
                            errors.append("brain_info.account_id is missing")
                        if not self.account_id:
                            errors.append("configured account_id is empty")
                        if stored_account != self.account_id:
                            errors.append(
                                "brain_info.account_id does not match configured account_id: "
                                f"stored={stored_account!r}, configured={self.account_id!r}"
                            )

                        resolved_path = self.db_path.resolve()
                        path_account: str | None = None
                        parent = resolved_path.parent
                        grandparent = parent.parent
                        if resolved_path.name.casefold() == "memory_brain.db":
                            if parent.name.casefold() == "bot":
                                # Flat sole-account layout: path does not encode account_id.
                                path_account = None
                            elif grandparent.name.casefold() == "accounts":
                                path_account = parent.name
                        if path_account is not None:
                            if stored_account != path_account:
                                errors.append(
                                    "brain_info.account_id does not match database path account: "
                                    f"stored={stored_account!r}, path={path_account!r}"
                                )
                            if self.account_id != path_account:
                                errors.append(
                                    "configured account_id does not match database path account: "
                                    f"configured={self.account_id!r}, path={path_account!r}"
                                )
                    except Exception as exc:
                        errors.append(_failure("account identity validation failed", exc))

                    fts_errors: list[str] = []
                    fts_token = uuid.uuid4().hex
                    fts_event_id = f"health_fts_evt_{fts_token}"
                    fts_chunk_id = f"health_fts_chk_{fts_token}"
                    fts_bvid = f"BV1{fts_token[:9]}"
                    fts_chinese = "联想记忆健康探针"
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        search_text = build_fts_text(fts_chinese, (fts_bvid,))
                        conn.execute(
                            "INSERT INTO memory_event_fts(event_id,search_text) VALUES(?,?)",
                            (fts_event_id, search_text),
                        )
                        conn.execute(
                            "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) "
                            "VALUES(?,?,?)",
                            (fts_chunk_id, fts_event_id, search_text),
                        )
                        fts_checks = (
                            (
                                "event Chinese",
                                "memory_event_fts",
                                "event_id",
                                fts_event_id,
                                self._fts_query(fts_chinese),
                            ),
                            (
                                "event BVID",
                                "memory_event_fts",
                                "event_id",
                                fts_event_id,
                                self._fts_query(fts_bvid),
                            ),
                            (
                                "chunk Chinese",
                                "memory_chunk_fts",
                                "chunk_id",
                                fts_chunk_id,
                                self._fts_query(fts_chinese),
                            ),
                            (
                                "chunk BVID",
                                "memory_chunk_fts",
                                "chunk_id",
                                fts_chunk_id,
                                self._fts_query(fts_bvid),
                            ),
                        )
                        missed: list[str] = []
                        for label, table, id_column, target_id, match_query in fts_checks:
                            if not match_query:
                                missed.append(f"{label} query was empty")
                                continue
                            found = conn.execute(
                                f"SELECT 1 FROM {table} "
                                f"WHERE {table} MATCH ? AND {id_column}=? LIMIT 1",
                                (match_query, target_id),
                            ).fetchone()
                            if found is None:
                                missed.append(f"{label} query did not match")
                        if missed:
                            fts_errors.append("FTS probe failed: " + "; ".join(missed))
                    except Exception as exc:
                        fts_errors.append(_failure("FTS probe failed", exc))
                    finally:
                        _rollback("FTS probe", fts_errors)

                    try:
                        residual = {
                            "memory_event_fts": conn.execute(
                                "SELECT count(*) FROM memory_event_fts WHERE event_id=?",
                                (fts_event_id,),
                            ).fetchone()[0],
                            "memory_chunk_fts": conn.execute(
                                "SELECT count(*) FROM memory_chunk_fts WHERE chunk_id=?",
                                (fts_chunk_id,),
                            ).fetchone()[0],
                        }
                        dirty = [f"{name}={count}" for name, count in residual.items() if count]
                        if dirty:
                            fts_errors.append(
                                "FTS probe rollback left residual rows: " + ", ".join(dirty)
                            )
                    except Exception as exc:
                        fts_errors.append(_failure("FTS residual check failed", exc))
                    fts_ok = not fts_errors
                    errors.extend(fts_errors)

                    vector_errors: list[str] = []
                    vector_token = uuid.uuid4().hex
                    vector_event_id = f"health_vec_evt_{vector_token}"
                    vector_model_id = f"health_vec_model_{vector_token}"
                    vector_embedding_id = f"health_vec_emb_{vector_token}"
                    try:
                        source_vector = [3.0, -4.0, 12.0]
                        blob, dimension = encode_vector(source_vector)
                        expected_vector = normalize_vector(source_vector)
                        now = time.time()
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            """INSERT INTO memory_events(
                                id,idempotency_key,content_hash,event_type,source_type,
                                created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?)""",
                            (
                                vector_event_id,
                                f"health-vector:{vector_token}",
                                content_hash(vector_token),
                                "health_probe",
                                "health_probe",
                                now,
                                now,
                            ),
                        )
                        conn.execute(
                            """INSERT INTO embedding_models(
                                id,provider,model,dimension,config_hash,created_at
                            ) VALUES(?,?,?,?,?,?)""",
                            (
                                vector_model_id,
                                "health_probe",
                                "float32_roundtrip",
                                dimension,
                                vector_token,
                                now,
                            ),
                        )
                        conn.execute(
                            """INSERT INTO memory_embeddings(
                                id,target_type,target_id,event_id,model_id,dimension,vector_blob,
                                content_hash,norm,created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                vector_embedding_id,
                                "event",
                                vector_event_id,
                                vector_event_id,
                                vector_model_id,
                                dimension,
                                sqlite3.Binary(blob),
                                content_hash(blob),
                                1.0,
                                now,
                                now,
                            ),
                        )
                        row = conn.execute(
                            """SELECT dimension,vector_blob,norm,
                                      typeof(vector_blob) AS storage_type,
                                      length(vector_blob) AS byte_length
                               FROM memory_embeddings WHERE id=?""",
                            (vector_embedding_id,),
                        ).fetchone()
                        if row is None:
                            raise RuntimeError("embedding row could not be read back")
                        stored_blob = bytes(row["vector_blob"])
                        decoded = decode_vector(stored_blob, int(row["dimension"]))
                        query_vector = normalize_vector([1.0, 2.0, -1.0])
                        expected_dot = sum(
                            left * right for left, right in zip(expected_vector, query_vector)
                        )
                        actual_dot = sum(
                            left * right for left, right in zip(decoded, query_vector)
                        )
                        checks = {
                            "SQLite storage class is BLOB": row["storage_type"] == "blob",
                            "dimension round-trip": int(row["dimension"]) == dimension,
                            "BLOB byte length": int(row["byte_length"]) == dimension * 4,
                            "little-endian float32 encoding": stored_blob
                            == struct.pack(f"<{dimension}f", *expected_vector),
                            "decoded values": all(
                                abs(actual - expected) <= 1e-6
                                for actual, expected in zip(decoded, expected_vector)
                            ),
                            "normalized vector": abs(
                                math.sqrt(sum(value * value for value in decoded)) - 1.0
                            )
                            <= 1e-6
                            and abs(float(row["norm"]) - 1.0) <= 1e-6,
                            "dot-product round-trip": abs(actual_dot - expected_dot) <= 1e-6,
                        }
                        failed_checks = [label for label, passed in checks.items() if not passed]
                        if failed_checks:
                            vector_errors.append(
                                "vector BLOB probe failed: " + "; ".join(failed_checks)
                            )
                    except Exception as exc:
                        vector_errors.append(_failure("vector BLOB probe failed", exc))
                    finally:
                        _rollback("vector BLOB probe", vector_errors)

                    try:
                        residual = {
                            "memory_events": conn.execute(
                                "SELECT count(*) FROM memory_events WHERE id=?",
                                (vector_event_id,),
                            ).fetchone()[0],
                            "embedding_models": conn.execute(
                                "SELECT count(*) FROM embedding_models WHERE id=?",
                                (vector_model_id,),
                            ).fetchone()[0],
                            "memory_embeddings": conn.execute(
                                "SELECT count(*) FROM memory_embeddings WHERE id=?",
                                (vector_embedding_id,),
                            ).fetchone()[0],
                        }
                        dirty = [f"{name}={count}" for name, count in residual.items() if count]
                        if dirty:
                            vector_errors.append(
                                "vector BLOB probe rollback left residual rows: "
                                + ", ".join(dirty)
                            )
                    except Exception as exc:
                        vector_errors.append(_failure("vector BLOB residual check failed", exc))
                    vector_ok = not vector_errors
                    errors.extend(vector_errors)

                    archive_errors: list[str] = []
                    archive_token = uuid.uuid4().hex
                    archive_event_id = f"health_arc_evt_{archive_token}"
                    archive_source_id = f"health_arc_src_{archive_token}"
                    archive_observation_id = f"health_arc_obs_{archive_token}"
                    archive_chunk_id = f"health_arc_chk_{archive_token}"
                    archive_job_id = f"health_arc_job_{archive_token}"
                    archive_key = f"health-probe:{archive_token}"
                    archive_body = f"联想记忆健康探针正文 {archive_token}"
                    archive_job_key = f"{archive_key}:summarize_event"
                    try:
                        now = time.time()
                        body_hash = content_hash(archive_body)
                        conn.execute("BEGIN IMMEDIATE")
                        conn.execute(
                            """INSERT INTO memory_events(
                                id,idempotency_key,content_hash,event_type,source_type,title,
                                summary,speaker_actor_id,persona_id,scene,importance,occurred_at,
                                metadata_json,index_status,created_at,updated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                archive_event_id,
                                archive_key,
                                body_hash,
                                "health_probe",
                                "health_probe",
                                "health archive rollback probe",
                                "",
                                "",
                                "",
                                "health_check",
                                0.0,
                                now,
                                "{}",
                                "pending",
                                now,
                                now,
                            ),
                        )
                        conn.execute(
                            """INSERT INTO memory_sources(
                                id,event_id,ordinal,source_type,external_id,full_text,
                                structured_json,content_hash,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?)""",
                            (
                                archive_source_id,
                                archive_event_id,
                                0,
                                "health_probe",
                                archive_key,
                                archive_body,
                                "{}",
                                body_hash,
                                now,
                            ),
                        )
                        conn.execute(
                            """INSERT INTO memory_observations(
                                id,event_id,source_id,ordinal,modality,actor_id,external_id,text,
                                structured_json,content_hash,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                archive_observation_id,
                                archive_event_id,
                                archive_source_id,
                                0,
                                "text",
                                "",
                                archive_key,
                                archive_body,
                                "{}",
                                body_hash,
                                now,
                            ),
                        )
                        conn.execute(
                            """INSERT INTO memory_chunks(
                                id,event_id,source_id,observation_id,ordinal,
                                observation_ordinal,text,content_hash,start_char,end_char,
                                overlap_chars,char_count,token_count,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                archive_chunk_id,
                                archive_event_id,
                                archive_source_id,
                                archive_observation_id,
                                0,
                                0,
                                archive_body,
                                body_hash,
                                0,
                                len(archive_body),
                                0,
                                len(archive_body),
                                estimate_tokens(archive_body),
                                now,
                            ),
                        )
                        archive_search_text = build_fts_text(
                            archive_body, (archive_event_id, archive_key)
                        )
                        conn.execute(
                            "INSERT INTO memory_event_fts(event_id,search_text) VALUES(?,?)",
                            (archive_event_id, archive_search_text),
                        )
                        conn.execute(
                            "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) "
                            "VALUES(?,?,?)",
                            (archive_chunk_id, archive_event_id, archive_search_text),
                        )
                        conn.execute(
                            """INSERT INTO brain_jobs(
                                id,dedupe_key,job_type,event_id,payload_json,status,attempts,
                                max_attempts,available_at,created_at,updated_at
                            ) VALUES(?,?,?,?,?,'pending',0,?,?,?,?)""",
                            (
                                archive_job_id,
                                archive_job_key,
                                "summarize_event",
                                archive_event_id,
                                _json_dumps({"event_id": archive_event_id}),
                                self.job_max_attempts,
                                now,
                                now,
                                now,
                            ),
                        )
                        inserted_checks = {
                            "event": conn.execute(
                                "SELECT count(*) FROM memory_events WHERE id=?",
                                (archive_event_id,),
                            ).fetchone()[0],
                            "source body": conn.execute(
                                "SELECT count(*) FROM memory_sources WHERE id=? AND full_text=?",
                                (archive_source_id, archive_body),
                            ).fetchone()[0],
                            "observation body": conn.execute(
                                "SELECT count(*) FROM memory_observations WHERE id=? AND text=?",
                                (archive_observation_id, archive_body),
                            ).fetchone()[0],
                            "chunk body": conn.execute(
                                "SELECT count(*) FROM memory_chunks WHERE id=? AND text=?",
                                (archive_chunk_id, archive_body),
                            ).fetchone()[0],
                            "event FTS": conn.execute(
                                "SELECT count(*) FROM memory_event_fts WHERE event_id=?",
                                (archive_event_id,),
                            ).fetchone()[0],
                            "chunk FTS": conn.execute(
                                "SELECT count(*) FROM memory_chunk_fts WHERE chunk_id=?",
                                (archive_chunk_id,),
                            ).fetchone()[0],
                            "job": conn.execute(
                                "SELECT count(*) FROM brain_jobs WHERE id=? AND event_id=?",
                                (archive_job_id, archive_event_id),
                            ).fetchone()[0],
                        }
                        missing = [
                            label for label, count in inserted_checks.items() if int(count) != 1
                        ]
                        if missing:
                            archive_errors.append(
                                "archive rollback probe could not verify inserted rows: "
                                + ", ".join(missing)
                            )
                    except Exception as exc:
                        archive_errors.append(_failure("archive rollback probe failed", exc))
                    finally:
                        _rollback("archive rollback probe", archive_errors)

                    try:
                        residual = {
                            "event": conn.execute(
                                "SELECT count(*) FROM memory_events "
                                "WHERE id=? OR idempotency_key=?",
                                (archive_event_id, archive_key),
                            ).fetchone()[0],
                            "source/body": conn.execute(
                                "SELECT count(*) FROM memory_sources "
                                "WHERE id=? OR full_text=?",
                                (archive_source_id, archive_body),
                            ).fetchone()[0],
                            "observation/body": conn.execute(
                                "SELECT count(*) FROM memory_observations "
                                "WHERE id=? OR text=?",
                                (archive_observation_id, archive_body),
                            ).fetchone()[0],
                            "chunk/body": conn.execute(
                                "SELECT count(*) FROM memory_chunks WHERE id=? OR text=?",
                                (archive_chunk_id, archive_body),
                            ).fetchone()[0],
                            "event FTS": conn.execute(
                                "SELECT count(*) FROM memory_event_fts WHERE event_id=?",
                                (archive_event_id,),
                            ).fetchone()[0],
                            "chunk FTS": conn.execute(
                                "SELECT count(*) FROM memory_chunk_fts WHERE chunk_id=?",
                                (archive_chunk_id,),
                            ).fetchone()[0],
                            "job": conn.execute(
                                "SELECT count(*) FROM brain_jobs "
                                "WHERE id=? OR event_id=? OR dedupe_key=?",
                                (archive_job_id, archive_event_id, archive_job_key),
                            ).fetchone()[0],
                        }
                        dirty = [f"{name}={count}" for name, count in residual.items() if count]
                        if dirty:
                            archive_errors.append(
                                "archive rollback probe left residual rows: " + ", ".join(dirty)
                            )
                    except Exception as exc:
                        archive_errors.append(
                            _failure("archive rollback residual check failed", exc)
                        )
                    errors.extend(archive_errors)
                finally:
                    if conn.in_transaction:
                        try:
                            conn.rollback()
                        except Exception as exc:  # pragma: no cover - broken connection
                            errors.append(_failure("final health-check rollback failed", exc))
                    conn.close()
        return HealthReport(
            ok=not errors,
            quick_check=quick,
            foreign_key_errors=tuple(foreign_errors),
            fts_ok=fts_ok,
            vector_ok=vector_ok,
            schema_version=version,
            errors=tuple(errors),
        )

    def backup_to(self, destination: str | Path) -> Path:
        target = Path(destination)
        if target.resolve() == self.db_path.resolve():
            raise ValueError("backup destination must differ from source database")
        target.parent.mkdir(parents=True, exist_ok=True)
        source_conn = self._connect()
        destination_conn = sqlite3.connect(str(target), timeout=30)
        try:
            source_conn.backup(destination_conn)
            destination_conn.commit()
        finally:
            destination_conn.close()
            source_conn.close()
        return target
