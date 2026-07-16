"""Durable leased worker for derived memory indexes and associations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from .gateway import MemoryModelGateway, validate_suggested_links
from .models import ClaimedJob, ProviderNotConfigured
from .store import MemoryBrainStore, content_hash


logger = logging.getLogger("bilibot.memory_brain.worker")


@dataclass(frozen=True)
class WorkerRunReport:
    claimed: int = 0
    completed: int = 0
    blocked: int = 0
    retried: int = 0
    dead: int = 0


class PersistentMemoryWorker:
    """Processes outbox jobs without holding SQLite connections across awaits."""

    def __init__(
        self,
        store: MemoryBrainStore,
        gateway: MemoryModelGateway,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 120.0,
        embedding_batch_size: int = 64,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.worker_id = worker_id or f"memory-{uuid.uuid4().hex}"
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.embedding_batch_size = max(1, min(int(embedding_batch_size), 256))

    async def run_once(self, limit: int = 1) -> WorkerRunReport:
        available = self.gateway.available_job_types()
        if available:
            self.store.unblock_blocked_jobs(available)
        jobs = self.store.claim_jobs(
            self.worker_id,
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        completed = blocked = retried = dead = 0
        for job in jobs:
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except ProviderNotConfigured as exc:
                if self.store.block_job(job.id, self.worker_id, str(exc)):
                    blocked += 1
            except Exception as exc:
                status = self.store.fail_job(
                    job.id,
                    self.worker_id,
                    f"{type(exc).__name__}: {exc}",
                )
                if status == "dead":
                    dead += 1
                elif status == "retry":
                    retried += 1
                logger.warning(
                    "memory job failed: job_id=%s type=%s status=%s error=%s",
                    job.id,
                    job.job_type,
                    status,
                    type(exc).__name__,
                )
            else:
                if self.store.complete_job(job.id, self.worker_id):
                    completed += 1
            finally:
                if job.event_id:
                    self.store.refresh_event_index_status(job.event_id)
        return WorkerRunReport(
            claimed=len(jobs),
            completed=completed,
            blocked=blocked,
            retried=retried,
            dead=dead,
        )

    async def run_until_idle(self, max_jobs: int = 1000) -> WorkerRunReport:
        totals = {"claimed": 0, "completed": 0, "blocked": 0, "retried": 0, "dead": 0}
        remaining = max(0, int(max_jobs))
        while remaining:
            report = await self.run_once(limit=min(16, remaining))
            for key in totals:
                totals[key] += getattr(report, key)
            remaining -= report.claimed
            if report.claimed == 0:
                break
        return WorkerRunReport(**totals)

    async def run_forever(
        self, stop_event: asyncio.Event, *, poll_interval: float = 1.0
    ) -> None:
        while not stop_event.is_set():
            report = await self.run_once(limit=8)
            if report.claimed:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=max(0.05, float(poll_interval))
                )
            except asyncio.TimeoutError:
                pass

    def _renew_or_lose(self, job: ClaimedJob) -> None:
        """Renew lease around long LLM awaits; raise if ownership was lost."""
        if not self.store.renew_job_lease(job.id, self.worker_id, self.lease_seconds):
            raise RuntimeError(f"memory job lease was lost: {job.job_type}:{job.id}")

    async def _execute(self, job: ClaimedJob) -> None:
        if not job.event_id:
            raise ValueError("memory enrichment job has no event_id")
        event = self.store.get_event_index_input(job.event_id)
        if job.job_type == "summarize_event":
            self._renew_or_lose(job)
            summary = await self.gateway.summarize_event(event)
            self._renew_or_lose(job)
            self.store.update_event_summary(job.event_id, summary)
            # These jobs may have completed before the summary was available.
            self.store.requeue_event_job(job.event_id, "embed_event")
            self.store.requeue_event_job(job.event_id, "link_associations")
            return
        if job.job_type == "embed_event":
            await self._embed_event(job, event)
            return
        if job.job_type == "embed_chunks":
            await self._embed_chunks(job, event)
            return
        if job.job_type == "extract_entities":
            self._renew_or_lose(job)
            entities = await self.gateway.extract_entities(event)
            self._renew_or_lose(job)
            self.store.upsert_entities(job.event_id, entities)
            self.store.requeue_event_job(job.event_id, "link_associations")
            return
        if job.job_type == "link_associations":
            # 候选池扩大到 50 条，避免较早的相关事件永远无法被关联
            candidates = [
                item
                for item in self.store.recent_events(limit=50)
                if item["id"] != job.event_id
            ]
            if candidates:
                self._renew_or_lose(job)
                links = await self.gateway.suggest_links(event, candidates)
                self._renew_or_lose(job)
                self.store.upsert_links(
                    job.event_id,
                    validate_suggested_links(event, candidates, links),
                )
            return
        raise ValueError(f"unsupported memory job type: {job.job_type}")

    async def _embed_event(self, job: ClaimedJob, event: dict[str, Any]) -> None:
        text = event["embedding_text"]
        if not text:
            # Do not complete-as-success with a missing vector; leave a diagnosable failure
            # so reindex/retry can pick it up after summary/title becomes available.
            raise ValueError("EMPTY_EMBEDDING_TEXT: event has no embedding_text")
        self._renew_or_lose(job)
        batch = await self.gateway.embed_texts([text])
        self._renew_or_lose(job)
        self.store.upsert_embedding(
            "event",
            event["id"],
            batch.vectors[0],
            provider=batch.provider,
            model=batch.model,
            content_digest=content_hash(text),
        )

    async def _embed_chunks(self, job: ClaimedJob, event: dict[str, Any]) -> None:
        chunks = event["chunks"]
        for start in range(0, len(chunks), self.embedding_batch_size):
            if not self.store.renew_job_lease(job.id, self.worker_id, self.lease_seconds):
                raise RuntimeError("embedding job lease was lost")
            current = chunks[start : start + self.embedding_batch_size]
            batch = await self.gateway.embed_texts([item["text"] for item in current])
            for item, vector in zip(current, batch.vectors):
                self.store.upsert_embedding(
                    "chunk",
                    item["id"],
                    vector,
                    provider=batch.provider,
                    model=batch.model,
                    content_digest=item["content_hash"],
                )
