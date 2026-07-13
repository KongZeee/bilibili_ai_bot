"""
视频理解配置 API 路由

提供：
- GET   /api/video-analysis        - 获取视频理解配置（脱敏）
- PATCH /api/video-analysis        - 更新视频理解配置
- POST  /api/video-analysis/test   - 测试视频理解（下载并分析）
"""
import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal, fail_invalid_input

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
                "image_max_size", "vision_window_size",
                "vision_requests_per_minute", "temp_dir",
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
                          "vision_requests_per_minute",
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

    # Task 28：local_whisper 子段 — 更新 asr_providers 列表中的 local_whisper 项
    if "local_whisper" in updates and isinstance(updates["local_whisper"], dict):
        lw_updates = updates["local_whisper"]
        asr_providers = raw.get("asr_providers", [])
        if not isinstance(asr_providers, list):
            asr_providers = []
        # 查找现有 local_whisper 项（含 model_size 或 whisper_device 键的 dict）
        lw_index = None
        for i, item in enumerate(asr_providers):
            if isinstance(item, dict) and ("model_size" in item or "whisper_device" in item):
                lw_index = i
                break
        if lw_index is not None:
            # 合并更新到现有项
            merged_lw = dict(asr_providers[lw_index])
            for k in ("enabled", "model_size", "device", "compute_type"):
                if k in lw_updates:
                    val = lw_updates[k]
                    if k == "enabled":
                        val = bool(val)
                    merged_lw[k] = val
            asr_providers[lw_index] = merged_lw
        else:
            # 新建 local_whisper 项
            new_lw = {}
            for k in ("enabled", "model_size", "device", "compute_type"):
                if k in lw_updates:
                    val = lw_updates[k]
                    if k == "enabled":
                        val = bool(val)
                    new_lw[k] = val
            if new_lw:
                asr_providers = list(asr_providers) + [new_lw]
        raw["asr_providers"] = asr_providers

    raw["video_analysis"] = va
    return raw


def _extract_bvid(text: str) -> str:
    """从用户输入提取 BV 号（支持纯 BV 号、完整 URL、短链接）"""
    if not text:
        return ""
    text = text.strip()
    # 直接匹配 BV 开头的号
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if m:
        return m.group(1)
    return ""


async def _archive_test_analysis(acc, *, bvid: str, vinfo: dict, result: dict) -> None:
    """Persist complete manual-test extraction before its artifacts are removed."""
    brain = getattr(acc, "memory_brain", None)
    archive = getattr(brain, "archive_observation_async", None)
    if not callable(archive):
        raise RuntimeError("V6 memory brain is unavailable for video test archival")

    from bilibot.memory_brain.ingestion import video_observation

    raw_tags = vinfo.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [item.strip() for item in raw_tags.split(",") if item.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(item) for item in raw_tags if item]
    else:
        tags = []
    account_id = str(getattr(acc, "account_id", "") or "default")
    envelope = video_observation(
        account_id=account_id,
        observation_key=f"manual-test:{bvid}:draft",
        bvid=bvid,
        oid=str(vinfo.get("aid") or bvid),
        title=str(vinfo.get("title") or ""),
        owner=str((vinfo.get("owner") or {}).get("name") or ""),
        context={"metadata": vinfo, "audiovisual": result},
        tags=tags,
        persona_id=str(getattr(acc, "persona_id", "") or ""),
    )
    canonical = {
        "metadata": envelope.metadata,
        "sources": [
            {
                "source_type": source.source_type,
                "external_id": source.external_id,
                "full_text": source.full_text,
                "data": source.data,
            }
            for source in envelope.sources
        ],
    }
    serialized = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
    content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]
    envelope = replace(
        envelope,
        idempotency_key=f"video:{account_id}:manual-test:{bvid}:{content_hash}",
    )
    archive_result = archive(envelope)
    if inspect.isawaitable(archive_result):
        archive_result = await archive_result
    committed = (
        archive_result.get("source_committed", False)
        if isinstance(archive_result, Mapping)
        else getattr(archive_result, "source_committed", False)
    )
    if committed is not True:
        raise RuntimeError("V6 memory source archive did not commit")


