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
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image

logger = logging.getLogger("bilibot.video_u.visual")


@dataclass
class VisualEvent:
    """视觉事件"""

    timestamp: float
    frame_number: int
    image_path: str
    description: str


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

        for start_frame, end_frame in scenes:
            mid_frame = (start_frame.frame_num + end_frame.frame_num) // 2
            timestamp = mid_frame / fps if fps else 0.0
            frame_path = os.path.join(output_dir, f"frame_{mid_frame:08d}.jpg")
            _extract_frame_at(video_path, timestamp, frame_path)
            if os.path.exists(frame_path):
                frames.append((mid_frame, frame_path))

        if len(frames) < no_of_frames // 2:
            logger.info("镜头数较少，补充等间隔帧")
            existing = {p for _, p in frames}
            for i in range(1, no_of_frames + 1):
                timestamp = duration * i / (no_of_frames + 1)
                frame_number = int(timestamp * fps)
                frame_path = os.path.join(output_dir, f"frame_{frame_number:08d}.jpg")
                if frame_path in existing:
                    continue
                _extract_frame_at(video_path, timestamp, frame_path)
                if os.path.exists(frame_path):
                    frames.append((frame_number, frame_path))

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
) -> Tuple[List[VisualEvent], bool]:
    """
    视觉轨处理入口

    Args:
        llm: 提供 `describe_image(image_path, prompt, max_tokens)` 方法的对象

    Returns:
        (视觉事件列表, 是否画面基本静止)
    """
    no_of_frames = _calculate_frame_count(duration)
    logger.info(f"目标抽帧数: {no_of_frames}")

    frames = extract_keyframes(
        video_path, output_dir, fps, duration, no_of_frames,
        frame_extractor=frame_extractor, scenedetect_threshold=scenedetect_threshold,
    )
    if not frames:
        logger.warning("未抽到任何关键帧")
        return [], False

    resized_frames = []
    for frame_number, path in frames:
        resized = _resize_image(path, image_max_size)
        resized_frames.append((frame_number, resized))

    visual_events: List[VisualEvent] = []
    semaphore = asyncio.Semaphore(vision_window_size)

    async def _describe_one(index: int, frame_number: int, image_path: str) -> Optional[VisualEvent]:
        async with semaphore:
            description = await llm.describe_image(image_path)
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

    is_static = len(frames) == 1
    if is_static:
        logger.info("画面基本静止：全局复用第一帧描述")

    return visual_events, is_static
