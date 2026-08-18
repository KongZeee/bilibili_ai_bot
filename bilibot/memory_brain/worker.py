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
        unblock_grace_seconds: float = 30.0,
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
        # A blocked job may reopen only after this many seconds. Without a
        # grace period, ProviderNotConfigured failures (including empty LLM
        # content) become a claim→block→unblock hot loop that burns one HTTP
        # call per second and resets attempts to 0 forever.
        self.unblock_grace_seconds = max(0.0, float(unblock_grace_seconds))
        self._last_unblock_at = 0.0
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
        # SQLite I/O must never run on the event loop: a 284MB brain + WAL
        # checkpoint can stall the scheduler and web console for tens of
        # seconds. Every store call is dispatched to a worker thread.
        if available:
            now = time.time()
            if now - self._last_unblock_at >= self.unblock_grace_seconds:
                reopened = await asyncio.to_thread(
                    self.store.unblock_blocked_jobs,
                    available,
                    min_blocked_age_seconds=self.unblock_grace_seconds,
                )
                if reopened:
                    logger.info(
                        "memory worker reopened %s blocked jobs after grace=%ss",
                        reopened,
                        self.unblock_grace_seconds,
                    )
                self._last_unblock_at = now
        claimable = self._claimable_job_types()
        if not claimable:
            return WorkerRunReport()
        jobs = await asyncio.to_thread(
            self.store.claim_jobs,
            self.worker_id,
            limit,
            self.lease_seconds,
            job_types=claimable,
        )
        completed = blocked = retried = dead = 0
        for job in jobs:
            self._last_job_started_at = time.time()
            self._last_job_type = job.job_type
            try:
                result_counts = await self._execute(job)
            except asyncio.CancelledError:
                raise
            except ProviderNotConfigured as exc:
                self._last_job_error_at = time.time()
                self._failed_total += 1
                logger.warning(
                    "memory job blocked: job_id=%s type=%s error=%s",
                    job.id,
                    job.job_type,
                    exc,
                )
                if await asyncio.to_thread(
                    self.store.block_job, job.id, self.worker_id, str(exc)
                ):
                    blocked += 1
            except Exception as exc:
                self._last_job_error_at = time.time()
                self._failed_total += 1
                self._record_job_failure(job.job_type)
                status = await asyncio.to_thread(
                    self.store.fail_job,
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
                if await asyncio.to_thread(
                    self.store.complete_job, job.id, self.worker_id, result_counts
                ):
                    completed += 1
                    self._completed_total += 1
            finally:
                self._last_job_finished_at = time.time()
                if job.event_id:
                    try:
                        await asyncio.to_thread(
                            self.store.refresh_event_index_status, job.event_id
                        )
                    except Exception as exc:
                        # The event may have been hard-deleted mid-job. A
                        # missing row must never kill the durable worker.
                        logger.debug(
                            "memory job cleanup skipped for deleted event: job_id=%s event=%s error=%s",
                            job.id,
                            job.event_id,
                            exc,
                        )
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
            try:
                report = await self.run_once(limit=1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("memory worker run_once crashed; continuing loop")
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=max(0.05, float(poll_interval))
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            if report.claimed:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=max(0.05, float(poll_interval))
                )
            except asyncio.TimeoutError:
                pass

    async def _renew_or_lose(self, job: ClaimedJob) -> None:
        """Renew lease around long LLM awaits; raise if ownership was lost."""
        if not await asyncio.to_thread(
            self.store.renew_job_lease, job.id, self.worker_id, self.lease_seconds
        ):
            raise RuntimeError(f"memory job lease was lost: {job.job_type}:{job.id}")

    async def _execute(self, job: ClaimedJob) -> dict[str, int]:
        if not job.event_id:
            raise ValueError("memory enrichment job has no event_id")
        event = await asyncio.to_thread(self.store.get_event_index_input, job.event_id)
        if job.job_type == "summarize_event":
            await self._renew_or_lose(job)
            summary = await self.gateway.summarize_event(
                event, timeout=self.enrichment_chat_timeout_seconds
            )
            await self._renew_or_lose(job)
            await asyncio.to_thread(
                self.store.update_event_summary, job.event_id, summary
            )
            # These jobs may have completed before the summary was available.
            await asyncio.to_thread(
                self.store.requeue_event_job, job.event_id, "embed_event"
            )
            await asyncio.to_thread(
                self.store.requeue_event_job, job.event_id, "link_associations"
            )
            return {"summary_chars": len(str(summary or ""))}
        if job.job_type == "embed_event":
            await self._embed_event(job, event)
            return {"event_embeddings": 1}
        if job.job_type == "embed_chunks":
            await self._embed_chunks(job, event)
            return {"chunk_embeddings": len(event["chunks"])}
        if job.job_type == "extract_entities":
            await self._renew_or_lose(job)
            entities = await self.gateway.extract_entities(
                event, timeout=self.enrichment_chat_timeout_seconds
            )
            await self._renew_or_lose(job)
            entity_ids = await asyncio.to_thread(
                self.store.upsert_entities, job.event_id, entities
            )
            await asyncio.to_thread(
                self.store.requeue_event_job, job.event_id, "link_associations"
            )
            return {"entity_mentions": len(entity_ids)}
        if job.job_type == "link_associations":
            recent = [
                item
                for item in await asyncio.to_thread(
                    self.store.recent_events, 50
                )
                if item["id"] != job.event_id
            ]
            candidates = self._rank_link_candidates(event, recent)
            if candidates:
                fallback_reason = ""
                cooling = (
                    callable(getattr(self.gateway, "link_model_cooling", None))
                    and self.gateway.link_model_cooling()
                )
                if not cooling:
                    await self._renew_or_lose(job)
                    try:
                        links = await self.gateway.suggest_links(
                            event,
                            candidates,
                            timeout=self.enrichment_chat_timeout_seconds,
                        )
                    except Exception as exc:
                        # Reasoning endpoints can exhaust even a 16k budget on
                        # chain-of-thought, return malformed JSON, or hit transport
                        # timeouts. Shared extracted entities are an
                        # evidence-backed fallback that lets the event converge
                        # instead of dead-lettering; the fallback never invents a
                        # target the brain has not already seen.
                        fallback_reason = type(exc).__name__
                        note = getattr(
                            self.gateway, "note_link_model_failure", None
                        )
                        entered_cooldown = bool(
                            note and note(fallback_reason)
                        )
                        if entered_cooldown:
                            logger.info(
                                "memory link model entered cooldown %.0fs "
                                "after consecutive failures; using entity "
                                "overlap fallback for link_associations",
                                float(
                                    getattr(
                                        self.gateway,
                                        "link_model_cooldown_seconds",
                                        600,
                                    )
                                ),
                            )
                        else:
                            logger.warning(
                                "memory link model unusable, using entity "
                                "overlap fallback: job_id=%s error=%s",
                                job.id,
                                fallback_reason,
                            )
                        links = await asyncio.to_thread(
                            self.store.suggest_entity_links,
                            job.event_id,
                            [str(item["id"]) for item in candidates],
                        )
                else:
                    fallback_reason = "link_model_cooldown"
                    logger.debug(
                        "memory link model cooling; entity overlap fallback "
                        "for job_id=%s",
                        job.id,
                    )
                    links = await asyncio.to_thread(
                        self.store.suggest_entity_links,
                        job.event_id,
                        [str(item["id"]) for item in candidates],
                    )
                await self._renew_or_lose(job)
                link_ids = await asyncio.to_thread(
                    self.store.upsert_links,
                    job.event_id,
                    validate_suggested_links(event, candidates, links),
                )
                result_counts = {"links": len(link_ids)}
                if fallback_reason:
                    result_counts["link_fallback"] = 1
                return result_counts
            return {"links": 0}
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
        await self._renew_or_lose(job)
        batch = await self.gateway.embed_texts([text])
        await self._renew_or_lose(job)
        await asyncio.to_thread(
            self.store.upsert_embedding,
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
            if not await asyncio.to_thread(
                self.store.renew_job_lease,
                job.id,
                self.worker_id,
                self.lease_seconds,
            ):
                raise RuntimeError("embedding job lease was lost")
            current = chunks[start : start + self.embedding_batch_size]
            batch = await self.gateway.embed_texts([item["text"] for item in current])
            for item, vector in zip(current, batch.vectors):
                await asyncio.to_thread(
                    self.store.upsert_embedding,
                    "chunk",
                    item["id"],
                    vector,
                    provider=batch.provider,
                    model=batch.model,
                    content_digest=item["content_hash"],
                )
