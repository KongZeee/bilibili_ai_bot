"""
文生图配置 API 路由

提供：
- GET   /api/image-generation       - 获取文生图配置（脱敏）
- PATCH /api/image-generation       - 更新文生图配置
- POST  /api/image-generation/test  - 测试文生图
"""
import base64
import logging
import re
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from bilibot.image.styles import (
    CUSTOM_IMAGE_STYLE,
    DEFAULT_IMAGE_STYLE,
    MAX_CUSTOM_STYLE_CHARS,
    apply_image_style,
    image_style_options,
    is_supported_image_style,
    normalize_custom_style,
    normalize_image_style,
)

from .responses import ok, fail, fail_internal

logger = logging.getLogger("bilibot.api.image_generation")

# default_size 格式：WIDTHxHEIGHT（如 1024x768）
_SIZE_RE = re.compile(r"^\d+x\d+$")


def _mask_config(cfg: dict) -> dict:
    """脱敏 api_key"""
    if not isinstance(cfg, dict):
        return cfg
    masked = dict(cfg)
    if masked.get("api_key"):
        masked["api_key"] = "***已配置***"
    return masked


def _dynamic_image_settings(raw: dict) -> dict:
    dp = raw.get("dynamic_publish", {}) if isinstance(raw, dict) else {}
    if not isinstance(dp, dict):
        dp = {}
    style = normalize_image_style(dp.get("image_style", DEFAULT_IMAGE_STYLE))
    return {
        "with_image": bool(dp.get("with_image", False)),
        "image_style": style,
        "image_style_custom": normalize_custom_style(
            dp.get("image_style_custom", "")
        ),
        "image_style_options": image_style_options(),
    }


