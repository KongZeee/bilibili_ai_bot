"""
模型路由 API — 统一管理所有类型的 Provider + 功能路由

提供：
- GET    /api/model-routing                    - 获取路由配置 + 各类型 Provider 列表
- PATCH  /api/model-routing                    - 更新功能路由
- GET    /api/model-routing/{type}             - 列出指定类型 Provider
- POST   /api/model-routing/{type}             - 添加 Provider
- DELETE /api/model-routing/{type}/{id}        - 删除 Provider
- PATCH  /api/model-routing/{type}/{id}        - 更新 Provider
- POST   /api/model-routing/{type}/{id}/test   - 测试 Provider 连接
"""
import logging
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal, fail_invalid_input
from ..llm.router import PROVIDER_TYPES, CONFIG_KEYS

logger = logging.getLogger("bilibot.api.model_routing")

# panel 注入 account_manager 的弱引用（或非 weakref 兜底 callable），供 rebind 扫账号
_ACCOUNT_MANAGER_REF = None

# 功能名称映射
FEATURE_LABELS = {
    "chat": "对话（主动回复/动态/记忆提取）",
    "vision": "视频视觉轨",
    "embedding": "记忆向量检索",
    "asr": "视频音频轨",
    "image": "动态配图",
    "rerank": "记忆重排序",
}


def _save_to_config(config_loader, router, config_path: str):
    """持久化到 config.yaml（在写锁内完成 RMW，避免并发丢段）"""
    try:
        def _mutate(raw: dict) -> None:
            saved = router.save_to_config()
            for ptype in PROVIDER_TYPES:
                key = CONFIG_KEYS[ptype]
                if key in saved:
                    raw[key] = saved[key]
            raw["model_routing"] = saved.get("model_routing", {})
            raw["allow_llm_fallback"] = saved.get(
                "allow_llm_fallback", raw.get("allow_llm_fallback", False)
            )
            # model_request_limits 以 router 当前值为准写回
            if "model_request_limits" in saved:
                raw["model_request_limits"] = saved["model_request_limits"]
            raw["config_revision"] = int(raw.get("config_revision", 0) or 0) + 1

        if hasattr(config_loader, "atomic_update"):
            config_loader.atomic_update(config_path, _mutate)
        else:
            raw = config_loader.get_raw_config()
            _mutate(raw)
            config_loader.save_config(raw, config_path)
        return True
    except Exception as e:
        logger.error(f"持久化模型配置失败: {e}", exc_info=True)
        return False


def _resolve_account_manager(explicit=None):
    """解析 account_manager：显式参数 → 路由工厂弱引用 → 无。"""
    if explicit is not None:
        return explicit
    try:
        ref = globals().get("_ACCOUNT_MANAGER_REF")
        if ref is not None:
            return ref() if callable(getattr(ref, "__call__", None)) else ref
    except Exception:
        pass
    return None


def _rebind_memory_brains_for_embedding(router, account_manager=None) -> int:
    """P004：embedding 路由/Provider 变更后，热重绑存活 MemoryBrain。

    优先走 MemoryBrainService 弱引用注册表；若注入/缓存 account_manager 则再扫一遍账号，
    防止 live registry 未注册时 rebind 漏账号。
    """
    emb = None
    try:
        resolve = getattr(router, "resolve_embedding", None)
        emb = resolve() if callable(resolve) else None
    except Exception as e:
        logger.warning("resolve_embedding 失败，跳过记忆重绑: %s", e)
        return 0

    rebound = 0
    try:
        from bilibot.memory_brain.service import rebind_all_live_brains

        rebound = rebind_all_live_brains(
            embedding_provider=emb,
            rebind_embedding=True,
        )
    except Exception as e:
        logger.warning("rebind_all_live_brains 失败: %s", e)

    am = _resolve_account_manager(account_manager)
    if am is not None:
        try:
            accounts = getattr(am, "_accounts", None) or {}
            seen_ids: set[str] = set()
            for acc in list(accounts.values()):
                brain = getattr(acc, "memory_brain", None)
                if brain is None or not hasattr(brain, "rebind_providers"):
                    continue
                aid = str(getattr(acc, "account_id", "") or id(brain))
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                try:
                    brain.rebind_providers(
                        chat_provider=getattr(acc, "llm", None),
                        embedding_provider=emb,
                        rebind_chat=True,
                        rebind_embedding=True,
                    )
                    rebound += 1
                except Exception as e:
                    logger.warning(
                        "记忆大脑 embedding 重绑失败 account=%s: %s",
                        getattr(acc, "account_id", "?"),
                        e,
                    )
        except Exception as e:
            logger.warning("遍历账号重绑记忆大脑失败: %s", e)

    if rebound:
        logger.info("已热重绑记忆 embedding provider（约 %s 次）", rebound)
    return rebound


