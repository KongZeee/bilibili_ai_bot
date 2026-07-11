"""
文生图配置 API 路由

提供：
- GET   /api/image-generation  - 获取文生图配置（脱敏）
- PATCH /api/image-generation  - 更新文生图配置
"""
import logging
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal

logger = logging.getLogger("bilibot.api.image_generation")


def _mask_config(cfg: dict) -> dict:
    """脱敏 api_key"""
    if not isinstance(cfg, dict):
        return cfg
    masked = dict(cfg)
    if masked.get("api_key"):
        masked["api_key"] = "***已配置***"
    return masked


def create_image_generation_routes(config_loader, config_path: str):
    """创建文生图配置路由"""

    async def get_image_generation(request: Request) -> JSONResponse:
        """获取文生图配置"""
        try:
            raw = config_loader.get_raw_config()
            ig = raw.get("image_generation", {})
            if not isinstance(ig, dict):
                ig = {}
            dp = raw.get("dynamic_publish", {})
            with_image = dp.get("with_image", False) if isinstance(dp, dict) else False
            return ok({"image_generation": _mask_config(ig), "with_image": with_image})
        except Exception as e:
            return fail_internal(str(e))

    async def update_image_generation(request: Request) -> JSONResponse:
        """更新文生图配置"""
        try:
            body = await request.json()
            raw = config_loader.get_raw_config()

            # 更新 image_generation 段
            ig = raw.get("image_generation", {})
            if not isinstance(ig, dict):
                ig = {}
            for key in ("enabled", "api_key", "base_url", "model", "default_size", "timeout"):
                if key in body:
                    val = body[key]
                    if key == "enabled":
                        val = bool(val)
                    elif key == "timeout":
                        val = int(val)
                    # api_key 占位符不覆盖
                    if key == "api_key" and val == "***已配置***":
                        continue
                    ig[key] = val
            raw["image_generation"] = ig

            # 同步更新 dynamic_publish.with_image
            if "with_image" in body:
                dp = raw.get("dynamic_publish", {})
                if not isinstance(dp, dict):
                    dp = {}
                dp["with_image"] = bool(body["with_image"])
                raw["dynamic_publish"] = dp

            raw["config_revision"] = int(raw.get("config_revision", 0)) + 1
            config_loader.save_config(raw, config_path)
            logger.info(f"文生图配置已更新: enabled={ig.get('enabled')}, model={ig.get('model')}")
            return ok({
                "image_generation": _mask_config(ig),
                "with_image": raw.get("dynamic_publish", {}).get("with_image", False),
            })
        except Exception as e:
            logger.error(f"更新文生图配置失败: {e}", exc_info=True)
            return fail_internal(str(e))

    return [
        Route("/api/image-generation", get_image_generation, methods=["GET"]),
        Route("/api/image-generation", update_image_generation, methods=["PATCH"]),
    ]
