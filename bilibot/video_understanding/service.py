"""
视频理解服务 — 整合视听双轨分析

将 demo/video_understanding 的能力接入 bilibot 主项目：
- 通过 ModelRouter（LLMManager）解析 vision/asr provider
- 从 config.yaml 的 video_analysis 段读取非 LLM 配置（资源边界、抽帧策略等）
- 端到端流程：预处理 → 视觉轨 + 音频轨并行 → 时序缝合 → 行为日志

PRD-V5 §8.2 VID-503：视频资源保护
- 下载前/分析前检查视频时长、文件大小、磁盘空间
- 同步工作（preprocess/ffprobe/ffmpeg/Katna/SceneDetect）进入受控线程执行器
- App 级全局 semaphore 限制所有账号总并发
- 本地 Whisper 默认关闭，开启时用独立工作池和超时
- 降级原因记录在返回结果中

使用方式：
    service = VideoUnderstandingService(llm_manager, config_loader)
    if service.is_available():
        result = await service.understand("/path/to/video.mp4")
        behavior_log = result["behavior_log"]  # Markdown 结构化日志
"""
import asyncio
import base64
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .alignment import align_events, build_behavior_log
from .audio_track import (
    ASRTranscriptionError,
    ASRTranscriptionResult,
    AudioEvent,
    shutdown_whisper_executor,
    transcribe_audio,
)
from .cleanup import schedule_cleanup
from .preprocess import (
    DEGRADATION_DOWNLOAD_SIZE_EXCEEDED,
    DEGRADATION_DURATION_EXCEEDS_LIMIT,
    DEGRADATION_INSUFFICIENT_DISK,
    DEGRADATION_SIZE_EXCEEDS_LIMIT,
    check_resource_limits,
    preprocess_video,
)
from .visual_track import (
    SUBTITLE_VISION_PROMPT,
    VisionTrackIncompleteError,
    describe_visual_track,
)

logger = logging.getLogger("bilibot.video_u.service")

VISION_SYSTEM_PROMPT = (
    "你是一个视频帧视觉描述专家。请用 1-2 句话客观、凝练地描述这张图片。"
    "重点关注：当前场景、核心主体的动作、画面中的显眼文字(OCR)或图表。"
    "不要添加任何主观推测或修辞手法。"
)

# 问答 Prompt（从 demo orchestrator.py 迁移）
_QA_SYSTEM_PROMPT = "你是一个高精度的视频内容深度分析专家。你需要结合视频的「视听对齐日志」来回答用户的问题。"

_QA_PROMPT_TEMPLATE = """# Role
你是一个高精度的视频内容深度分析专家。你需要结合视频的「视听对齐日志」来回答用户的问题。

# Video Content Log
以下是目标视频经过 AI 双轨技术提取出的行为日志（包含精准时间戳、听到的台词、看到的画面）：
--- LOG START ---
{log}
--- LOG END ---

# Constraints
1. 请严格、仅基于上方提供的 Video Content Log 进行回答。
2. 如果用户问及的时间段在日志中没有对应的视觉和音频记录，请直接回答：「根据视频提取日志，在该时间段内未捕获到相关核心信息。」
3. 回答时，如果引用了日志内容，必须在回答中指明对应的时间戳。
4. 绝不要凭空捏造日志中不存在的画面细节或台词。

# User Question
{question}

# Answer
"""


