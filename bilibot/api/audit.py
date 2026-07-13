"""
审计 API 路由
"""
import json
import logging
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .responses import ok, fail, fail_internal

logger = logging.getLogger("bilibot.api.audit")


def create_audit_routes(audit_store):
    async def list_generations(request: Request) -> JSONResponse:
        try:
            scene = request.query_params.get("scene")
            persona_id = request.query_params.get("persona_id")
            keyword = request.query_params.get("keyword")
            try:
                page = max(1, int(request.query_params.get("page", 1)))
                page_size = max(1, min(int(request.query_params.get("page_size", 20)), 100))
            except (ValueError, TypeError):
                return fail("INVALID_INPUT", "page/page_size 必须是正整数", status_code=400)
            offset = (page - 1) * page_size

            items = audit_store.query(
                scene=scene or None,
                persona_id=persona_id or None,
                keyword=keyword or None,
                limit=page_size,
                offset=offset,
            )
            return ok({"items": items, "page": page, "page_size": page_size})
        except Exception as e:
            logger.error(f"list_generations 操作失败: {e}", exc_info=True)
            return fail_internal()

    async def get_generation(request: Request) -> JSONResponse:
        try:
            aid = request.path_params.get("id")
            item = audit_store.get(aid)
            if not item:
                return fail("NOT_FOUND", "记录不存在", details={"id": aid}, status_code=404)
            return ok(item)
        except Exception as e:
            logger.error(f"get_generation 操作失败: {e}", exc_info=True)
            return fail_internal()

    async def get_audit_stats(request: Request) -> JSONResponse:
        try:
            stats = audit_store.stats()
            return ok(stats)
        except Exception as e:
            logger.error(f"get_audit_stats 操作失败: {e}", exc_info=True)
            return fail_internal()

    async def get_audit_analytics(request: Request) -> JSONResponse:
        try:
            data = audit_store.analytics()
            return ok(data)
        except Exception as e:
            logger.error(f"get_audit_analytics 操作失败: {e}", exc_info=True)
            return fail_internal()

    return [
        Route("/api/audit/generations", list_generations, methods=["GET"]),
        Route("/api/audit/generations/{id}", get_generation, methods=["GET"]),
        Route("/api/audit/stats", get_audit_stats, methods=["GET"]),
        Route("/api/audit/analytics", get_audit_analytics, methods=["GET"]),
    ]
