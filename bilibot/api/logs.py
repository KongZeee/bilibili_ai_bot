"""
日志 API 路由

- GET /api/logs?level=&keyword=&limit=
- GET /api/logs/download
"""
import asyncio
import logging
import os
import re
from datetime import datetime
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .responses import ok, fail

logger = logging.getLogger("bilibot.api.logs")

_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def _read_log_tail(path: str, n: int = 200) -> list[str]:
    """从文件尾部倒读 n 行"""
    BLOCK_SIZE = 8192
    lines: list[str] = []
    with open(path, "rb") as f:
        f.seek(0, 2)  # 到文件末尾
        pos = f.tell()
        while pos > 0 and len(lines) < n:
            read_size = min(BLOCK_SIZE, pos)
            pos -= read_size
            f.seek(pos)
            block = f.read(read_size).decode("utf-8", errors="replace")
            block_lines = block.split("\n")
            if lines:
                # 衔接上一个块的第一行
                block_lines[-1] = block_lines[-1] + lines[0]
                lines = lines[1:]
            lines = block_lines + lines
            # 只保留最后 n 行
            if len(lines) > n:
                lines = lines[-n:]
    return [l for l in lines if l.strip()]


def _read_log_file(path: str, level: str, keyword: str, limit: int) -> list[dict]:
    """从日志文件读取匹配的行"""
    results: list[dict] = []
    if not os.path.exists(path):
        return results

    try:
        lines = _read_log_tail(path, limit)
    except Exception:
        return results

    # 从尾部向前扫描
    matched = 0
    for line in reversed(lines):
        line = line.rstrip("\n")
        if not line:
            continue
        # 按 level 过滤
        if level:
            lvl = line.split("]")[0].split("[")[-1].strip() if "[" in line else ""
            if lvl.upper() != level.upper():
                continue
        # 关键词过滤
        if keyword and keyword.lower() not in line.lower():
            continue
        # 解析级别
        parsed_level = "INFO"
        for lv in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            if f"[{lv}]" in line:
                parsed_level = lv
                break
        ts_match = _TS_RE.match(line)
        ts = ts_match.group(1) if ts_match else ""
        results.append({"line": line, "level": parsed_level, "ts": ts})
        matched += 1
        if matched >= limit:
            break

    return list(reversed(results))


def create_logs_routes(log_file: str = "./data/bililog.log"):
    async def api_get_logs(request: Request) -> JSONResponse:
        level = request.query_params.get("level", "")
        keyword = request.query_params.get("keyword", "")
        try:
            limit = int(request.query_params.get("limit", 200))
        except (ValueError, TypeError):
            return fail("INVALID_INPUT", "limit 参数必须是整数", status_code=400)
        limit = min(limit, 2000)

        lines = await asyncio.to_thread(_read_log_file, log_file, level, keyword, limit)
        return ok(lines)

    async def api_download_logs(request: Request) -> PlainTextResponse:
        if not os.path.exists(log_file):
            return PlainTextResponse("日志文件不存在", status_code=404)
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(10 * 1024 * 1024)
        except Exception as e:
            logger.error(f"日志文件读取失败: {e}", exc_info=True)
            return PlainTextResponse("读取失败: 内部服务器错误", status_code=500)

        filename = f"bilibot_{datetime.now():%Y-%m-%d}.log"
        return PlainTextResponse(
            content,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    return [
        Route("/api/logs", api_get_logs, methods=["GET"]),
        Route("/api/logs/download", api_download_logs, methods=["GET"]),
    ]
