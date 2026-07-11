"""
备份与恢复 API 路由（PRD V4 §5.7）

提供数据备份、恢复、列表、删除功能：
- POST   /api/backup/create   — 创建备份
- GET    /api/backup/list     — 列出所有备份
- POST   /api/backup/restore  — 从备份恢复
- DELETE /api/backup/{name}   — 删除备份
"""
import logging
import shutil
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .responses import ok, fail, fail_not_found, fail_internal

logger = logging.getLogger("bilibot.api.backup")

# 需要备份的 glob 模式（数据库文件及其 WAL/SHM 伴随文件）
BACKUP_PATTERNS = ["*.db", "*.db-wal", "*.db-shm"]
# 需要备份的固定文件名
BACKUP_FILES = ["personas.json", "audit_backup.json"]


def _collect_backup_files(data_dir: Path) -> list[Path]:
    """收集 data_dir 下需要备份的文件列表

    Args:
        data_dir: 数据目录

    Returns:
        待备份文件 Path 列表
    """
    files: list[Path] = []
    if not data_dir.is_dir():
        return files
    for pattern in BACKUP_PATTERNS:
        files.extend(data_dir.glob(pattern))
    for name in BACKUP_FILES:
        p = data_dir / name
        if p.is_file():
            files.append(p)
    return files


def _count_files(directory: Path) -> int:
    """统计目录内文件数量"""
    if not directory.is_dir():
        return 0
    return sum(1 for p in directory.iterdir() if p.is_file())


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
        "directory": str(backup_dir),
        "backup_timestamp": ts,
        "file_count": _count_files(backup_dir),
    }


def _sanitize_name(name: str) -> str:
    """清理备份名称，仅允许字母数字、下划线、连字符，防止路径穿越"""
    return "".join(c for c in name if c.isalnum() or c in ("_", "-"))


def create_backup_routes(data_dir: str = "./data") -> list[Route]:
    """创建备份恢复 API 路由

    Args:
        data_dir: 数据目录路径
    """
    data_path = Path(data_dir)
    backups_root = data_path / "backups"

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
            if backup_dir.exists():
                return fail(
                    "BACKUP_EXISTS",
                    f"备份已存在: {backup_name}",
                    details={"name": backup_name},
                    status_code=409,
                )

            backups_root.mkdir(parents=True, exist_ok=True)
            backup_dir.mkdir(parents=True, exist_ok=True)

            # 复制文件，使用 copy2 保留元数据
            sources = _collect_backup_files(data_path)
            count = 0
            for src in sources:
                dst = backup_dir / src.name
                shutil.copy2(src, dst)
                count += 1

            logger.info(f"创建备份 {backup_name}，共 {count} 个文件")
            return ok(
                {
                    "name": backup_name,
                    "directory": str(backup_dir),
                    "backup_timestamp": now.isoformat(),
                    "file_count": count,
                },
                message="备份创建成功",
            )
        except Exception as e:
            logger.error(f"创建备份失败: {e}", exc_info=True)
            return fail_internal(f"创建备份失败: {e}")

    async def list_backups(request: Request) -> JSONResponse:
        """列出所有备份"""
        try:
            if not backups_root.is_dir():
                return ok({"backups": []})

            backups = []
            # 按名称倒序排列（新的 backup_YYYYMMDD 在前）
            for item in sorted(backups_root.iterdir(), key=lambda p: p.name, reverse=True):
                if item.is_dir():
                    backups.append(_backup_info(item))

            return ok({"backups": backups})
        except Exception as e:
            logger.error(f"列出备份失败: {e}", exc_info=True)
            return fail_internal(f"列出备份失败: {e}")

    async def restore_backup(request: Request) -> JSONResponse:
        """从备份恢复"""
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

            backup_dir = backups_root / safe_name
            if not backup_dir.is_dir():
                return fail_not_found(f"备份不存在: {safe_name}")

            # 恢复前先创建当前状态的自动备份（safety net）
            try:
                safety_name = f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                safety_dir = backups_root / safety_name
                safety_dir.mkdir(parents=True, exist_ok=True)
                for src in _collect_backup_files(data_path):
                    shutil.copy2(src, safety_dir / src.name)
                logger.info(f"恢复前安全备份已创建: {safety_name}")
            except Exception as e:
                logger.warning(f"创建安全网备份失败（继续恢复）: {e}")

            # 将备份文件复制回 data_dir（覆盖现有文件）
            restored = 0
            for src in backup_dir.iterdir():
                if src.is_file():
                    dst = data_path / src.name
                    shutil.copy2(src, dst)
                    restored += 1

            logger.info(f"从备份 {safe_name} 恢复了 {restored} 个文件")
            return ok(
                {"name": safe_name, "restored_files": restored},
                message="恢复成功",
            )
        except Exception as e:
            logger.error(f"恢复备份失败: {e}", exc_info=True)
            return fail_internal(f"恢复备份失败: {e}")

    async def delete_backup(request: Request) -> JSONResponse:
        """删除指定备份"""
        try:
            name = str(request.path_params.get("name", ""))
            # 防止路径穿越：清理后必须与原值一致
            safe_name = _sanitize_name(name)
            if not safe_name or safe_name != name:
                return fail("INVALID_INPUT", "备份名称无效", status_code=400)

            backup_dir = backups_root / safe_name
            if not backup_dir.is_dir():
                return fail_not_found(f"备份不存在: {safe_name}")

            shutil.rmtree(backup_dir)
            logger.info(f"已删除备份: {safe_name}")
            return ok({"deleted": safe_name}, message="备份已删除")
        except Exception as e:
            logger.error(f"删除备份失败: {e}", exc_info=True)
            return fail_internal(f"删除备份失败: {e}")

    return [
        Route("/api/backup/create", create_backup, methods=["POST"]),
        Route("/api/backup/list", list_backups, methods=["GET"]),
        Route("/api/backup/restore", restore_backup, methods=["POST"]),
        Route("/api/backup/{name}", delete_backup, methods=["DELETE"]),
    ]
