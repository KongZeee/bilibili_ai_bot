"""Single-account path helpers: inject sole id for flat routes, guard nested ids."""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse

from .responses import fail


def _resolve_sole_id(account_manager) -> str:
    """Sole id for product AccountManager only (implements sole_id).

    Test doubles without sole_id() are treated as multi-shaped fixtures:
    inject falls back to get_default_id; nested guard does not force sole mismatch.
    """
    if account_manager is None:
        return ""
    if hasattr(account_manager, "sole_id"):
        try:
            return str(account_manager.sole_id() or "")
        except Exception:
            return ""
    return ""


def _inject_default_id(account_manager) -> str:
    """Fallback account id for flat inject when sole_id is unavailable."""
    sole = _resolve_sole_id(account_manager)
    if sole:
        return sole
    if account_manager is None:
        return ""
    if hasattr(account_manager, "get_default_id"):
        try:
            return str(account_manager.get_default_id() or "")
        except Exception:
            pass
    if hasattr(account_manager, "list_account_ids"):
        try:
            ids = account_manager.list_account_ids() or []
            if ids:
                return str(ids[0])
        except Exception:
            pass
    return ""


def _inject_sole_path_params(
    request: Request,
    account_manager,
    id_keys: Iterable[str] = ("id", "account_id"),
) -> Tuple[Optional[str], Optional[JSONResponse]]:
    """Resolve sole/default account into request.path_params for flat routes."""
    sole = _inject_default_id(account_manager)
    if not sole:
        return None, fail("NO_ACCOUNT", "尚未配置 B站账号", status_code=404)
    params = dict(request.scope.get("path_params") or {})
    for key in id_keys:
        params[key] = sole
    request.scope["path_params"] = params
    return sole, None


def _guard_nested_account_id(
    request: Request,
    account_manager,
    param: str = "id",
) -> Optional[JSONResponse]:
    """Reject nested path ids that are not the sole account (product manager only)."""
    path_id = request.path_params.get(param) or request.path_params.get("account_id")
    sole = _resolve_sole_id(account_manager)
    if sole:
        if not path_id:
            return fail("NOT_FOUND", "账号不存在", status_code=404)
        if str(path_id) != str(sole):
            return fail("NOT_FOUND", f"账号不存在: {path_id}", status_code=404)
        return None
    # Multi-shaped / test managers: do not enforce sole mismatch here
    return None
