"""
视觉轨模块

职责：
- 根据视频时长动态决定抽帧数量
- 使用 Katna 智能抽帧（未安装则回退到 FFmpeg 等间隔抽帧）
- 缩放图片到最大边长 <= image_max_size
- 使用 Vision-LLM 并行描述关键帧
- 返回带时间戳的画面描述列表

注：llm 参数只需提供 `describe_image(image_path, prompt, max_tokens)` 异步方法即可，
由 service.py 中的 LLMVisionAdapter 适配主项目 LLMProvider。
"""
import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Tuple

from PIL import Image

logger = logging.getLogger("bilibot.video_u.visual")

# 默认视觉描述 prompt（与 service.VISION_SYSTEM_PROMPT 保持一致，避免循环 import）
DEFAULT_VISION_PROMPT = (
    "你是一个视频帧视觉描述专家。请用 1-2 句话客观、凝练地描述这张图片。"
    "重点关注：当前场景、核心主体的动作、画面中的显眼文字(OCR)或图表。"
    "不要添加任何主观推测或修辞手法。"
)

# 番剧字幕识别模式：要求 Vision LLM 在描述画面的同时转写画面里的硬字幕
SUBTITLE_VISION_PROMPT = (
    "你是一个视频帧视觉描述专家。请用 1-2 句话客观、凝练地描述这张图片。"
    "重点关注：当前场景、核心主体的动作、画面中的显眼文字(OCR)或图表。"
    "番剧的中文字幕通常压在画面底部（硬字幕）。如果画面中有字幕/台词文字，"
    "请在描述末尾另起一行，以「【字幕】」开头原样转写字幕文本；没有字幕则不写该行。"
    "不要添加任何主观推测或修辞手法。"
)


@dataclass
class VisualEvent:
    """视觉事件"""

    timestamp: float
    frame_number: int
    image_path: str
    description: str


class VisionTrackIncompleteError(RuntimeError):
    """A retryable visual extraction failure in completeness-sensitive flows."""

    def __init__(self, code: str, reason: str, *, expected: int = 0, completed: int = 0):
        self.code = code
        self.reason = reason
        self.expected = max(0, int(expected))
        self.completed = max(0, int(completed))
        self.retryable = True
        self.work_dir = ""
        super().__init__(f"{code}: {reason}")


