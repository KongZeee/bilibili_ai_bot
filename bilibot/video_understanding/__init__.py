"""
视频理解模块 — 视听双轨多模态分析

整合自 demo/video_understanding，接入 bilibot 主项目的 LLMProvider。

核心入口：
    VideoUnderstandingService.understand(video_path) -> {behavior_log, answer, work_dir}

PRD-V5 §8.2 VID-503：
    configure_global_semaphore(max_concurrent) — App 级全局并发限制
"""
from .service import (
    VideoUnderstandingConfig,
    VideoUnderstandingService,
    LLMVisionAdapter,
    configure_global_semaphore,
    get_global_semaphore,
    reset_global_semaphore,
)

__all__ = [
    "VideoUnderstandingService",
    "VideoUnderstandingConfig",
    "LLMVisionAdapter",
    "configure_global_semaphore",
    "get_global_semaphore",
    "reset_global_semaphore",
]
