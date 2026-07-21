"""Migrate account-scoped data from ``accounts/{id}/`` to flat ``bot/``."""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Final, Mapping, TypedDict

from bilibot.memory_brain.bootstrap import (
    LAYOUT_VERSION,
    account_db_path,
    bot_data_dir,
    layout_marker_path,
    legacy_account_dir,
)

logger = logging.getLogger("bilibot.layout_migrate")

_MEMORY_BRAIN_NAME: Final = "memory_brain.db"
_SQLITE_SIDECARS: Final = (".db-wal", ".db-shm", ".db-journal")
_SHA256_MAX_BYTES: Final = 64 * 1024 * 1024  # size-match always; hash small files


class LayoutMigrateResult(TypedDict):
    status: str
    data_root: str
    sole_account_id: str
    source: str
    dest: str
    marker: str
    dry_run: bool
    force: bool
    prune_orphans: bool
    copied: list[str]
    skipped: list[str]
    pruned: list[str]
    errors: list[str]
    message: str


def _empty_result(
    data_root: Path,
    sole_account_id: str,
    *,
    dry_run: bool,
    force: bool,
    prune_orphans: bool,
) -> LayoutMigrateResult:
    dest = bot_data_dir(data_root)
    source = legacy_account_dir(data_root, sole_account_id)
    return {
        "status": "noop",
        "data_root": str(data_root.resolve()),
        "sole_account_id": sole_account_id,
        "source": str(source),
        "dest": str(dest),
        "marker": str(layout_marker_path(data_root)),
        "dry_run": dry_run,
        "force": force,
        "prune_orphans": prune_orphans,
        "copied": [],
        "skipped": [],
        "pruned": [],
        "errors": [],
        "message": "",
    }