class VisionRequestPacer:
    """Serialize request starts to a stable per-video rate budget."""

    def __init__(
        self,
        requests_per_minute: float,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        rate = max(0.0, float(requests_per_minute))
        self.min_interval = 60.0 / rate if rate else 0.0
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    async def wait_turn(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = self._clock()
            delay = self._next_start - now
            if delay > 0:
                await self._sleep(delay)
                now = self._clock()
            self._next_start = max(self._next_start, now) + self.min_interval


def _select_timeline_frames(frame_numbers: List[int], limit: int) -> List[int]:
    """Downsample scene midpoints across the full timeline deterministically."""

    values = sorted(set(int(value) for value in frame_numbers))
    if limit <= 0:
        return []
    if len(values) <= limit:
        return values
    if limit == 1:
        return [values[len(values) // 2]]

    remaining = list(values)
    first, last = values[0], values[-1]
    selected: List[int] = []
    for index in range(limit):
        target = first + (last - first) * index / (limit - 1)
        choice = min(remaining, key=lambda value: (abs(value - target), value))
        selected.append(choice)
        remaining.remove(choice)
    return sorted(selected)


def _calculate_frame_count(duration: float) -> int:
    """根据视频时长动态决定抽帧数量"""
    if duration <= 60:
        return 5
    if duration <= 300:
        return 10
    if duration <= 1200:
        return 20
    # PRD 5.2：长视频帧数上限 30，避免帧数爆炸导致 Vision-LLM 调用过多
    return min(int(duration / 60) * 2, 30)


def _resize_image(image_path: str, max_size: int) -> str:
    """等比例缩放图片，最大边长不超过 max_size"""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            if max(width, height) <= max_size:
                return image_path
            ratio = max_size / max(width, height)
            new_size = (int(width * ratio), int(height * ratio))
            resized = img.resize(new_size, Image.Resampling.LANCZOS)
            base, ext = os.path.splitext(image_path)
            resized_path = f"{base}_resized{ext}"
            resized.save(resized_path, quality=90)
            # PRD 4.1：原地替换原图，避免残留文件导致磁盘占用翻倍
            os.replace(resized_path, image_path)
            return image_path
    except Exception as e:
        logger.warning(f"缩放图片失败 {image_path}: {e}")
        return image_path


def _extract_frames_fallback(
    video_path: str,
    output_dir: str,
    fps: float,
    duration: float,
    no_of_frames: int,
) -> List[Tuple[int, str]]:
    """FFmpeg 等间隔抽帧"""
    os.makedirs(output_dir, exist_ok=True)
    frames: List[Tuple[int, str]] = []
    if duration <= 0 or no_of_frames <= 0:
        return frames

    for i in range(1, no_of_frames + 1):
        timestamp = duration * i / (no_of_frames + 1)
        frame_number = int(timestamp * fps)
        frame_path = os.path.join(output_dir, f"frame_{frame_number:08d}.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", video_path,
            "-vframes", "1", "-q:v", "2", frame_path,
        ]
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=60, encoding="utf-8", errors="ignore",
            )
            if proc.returncode == 0 and os.path.exists(frame_path):
                frames.append((frame_number, frame_path))
        except Exception as e:
            logger.warning(f"FFmpeg 抽帧失败 (t={timestamp:.2f}s): {e}")

    logger.info(f"FFmpeg fallback 抽帧完成: {len(frames)} 张")
    return frames


def _extract_frames_katna(
    video_path: str,
    output_dir: str,
    fps: float,
    duration: float,
    no_of_frames: int,
) -> List[Tuple[int, str]]:
    """使用 Katna 智能抽帧（通过 subprocess 调用独立工作进程）"""
    os.makedirs(output_dir, exist_ok=True)

    python_exe = sys.executable
    worker_module = "bilibot.video_understanding.katna_worker"

    cmd = [python_exe, "-m", worker_module, video_path, output_dir, str(no_of_frames)]

    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=600, encoding="utf-8", errors="ignore",
        )
    except Exception as e:
        logger.warning(f"Katna 工作进程启动失败，回退 FFmpeg: {e}")
        raise

    if proc.returncode != 0:
        logger.warning(f"Katna 抽帧失败，回退 FFmpeg: {proc.stderr[:300]}")
        raise RuntimeError("Katna worker failed")

    frames: List[Tuple[int, str]] = []
    pattern = re.compile(r"_\d+\.jpe?g$", re.IGNORECASE)
    all_files = [f for f in os.listdir(output_dir) if pattern.search(f)]
    total_count = len(all_files)
    for name in sorted(all_files):
        idx_match = re.search(r"_(\d+)\.jpe?g$", name, re.IGNORECASE)
        if idx_match:
            index = int(idx_match.group(1))
            if total_count:
                estimated_frame = int(index / max(total_count - 1, 1) * duration * fps)
            else:
                estimated_frame = index
            frames.append((estimated_frame, os.path.join(output_dir, name)))

    logger.info(f"Katna 抽帧完成: {len(frames)} 张")
    return frames


def _extract_frames_scenedetect(
    video_path: str,
    output_dir: str,
    fps: float,
    duration: float,
    no_of_frames: int,
    threshold: float = 27.0,
) -> List[Tuple[int, str]]:
    """使用 PySceneDetect 进行镜头边界检测"""
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector
    except ImportError as e:
        logger.warning(f"PySceneDetect 未安装，回退 FFmpeg: {e}")
        raise RuntimeError("scenedetect not available")

    os.makedirs(output_dir, exist_ok=True)
    frames: List[Tuple[int, str]] = []

    proxy_path = os.path.join(output_dir, "proxy_480p.mp4")
    _run_ffmpeg(
        ["ffmpeg", "-y", "-i", video_path, "-vf", "scale=-2:480",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-an", proxy_path],
        timeout=300,
    )
    if not os.path.exists(proxy_path):
        raise RuntimeError("代理视频生成失败")

    try:
        video = open_video(proxy_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=threshold))
        scene_manager.detect_scenes(video)
        scenes = scene_manager.get_scene_list()
        logger.info(f"PySceneDetect 检测到 {len(scenes)} 个镜头")

        scene_midpoints = [
            (start_frame.frame_num + end_frame.frame_num) // 2
            for start_frame, end_frame in scenes
        ]
        # 镜头全送，不下采样；no_of_frames 仅用于下方"镜头过少时补充等间隔帧"的判断
        selected_midpoints = scene_midpoints

        for mid_frame in selected_midpoints:
            timestamp = mid_frame / fps if fps else 0.0
            frame_path = os.path.join(output_dir, f"frame_{mid_frame:08d}.jpg")
            _extract_frame_at(video_path, timestamp, frame_path)
            if os.path.exists(frame_path):
                frames.append((mid_frame, frame_path))

        if len(frames) < no_of_frames // 2:
            logger.info("镜头数较少，补充等间隔帧")
            existing = {frame_number for frame_number, _ in frames}
            candidate_count = no_of_frames * 2
            for i in range(1, candidate_count + 1):
                if len(frames) >= no_of_frames:
                    break
                timestamp = duration * i / (candidate_count + 1)
                frame_number = int(timestamp * fps)
                frame_path = os.path.join(output_dir, f"frame_{frame_number:08d}.jpg")
                if frame_number in existing:
                    continue
                _extract_frame_at(video_path, timestamp, frame_path)
                if os.path.exists(frame_path):
                    frames.append((frame_number, frame_path))
                    existing.add(frame_number)

        frames.sort(key=lambda x: x[0])
        logger.info(f"PySceneDetect 抽帧完成: {len(frames)} 张")
        return frames
    except Exception as e:
        logger.warning(f"PySceneDetect 抽帧失败，回退 FFmpeg: {e}")
        raise


def _extract_frame_at(video_path: str, timestamp: float, output_path: str) -> bool:
    cmd = [
        "ffmpeg", "-y", "-ss", f"{timestamp:.3f}", "-i", video_path,
        "-vframes", "1", "-q:v", "2", output_path,
    ]
    return _run_ffmpeg(cmd, timeout=60) and os.path.exists(output_path)


def _run_ffmpeg(cmd: List[str], timeout: int = 60) -> bool:
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, encoding="utf-8", errors="ignore",
        )
        if proc.returncode != 0:
            logger.warning(f"FFmpeg 失败: {proc.stderr[:300]}")
            return False
        return True
    except Exception as e:
        logger.warning(f"FFmpeg 运行异常: {e}")
        return False


def extract_keyframes(
    video_path: str,
    output_dir: str,
    fps: float,
    duration: float,
    no_of_frames: int,
    frame_extractor: str = "katna",
    scenedetect_threshold: float = 27.0,
) -> List[Tuple[int, str]]:
    """抽帧入口，根据配置选择策略，失败自动回退 FFmpeg"""
    strategy = frame_extractor.lower()

    if strategy == "scenedetect":
        try:
            return _extract_frames_scenedetect(
                video_path, output_dir, fps, duration, no_of_frames, scenedetect_threshold
            )
        except Exception:
            return _extract_frames_fallback(video_path, output_dir, fps, duration, no_of_frames)

    if strategy == "katna":
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        if duration > 120 or file_size_mb > 50:
            logger.info(f"视频较大（{file_size_mb:.1f}MB / {duration:.0f}s），直接使用 FFmpeg 抽帧")
            return _extract_frames_fallback(video_path, output_dir, fps, duration, no_of_frames)
        try:
            return _extract_frames_katna(video_path, output_dir, fps, duration, no_of_frames)
        except Exception:
            return _extract_frames_fallback(video_path, output_dir, fps, duration, no_of_frames)

    return _extract_frames_fallback(video_path, output_dir, fps, duration, no_of_frames)


def _prepare_visual_frames(
    video_path: str,
    fps: float,
    duration: float,
    output_dir: str,
    frame_extractor: str = "katna",
    scenedetect_threshold: float = 27.0,
    image_max_size: int = 768,
) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]], int]:
    """CPU/ffmpeg-heavy keyframe extraction + resize (must not run on event loop)."""
    no_of_frames = _calculate_frame_count(duration)
    logger.info(f"目标抽帧数: {no_of_frames}")

    frames = extract_keyframes(
        video_path, output_dir, fps, duration, no_of_frames,
        frame_extractor=frame_extractor, scenedetect_threshold=scenedetect_threshold,
    )
    if not frames:
        return [], [], no_of_frames

    resized_frames: List[Tuple[int, str]] = []
    for frame_number, path in frames:
        resized = _resize_image(path, image_max_size)
        resized_frames.append((frame_number, resized))
    return frames, resized_frames, no_of_frames


