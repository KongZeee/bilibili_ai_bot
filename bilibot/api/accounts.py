"""
账号管理 API 路由（PRD V2 多账号架构）

提供：
- GET    /api/accounts              - 列出所有账号
- POST   /api/accounts              - 添加账号
- DELETE /api/accounts/{id}         - 删除账号
- PATCH  /api/accounts/{id}         - 更新账号配置
- POST   /api/accounts/{id}/set-default - 设为默认账号
- POST   /api/accounts/{id}/persona - 绑定人格
- POST   /api/accounts/{id}/llm     - 绑定 LLM
- POST   /api/accounts/{id}/start   - 启动账号
- POST   /api/accounts/{id}/stop    - 停止账号

PRD V4 ACC-003：二维码登录安全模型
- POST /api/accounts/{id}/qr-login            - 创建目标绑定的登录会话
- GET  /api/accounts/{id}/qr-login/{session}  - 鉴权轮询并定向写入
"""
import asyncio
import hashlib
import logging
import time
import uuid
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal

logger = logging.getLogger("bilibot.api.accounts")


_config_write_lock = None  # asyncio.Lock，延迟初始化（须在 event loop 中创建）


def _get_config_write_lock() -> "asyncio.Lock":
    """延迟初始化配置写锁（首次调用须在 event loop 中）"""
    global _config_write_lock
    if _config_write_lock is None:
        _config_write_lock = asyncio.Lock()
    return _config_write_lock


async def _save_accounts_to_config(config_loader, account_manager, config_path: str):
    """将账号配置持久化到 config.yaml（加锁防止并发读-改-写丢失修改）"""
    try:
        async with _get_config_write_lock():
            raw = config_loader.get_raw_config()
            accounts_dict = account_manager.save_to_config()
            raw["accounts"] = accounts_dict["accounts"]
            raw["default_account"] = accounts_dict["default_account"]
            raw["config_revision"] = int(raw.get("config_revision", 0)) + 1
            config_loader.save_config(raw, config_path)
            return True
    except Exception as e:
        logger.error(f"持久化账号配置失败: {e}")
        return False


def _validate_llm_id(llm_manager, llm_id: str):
    """PRD-V5 §5.3 LLM-501：校验 llm_id 指向已存在且 enabled 的 Provider

    Returns:
        None 如果校验通过，否则 (error_code, message) 元组
    """
    if not llm_id:
        return None  # 空 llm_id 允许（使用默认）
    # get_provider()/resolve_chat() intentionally falls back to the routed
    # default. Validation must inspect the exact configured provider instead,
    # otherwise an unknown ID is silently accepted as the default provider.
    providers = getattr(llm_manager, "_providers", None)
    if isinstance(providers, dict):
        provider = providers.get(llm_id)
    else:
        provider = llm_manager.get_provider(llm_id)
    if provider is None:
        return ("LLM_PROVIDER_NOT_FOUND", f"LLM Provider 不存在: {llm_id}")
    if not provider.enabled:
        return ("LLM_PROVIDER_NOT_FOUND", f"LLM Provider 已禁用: {llm_id}")
    return None


def _enrich_llm_status(status: dict, llm_manager):
    """PRD-V5 §5.3 LLM-501：为无运行时实例的账号状态补充 LLM 字段

    运行时账号的 get_status() 已包含 configured/effective/fallback 字段，
    但禁用或未初始化账号的状态由 AccountManager 构造（不含这些字段），
    此函数在 API 层补充。
    """
    if "configured_llm_id" not in status:
        configured = status.get("llm_id", "")
        status["configured_llm_id"] = configured
        status["effective_llm_id"] = ""
        status["fallback_reason"] = ""
    return status


# ═══════════════════════════════════════════════════════
#  PRD-V5 §5.2 ACC-503：二维码登录会话闭环 — 会话存储与工具
# ═══════════════════════════════════════════════════════

# 会话模型: qr_session_id -> {account_id, creator_session_hash, qrcode_key_hash, status, expire_at, created_at}
# 不含 raw qrcode_key — 仅存 hash（满足 ACC-503 安全模型）
_qr_sessions: dict = {}

# 原始 qrcode_key 临时存储（仅用于轮询 B站 API，不入会话模型、不入日志）
_qr_session_keys: dict = {}

_QR_SESSION_TTL = 180  # 秒
_QR_TERMINAL_STATUSES = ("confirmed", "expired", "cancelled")
_QR_TERMINAL_RETENTION = 300  # 终态会话保留 5 分钟（用于 410 响应后清理）

_qr_lock = None  # asyncio.Lock，延迟初始化（须在 event loop 中创建）


def _get_qr_lock() -> "asyncio.Lock":
    """延迟初始化 QR 会话锁（首次调用须在 event loop 中）"""
    global _qr_lock
    if _qr_lock is None:
        _qr_lock = asyncio.Lock()
    return _qr_lock