def _rebind_memory_brains_for_rerank(router, account_manager=None) -> int:
    """rerank 路由/Provider 变更后，热重绑存活 MemoryBrain 的 rerank 侧。"""
    rr = None
    try:
        resolve = getattr(router, "resolve_rerank", None)
        rr = resolve() if callable(resolve) else None
    except Exception as e:
        logger.warning("resolve_rerank 失败，跳过记忆重绑: %s", e)
        return 0

    rebound = 0
    already_rebound: set[int] = set()
    try:
        from bilibot.memory_brain.service import (
            iter_live_brains,
            rebind_all_live_brains,
        )

        rebound = rebind_all_live_brains(
            rerank_provider=rr,
            rebind_rerank=True,
        )
        # The account loop below is only a fallback for brains that are not
        # registered as live (e.g. constructed but never started). Skip the
        # ones the global pass just rebound so the count isn't inflated.
        already_rebound = {id(brain) for brain in iter_live_brains()}
    except Exception as e:
        logger.warning("rebind_all_live_brains(rerank) 失败: %s", e)

    am = _resolve_account_manager(account_manager)
    if am is not None:
        try:
            accounts = getattr(am, "_accounts", None) or {}
            seen_ids: set[str] = set()
            for acc in list(accounts.values()):
                brain = getattr(acc, "memory_brain", None)
                if brain is None or not hasattr(brain, "rebind_providers"):
                    continue
                if id(brain) in already_rebound:
                    continue
                aid = str(getattr(acc, "account_id", "") or id(brain))
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                try:
                    brain.rebind_providers(
                        rerank_provider=rr,
                        rebind_rerank=True,
                    )
                    rebound += 1
                except Exception as e:
                    logger.warning(
                        "记忆大脑 rerank 重绑失败 account=%s: %s",
                        getattr(acc, "account_id", "?"),
                        e,
                    )
        except Exception as e:
            logger.warning("遍历账号重绑记忆大脑 rerank 失败: %s", e)

    if rebound:
        logger.info("已热重绑记忆 rerank provider（%s 次）", rebound)
    return rebound


