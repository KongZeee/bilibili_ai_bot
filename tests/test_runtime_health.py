from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bilibot.memory_brain import MemoryBrainStore, MemoryModelGateway, ObservationEnvelope
from bilibot.memory_brain.worker import PersistentMemoryWorker
from bilibot.runtime_health import (
    consolidation_schedule_reached,
    evaluate_consolidation_state,
    memory_enrichment_is_clear,
)


def test_consolidation_catches_up_after_exact_hour() -> None:
    before = datetime(2026, 8, 18, 2, 59, tzinfo=timezone.utc)
    after = datetime(2026, 8, 18, 14, 24, tzinfo=timezone.utc)

    assert consolidation_schedule_reached(before, hour=3) is False
    assert consolidation_schedule_reached(after, hour=3) is True


def test_missing_consolidation_state_is_only_clear_before_window() -> None:
    before = datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc)
    after = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)

    assert evaluate_consolidation_state(
        {}, now=before, state_exists=False, hour=3
    )["clear"] is True
    after_report = evaluate_consolidation_state(
        {}, now=after, state_exists=False, hour=3
    )
    assert after_report["clear"] is False
    assert after_report["outcome"] == "missing"


def test_recent_consolidation_error_is_not_ready() -> None:
    now = datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)
    report = evaluate_consolidation_state(
        {
            "last_attempt_date": "2026-08-18",
            "attempted_at": (now - timedelta(minutes=5)).isoformat(),
            "outcome": "exception",
        },
        now=now,
    )

    assert report["clear"] is False
    assert report["outcome"] == "exception"


def test_worker_claims_a_bounded_concurrent_batch(tmp_path: Path) -> None:
    store = MemoryBrainStore(tmp_path / "brain.db", account_id="a")
    for index in range(2):
        store.archive_observation(
            ObservationEnvelope(
                idempotency_key=f"p2-worker:{index}",
                source_type="comment",
                source_text="bounded enrichment test",
                job_types=("summarize_event",),
            )
        )

    class SlowWorker(PersistentMemoryWorker):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.active = 0
            self.max_active = 0

        async def _execute(self, job):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                return {"processed": 1}
            finally:
                self.active -= 1

    class Chat:
        enabled = True

        async def generate(self, prompt: str, **kwargs) -> str:
            return "ok"

    worker = SlowWorker(
        store,
        MemoryModelGateway(Chat(), None),
        worker_concurrency=2,
    )
    report = asyncio.run(worker.run_once(limit=2))

    assert report.claimed == 2
    assert report.completed == 2
    assert worker.max_active == 2


def test_memory_queue_contract_matches_count_and_age_limits() -> None:
    healthy = {
        "jobs": {"dead": 0},
        "operations": {
            "active_jobs": 2,
            "oldest_active_job_age_seconds": 120.0,
        },
    }
    stale = {
        "jobs": {"dead": 0},
        "operations": {
            "active_jobs": 2,
            "oldest_active_job_age_seconds": 901.0,
        },
    }

    assert memory_enrichment_is_clear(healthy) is True
    assert memory_enrichment_is_clear(stale) is False
    assert memory_enrichment_is_clear({"pending_jobs": 0, "dead_jobs": 1}) is False
