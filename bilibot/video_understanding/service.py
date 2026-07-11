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
from .audio_track import shutdown_whisper_executor, transcribe_audio
from .cleanup import schedule_cleanup
from .preprocess import (
    DEGRADATION_DOWNLOAD_SIZE_EXCEEDED,
    DEGRADATION_DURATION_EXCEEDS_LIMIT,
    DEGRADATION_INSUFFICIENT_DISK,
    DEGRADATION_SIZE_EXCEEDS_LIMIT,
    check_resource_limits,
    preprocess_video,
)
from .visual_track import describe_visual_track

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
        self.vision_window_size: int = int(va.get("vision_window_size", 5))

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
        self.asr_base_url: str = asr.get("base_url", "")
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
    """获取全局 semaphore（如未配置则默认 max=1）"""
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
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{b64}"
            return await self.llm.vision_analyze(data_url, prompt, max_tokens)
        except Exception as e:
            logger.error(f"Vision 描述失败 ({image_path}): {e}")
            return None

    async def generate(
        self, prompt: str, system_prompt: str = "", max_tokens: int = 1024, temperature: float = 0.7
    ) -> Optional[str]:
        return await self.llm.generate(
            prompt, system_prompt=system_prompt, max_tokens=max_tokens
        )


class VideoUnderstandingService:
    """视频理解服务（端到端）"""

    def __init__(self, llm_manager, config_loader):
        self.llm_manager = llm_manager
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
                if asr_p.base_url:
                    self.cfg.asr_base_url = asr_p.base_url
        # local_whisper（property 返回 dict）
        lw = getattr(self.llm_manager, "local_whisper", None)
        if lw:
            if lw.get("enabled"):
                self.cfg.local_whisper_enabled = True
            if lw.get("model_size"):
                self.cfg.whisper_model_size = lw["model_size"]
            if lw.get("device"):
                self.cfg.whisper_device = lw["device"]
            if lw.get("compute_type"):
                self.cfg.whisper_compute_type = lw["compute_type"]

    def _get_executor(self) -> ThreadPoolExecutor:
        """获取或创建受控线程执行器（max_workers = max_concurrent_per_account）"""
        if self._executor is None:
            workers = max(1, self.cfg.max_concurrent_per_account)
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

    async def understand(self, video_path: str, question: str = "") -> dict:
        """
        端到端视频理解

        Args:
            video_path: 本地视频文件路径
            question: 可选问题（为空则只生成行为日志不问答）

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
                        logger.info("Vision 未配置，跳过视觉轨")
                        return [], False
                    return await describe_visual_track(
                        prep.video_path, prep.fps, prep.duration, frames_dir, self.adapter,
                        frame_extractor=cfg.frame_extractor,
                        scenedetect_threshold=cfg.scenedetect_threshold,
                        image_max_size=cfg.image_max_size,
                        vision_window_size=cfg.vision_window_size,
                    )

                def _audio_task():
                    if prep.audio_path:
                        return transcribe_audio(
                            prep.audio_path,
                            asr_model=cfg.asr_model,
                            asr_api_key=cfg.asr_api_key,
                            asr_base_url=cfg.asr_base_url,
                            whisper_model_size=cfg.whisper_model_size,
                            whisper_device=cfg.whisper_device,
                            whisper_compute_type=cfg.whisper_compute_type,
                            local_whisper_enabled=cfg.local_whisper_enabled,
                            max_local_whisper_workers=cfg.max_local_whisper_workers,
                            whisper_timeout=cfg.analysis_timeout_seconds,
                        )
                    return []

                # VID-605：确保 audio_task 抛异常时 visual_future 被取消，避免 Vision LLM 调用泄漏
                visual_future = asyncio.create_task(_visual_task())
                try:
                    audio_events = await asyncio.to_thread(_audio_task)
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
                no_audio = not prep.has_audio or not audio_events
                if no_audio and visual_events:
                    logger.info("音频轨为空，使用视觉轨独立分析")

                behavior_log = build_behavior_log(blocks, is_static=is_static, no_audio=no_audio)

                # 保存日志
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
                    "degradation_reason": "",
                }
            except Exception:
                # PRD 3.9：异常时立即清理 work_dir，避免临时文件残留
                try:
                    import shutil
                    shutil.rmtree(prep.work_dir, ignore_errors=True)
                except Exception:
                    pass
                raise
            finally:
                # 正常完成时延迟清理（30 分钟）
                schedule_cleanup([prep.work_dir], delay_seconds=1800)

    async def _answer_question(
        self, behavior_log: str, question: str, max_tokens: int = 1024
    ) -> Optional[str]:
        """基于行为日志回答问题"""
        if not self.adapter:
            return None
        prompt = _QA_PROMPT_TEMPLATE.format(log=behavior_log, question=question)
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
