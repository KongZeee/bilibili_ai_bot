"""
LLM Provider 管理 API 路由（PRD V2 多 LLM 架构）

提供：
- GET    /api/llm-providers              - 列出所有 Provider（脱敏）
- POST   /api/llm-providers              - 添加 Provider
- DELETE /api/llm-providers/{id}         - 删除 Provider
- PATCH  /api/llm-providers/{id}         - 更新 Provider
- POST   /api/llm-providers/{id}/set-default - 设为默认
- POST   /api/llm-providers/{id}/test    - 测试连接
"""
import logging
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal

logger = logging.getLogger("bilibot.api.llm_providers")


def _save_llm_to_config(config_loader, llm_manager, config_path: str):
    """将 LLM 配置持久化到 config.yaml"""
    try:
        def _mutate(raw: dict) -> None:
            llm_dict = llm_manager.save_to_config()
            # V3：优先写 chat_providers；兼容仍写 llm_providers 的旧 save_to_config
            if "chat_providers" in llm_dict:
                raw["chat_providers"] = llm_dict["chat_providers"]
            if "llm_providers" in llm_dict:
                raw["llm_providers"] = llm_dict["llm_providers"]
            if "default_llm" in llm_dict:
                raw["default_llm"] = llm_dict["default_llm"]
            if "model_routing" in llm_dict:
                raw["model_routing"] = llm_dict["model_routing"]
            raw["config_revision"] = int(raw.get("config_revision", 0) or 0) + 1

        if hasattr(config_loader, "atomic_update"):
            config_loader.atomic_update(config_path, _mutate)
        else:
            raw = config_loader.get_raw_config()
            _mutate(raw)
            config_loader.save_config(raw, config_path)
        return True
    except Exception as e:
        logger.error(f"持久化 LLM 配置失败: {e}")
        return False


def _save_accounts_to_config_local(config_loader, account_manager, config_path: str):
    """将账号配置持久化到 config.yaml（force 删除清除引用后保存）"""
    try:
        def _mutate(raw: dict) -> None:
            accounts_dict = account_manager.save_to_config()
            raw["accounts"] = accounts_dict["accounts"]
            raw["default_account"] = accounts_dict["default_account"]
            raw["config_revision"] = int(raw.get("config_revision", 0) or 0) + 1

        if hasattr(config_loader, "atomic_update"):
            config_loader.atomic_update(config_path, _mutate)
        else:
            raw = config_loader.get_raw_config()
            _mutate(raw)
            config_loader.save_config(raw, config_path)
        return True
    except Exception as e:
        logger.error(f"持久化账号配置失败: {e}")
        return False


