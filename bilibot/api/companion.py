"""Companion life API routes (per-account)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("bilibot.api.companion")


def _ok(data: Any = None, message: str = "") -> JSONResponse:
    body: dict = {"success": True, "data": data}
    if message:
        body["message"] = message
    return JSONResponse(body)


def _err(message: str, code: str = "BAD_REQUEST", status: int = 400) -> JSONResponse:
    return JSONResponse(
        {
            "success": False,
            "error": {"code": code, "message": message, "details": {}},
        },
        status_code=status,
    )


def create_companion_routes(account_manager) -> list:
    """Create companion life routes bound to AccountManager."""

    def _get_account(account_id: str):
        if account_manager is None:
            return None
        try:
            if hasattr(account_manager, "get_account"):
                return account_manager.get_account(account_id)
            if hasattr(account_manager, "get"):
                return account_manager.get(account_id)
            accounts = getattr(account_manager, "accounts", None)
            if isinstance(accounts, dict):
                return accounts.get(account_id)
        except Exception:
            return None
        return None

    def _get_companion(account_id: str):
        acc = _get_account(account_id)
        if acc is None:
            return None, None
        companion = getattr(acc, "companion", None)
        return acc, companion

    async def get_state(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        try:
            companion.reload_config()
            return _ok(companion.get_status_snapshot())
        except Exception as e:
            logger.error("get companion state failed: %s", e, exc_info=True)
            return _err("读取状态失败", "INTERNAL", 500)

    async def get_plan(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        plan = companion.store.get_daily_plan()
        detail = companion.store.get_story_detail()
        return _ok({"plan": plan.to_dict(), "story_detail": detail.to_dict()})

    async def regenerate_plan(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        if not companion.enabled:
            return _err("陪伴层未启用（companion.enabled=false）", "DISABLED", 400)
        try:
            plan = await companion.ensure_daily_plan(force=True)
            return _ok(plan.to_dict(), message="日程已重新生成")
        except Exception as e:
            logger.error("regenerate plan failed: %s", e, exc_info=True)
            return _err("生成失败", "INTERNAL", 500)

    async def list_diaries(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        items = [d.to_dict() for d in companion.store.get_diaries()]
        return _ok({"items": items, "total": len(items)})

    async def list_dreams(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        dream = companion.store.get_latest_dream()
        frags = [f.to_dict() for f in companion.store.get_dream_fragments()]
        return _ok({
            "latest": dream.to_dict() if dream else None,
            "fragments": frags,
        })

    async def list_notes(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        items = [n.to_dict() for n in companion.store.get_explore_notes()]
        return _ok({"items": items, "total": len(items)})

    async def list_bookshelf(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        items = [p.to_dict() for p in companion.store.get_projects()]
        return _ok({"items": items, "total": len(items)})

    async def trigger_tick(request: Request) -> JSONResponse:
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        if not companion.enabled:
            return _err("陪伴层未启用（companion.enabled=false）", "DISABLED", 400)
        try:
            body = {}
            try:
                body = await request.json()
            except Exception:
                body = {}
            action = str((body or {}).get("action") or "tick").strip().lower()
            if action == "diary":
                data = await companion.generate_diary(force=True)
                return _ok(data.to_dict() if data else None, message="日记已生成")
            if action == "dream":
                data = await companion.generate_dream(force=True)
                return _ok(data.to_dict() if data else None, message="梦境已生成")
            if action == "explore":
                data = await companion.maybe_explore(force=True)
                return _ok(data.to_dict() if data else None, message="探索已执行" if data else "探索未产生结果")
            if action == "creative":
                data = await companion.maybe_advance_creative(force=True)
                return _ok(data.to_dict() if data else None, message="创作已推进" if data else "创作未推进")
            if action == "plan":
                data = await companion.ensure_daily_plan(force=True)
                return _ok(data.to_dict() if data else None, message="日程已生成")
            result = await companion.tick()
            return _ok(result, message="tick 完成")
        except Exception as e:
            logger.error("companion trigger failed: %s", e, exc_info=True)
            return _err("触发失败", "INTERNAL", 500)

    return [
        Route("/api/accounts/{account_id}/companion/state", get_state, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/plan", get_plan, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/plan/regenerate", regenerate_plan, methods=["POST"]),
        Route("/api/accounts/{account_id}/companion/diaries", list_diaries, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/dreams", list_dreams, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/notes", list_notes, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/bookshelf", list_bookshelf, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/trigger", trigger_tick, methods=["POST"]),
    ]
