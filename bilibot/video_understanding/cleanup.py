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
_MISSING = object()


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


def _snapshot_paths(paths: Iterable[Optional[str]]) -> dict[str, Optional[tuple]]:
    """Capture (mtime_ns, size) for each existing path; missing paths map to None."""
    snapshots: dict[str, Optional[tuple]] = {}
    for path in paths:
        if not path:
            continue
        try:
            st = os.stat(str(path))
            snapshots[str(path)] = (st.st_mtime_ns, st.st_size)
        except OSError:
            snapshots[str(path)] = None
    return snapshots


def cleanup_paths_if_unchanged(
    paths: Iterable[Optional[str]],
    snapshots: Optional[dict] = None,
) -> None:
    """Delete paths only when they still match the scheduling-time snapshot.

    Delayed cleanup timers are dangerous when paths are reused (e.g. the same
    ``video_temp/{bvid}.mp4`` re-downloaded for a new task): an old timer must
    never delete a fresh file that happens to live at the same path. Missing
    entries in ``snapshots`` are skipped — a file that appears after scheduling
    is not ours to delete.
    """
    snaps = snapshots or _snapshot_paths(paths)
    for path in paths:
        if not path:
            continue
        expected = snaps.get(str(path), _MISSING)
        if expected is _MISSING or expected is None:
            continue
        try:
            st = os.stat(str(path))
            if (st.st_mtime_ns, st.st_size) != expected:
                logger.info(
                    "跳过延迟清理，路径已被复用或修改: %s", path
                )
                continue
        except OSError:
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
                # video_temp is a bot-owned runtime directory. Any file older
                # than the retention window is an orphan (known media extensions
                # or not) and can be removed.
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


def schedule_cleanup(
    paths: List[str],
    delay_seconds: int = 1800,
    *,
    delete_if_appears: bool = False,
) -> threading.Timer:
    """延迟清理临时文件。

    默认只删除与调度时快照一致的文件（路径复用保护）。
    ``delete_if_appears=True`` 用于调用方持有唯一随机路径的场景
    （如确定性 task_id 的预处理超时目录）：即使调度时目录尚未被
    后台线程创建，超时后也直接删除，不会泄漏到下次启动。
    """

    snapshots = _snapshot_paths(paths)
    holder: dict[str, threading.Timer] = {}

    def _run():
        try:
            if delete_if_appears:
                cleanup_now(paths)
            else:
                cleanup_paths_if_unchanged(paths, snapshots=snapshots)
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
