"""Account database bootstrap, health-gated cutover, and legacy cleanup."""

from __future__ import annotations

from dataclasses import replace
import logging
import math
from pathlib import Path
import sqlite3
import stat
import struct
import time
from typing import Iterable, Sequence
import uuid

from .models import BootstrapResult, CleanupRecord, HealthReport
from .store import (
    MemoryBrainStore,
    build_fts_text,
    decode_vector,
    encode_vector,
    normalize_search_text,
    normalize_vector,
)

logger = logging.getLogger("bilibot.memory_brain.bootstrap")


LEGACY_MEMORY_FILENAMES = (
    "knowledge_base.db",
    "knowledge_base.db-wal",
    "knowledge_base.db-shm",
    "knowledge_base.db-journal",
    "vector_index.json",
    "vector_index.json.tmp",
    "memory.json",
    "permanent_memory.json",
    "chat_memory.json",
    "bangumi_memory.json",
    "bangumi_watch_log.json",
    "watch_log.json",
    "dynamic_log.json",
    "weekly_summary.json",
)

_CUTOVER_AUDIT_FILENAME = "memory_v6_cutover_audit.db"

LAYOUT_VERSION = "flat-bot-v1"
BOT_DIR_NAME = "bot"
_MEMORY_BRAIN_DB_NAME = "memory_brain.db"
_LAYOUT_MARKER_NAME = ".layout_version"