def create_model_routing_routes(
    router,
    config_loader,
    config_path: str = "config.yaml",
    account_manager=None,
):
    """创建模型路由管理路由

    account_manager: 可选；传入后 embedding 相关变更会热重绑各账号 MemoryBrain。
    未传时仍优先 live brain 弱引用表；若后续通过同一进程二次注入可写 _ACCOUNT_MANAGER_REF。
    """
    global _ACCOUNT_MANAGER_REF
    if account_manager is not None:
        try:
            import weakref

            _ACCOUNT_MANAGER_REF = weakref.ref(account_manager)
        except TypeError:
            _ACCOUNT_MANAGER_REF = lambda: account_manager  # noqa: E731 — 非 weakref 兜底

    async def get_overview(request: Request) -> JSONResponse:
        """获取路由总览：功能 → Provider 映射 + 各类型 Provider 列表"""
        routing = router.get_routing()
        overview = {}
        for ptype in PROVIDER_TYPES:
            providers = router.list_providers(ptype)
            routed_id = routing.get(ptype, "")
            routed_provider = None
            if routed_id:
                for p in providers:
                    if p.get("id") == routed_id:
                        routed_provider = p
                        break
            overview[ptype] = {
                "label": FEATURE_LABELS.get(ptype, ptype),
                "routed_provider_id": routed_id,
                "routed_provider": routed_provider,
                "providers": providers,
            }
        return ok({
            "routing": routing,
            "features": overview,
            "local_whisper": router.local_whisper,
        })

    async def update_routing(request: Request) -> JSONResponse:
        """更新功能路由"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return fail_invalid_input("请求体必须是 JSON 对象")
            updated = {}
            for ptype in PROVIDER_TYPES:
                if ptype in body:
                    pid = body[ptype]
                    if pid and not router.set_routing(ptype, pid):
                        return fail_invalid_input(f"无法设置 {ptype} → {pid}（Provider 不存在）")
                    updated[ptype] = pid
            if not updated:
                return fail_invalid_input("未提供任何路由更新")
            _save_to_config(config_loader, router, config_path)
            if "embedding" in updated:
                _rebind_memory_brains_for_embedding(router, account_manager)
            if "rerank" in updated:
                _rebind_memory_brains_for_rerank(router, account_manager)
            return ok(router.get_routing(), "路由已更新")
        except Exception as e:
            logger.error(f"更新模型路由失败: {e}", exc_info=True)
            return fail_internal()

    async def list_by_type(request: Request) -> JSONResponse:
        """列出指定类型的 Provider"""
        ptype = request.path_params.get("type")
        if ptype not in PROVIDER_TYPES:
            return fail_invalid_input(f"未知 Provider 类型: {ptype}")
        return ok(router.list_providers(ptype))

    async def add_by_type(request: Request) -> JSONResponse:
        """添加 Provider"""
        ptype = request.path_params.get("type")
        if ptype not in PROVIDER_TYPES:
            return fail_invalid_input(f"未知 Provider 类型: {ptype}")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return fail_invalid_input("请求体必须是 JSON 对象")
            if not body.get("model"):
                return fail("VALIDATION_ERROR", "model 不能为空")
            # local-whisper ASR 可不填 key；其余类型至少需要一个密钥
            if ptype != "asr" or (body.get("id") != "local-whisper" and not body.get("model_size")):
                has_keys = bool(body.get("api_key")) or bool(body.get("api_keys"))
                if not has_keys and ptype != "asr":
                    return fail("VALIDATION_ERROR", "api_key / api_keys 不能为空")
            pid = router.add_provider(ptype, body)
            _save_to_config(config_loader, router, config_path)
            if ptype == "embedding":
                _rebind_memory_brains_for_embedding(router, account_manager)
            if ptype == "rerank":
                _rebind_memory_brains_for_rerank(router, account_manager)
            return ok(router.get_provider_by_type(ptype, pid).get_info(), "Provider 添加成功")
        except ValueError as e:
            logger.warning("模型路由校验失败: %s", e)
            return fail("VALIDATION_ERROR", "路由参数不合法")
        except Exception as e:
            logger.error(f"添加 Provider 失败: {e}", exc_info=True)
            return fail_internal()

    async def delete_by_type(request: Request) -> JSONResponse:
        """删除 Provider"""
        ptype = request.path_params.get("type")
        pid = request.path_params.get("id")
        if ptype not in PROVIDER_TYPES:
            return fail_invalid_input(f"未知 Provider 类型: {ptype}")
        # 不允许删除路由中唯一启用的 Provider
        if router.count_providers(ptype) <= 1 and router.get_routing().get(ptype) == pid:
            return fail("LAST_PROVIDER", f"不能删除最后一个 {ptype} Provider")
        if not router.remove_provider(ptype, pid):
            return fail("NOT_FOUND", f"Provider 不存在: {pid}")
        _save_to_config(config_loader, router, config_path)
        if ptype == "embedding":
            _rebind_memory_brains_for_embedding(router, account_manager)
        if ptype == "rerank":
            _rebind_memory_brains_for_rerank(router, account_manager)
        return ok(message="Provider 已删除")

    async def update_by_type(request: Request) -> JSONResponse:
        """更新 Provider"""
        ptype = request.path_params.get("type")
        pid = request.path_params.get("id")
        if ptype not in PROVIDER_TYPES:
            return fail_invalid_input(f"未知 Provider 类型: {ptype}")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return fail_invalid_input("请求体必须是 JSON 对象")
            if not router.update_provider(ptype, pid, body):
                return fail("NOT_FOUND", f"Provider 不存在: {pid}")
            _save_to_config(config_loader, router, config_path)
            if ptype == "embedding":
                _rebind_memory_brains_for_embedding(router, account_manager)
            if ptype == "rerank":
                _rebind_memory_brains_for_rerank(router, account_manager)
            return ok(router.get_provider_by_type(ptype, pid).get_info(), "Provider 已更新")
        except Exception as e:
            logger.error(f"更新 Provider 失败: {e}", exc_info=True)
            return fail_internal()

    async def test_by_type(request: Request) -> JSONResponse:
        """测试 Provider 连接（按类型调用不同测试方法）"""
        ptype = request.path_params.get("type")
        pid = request.path_params.get("id")
        if ptype not in PROVIDER_TYPES:
            return fail_invalid_input(f"未知 Provider 类型: {ptype}")
        provider = router.get_provider_by_type(ptype, pid)
        if not provider:
            return fail("NOT_FOUND", f"Provider 不存在: {pid}")
        try:
            # 按类型选择测试方法
            if ptype == "chat":
                success, err_msg = await provider.test()
            elif ptype == "vision":
                success, err_msg = await provider.test_vision()
            elif ptype == "embedding":
                success, err_msg = await provider.test_embedding()
            elif ptype == "asr":
                success, err_msg = await provider.test_asr()
            elif ptype == "image":
                success, err_msg = await provider.test_image()
            elif ptype == "rerank":
                success, err_msg = await provider.test_rerank()
            else:
                success, err_msg = await provider.test()

            if success:
                msg = "连接成功" if not err_msg else f"连接成功（{err_msg}）"
                return ok({"connected": True, "model": provider.model}, msg)
            else:
                return fail("CONNECTION_FAILED", f"连接失败: {err_msg}")
        except Exception as e:
            return fail("CONNECTION_FAILED", f"测试失败: {e}")

    return [
        Route("/api/model-routing", get_overview, methods=["GET"]),
        Route("/api/model-routing", update_routing, methods=["PATCH"]),
        Route("/api/model-routing/{type}", list_by_type, methods=["GET"]),
        Route("/api/model-routing/{type}", add_by_type, methods=["POST"]),
        Route("/api/model-routing/{type}/{id}", delete_by_type, methods=["DELETE"]),
        Route("/api/model-routing/{type}/{id}", update_by_type, methods=["PATCH"]),
        Route("/api/model-routing/{type}/{id}/test", test_by_type, methods=["POST"]),
    ]
