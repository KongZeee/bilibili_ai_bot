"""
备份与恢复 API 路由（PRD V4 §5.7）

提供数据备份、恢复、列表、删除、下载功能：
- POST   /api/backup/create         — 创建备份
- GET    /api/backup/list           — 列出所有备份
- POST   /api/backup/restore        — 从备份恢复
- DELETE /api/backup/{name}         — 删除备份
- GET    /api/backup/{name}/download — 下载备份（zip）
"""
import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .responses import ok, fail, fail_not_found, fail_internal

logger = logging.getLogger("bilibot.api.backup")

# V6 不把任何 legacy memory 载体写入新备份，也不从旧备份恢复它们。
LEGACY_MEMORY_FILES = frozenset({
    "knowledge_base.db", "knowledge_base.db-wal", "knowledge_base.db-shm",
    "knowledge_base.db-journal", "vector_index.json", "vector_index.json.tmp",
    "memory.json", "permanent_memory.json", "chat_memory.json",
    "bangumi_memory.json", "bangumi_watch_log.json", "watch_log.json",
    "dynamic_log.json", "weekly_summary.json",
})


class RestoreValidationError(ValueError):
    """A backup cannot be restored without violating the V6 contract."""


class RestoreCommitError(RuntimeError):
    """A staged commit failed, optionally with an incomplete rollback."""

    def __init__(self, message: str, *, rollback_complete: bool) -> None:
        super().__init__(message)
        self.rollback_complete = bool(rollback_complete)


@dataclass(frozen=True)
class _StagedRestore:
    source: Path
    relative: Path
    staged: Path
    target: Path
    account_id: str = ""

    @property
    def is_memory_brain(self) -> bool:
        parts = self.relative.parts
        if (
            len(parts) == 3
            and parts[0] == "accounts"
            and parts[2].casefold() == "memory_brain.db"
        ):
            return True
        return (
            len(parts) == 2
            and parts[0] == "bot"
            and parts[1].casefold() == "memory_brain.db"
        )


def _collect_backup_entries(data_dir: Path) -> list[tuple[Path, Path]]:
    """Collect root/account state without traversing caches or backups.

    Args:
        data_dir: 数据目录

    Returns:
        ``(absolute source, relative archive path)`` entries.
    """
    files: list[tuple[Path, Path]] = []
    if not data_dir.is_dir():
        return files
    legacy_names = {name.casefold() for name in LEGACY_MEMORY_FILES}

    def add_directory(directory: Path, relative_root: Path) -> None:
        """Collect .db/.json files under directory (including one level of subdirs).

        Companion stores JSON under ``bot/companion/``; recurse one level so those
        files are included without walking deep caches.
        """
        if not directory.is_dir():
            return

        def maybe_add(source: Path, relative: Path) -> None:
            source_name = source.name.casefold()
            if not source.is_file() or source_name in legacy_names:
                return
            # V6 brains must not live at data root.
            if source_name == "memory_brain.db" and relative_root == Path("root"):
                return
            if source_name.endswith((".db-wal", ".db-shm", ".db-journal")):
                return
            if source.suffix.lower() not in {".db", ".json"}:
                return
            files.append((source, relative))

        # Skip nested product trees when scanning data root as "root"
        skip_dirs = set()
        if relative_root == Path("root"):
            skip_dirs = {"bot", "accounts", "backups", ".restore_stage", ".restore_rollback"}

        for source in sorted(directory.iterdir(), key=lambda item: item.name):
            if source.is_file():
                maybe_add(source, relative_root / source.name)
                continue
            if not source.is_dir():
                continue
            if source.name.casefold() in skip_dirs:
                continue
            # One-level subdirs (e.g. bot/companion/*.json)
            for child in sorted(source.iterdir(), key=lambda item: item.name):
                if child.is_file():
                    maybe_add(child, relative_root / source.name / child.name)

    add_directory(data_dir, Path("root"))
    bot_dir = data_dir / "bot"
    if bot_dir.is_dir():
        add_directory(bot_dir, Path("bot"))
    # Flat layout: if bot/ exists, skip packing legacy accounts/* to avoid dual-tree backups.
    # Legacy-only installs (no bot/) still pack accounts/{id}/ for restore remap.
    accounts_root = data_dir / "accounts"
    if accounts_root.is_dir() and not bot_dir.is_dir():
        for account_dir in sorted(accounts_root.iterdir(), key=lambda item: item.name):
            if account_dir.is_dir():
                add_directory(account_dir, Path("accounts") / account_dir.name)
    return files


