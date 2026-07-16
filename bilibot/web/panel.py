"""
Web 面板 - 修复版

基于 Starlette 的现代 Web 管理界面。
修复：鉴权中间件、CORS、会话过期

会话存储为进程内 dict，仅支持单进程（workers=1）。
多 worker / 多副本部署下会话不会共享，请勿横向扩展 Web 进程。
"""
import logging
import time
import json
import hashlib
import hmac
import random
import secrets
import bcrypt
from pathlib import Path
from typing import Optional, Dict, Any

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from ..services import PersonaStore
from ..prompts import PromptOrchestrator

logger = logging.getLogger("bilibot.web")

# 默认弱口令（明文或 bcrypt 哈希匹配均视为弱）
_DEFAULT_ADMIN_PASSWORD = "admin123"


def is_weak_admin_password(stored: str) -> bool:
    """检测 admin_password 是否为默认弱口令 admin123。

    支持明文与 bcrypt（$2a$/$2b$/$2y$）哈希：哈希时用 checkpw 比对明文 admin123。
    """
    if not isinstance(stored, str) or not stored:
        return False
    if stored == _DEFAULT_ADMIN_PASSWORD:
        return True
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(
                _DEFAULT_ADMIN_PASSWORD.encode("utf-8"),
                stored.encode("utf-8"),
            )
        except (ValueError, TypeError):
            return False
    return False


