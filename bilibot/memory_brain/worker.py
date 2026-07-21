"""Durable leased worker for derived memory indexes and associations."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .gateway import MemoryModelGateway, validate_suggested_links
from .models import ClaimedJob, ProviderNotConfigured
from .store import MemoryBrainStore, content_hash


logger = logging.getLogger("bilibot.memory_brain.worker")

_ALL_JOB_TYPES = (
    "summarize_event",
    "embed_event",
    "embed_chunks",
    "extract_entities",
    "link_associations",
)
_EXPENSIVE_CHAT_JOB_TYPES = frozenset(
    {"summarize_event", "extract_entities", "link_associations"}
)


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
        enrichment_chat_timeout_seconds: float = 12.0,
        link_candidate_limit: int = 12,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.worker_id = worker_id or f"memory-{uuid.uuid4().hex}"
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.embedding_batch_size = max(1, min(int(embedding_batch_size), 256))
        self.enrichment_chat_timeout_seconds = max(
            1.0, float(enrichment_chat_timeout_seconds)
        )
        self.link_candidate_limit = max(4, min(int(link_candidate_limit), 24))
        self._throttle_reasons: set[str] = set()
        self._failure_streaks: dict[str, int] = {}
        self._circuit_until: dict[str, float] = {}
        self._last_poll_at = 0.0
        self._last_job_started_at = 0.0
        self._last_job_finished_at = 0.0
        self._last_job_error_at = 0.0
        self._last_job_type = ""
        self._completed_total = 0
        self._failed_total = 0

    def set_throttled(self, enabled: bool, *, reason: str = "") -> None:
        key = str(reason or "manual").strip() or "manual"
        if enabled:
            self._throttle_reasons.add(key)
        else:
            self._throttle_reasons.discard(key)

    def runtime_status(self) -> dict[str, Any]:
        now = time.time()
        reasons = sorted(self._throttle_reasons)
        return {
            "throttled": bool(reasons),
            "throttle_reason": ",".join(reasons),
            "throttle_reasons": reasons,
            "circuits": {
                key: until
                for key, until in self._circuit_until.items()
                if float(until or 0) > now
            },
            "failure_streaks": dict(self._failure_streaks),
            "last_poll_at": self._last_poll_at or None,
            "last_job_started_at": self._last_job_started_at or None,
            "last_job_finished_at": self._last_job_finished_at or None,
            "last_job_error_at": self._last_job_error_at or None,
            "last_job_type": self._last_job_type,
            "completed_total": self._completed_total,
            "failed_total": self._failed_total,
        }

    def _claimable_job_types(self) -> tuple[str, ...]:
        now = time.time()
        result = []
        for job_type in _ALL_JOB_TYPES:
            if self._throttle_reasons and job_type in _EXPENSIVE_CHAT_JOB_TYPES:
                continue
            if float(self._circuit_until.get(job_type) or 0) > now:
                continue
            result.append(job_type)
        return tuple(result)

    def _record_job_failure(self, job_type: str) -> None:
        if job_type not in _EXPENSIVE_CHAT_JOB_TYPES:
            return
        streak = int(self._failure_streaks.get(job_type) or 0) + 1
        self._failure_streaks[job_type] = streak
        if streak >= 3:
            cooldown = min(15 * 60.0, 2 * 60.0 * (2 ** min(streak - 3, 3)))
            self._circuit_until[job_type] = time.time() + cooldown
            logger.warning(
                "memory enrichment circuit opened: type=%s streak=%s cooldown=%ss",
                job_type,
                streak,
                int(cooldown),
            )

    def _record_job_success(self, job_type: str) -> None:
        self._failure_streaks.pop(job_type, None)
        self._circuit_until.pop(job_type, None)

    async def run_once(self, limit: int = 1) -> WorkerRunReport:
        self._last_poll_at = time.time()
        available = self.gateway.available_job_types()
        if available:
            self.store.unblock_blocked_jobs(available)
        claimable = self._claimable_job_types()
        if not claimable:
            return WorkerRunReport()
        jobs = self.store.claim_jobs(
            self.worker_id,
            limit=limit,
            lease_seconds=self.lease_seconds,
            job_types=claimable,
        )
        completed = blocked = retried = dead = 0
        for job in jobs:
            self._last_job_started_at = time.time()
            self._last_job_type = job.job_type
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except ProviderNotConfigured as exc:
                self._last_job_error_at = time.time()
                self._failed_total += 1
                if self.store.block_job(job.id, self.worker_id, str(exc)):
                    blocked += 1
            except Exception as exc:
                self._last_job_error_at = time.time()
                self._failed_total += 1
                self._record_job_failure(job.job_type)
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
                self._record_job_success(job.job_type)
                if self.store.complete_job(job.id, self.worker_id):
                    completed += 1
                    self._completed_total += 1
            finally:
                self._last_job_finished_at = time.time()
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
            # Claim exactly one durable job so a newly-arrived online request can
            # throttle expensive enrichment before the next chat call. Claiming
            # eight jobs gave no throughput benefit because execution is serial.
            report = await self.run_once(limit=1)
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
            summary = await self.gateway.summarize_event(
                event, timeout=self.enrichment_chat_timeout_seconds
            )
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
            entities = await self.gateway.extract_entities(
                event, timeout=self.enrichment_chat_timeout_seconds
            )
            self._renew_or_lose(job)
            self.store.upsert_entities(job.event_id, entities)
            self.store.requeue_event_job(job.event_id, "link_associations")
            return
        if job.job_type == "link_associations":
            recent = [
                item
                for item in self.store.recent_events(limit=50)
                if item["id"] != job.event_id
            ]
            candidates = self._rank_link_candidates(event, recent)
            if candidates:
                self._renew_or_lose(job)
                links = await self.gateway.suggest_links(
                    event,
                    candidates,
                    timeout=self.enrichment_chat_timeout_seconds,
                )
                self._renew_or_lose(job)
                self.store.upsert_links(
                    job.event_id,
                    validate_suggested_links(event, candidates, links),
                )
            return
        raise ValueError(f"unsupported memory job type: {job.job_type}")

    @staticmethod
    def _link_terms(value: Any) -> set[str]:
        text = " ".join(str(value or "").casefold().split())
        terms = set(re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", text))
        # Chinese summaries often arrive as one long token. Character bigrams
        # retain topical overlap without sending the whole recent history to LLM.
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            terms.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
        return terms

    def _rank_link_candidates(
        self,
        event: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        source_terms = self._link_terms(
            f"{event.get('title') or ''} {event.get('summary') or ''}"
        )
        source_type = str(event.get("event_type") or event.get("source_type") or "")
        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for position, item in enumerate(candidates):
            candidate_terms = self._link_terms(
                f"{item.get('title') or ''} {item.get('summary') or ''}"
            )
            overlap = len(source_terms & candidate_terms)
            score = float(overlap)
            candidate_type = str(
                item.get("event_type") or item.get("source_type") or ""
            )
            if source_type and candidate_type == source_type:
                score += 1.5
            ranked.append((score, -position, item))
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        positive = [item for score, _position, item in ranked if score > 0]
        if len(positive) < min(4, self.link_candidate_limit):
            seen = {str(item.get("id") or "") for item in positive}
            positive.extend(
                item
                for item in candidates
                if str(item.get("id") or "") not in seen
            )
        return positive[: self.link_candidate_limit]

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
