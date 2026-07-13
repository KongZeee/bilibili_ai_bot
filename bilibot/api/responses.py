"""
API 响应工具（PRD V4 §4.8.1）

统一成功 / 失败响应格式：

成功：
    {"success": true, "message": "optional", "data": {}}

失败：
    {"success": false, "error": {"code": "STRING_CODE", "message": "...", "details": {}}}
"""
from typing import Any, Dict, Optional

from starlette.responses import JSONResponse


def ok(data: Any = None, message: str = "", status_code: int = 200) -> JSONResponse:
    """成功响应

    Args:
        data: 任意可 JSON 序列化数据
        message: 可选消息
        status_code: HTTP 状态码（默认 200）
    """
    body: Dict[str, Any] = {"success": True}
    if message:
        body["message"] = message
    body["data"] = data
    return JSONResponse(body, status_code=status_code)


def fail(
    code: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    status_code: int = 400,
) -> JSONResponse:
    """失败响应

    Args:
        code: 错误码字符串（如 "UNAUTHORIZED" / "NOT_FOUND"）
        message: 人类可读消息
        details: 可选详情
        status_code: HTTP 状态码（默认 400）
    """
    body = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        },
    }
    return JSONResponse(body, status_code=status_code)


# 常用错误码快捷构造
def fail_unauthorized(message: str = "请先登录") -> JSONResponse:
    return fail("UNAUTHORIZED", message, status_code=401)


def fail_session_expired() -> JSONResponse:
    return fail("SESSION_EXPIRED", "会话已过期", status_code=401)


def fail_not_found(message: str = "资源不存在") -> JSONResponse:
    return fail("NOT_FOUND", message, status_code=404)


def fail_internal(
    message: str = "内部服务器错误",
    code: str = "INTERNAL_ERROR",
    details: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    return fail(code, message, details=details, status_code=500)


def fail_invalid_input(message: str = "输入无效") -> JSONResponse:
    return fail("INVALID_INPUT", message, status_code=400)
