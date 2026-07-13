"""Public data contracts for the account-scoped V6 memory brain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 2
DEFAULT_JOB_TYPES = (
    "summarize_event",
    "embed_event",
    "embed_chunks",
    "extract_entities",
    "link_associations",
)


class MemoryBrainError(RuntimeError):
    """Base class for memory-brain failures with a stable machine code."""

    code = "MEMORY_BRAIN_ERROR"


class IdempotencyConflictError(MemoryBrainError):
    code = "IDEMPOTENCY_CONFLICT"


class ReingestBlockedError(MemoryBrainError):
    code = "REINGEST_BLOCKED"


class ProviderNotConfigured(MemoryBrainError):
    code = "PROVIDER_NOT_CONFIGURED"


class VectorDimensionError(MemoryBrainError):
    code = "VECTOR_DIMENSION_MISMATCH"


@dataclass(frozen=True)
class Observation:
    """One lossless turn, cue, sound, visual observation, or time range."""

    text: str
    modality: str = "text"
    actor_id: str = ""
    occurred_at: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    external_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    ignored: bool = False
    ignore_reason: str = ""
    extractor_version: str = ""

    @classmethod
    def coerce(cls, value: Observation | Mapping[str, Any] | str) -> Observation:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(text=value)
        if isinstance(value, Mapping):
            payload = dict(value)
            if "structured_data" in payload and "data" not in payload:
                payload["data"] = payload.pop("structured_data")
            return cls(**payload)
        raise TypeError(f"unsupported observation type: {type(value).__name__}")


@dataclass(frozen=True)
class SourceDocument:
    """One complete extracted source within an observed event."""

    source_type: str
    full_text: str = ""
    external_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    observations: Sequence[Observation | Mapping[str, Any] | str] = field(default_factory=tuple)

    @classmethod
    def coerce(cls, value: SourceDocument | Mapping[str, Any]) -> SourceDocument:
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            payload = dict(value)
            aliases = {
                "source_text": "full_text",
                "source_external_id": "external_id",
                "source_data": "data",
            }
            for old, new in aliases.items():
                if old in payload and new not in payload:
                    payload[new] = payload.pop(old)
            return cls(**payload)
        raise TypeError(f"unsupported source type: {type(value).__name__}")

    def normalized_observations(self) -> tuple[Observation, ...]:
        items = tuple(Observation.coerce(item) for item in self.observations)
        if items:
            return items
        if self.full_text:
            return (Observation(text=self.full_text, modality=self.source_type),)
        return ()


@dataclass(frozen=True)
class ObservationEnvelope:
    """All extracted data for one observed item and its idempotency boundary."""

    idempotency_key: str
    account_id: str = ""
    source_type: str = ""
    source_text: str = ""
    source_external_id: str = ""
    source_data: Mapping[str, Any] = field(default_factory=dict)
    observations: Sequence[Observation | Mapping[str, Any] | str] = field(default_factory=tuple)
    sources: Sequence[SourceDocument | Mapping[str, Any]] = field(default_factory=tuple)
    event_type: str = "observation"
    event_title: str = ""
    event_summary: str = ""
    speaker_actor_id: str = ""
    persona_id: str = ""
    scene: str = ""
    importance: float = 0.5
    occurred_at: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    job_types: Sequence[str] = field(default_factory=lambda: DEFAULT_JOB_TYPES)

    def normalized_observations(self) -> tuple[Observation, ...]:
        items = tuple(Observation.coerce(item) for item in self.observations)
        if items:
            return items
        if self.source_text:
            return (Observation(text=self.source_text, modality=self.source_type),)
        return ()

    def normalized_sources(self) -> tuple[SourceDocument, ...]:
        if self.sources:
            return tuple(SourceDocument.coerce(item) for item in self.sources)
        return (
            SourceDocument(
                source_type=self.source_type,
                full_text=self.source_text,
                external_id=self.source_external_id,
                data=self.source_data,
                observations=self.normalized_observations(),
            ),
        )


@dataclass(frozen=True)
class ArchiveResult:
    event_id: str
    source_id: str
    source_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    content_hash: str
    created: bool
    source_committed: bool = True
    fts_status: str = "ready"


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    job_type: str
    event_id: str | None
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    leased_until: float


@dataclass(frozen=True)
class HealthReport:
    ok: bool
    quick_check: str
    foreign_key_errors: tuple[Mapping[str, Any], ...]
    fts_ok: bool
    vector_ok: bool
    schema_version: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CleanupRecord:
    path: str
    status: str
    error: str = ""


@dataclass(frozen=True)
class BootstrapResult:
    stores: Mapping[str, Any]
    health: Mapping[str, HealthReport]
    cleanup: tuple[CleanupRecord, ...]
