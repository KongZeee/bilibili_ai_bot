"""回复审计 API 路由（PRD V4 §7）

提供 Bot 评论回复记录的查询：
- GET /api/replies                    列出回复记录（分页 + status 筛选）
- GET /api/replies/{reply_id}/context 查看某条回复使用的完整上下文摘要
"""
import json
import logging
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .responses import ok, fail, fail_not_found, fail_internal

logger = logging.getLogger("bilibot.api.replies")


def _parse_target(target_field: Any) -> Dict[str, Any]:
    """解析 target 字段（容忍 JSON 字符串 / dict / None）"""
    if not target_field:
        return {}
    if isinstance(target_field, dict):
        return target_field
    try:
        return json.loads(target_field)
    except Exception:
        return {}


def _has_failure_reason(item: Dict[str, Any]) -> bool:
    """判断审计记录是否带失败原因（target.failure_reason）"""
    target = _parse_target(item.get("target"))
    return bool(target.get("failure_reason"))


def create_replies_routes(audit_store) -> list[Route]:
    """创建回复相关 API 路由

    Args:
        audit_store: AuditStore 实例（与生成流程共享）
    """

    async def list_replies(request: Request) -> JSONResponse:
        try:
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
            status = request.query_params.get("status", "")

            # UI-606：将状态筛选下推到 SQL 层（AuditStore.list_by_status），
            # 用 WHERE + LIMIT/OFFSET 替代旧的「拉全量后内存过滤」。
            result = audit_store.list_by_status(
                scene="reply_comment",
                status=status,
                page=page,
                page_size=page_size,
            )

            return ok({
                "items": result["items"],
                "total": result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
            })
        except Exception as e:
            logger.exception("list_replies 失败")
            return fail_internal(str(e))

    async def get_reply_context(request: Request) -> JSONResponse:
        try:
            reply_id = request.path_params.get("reply_id")
            item = audit_store.get(reply_id)
            if not item:
                return fail_not_found("回复不存在")
            return ok({
                "reply": item,
                "context_summary": item.get("context_summary", ""),
                "prompt_preview": item.get("prompt_preview", ""),
            })
        except Exception as e:
            logger.exception("get_reply_context 失败")
            return fail_internal(str(e))

    return [
        Route("/api/replies", list_replies, methods=["GET"]),
        Route("/api/replies/{reply_id}/context", get_reply_context, methods=["GET"]),
    ]