class VideoUnderstandingConfig:
    """从主项目 config.yaml 的 video_analysis 段读取配置"""

    def __init__(self, raw_config: dict, data_dir: str = "./data"):
        va = raw_config.get("video_analysis", {})

        self.enabled: bool = va.get("enabled", False)
        self.frame_extractor: str = va.get("frame_extractor", "katna")
        self.scenedetect_threshold: float = float(va.get("scenedetect_threshold", 27.0))
        self.image_max_size: int = int(va.get("image_max_size", 768))
        # 抽帧上限：镜头未超则全抽，超过则等距下采样（配置页可改，默认 150）
        try:
            max_kf = int(va.get("max_keyframes", 150))
        except (TypeError, ValueError):
            max_kf = 150
        self.max_keyframes: int = max(1, min(max_kf, 500))
        # Soft request from video_analysis; absolute clamp applied later via router hard cap.
        try:
            requested_vision_window = int(va.get("vision_window_size", 2))
        except (TypeError, ValueError):
            requested_vision_window = 2
        self.vision_window_size: int = max(1, min(requested_vision_window, 64))
        try:
            requested_vision_rate = int(va.get("vision_requests_per_minute", 10))
        except (TypeError, ValueError):
            requested_vision_rate = 10
        self.vision_requests_per_minute: int = max(
            1, min(requested_vision_rate, 600)
        )

        # Vision 单帧瞬时故障重试 + 可用成功率阈值（require_complete 时生效）
        try:
            frame_retries = int(va.get("vision_frame_max_retries", 2))
        except (TypeError, ValueError):
            frame_retries = 2
        self.vision_frame_max_retries: int = max(0, min(frame_retries, 5))
        try:
            frame_backoff = float(va.get("vision_frame_retry_backoff_seconds", 1.5))
        except (TypeError, ValueError):
            frame_backoff = 1.5
        self.vision_frame_retry_backoff_seconds: float = max(0.0, min(frame_backoff, 30.0))
        try:
            min_ratio = float(va.get("vision_min_success_ratio", 0.5))
        except (TypeError, ValueError):
            min_ratio = 0.5
        self.vision_min_success_ratio: float = min(1.0, max(0.0, min_ratio))

        # PRD-V5 §8.2 VID-503：资源边界配置
        self.max_duration_seconds: int = int(va.get("max_duration_seconds", 600))
        self.max_download_bytes: int = int(va.get("max_download_bytes", 209715200))
        self.max_concurrent_global: int = int(va.get("max_concurrent_global", 1))
        self.max_concurrent_per_account: int = int(va.get("max_concurrent_per_account", 1))
        self.download_timeout_seconds: int = int(va.get("download_timeout_seconds", 90))
        self.preprocess_timeout_seconds: int = int(va.get("preprocess_timeout_seconds", 180))
        self.analysis_timeout_seconds: int = int(va.get("analysis_timeout_seconds", 600))
        self.local_whisper_enabled: bool = bool(va.get("local_whisper_enabled", False))
        self.max_local_whisper_workers: int = int(va.get("max_local_whisper_workers", 1))
        self.temp_disk_quota_bytes: int = int(va.get("temp_disk_quota_bytes", 1073741824))

        # ASR 配置
        asr = va.get("asr", {})
        self.asr_model: str = asr.get("model", "")
        self.asr_api_key: str = asr.get("api_key", "")
        raw_asr_keys = asr.get("api_keys") or []
        if isinstance(raw_asr_keys, str):
            raw_asr_keys = [k.strip() for k in raw_asr_keys.replace(",", "\n").splitlines() if k.strip()]
        elif not isinstance(raw_asr_keys, list):
            raw_asr_keys = []
        self.asr_api_keys: list = [k for k in raw_asr_keys if isinstance(k, str) and k.strip()]
        self.asr_base_url: str = asr.get("base_url", "")
        try:
            self.asr_rate_limit_cooldown_seconds: float = float(
                asr.get("rate_limit_cooldown_seconds", 30)
            )
        except (TypeError, ValueError):
            self.asr_rate_limit_cooldown_seconds = 30.0
        self.whisper_model_size: str = asr.get("whisper_model_size", "base")
        self.whisper_device: str = asr.get("whisper_device", "cpu")
        self.whisper_compute_type: str = asr.get("whisper_compute_type", "int8")

        # 临时目录
        self.temp_dir: str = va.get("temp_dir", "") or os.path.join(data_dir, "video_temp")


# PRD-V5 §8.2 VID-503：App 级全局 semaphore（所有账号共享）
_global_semaphore: Optional[asyncio.Semaphore] = None
_global_semaphore_max: int = 0


