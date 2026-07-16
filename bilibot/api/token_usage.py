"""Token usage statistics API."""

from __future__ import annotations

import logging
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("bilibot.api.token_usage")


def create_token_usage_routes(token_store=None, data_dir: str = "./data") -> list:
    def _store():
        if token_store is not None:
            return token_store
        try:
            from bilibot.services.token_usage import get_global_token_store, TokenUsageStore
            s = get_global_token_store()
            if s is not None:
                return s
            # lazy init if app path skipped
            s = TokenUsageStore(data_dir=data_dir)
            from bilibot.services.token_usage import set_global_token_store
            set_global_token_store(s)
            return s
        except Exception as e:
            logger.warning("token store unavailable: %s", e)
            return None

    async def get_summary(request: Request) -> JSONResponse:
        store = _store()
        if store is None:
            return JSONResponse({
                "success": True,
                "data": {
                    "days": 7,
                    "today": {"total_tokens": 0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
                    "totals": {"total_tokens": 0, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0},
                    "daily": [],
                    "by_kind": [],
                    "by_scene": [],
                    "by_model": [],
                    "by_account": [],
                    "recent": [],
                    "note": "token store not initialized",
                },
            })
        try:
            days = int(request.query_params.get("days") or 7)
        except Exception:
            days = 7
        account_id = (request.query_params.get("account_id") or "").strip()
        try:
            data = store.summary(days=days, account_id=account_id)
            return JSONResponse({"success": True, "data": data})
        except Exception as e:
            logger.error("token summary failed: %s", e, exc_info=True)
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL", "message": "统计失败", "details": {}},
            }, status_code=500)

    async def get_today(request: Request) -> JSONResponse:
        store = _store()
        if store is None:
            return JSONResponse({"success": True, "data": {"total_tokens": 0, "calls": 0}})
        try:
            data = store.summary(days=1)
            return JSONResponse({"success": True, "data": data.get("today") or {}})
        except Exception as e:
            logger.error("token today failed: %s", e, exc_info=True)
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL", "message": "统计失败", "details": {}},
            }, status_code=500)

    return [
        Route("/api/token-usage/summary", get_summary, methods=["GET"]),
        Route("/api/token-usage/today", get_today, methods=["GET"]),
    ]
