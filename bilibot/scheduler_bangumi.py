"""_maybe_schedule_bangumi_check, _run_bangumi_daily_check, _do_bangumi_task extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime


logger = logging.getLogger("bilibot.scheduler_bangumi")


def maybe_schedule_bangumi_check(self, now: datetime) -> bool:
    """Dispatch the daily bangumi check without blocking the main loop."""
    service = getattr(self, "bangumi_service", None)
    if service is None or now.date() == self._last_bangumi_check_date:
        return False
    task = getattr(self, "_bangumi_check_task", None)
    if task is not None and not task.done():
        return False
    if time.monotonic() < float(getattr(self, "_bangumi_retry_after", 0.0) or 0.0):
        return False
    self._bangumi_check_task = self._spawn_memory_task(
        self._run_bangumi_daily_check(service, now.date()),
        tag=f"bangumi_daily:{self.account_id or '-'}:{now.date().isoformat()}",
    )
    return True

async def run_bangumi_daily_check(self, service, check_date) -> None:
    """Run a potentially long PGC update/watch job with bounded retries."""
    current = asyncio.current_task()
    try:
        result = await service.check_updates()
        self._last_bangumi_check_date = check_date
        self._bangumi_failure_count = 0
        self._bangumi_retry_after = 0.0
        result = result if isinstance(result, dict) else {}
        if result.get("updated", 0) > 0:
            logger.info(
                "追番更新检测: %s 部有更新，已观看 %s 集",
                result.get("updated", 0),
                result.get("watched", 0),
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failures = int(getattr(self, "_bangumi_failure_count", 0) or 0) + 1
        self._bangumi_failure_count = failures
        # 5m, 10m, 20m ... capped at 6h.  Avoid hammering Bilibili every
        # main-loop minute while preserving same-day recovery.
        delay = min(6 * 3600.0, 300.0 * (2 ** min(failures - 1, 8)))
        self._bangumi_retry_after = time.monotonic() + delay
        logger.error(
            "追番更新检测失败，将在 %.0f 秒后重试: %s",
            delay,
            type(exc).__name__,
            exc_info=True,
        )
    finally:
        if getattr(self, "_bangumi_check_task", None) is current:
            self._bangumi_check_task = None

async def do_bangumi_task(self, task_id: str) -> None:
    """Execute one observable manual/retry bangumi TaskRun."""
    task = self.task_store.get(task_id) if self.task_store else None
    if task is None:
        logger.warning("番剧 TaskRun 不存在: %s", task_id)
        return
    status = getattr(task, "status", None)
    if status == "scheduled" and not self.task_store.claim(task_id):
        logger.warning("番剧 TaskRun claim 失败: %s", task_id)
        return
    elif status not in ("scheduled", "claimed"):
        logger.warning("番剧 TaskRun 状态不可执行: %s status=%s", task_id, status)
        return
    if not self.task_store.start(task_id):
        logger.warning("番剧 TaskRun start 失败: %s", task_id)
        return
    service = getattr(self, "bangumi_service", None)
    if service is None:
        self._fail_task(
            task_id,
            "BANGUMI_DISABLED",
            "番剧追更未启用或记忆大脑未就绪",
            retryable=False,
        )
        return
    try:
        result = await service.check_updates()
        result = result if isinstance(result, dict) else {}
        self._last_bangumi_check_date = datetime.now().date()
        self._bangumi_failure_count = 0
        self._bangumi_retry_after = 0.0
        self._succeed_task(
            task_id,
            {
                "success": True,
                "summary": (
                    f"追番检查完成：更新 {result.get('updated', 0)} 部，"
                    f"观看 {result.get('watched', 0)} 集"
                ),
                "updated": int(result.get("updated", 0) or 0),
                "watched": int(result.get("watched", 0) or 0),
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("手动追番任务失败: %s", type(exc).__name__, exc_info=True)
        self._fail_task(
            task_id,
            "BANGUMI_CHECK_FAILED",
            str(exc)[:500],
            retryable=True,
        )
