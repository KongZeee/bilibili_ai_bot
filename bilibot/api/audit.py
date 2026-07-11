"""
审计 API 路由
"""
import json
import logging
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("bilibot.api.audit")


def create_audit_routes(audit_store):
    async def list_generations(request: Request) -> JSONResponse:
        try:
            scene = request.query_params.get("scene")
            persona_id = request.query_params.get("persona_id")
            keyword = request.query_params.get("keyword")
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
            offset = (page - 1) * page_size

            items = audit_store.query(
                scene=scene or None,
                persona_id=persona_id or None,
                keyword=keyword or None,
                limit=page_size,
                offset=offset,
            )
            return JSONResponse({"success": True, "data": items, "page": page, "page_size": page_size})
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    async def get_generation(request: Request) -> JSONResponse:
        try:
            aid = request.path_params.get("id")
            item = audit_store.get(aid)
            if not item:
                return JSONResponse({
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "记录不存在", "details": {"id": aid}},
                }, status_code=404)
            return JSONResponse({"success": True, "data": item})
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    async def get_audit_stats(request: Request) -> JSONResponse:
        try:
            stats = audit_store.stats()
            return JSONResponse({"success": True, "data": stats})
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    async def get_audit_analytics(request: Request) -> JSONResponse:
        try:
            data = audit_store.analytics()
            return JSONResponse({"success": True, "data": data})
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    return [
        Route("/api/audit/generations", list_generations, methods=["GET"]),
        Route("/api/audit/generations/{id}", get_generation, methods=["GET"]),
        Route("/api/audit/stats", get_audit_stats, methods=["GET"]),
        Route("/api/audit/analytics", get_audit_analytics, methods=["GET"]),
    ]
