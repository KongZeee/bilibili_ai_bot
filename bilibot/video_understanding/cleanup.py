"""
临时文件清理模块

职责：
- 任务结束后立即或延迟清理临时目录
- 默认延迟 30 分钟（仅非 defer_cleanup 成功路径）
- 成功归档后立即清理；失败证据短暂保留并延迟清理
- 启动时清理超过保留窗口的孤儿临时文件（进程被杀后未清理的）
"""
import logging
import os
import shutil
import threading
import time
from typing import Iterable, List, Optional

logger = logging.getLogger("bilibot.video_u.cleanup")

_SCHEDULED: List[threading.Timer] = []
_SCHEDULED_LOCK = threading.Lock()


def cleanup_now(paths: List[str]) -> None:
    """立即删除指定路径"""
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info(f"已删除临时文件: {path}")
            elif os.path.isdir(path):
                shutil.rmtree(path)
                logger.info(f"已删除临时目录: {path}")
        except Exception as e:
            logger.warning(f"清理失败 {path}: {e}")


def cleanup_media_artifacts(
    *paths: Optional[str],
    extra: Optional[Iterable[Optional[str]]] = None,
) -> None:
    """清理视频文件与处理目录（去重、忽略空路径）。

    用于 scheduler / test API：成功归档后或失败退出时都应调用。
    """
    ordered: List[str] = []
    seen = set()
    for path in list(paths) + list(extra or ()):
        if not path:
            continue
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(str(path))
    if ordered:
        cleanup_now(ordered)


def cleanup_orphaned_video_temp(
    video_temp_dir: str, min_age_seconds: int = 1800
) -> int:
    """启动时清理 video_temp 目录中的孤儿临时文件。

    进程被杀后，已下载的 .mp4、关键帧目录、.m4s 中间文件等不会被清理。
    本函数在 scheduler 启动时调用，只删除超过保留窗口的残留文件。
    新鲜失败证据可能尚未完成人工补归档，不能在快速重启时立即抹掉。

    Args:
        video_temp_dir: video_temp 目录路径

    Returns:
        清理的文件/目录数量
    """
    if not video_temp_dir or not os.path.isdir(video_temp_dir):
        return 0

    cleaned = 0
    try:
        entries = os.listdir(video_temp_dir)
    except Exception as e:
        logger.warning(f"无法读取 video_temp 目录: {e}")
        return 0

    for name in entries:
        path = os.path.join(video_temp_dir, name)
        try:
            try:
                age = max(0.0, time.time() - os.path.getmtime(path))
            except OSError:
                age = float("inf")
            if age < max(0, int(min_age_seconds)):
                logger.info(
                    "保留新鲜 video_temp 证据: %s (age=%.0fs)", name, age
                )
                continue
            if os.path.isfile(path):
                # 删除 .mp4、.m4s、.mp3、.wav 等临时媒体文件
                ext = os.path.splitext(name)[1].lower()
                if ext in (".mp4", ".m4s", ".mp3", ".wav", ".flv", ".mkv"):
                    os.remove(path)
                    cleaned += 1
                    logger.info(f"启动清理孤儿文件: {name}")
            elif os.path.isdir(path):
                # 删除工作目录（关键帧、音频分析等）
                shutil.rmtree(path)
                cleaned += 1
                logger.info(f"启动清理孤儿目录: {name}")
        except Exception as e:
            logger.warning(f"启动清理失败 {name}: {e}")

    if cleaned:
        logger.info(f"启动清理 video_temp 完成：共清理 {cleaned} 个孤儿文件/目录")
    return cleaned


def schedule_cleanup(paths: List[str], delay_seconds: int = 1800) -> threading.Timer:
    """延迟清理临时文件"""

    holder: dict[str, threading.Timer] = {}

    def _run():
        try:
            cleanup_now(paths)
        finally:
            timer_ref = holder.get("timer")
            if timer_ref is not None:
                with _SCHEDULED_LOCK:
                    try:
                        _SCHEDULED.remove(timer_ref)
                    except ValueError:
                        pass

    timer = threading.Timer(delay_seconds, _run)
    holder["timer"] = timer
    timer.daemon = True
    with _SCHEDULED_LOCK:
        _SCHEDULED.append(timer)
    timer.start()
    logger.info(f"已调度 {len(paths)} 个临时路径，{delay_seconds} 秒后清理")
    return timer


def cancel_all_scheduled() -> None:
    """取消所有已调度的清理任务（测试用）"""
    with _SCHEDULED_LOCK:
        timers = list(_SCHEDULED)
        _SCHEDULED.clear()
    for timer in timers:
        timer.cancel()
