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

from bilibot.app.config_loader import is_sensitive_placeholder

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
    # 帧拼接开关会影响 max_keyframes 的合法上限，先取本次更新的值（未提交则用现值）。
    _pack_enabled = bool(updates.get("frame_pack_enabled", va.get("frame_pack_enabled", False)))
    try:
        _per_tile = int(updates.get("frames_per_tile", va.get("frames_per_tile", 4)))
    except (TypeError, ValueError):
        _per_tile = 4
    _per_tile = max(1, min(_per_tile, 9))
    for key in ("enabled", "frame_extractor", "scenedetect_threshold",
                "image_max_size", "max_keyframes", "vision_window_size",
                "vision_requests_per_minute", "temp_dir",
                # CFG-604：VID-503 资源边界配置
                "max_duration_seconds", "max_download_bytes",
                "max_concurrent_global", "max_concurrent_per_account",
                "download_timeout_seconds", "preprocess_timeout_seconds",
                "analysis_timeout_seconds", "local_whisper_enabled",
                "max_local_whisper_workers", "temp_disk_quota_bytes",
                # 视觉轨重试 / 成功率（运行时 VideoUnderstandingConfig 读取）
                "vision_frame_max_retries", "vision_frame_retry_backoff_seconds",
                "vision_min_success_ratio",
                # 帧拼接（九宫格连续帧）
                "frame_pack_enabled", "frames_per_tile", "frame_pack_tile_size"):
        if key in updates:
            val = updates[key]
            if key in ("enabled", "local_whisper_enabled", "frame_pack_enabled"):
                val = bool(val)
            elif key in ("scenedetect_threshold", "vision_frame_retry_backoff_seconds",
                         "vision_min_success_ratio"):
                val = float(val)
                if key == "vision_min_success_ratio":
                    val = max(0.0, min(val, 1.0))
                if key == "vision_frame_retry_backoff_seconds":
                    val = max(0.0, min(val, 30.0))
            elif key in ("image_max_size", "max_keyframes", "vision_window_size",
                          "vision_requests_per_minute",
                          "max_duration_seconds", "max_download_bytes",
                          "max_concurrent_global", "max_concurrent_per_account",
                          "download_timeout_seconds", "preprocess_timeout_seconds",
                          "analysis_timeout_seconds", "max_local_whisper_workers",
                          "temp_disk_quota_bytes", "vision_frame_max_retries",
                          "frames_per_tile", "frame_pack_tile_size"):
                val = int(val)
                if key == "max_keyframes":
                    val = max(1, min(val, 64 * (_per_tile if _pack_enabled else 1)))
                if key == "vision_frame_max_retries":
                    val = max(0, min(val, 5))
                if key == "frames_per_tile":
                    val = max(1, min(val, 9))
                if key == "frame_pack_tile_size":
                    val = max(256, min(val, 1024))
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
                if key == "api_key" and is_sensitive_placeholder(val):
                    continue
                asr[key] = val
        va["asr"] = asr

    # Task 28：local_whisper 子段 — 更新 asr_providers 列表中的 local_whisper 项
    if "local_whisper" in updates and isinstance(updates["local_whisper"], dict):
        lw_updates = updates["local_whisper"]
        asr_providers = raw.get("asr_providers", [])
        if not isinstance(asr_providers, list):
            asr_providers = []
        # 查找现有 local_whisper 项（含 model_size / device+compute_type / 固定 id）
        lw_index = None
        for i, item in enumerate(asr_providers):
            if not isinstance(item, dict):
                continue
            if (
                "model_size" in item
                or "whisper_device" in item
                or item.get("id") in ("local-whisper", "local_whisper")
                or ("device" in item and "compute_type" in item and "api_key" not in item)
            ):
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
            # 同步顶层 local_whisper_enabled，避免双轨不一致
            if "enabled" in lw_updates:
                va["local_whisper_enabled"] = bool(lw_updates["enabled"])
            asr_providers[lw_index] = merged_lw
        else:
            # 新建 local_whisper 项
            new_lw = {"id": "local-whisper"}
            for k in ("enabled", "model_size", "device", "compute_type"):
                if k in lw_updates:
                    val = lw_updates[k]
                    if k == "enabled":
                        val = bool(val)
                    new_lw[k] = val
            if len(new_lw) > 1:
                asr_providers = list(asr_providers) + [new_lw]
                if "enabled" in new_lw:
                    va["local_whisper_enabled"] = bool(new_lw["enabled"])
        raw["asr_providers"] = asr_providers

    raw["video_analysis"] = va
    return raw


def _extract_bvid(text: str) -> str:
    """从用户输入提取 BV 号（支持纯 BV 号、完整 URL、短链接）"""
    if not text:
        return ""
    text = text.strip()
    # BV + 10 位字符；要求前后不是同类字符，避免从 12 位以上的字符串中截出假 BV
    m = re.search(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])", text)
    if m:
        return m.group(1) if m.lastindex else m.group(0)
    return ""