def _dir_nonempty(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _verify_copy(source: Path, dest: Path, *, sqlite_online: bool = False) -> str | None:
    """Return an error message when copy verification fails, else None.

    SQLite online backups are byte-divergent from the source (WAL merge / page
    layout); for those we only require a non-empty dest and leave integrity to
    ``health_check``. Regular files get size + optional sha256.
    """
    if not dest.is_file():
        return f"destination missing after copy: {dest}"
    dst_size = dest.stat().st_size
    if sqlite_online:
        if dst_size <= 0:
            return f"sqlite backup produced empty file: {dest.name}"
        return None
    src_size = source.stat().st_size
    if src_size != dst_size:
        return f"size mismatch for {source.name}: src={src_size} dst={dst_size}"
    if src_size <= _SHA256_MAX_BYTES:
        src_hash = _file_sha256(source)
        dst_hash = _file_sha256(dest)
        if src_hash != dst_hash:
            return f"sha256 mismatch for {source.name}"
    return None


def _brain_healthy(db_path: Path, account_id: str) -> tuple[bool, str]:
    if not db_path.is_file():
        return False, "memory_brain.db missing"
    try:
        from bilibot.memory_brain.store import MemoryBrainStore

        store = MemoryBrainStore(db_path, account_id=account_id)
        report = store.health_check()
        if report.ok:
            return True, ""
        return False, "; ".join(report.errors[:5]) or "health_check failed"
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _marker_matches(marker: Path) -> bool:
    if not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == LAYOUT_VERSION
    except OSError:
        return False


def _write_marker(marker: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(LAYOUT_VERSION + "\n", encoding="utf-8")


def _iter_source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    if not source.is_dir():
        return files
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        name = path.name.casefold()
        if any(name.endswith(suffix) for suffix in _SQLITE_SIDECARS):
            continue
        files.append(path)
    return files


def _copy_file(source: Path, dest: Path, *, dry_run: bool) -> str | None:
    if dry_run:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".db":
        try:
            source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
            src_conn = sqlite3.connect(source_uri, uri=True, timeout=30)
            dst_conn = sqlite3.connect(str(dest), timeout=30)
            try:
                src_conn.backup(dst_conn)
                dst_conn.commit()
            finally:
                dst_conn.close()
                src_conn.close()
        except sqlite3.Error as exc:
            return f"sqlite backup failed for {source.name}: {exc}"
        return _verify_copy(source, dest, sqlite_online=True)
    shutil.copy2(source, dest)
    return _verify_copy(source, dest, sqlite_online=False)


def migrate_to_bot_layout(
    data_root: str | Path,
    sole_account_id: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    prune_orphans: bool = False,
) -> LayoutMigrateResult:
    """Copy ``accounts/{sole_account_id}/`` into ``bot/`` and write the layout marker.

    Idempotent when the flat-bot marker is present and the destination brain is healthy.
    Never deletes the source tree unless ``prune_orphans`` is true (and only after verify).
    """
    root = Path(data_root)
    result = _empty_result(
        root,
        sole_account_id,
        dry_run=dry_run,
        force=force,
        prune_orphans=prune_orphans,
    )
    source = Path(result["source"])
    dest = Path(result["dest"])
    marker = Path(result["marker"])
    dest_brain = account_db_path(root, sole_account_id)
    source_brain = source / _MEMORY_BRAIN_NAME

    # Idempotent fast path.
    if _marker_matches(marker):
        healthy, detail = _brain_healthy(dest_brain, sole_account_id)
        if healthy:
            result["status"] = "already_migrated"
            result["message"] = "flat-bot-v1 marker present and destination brain healthy"
            result["skipped"].append(str(dest_brain))
            return result
        if not force:
            result["status"] = "error"
            result["errors"].append(
                f"marker present but destination brain unhealthy: {detail}"
            )
            result["message"] = "refusing to overwrite without force"
            return result

    if not source.is_dir():
        # No legacy tree: ensure bot/ exists for bootstrap and write marker if brain ok.
        if dest_brain.is_file():
            healthy, detail = _brain_healthy(dest_brain, sole_account_id)
            if healthy:
                _write_marker(marker, dry_run=dry_run)
                result["status"] = "marker_only" if not dry_run else "would_marker_only"
                result["message"] = "no legacy source; destination brain already healthy"
                return result
            result["status"] = "error"
            result["errors"].append(f"no source and dest brain unhealthy: {detail}")
            result["message"] = "nothing to migrate"
            return result
        if dry_run:
            result["status"] = "would_create_empty"
            result["message"] = "no legacy source; would create empty bot/ for bootstrap"
            return result
        dest.mkdir(parents=True, exist_ok=True)
        result["status"] = "empty_ready"
        result["message"] = "no legacy source; bot/ ready for bootstrap"
        return result

    if _dir_nonempty(dest) and source.is_dir() and not force:
        # Allow re-entry when dest only has incomplete partial copies without marker.
        if _marker_matches(marker):
            result["status"] = "error"
            result["errors"].append("destination non-empty with marker but brain not ready")
            result["message"] = "refusing migrate without force"
            return result
        # If dest has any real content besides what we might re-copy, require force.
        existing = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()}
        if existing:
            result["status"] = "error"
            result["errors"].append(
                f"destination {dest} is non-empty ({len(existing)} files); pass force=True"
            )
            result["message"] = "refusing to clobber non-empty bot/ without force"
            return result

    files = _iter_source_files(source)
    if not files and not source_brain.is_file():
        result["status"] = "error"
        result["errors"].append(f"legacy source has no files: {source}")
        result["message"] = "empty source"
        return result

    for src_file in files:
        rel = src_file.relative_to(source)
        dst_file = dest / rel
        err = _copy_file(src_file, dst_file, dry_run=dry_run)
        if err:
            result["status"] = "error"
            result["errors"].append(err)
            result["message"] = "copy verification failed; source preserved"
            return result
        result["copied"].append(str(rel).replace("\\", "/"))

    if not dry_run:
        healthy, detail = _brain_healthy(dest_brain, sole_account_id)
        if not healthy:
            result["status"] = "error"
            result["errors"].append(f"post-copy brain unhealthy: {detail}")
            result["message"] = "source preserved; destination not marked"
            return result
        _write_marker(marker, dry_run=False)
    else:
        result["status"] = "would_migrate"
        result["message"] = f"would copy {len(result['copied'])} files and write marker"
        return result

    if prune_orphans:
        # Optional: remove other accounts/* trees after successful sole migrate.
        accounts_root = root.resolve() / "accounts"
        if accounts_root.is_dir():
            for entry in sorted(accounts_root.iterdir(), key=lambda item: item.name):
                if not entry.is_dir():
                    continue
                if entry.name == sole_account_id:
                    continue
                try:
                    shutil.rmtree(entry)
                    result["pruned"].append(str(entry))
                except OSError as exc:
                    result["errors"].append(f"prune failed {entry}: {exc}")

    result["status"] = "migrated"
    result["message"] = (
        f"migrated {len(result['copied'])} files from accounts/{sole_account_id} to bot/"
    )
    logger.info(
        "layout migrate %s sole=%s copied=%d",
        result["status"],
        sole_account_id,
        len(result["copied"]),
    )
    return result


def maybe_auto_migrate(
    data_root: str | Path,
    sole_account_id: str,
) -> LayoutMigrateResult | None:
    """Best-effort auto migrate for boot (never force, never prune).

    Returns None when there is nothing useful to do (no sole id / no legacy dir).
    Errors are logged and returned; callers must not raise.
    """
    account_id = str(sole_account_id or "").strip()
    if not account_id:
        return None
    root = Path(data_root)
    try:
        source = legacy_account_dir(root, account_id)
        dest = bot_data_dir(root)
        marker = layout_marker_path(root)
    except ValueError as exc:
        logger.warning("layout auto-migrate skipped: %s", exc)
        return None

    if _marker_matches(marker) and (dest / _MEMORY_BRAIN_NAME).is_file():
        return None
    if not source.is_dir():
        return None

    try:
        result = migrate_to_bot_layout(
            root,
            account_id,
            force=False,
            dry_run=False,
            prune_orphans=False,
        )
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        logger.error("layout auto-migrate failed: %s", exc, exc_info=True)
        return {
            **_empty_result(
                root,
                account_id,
                dry_run=False,
                force=False,
                prune_orphans=False,
            ),
            "status": "error",
            "errors": [f"{type(exc).__name__}: {exc}"],
            "message": "auto-migrate raised",
        }

    if result["status"] in {"migrated", "already_migrated", "marker_only", "empty_ready"}:
        logger.info("layout auto-migrate: %s — %s", result["status"], result["message"])
    elif result["status"] == "error":
        logger.error(
            "layout auto-migrate error: %s | %s",
            result["message"],
            "; ".join(result["errors"][:3]),
        )
    return result


def summarize_result(result: Mapping[str, object]) -> str:
    """Human-readable one-line summary for CLI output."""
    status = result.get("status", "?")
    message = result.get("message", "")
    errors = result.get("errors") or []
    if errors:
        return f"{status}: {message} ({'; '.join(str(e) for e in errors[:3])})"
    return f"{status}: {message}"