def create_llm_providers_routes(
    llm_manager,
    config_loader,
    config_path: str = "config.yaml",
    account_manager=None,
):
    """创建 LLM Provider 管理路由

    Args:
        account_manager: AccountManager 实例（用于 LLM-501 删除引用检查）。
                         None 时跳过引用检查（向后兼容）。
    """

    async def list_providers(request: Request) -> JSONResponse:
        # ModelRouter.list_providers(ptype) 需要类型参数，这里合并所有类型返回扁平列表
        from bilibot.llm.router import PROVIDER_TYPES
        all_providers = []
        for ptype in PROVIDER_TYPES:
            for info in llm_manager.list_providers(ptype):
                info = dict(info)
                info["type"] = ptype
                all_providers.append(info)
        return ok(all_providers)

    async def add_provider(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            has_keys = bool(body.get("api_key")) or bool(body.get("api_keys"))
            if not has_keys:
                return fail("VALIDATION_ERROR", "api_key / api_keys 不能为空")
            if not body.get("model"):
                return fail("VALIDATION_ERROR", "model 不能为空")
            llm_id = llm_manager.add_provider(body)
            _save_llm_to_config(config_loader, llm_manager, config_path)
            return ok(llm_manager.get_provider(llm_id).get_info(), "LLM Provider 添加成功")
        except ValueError as e:
            logger.warning("添加 LLM Provider 校验失败: %s", e)
            return fail("VALIDATION_ERROR", "Provider 参数不合法")
        except Exception as e:
            logger.error(f"添加 LLM Provider 失败: {e}", exc_info=True)
            return fail_internal()

    async def delete_provider(request: Request) -> JSONResponse:
        llm_id = request.path_params.get("id")
        if llm_id == llm_manager.get_default_id() and len(llm_manager) <= 1:
            return fail("LAST_PROVIDER", "不能删除最后一个默认 Provider")

        # PRD-V5 §5.3 LLM-501：删除前检查账号引用
        force = request.query_params.get("force", "").lower() in ("true", "1", "yes")
        referencing_accounts = []
        if account_manager is not None:
            for cfg in account_manager.config_registry.list_all():
                if cfg.get("llm_id", "") == llm_id:
                    referencing_accounts.append(cfg.get("id", ""))

            if referencing_accounts:
                if not force:
                    return fail(
                        "LLM_PROVIDER_IN_USE",
                        f"Provider 被以下账号引用，无法删除: {', '.join(referencing_accounts)}",
                        details={"referencing_accounts": referencing_accounts},
                        status_code=409,
                    )
                # force=true：清除引用（设为空，回退到默认）
                for acc_id in referencing_accounts:
                    account_manager.update_account_config(acc_id, {"llm_id": ""})
                    acc = account_manager.get_account(acc_id)
                    if acc:
                        acc.llm_id = ""
                        acc.account_config["llm_id"] = ""
                _save_accounts_to_config_local(config_loader, account_manager, config_path)

        if not llm_manager.remove_provider(llm_id):
            return fail("NOT_FOUND", f"Provider 不存在: {llm_id}")
        _save_llm_to_config(config_loader, llm_manager, config_path)
        if referencing_accounts and force:
            return ok(
                {"cleared_references": referencing_accounts},
                f"Provider 已删除，已清除 {len(referencing_accounts)} 个账号的引用",
            )
        return ok(message="Provider 已删除")

    async def update_provider(request: Request) -> JSONResponse:
        try:
            llm_id = request.path_params.get("id")
            body = await request.json()
            if not llm_manager.update_provider(llm_id, body):
                return fail("NOT_FOUND", f"Provider 不存在: {llm_id}")
            _save_llm_to_config(config_loader, llm_manager, config_path)
            return ok(llm_manager.get_provider(llm_id).get_info(), "Provider 已更新")
        except Exception as e:
            logger.error(f"更新 LLM Provider 失败: {e}", exc_info=True)
            return fail_internal()

    async def set_default(request: Request) -> JSONResponse:
        llm_id = request.path_params.get("id")
        if not llm_manager.set_default(llm_id):
            return fail("NOT_FOUND", f"Provider 不存在: {llm_id}")
        _save_llm_to_config(config_loader, llm_manager, config_path)
        return ok(message=f"默认 LLM 已设置为: {llm_id}")

    async def test_provider(request: Request) -> JSONResponse:
        llm_id = request.path_params.get("id")
        provider = llm_manager.get_provider(llm_id)
        if not provider:
            return fail("NOT_FOUND", f"Provider 不存在: {llm_id}")
        try:
            success, err_msg = await provider.test()
            if success:
                return ok({"connected": True, "model": provider.model}, "连接成功")
            else:
                return fail("CONNECTION_FAILED", f"连接失败: {err_msg or '请检查 api_key / base_url / model'}")
        except Exception as e:
            logger.error(f"测试 LLM Provider 失败: {e}", exc_info=True)
            return fail("CONNECTION_FAILED", "连接测试失败，请检查 api_key / base_url / model")

    return [
        Route("/api/llm-providers", list_providers, methods=["GET"]),
        Route("/api/llm-providers", add_provider, methods=["POST"]),
        Route("/api/llm-providers/{id}", delete_provider, methods=["DELETE"]),
        Route("/api/llm-providers/{id}", update_provider, methods=["PATCH"]),
        Route("/api/llm-providers/{id}/set-default", set_default, methods=["POST"]),
        Route("/api/llm-providers/{id}/test", test_provider, methods=["POST"]),
    ]
