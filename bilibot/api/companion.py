"""Companion life API routes (per-account)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .sole_account import _guard_nested_account_id, _inject_sole_path_params

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

    def _nested_guard(request: Request):
        return _guard_nested_account_id(request, account_manager, param="account_id")

    def _as_flat(handler):
        async def _flat(request: Request) -> JSONResponse:
            _, err = _inject_sole_path_params(
                request, account_manager, id_keys=("account_id",)
            )
            if err is not None:
                return err
            return await handler(request)

        return _flat

    async def get_state(request: Request) -> JSONResponse:
        err = _nested_guard(request)
        if err is not None:
            return err
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
        err = _nested_guard(request)
        if err is not None:
            return err
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        plan = companion.store.get_daily_plan()
        detail = companion.store.get_story_detail()
        return _ok({"plan": plan.to_dict(), "story_detail": detail.to_dict()})

    async def regenerate_plan(request: Request) -> JSONResponse:
        err = _nested_guard(request)
        if err is not None:
            return err
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
        err = _nested_guard(request)
        if err is not None:
            return err
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        items = [d.to_dict() for d in companion.store.get_diaries()]
        return _ok({"items": items, "total": len(items)})

    async def list_dreams(request: Request) -> JSONResponse:
        err = _nested_guard(request)
        if err is not None:
            return err
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
        err = _nested_guard(request)
        if err is not None:
            return err
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        items = [n.to_dict() for n in companion.store.get_explore_notes()]
        return _ok({"items": items, "total": len(items)})

    async def list_bookshelf(request: Request) -> JSONResponse:
        err = _nested_guard(request)
        if err is not None:
            return err
        account_id = request.path_params.get("account_id") or ""
        _, companion = _get_companion(account_id)
        if companion is None:
            return _err("账号不存在或陪伴层未初始化", "NOT_FOUND", 404)
        items = [p.to_dict() for p in companion.store.get_projects()]
        return _ok({"items": items, "total": len(items)})

    async def trigger_tick(request: Request) -> JSONResponse:
        """POST /api/accounts/{account_id}/companion/trigger

        信封契约（前端 toast 依赖）：
          success: true  → data.produced 表示是否真正产出；message 给人读
          success: false → error.message 给人读；HTTP 4xx/5xx
        探索失败不得 success:true + 无 produced 误导；未产出时 produced=false。
        """
        err = _nested_guard(request)
        if err is not None:
            return err
        account_id = request.path_params.get("account_id") or ""
        acc, companion = _get_companion(account_id)
        if acc is None:
            return _err(f"账号不存在: {account_id}", "NOT_FOUND", 404)
        if companion is None:
            return _err("陪伴层未初始化", "NOT_FOUND", 404)
        if not companion.enabled:
            return _err("陪伴层未启用（companion.enabled=false）", "DISABLED", 400)
        try:
            body = {}
            try:
                body = await request.json()
            except Exception:
                body = {}
            action = str((body or {}).get("action") or "tick").strip().lower()
            allowed = {"tick", "diary", "dream", "explore", "creative", "plan"}
            if action not in allowed:
                return _err(
                    f"不支持的 action: {action!r}，允许: {sorted(allowed)}",
                    "BAD_REQUEST",
                    400,
                )

            def _payload(produced: bool, item: Any, **extra: Any) -> dict:
                out = {
                    "produced": bool(produced),
                    "action": action,
                    "account_id": account_id,
                    "item": item,
                }
                out.update(extra)
                return out

            if action == "diary":
                data = await companion.generate_diary(force=True)
                produced = bool(data)
                return _ok(
                    _payload(produced, data.to_dict() if data else None),
                    message="日记已生成" if produced else "日记未生成",
                )
            if action == "dream":
                data = await companion.generate_dream(force=True)
                produced = bool(data)
                return _ok(
                    _payload(produced, data.to_dict() if data else None),
                    message="梦境已生成" if produced else "梦境未生成",
                )
            if action == "explore":
                data = await companion.maybe_explore(force=True)
                produced = bool(data)
                return _ok(
                    _payload(produced, data.to_dict() if data else None),
                    message=(
                        "探索已执行"
                        if produced
                        else "探索未产生结果（检查 web_search / 场景开关 / 冷却）"
                    ),
                )
            if action == "creative":
                data = await companion.maybe_advance_creative(force=True)
                produced = bool(data)
                return _ok(
                    _payload(produced, data.to_dict() if data else None),
                    message=(
                        "创作已推进"
                        if produced
                        else "创作未推进（检查 creative 开关 / 空闲条件）"
                    ),
                )
            if action == "plan":
                data = await companion.ensure_daily_plan(force=True)
                produced = bool(data and getattr(data, "items", None))
                return _ok(
                    _payload(produced, data.to_dict() if data else None),
                    message="日程已生成" if produced else "日程生成失败",
                )
            result = await companion.tick()
            # tick：有 actions 列表时以是否非空为准；否则看 ok；默认 produced=False 防误报
            produced = False
            item = result if isinstance(result, dict) else {"result": result}
            if isinstance(result, dict):
                actions = result.get("actions")
                if isinstance(actions, list):
                    produced = len(actions) > 0
                elif "ok" in result:
                    produced = bool(result.get("ok"))
                else:
                    # 未知结构：标记为已运行但未确认产出，由前端 info/warning 展示
                    produced = False
                    item = {**item, "ran": True}
            return _ok(
                _payload(produced, item if isinstance(item, dict) else {"result": item}),
                message="tick 完成" if produced else "tick 完成（本轮无额外产出）",
            )
        except Exception as e:
            logger.error("companion trigger failed: %s", e, exc_info=True)
            return _err("触发失败", "INTERNAL", 500)

    return [
        # Flat single-account shell
        Route("/api/companion/state", _as_flat(get_state), methods=["GET"]),
        Route("/api/companion/plan", _as_flat(get_plan), methods=["GET"]),
        Route("/api/companion/plan/regenerate", _as_flat(regenerate_plan), methods=["POST"]),
        Route("/api/companion/diaries", _as_flat(list_diaries), methods=["GET"]),
        Route("/api/companion/dreams", _as_flat(list_dreams), methods=["GET"]),
        Route("/api/companion/notes", _as_flat(list_notes), methods=["GET"]),
        Route("/api/companion/bookshelf", _as_flat(list_bookshelf), methods=["GET"]),
        Route("/api/companion/trigger", _as_flat(trigger_tick), methods=["POST"]),
        # Nested (kept; wrong id → 404 via sole guard)
        Route("/api/accounts/{account_id}/companion/state", get_state, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/plan", get_plan, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/plan/regenerate", regenerate_plan, methods=["POST"]),
        Route("/api/accounts/{account_id}/companion/diaries", list_diaries, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/dreams", list_dreams, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/notes", list_notes, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/bookshelf", list_bookshelf, methods=["GET"]),
        Route("/api/accounts/{account_id}/companion/trigger", trigger_tick, methods=["POST"]),
    ]