async def describe_visual_track(
    video_path: str,
    fps: float,
    duration: float,
    output_dir: str,
    llm,
    frame_extractor: str = "katna",
    scenedetect_threshold: float = 27.0,
    image_max_size: int = 768,
    vision_window_size: int = 5,
    vision_requests_per_minute: float = 10.0,
    vision_prompt: str = None,
    request_pacer: Optional[VisionRequestPacer] = None,
    require_complete: bool = False,
) -> Tuple[List[VisualEvent], bool]:
    """
    视觉轨处理入口

    Args:
        llm: 提供 `describe_image(image_path, prompt, max_tokens)` 方法的对象

    Returns:
        (视觉事件列表, 是否画面基本静止)
    """
    # Offload scenedetect/katna/ffmpeg/resize so the asyncio event loop (Web) stays responsive.
    frames, resized_frames, no_of_frames = await asyncio.to_thread(
        _prepare_visual_frames,
        video_path,
        fps,
        duration,
        output_dir,
        frame_extractor,
        scenedetect_threshold,
        image_max_size,
    )
    if not frames:
        logger.warning("未抽到任何关键帧")
        if require_complete:
            raise VisionTrackIncompleteError(
                "VISION_NO_FRAMES",
                "keyframe extraction produced no frames",
                expected=no_of_frames,
                completed=0,
            )
        return [], False

    visual_events: List[VisualEvent] = []
    # Hard cap is applied by caller (VideoUnderstandingConfig / ModelRouter).
    # Keep a safety floor only; do not force the old hardcoded "2".
    effective_window = max(1, int(vision_window_size))
    if effective_window != vision_window_size:
        logger.warning(
            f"Vision concurrency capped: {vision_window_size} -> {effective_window}"
        )
    semaphore = asyncio.Semaphore(effective_window)
    pacer = request_pacer or VisionRequestPacer(vision_requests_per_minute)
    logger.info(
        f"Vision request budget: {vision_requests_per_minute:g}/min, "
        f"concurrency={effective_window}, frames={len(resized_frames)}"
    )

    async def _describe_one(index: int, frame_number: int, image_path: str) -> Optional[VisualEvent]:
        async with semaphore:
            await pacer.wait_turn()
            description = await llm.describe_image(image_path, prompt=vision_prompt or DEFAULT_VISION_PROMPT)
        if not description:
            return None
        timestamp = round(frame_number / fps, 2) if fps else 0.0
        return VisualEvent(
            timestamp=timestamp, frame_number=frame_number,
            image_path=image_path, description=description,
        )

    tasks = [
        _describe_one(index, frame_number, path)
        for index, (frame_number, path) in enumerate(resized_frames)
    ]
    results = await asyncio.gather(*tasks)
    visual_events = [r for r in results if r is not None]
    visual_events.sort(key=lambda x: x.timestamp)
    if require_complete and len(visual_events) != len(resized_frames):
        raise VisionTrackIncompleteError(
            "VISION_INCOMPLETE_DESCRIPTIONS",
            (
                f"described {len(visual_events)} of {len(resized_frames)} "
                "extracted frames"
            ),
            expected=len(resized_frames),
            completed=len(visual_events),
        )

    is_static = len(frames) == 1
    if is_static:
        logger.info("画面基本静止：全局复用第一帧描述")

    return visual_events, is_static