def configure_global_semaphore(max_concurrent: int) -> None:
    """配置 App 级全局视频分析并发 semaphore

    在 app.py 初始化时调用，限制所有账号合计的视频分析并发数。
    """
    global _global_semaphore, _global_semaphore_max
    if max_concurrent < 1:
        max_concurrent = 1
    if _global_semaphore is None or _global_semaphore_max != max_concurrent:
        _global_semaphore = asyncio.Semaphore(max_concurrent)
        _global_semaphore_max = max_concurrent
        logger.info(f"视频分析全局并发 semaphore 已配置: max={max_concurrent}")


def get_global_semaphore() -> asyncio.Semaphore:
    """获取全局 semaphore（如未配置则默认 max=1）

    修复 Task 3：原实现通过 `getattr(_global_semaphore, '_loop', None) is not loop`
    检查并重建 semaphore，但 asyncio.Semaphore._loop 属性在 Python 3.10 已被移除，
    导致 3.10+ 下 `getattr(..., '_loop', None)` 恒为 None，`None is not loop` 恒为 True，
    每次调用 understand() 都会重建一个全新的 semaphore，全局并发限制完全失效。

    现改为仅返回缓存实例；semaphore 由 configure_global_semaphore 在 app 初始化时
    创建一次并缓存，后续不再因 loop 变化重建。
    """
    global _global_semaphore
    if _global_semaphore is None:
        configure_global_semaphore(1)
    return _global_semaphore


def reset_global_semaphore() -> None:
    """重置全局 semaphore（测试用）"""
    global _global_semaphore, _global_semaphore_max
    _global_semaphore = None
    _global_semaphore_max = 0


class LLMVisionAdapter:
    """
    把 LLMManager（ModelRouter）包装成 demo 的 describe_image / generate 接口。

    LLMManager.vision_analyze 通过 resolve_vision() 路由到独立的 vision provider，
    LLMManager.generate 通过 resolve_chat() 路由到对话 provider。
    这里读取本地图片文件转 base64 data URL 后委托给 vision_analyze。
    """

    def __init__(self, llm_manager):
        self.llm = llm_manager

    async def describe_image(
        self, image_path: str, prompt: str = VISION_SYSTEM_PROMPT, max_tokens: int = 250
    ) -> Optional[str]:
        try:
            # 同步读盘 + base64 绝不能堵事件循环（否则 Web 面板会卡死）。
            data_url = await asyncio.to_thread(self._load_image_data_url, image_path)
            if data_url is None:
                return None
            from bilibot.services.token_usage import usage_context
            with usage_context(scene="video_vision", account_id=getattr(self, "account_id", "") or ""):
                return await self.llm.vision_analyze(data_url, prompt, max_tokens)
        except Exception as e:
            # 上抛临时故障，避免静默丢帧；调用方（visual_track）按 require_complete 决定是否降级
            logger.error(f"Vision 描述失败 ({image_path}): {e}")
            raise

    @staticmethod
    def _load_image_data_url(image_path: str) -> Optional[str]:
        """Read + base64-encode a local image off the event loop."""
        max_size = 20 * 1024 * 1024  # 20MB
        file_size = os.path.getsize(image_path)
        if file_size > max_size:
            logger.warning(
                f"图片文件过大，跳过 vision 分析: {image_path} "
                f"({file_size / 1024 / 1024:.1f}MB > 20MB)"
            )
            return None
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"

    async def generate(
        self, prompt: str, system_prompt: str = "", max_tokens: int = 1024, temperature: float = 0.7
    ) -> Optional[str]:
        from bilibot.services.token_usage import usage_context
        with usage_context(scene="video_understanding", account_id=getattr(self, "account_id", "") or ""):
            return await self.llm.generate(
                prompt, system_prompt=system_prompt, max_tokens=max_tokens
            )


