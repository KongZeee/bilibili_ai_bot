"""
视频理解配置 API 路由

提供：
- GET   /api/video-analysis  - 获取视频理解配置（脱敏）
- PATCH /api/video-analysis  - 更新视频理解配置
"""
import logging
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal

logger = logging.getLogger("bilibot.api.video_analysis")


def _mask_config(cfg: dict) -> dict:
    """脱敏 ASR api_key"""
    if not isinstance(cfg, dict):
        return cfg
    masked = dict(cfg)
    asr = masked.get("asr", {})
    if isinstance(asr, dict) and asr.get("api_key"):
        asr = dict(asr)
        asr["api_key"] = "***已配置***"
        masked["asr"] = asr
    return masked


def _merge_video_analysis(raw: dict, updates: dict) -> dict:
    """将前端提交的 updates 合并到 raw['video_analysis']"""
    va = raw.get("video_analysis", {})
    if not isinstance(va, dict):
        va = {}

    # 顶层标量字段
    for key in ("enabled", "frame_extractor", "scenedetect_threshold",
                "image_max_size", "vision_window_size", "temp_dir",
                # CFG-604：VID-503 资源边界配置
                "max_duration_seconds", "max_download_bytes",
                "max_concurrent_global", "max_concurrent_per_account",
                "download_timeout_seconds", "preprocess_timeout_seconds",
                "analysis_timeout_seconds", "local_whisper_enabled",
                "max_local_whisper_workers", "temp_disk_quota_bytes"):
        if key in updates:
            val = updates[key]
            if key in ("enabled", "local_whisper_enabled"):
                val = bool(val)
            elif key in ("scenedetect_threshold",):
                val = float(val)
            elif key in ("image_max_size", "vision_window_size",
                         "max_duration_seconds", "max_download_bytes",
                         "max_concurrent_global", "max_concurrent_per_account",
                         "download_timeout_seconds", "preprocess_timeout_seconds",
                         "analysis_timeout_seconds", "max_local_whisper_workers",
                         "temp_disk_quota_bytes"):
                val = int(val)
            va[key] = val

    # ASR 子段
    if "asr" in updates and isinstance(updates["asr"], dict):
        asr = va.get("asr", {})
        if not isinstance(asr, dict):
            asr = {}
        asr = dict(asr)
        for key in ("model", "api_key", "base_url",
                     "whisper_model_size", "whisper_device", "whisper_compute_type"):
            if key in updates["asr"]:
                val = updates["asr"][key]
                # api_key 为占位符时不覆盖
                if key == "api_key" and val == "***已配置***":
                    continue
                asr[key] = val
        va["asr"] = asr

    raw["video_analysis"] = va
    return raw


def create_video_analysis_routes(config_loader, config_path: str):
    """创建视频理解配置路由"""

    async def get_video_analysis(request: Request) -> JSONResponse:
        """获取视频理解配置"""
        try:
            raw = config_loader.get_raw_config()
            va = raw.get("video_analysis", {})
            if not isinstance(va, dict):
                va = {}
            return ok(_mask_config(va))
        except Exception as e:
            return fail_internal(str(e))

    async def update_video_analysis(request: Request) -> JSONResponse:
        """更新视频理解配置"""
        try:
            body = await request.json()
            raw = config_loader.get_raw_config()
            raw = _merge_video_analysis(raw, body)
            raw["config_revision"] = int(raw.get("config_revision", 0)) + 1
            config_loader.save_config(raw, config_path)
            va = raw.get("video_analysis", {})
            logger.info(f"视频理解配置已更新: enabled={va.get('enabled')}, frame_extractor={va.get('frame_extractor')}")
            return ok(_mask_config(va))
        except Exception as e:
            logger.error(f"更新视频理解配置失败: {e}", exc_info=True)
            return fail_internal(str(e))

    return [
        Route("/api/video-analysis", get_video_analysis, methods=["GET"]),
        Route("/api/video-analysis", update_video_analysis, methods=["PATCH"]),
    ]
