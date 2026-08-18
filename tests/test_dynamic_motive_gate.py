from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bilibot.scheduler_motive import MotiveSnapshot
from bilibot.scheduler_proactive_tasks import check_proactive_tasks


@pytest.mark.asyncio
async def test_due_dynamic_is_not_dropped_by_rest_motive() -> None:
    spawned: list[asyncio.Task] = []

    class Config:
        def get_raw_config(self) -> dict:
            return {"features": {"dynamic_post": True, "proactive_video": False}}

    class Scheduler:
        config_loader = Config()
        _dynamic_times = [(10, 0)]
        _dynamic_triggered: set[str] = set()
        _proactive_times: list[tuple[int, int]] = []
        _proactive_triggered: set[str] = set()
        _tick_motive_snapshot_prepared = True
        _tick_motive_snapshot = MotiveSnapshot(action="rest", score=9.0)
        companion = SimpleNamespace(enabled=False)

        def _claim_task_for_slot(self, scene: str, slot: str) -> str | None:
            assert (scene, slot) == ("dynamic", "10:00")
            return "task-dynamic"

        async def _do_post_dynamic(self, task_id: str) -> None:
            assert task_id == "task-dynamic"

        def _spawn_memory_task(self, coro, tag: str = "") -> None:
            spawned.append(asyncio.create_task(coro))

        def _save_dynamic_schedule_state(self) -> None:
            pass

    scheduler = Scheduler()
    await check_proactive_tasks(scheduler, "10:01")
    await asyncio.gather(*spawned)

    assert len(spawned) == 1
    assert scheduler._dynamic_triggered == {"10:00"}