class VideoUnderstandingService:
    """视频理解服务（端到端）"""

    def __init__(self, llm_manager, config_loader):
        self.llm_manager = llm_manager
        self._config_loader = config_loader
        raw = config_loader.get_raw_config() if hasattr(config_loader, "get_raw_config") else {}
        data_dir = config_loader.get("data_dir", "./data") if hasattr(config_loader, "get") else "./data"
        self.cfg = VideoUnderstandingConfig(raw, data_dir)

        # 通过 ModelRouter 覆盖 ASR / local_whisper 配置（优先于 raw config）
        self._apply_router_config()

        self.adapter = LLMVisionAdapter(llm_manager) if llm_manager else None

        # PRD-V5 §8.2 VID-503：受控线程执行器（用于同步工作）
        self._executor: Optional[ThreadPoolExecutor] = None
        # PRD-V5 §8.2 VID-503：关闭标志
        self._shutdown: bool = False

    def _apply_router_config(self) -> None:
        """从 ModelRouter 覆盖 ASR / local_whisper 配置（如果 llm_manager 支持路由）

        VideoUnderstandingConfig 先从 raw config 读取（V1 兼容），
        此处用 router 解析的结果覆盖，确保 V3 路由配置优先。
        """
        if not self.llm_manager:
            return
        # ASR provider（resolve_asr 返回 LLMProvider 或 None）
        resolve_asr = getattr(self.llm_manager, "resolve_asr", None)
        if callable(resolve_asr):
            asr_p = resolve_asr()
            if asr_p:
                if asr_p.model:
                    self.cfg.asr_model = asr_p.model
                if asr_p.api_key:
                    self.cfg.asr_api_key = asr_p.api_key
                # Multi-key pool from LLMProvider when available
                provider_keys = getattr(asr_p, "api_keys", None) or []
                if provider_keys:
                    self.cfg.asr_api_keys = list(provider_keys)
                if asr_p.base_url:
                    self.cfg.asr_base_url = asr_p.base_url
                cooldown = getattr(asr_p, "rate_limit_cooldown_seconds", None)
                if cooldown is not None:
                    try:
                        self.cfg.asr_rate_limit_cooldown_seconds = float(cooldown)
                    except (TypeError, ValueError):
                        pass
        # local_whisper（property 返回 dict）
        # 必须双向同步 enabled：只写 True 会导致 Web 关闭后运行时仍启用
        lw = getattr(self.llm_manager, "local_whisper", None)
        if isinstance(lw, dict) and lw:
            if "enabled" in lw:
                self.cfg.local_whisper_enabled = bool(lw.get("enabled"))
            if lw.get("model_size"):
                self.cfg.whisper_model_size = lw["model_size"]
            if lw.get("device"):
                self.cfg.whisper_device = lw["device"]
            if lw.get("compute_type"):
                self.cfg.whisper_compute_type = lw["compute_type"]

    def reload_config(self, config_loader=None) -> None:
        """从最新 config 重建 VideoUnderstandingConfig（Web 保存后热生效）。

        保留已创建的线程池与 adapter；仅刷新资源边界 / 抽帧 / ASR 路由覆盖。
        """
        if config_loader is not None:
            self._config_loader = config_loader
        loader = getattr(self, "_config_loader", None)
        if loader is None:
            return
        raw = loader.get_raw_config() if hasattr(loader, "get_raw_config") else {}
        data_dir = loader.get("data_dir", "./data") if hasattr(loader, "get") else "./data"
        old_workers = int(getattr(self.cfg, "max_concurrent_per_account", 1) or 1)
        self.cfg = VideoUnderstandingConfig(raw, data_dir)
        self._apply_router_config()
        # 并发变大时重建线程池；变小则保留（避免中断进行中任务）
        new_workers = max(2, int(self.cfg.max_concurrent_per_account or 1))
        if self._executor is not None and new_workers > max(2, old_workers):
            try:
                self._executor.shutdown(wait=False, cancel_futures=False)
            except TypeError:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
            self._executor = None
        logger.info(
            "视频理解配置已热重载: enabled=%s max_keyframes=%s vision_window=%s "
            "local_whisper=%s rpm=%s",
            self.cfg.enabled,
            self.cfg.max_keyframes,
            self.cfg.vision_window_size,
            self.cfg.local_whisper_enabled,
            self.cfg.vision_requests_per_minute,
        )

    def _get_executor(self) -> ThreadPoolExecutor:
        """获取或创建受控线程执行器。

        至少 2 个 worker：视觉抽帧与 ASR 会并行，不能只留 1 个线程，
        否则会互相排队；也避免把重活挤回默认线程池拖死 Web。
        """
        if self._executor is None:
            workers = max(2, int(self.cfg.max_concurrent_per_account or 1))
            self._executor = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="video-prep"
            )
            logger.info(f"视频预处理线程池已创建: max_workers={workers}")
        return self._executor

    def is_available(self) -> bool:
        """是否可用（启用 + LLM 已配置 + 未关闭）"""
        if not self.cfg.enabled:
            return False
        if self.llm_manager is None:
            return False
        if self._shutdown:
            return False
        return True

    def has_vision(self) -> bool:
        """LLM 是否配置了视觉模型"""
        if not self.llm_manager:
            return False
        # 优先通过 ModelRouter 解析 vision provider
        resolve_vision = getattr(self.llm_manager, "resolve_vision", None)
        if callable(resolve_vision):
            vp = resolve_vision()
            return bool(vp and vp.client and vp.model)
        # 向后兼容：直接访问属性（旧 LLMProvider / Mock）
        return bool(
            getattr(self.llm_manager, "vision_client", None)
            and getattr(self.llm_manager, "vision_model", None)
        )

    async def understand(
        self,
        video_path: str,
        question: str = "",
        subtitle_segments: Optional[list] = None,
        read_subtitles: bool = False,
        defer_cleanup: bool = False,
        require_complete_audio: bool = False,
        require_complete_visual: bool = False,
    ) -> dict:
        """
        端到端视频理解

        Args:
            video_path: 本地视频文件路径
            question: 可选问题（为空则只生成行为日志不问答）
            subtitle_segments: 可选字幕段列表 [{"from": float, "to": float, "content": str}]。
                提供时跳过音频 ASR，直接用字幕轨替代声音轨（番剧场景：
                番剧一般都有字幕，且无需消耗音频下载/转写资源）。
            read_subtitles: 番剧字幕识别模式。True 时 Vision LLM 在描述每帧画面的
                同时转写画面里的硬字幕（B站正版番剧的中文字幕通常压在画面内，无独立
                字幕轨文件），并跳过音频 ASR（严格"不用声音"）。与 subtitle_segments
                互斥但可叠加：有字幕文件优先用文件，否则用帧内字幕识别。
            defer_cleanup: V6 归档调用方设为 True；完整提取内容提交后由调用方清理。
            require_complete_audio: 音轨 ASR 失败时抛出可重试错误，禁止将不完整提取归档。
            require_complete_visual: 任一视觉帧未完成描述时抛出可重试错误。

        Returns:
            {behavior_log, answer, work_dir, degradation_reason}  失败时 behavior_log 为空字符串
            degradation_reason 非空表示因资源限制降级为元数据分析
        """
        # PRD-V5 §8.2 VID-503：关闭后不再接受新任务（优先检查）
        if self._shutdown:
            logger.warning("视频理解服务已关闭，不再接受新任务")
            return {"behavior_log": "", "answer": None, "work_dir": "", "degradation_reason": "service_shutdown"}

        if not self.is_available():
            logger.warning("视频理解服务未启用或 LLM 未配置")
            return {"behavior_log": "", "answer": None, "work_dir": "", "degradation_reason": ""}

        task_id = uuid.uuid4().hex[:12]
        cfg = self.cfg

        # PRD-V5 §8.2 VID-503：获取全局 semaphore（跨账号总并发限制）
        global_sem = get_global_semaphore()

        async with global_sem:
            # PRD-V5 §8.2 VID-503：分析前资源边界检查
            degradation = check_resource_limits(
                video_path,
                cfg.temp_dir,
                cfg.max_duration_seconds,
                cfg.max_download_bytes,
                cfg.temp_disk_quota_bytes,
            )
            if degradation:
                logger.warning(f"视频资源检查未通过，降级为元数据分析: {degradation}")
                return {
                    "behavior_log": "",
                    "answer": None,
                    "work_dir": "",
                    "degradation_reason": degradation,
                }

            logger.info(f"开始视频理解: {video_path}")

            # PRD-V5 §8.2 VID-503：同步预处理工作放入受控线程执行器
            loop = asyncio.get_running_loop()
            executor = self._get_executor()

            try:
                prep = await asyncio.wait_for(
                    loop.run_in_executor(
                        executor,
                        preprocess_video,
                        video_path,
                        cfg.temp_dir,
                        task_id,
                        cfg.preprocess_timeout_seconds,
                    ),
                    timeout=cfg.preprocess_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(f"视频预处理超时（{cfg.preprocess_timeout_seconds}s）")
                return {
                    "behavior_log": "",
                    "answer": None,
                    "work_dir": "",
                    "degradation_reason": "preprocess_timeout",
                }

            try:
                # 2. 双轨并行
                frames_dir = os.path.join(prep.work_dir, "keyframes")
                use_vision = self.has_vision()

                async def _visual_task():
                    if not use_vision or not self.adapter:
                        if require_complete_visual:
                            raise VisionTrackIncompleteError(
                                "VISION_NOT_CONFIGURED",
                                "video extraction requires a configured vision provider",
                            )
                        logger.info("Vision 未配置，跳过视觉轨")
                        return [], False
                    # 番剧字幕识别模式：使用带字幕转写指令的视觉 prompt
                    vision_prompt = SUBTITLE_VISION_PROMPT if read_subtitles else VISION_SYSTEM_PROMPT
                    effective_window = cfg.vision_window_size
                    effective_rpm = cfg.vision_requests_per_minute
                    resolve_effective = getattr(
                        self.llm_manager, "vision_effective_concurrency", None
                    )
                    if callable(resolve_effective):
                        effective_window = resolve_effective(cfg.vision_window_size)
                    # Scale RPM with key count so multi-key pools are not starved by a single-key budget.
                    key_count = 1
                    resolve_vision = getattr(self.llm_manager, "resolve_vision", None)
                    if callable(resolve_vision):
                        vp = resolve_vision()
                        if vp is not None:
                            key_count = max(
                                1,
                                int(
                                    getattr(vp, "vision_api_key_count", 0)
                                    or getattr(vp, "api_key_count", 1)
                                    or 1
                                ),
                            )
                    effective_rpm = max(1, int(cfg.vision_requests_per_minute) * key_count)
                    return await describe_visual_track(
                        prep.video_path, prep.fps, prep.duration, frames_dir, self.adapter,
                        frame_extractor=cfg.frame_extractor,
                        scenedetect_threshold=cfg.scenedetect_threshold,
                        image_max_size=cfg.image_max_size,
                        vision_window_size=effective_window,
                        vision_requests_per_minute=effective_rpm,
                        vision_prompt=vision_prompt,
                        require_complete=require_complete_visual,
                        frame_max_retries=cfg.vision_frame_max_retries,
                        frame_retry_backoff_seconds=cfg.vision_frame_retry_backoff_seconds,
                        min_success_ratio=cfg.vision_min_success_ratio,
                        max_keyframes=cfg.max_keyframes,
                        executor=executor,
                    )

                def _audio_task():
                    if prep.audio_path:
                        result = transcribe_audio(
                            prep.audio_path,
                            asr_model=cfg.asr_model,
                            asr_api_key=cfg.asr_api_key,
                            asr_base_url=cfg.asr_base_url,
                            asr_api_keys=getattr(cfg, "asr_api_keys", None) or None,
                            rate_limit_cooldown_seconds=getattr(
                                cfg, "asr_rate_limit_cooldown_seconds", 30.0
                            ),
                            whisper_model_size=cfg.whisper_model_size,
                            whisper_device=cfg.whisper_device,
                            whisper_compute_type=cfg.whisper_compute_type,
                            local_whisper_enabled=cfg.local_whisper_enabled,
                            max_local_whisper_workers=cfg.max_local_whisper_workers,
                            whisper_timeout=cfg.analysis_timeout_seconds,
                            return_result=True,
                            raise_on_error=require_complete_audio,
                        )
                        if (
                            require_complete_audio
                            and prep.has_audio
                            and result.status == "skipped"
                        ):
                            raise ASRTranscriptionError(
                                "ASR_NOT_CONFIGURED",
                                "video has an audio track but no ASR provider is enabled",
                            )
                        return result
                    if require_complete_audio and prep.has_audio:
                        raise ASRTranscriptionError(
                            "ASR_AUDIO_EXTRACTION_MISSING",
                            "video reports an audio track but preprocessing produced no audio file",
                        )
                    return ASRTranscriptionResult([], "not_present")

                # 番剧字幕识别：用字幕轨替代声音轨，跳过音频 ASR（不消耗音频资源）
                subtitle_events = None
                if subtitle_segments:
                    subtitle_events = [
                        AudioEvent(
                            start=float(seg.get("from", 0) or 0),
                            end=float(seg.get("to", 0) or 0),
                            text=(seg.get("content") or "").strip(),
                            source="subtitle",
                        )
                        for seg in subtitle_segments
                        if (seg.get("content") or "").strip()
                    ]

                # VID-605：确保 audio_task 抛异常时 visual_future 被取消，避免 Vision LLM 调用泄漏
                visual_future = asyncio.create_task(_visual_task())
                try:
                    if subtitle_events or read_subtitles:
                        # 字幕轨：不调用 ASR（番剧字幕识别模式严格"不用声音"）
                        audio_events = subtitle_events or []
                        audio_result = ASRTranscriptionResult(
                            audio_events,
                            "subtitle" if subtitle_events else "visual_subtitle",
                        )
                        if read_subtitles and not subtitle_events:
                            logger.info(
                                "番剧字幕识别模式：跳过音频 ASR，由 Vision LLM 从画面帧转写硬字幕"
                            )
                        elif subtitle_events:
                            logger.info(
                                f"番剧字幕识别模式：跳过音频 ASR，使用 {len(audio_events)} 段字幕作为文本轨"
                            )
                        visual_events, is_static = await visual_future
                    else:
                        # 用专用线程池跑 ASR，避免占满默认 to_thread 池导致 Web 无响应。
                        audio_result = await loop.run_in_executor(executor, _audio_task)
                        audio_events = audio_result.events
                        visual_events, is_static = await visual_future
                except Exception:
                    visual_future.cancel()
                    try:
                        await visual_future
                    except asyncio.CancelledError:
                        pass
                    raise

                # 3. 时序缝合
                blocks = align_events(audio_events, visual_events, prep.duration, is_static=is_static)
                # no_audio：无声音轨且无字幕轨时，按视觉轨独立分析
                no_audio = (not prep.has_audio and not subtitle_events) or not audio_events
                if no_audio and visual_events:
                    logger.info("音频轨为空，使用视觉轨独立分析")

                behavior_log = build_behavior_log(blocks, is_static=is_static, no_audio=no_audio)

                # 保存日志
                nonempty_audio_texts = {
                    " ".join(event.text.split()).casefold()
                    for event in audio_events
                    if event.text and event.text.strip()
                }
                audio_segment_count = len(audio_events)
                duplicate_ratio = (
                    1.0 - (len(nonempty_audio_texts) / audio_segment_count)
                    if audio_segment_count
                    else 0.0
                )
                audio_quality_status = (
                    "repetitive"
                    if audio_segment_count >= 5 and duplicate_ratio >= 0.7
                    else "normal"
                )

                log_path = os.path.join(prep.work_dir, "behavior_log.md")
                try:
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write(behavior_log)
                except Exception:
                    pass

                # 4. 问答（可选）
                answer = None
                if question and self.adapter:
                    logger.info(f"提交问题: {question}")
                    answer = await self._answer_question(behavior_log, question)

                return {
                    "behavior_log": behavior_log,
                    "answer": answer,
                    "work_dir": prep.work_dir,
                    "degradation_reason": (
                        f"asr_failed:{audio_result.error_code}"
                        if audio_result.status == "failed"
                        else ""
                    ),
                    "audio_status": {
                        "status": audio_result.status,
                        "error_code": audio_result.error_code,
                        "error_reason": audio_result.error_reason,
                        "retryable": audio_result.retryable,
                        "duration_seconds": audio_result.duration_seconds or prep.duration,
                        "segment_count": audio_segment_count,
                        "unique_text_count": len(nonempty_audio_texts),
                        "duplicate_ratio": round(duplicate_ratio, 4),
                        "timestamp_clamped_count": audio_result.timestamp_clamped_count,
                        "quality_status": audio_quality_status,
                    },
                    # V6 memory brain: preserve every extracted textual observation.
                    # Frame paths are intentionally omitted because keyframes/media are
                    # temporary processing artifacts and must never enter long-term memory.
                    "audio_observations": [
                        {
                            "start": event.start,
                            "end": event.end,
                            "text": event.text,
                            "source": getattr(event, "source", "asr"),
                        }
                        for event in audio_events
                    ],
                    "visual_observations": [
                        {
                            "timestamp": event.timestamp,
                            "frame_number": event.frame_number,
                            "description": event.description,
                        }
                        for event in visual_events
                    ],
                    "timeline_observations": [
                        {
                            "start": block.start,
                            "end": block.end,
                            "is_gap_fill": block.is_gap_fill,
                            "audio": [
                                {
                                    "text": event.text,
                                    "source": getattr(event, "source", "asr"),
                                    "start": event.start,
                                    "end": event.end,
                                }
                                for event in block.audio
                            ],
                            "visual": [event.description for event in block.visuals],
                        }
                        for block in blocks
                    ],
                }
            except Exception as error:
                # 失败不再保留 work_dir：重试会重新下载/抽帧，残留只会占满磁盘。
                # 仍把 work_dir 挂到异常上，方便调用方 finally 做幂等清理。
                try:
                    error.work_dir = prep.work_dir
                except Exception:
                    pass
                try:
                    import shutil
                    shutil.rmtree(prep.work_dir, ignore_errors=True)
                except Exception:
                    pass
                logger.warning(
                    "视频提取未完成，已清理处理目录: work_dir=%s error=%s",
                    prep.work_dir,
                    type(error).__name__,
                )
                raise
            finally:
                # 成功且 defer_cleanup=True：由调用方在归档提交后再删（避免归档失败丢证据）。
                # 其它成功路径：延迟清理，便于短时调试。
                if not defer_cleanup:
                    schedule_cleanup([prep.work_dir], delay_seconds=1800)

    async def _answer_question(
        self, behavior_log: str, question: str, max_tokens: int = 1024
    ) -> Optional[str]:
        """基于行为日志回答问题"""
        if not self.adapter:
            return None
        prompt = _QA_PROMPT_TEMPLATE.format(log=behavior_log, question=question)
        from bilibot.services.token_usage import usage_context
        with usage_context(scene="video_qa", account_id=getattr(self, "account_id", "") or ""):
            return await self.adapter.generate(
                prompt, system_prompt=_QA_SYSTEM_PROMPT, max_tokens=max_tokens, temperature=0.5
            )

    def shutdown(self) -> None:
        """PRD-V5 §8.2 VID-503：优雅关闭

        - 停止接受新视频分析任务
        - 关闭受控线程执行器
        - 关闭本地 Whisper 工作池
        - 清理临时目录
        """
        self._shutdown = True
        logger.info("视频理解服务正在关闭...")

        # 关闭预处理线程池
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None
            logger.info("视频预处理线程池已关闭")

        # 关闭 Whisper 工作池
        shutdown_whisper_executor()

        # 清理临时目录
        try:
            if os.path.exists(self.cfg.temp_dir):
                import shutil
                shutil.rmtree(self.cfg.temp_dir, ignore_errors=True)
                logger.info(f"视频临时目录已清理: {self.cfg.temp_dir}")
        except Exception as e:
            logger.warning(f"清理视频临时目录失败: {e}")

        logger.info("视频理解服务已关闭")