def _sqlite_online_backup(source: Path, destination: Path) -> None:
    """Create a consistent SQLite snapshot, including live WAL contents."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    src_conn = sqlite3.connect(source_uri, uri=True, timeout=30)
    dst_conn = sqlite3.connect(str(destination), timeout=30)
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()


def _write_backup(data_dir: Path, backup_dir: Path) -> int:
    entries = _collect_backup_entries(data_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for source, relative in entries:
        destination = backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".db":
            _sqlite_online_backup(source, destination)
        else:
            shutil.copy2(source, destination)
    return len(entries)


def _collect_restore_entries(backup_dir: Path) -> list[tuple[Path, Path]]:
    """Return safe V6 restore entries and ignore legacy memory in old backups.

    Returns ``(source, archive_relative)`` pairs. Live targets are resolved later
    so legacy ``accounts/{id}/memory_brain.db`` archives still carry path identity
    while landing under flat ``bot/``.
    """
    entries: list[tuple[Path, Path]] = []
    legacy_names = {name.casefold() for name in LEGACY_MEMORY_FILES}
    for source in sorted(backup_dir.rglob("*")):
        if not source.is_file() or source.name.casefold() in legacy_names:
            continue
        relative = source.relative_to(backup_dir)
        parts = relative.parts
        is_canonical_brain = (
            (
                len(parts) == 3
                and parts[0] == "accounts"
                and parts[2].casefold() == "memory_brain.db"
            )
            or (
                len(parts) == 2
                and parts[0] == "bot"
                and parts[1].casefold() == "memory_brain.db"
            )
        )
        if source.name.casefold() == "memory_brain.db" and not is_canonical_brain:
            continue
        if parts and parts[0] == "root":
            target_rel = Path(*parts[1:])
        elif parts and parts[0] == "bot" and len(parts) >= 2:
            # bot/memory_brain.db or bot/companion/*.json etc.
            target_rel = relative
        elif parts and parts[0] == "accounts" and len(parts) >= 3:
            # accounts/{id}/file or accounts/{id}/subdir/file
            target_rel = relative
        elif len(parts) == 1:
            # Backwards-compatible non-memory root backup.
            target_rel = relative
        else:
            continue
        if source.suffix.lower() not in {".db", ".json"}:
            continue
        entries.append((source, target_rel))
    return entries


def _live_restore_target(data_dir: Path, relative: Path) -> Path:
    """Map an archive-relative path to the live flat layout destination.

    Flat product layout: all sole-account runtime files live under ``data/bot/``.
    Legacy archives under ``accounts/{id}/…`` are remapped into ``bot/…`` so
    restore does not re-create a nested dual tree.
    """
    parts = relative.parts
    if not parts:
        return data_dir / relative
    # Already flat bot/ archive
    if parts[0] == "bot":
        return data_dir.joinpath(*parts)
    # Legacy accounts/{id}/… → bot/… (drop account id segment)
    if parts[0] == "accounts" and len(parts) >= 2:
        rest = parts[2:] if len(parts) >= 3 else ()
        if rest:
            return data_dir.joinpath("bot", *rest)
        # accounts/{id} alone (shouldn't happen for files)
        return data_dir / "bot"
    # root/… or bare root files
    if parts[0] == "root":
        return data_dir.joinpath(*parts[1:]) if len(parts) > 1 else data_dir
    return data_dir / relative


def _memory_brain_account(relative: Path) -> str:
    """Return path-encoded account for legacy archives; empty for flat bot/."""
    parts = relative.parts
    if (
        len(parts) == 3
        and parts[0] == "accounts"
        and parts[2].casefold() == "memory_brain.db"
    ):
        return str(parts[1])
    return ""


def _stage_restore_entries(
    data_dir: Path,
    restore_entries: Iterable[tuple[Path, Path]],
    stage_root: Path,
) -> list[_StagedRestore]:
    """Materialize a restore set without touching any live destination."""
    data_root = data_dir.resolve()
    staged_entries: list[_StagedRestore] = []
    seen_targets: set[str] = set()

    for source, relative in restore_entries:
        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise RestoreValidationError(f"unsafe restore path: {relative}")
        target = _live_restore_target(data_dir, relative)
        try:
            target.parent.resolve().relative_to(data_root)
        except ValueError as exc:
            raise RestoreValidationError(
                f"restore path escapes data directory: {relative}"
            ) from exc

        target_key = os.path.normcase(str(target.resolve()))
        if target_key in seen_targets:
            raise RestoreValidationError(f"duplicate restore destination: {relative}")
        seen_targets.add(target_key)

        staged = stage_root / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".db":
            try:
                _sqlite_online_backup(source, staged)
            except sqlite3.Error as exc:
                raise RestoreValidationError(
                    f"SQLite restore candidate is unreadable: {relative}"
                ) from exc
        else:
            shutil.copy2(source, staged)
        staged_entries.append(
            _StagedRestore(
                source=source,
                relative=relative,
                staged=staged,
                target=target,
                account_id=_memory_brain_account(relative),
            )
        )
    return staged_entries


def _read_brain_identity(db_path: Path) -> dict[str, str]:
    """Read immutable identity markers before MemoryBrainStore may migrate a DB."""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_info'"
        ).fetchone()
        if table is None:
            raise RestoreValidationError("memory_brain.db is missing brain_info")
        rows = conn.execute(
            "SELECT key,value FROM brain_info WHERE key IN ('account_id','created_by')"
        ).fetchall()
        return {str(key): str(value) for key, value in rows}
    except sqlite3.Error as exc:
        raise RestoreValidationError(f"memory_brain.db identity is unreadable: {exc}") from exc
    finally:
        conn.close()


def _validate_staged_memory_brains(
    data_dir: Path,
    staged_entries: Iterable[_StagedRestore],
) -> tuple[str, ...]:
    """Validate every staged V6 brain against schema, account and path contracts."""
    from bilibot.memory_brain.bootstrap import account_db_path
    from bilibot.memory_brain.store import MemoryBrainStore

    account_ids: list[str] = []
    for entry in staged_entries:
        if not entry.is_memory_brain:
            continue

        identity = _read_brain_identity(entry.staged)
        account_id = entry.account_id or str(identity.get("account_id") or "")
        if not account_id:
            raise RestoreValidationError("memory brain identity is missing account_id")

        # Live layout is always flat bot/memory_brain.db.
        expected_target = account_db_path(data_dir, account_id)
        if entry.target.resolve() != expected_target.resolve():
            raise RestoreValidationError(
                f"memory brain target does not match account {account_id!r}"
            )

        if identity.get("created_by") != "memory_brain_v6":
            raise RestoreValidationError(
                f"memory brain for account {account_id!r} is not a V6 database"
            )
        if identity.get("account_id") != account_id:
            raise RestoreValidationError(
                "memory brain account mismatch: "
                f"archive={identity.get('account_id')!r}, path={account_id!r}"
            )

        # Health-check the staged file; flat bot/ skips path-encoded account match.
        store = MemoryBrainStore(entry.staged, account_id=account_id)
        health = store.health_check()
        if not health.ok:
            details = "; ".join(health.errors[:5]) or "unknown health failure"
            raise RestoreValidationError(
                f"memory brain for account {account_id!r} is unhealthy: {details}"
            )
        store.checkpoint("TRUNCATE")
        account_ids.append(account_id)
    return tuple(dict.fromkeys(account_ids))


def _configured_account_missing(account_manager: Any, account_id: str) -> bool:
    if account_manager is None:
        return False
    has_account = getattr(account_manager, "has_account", None)
    if callable(has_account):
        return not bool(has_account(account_id))
    list_ids = getattr(account_manager, "list_account_ids", None)
    if callable(list_ids):
        return account_id not in {str(item) for item in list_ids()}
    return False


def _active_memory_writers(account_manager: Any, account_ids: Iterable[str]) -> tuple[str, ...]:
    """Fail closed when runtime ownership cannot be ruled out."""
    ids = tuple(dict.fromkeys(str(item) for item in account_ids))
    if not ids:
        return ()
    if account_manager is None:
        return ids

    get_account = getattr(account_manager, "get_account", None)
    if not callable(get_account):
        return ids
    active: list[str] = []
    for account_id in ids:
        runtime = get_account(account_id)
        if runtime is None:
            continue
        brain = getattr(runtime, "memory_brain", None)
        if brain is None:
            brain = getattr(runtime, "memory_brain_service", None)
        # A retained service can still accept synchronous archive calls even after
        # its worker task stops, so existence is treated as active ownership.
        if brain is not None:
            active.append(account_id)
    return tuple(active)


def _replace_staged_entries(
    staged_entries: Iterable[_StagedRestore],
    rollback_root: Path,
) -> None:
    """Commit staged files with per-file atomic replacement and full rollback."""
    entries = list(staged_entries)
    moved_originals: dict[Path, Path] = {}
    moved_sidecars: dict[Path, Path] = {}
    touched: list[_StagedRestore] = []
    installed_targets: set[Path] = set()

    try:
        for entry in entries:
            entry.target.parent.mkdir(parents=True, exist_ok=True)
            touched.append(entry)
            original = rollback_root / "originals" / entry.relative
            original.parent.mkdir(parents=True, exist_ok=True)
            if entry.target.exists():
                os.replace(entry.target, original)
                moved_originals[entry.target] = original

            if entry.target.suffix.lower() == ".db":
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = Path(f"{entry.target}{suffix}")
                    if not sidecar.exists():
                        continue
                    saved = rollback_root / "sidecars" / entry.relative.parent / (
                        entry.relative.name + suffix
                    )
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(sidecar, saved)
                    moved_sidecars[sidecar] = saved

            os.replace(entry.staged, entry.target)
            installed_targets.add(entry.target)
    except Exception as commit_exc:
        rollback_errors: list[BaseException] = []
        for entry in reversed(touched):
            target = entry.target
            original = moved_originals.get(target)
            if target in installed_targets and target.exists():
                try:
                    discarded = rollback_root / "discarded" / entry.relative
                    discarded.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, discarded)
                except BaseException as rollback_exc:  # preserve the commit failure
                    rollback_errors.append(rollback_exc)
            if original is not None and original.exists():
                try:
                    os.replace(original, target)
                except BaseException as rollback_exc:
                    rollback_errors.append(rollback_exc)

        for sidecar, saved in moved_sidecars.items():
            try:
                if sidecar.exists():
                    discarded = rollback_root / "discarded_sidecars" / sidecar.name
                    discarded.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(sidecar, discarded)
                if saved.exists():
                    os.replace(saved, sidecar)
            except BaseException as rollback_exc:
                rollback_errors.append(rollback_exc)

        if rollback_errors:
            logger.critical(
                "restore rollback encountered %d filesystem errors",
                len(rollback_errors),
                exc_info=rollback_errors[0],
            )
        raise RestoreCommitError(
            "restore commit failed",
            rollback_complete=not rollback_errors,
        ) from commit_exc


def _count_files(directory: Path) -> int:
    """统计目录内文件数量"""
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.rglob("*") if p.is_file())


def _parse_timestamp_from_name(name: str) -> str:
    """从备份目录名解析时间戳

    支持格式：
        backup_YYYYMMDD_HHMMSS -> ISO 格式字符串
        其它名称 -> 返回空字符串（由调用方回退到目录 mtime）
    """
    prefix = "backup_"
    if name.startswith(prefix):
        ts_str = name[len(prefix):]
        try:
            dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
            return dt.isoformat()
        except ValueError:
            return ""
    return ""


def _backup_info(backup_dir: Path) -> dict:
    """构造单个备份的元数据字典"""
    name = backup_dir.name
    ts = _parse_timestamp_from_name(name)
    if not ts:
        # 自定义名称回退到目录修改时间
        ts = datetime.fromtimestamp(backup_dir.stat().st_mtime).isoformat()
    return {
        "name": name,
        # 不返回绝对路径，避免泄露主机目录结构
        "backup_timestamp": ts,
        "file_count": _count_files(backup_dir),
    }


def _sanitize_name(name: str) -> str:
    """清理备份名称，仅允许字母数字、下划线、连字符，防止路径穿越

    过滤后限制最大长度 64，避免过长名称导致的文件系统问题。
    """
    safe_name = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    safe_name = safe_name[:64]
    return safe_name


def _is_staging_backup_name(name: str) -> bool:
    """半成品 / 隐藏备份名：list/download/restore/delete 均应拒绝。"""
    n = str(name or "")
    return (not n) or n.startswith(".") or n.endswith(".creating")


def _cleanup_stale_staging(backups_root: Path, max_age_seconds: float = 3600.0) -> int:
    """删除过期的 ``.xxx.creating`` staging 目录（进程崩溃遗留）。

    仅清理名称符合 staging 约定且 mtime 超过 max_age 的目录；返回删除个数。
    """
    removed = 0
    try:
        if not backups_root.is_dir():
            return 0
        now = time.time()
        for item in backups_root.iterdir():
            if not item.is_dir():
                continue
            name = item.name
            if not (name.startswith(".") and name.endswith(".creating")):
                continue
            try:
                age = now - item.stat().st_mtime
            except OSError:
                continue
            if age < max_age_seconds:
                continue
            try:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
                logger.info("已清理过期备份 staging: %s age=%.0fs", name, age)
            except Exception as e:
                logger.warning("清理备份 staging 失败 %s: %s", name, e)
    except Exception as e:
        logger.debug("cleanup stale staging skipped: %s", e)
    return removed


def create_backup_routes(
    data_dir: str = "./data",
    *,
    account_manager: Any = None,
) -> list[Route]:
    """创建备份恢复 API 路由

    Args:
        data_dir: 数据目录路径
    """
    data_path = Path(data_dir)
    backups_root = data_path / "backups"
    restore_lock = asyncio.Lock()

    async def create_backup(request: Request) -> JSONResponse:
        """创建备份"""
        try:
            # 可选自定义名称
            custom_name = None
            try:
                body = await request.json()
                if isinstance(body, dict):
                    custom_name = body.get("name")
            except Exception:
                # 无请求体或非 JSON，使用默认时间戳命名
                pass

            now = datetime.now()
            if custom_name:
                safe_name = _sanitize_name(str(custom_name))
                if not safe_name:
                    return fail("INVALID_INPUT", "备份名称无效", status_code=400)
                backup_name = safe_name
            else:
                backup_name = f"backup_{now.strftime('%Y%m%d_%H%M%S')}"

            backup_dir = backups_root / backup_name
            if await asyncio.to_thread(backup_dir.exists):
                return fail(
                    "BACKUP_EXISTS",
                    f"备份已存在: {backup_name}",
                    details={"name": backup_name},
                    status_code=409,
                )

            def _do_create() -> int:
                # 先写到临时目录再原子 rename，避免半成品备份目录被 list/download
                backups_root.mkdir(parents=True, exist_ok=True)
                # 创建前顺带清理崩溃遗留的过期 staging，避免盘占满
                _cleanup_stale_staging(backups_root)
                stage = backups_root / f".{backup_name}.creating"
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)
                try:
                    count = _write_backup(data_path, stage)
                    if backup_dir.exists():
                        raise FileExistsError(backup_name)
                    os.replace(stage, backup_dir)
                    return count
                except Exception:
                    if stage.exists():
                        shutil.rmtree(stage, ignore_errors=True)
                    raise

            try:
                count = await asyncio.to_thread(_do_create)
            except FileExistsError:
                return fail(
                    "BACKUP_EXISTS",
                    f"备份已存在: {backup_name}",
                    details={"name": backup_name},
                    status_code=409,
                )

            logger.info(f"创建备份 {backup_name}，共 {count} 个文件")
            return ok(
                {
                    "name": backup_name,
                    "backup_timestamp": now.isoformat(),
                    "file_count": count,
                },
                message="备份创建成功",
            )
        except Exception as e:
            logger.error(f"创建备份失败: {e}", exc_info=True)
            return fail_internal()

    async def list_backups(request: Request) -> JSONResponse:
        """列出所有备份"""
        try:
            if not await asyncio.to_thread(backups_root.is_dir):
                return ok({"backups": []})

            def _do_list() -> list:
                # 顺带清理过期 staging（崩溃遗留的 .xxx.creating）
                _cleanup_stale_staging(backups_root)
                result = []
                # 按名称倒序排列（新的 backup_YYYYMMDD 在前）
                # 跳过半成品 staging（.name.creating）与隐藏目录，避免 list/download 半文件
                for item in sorted(backups_root.iterdir(), key=lambda p: p.name, reverse=True):
                    if not item.is_dir():
                        continue
                    name = item.name
                    if _is_staging_backup_name(name):
                        continue
                    result.append(_backup_info(item))
                return result

            backups = await asyncio.to_thread(_do_list)

            return ok({"backups": backups})
        except Exception as e:
            logger.error(f"列出备份失败: {e}", exc_info=True)
            return fail_internal()

    async def restore_backup(request: Request) -> JSONResponse:
        """从备份恢复"""
        async with restore_lock:
            try:
                try:
                    body = await request.json()
                except Exception:
                    return fail("INVALID_INPUT", "请求体必须是 JSON", status_code=400)

                if not isinstance(body, dict) or not body.get("name"):
                    return fail("INVALID_INPUT", "缺少 name 字段", status_code=400)

                name = str(body["name"])
                # 防止路径穿越：清理后必须与原值一致
                safe_name = _sanitize_name(name)
                if not safe_name or safe_name != name:
                    return fail("INVALID_INPUT", "备份名称无效", status_code=400)
                if _is_staging_backup_name(safe_name):
                    return fail("INVALID_INPUT", "备份名称无效", status_code=400)

                backup_dir = backups_root / safe_name
                if not await asyncio.to_thread(backup_dir.is_dir):
                    return fail_not_found(f"备份不存在: {safe_name}")

                # V6 only: legacy memory files in older backups are intentionally ignored.
                restore_files = await asyncio.to_thread(_collect_restore_entries, backup_dir)
                if not restore_files:
                    return fail("BACKUP_EMPTY", "备份中无可恢复文件", status_code=400)

                data_path.mkdir(parents=True, exist_ok=True)
                stage_root = Path(
                    tempfile.mkdtemp(prefix=".restore_stage_", dir=data_path)
                )
                rollback_root = Path(
                    tempfile.mkdtemp(prefix=".restore_rollback_", dir=data_path)
                )
                preserve_rollback = False
                try:
                    try:
                        staged = await asyncio.to_thread(
                            _stage_restore_entries,
                            data_path,
                            restore_files,
                            stage_root,
                        )
                        brain_accounts = await asyncio.to_thread(
                            _validate_staged_memory_brains, data_path, staged
                        )
                    except RestoreValidationError as exc:
                        return fail(
                            "RESTORE_VALIDATION_FAILED",
                            str(exc),
                            status_code=400,
                        )

                    missing_accounts = tuple(
                        account_id
                        for account_id in brain_accounts
                        if _configured_account_missing(account_manager, account_id)
                    )
                    if missing_accounts:
                        return fail(
                            "RESTORE_ACCOUNT_NOT_CONFIGURED",
                            "备份包含未配置账号的记忆脑库",
                            details={"account_ids": list(missing_accounts)},
                            status_code=409,
                        )

                    active_accounts = _active_memory_writers(
                        account_manager, brain_accounts
                    )
                    if active_accounts:
                        return fail(
                            "MEMORY_WRITER_ACTIVE",
                            "账号记忆写入器仍在运行，恢复已中止且未修改现有数据库",
                            details={"account_ids": list(active_accounts)},
                            status_code=409,
                        )

                    # Validation and writer ownership checks happen before the safety
                    # snapshot and before any live file is moved.
                    try:
                        safety_name = (
                            "pre_restore_"
                            + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        )
                        safety_dir = backups_root / safety_name
                        await asyncio.to_thread(_write_backup, data_path, safety_dir)
                        logger.info("恢复前安全备份已创建: %s", safety_name)
                    except Exception as exc:
                        logger.error(
                            "创建安全网备份失败，已中止恢复: %s",
                            exc,
                            exc_info=True,
                        )
                        return fail(
                            "SAFETY_BACKUP_FAILED",
                            "安全网备份创建失败，已中止恢复",
                            status_code=503,
                        )

                    try:
                        await asyncio.to_thread(
                            _replace_staged_entries, staged, rollback_root
                        )
                    except RestoreCommitError as exc:
                        preserve_rollback = not exc.rollback_complete
                        raise
                finally:
                    await asyncio.to_thread(shutil.rmtree, stage_root, True)
                    if preserve_rollback:
                        logger.critical(
                            "保留未完整回滚的恢复材料: %s",
                            rollback_root,
                        )
                    else:
                        await asyncio.to_thread(shutil.rmtree, rollback_root, True)

                restored = len(restore_files)
                logger.info(f"从备份 {safe_name} 恢复了 {restored} 个文件")
                return ok(
                    {"name": safe_name, "restored_files": restored},
                    message="恢复成功，建议重启服务以使数据库连接重新加载",
                )
            except Exception as e:
                logger.error(f"恢复备份失败: {e}", exc_info=True)
                return fail_internal()

    async def delete_backup(request: Request) -> JSONResponse:
        """删除指定备份"""
        try:
            name = str(request.path_params.get("name", ""))
            # 防止路径穿越：清理后必须与原值一致
            safe_name = _sanitize_name(name)
            if not safe_name or safe_name != name:
                return fail("INVALID_INPUT", "备份名称无效", status_code=400)
            if _is_staging_backup_name(safe_name):
                return fail("INVALID_INPUT", "备份名称无效", status_code=400)

            backup_dir = backups_root / safe_name
            if not await asyncio.to_thread(backup_dir.is_dir):
                return fail_not_found(f"备份不存在: {safe_name}")

            await asyncio.to_thread(shutil.rmtree, backup_dir)
            logger.info(f"已删除备份: {safe_name}")
            return ok({"deleted": safe_name}, message="备份已删除")
        except Exception as e:
            logger.error(f"删除备份失败: {e}", exc_info=True)
            return fail_internal()

    async def download_backup(request: Request):
        """下载指定备份（打包为 zip 流式返回）"""
        try:
            name = str(request.path_params.get("name", ""))
            safe_name = _sanitize_name(name)
            if not safe_name or safe_name != name:
                return fail("INVALID_INPUT", "备份名称无效", status_code=400)

            backup_dir = backups_root / safe_name
            if not await asyncio.to_thread(backup_dir.is_dir):
                return fail_not_found(f"备份不存在: {safe_name}")
            # 禁止下载 staging / 隐藏名（与 list 过滤一致）
            if _is_staging_backup_name(safe_name):
                return fail("INVALID_INPUT", "备份名称无效", status_code=400)

            files = await asyncio.to_thread(lambda: sorted(backup_dir.iterdir()))
            if not files:
                return fail_not_found("备份为空")

            # 先将 zip 写入临时文件，再分块流式返回，避免在内存中持有整个 zip
            # 临时 zip 使用 .creating 后缀，list 逻辑会当 staging 跳过
            tmp_zip = data_path / f".{safe_name}.download.zip.creating"
            try:
                if tmp_zip.exists():
                    tmp_zip.unlink()
            except OSError:
                pass
            try:
                def _do_zip() -> None:
                    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                        for f in files:
                            if f.is_file():
                                zf.write(f, f.relative_to(backup_dir).as_posix())
                            elif f.is_dir():
                                for nested in f.rglob("*"):
                                    if nested.is_file():
                                        zf.write(
                                            nested,
                                            nested.relative_to(backup_dir).as_posix(),
                                        )

                await asyncio.to_thread(_do_zip)

                def generate_zip():
                    try:
                        with open(tmp_zip, "rb") as fp:
                            while True:
                                chunk = fp.read(64 * 1024)
                                if not chunk:
                                    break
                                yield chunk
                    finally:
                        try:
                            tmp_zip.unlink()
                        except OSError:
                            pass

                headers = {
                    "Content-Disposition": f'attachment; filename="{safe_name}.zip"',
                }
                return StreamingResponse(
                    generate_zip(),
                    media_type="application/zip",
                    headers=headers,
                )
            except Exception:
                if tmp_zip.exists():
                    try:
                        tmp_zip.unlink()
                    except OSError:
                        pass
                raise
        except Exception as e:
            logger.error(f"下载备份失败: {e}", exc_info=True)
            return fail_internal()

    return [
        Route("/api/backup/create", create_backup, methods=["POST"]),
        Route("/api/backup/list", list_backups, methods=["GET"]),
        Route("/api/backup/restore", restore_backup, methods=["POST"]),
        Route("/api/backup/{name}/download", download_backup, methods=["GET"]),
        Route("/api/backup/{name}", delete_backup, methods=["DELETE"]),
    ]
