"""Account-scoped V6 associative memory brain."""

from .bootstrap import (
    LEGACY_MEMORY_FILENAMES,
    account_db_path,
    bootstrap_accounts,
    cleanup_legacy_memory_files,
)
from .gateway import EmbeddingBatch, MemoryModelGateway
from .ingestion import (
    bangumi_episode_observation,
    bot_action_observation,
    comment_observation,
    text_observation,
    video_observation,
)
from .models import (
    ArchiveResult,
    BootstrapResult,
    ClaimedJob,
    CleanupRecord,
    HealthReport,
    IdempotencyConflictError,
    MemoryBrainError,
    Observation,
    ObservationEnvelope,
    ProviderNotConfigured,
    ReingestBlockedError,
    SourceDocument,
    VectorDimensionError,
)
from .store import (
    MemoryBrainStore,
    build_fts_text,
    chunk_text,
    decode_vector,
    encode_vector,
    normalize_search_text,
    normalize_vector,
)
from .recall import RecallQuery, RecallResult
from .redaction import PrivateMessageRedactor, RedactionResult
from .service import MemoryBrainService
from .worker import PersistentMemoryWorker, WorkerRunReport

__all__ = [
    "ArchiveResult",
    "BootstrapResult",
    "ClaimedJob",
    "CleanupRecord",
    "EmbeddingBatch",
    "HealthReport",
    "IdempotencyConflictError",
    "LEGACY_MEMORY_FILENAMES",
    "MemoryBrainError",
    "MemoryBrainService",
    "MemoryBrainStore",
    "MemoryModelGateway",
    "Observation",
    "ObservationEnvelope",
    "PersistentMemoryWorker",
    "PrivateMessageRedactor",
    "ProviderNotConfigured",
    "ReingestBlockedError",
    "RecallQuery",
    "RecallResult",
    "RedactionResult",
    "SourceDocument",
    "VectorDimensionError",
    "WorkerRunReport",
    "account_db_path",
    "bangumi_episode_observation",
    "bootstrap_accounts",
    "build_fts_text",
    "bot_action_observation",
    "chunk_text",
    "comment_observation",
    "cleanup_legacy_memory_files",
    "decode_vector",
    "encode_vector",
    "normalize_search_text",
    "normalize_vector",
    "text_observation",
    "video_observation",
]