def _extract_admin_token(request: Request) -> str:
    """从请求中提取管理员会话 token

    优先级：
    1. Authorization: Bearer <token> 头（API 客户端）
    2. token cookie（Web 面板）
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("token", "")


def _sha256_hex(value: str) -> str:
    """计算 SHA-256 十六进制摘要"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _short(value: str, n: int = 8) -> str:
    """截断敏感值用于日志（只保留前 n 位）"""
    if not value:
        return ""
    return value[:n] + "..." if len(value) > n else value


def _invalidate_qr_session(session_id: str, status: str):
    """标记会话为终态并移除临时 key（调用方须持有 _qr_lock）"""
    sess = _qr_sessions.get(session_id)
    if sess is not None:
        sess["status"] = status
        sess["terminal_at"] = time.time()
    _qr_session_keys.pop(session_id, None)


async def _cleanup_terminal_qr_sessions():
    """清理超过保留期的终态会话（防止内存泄漏）

    终态会话（confirmed/expired/cancelled）保留 _QR_TERMINAL_RETENTION 秒
    用于 410 响应，超期后删除。
    """
    now = time.time()
    async with _get_qr_lock():
        for sid in list(_qr_sessions.keys()):
            sess = _qr_sessions[sid]
            terminal_at = sess.get("terminal_at")
            if terminal_at is not None and (now - terminal_at) > _QR_TERMINAL_RETENTION:
                _qr_sessions.pop(sid, None)
                _qr_session_keys.pop(sid, None)