def create_image_generation_routes(config_loader, config_path: str):
    """创建文生图配置路由"""

    async def get_image_generation(request: Request) -> JSONResponse:
        """获取文生图配置"""
        try:
            raw = config_loader.get_raw_config()
            ig = raw.get("image_generation", {})
            if not isinstance(ig, dict):
                ig = {}
            return ok({
                "image_generation": _mask_config(ig),
                **_dynamic_image_settings(raw),
            })
        except Exception as e:
            logger.error(f"获取文生图配置失败: {e}", exc_info=True)
            return fail_internal()

    async def update_image_generation(request: Request) -> JSONResponse:
        """更新文生图配置"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)

            # 预校验（失败不写盘）
            for key in ("enabled", "api_key", "base_url", "model", "default_size", "timeout"):
                if key not in body:
                    continue
                val = body[key]
                if key == "timeout":
                    try:
                        max(1, min(int(val), 300))
                    except (ValueError, TypeError):
                        return fail("INVALID_INPUT", "timeout 必须是整数", status_code=400)
                elif key == "base_url":
                    if not isinstance(val, str) or not val.startswith(("http://", "https://")):
                        return fail("INVALID_INPUT", "base_url 必须以 http:// 或 https:// 开头", status_code=400)
                elif key == "default_size":
                    if not isinstance(val, str) or not _SIZE_RE.match(val):
                        return fail("INVALID_INPUT", "default_size 格式必须为 WIDTHxHEIGHT（如 1024x768）", status_code=400)

            if "image_style" in body and not is_supported_image_style(
                body.get("image_style")
            ):
                return fail(
                    "INVALID_INPUT",
                    "image_style 不是受支持的配图风格",
                    status_code=400,
                )
            if "image_style_custom" in body:
                raw_custom = body.get("image_style_custom")
                if not isinstance(raw_custom, str):
                    return fail(
                        "INVALID_INPUT",
                        "image_style_custom 必须是字符串",
                        status_code=400,
                    )
                if len(raw_custom) > MAX_CUSTOM_STYLE_CHARS:
                    return fail(
                        "INVALID_INPUT",
                        f"image_style_custom 最多 {MAX_CUSTOM_STYLE_CHARS} 个字符",
                        status_code=400,
                    )

            if normalize_image_style(body.get("image_style")) == CUSTOM_IMAGE_STYLE:
                current = _dynamic_image_settings(config_loader.get_raw_config())
                custom = normalize_custom_style(
                    body.get(
                        "image_style_custom",
                        current.get("image_style_custom", ""),
                    )
                )
                if not custom:
                    return fail(
                        "INVALID_INPUT",
                        "选择自定义风格时必须填写风格描述",
                        status_code=400,
                    )

            def _mutate(raw: dict) -> None:
                ig = raw.get("image_generation", {})
                if not isinstance(ig, dict):
                    ig = {}
                for key in ("enabled", "api_key", "base_url", "model", "default_size", "timeout"):
                    if key not in body:
                        continue
                    val = body[key]
                    if key == "enabled":
                        val = bool(val)
                    elif key == "timeout":
                        val = max(1, min(int(val), 300))
                    if key == "api_key" and val == "***已配置***":
                        continue
                    ig[key] = val
                raw["image_generation"] = ig
                if "with_image" in body:
                    dp = raw.get("dynamic_publish", {})
                    if not isinstance(dp, dict):
                        dp = {}
                    dp["with_image"] = bool(body["with_image"])
                else:
                    dp = raw.get("dynamic_publish", {})
                    if not isinstance(dp, dict):
                        dp = {}
                if "image_style" in body:
                    dp["image_style"] = normalize_image_style(body["image_style"])
                if "image_style_custom" in body:
                    dp["image_style_custom"] = normalize_custom_style(
                        body["image_style_custom"]
                    )
                raw["dynamic_publish"] = dp
                raw["config_revision"] = int(raw.get("config_revision", 0) or 0) + 1

            if hasattr(config_loader, "atomic_update"):
                raw = config_loader.atomic_update(config_path, _mutate)
            else:
                raw = config_loader.get_raw_config()
                _mutate(raw)
                config_loader.save_config(raw, config_path)

            ig = raw.get("image_generation", {}) if isinstance(raw, dict) else {}
            logger.info(f"文生图配置已更新: enabled={ig.get('enabled')}, model={ig.get('model')}")
            return ok({
                "image_generation": _mask_config(ig if isinstance(ig, dict) else {}),
                **_dynamic_image_settings(raw),
            })
        except Exception as e:
            logger.error(f"更新文生图配置失败: {e}", exc_info=True)
            return fail_internal()

    async def test_image_generation(request: Request) -> JSONResponse:
        """测试文生图：用用户 prompt 生成图片，返回 base64"""
        try:
            body = {}
            try:
                body = await request.json()
            except Exception:
                pass
            prompt = body.get("prompt", "").strip()
            if not prompt:
                return fail("INVALID_INPUT", "请输入 prompt 提示词", status_code=400)

            raw = config_loader.get_raw_config()
            settings = _dynamic_image_settings(raw)
            requested_style = body.get("style", settings["image_style"])
            if not is_supported_image_style(requested_style):
                return fail(
                    "INVALID_INPUT",
                    "style 不是受支持的配图风格",
                    status_code=400,
                )
            requested_custom = body.get(
                "custom_style",
                settings["image_style_custom"],
            )
            if not isinstance(requested_custom, str):
                return fail(
                    "INVALID_INPUT",
                    "custom_style 必须是字符串",
                    status_code=400,
                )
            if len(requested_custom) > MAX_CUSTOM_STYLE_CHARS:
                return fail(
                    "INVALID_INPUT",
                    f"custom_style 最多 {MAX_CUSTOM_STYLE_CHARS} 个字符",
                    status_code=400,
                )
            requested_custom = normalize_custom_style(requested_custom)
            if normalize_image_style(requested_style) == CUSTOM_IMAGE_STYLE and not requested_custom:
                return fail(
                    "INVALID_INPUT",
                    "选择自定义风格时必须填写风格描述",
                    status_code=400,
                )
            effective_prompt = apply_image_style(
                prompt,
                style=requested_style,
                custom_style=requested_custom,
            )

            # 从 model_routing + image_providers 获取当前路由的 provider 配置
            routing = raw.get("model_routing", {}) or {}
            image_pid = routing.get("image", "")
            providers = raw.get("image_providers", []) or []
            provider_cfg = None
            for p in providers:
                if p.get("id") == image_pid and p.get("enabled", True):
                    provider_cfg = p
                    break
            if not provider_cfg:
                return fail("IMAGE_NOT_CONFIGURED", "未找到已启用的文生图 Provider，请到模型分配页配置", status_code=400)
            has_key = bool(provider_cfg.get("api_key")) or bool(provider_cfg.get("api_keys"))
            if not has_key or not provider_cfg.get("model"):
                return fail("IMAGE_NOT_CONFIGURED", "文生图 Provider 未配置 api_key/api_keys 或 model", status_code=400)

            from bilibot.image.provider import ImageProvider
            provider = ImageProvider(provider_cfg)
            size = body.get("size") or provider_cfg.get("default_size", "1024x768")
            try:
                image_bytes = await provider.generate(effective_prompt, size=size)
            finally:
                await provider.close()
            if image_bytes:
                b64 = base64.b64encode(image_bytes).decode("utf-8")
                return ok({
                    "success": True,
                    "image_b64": b64,
                    "size": len(image_bytes),
                    "prompt": effective_prompt,
                    "style": normalize_image_style(requested_style),
                    "model": provider_cfg.get("model", ""),
                }, "图片生成成功")
            return fail("IMAGE_GENERATION_FAILED", "文生图返回空结果", status_code=500)
        except Exception as e:
            logger.error(f"测试文生图失败: {e}", exc_info=True)
            return fail_internal()


    return [
        Route("/api/image-generation", get_image_generation, methods=["GET"]),
        Route("/api/image-generation", update_image_generation, methods=["PATCH"]),
        Route("/api/image-generation/test", test_image_generation, methods=["POST"]),
    ]
