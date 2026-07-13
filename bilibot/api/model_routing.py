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

# 功能名称映射
FEATURE_LABELS = {
    "chat": "对话（主动回复/动态/记忆提取）",
    "vision": "视频视觉轨",
    "embedding": "记忆向量检索",
    "asr": "视频音频轨",
    "image": "动态配图",
}


def _save_to_config(config_loader, router, config_path: str):
    """持久化到 config.yaml"""
    try:
        raw = config_loader.get_raw_config()
        saved = router.save_to_config()
        # 更新各 provider 列表
        for ptype in PROVIDER_TYPES:
            key = CONFIG_KEYS[ptype]
            if key in saved:
                raw[key] = saved[key]
        raw["model_routing"] = saved.get("model_routing", {})
        raw["allow_llm_fallback"] = saved.get("allow_llm_fallback", raw.get("allow_llm_fallback", False))
        raw["config_revision"] = int(raw.get("config_revision", 0)) + 1
        config_loader.save_config(raw, config_path)
        return True
    except Exception as e:
        logger.error(f"持久化模型配置失败: {e}", exc_info=True)
        return False


def create_model_routing_routes(router, config_loader, config_path: str = "config.yaml"):
    """创建模型路由管理路由"""

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
            if not body.get("model"):
                return fail("VALIDATION_ERROR", "model 不能为空")
            pid = router.add_provider(ptype, body)
            _save_to_config(config_loader, router, config_path)
            return ok(router.get_provider_by_type(ptype, pid).get_info(), "Provider 添加成功")
        except ValueError as e:
            return fail("VALIDATION_ERROR", str(e))
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
        return ok(message="Provider 已删除")

    async def update_by_type(request: Request) -> JSONResponse:
        """更新 Provider"""
        ptype = request.path_params.get("type")
        pid = request.path_params.get("id")
        if ptype not in PROVIDER_TYPES:
            return fail_invalid_input(f"未知 Provider 类型: {ptype}")
        try:
            body = await request.json()
            if not router.update_provider(ptype, pid, body):
                return fail("NOT_FOUND", f"Provider 不存在: {pid}")
            _save_to_config(config_loader, router, config_path)
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