def _is_link_or_junction(path: Path) -> bool:
    """Return true for filesystem indirections that cleanup must not traverse."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _validate_account_id(account_id: str) -> str:
    value = str(account_id).strip()
    if not value or value in {".", ".."}:
        raise ValueError("account_id must be a non-empty path component")
    if any(character in value for character in ("/", "\\", "\x00", ":")):
        raise ValueError("account_id cannot contain path separators or a drive prefix")
    return value


def _resolved_data_root(data_root: str | Path) -> Path:
    raw_root = Path(data_root)
    if _is_link_or_junction(raw_root):
        raise ValueError("data_root cannot be a symlink or junction")
    return raw_root.resolve()


def bot_data_dir(data_root: str | Path) -> Path:
    """Return the flat sole-account data directory ``{data_root}/bot``."""

    root = _resolved_data_root(data_root)
    bot_dir = root / BOT_DIR_NAME
    if _is_link_or_junction(bot_dir):
        raise ValueError("bot data path cannot traverse a symlink or junction")
    if bot_dir.parent != root:
        raise ValueError("bot directory escaped the data root")
    return bot_dir


def account_data_dir(data_root: str | Path, account_id: str = "") -> Path:
    """Return the account data directory (always ``bot/`` under the flat layout)."""

    if account_id:
        _validate_account_id(account_id)
    return bot_data_dir(data_root)


def account_db_path(data_root: str | Path, account_id: str) -> Path:
    """Return the flat memory brain path ``{data_root}/bot/memory_brain.db``."""

    _validate_account_id(account_id)
    return bot_data_dir(data_root) / _MEMORY_BRAIN_DB_NAME


def layout_marker_path(data_root: str | Path) -> Path:
    """Return the layout version marker path under ``bot/``."""

    return bot_data_dir(data_root) / _LAYOUT_MARKER_NAME


def legacy_account_dir(data_root: str | Path, account_id: str) -> Path:
    """Return the pre-flat account directory ``{data_root}/accounts/{account_id}``."""

    root = _resolved_data_root(data_root)
    value = _validate_account_id(account_id)
    accounts_root = root / "accounts"
    account_dir = accounts_root / value
    if _is_link_or_junction(accounts_root) or _is_link_or_junction(account_dir):
        raise ValueError("legacy account path cannot traverse a symlink or junction")
    if account_dir.parent != accounts_root:
        raise ValueError("legacy account directory escaped the accounts directory")
    return account_dir


class _CutoverAuditLog:
    """Fallback audit for disk account directories without a configured brain."""

    def __init__(self, data_root: Path) -> None:
        self.path = data_root / _CUTOVER_AUDIT_FILENAME
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS legacy_cleanup_log (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    target_account_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('deleted','missing','failed')),
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    last_attempted_at REAL NOT NULL
                )"""
            )

    def log(
        self,
        record: CleanupRecord,
        *,
        filename: str,
        target_account_id: str,
    ) -> None:
        now = time.time()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO legacy_cleanup_log(
                    id,path,filename,target_account_id,status,error,attempts,last_attempted_at
                ) VALUES(?,?,?,?,?,?,1,?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename,
                    target_account_id=excluded.target_account_id,
                    status=excluded.status,
                    error=excluded.error,
                    attempts=legacy_cleanup_log.attempts+1,
                    last_attempted_at=excluded.last_attempted_at""",
                (
                    f"cleanup_{uuid.uuid4().hex}",
                    record.path,
                    filename,
                    target_account_id,
                    record.status,
                    record.error[:4000],
                    now,
                ),
            )


def _base_health_failure(exc: Exception) -> HealthReport:
    return HealthReport(
        ok=False,
        quick_check="error",
        foreign_key_errors=(),
        fts_ok=False,
        vector_ok=False,
        schema_version=0,
        errors=(f"{type(exc).__name__}: {exc}",),
    )


def _cutover_health_check(
    store: MemoryBrainStore,
    *,
    expected_db_path: Path,
    expected_account_id: str,
) -> HealthReport:
    """Run the destructive-cutover probes in addition to the store health check."""

    try:
        base = store.health_check()
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        base = _base_health_failure(exc)

    errors = list(base.errors)
    enhanced_fts_ok = False
    enhanced_vector_ok = False
    conn: sqlite3.Connection | None = None
    token = uuid.uuid4().hex
    fts_event_id = f"health_fts_{token}"
    archive_event_id = f"health_archive_{token}"
    try:
        if store.db_path.resolve() != expected_db_path.resolve():
            errors.append(
                f"database path is {store.db_path.resolve()}, expected {expected_db_path.resolve()}"
            )
        if store.account_id != expected_account_id:
            errors.append(
                f"store account is {store.account_id!r}, expected {expected_account_id!r}"
            )

        conn = store._connect()
        brain_account = conn.execute(
            "SELECT value FROM brain_info WHERE key='account_id'"
        ).fetchone()
        if not brain_account or brain_account["value"] != expected_account_id:
            actual = brain_account["value"] if brain_account else ""
            errors.append(
                f"brain_info account is {actual!r}, expected {expected_account_id!r}"
            )

        bvid = f"bv1v6{token[:12]}"
        # 用 probe 专用中文片段；MATCH 用 bigram（unicode61+jieba 索引不保证整词命中）
        chinese_token = f"切面探{token[:8]}"
        search_text = build_fts_text(
            f"统一{chinese_token}健康探针 {bvid}", stable_ids=(bvid,)
        )
        # bigrams from the unique probe prefix — present in build_fts_text output
        chinese_match = '"切面" OR "面探"'
        conn.execute("SAVEPOINT cutover_fts")
        try:
            conn.execute(
                "INSERT INTO memory_event_fts(event_id,search_text) VALUES(?,?)",
                (fts_event_id, search_text),
            )
            # 必须限定 event_id，否则库内其它命中会让 fetchone 拿到错误行
            chinese_hit = conn.execute(
                "SELECT event_id FROM memory_event_fts "
                "WHERE memory_event_fts MATCH ? AND event_id=? LIMIT 1",
                (chinese_match, fts_event_id),
            ).fetchone()
            bvid_hit = conn.execute(
                "SELECT event_id FROM memory_event_fts "
                "WHERE memory_event_fts MATCH ? AND event_id=? LIMIT 1",
                (f'"{normalize_search_text(bvid)}"', fts_event_id),
            ).fetchone()
            conn.execute("DELETE FROM memory_event_fts WHERE event_id=?", (fts_event_id,))
            deleted = conn.execute(
                "SELECT 1 FROM memory_event_fts WHERE event_id=?", (fts_event_id,)
            ).fetchone()
            enhanced_fts_ok = bool(
                chinese_hit
                and chinese_hit["event_id"] == fts_event_id
                and bvid_hit
                and bvid_hit["event_id"] == fts_event_id
                and deleted is None
            )
        finally:
            conn.execute("ROLLBACK TO cutover_fts")
            conn.execute("RELEASE cutover_fts")
        if not enhanced_fts_ok:
            errors.append("Chinese/BVID FTS insert-query-delete probe failed")

        vector = [3.0, 4.0, 0.0]
        blob, dimension = encode_vector(vector)
        decoded = decode_vector(blob, dimension)
        expected = normalize_vector(vector)
        dot = sum(left * right for left, right in zip(decoded, decoded))
        norm = math.sqrt(dot)
        enhanced_vector_ok = (
            dimension == 3
            and len(blob) == dimension * 4
            and blob == struct.pack("<3f", *expected)
            and abs(norm - 1.0) <= 1e-6
            and abs(dot - 1.0) <= 1e-6
        )
        if not enhanced_vector_ok:
            errors.append("little-endian normalized float32 BLOB/dot-product probe failed")

        now = time.time()
        source_id = f"health_source_{token}"
        observation_id = f"health_observation_{token}"
        chunk_id = f"health_chunk_{token}"
        job_id = f"health_job_{token}"
        probe_text = f"V6归档回滚健康探针 {token}"
        conn.execute("SAVEPOINT cutover_archive")
        try:
            conn.execute(
                """INSERT INTO memory_events(
                    id,idempotency_key,content_hash,event_type,source_type,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    archive_event_id,
                    f"health:{token}",
                    token,
                    "health_probe",
                    "health_probe",
                    now,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO memory_sources(
                    id,event_id,source_type,full_text,content_hash,created_at
                ) VALUES(?,?,?,?,?,?)""",
                (source_id, archive_event_id, "health_probe", probe_text, token, now),
            )
            conn.execute(
                """INSERT INTO memory_observations(
                    id,event_id,source_id,ordinal,modality,text,content_hash,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    observation_id,
                    archive_event_id,
                    source_id,
                    0,
                    "text",
                    probe_text,
                    token,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO memory_chunks(
                    id,event_id,source_id,observation_id,ordinal,observation_ordinal,text,
                    content_hash,start_char,end_char,char_count,token_count,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    chunk_id,
                    archive_event_id,
                    source_id,
                    observation_id,
                    0,
                    0,
                    probe_text,
                    token,
                    0,
                    len(probe_text),
                    len(probe_text),
                    len(probe_text),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO memory_event_fts(event_id,search_text) VALUES(?,?)",
                (archive_event_id, build_fts_text(probe_text)),
            )
            conn.execute(
                "INSERT INTO memory_chunk_fts(chunk_id,event_id,search_text) VALUES(?,?,?)",
                (chunk_id, archive_event_id, build_fts_text(probe_text)),
            )
            conn.execute(
                """INSERT INTO brain_jobs(
                    id,dedupe_key,job_type,event_id,available_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    job_id,
                    f"health:{token}",
                    "health_probe",
                    archive_event_id,
                    now,
                    now,
                    now,
                ),
            )
        finally:
            conn.execute("ROLLBACK TO cutover_archive")
            conn.execute("RELEASE cutover_archive")

        residue = any(
            conn.execute(query, params).fetchone() is not None
            for query, params in (
                ("SELECT 1 FROM memory_events WHERE id=?", (archive_event_id,)),
                ("SELECT 1 FROM memory_sources WHERE id=?", (source_id,)),
                ("SELECT 1 FROM memory_observations WHERE id=?", (observation_id,)),
                ("SELECT 1 FROM memory_chunks WHERE id=?", (chunk_id,)),
                ("SELECT 1 FROM memory_event_fts WHERE event_id=?", (archive_event_id,)),
                ("SELECT 1 FROM memory_chunk_fts WHERE chunk_id=?", (chunk_id,)),
                ("SELECT 1 FROM brain_jobs WHERE id=?", (job_id,)),
            )
        )
        if residue:
            errors.append("archive transaction rollback left probe content behind")
    except Exception as exc:
        errors.append(f"cutover probe {type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            conn.close()

    return replace(
        base,
        ok=not errors,
        fts_ok=base.fts_ok and enhanced_fts_ok,
        vector_ok=base.vector_ok and enhanced_vector_ok,
        errors=tuple(errors),
    )


def cleanup_legacy_memory_files(
    data_root: str | Path,
    stores: Iterable[MemoryBrainStore] = (),
    *,
    default_account_id: str | None = None,
) -> tuple[CleanupRecord, ...]:
    """Delete exact legacy names from root and immediate, non-linked account dirs."""

    raw_root = Path(data_root)
    if _is_link_or_junction(raw_root):
        raise ValueError("data_root cannot be a symlink or junction")
    root = raw_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    store_by_account = {
        str(store.account_id): store for store in tuple(stores) if str(store.account_id)
    }
    if default_account_id is not None:
        default_id = _validate_account_id(default_account_id)
        if default_id not in store_by_account:
            raise ValueError("default_account_id must name one of the supplied stores")
    else:
        default_id = next(iter(store_by_account), "")
    default_store = store_by_account.get(default_id)

    directories: list[tuple[Path, str]] = [(root, "")]
    bot_dir = root / BOT_DIR_NAME
    if not _is_link_or_junction(bot_dir) and bot_dir.is_dir():
        directories.append((bot_dir, default_id or BOT_DIR_NAME))
    accounts_root = root / "accounts"
    if not _is_link_or_junction(accounts_root) and accounts_root.is_dir():
        directories.extend(
            (entry, entry.name)
            for entry in sorted(accounts_root.iterdir(), key=lambda item: item.name)
            if not _is_link_or_junction(entry) and entry.is_dir()
        )

    records: list[CleanupRecord] = []
    fallback_audit: _CutoverAuditLog | None = None
    for directory, target_account_id in directories:
        owner = (
            default_store
            if directory == root
            else store_by_account.get(target_account_id)
        )
        for filename in LEGACY_MEMORY_FILENAMES:
            candidate = directory / filename
            normalized_path = str(candidate.absolute())
            if _is_link_or_junction(candidate):
                record = CleanupRecord(
                    normalized_path,
                    "failed",
                    "refused to delete symlink or junction",
                )
            else:
                try:
                    # Snapshot the file before deletion; if it changes between
                    # health-gate and unlink (a concurrent writer replaced it),
                    # refuse to delete instead of silently removing fresh data.
                    try:
                        before = candidate.lstat()
                    except FileNotFoundError:
                        before = None
                    if before is None:
                        record = CleanupRecord(normalized_path, "missing", "")
                    else:
                        try:
                            after = candidate.lstat()
                        except FileNotFoundError:
                            after = None
                        if after is None or (
                            before.st_mtime_ns, before.st_size
                        ) != (after.st_mtime_ns, after.st_size):
                            record = CleanupRecord(
                                normalized_path,
                                "failed",
                                "file changed during cleanup snapshot",
                            )
                        else:
                            candidate.unlink()
                            record = CleanupRecord(normalized_path, "deleted", "")
                except OSError as exc:
                    record = CleanupRecord(
                        normalized_path,
                        "failed",
                        f"{type(exc).__name__}: {exc}",
                    )
            records.append(record)
            if owner is not None:
                owner.log_legacy_cleanup(record.path, record.status, record.error)
            else:
                if fallback_audit is None:
                    fallback_audit = _CutoverAuditLog(root)
                fallback_audit.log(
                    record,
                    filename=filename,
                    target_account_id=target_account_id,
                )
    return tuple(records)


def bootstrap_accounts(
    data_root: str | Path,
    account_ids: Sequence[str],
    *,
    cleanup_legacy: bool = True,
    default_account_id: str | None = None,
) -> BootstrapResult:
    """Create and health-check every configured brain before irreversible cleanup."""

    unique_ids = list(dict.fromkeys(_validate_account_id(value) for value in account_ids))
    if not unique_ids:
        raise ValueError("at least one configured account is required")
    if default_account_id is not None:
        default_id = _validate_account_id(default_account_id)
        if default_id not in unique_ids:
            raise ValueError("default_account_id must be a configured account")
    else:
        default_id = unique_ids[0]

    # Flat sole-account layout: every configured id shares bot/memory_brain.db.
    # brain_info.account_id is the default/sole owner (not encoded in the path).
    # Extra configured ids (historical / disabled) are aliases for health-gate
    # compatibility only; only the default account may boot a runtime service.
    if len(unique_ids) > 1:
        logger.warning(
            "memory bootstrap: %s account ids configured; flat layout aliases "
            "all of them to the sole brain owned by %r. Only that account can "
            "instantiate a runtime MemoryBrainService.",
            len(unique_ids),
            default_id,
        )
    db_path = account_db_path(data_root, default_id)
    sole_store = MemoryBrainStore(db_path, account_id=default_id)
    stores: dict[str, MemoryBrainStore] = {
        account_id: sole_store for account_id in unique_ids
    }
    sole_health = _cutover_health_check(
        sole_store,
        expected_db_path=db_path,
        expected_account_id=default_id,
    )
    health = {account_id: sole_health for account_id in unique_ids}
    failed = {account_id: report for account_id, report in health.items() if not report.ok}
    if failed:
        details = "; ".join(
            f"{account_id}: {', '.join(report.errors)}" for account_id, report in failed.items()
        )
        raise RuntimeError(f"memory brain health gate failed; legacy files were preserved: {details}")
    try:
        pruned_jobs = sole_store.prune_finished_jobs()
        if pruned_jobs:
            logger.info("memory bootstrap pruned %s finished outbox jobs", pruned_jobs)
        pruned_traces = sole_store.prune_recall_traces()
        if pruned_traces:
            logger.info("memory bootstrap pruned %s old recall traces", pruned_traces)
    except Exception as exc:
        logger.warning("memory bootstrap prune failed: %s", exc)
    cleanup = (
        cleanup_legacy_memory_files(
            data_root,
            stores.values(),
            default_account_id=default_id,
        )
        if cleanup_legacy
        else ()
    )
    return BootstrapResult(stores=stores, health=health, cleanup=tuple(cleanup))