def _cleanup_test_analysis_artifacts(video_file: str, result: dict) -> None:
    """Remove only the exact media/work paths returned by a committed analysis."""
    paths = [Path(video_file)]
    work_dir = result.get("work_dir") if isinstance(result, dict) else None
    if work_dir:
        paths.append(Path(str(work_dir)))
    for path in sorted(dict.fromkeys(paths), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("[test] 清理已归档视频临时文件失败 %s: %s", path, type(exc).__name__)


def create_video_analysis_routes(config_loader, config_path: str, account_manager=None):
    """创建视频理解配置路由

    Args:
        config_loader: 配置加载器
        config_path: 配置文件路径
        account_manager: 账号管理器（用于 test 端点获取默认账号的视频下载/分析能力）
    """

    async def get_video_analysis(request: Request) -> JSONResponse:
        """获取视频理解配置"""
        try:
            raw = config_loader.get_raw_config()
            va = raw.get("video_analysis", {})
            if not isinstance(va, dict):
                va = {}
            return ok(_mask_config(va))
        except Exception as e:
            logger.error(f"获取视频理解配置失败: {e}", exc_info=True)
            return fail_internal()

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
            return fail_internal()

    async def test_video_analysis(request: Request) -> JSONResponse:
        """测试视频理解：下载视频并执行视听双轨分析

        Body: { "video_url": "BVxxxxxx 或完整 URL", "config": {...可选覆盖} }
        """
        try:
            body = await request.json()
            video_url = body.get("video_url") or body.get("url") or ""
            bvid = _extract_bvid(video_url)
            if not bvid:
                return fail_invalid_input("无法识别视频 BV 号，请输入 BV 开头的 12 位代码或 B站视频链接")

            if account_manager is None:
                return fail("NO_ACCOUNT_MANAGER", "账号管理器未初始化，无法执行测试")

            # 取默认账号
            acc = account_manager.get_default()
            if acc is None:
                return fail("NO_ACCOUNT", "没有可用的账号来执行视频下载")

            # 检查视频理解服务是否可用
            vu = getattr(acc, "video_understanding", None)
            if vu is None or not vu.is_available():
                return fail("SERVICE_DISABLED",
                            "视频理解服务未启用，请先在配置中开启并确保 LLM 已配置视觉模型")

            bili = getattr(acc, "bili", None)
            if bili is None:
                return fail("NO_BILI_CLIENT", "B站客户端未初始化")

            # 1. 获取视频信息（cid）
            data, _ = await bili._http_get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
            )
            if not data or not data.get("data"):
                return fail("VIDEO_NOT_FOUND", f"无法获取视频信息: {bvid}")

            vinfo = data["data"]
            title = vinfo.get("title", "")
            cid = vinfo.get("cid", 0)
            if not cid:
                pages = vinfo.get("pages", [])
                if pages:
                    cid = pages[0].get("cid", 0)
            if not cid:
                return fail("NO_CID", f"无法获取视频 CID（视频: {title}）")

            logger.info(f"[test] 视频信息: bvid={bvid}, cid={cid}, title={title}, duration={vinfo.get('duration', 0)}s")

            duration = vinfo.get("duration", 0)
            owner = vinfo.get("owner", {}).get("name", "")

            # 2. 下载视频到临时目录
            data_dir = getattr(acc, "account_data_dir", "") or os.path.join(os.getcwd(), "data")
            video_temp_dir = os.path.join(data_dir, "video_temp")
            save_path = os.path.join(video_temp_dir, f"test_{bvid}")
            video_file = await bili.download_video(bvid, cid, save_path, quality=32)

            if not video_file or not os.path.exists(video_file):
                return fail("DOWNLOAD_FAILED", f"视频下载失败: {bvid}")

            logger.info(f"[test] 视频已下载: {video_file}, 开始分析...")

            # 3. 执行视频理解
            result = await asyncio.wait_for(
                vu.understand(
                    video_file,
                    defer_cleanup=True,
                    require_complete_audio=True,
                    require_complete_visual=True,
                ),
                timeout=600,
            )
            if not isinstance(result, dict):
                raise RuntimeError("视频理解返回了无效结果")

            behavior_log = result.get("behavior_log", "")
            degradation = result.get("degradation_reason", "")

            if degradation:
                logger.warning(
                    "[test] 视频提取未完成，保留媒体与处理目录: %s",
                    degradation,
                )
                return ok({
                    "title": title,
                    "owner": owner,
                    "duration": duration,
                    "bvid": bvid,
                    "degradation_reason": degradation,
                    "description": f"视频因资源限制降级为元数据分析: {degradation}",
                    "frames": [],
                })

            # 4. 完整提取内容提交到账号脑库后，才允许清理媒体与处理目录。
            await _archive_test_analysis(
                acc,
                bvid=bvid,
                vinfo=vinfo,
                result=result,
            )
            _cleanup_test_analysis_artifacts(video_file, result)

            # 截取行为日志前 2000 字作为描述
            desc = behavior_log[:2000] if behavior_log else "（未生成行为日志）"

            return ok({
                "title": title,
                "owner": owner,
                "duration": duration,
                "bvid": bvid,
                "description": desc,
                "behavior_log": behavior_log,
                "degradation_reason": degradation,
                "frames": [],
            })

        except asyncio.TimeoutError:
            return fail("ANALYSIS_TIMEOUT", "视频分析超时（超过 10 分钟）")
        except Exception as e:
            logger.error(f"[test] 视频理解测试失败: {e}", exc_info=True)
            return fail_internal()

    return [
        Route("/api/video-analysis", get_video_analysis, methods=["GET"]),
        Route("/api/video-analysis", update_video_analysis, methods=["PATCH"]),
        Route("/api/video-analysis/test", test_video_analysis, methods=["POST"]),
    ]
