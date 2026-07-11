"""
Web 面板 - 修复版

基于 Starlette 的现代 Web 管理界面。
修复：鉴权中间件、CORS、会话过期
"""
import logging
import time
import json
import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional, Dict, Any

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, HTMLResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from ..services import PersonaStore
from ..prompts import PromptOrchestrator

logger = logging.getLogger("bilibot.web")

# ═══════════════════════════════════════════════════════
# 认证中间件
# ═══════════════════════════════════════════════════════

class AuthMiddleware(BaseHTTPMiddleware):
    """统一鉴权中间件"""

    # 允许匿名访问的路径
    # PRD V4 ACC-003：QR 登录端点不再匿名，需要管理员鉴权
    ANON_PATHS = {
        "/", "/login",
        "/api/login", "/api/logout",
        "/api/status/public",
    }

    def _is_anon(self, path: str) -> bool:
        if path.startswith("/static/"):
            return True
        if path in self.ANON_PATHS:
            return True
        # /api/login 带 query 也放行
        if path.startswith("/api/login"):
            return True
        return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 匿名路径直接放行
        if self._is_anon(path):
            return await call_next(request)

        # 检查 session token
        token = request.cookies.get("token")
        if not token or token not in _sessions:
            return JSONResponse(
                {"success": False, "error": {"code": "UNAUTHORIZED", "message": "请先登录", "details": {}}},
                status_code=401,
            )

        # 检查过期
        session = _sessions.get(token, {})
        login_ts = session.get("login_time", 0)
        ttl = _session_ttl
        if time.time() - login_ts > ttl:
            _sessions.pop(token, None)
            return JSONResponse(
                {"success": False, "error": {"code": "SESSION_EXPIRED", "message": "会话已过期", "details": {}}},
                status_code=401,
            )

        return await call_next(request)


# ═══════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════

_sessions: Dict[str, dict] = {}
_session_ttl: int = 3600  # 默认 1h，由配置覆盖


def _set_session_ttl(seconds: int):
    global _session_ttl
    _session_ttl = max(60, seconds)


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
        try:
            body = await request.json()
            username = body.get("username", "")
            password = body.get("password", "")

            if username == config_loader.web.admin_username and password == config_loader.web.admin_password:
                token = hashlib.sha256(
                    f"{username}{secrets.token_hex(16)}{time.time()}".encode()
                ).hexdigest()

                _sessions[token] = {
                    "username": username,
                    "login_time": time.time(),
                }

                resp = JSONResponse({"success": True, "token": token})
                resp.set_cookie(
                    key="token", value=token,
                    httponly=True, max_age=_session_ttl,
                    secure=config_loader.web.secure_cookies,
                )
                return resp
            else:
                return fail("INVALID_CREDENTIALS", "用户名或密码错误", status_code=401)
        except Exception as e:
            return fail_internal(str(e))

    async def api_logout(request: Request) -> JSONResponse:
        """登出"""
        token = request.cookies.get("token")
        if token and token in _sessions:
            del _sessions[token]
        resp = JSONResponse({"success": True})
        resp.delete_cookie(key="token")
        return resp

    # ═══════════════════════════════════════════════════════
    #  系统状态
    # ═══════════════════════════════════════════════════════

    async def api_status_public(request: Request) -> JSONResponse:
        """公开健康状态（不泄露敏感信息）"""
        return JSONResponse({
            "running": True,
            "version": "2.0.0",
        })

    async def api_status(request: Request) -> JSONResponse:
        """获取系统状态（需登录）"""
        raw = config_loader.get_raw_config()
        pwd = raw.get("web", {}).get("admin_password", "")
        is_default = (pwd == "admin123")
        cors_origins = raw.get("web", {}).get("cors_origins", [])
        cors_open = "*" in cors_origins if cors_origins else False

        return JSONResponse({
            "running": True,
            "current_persona": persona_store.get_current_dict(),
            "bilibili": {
                "authenticated": config_loader.bilibili.is_authenticated,
                "uid": config_loader.bilibili.dede_user_id,
            },
            "llm": {
                "connected": bool(config_loader.llm.api_key),
                "model": config_loader.llm.model,
                "base_url": config_loader.llm.base_url,
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
    persona_routes = create_personas_routes(persona_store, orchestrator)

    # 记忆（旧路由，标记废弃）—— 显式 data_dir 参数，PRD V3 §9.2
    # PRD-V5 §9.2 MEM-502：传入 account_manager 以解析默认账号数据目录
    from ..api.memory import create_memory_routes, create_account_memory_routes
    data_dir = config_loader.get("data_dir", "./data") if hasattr(config_loader, "get") else "./data"
    memory_routes = create_memory_routes(persona_store, scheduler, data_dir=data_dir, account_manager=account_manager)

    # PRD V4 MEM-009：账号化记忆路由（新实现，从 account_manager 解析数据目录）
    account_memory_routes = create_account_memory_routes(account_manager)

    # 日志
    from ..api.logs import create_logs_routes
    log_file = config_loader.get("logging.file", "./data/bililog.log") if hasattr(config_loader, "get") else "./data/bililog.log"
    logs_routes = create_logs_routes(log_file)

    # 审计：使用应用级单例，不另建（PRD V3 §10.2）
    from ..api.audit import create_audit_routes
    if audit_store is None:
        from ..services.audit_store import AuditStore
        audit_store = AuditStore(data_dir=data_dir)
    audit_routes = create_audit_routes(audit_store)

    # 回复审计（PRD §7）
    replies_routes = create_replies_routes(audit_store)

    # 任务触发（PRD §7）
    tasks_routes = create_tasks_routes(scheduler)

    # 备份恢复（PRD §5.7）
    backup_routes = create_backup_routes(data_dir)

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
    if llm_manager is not None:
        from ..api.llm_providers import create_llm_providers_routes
        llm_providers_routes = create_llm_providers_routes(
            llm_manager, config_loader, config_path, account_manager=account_manager
        )

    # 视频理解配置
    from ..api.video_analysis import create_video_analysis_routes
    video_analysis_routes = create_video_analysis_routes(config_loader, config_path)

    # 文生图配置
    from ..api.image_generation import create_image_generation_routes
    image_generation_routes = create_image_generation_routes(config_loader, config_path)

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
        llm_providers_routes +
        video_analysis_routes +
        image_generation_routes
    )

    # 创建应用
    app = Starlette(
        routes=all_routes,
        debug=False,
    )

    # 静态文件
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # CORS：从配置读取 origins，默认空（不开放任意跨域，PRD V3 §4.3）
    cors_origins: list = []
    try:
        raw = config_loader.get_raw_config()
        cors_origins = raw.get("web", {}).get("cors_origins", []) or []
    except Exception:
        pass

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
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


def _static_version(filename: str) -> str:
    """UI-607：基于文件 mtime 自动生成静态资源版本号

    替代手动维护的 ?v=N，确保每次文件变更后浏览器缓存自动失效。

    Args:
        filename: 相对 static 目录的路径，如 "css/tokens.css"

    Returns:
        版本号字符串（mtime 整数）；文件不存在时返回 "0"
    """
    try:
        path = Path(__file__).parent / "static" / filename
        return str(int(os.path.getmtime(str(path))))
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
    return f"""<!DOCTYPE html>
<html lang="zh-CN" style="color-scheme: light dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="theme-color" content="#3b352b">
    <meta name="description" content="BiliBot B站 AI Bot 管理面板">
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