_SHORTLINK_PATTERN = re.compile(r"((?:https?://)?b23\.tv/[0-9A-Za-z]+)")


def _extract_shortlink_url(text: str) -> str:
    """从用户输入提取 b23.tv 短链接，并补全 https scheme。"""
    if not text:
        return ""
    m = _SHORTLINK_PATTERN.search(text.strip())
    if not m:
        return ""
    url = m.group(1)
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


async def _resolve_video_bvid(text: str, bili) -> str:
    """提取 BV 号；短链接通过 BilibiliAPI.resolve_shortlink 解析后二次提取。"""
    bvid = _extract_bvid(text)
    if bvid:
        return bvid
    shortlink = _extract_shortlink_url(text)
    if not shortlink:
        return ""
    resolver = getattr(bili, "resolve_shortlink", None)
    if not callable(resolver):
        return ""
    try:
        location, _err = await resolver(shortlink)
    except Exception:
        return ""
    return _extract_bvid(location or "")


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
    title = str(vinfo.get("title") or "")
    owner = str((vinfo.get("owner") or {}).get("name") or "")
    behavior_log = ""
    if isinstance(result, dict):
        behavior_log = str(result.get("behavior_log") or "")
    video_detail = ""
    if behavior_log:
        try:
            from bilibot.memory_brain.gateway import MemoryModelGateway

            gateway = None
            brain = getattr(acc, "memory_brain", None)
            if brain is not None and getattr(brain, "gateway", None) is not None:
                gateway = brain.gateway
            else:
                gateway = MemoryModelGateway()
            video_detail = await gateway.summarize_video_detail(
                title=title,
                owner=owner,
                behavior_log=behavior_log,
                max_chars=2000,
            )
        except Exception:
            from bilibot.memory_brain.gateway import MemoryModelGateway

            video_detail = MemoryModelGateway.heuristic_video_detail(
                title=title,
                owner=owner,
                behavior_log=behavior_log,
                max_chars=2000,
            )
    redactor = getattr(getattr(acc, "memory_brain", None), "redactor", None)

    def _pseudo_actor(value) -> str:
        if redactor is not None and callable(
            getattr(redactor, "pseudonymize_identifier", None)
        ):
            return str(
                redactor.pseudonymize_identifier(value, namespace="uid")
            )
        return str(value or "")

    envelope = video_observation(
        account_id=account_id,
        observation_key=f"manual-test:{bvid}:draft",
        bvid=bvid,
        oid=str(vinfo.get("aid") or bvid),
        title=title,
        owner=owner,
        context={"metadata": vinfo, "audiovisual": result},
        tags=tags,
        persona_id=str(getattr(acc, "persona_id", "") or ""),
        video_detail=video_detail,
        pseudonymize_actor=_pseudo_actor,
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
        """更新视频理解配置，并热重载运行中账号的 VU / local_whisper。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return fail_invalid_input("请求体必须是 JSON 对象")

            def _mutate(raw: dict) -> None:
                _merge_video_analysis(raw, body)
                raw["config_revision"] = int(raw.get("config_revision", 0) or 0) + 1

            if hasattr(config_loader, "atomic_update"):
                raw = config_loader.atomic_update(config_path, _mutate)
            else:
                raw = config_loader.get_raw_config()
                _mutate(raw)
                config_loader.save_config(raw, config_path)

            va = raw.get("video_analysis", {}) if isinstance(raw, dict) else {}

            # 刷新 ModelRouter 的 local_whisper 缓存（关本地 Whisper 依赖此路径）
            try:
                llm_mgr = None
                if account_manager is not None:
                    llm_mgr = getattr(account_manager, "llm_manager", None)
                if llm_mgr is not None and hasattr(llm_mgr, "reload_local_whisper_from_config"):
                    llm_mgr.reload_local_whisper_from_config(raw)
            except Exception as e:
                logger.warning(f"刷新 local_whisper 路由缓存失败: {e}")

            # App 级全局并发 semaphore（启动时配置一次，保存后必须再刷）
            try:
                from bilibot.video_understanding import configure_global_semaphore
                max_g = int((va or {}).get("max_concurrent_global", 1) or 1)
                configure_global_semaphore(max(1, max_g))
            except Exception as e:
                logger.warning(f"刷新视频分析全局并发失败: {e}")

            # 账号运行时 VU 热重载
            if account_manager is not None and hasattr(account_manager, "reload_all"):
                try:
                    await account_manager.reload_all()
                except Exception as e:
                    logger.warning(f"视频理解保存后账号热重载失败: {e}")

            logger.info(
                f"视频理解配置已更新: enabled={va.get('enabled')}, "
                f"frame_extractor={va.get('frame_extractor')}, "
                f"max_keyframes={va.get('max_keyframes')}"
            )
            return ok(_mask_config(va if isinstance(va, dict) else {}))
        except Exception as e:
            logger.error(f"更新视频理解配置失败: {e}", exc_info=True)
            return fail_internal()

    async def test_video_analysis(request: Request) -> JSONResponse:
        """测试视频理解：下载视频并执行视听双轨分析

        Body: { "video_url": "BVxxxxxx 或完整 URL" }
        说明：不接受未保存的 config 覆盖；请先保存视频理解配置再测试，
        避免「表单临时值」与运行时快照不一致。
        """
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return fail_invalid_input("请求体必须是 JSON 对象")
            if isinstance(body, dict) and body.get("config"):
                logger.info("[test] 忽略未保存的 config 覆盖，请先保存视频理解配置")
            video_url = body.get("video_url") or body.get("url") or ""
            bvid = _extract_bvid(video_url)
            shortlink = _extract_shortlink_url(video_url)
            if not bvid and not shortlink:
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

            # b23.tv 短链接需要先解析出真实视频 URL 再提取 BV 号
            if not bvid and shortlink:
                bvid = await _resolve_video_bvid(video_url, bili)
            if not bvid:
                return fail_invalid_input("无法解析该 B站短链接，请直接粘贴 BV 开头的 12 位代码")

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

            # 下载前资源预检：超长视频直接返回元数据降级，不下载。
            try:
                max_duration = int(
                    getattr(getattr(vu, "cfg", None), "max_duration_seconds", 0) or 0
                )
            except (TypeError, ValueError):
                max_duration = 0
            if max_duration > 0:
                try:
                    duration_f = float(duration or 0)
                except (TypeError, ValueError):
                    duration_f = 0.0
                if duration_f > max_duration:
                    return ok({
                        "title": title,
                        "owner": owner,
                        "duration": duration,
                        "bvid": bvid,
                        "degradation_reason": "duration_exceeds_limit",
                        "description": "视频时长超过配置上限，已跳过下载，返回元数据分析",
                        "frames": [],
                    })

            # 2. 下载视频到临时目录（带字节上限与超时）
            data_dir = getattr(acc, "account_data_dir", "") or os.path.join(os.getcwd(), "data")
            video_temp_dir = os.path.join(data_dir, "video_temp")
            save_path = os.path.join(video_temp_dir, f"test_{bvid}")
            try:
                max_bytes = max(
                    0,
                    int(getattr(getattr(vu, "cfg", None), "max_download_bytes", 0) or 0),
                )
                timeout = max(
                    30,
                    int(getattr(getattr(vu, "cfg", None), "download_timeout_seconds", 0) or 90),
                )
            except (TypeError, ValueError):
                max_bytes, timeout = 0, 600
            video_file = await bili.download_video(
                bvid, cid, save_path, quality=32, max_bytes=max_bytes, timeout=timeout
            )

            if not video_file or not os.path.exists(video_file):
                return fail("DOWNLOAD_FAILED", f"视频下载失败: {bvid}")

            logger.info(f"[test] 视频已下载: {video_file}, 开始分析...")

            result = None
            cleanup_ready = False
            try:
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
                        "[test] 视频提取降级，返回元数据并清理临时文件: %s",
                        degradation,
                    )
                    cleanup_ready = True
                    return ok({
                        "title": title,
                        "owner": owner,
                        "duration": duration,
                        "bvid": bvid,
                        "degradation_reason": degradation,
                        "description": f"视频因资源限制降级为元数据分析: {degradation}",
                        "frames": [],
                    })

                # 无 behavior_log 且无有效视听观测 → 不写无意义评价/观测记忆
                visual_obs = result.get("visual_observations") or []
                audio_obs = result.get("audio_observations") or []
                has_extract = bool(behavior_log) or bool(visual_obs) or bool(audio_obs)
                if has_extract:
                    # 4. 完整提取内容提交到账号脑库后，再清理媒体与处理目录。
                    await _archive_test_analysis(
                        acc,
                        bvid=bvid,
                        vinfo=vinfo,
                        result=result,
                    )
                else:
                    logger.warning(
                        "[test] 视频理解无有效提取内容，跳过记忆归档 bvid=%s", bvid,
                    )
                cleanup_ready = True

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
            finally:
                if cleanup_ready:
                    # 只有无需归档或账号脑库已经确认提交，才可立即删除证据。
                    _cleanup_test_analysis_artifacts(video_file, result or {})
                else:
                    # 提取/归档失败时保留一小段诊断窗口，避免“记忆没写入，
                    # 原始证据也没了”。定时清理仍给磁盘占用设置上界。
                    from bilibot.video_understanding.cleanup import schedule_cleanup

                    paths = [video_file]
                    work_dir = (result or {}).get("work_dir")
                    if work_dir:
                        paths.append(work_dir)
                    schedule_cleanup(paths, delay_seconds=1800)
                    logger.warning(
                        "[test] 视频证据因提取/归档失败保留 1800 秒: bvid=%s",
                        bvid,
                    )

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