class NoCacheStaticFiles(StaticFiles):
    """Prevent stale ES modules from keeping an older control panel alive."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

# ═══════════════════════════════════════════════════════
# 认证中间件
# ═══════════════════════════════════════════════════════

class AuthMiddleware(BaseHTTPMiddleware):
    """统一鉴权中间件"""

    # 允许匿名访问的路径（/ 需登录；/static 见 _is_anon）
    # PRD V4 ACC-003：QR 登录端点不再匿名，需要管理员鉴权
    ANON_PATHS = {
        "/login",
        "/api/login",
        "/api/status/public",
    }

    def _is_anon(self, path: str) -> bool:
        if path.startswith("/static/"):
            return True
        return path in self.ANON_PATHS

    def _unauthorized(self, request: Request, code: str, message: str) -> JSONResponse | RedirectResponse:
        """API 返回 JSON 401；页面导航 302 到登录页。"""
        path = request.url.path
        accept = (request.headers.get("accept") or "").lower()
        wants_html = (
            not path.startswith("/api/")
            and ("text/html" in accept or request.method == "GET")
        )
        if wants_html:
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(
            {"success": False, "error": {"code": code, "message": message, "details": {}}},
            status_code=401,
        )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # BUG F-007：OPTIONS 预检请求直接放行，避免被 CORS 中间件之前拦截
        if request.method == "OPTIONS":
            return await call_next(request)

        # Task 32：CSRF 防护——非 GET 的 state-changing 请求必须携带自定义请求头
        # 浏览器同源策略保证：跨站 form/fetch 无法设置自定义头，从而阻止 CSRF；
        # 登录请求也需检查，前端 login.js 已统一带上该头。
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            if not request.headers.get("X-Requested-With"):
                return JSONResponse(
                    {"success": False, "error": {"code": "CSRF_TOKEN_MISSING", "message": "缺少 CSRF 请求头", "details": {}}},
                    status_code=403,
                )

        # 匿名路径直接放行
        if self._is_anon(path):
            return await call_next(request)

        # 检查 session token（BUG F-007：同时支持 cookie 和 Bearer token）
        token = request.cookies.get("token")
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
        if not token or token not in _sessions:
            return self._unauthorized(request, "UNAUTHORIZED", "请先登录")

        # 检查过期
        session = _sessions.get(token, {})
        login_ts = session.get("login_time", 0)
        ttl = _session_ttl
        if time.time() - login_ts > ttl:
            _sessions.pop(token, None)
            return self._unauthorized(request, "SESSION_EXPIRED", "会话已过期")

        # Task 30：滑动续期——长时间无活动则失效（idle_timeout，默认 15 分钟）
        last_activity = session.get("last_activity", login_ts)
        if time.time() - last_activity > _idle_timeout:
            _sessions.pop(token, None)
            return self._unauthorized(request, "SESSION_EXPIRED", "会话因长时间无活动已失效")
        # 刷新最后活动时间，实现滑动续期
        session["last_activity"] = time.time()

        # M23：轻量级会话清理（按概率触发，与会话数解耦）
        # 放在会话检查之后，避免提前删除过期会话导致 SESSION_EXPIRED 变为 UNAUTHORIZED
        if random.random() < 0.01:  # 1% 概率
            cleanup_sessions()

        return await call_next(request)


# ═══════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════

_sessions: Dict[str, dict] = {}
_session_ttl: int = 3600  # 默认 1h，由配置覆盖
_idle_timeout: int = 900  # Task 30：无活动超时（滑动续期），默认 15 分钟

# M22 登录频率限制：IP -> 失败时间戳列表
_login_attempts: Dict[str, list] = {}
_LOGIN_WINDOW_SECONDS: int = 300  # 5 分钟窗口
_LOGIN_MAX_FAILURES: int = 10     # 窗口内最大失败次数


def _set_session_ttl(seconds: int):
    global _session_ttl
    _session_ttl = max(60, seconds)


def cleanup_sessions() -> None:
    """M23：清理所有过期会话 + 单用户会话数限制（轻量级，调用方应自行限制频次）"""
    now = time.time()
    expired = [
        token for token, sess in _sessions.items()
        if now - sess.get("login_time", 0) > _session_ttl
        or now - sess.get("last_activity", sess.get("login_time", 0)) > _idle_timeout
    ]
    for token in expired:
        _sessions.pop(token, None)
    # Task 6：单用户最大活跃会话数限制（遍历所有用户）
    seen_users = set()
    for sess in _sessions.values():
        uname = sess.get("username", "")
        if uname not in seen_users:
            seen_users.add(uname)
            _enforce_user_session_limit(uname)


def _enforce_user_session_limit(username: str, max_per_user: int = 5) -> None:
    """Task 6：限制单用户最大活跃会话数，超过时删除最旧的

    Args:
        username: 目标用户名
        max_per_user: 单用户最大活跃会话数（默认 5）
    """
    user_sessions = [
        (sess.get("login_time", 0), token)
        for token, sess in _sessions.items()
        if sess.get("username", "") == username
    ]
    if len(user_sessions) > max_per_user:
        # 按 login_time 升序，删除最旧的
        user_sessions.sort(key=lambda x: x[0])
        for _, token in user_sessions[:len(user_sessions) - max_per_user]:
            _sessions.pop(token, None)


def _cleanup_login_attempts() -> None:
    """M22：清理过期的登录失败记录"""
    now = time.time()
    for ip in list(_login_attempts.keys()):
        _login_attempts[ip] = [
            ts for ts in _login_attempts[ip]
            if now - ts < _LOGIN_WINDOW_SECONDS
        ]
        if not _login_attempts[ip]:
            _login_attempts.pop(ip, None)


def create_web_app(
    config_loader,
    persona_store: PersonaStore,
    orchestrator: PromptOrchestrator,
    scheduler=None,
    config_path: str = "config.yaml",
    audit_store=None,
    context_builder=None,
    account_manager=None,
    llm_manager=None,
    safety_checker=None,
):
    """创建 Web 应用

    Args:
        audit_store: 应用级 AuditStore 单例（PRD V3 §10.2）。若为 None 则回退到本地构造，
                     但调用方应传入与生成流程共享的同一实例。
        context_builder: 应用级 ContextBuilder 单例（PRD V3 §8.2），保留参数以备 audit 预览使用。
    """

    # 读取会话 TTL
    try:
        raw = config_loader.get_raw_config()
        ttl = raw.get("web", {}).get("session_ttl_seconds", 3600)
        _set_session_ttl(int(ttl))
    except Exception:
        pass

    # 导入 API 路由
    from ..api import create_personas_routes, create_config_routes
    from ..api.replies import create_replies_routes
    from ..api.tasks import create_tasks_routes
    from ..api.backup import create_backup_routes
    from ..services import SafetyChecker

    # ═══════════════════════════════════════════════════════
    #  页面路由
    # ═══════════════════════════════════════════════════════

    async def dashboard_page(request: Request) -> HTMLResponse:
        """仪表盘页面"""
        return HTMLResponse(_get_dashboard_html())

    async def login_page(request: Request) -> HTMLResponse:
        """登录页面"""
        return HTMLResponse(_get_login_html())

    # ═══════════════════════════════════════════════════════
    #  认证 API
    # ═══════════════════════════════════════════════════════

    async def api_login(request: Request) -> JSONResponse:
        """登录"""
        from ..api.responses import fail, fail_internal
        client_ip = request.client.host if request.client else "unknown"
        try:
            # M22：IP 频率限制
            # Task 5：仅当直连 IP 属于可信代理时才信任 X-Forwarded-For，防止伪造绕过限流
            trusted_proxies = config_loader.web.trusted_proxies or []
            if trusted_proxies and client_ip in trusted_proxies:
                # 仅可信代理时才解析 x-forwarded-for
                xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                if xff:
                    client_ip = xff
            _cleanup_login_attempts()
            attempts = _login_attempts.get(client_ip, [])
            if len(attempts) >= _LOGIN_MAX_FAILURES:
                return JSONResponse(
                    {"success": False, "error": {"code": "TOO_MANY_REQUESTS", "message": "登录失败次数过多，请稍后再试", "details": {}}},
                    status_code=429,
                )

            body = await request.json()
            # Task 18：请求体类型校验
            if not isinstance(body, dict):
                return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
            username = str(body.get("username", ""))
            password = str(body.get("password", ""))

            # Task 16：密码哈希校验（支持 bcrypt，兼容明文）
            stored_password = config_loader.web.admin_password
            username_ok = hmac.compare_digest(username.encode(), config_loader.web.admin_username.encode())
            if stored_password.startswith("$2b$"):
                password_ok = bcrypt.checkpw(password.encode(), stored_password.encode())
            else:
                password_ok = hmac.compare_digest(password.encode(), stored_password.encode())
                if username_ok and password_ok:
                    logger.warning("admin_password 仍为明文存储，建议改为 bcrypt 哈希")

            if username_ok and password_ok:
                # Task 30：使用 secrets.token_urlsafe 生成 token，简化并保证密码学安全
                token = secrets.token_urlsafe(32)

                _sessions[token] = {
                    "username": username,
                    "login_time": time.time(),
                    "last_activity": time.time(),
                }
                # Task 6：单用户最大活跃会话数限制（5 个），删除最旧的
                _enforce_user_session_limit(username)
                # M23：登录成功后顺便清理过期会话（按概率触发，与会话数解耦）
                if random.random() < 0.01:
                    cleanup_sessions()
                # M22：登录成功，清除该 IP 的失败记录
                _login_attempts.pop(client_ip, None)

                # Task 30：token 仅通过 HttpOnly Cookie 下发，不再放入响应体
                resp = JSONResponse({"success": True})
                resp.set_cookie(
                    key="token", value=token,
                    httponly=True, max_age=_session_ttl,
                    secure=config_loader.web.secure_cookies,
                    samesite="lax",
                )
                return resp
            else:
                # M22：记录失败时间戳
                _login_attempts.setdefault(client_ip, []).append(time.time())
                return fail("INVALID_CREDENTIALS", "用户名或密码错误", status_code=401)
        except Exception as e:
            logger.error(f"登录处理异常: {e}", exc_info=True)
            # Task 18：异常路径也计入登录失败次数
            _login_attempts.setdefault(client_ip, []).append(time.time())
            return fail_internal()

    async def api_logout(request: Request) -> JSONResponse:
        """登出"""
        token = request.cookies.get("token") or ""
        # Task 21：同时支持 Cookie 和 Bearer Token
        if not token:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:]
        if token and token in _sessions:
            del _sessions[token]
        resp = JSONResponse({"success": True})
        # L14：delete_cookie 属性需与 set_cookie 一致，否则浏览器可能无法正确删除
        resp.delete_cookie(
            key="token",
            path="/",
            httponly=True,
            secure=config_loader.web.secure_cookies,
            samesite="lax",
        )
        return resp

    # ═══════════════════════════════════════════════════════
    #  系统状态
    # ═══════════════════════════════════════════════════════

    async def api_status_public(request: Request) -> JSONResponse:
        """公开健康状态（不泄露敏感信息）"""
        import bilibot
        return JSONResponse({
            "running": True,
            "version": bilibot.__version__,
        })

    async def api_status(request: Request) -> JSONResponse:
        """获取系统状态（需登录）

        多账号架构下聚合 account_manager / llm_manager 状态，
        不再只读顶层 V1 bilibili/llm 配置（否则控制台会误报异常）。
        """
        from ..api.responses import ok

        raw = config_loader.get_raw_config()
        pwd = raw.get("web", {}).get("admin_password", "")
        # 明文 admin123 或 bcrypt 哈希匹配 admin123 均视为弱口令
        is_default = is_weak_admin_password(str(pwd) if pwd is not None else "")
        cors_origins = raw.get("web", {}).get("cors_origins", [])
        cors_open = "*" in cors_origins if cors_origins else False

        # B站：任一已配置/已认证账号即视为可用
        bili_authenticated = False
        bili_uid = config_loader.bilibili.dede_user_id or ""
        if account_manager is not None:
            try:
                for st in account_manager.list_accounts():
                    if st.get("authenticated"):
                        bili_authenticated = True
                        bili_uid = st.get("uid") or st.get("dede_user_id") or bili_uid
                        break
            except Exception:
                bili_authenticated = config_loader.bilibili.is_authenticated
        else:
            bili_authenticated = config_loader.bilibili.is_authenticated

        # LLM：任一 enabled chat provider / 默认路由即可
        llm_connected = False
        llm_model = config_loader.llm.model
        llm_base = config_loader.llm.base_url
        if llm_manager is not None:
            try:
                providers = []
                if hasattr(llm_manager, "list_providers"):
                    providers = llm_manager.list_providers("chat") or llm_manager.list_providers() or []
                for p in providers:
                    if isinstance(p, dict) and p.get("enabled", True) and (
                        p.get("api_key") or p.get("api_keys") or p.get("has_api_key")
                    ):
                        llm_connected = True
                        llm_model = p.get("model") or llm_model
                        llm_base = p.get("base_url") or llm_base
                        break
                if not llm_connected and hasattr(llm_manager, "get_default"):
                    default_p = llm_manager.get_default()
                    if default_p is not None:
                        llm_connected = bool(getattr(default_p, "api_key", None) or getattr(default_p, "api_keys", None))
                        llm_model = getattr(default_p, "model", None) or llm_model
                        llm_base = getattr(default_p, "base_url", None) or llm_base
            except Exception:
                llm_connected = bool(config_loader.llm.api_key)
        else:
            llm_connected = bool(config_loader.llm.api_key)

        return ok({
            "running": True,
            "current_persona": persona_store.get_current_dict(),
            "bilibili": {
                "authenticated": bili_authenticated,
                "uid": bili_uid,
            },
            "llm": {
                "connected": llm_connected,
                "model": llm_model,
                "base_url": llm_base,
            },
            "web": {
                "enabled": config_loader.web.enabled,
                "port": config_loader.web.port,
            },
            "security": {
                "default_password": is_default,
                "cors_open": cors_open,
            },
            "config": config_loader.mask_sensitive(raw),
        })

    # ═══════════════════════════════════════════════════════
    #  B站登录（旧端点，PRD-V5 §5.2 ACC-503 标记为 410 Gone）
    #  新实现请使用 /api/accounts/{id}/qr-login 系列端点：
    #    - POST /api/accounts/{id}/qr-login             创建会话
    #    - GET  /api/accounts/{id}/qr-login/{sid}       轮询并定向写入
    #    - POST /api/accounts/{id}/qr-login/{sid}/cancel 取消会话
    # ═══════════════════════════════════════════════════════

    def _qrcode_gone_response(new_endpoint: str, message: str) -> JSONResponse:
        """ACC-503：旧 qrcode 端点统一返回 410 Gone"""
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "GONE",
                    "message": message,
                    "details": {"new_endpoint": new_endpoint},
                },
            },
            status_code=410,
            headers={"Deprecation": "true", "Sunset": "v2.1"},
        )

    async def api_get_qrcode(request: Request) -> JSONResponse:
        """[410 Gone] 获取 B站登录二维码，请改用 POST /api/accounts/{id}/qr-login"""
        return _qrcode_gone_response(
            "/api/accounts/{account_id}/qr-login",
            "This endpoint is deprecated. Use POST /api/accounts/{account_id}/qr-login instead.",
        )

    async def api_qrcode_status(request: Request) -> JSONResponse:
        """[410 Gone] 查询扫码状态，请改用 GET /api/accounts/{id}/qr-login/{session_id}"""
        return _qrcode_gone_response(
            "/api/accounts/{account_id}/qr-login/{session_id}",
            "This endpoint is deprecated. Use GET /api/accounts/{account_id}/qr-login/{session_id} instead.",
        )

    # ═══════════════════════════════════════════════════════
    #  路由列表
    # ═══════════════════════════════════════════════════════

    # 页面
    page_routes = [
        Route("/", dashboard_page, methods=["GET"]),
        Route("/login", login_page, methods=["GET"]),
    ]

    # 认证
    auth_routes = [
        Route("/api/login", api_login, methods=["POST"]),
        Route("/api/logout", api_logout, methods=["POST"]),
    ]

    # 系统（公开 + 需登录）
    system_routes = [
        Route("/api/status/public", api_status_public, methods=["GET"]),
        Route("/api/status", api_status, methods=["GET"]),
        Route("/api/bilibili/qrcode", api_get_qrcode, methods=["GET"]),
        Route("/api/bilibili/qrcode/status", api_qrcode_status, methods=["GET", "POST"]),
    ]

    # 人格
    persona_routes = create_personas_routes(persona_store, orchestrator, llm_manager)

    # 记忆（旧路由，标记废弃）—— 显式 data_dir 参数，PRD V3 §9.2
    # PRD-V5 §9.2 MEM-502：传入 account_manager 以解析默认账号数据目录
    from ..api.memory import create_memory_routes, create_account_memory_routes
    data_dir = config_loader.get("data_dir", "./data") if hasattr(config_loader, "get") else "./data"
    memory_routes = create_memory_routes(persona_store, scheduler, data_dir=data_dir, account_manager=account_manager)

    # PRD V4 MEM-009：账号化记忆路由（新实现，从 account_manager 解析数据目录）
    account_memory_routes = create_account_memory_routes(account_manager)

    # 日志（限制在 data_dir 下，防止路径穿越）
    from ..api.logs import create_logs_routes
    log_file = config_loader.get("logging.file", "./data/bililog.log") if hasattr(config_loader, "get") else "./data/bililog.log"
    logs_routes = create_logs_routes(log_file, data_dir=data_dir)

    # 审计：使用应用级单例，不另建（PRD V3 §10.2）
    from ..api.audit import create_audit_routes
    if audit_store is None:
        from ..services.audit_store import AuditStore
        audit_store = AuditStore(data_dir=data_dir)
    audit_routes = create_audit_routes(audit_store)

    # 回复审计（PRD §7）
    replies_routes = create_replies_routes(audit_store, account_manager=account_manager)

    # 任务触发（PRD §7）
    tasks_routes = create_tasks_routes(scheduler)

    # 备份恢复（PRD §5.7）
    backup_routes = create_backup_routes(data_dir, account_manager=account_manager)

    # 账号管理 + LLM 管理（PRD V2 多账号/多 LLM）
    accounts_routes = []
    llm_providers_routes = []
    dynamic_drafts_routes = []
    if account_manager is not None:
        from ..api.accounts import create_accounts_routes
        accounts_routes = create_accounts_routes(account_manager, config_loader, config_path)
        # PRD-V5 §4.1 DYN-501：动态草稿审核管理路由
        from ..api.dynamic_drafts import create_dynamic_drafts_routes
        dynamic_drafts_routes = create_dynamic_drafts_routes(account_manager, config_loader)
        # 陪伴生活层
        from ..api.companion import create_companion_routes
        companion_routes = create_companion_routes(account_manager)
    else:
        companion_routes = []
    if llm_manager is not None:
        from ..api.llm_providers import create_llm_providers_routes
        llm_providers_routes = create_llm_providers_routes(
            llm_manager, config_loader, config_path, account_manager=account_manager
        )

    # 视频理解配置
    from ..api.video_analysis import create_video_analysis_routes
    video_analysis_routes = create_video_analysis_routes(config_loader, config_path, account_manager=account_manager)

    # 文生图配置
    from ..api.image_generation import create_image_generation_routes
    image_generation_routes = create_image_generation_routes(config_loader, config_path)

    # 模型路由管理（V3 统一架构）
    model_routing_routes = []
    if llm_manager is not None:
        from ..api.model_routing import create_model_routing_routes
        model_routing_routes = create_model_routing_routes(llm_manager, config_loader, config_path)

    # Token 用量统计
    token_usage_routes = []
    try:
        from ..api.token_usage import create_token_usage_routes
        from ..services.token_usage import get_global_token_store
        token_usage_routes = create_token_usage_routes(
            token_store=get_global_token_store(),
            data_dir=data_dir,
        )
    except Exception as e:
        logger.warning("token usage routes 注册失败: %s", e)
        token_usage_routes = []

    # ───────────────────────────────────────────────────
    # 安全 API（PRD §5.9）：全局暂停 / 黑名单
    # PRD V4 BOOT-003：使用 App 层创建的 SafetyChecker，不再在 panel 中新建
    # ───────────────────────────────────────────────────
    if safety_checker is None:
        # 兼容旧调用方（如测试中直接调用 create_web_app 未传 safety_checker）
        from ..services.safety import build_safety_config
        raw = config_loader.get_raw_config()
        safety_checker = SafetyChecker(
            data_dir=data_dir,
            config=build_safety_config(raw),
        )

    # 配置
    # PRD V5 CFG-502：传入 safety_checker 以支持 immediate 字段热重载
    config_routes = create_config_routes(
        config_loader, config_path,
        account_manager=account_manager,
        safety_checker=safety_checker,
    )

    async def api_safety_pause_status(request: Request) -> JSONResponse:
        """获取全局暂停状态（PRD §5.9）"""
        from ..api.responses import ok
        return ok(safety_checker.get_pause_status())

    async def api_safety_pause(request: Request) -> JSONResponse:
        """全局暂停 Bot（PRD §5.9）"""
        from ..api.responses import ok, fail_invalid_input
        reason = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                reason = str(body.get("reason", ""))
        except Exception:
            pass
        safety_checker.pause(reason=reason)
        return ok(safety_checker.get_pause_status(), message="Bot 已暂停")

    async def api_safety_resume(request: Request) -> JSONResponse:
        """恢复 Bot 运行（PRD §5.9）"""
        from ..api.responses import ok
        safety_checker.resume()
        return ok(safety_checker.get_pause_status(), message="Bot 已恢复")

    async def api_safety_blacklist_list(request: Request) -> JSONResponse:
        """列出黑名单（PRD §5.9）"""
        from ..api.responses import ok
        return ok({"items": safety_checker.list_blacklist()})

    async def api_safety_blacklist_add(request: Request) -> JSONResponse:
        """添加黑名单（PRD §5.9）"""
        from ..api.responses import ok, fail_invalid_input
        try:
            body = await request.json()
        except Exception:
            return fail_invalid_input("请求体必须是 JSON")
        if not isinstance(body, dict) or not body.get("user_id"):
            return fail_invalid_input("缺少 user_id 字段")
        user_id = str(body["user_id"])
        reason = str(body.get("reason", ""))
        safety_checker.add_to_blacklist(user_id, reason=reason)
        return ok({"user_id": user_id, "reason": reason}, message="已添加黑名单")

    async def api_safety_blacklist_remove(request: Request) -> JSONResponse:
        """移除黑名单（PRD §5.9）"""
        from ..api.responses import ok, fail_invalid_input
        user_id = request.path_params.get("user_id", "")
        if not user_id:
            return fail_invalid_input("缺少 user_id")
        safety_checker.remove_from_blacklist(str(user_id))
        return ok({"user_id": user_id}, message="已移除黑名单")

    safety_routes = [
        Route("/api/safety/pause-status", api_safety_pause_status, methods=["GET"]),
        Route("/api/safety/pause", api_safety_pause, methods=["POST"]),
        Route("/api/safety/resume", api_safety_resume, methods=["POST"]),
        Route("/api/safety/blacklist", api_safety_blacklist_list, methods=["GET"]),
        Route("/api/safety/blacklist", api_safety_blacklist_add, methods=["POST"]),
        Route("/api/safety/blacklist/{user_id}", api_safety_blacklist_remove, methods=["DELETE"]),
    ]

    # PRD V4 BOOT-003：SafetyChecker 已在 App 层注入 Scheduler，
    # panel 不再覆盖 scheduler.safety_checker。

    # 组合所有路由
    all_routes = (
        page_routes +
        auth_routes +
        system_routes +
        persona_routes +
        config_routes +
        memory_routes +
        account_memory_routes +
        logs_routes +
        audit_routes +
        replies_routes +
        tasks_routes +
        backup_routes +
        safety_routes +
        accounts_routes +
        dynamic_drafts_routes +
        companion_routes +
        llm_providers_routes +
        video_analysis_routes +
        image_generation_routes +
        model_routing_routes +
        token_usage_routes
    )

    # 创建应用
    app = Starlette(
        routes=all_routes,
        debug=False,
    )

    # 静态文件
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", NoCacheStaticFiles(directory=str(static_dir)), name="static")

    # CORS：从配置读取 origins，默认空（不开放任意跨域，PRD V3 §4.3）
    cors_origins: list = []
    try:
        raw = config_loader.get_raw_config()
        cors_origins = raw.get("web", {}).get("cors_origins", []) or []
    except Exception:
        pass

    # M21：allow_credentials=True 时，CORS 不可使用通配符 "*"，否则浏览器会拒绝；
    # 同时通配符 + 凭证是危险组合（任何站点都能携带 cookie 跨域请求）。
    if "*" in cors_origins:
        logger.warning("cors_origins 含 '*' 且 allow_credentials=True，已移除通配符")
        cors_origins = [o for o in cors_origins if o != "*"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        allow_credentials=True,
    )

    # 鉴权中间件
    app.add_middleware(AuthMiddleware)

    return app


# ═══════════════════════════════════════════════════════
#  HTML 模板（HTML/CSS/JS 分离，PRD §11.4）
# ═══════════════════════════════════════════════════════

_LOGIN_HTML_CACHE: Optional[str] = None


def _get_login_html() -> str:
    """登录页面 HTML（从 templates/login.html 加载）"""
    global _LOGIN_HTML_CACHE
    if _LOGIN_HTML_CACHE is not None:
        return _LOGIN_HTML_CACHE
    template_path = Path(__file__).parent / "templates" / "login.html"
    try:
        _LOGIN_HTML_CACHE = template_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"加载登录页模板失败: {e}")
        _LOGIN_HTML_CACHE = "<h1>BiliBot 登录</h1><p>模板加载失败，请检查 templates/login.html</p>"
    return _LOGIN_HTML_CACHE


_STATIC_VERSION_CACHE: Dict[str, str] = {}


def _static_version(filename: str) -> str:
    """Task 30：基于文件内容 md5 生成静态资源版本号

    替代原先暴露文件 mtime 的方式，改用内容哈希前 8 位，避免泄露修改时间。
    结果带缓存，避免每次请求都读取文件。

    Args:
        filename: 相对 static 目录的路径，如 "css/tokens.css"

    Returns:
        版本号字符串（md5 前 8 位）；文件不存在时返回 "0"
    """
    cached = _STATIC_VERSION_CACHE.get(filename)
    if cached is not None:
        return cached
    try:
        path = Path(__file__).parent / "static" / filename
        content = path.read_bytes()
        v = hashlib.md5(content).hexdigest()[:8]
        _STATIC_VERSION_CACHE[filename] = v
        return v
    except Exception:
        return "0"


def _get_dashboard_html() -> str:
    """仪表盘 HTML - Vue 3 应用外壳"""
    vue_v = _static_version("vendor/vue.global.prod.js")
    app_v = _static_version("js/app.js")
    tokens_v = _static_version("css/tokens.css")
    base_v = _static_version("css/base.css")
    layout_v = _static_version("css/layout.css")
    comp_v = _static_version("css/components.css")
    # CSP：允许本站资源 + Google Fonts（display=swap 样式与字体文件）
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN" style="color-scheme: light dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#3b352b">
    <meta name="description" content="BiliBot B站 AI Bot 管理面板">
    <meta http-equiv="Content-Security-Policy" content="{csp}">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&display=swap" rel="stylesheet">
    <title>BiliBot - 控制台</title>
    <link rel="stylesheet" href="/static/css/tokens.css?v={tokens_v}">
    <link rel="stylesheet" href="/static/css/base.css?v={base_v}">
    <link rel="stylesheet" href="/static/css/layout.css?v={layout_v}">
    <link rel="stylesheet" href="/static/css/components.css?v={comp_v}">
</head>
<body>
    <a href="#app" class="skip-link">跳到主内容</a>
    <div id="app"></div>
    <noscript>此页面需要 JavaScript 支持，请启用 JavaScript 后访问。</noscript>
    <script src="/static/vendor/vue.global.prod.js?v={vue_v}"></script>
    <script type="module" src="/static/js/app.js?v={app_v}"></script>
</body>
</html>"""
