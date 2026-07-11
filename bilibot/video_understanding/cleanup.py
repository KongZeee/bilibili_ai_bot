"""
临时文件清理模块

职责：
- 任务结束后延迟清理临时目录（支持立即清理和定时清理）
- 默认保留 30 分钟后删除
"""
import logging
import os
import shutil
import threading
from typing import List

logger = logging.getLogger("bilibot.video_u.cleanup")

_SCHEDULED: List[threading.Timer] = []


def cleanup_now(paths: List[str]) -> None:
    """立即删除指定路径"""
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info(f"已删除临时文件: {path}")
            elif os.path.isdir(path):
                shutil.rmtree(path)
                logger.info(f"已删除临时目录: {path}")
        except Exception as e:
            logger.warning(f"清理失败 {path}: {e}")


def schedule_cleanup(paths: List[str], delay_seconds: int = 1800) -> threading.Timer:
    """延迟清理临时文件"""

    def _run():
        cleanup_now(paths)

    timer = threading.Timer(delay_seconds, _run)
    timer.daemon = True
    timer.start()
    _SCHEDULED.append(timer)
    logger.info(f"已调度 {len(paths)} 个临时路径，{delay_seconds} 秒后清理")
    return timer


def cancel_all_scheduled() -> None:
    """取消所有已调度的清理任务（测试用）"""
    for timer in _SCHEDULED:
        timer.cancel()
    _SCHEDULED.clear()