def create_accounts_routes(account_manager, config_loader, config_path: str = "config.yaml"):
    """创建账号管理路由"""
    # PRD-V5 §5.3 LLM-501：从 account_manager 获取 llm_manager 用于 Provider 校验
    llm_manager = account_manager.llm_manager

    async def list_accounts(request: Request) -> JSONResponse:
        statuses = account_manager.list_accounts()
        for s in statuses:
            _enrich_llm_status(s, llm_manager)
        return ok(statuses)

    async def get_account(request: Request) -> JSONResponse:
        acc_id = request.path_params.get("id")
        if not account_manager.has_account(acc_id):
            return fail("NOT_FOUND", f"账号不存在: {acc_id}", status_code=404)
        status = account_manager.get_account_status(acc_id)
        _enrich_llm_status(status, llm_manager)
        return ok(status)

    async def add_account(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            acc_id = body.get("id") or ""
            if not acc_id:
                return fail("VALIDATION_ERROR", "账号 ID 不能为空")
            if acc_id in account_manager:
                return fail("DUPLICATE_ID", f"账号 ID 已存在: {acc_id}")
            # PRD-V5 §5.3 LLM-501：校验 llm_id 指向已存在且 enabled 的 Provider
            llm_id = body.get("llm_id", "")
            err = _validate_llm_id(llm_manager, llm_id)
            if err:
                return fail(err[0], err[1])
            account_manager.add_account(body)
            await _save_accounts_to_config(config_loader, account_manager, config_path)
            # ACC-501：账号可能 enabled=false（无运行时实例），用 get_account_status
            status = account_manager.get_account_status(acc_id)
            _enrich_llm_status(status, llm_manager)
            return ok(status, "账号添加成功")
        except ValueError as e:
            logger.warning("添加账号校验失败: %s", e)
            return fail("VALIDATION_ERROR", "账号参数不合法")
        except Exception as e:
            logger.error(f"添加账号失败: {e}", exc_info=True)
            return fail_internal()

    async def delete_account(request: Request) -> JSONResponse:
        acc_id = request.path_params.get("id")
        if acc_id == account_manager.get_default_id() and len(account_manager) <= 1:
            return fail("LAST_ACCOUNT", "不能删除最后一个默认账号")
        # ACC-501：显式 DELETE 操作，从配置注册表 + 运行时实例删除，写审计
        removed = await account_manager.remove_account_async(acc_id)
        if not removed:
            return fail("NOT_FOUND", f"账号不存在: {acc_id}")
        await _save_accounts_to_config(config_loader, account_manager, config_path)
        return ok(message="账号已删除")

    async def update_account(request: Request) -> JSONResponse:
        try:
            acc_id = request.path_params.get("id")
            # ACC-501：同步配置注册表（拾取 qrlogin 等外部写入）
            account_manager.sync_registry_from_config()
            # ACC-501：检查账号存在（含禁用账号）
            if not account_manager.has_account(acc_id):
                return fail("NOT_FOUND", f"账号不存在: {acc_id}")
            body = await request.json()
            # PRD-V5 §5.3 LLM-501：校验 llm_id（仅在 PATCH 中显式提供时校验）
            if "llm_id" in body:
                err = _validate_llm_id(llm_manager, body.get("llm_id", ""))
                if err:
                    return fail(err[0], err[1])
            # ACC-501：通过配置注册表更新（敏感字段占位符保留原值）
            account_manager.update_account_config(acc_id, body)
            await _save_accounts_to_config(config_loader, account_manager, config_path)
            # ACC-501：用 get_account_status 支持禁用账号
            status = account_manager.get_account_status(acc_id)
            _enrich_llm_status(status, llm_manager)
            return ok(status, "账号配置已更新（重启后生效）")
        except Exception as e:
            logger.error(f"更新账号配置失败: {e}", exc_info=True)
            return fail_internal()

    async def set_default(request: Request) -> JSONResponse:
        acc_id = request.path_params.get("id")
        if not account_manager.set_default(acc_id):
            return fail("NOT_FOUND", f"账号不存在: {acc_id}")
        await _save_accounts_to_config(config_loader, account_manager, config_path)
        return ok(message=f"默认账号已设置为: {acc_id}")

    async def bind_persona(request: Request) -> JSONResponse:
        """绑定账号到人格或 profile

        请求体支持两种模式（PRD V3 §7）：
        - {"persona_id": "xxx"}           单人格绑定（向后兼容）
        - {"profile_id": "xxx"}           绑定到 profile（多人格组，可用 switch-persona 切换）
        - {"profile_id": "xxx", "persona_id": "yyy"}  绑定 profile 同时指定初始激活人格
        - {"persona_id": ""}              解绑
        """
        try:
            acc_id = request.path_params.get("id")
            acc = account_manager.get_account(acc_id)
            if not acc:
                return fail("NOT_FOUND", f"账号不存在: {acc_id}")
            body = await request.json()
            profile_id = body.get("profile_id", "")
            persona_id = body.get("persona_id", "")

            if profile_id:
                # 新模式：绑定 profile
                ok = acc.persona_store.set_account_profile(acc_id, profile_id, persona_id or None)
                if not ok:
                    return fail("NOT_FOUND", f"profile 不存在或人格不在 profile.personas 内: {profile_id}")
                acc.profile_id = profile_id
                acc.account_config["profile_id"] = profile_id
                # 同步 persona_id 为激活人格
                active_id = acc.persona_store.get_account_persona_id(acc_id) or ""
                acc.persona_id = active_id
                acc.account_config["persona_id"] = active_id
            elif persona_id:
                # 旧模式：单人格绑定
                if not acc.persona_store.set_account_persona(acc_id, persona_id):
                    return fail("NOT_FOUND", f"人格不存在: {persona_id}")
                acc.profile_id = ""
                acc.persona_id = persona_id
                acc.account_config["profile_id"] = ""
                acc.account_config["persona_id"] = persona_id
            else:
                # 解绑
                acc.persona_store.unset_account_persona(acc_id)
                acc.profile_id = ""
                acc.persona_id = ""
                acc.account_config["profile_id"] = ""
                acc.account_config["persona_id"] = ""

            # ACC-501：同步到配置注册表（save_to_config 从注册表序列化）
            account_manager.update_account_config(acc_id, {
                "profile_id": acc.profile_id,
                "persona_id": acc.persona_id,
            })
            await _save_accounts_to_config(config_loader, account_manager, config_path)
            active = acc.persona_store.get_account_persona_id(acc_id) or "(默认)"
            return ok({
                "profile_id": acc.profile_id,
                "active_persona_id": active,
                "available_personas": acc.persona_store.get_available_personas_for_account(acc_id),
            }, f"账号 {acc_id} 人格已绑定: {active}")
        except Exception as e:
            logger.error(f"绑定人格失败: {e}", exc_info=True)
            return fail_internal()

    async def switch_persona(request: Request) -> JSONResponse:
        """切换账号当前激活的人格（仅当账号绑定了 profile 时有效）

        请求体：{"persona_id": "xxx"}
        """
        try:
            acc_id = request.path_params.get("id")
            acc = account_manager.get_account(acc_id)
            if not acc:
                return fail("NOT_FOUND", f"账号不存在: {acc_id}")
            body = await request.json()
            persona_id = body.get("persona_id", "")
            if not persona_id:
                return fail("VALIDATION_ERROR", "persona_id 不能为空")
            if not acc.persona_store.set_account_active_persona(acc_id, persona_id):
                return fail("NOT_FOUND", f"人格不存在或不在 profile.personas 列表内: {persona_id}")
            # 同步账号 persona_id 字段
            acc.persona_id = persona_id
            acc.account_config["persona_id"] = persona_id
            # ACC-501：同步到配置注册表
            account_manager.update_account_config(acc_id, {"persona_id": persona_id})
            await _save_accounts_to_config(config_loader, account_manager, config_path)
            return ok({
                "active_persona_id": persona_id,
                "available_personas": acc.persona_store.get_available_personas_for_account(acc_id),
            }, f"账号 {acc_id} 已切换人格: {persona_id}")
        except Exception as e:
            logger.error(f"切换人格失败: {e}", exc_info=True)
            return fail_internal()

    async def list_account_personas(request: Request) -> JSONResponse:
        """列出账号可用的人格（含当前激活人格）"""
        acc_id = request.path_params.get("id")
        acc = account_manager.get_account(acc_id)
        if not acc:
            return fail("NOT_FOUND", f"账号不存在: {acc_id}")
        return ok(acc.persona_store.get_account_persona_status(acc_id))

    async def list_profiles(request: Request) -> JSONResponse:
        """列出所有 profile（含可用人格详情）"""
        # 任意账号的 persona_store 都共享同一个 config_loader
        acc = account_manager.get_default()
        if acc and acc.persona_store:
            return ok(acc.persona_store.list_profiles())
        return ok([])

    async def bind_llm(request: Request) -> JSONResponse:
        try:
            acc_id = request.path_params.get("id")
            acc = account_manager.get_account(acc_id)
            if not acc:
                return fail("NOT_FOUND", f"账号不存在: {acc_id}")
            body = await request.json()
            llm_id = body.get("llm_id", "")
            # PRD-V5 §5.3 LLM-501：校验 llm_id 指向已存在且 enabled 的 Provider
            err = _validate_llm_id(llm_manager, llm_id)
            if err:
                return fail(err[0], err[1])
            acc.llm_id = llm_id
            acc.account_config["llm_id"] = llm_id
            # ACC-501：同步到配置注册表
            account_manager.update_account_config(acc_id, {"llm_id": llm_id})
            await _save_accounts_to_config(config_loader, account_manager, config_path)
            return ok(message=f"账号 {acc_id} LLM 已绑定: {llm_id or '(默认)'}")
        except Exception as e:
            logger.error(f"绑定 LLM 失败: {e}", exc_info=True)
            return fail_internal()

    async def start_account(request: Request) -> JSONResponse:
        """PRD V4 BOOT-002：后台启动，立即返回 task_id 和状态，不等待调度循环"""
        acc_id = request.path_params.get("id")
        acc = account_manager.get_account(acc_id)
        if not acc:
            # ACC-501：账号可能刚通过 PATCH 启用但无运行时实例
            if not account_manager.create_runtime_instance(acc_id):
                return fail("INSTANCE_CREATE_FAILED", f"账号不存在或未启用: {acc_id}")
            acc = account_manager.get_account(acc_id)
            if not acc:
                return fail_internal("创建账号实例失败")
        if acc.is_running():
            return ok({
                "account_id": acc_id,
                "state": "running",
                "task_id": id(acc._scheduler_task) if acc._scheduler_task else None,
            }, "账号已在运行中")
        try:
            if acc.scheduler is None:
                await acc.initialize()
            await acc.start()
            return ok({
                "account_id": acc_id,
                "state": acc.get_status().get("state", "running"),
                "task_id": id(acc._scheduler_task) if acc._scheduler_task else None,
            }, "账号已启动")
        except Exception as e:
            logger.error(f"启动账号失败: {e}", exc_info=True)
            return fail_internal()

    async def stop_account(request: Request) -> JSONResponse:
        acc_id = request.path_params.get("id")
        acc = account_manager.get_account(acc_id)
        if not acc:
            return fail("NOT_FOUND", f"账号不存在: {acc_id}")
        try:
            # close() 取消调度任务并释放 memory worker / HTTP session，避免 stop/start 泄漏
            if hasattr(acc, "close"):
                await acc.close()
            else:
                acc.stop()
            status = account_manager.get_account_status(acc_id)
            _enrich_llm_status(status, llm_manager)
            return ok(status, "账号已停止")
        except Exception as e:
            logger.error(f"停止账号失败: {e}", exc_info=True)
            return fail_internal()

    # ═══════════════════════════════════════════════════════
    #  PRD-V5 §5.2 ACC-503：二维码登录会话闭环
    # ═══════════════════════════════════════════════════════

    async def qr_login_init(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/qr-login

        ACC-503：创建目标绑定的 QR 登录会话。
        - 绑定创建者管理员会话（creator_session_hash）
        - 不返回 raw qrcode_key
        - 180 秒有效，一次性
        - 账号不存在时不回退 V1
        """
        try:
            acc_id = request.path_params.get("id")
            # ACC-503：账号不存在 → 不回退 V1 配置
            if not account_manager.has_account(acc_id):
                return fail("NOT_FOUND", f"账号不存在: {acc_id}", status_code=404)

            # 提取管理员会话 token 并计算 hash
            admin_token = _extract_admin_token(request)
            if not admin_token:
                return fail("UNAUTHORIZED", "缺少管理员会话凭证", status_code=401)
            creator_hash = _sha256_hex(admin_token)

            # 清理终态会话（全局，防止内存泄漏）
            await _cleanup_terminal_qr_sessions()

            now = time.time()
            # 加锁：清理该账号过期/终态会话 + 检查活跃会话（TOCTOU 防护）
            async with _get_qr_lock():
                for sid in list(_qr_sessions.keys()):
                    sess = _qr_sessions[sid]
                    if sess.get("account_id") != acc_id:
                        continue
                    if sess.get("expire_at", 0) < now or sess.get("status") in _QR_TERMINAL_STATUSES:
                        _qr_sessions.pop(sid, None)
                        _qr_session_keys.pop(sid, None)

                # 同一账号同一时刻最多一个活跃会话
                for sess in _qr_sessions.values():
                    if (sess.get("account_id") == acc_id
                            and sess.get("expire_at", 0) >= now
                            and sess.get("status") not in _QR_TERMINAL_STATUSES):
                        return fail("QR_SESSION_EXISTS", "该账号已有活跃的二维码登录会话，请等待过期后重试")

            # 调用 B站 API 获取二维码
            from ..bilibili_qrlogin import BilibiliQRLogin
            qr_login = BilibiliQRLogin(config_loader, config_path=config_path)
            try:
                result = await qr_login.get_qrcode()
            finally:
                await qr_login.close()

            if "error" in result:
                return fail("QRCODE_FAILED", str(result["error"]), status_code=400)

            raw_key = result["key"]
            key_hash = _sha256_hex(raw_key)

            # 加锁：创建会话（不含 raw key）
            async with _get_qr_lock():
                qr_session_id = uuid.uuid4().hex
                _qr_sessions[qr_session_id] = {
                    "account_id": acc_id,
                    "creator_session_hash": creator_hash,
                    "qrcode_key_hash": key_hash,
                    "status": "created",
                    "expire_at": now + _QR_SESSION_TTL,
                    "created_at": now,
                }
                # raw key 仅临时保留在内存，用于轮询 B站 API
                _qr_session_keys[qr_session_id] = raw_key

            logger.info(
                f"QR session created: sid={_short(qr_session_id)} account={acc_id} "
                f"creator={_short(creator_hash)} status=created"
            )

            return ok({
                "qr_session_id": qr_session_id,
                "qrcode_url": result["url"],
                "qrcode_data_url": result["data_url"],
                "expire_at": int(now + _QR_SESSION_TTL),
                "account_id": acc_id,
                "status": "created",
            }, "二维码登录会话已创建")
        except Exception as e:
            logger.error(f"创建 QR 登录会话失败: {e}", exc_info=True)
            return fail_internal()

    async def qr_login_poll(request: Request) -> JSONResponse:
        """GET /api/accounts/{id}/qr-login/{session_id}

        ACC-503：鉴权轮询并定向写入 Cookie。
        - 验证创建者管理员会话（QR_SESSION_OWNER_MISMATCH → 403）
        - 验证会话属于目标账号（QR_SESSION_MISMATCH → 403）
        - 一次性：终态会话再次访问 → 410
        - 确认后立即失效，不返回 raw Cookie
        - 仅重载目标账号
        """
        try:
            acc_id = request.path_params.get("id")
            session_id = request.path_params.get("session_id")

            # 清理终态会话（全局，防止内存泄漏）
            await _cleanup_terminal_qr_sessions()

            # 加锁：会话查找 + 鉴权 + 终态/过期检查 + 取临时 key
            async with _get_qr_lock():
                sess = _qr_sessions.get(session_id)
                if not sess:
                    return fail("QR_SESSION_NOT_FOUND", "二维码会话不存在或已失效", status_code=404)

                # 验证创建者管理员会话
                admin_token = _extract_admin_token(request)
                if not admin_token:
                    return fail("UNAUTHORIZED", "缺少管理员会话凭证", status_code=401)
                creator_hash = _sha256_hex(admin_token)
                if sess.get("creator_session_hash") != creator_hash:
                    logger.warning(
                        f"QR session owner mismatch: sid={_short(session_id)} "
                        f"account={acc_id} creator={_short(creator_hash)}"
                    )
                    return fail("QR_SESSION_OWNER_MISMATCH",
                                "会话创建者不匹配", status_code=403)

                # 验证会话属于目标账号
                if sess.get("account_id") != acc_id:
                    return fail("QR_SESSION_MISMATCH",
                                "会话与目标账号不匹配", status_code=403)

                # 终态会话：一次性，再次操作返回 410
                status = sess.get("status", "created")
                if status in _QR_TERMINAL_STATUSES:
                    return fail("QR_SESSION_GONE",
                                f"会话已结束: {status}", status_code=410)

                # 验证未过期
                now = time.time()
                if sess.get("expire_at", 0) < now:
                    _invalidate_qr_session(session_id, "expired")
                    logger.info(f"QR session expired: sid={_short(session_id)} account={acc_id}")
                    return fail("QR_SESSION_EXPIRED", "二维码会话已过期", status_code=410)

                # 取临时 key 轮询 B站
                raw_key = _qr_session_keys.get(session_id, "")
                if not raw_key:
                    _invalidate_qr_session(session_id, "expired")
                    return fail("QR_SESSION_EXPIRED", "会话凭据缺失", status_code=410)

            from ..bilibili_qrlogin import BilibiliQRLogin
            qr_login = BilibiliQRLogin(config_loader, config_path=config_path)
            try:
                result = await qr_login.check_status(raw_key)
            finally:
                await qr_login.close()

            b_status = result.get("status", "unknown")
            message = result.get("message", "")

            if b_status == "scanned":
                async with _get_qr_lock():
                    sess = _qr_sessions.get(session_id)
                    if sess is not None:
                        sess["status"] = "scanned"
                return ok({
                    "status": "scanned",
                    "message": message,
                    "account_id": acc_id,
                })

            if b_status == "expired":
                async with _get_qr_lock():
                    _invalidate_qr_session(session_id, "expired")
                logger.info(f"QR session expired (B站): sid={_short(session_id)} account={acc_id}")
                return ok({
                    "status": "expired",
                    "message": message,
                    "account_id": acc_id,
                })

            if b_status == "confirmed":
                # 账号可能在会话创建后被删除
                if not account_manager.has_account(acc_id):
                    async with _get_qr_lock():
                        _invalidate_qr_session(session_id, "expired")
                    return fail("ACCOUNT_NOT_FOUND",
                                f"账号已被删除: {acc_id}", status_code=404)

                cookies = result.get("cookies", {})
                # 写入目标账号配置（account_id 非空时不回退 V1）
                save_result = qr_login.update_config(cookies, account_id=acc_id)
                if not save_result.get("success"):
                    return fail("QR_SAVE_FAILED", save_result.get("message", "保存配置失败"))

                # 立即失效会话（一次性）
                async with _get_qr_lock():
                    _invalidate_qr_session(session_id, "confirmed")

                # 同步配置注册表（qrlogin 直接写 config.yaml，需拾取到注册表）
                try:
                    account_manager.sync_registry_from_config()
                except Exception as e:
                    logger.warning(f"扫码登录后配置注册表同步失败: {e}")

                # ACC-503：仅重载目标账号（不 reload_all）
                try:
                    acc = account_manager.get_account(acc_id)
                    if acc:
                        await acc.reload()
                except Exception as e:
                    logger.warning(f"扫码登录后账号凭据热重载失败: {e}")

                logger.info(
                    f"QR session confirmed: sid={_short(session_id)} account={acc_id} "
                    f"creator={_short(creator_hash)}"
                )

                return ok({
                    "status": "confirmed",
                    "message": "登录成功！",
                    "account_id": acc_id,
                    "uid": save_result.get("info", {}).get("uid", ""),
                }, "登录成功")

            return ok({
                "status": b_status,
                "message": message,
                "account_id": acc_id,
            })
        except Exception as e:
            logger.error(f"轮询 QR 登录状态失败: {e}", exc_info=True)
            return fail_internal()

    async def qr_login_cancel(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/qr-login/{session_id}/cancel

        ACC-503：取消 QR 登录会话。
        - 验证创建者管理员会话
        - 标记为 cancelled，立即失效
        """
        try:
            acc_id = request.path_params.get("id")
            session_id = request.path_params.get("session_id")

            # 加锁：会话查找 + 鉴权 + 终态检查 + 取消
            async with _get_qr_lock():
                sess = _qr_sessions.get(session_id)
                if not sess:
                    return fail("QR_SESSION_NOT_FOUND", "二维码会话不存在或已失效", status_code=404)

                admin_token = _extract_admin_token(request)
                if not admin_token:
                    return fail("UNAUTHORIZED", "缺少管理员会话凭证", status_code=401)
                creator_hash = _sha256_hex(admin_token)
                if sess.get("creator_session_hash") != creator_hash:
                    return fail("QR_SESSION_OWNER_MISMATCH",
                                "会话创建者不匹配", status_code=403)

                if sess.get("account_id") != acc_id:
                    return fail("QR_SESSION_MISMATCH",
                                "会话与目标账号不匹配", status_code=403)

                status = sess.get("status", "created")
                if status in _QR_TERMINAL_STATUSES:
                    return fail("QR_SESSION_GONE",
                                f"会话已结束: {status}", status_code=410)

                _invalidate_qr_session(session_id, "cancelled")
            logger.info(f"QR session cancelled: sid={_short(session_id)} account={acc_id}")
            return ok({"status": "cancelled", "account_id": acc_id}, "会话已取消")
        except Exception as e:
            logger.error(f"取消 QR 登录会话失败: {e}", exc_info=True)
            return fail_internal()

    # ═══════════════════════════════════════════════════════
    #  PRD-V5 §7.4 / TASK-501：手动任务 API（账号级）
    # ═══════════════════════════════════════════════════════

    def _get_account_scheduler(acc_id: str):
        """获取账号调度器（若未运行返回 None）"""
        acc = account_manager.get_account(acc_id)
        if not acc:
            return None, fail("NOT_FOUND", f"账号不存在: {acc_id}", status_code=404)
        if acc.scheduler is None:
            return None, fail("SCHEDULER_UNAVAILABLE", "账号调度器未初始化", status_code=503)
        return acc.scheduler, None

    def _get_account_task_store(acc_id: str):
        """获取账号的 TaskRunStore（不要求调度器运行）

        调度器运行时复用其 task_store；否则基于 account_data_dir 构造
        临时实例读取持久化 SQLite（任务列表查询无需运行时调度器）。
        """
        acc = account_manager.get_account(acc_id)
        if not acc:
            return None, fail("NOT_FOUND", f"账号不存在: {acc_id}", status_code=404)
        if acc.scheduler is not None and getattr(acc.scheduler, "task_store", None) is not None:
            return acc.scheduler.task_store, None
        # 调度器未初始化：基于账号数据目录构造 TaskRunStore（只读查询）
        import os
        from bilibot.services.task_store import TaskRunStore
        db_path = os.path.join(acc.account_data_dir, "task_runs.db")
        return TaskRunStore(db_path, account_id=acc_id), None

    async def trigger_proactive_video_task(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/tasks/proactive-video → 202 + task_id

        PRD-V5 §7.4：手动创建 TaskRun（trigger_type=manual），异步触发 _do_proactive_video。
        创建协程 ≠ 成功；任务状态通过 GET /tasks/{task_id} 查询。
        """
        try:
            acc_id = request.path_params.get("id")
            scheduler, err = _get_account_scheduler(acc_id)
            if err is not None:
                return err
            # 读取可选 body（不强制要求）
            try:
                body = await request.json()
            except Exception:
                body = {}
            scene = "proactive_video"
            task_id = scheduler.create_manual_task(scene, input_data=body or None)
            if not task_id:
                return fail_internal("创建 TaskRun 失败")
            # 异步触发（claim + start 在协程内完成）
            scheduler._spawn_memory_task(
                scheduler._do_proactive_video(task_id=task_id),
                tag=f"manual_proactive_video:{task_id}",
            )
            return ok({"task_id": task_id, "status": "scheduled"},
                       "任务已创建（异步执行）", status_code=202)
        except Exception as e:
            logger.error(f"触发主动视频任务失败: {e}", exc_info=True)
            return fail_internal()

    async def trigger_dynamic_task(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/tasks/dynamic → 202 + task_id"""
        try:
            acc_id = request.path_params.get("id")
            scheduler, err = _get_account_scheduler(acc_id)
            if err is not None:
                return err
            try:
                body = await request.json()
            except Exception:
                body = {}
            scene = "dynamic"
            task_id = scheduler.create_manual_task(scene, input_data=body or None)
            if not task_id:
                return fail_internal("创建 TaskRun 失败")
            scheduler._spawn_memory_task(
                scheduler._do_post_dynamic(task_id=task_id),
                tag=f"manual_dynamic:{task_id}",
            )
            return ok({"task_id": task_id, "status": "scheduled"},
                       "任务已创建（异步执行）", status_code=202)
        except Exception as e:
            logger.error(f"触发动态任务失败: {e}", exc_info=True)
            return fail_internal()

    async def list_account_tasks(request: Request) -> JSONResponse:
        """GET /api/accounts/{id}/tasks → 分页列出账号任务

        支持查询参数：page（默认 1）、page_size（默认 20，上限 100）、
        status（可选，按任务状态过滤）。不要求调度器运行。
        """
        try:
            acc_id = request.path_params.get("id")
            task_store, err = _get_account_task_store(acc_id)
            if err is not None:
                return err
            # 分页参数解析（容错）
            try:
                page = max(1, int(request.query_params.get("page", "1")))
            except (ValueError, TypeError):
                page = 1
            try:
                page_size = max(1, min(100, int(request.query_params.get("page_size", "20"))))
            except (ValueError, TypeError):
                page_size = 20
            status = request.query_params.get("status") or None
            offset = (page - 1) * page_size
            from bilibot.services.task_store import desensitize_task_run
            tasks = task_store.list_by_account(
                acc_id, limit=page_size, offset=offset, status=status,
            )
            total = task_store.count_by_account(acc_id, status=status)
            return ok({
                "items": [desensitize_task_run(t) for t in tasks],
                "total": total,
                "page": page,
                "page_size": page_size,
            })
        except Exception as e:
            logger.error(f"列出账号任务失败: {e}", exc_info=True)
            return fail_internal()

    async def get_account_task(request: Request) -> JSONResponse:
        """GET /api/accounts/{id}/tasks/{task_id} → status + desensitized result"""
        try:
            acc_id = request.path_params.get("id")
            task_id = request.path_params.get("task_id")
            scheduler, err = _get_account_scheduler(acc_id)
            if err is not None:
                return err
            task = scheduler.task_store.get(task_id)
            if task is None:
                return fail("NOT_FOUND", f"任务不存在: {task_id}", status_code=404)
            from bilibot.services.task_store import desensitize_task_run
            return ok(desensitize_task_run(task))
        except Exception as e:
            logger.error(f"获取账号任务失败: {e}", exc_info=True)
            return fail_internal()

    async def cancel_account_task(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/tasks/{task_id}/cancel

        PRD-V5 §7.4：取消未外部发布的任务（scheduled/claimed）。
        running/succeeded 状态不可取消（可能已发布）。
        """
        try:
            acc_id = request.path_params.get("id")
            task_id = request.path_params.get("task_id")
            scheduler, err = _get_account_scheduler(acc_id)
            if err is not None:
                return err
            task = scheduler.task_store.get(task_id)
            if task is None:
                return fail("NOT_FOUND", f"任务不存在: {task_id}", status_code=404)
            if scheduler.task_store.cancel(task_id):
                return ok({"task_id": task_id, "status": "cancelled"},
                           "任务已取消")
            return fail("INVALID_STATE",
                        f"任务状态 {task.status} 不允许取消（仅 scheduled/claimed 可取消）",
                        status_code=409)
        except Exception as e:
            logger.error(f"取消账号任务失败: {e}", exc_info=True)
            return fail_internal()

    async def retry_account_task(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/tasks/{task_id}/retry

        PRD-V5 §7.4：重试 retry_wait/failed/interrupted 状态的任务。
        重置为 scheduled，需要调度循环重新拾取。
        """
        try:
            acc_id = request.path_params.get("id")
            task_id = request.path_params.get("task_id")
            scheduler, err = _get_account_scheduler(acc_id)
            if err is not None:
                return err
            task = scheduler.task_store.get(task_id)
            if task is None:
                return fail("NOT_FOUND", f"任务不存在: {task_id}", status_code=404)
            if scheduler.task_store.retry(task_id):
                return ok({"task_id": task_id, "status": "scheduled"},
                           "任务已重新入队")
            return fail("INVALID_STATE",
                        f"任务状态 {task.status} 不允许重试（仅 retry_wait/failed/interrupted 可重试）",
                        status_code=409)
        except Exception as e:
            logger.error(f"重试账号任务失败: {e}", exc_info=True)
            return fail_internal()

    return [
        Route("/api/accounts", list_accounts, methods=["GET"]),
        Route("/api/accounts", add_account, methods=["POST"]),
        Route("/api/accounts/{id}", get_account, methods=["GET"]),
        Route("/api/accounts/{id}", delete_account, methods=["DELETE"]),
        Route("/api/accounts/{id}", update_account, methods=["PATCH"]),
        Route("/api/accounts/{id}/set-default", set_default, methods=["POST"]),
        Route("/api/accounts/{id}/persona", bind_persona, methods=["POST"]),
        # PRD V3 §7：切换激活人格 + 列出可用人格 + 列出所有 profile
        Route("/api/accounts/{id}/switch-persona", switch_persona, methods=["POST"]),
        Route("/api/accounts/{id}/personas", list_account_personas, methods=["GET"]),
        Route("/api/accounts/profiles", list_profiles, methods=["GET"]),
        Route("/api/accounts/{id}/llm", bind_llm, methods=["POST"]),
        Route("/api/accounts/{id}/start", start_account, methods=["POST"]),
        Route("/api/accounts/{id}/stop", stop_account, methods=["POST"]),
        # PRD-V5 §5.2 ACC-503：二维码登录会话闭环（需管理员鉴权）
        Route("/api/accounts/{id}/qr-login", qr_login_init, methods=["POST"]),
        Route("/api/accounts/{id}/qr-login/{session_id}", qr_login_poll, methods=["GET"]),
        Route("/api/accounts/{id}/qr-login/{session_id}/cancel", qr_login_cancel, methods=["POST"]),
        # PRD-V5 §7.4 / TASK-501：手动任务 API（账号级）
        Route("/api/accounts/{id}/tasks", list_account_tasks, methods=["GET"]),
        Route("/api/accounts/{id}/tasks/proactive-video", trigger_proactive_video_task, methods=["POST"]),
        Route("/api/accounts/{id}/tasks/dynamic", trigger_dynamic_task, methods=["POST"]),
        Route("/api/accounts/{id}/tasks/{task_id}", get_account_task, methods=["GET"]),
        Route("/api/accounts/{id}/tasks/{task_id}/cancel", cancel_account_task, methods=["POST"]),
        Route("/api/accounts/{id}/tasks/{task_id}/retry", retry_account_task, methods=["POST"]),
    ]
