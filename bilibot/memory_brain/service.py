"""Account-scoped facade for V6 archive, enrichment, recall and privacy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .gateway import MemoryModelGateway
from .models import Observation, ObservationEnvelope, SourceDocument
from .recall import RecallEngine, RecallQuery, RecallResult
from .redaction import PrivateMessageRedactor, RedactionResult
from .store import MemoryBrainStore
from .worker import PersistentMemoryWorker, WorkerRunReport


logger = logging.getLogger("bilibot.memory_brain")


def _config_value(config: Any, key: str, default: Any) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MemoryBrainService:
    """The only runtime owner of one account's memory brain."""

    def __init__(
        self,
        account_id: str,
        data_dir: str | Path,
        *,
        chat_provider: Any = None,
        embedding_provider: Any = None,
        memory_config: Any = None,
        store: MemoryBrainStore | None = None,
    ) -> None:
        self.account_id = str(account_id)
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "memory_brain.db"
        store_config = {
            "chunk_target_chars": int(_config_value(memory_config, "chunk_target_chars", 600)),
            "chunk_hard_chars": int(_config_value(memory_config, "chunk_hard_chars", 900)),
            "chunk_target_tokens": int(_config_value(memory_config, "chunk_target_tokens", 450)),
            "chunk_hard_tokens": int(_config_value(memory_config, "chunk_hard_tokens", 700)),
            "chunk_overlap_chars": int(_config_value(memory_config, "chunk_overlap_chars", 100)),
            "job_max_attempts": int(_config_value(memory_config, "job_max_attempts", 8)),
            "vector_batch_size": int(_config_value(memory_config, "vector_batch_size", 2048)),
            "vector_cache_limit": int(_config_value(memory_config, "vector_cache_limit", 50_000)),
        }
        self.store = store or MemoryBrainStore(
            self.db_path,
            account_id=self.account_id,
            **store_config,
        )
        if store is not None:
            self.store.configure_runtime(**store_config)
        if self.store.account_id and self.store.account_id != self.account_id:
            raise ValueError("memory store account does not match service account")
        self.gateway = MemoryModelGateway(chat_provider, embedding_provider)
        self.worker = PersistentMemoryWorker(self.store, self.gateway)
        self.recall_engine = RecallEngine(
            self.store,
            self.gateway,
            prompt_budget=int(_config_value(memory_config, "prompt_char_budget", 5000)),
            max_candidates=int(_config_value(memory_config, "recall_candidate_limit", 20)),
            max_events=int(_config_value(memory_config, "recall_inject_limit", 5)),
            max_associations=int(_config_value(memory_config, "recall_association_limit", 2)),
            relevance_baseline=float(
                _config_value(memory_config, "rerank_relevance_baseline", 0.65)
            ),
            vector_batch_size=store_config["vector_batch_size"],
        )
        self.redactor = PrivateMessageRedactor(self.store.get_privacy_salt())
        self.memory_config = memory_config
        self._stop_event: asyncio.Event | None = None
        self._worker_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self.worker.run_forever(self._stop_event),
            name=f"memory-brain-worker:{self.account_id}",
        )

    async def close(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            if task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                await asyncio.to_thread(self.flush)
                return
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                # The owned worker was cancelled independently.
        await asyncio.to_thread(self.flush)

    def flush(self) -> None:
        checkpoint = getattr(self.store, "checkpoint", None)
        if callable(checkpoint):
            checkpoint()

    def health_check(self):
        return self.store.health_check()

    def archive_observation(self, envelope: ObservationEnvelope):
        if isinstance(envelope, Mapping):
            envelope = ObservationEnvelope(**dict(envelope))
        if not isinstance(envelope, ObservationEnvelope):
            raise TypeError("envelope must be an ObservationEnvelope or mapping")
        if envelope.account_id and envelope.account_id != self.account_id:
            raise ValueError("observation account_id does not match the memory service account")
        if not envelope.account_id:
            envelope = replace(envelope, account_id=self.account_id)
        return self.store.archive_observation(envelope)

    async def archive_observation_async(self, envelope: ObservationEnvelope):
        return await asyncio.to_thread(self.archive_observation, envelope)

    async def run_jobs_until_idle(self, max_jobs: int = 1000) -> WorkerRunReport:
        return await self.worker.run_until_idle(max_jobs=max_jobs)

    async def consolidate_recent(self, day_key: str, limit: int = 40) -> int:
        """Add one evidence-linked nightly reflection without rewriting sources."""
        marker = f"nightly:{day_key}"
        if await asyncio.to_thread(self.has_identifier, marker):
            return 0
        if not self.gateway.chat_configured:
            return 0
        events = await asyncio.to_thread(self.store.recent_events, max(2, min(limit, 100)))
        compact = [
            {
                "event_id": event.get("id"),
                "title": event.get("title"),
                "summary": event.get("summary") or event.get("content"),
                "time": event.get("occurred_at") or event.get("created_at"),
            }
            for event in events
            if event.get("id")
            and event.get("event_type") != "reflection"
            and (event.get("summary") or event.get("content") or event.get("title"))
        ]
        if len(compact) < 2:
            return 0
        allowed_ids = {str(item["event_id"]) for item in compact}
        prompt = (
            "Create zero to five concise Chinese memory reflections supported only by the "
            "event IDs below. Return strict JSON array. Each item must contain summary, "
            "relation (reflection|updates|supersedes|contradicts|corrects), and "
            "evidence_event_ids. Never overwrite source experiences and do not turn a "
            "reported user claim into a verified fact.\n"
            + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        )
        rows = await self.gateway.generate_json(
            prompt,
            max_tokens=600,
            temperature=0.0,
            timeout=30.0,
        )
        if not isinstance(rows, list):
            raise ValueError("nightly consolidation must return a JSON array")
        allowed_relations = {
            "reflection",
            "updates",
            "supersedes",
            "contradicts",
            "corrects",
        }
        validated = []
        for row in rows[:5]:
            if not isinstance(row, Mapping):
                continue
            summary = str(row.get("summary") or "").strip()
            evidence = list(dict.fromkeys(str(item) for item in row.get("evidence_event_ids") or ()))
            relation = str(row.get("relation") or "reflection")
            if (
                not summary
                or not evidence
                or not set(evidence).issubset(allowed_ids)
                or relation not in allowed_relations
            ):
                continue
            validated.append({"summary": summary, "relation": relation, "evidence": evidence})
        if not validated:
            return 0

        from .ingestion import text_observation

        result = await self.archive_observation_async(
            text_observation(
                account_id=self.account_id,
                idempotency_key=marker,
                source_type="reflection",
                event_type="reflection",
                text=json.dumps(validated, ensure_ascii=False, indent=2),
                title=f"夜间记忆巩固 {day_key}",
                scene="system",
                metadata={"day": day_key, "reflection_count": len(validated)},
                importance=0.7,
            )
        )
        links = []
        for row in validated:
            for evidence_id in row["evidence"]:
                links.append(
                    {
                        "target_event_id": evidence_id,
                        "relation_type": row["relation"],
                        "weight": 0.8,
                        "evidence_ids": [evidence_id],
                    }
                )
        await asyncio.to_thread(self.store.upsert_links, result.event_id, links)
        return len(validated)

    async def recall(self, query: RecallQuery | Mapping[str, Any]) -> RecallResult:
        if isinstance(query, Mapping):
            query = RecallQuery.from_mapping(query)
        if not query.account_id:
            query = RecallQuery(**{**asdict(query), "account_id": self.account_id})
        result = await self.recall_engine.recall(query)
        try:
            await asyncio.to_thread(
                self.store.record_recall_trace,
                query,
                self._trace_payload(result),
            )
        except Exception as exc:
            logger.warning(
                "failed to persist memory recall trace: account=%s error=%s",
                self.account_id,
                type(exc).__name__,
            )
        return result

    @staticmethod
    def _trace_payload(result: RecallResult) -> dict[str, Any]:
        trace = result.trace
        injected = set(trace.injected_event_ids)
        return {
            "used_fallback": trace.used_fallback,
            "rerank_status": trace.rerank_status,
            "rerank_calls": trace.rerank_calls,
            "channel_errors": dict(trace.channel_errors),
            "latency_ms": trace.latency_ms,
            "prompt_chars": trace.prompt_chars,
            "candidates": [
                {
                    "event_id": item.candidate_id,
                    "channels": list(item.channels),
                    "channel_ranks": dict(item.channel_ranks),
                    "rrf_score": item.rrf_score,
                    "deterministic_score": item.deterministic_score,
                    "llm_score": item.llm_score,
                    "final_score": item.final_score,
                    "decision": item.kind,
                    "threshold": item.threshold,
                    "reason": item.reason,
                    "evidence_ids": list(item.evidence_ids),
                    "injected": item.candidate_id in injected,
                    "accepted": item.accepted,
                }
                for item in trace.candidates
            ],
        }

    def redact_private_message(
        self, text: str, *, actor_id: str | int, username: str = ""
    ) -> RedactionResult:
        return self.redactor.redact(text, actor_id=actor_id, current_username=username)

    async def archive_private_message(
        self,
        *,
        platform_message_id: str,
        text: str,
        actor_id: str | int,
        username: str = "",
        direction: str = "incoming",
        persona_id: str = "",
        redacted: RedactionResult | None = None,
    ) -> tuple[RedactionResult, Any]:
        safe = redacted or self.redact_private_message(
            text, actor_id=actor_id, username=username
        )
        safe_payload = self.redactor.redact_payload(
            {"direction": direction, "username": username},
            current_username=username,
        )
        safe_message_id = self.redactor.pseudonymize_identifier(
            platform_message_id, namespace="pm_message"
        )
        envelope = ObservationEnvelope(
            idempotency_key=(
                f"private_message:{self.account_id}:{safe_message_id}:{direction}"
            ),
            account_id=self.account_id,
            source_type="private_message",
            event_type="conversation_message" if direction == "incoming" else "bot_action",
            event_title="私信观察" if direction == "incoming" else "私信回复",
            speaker_actor_id=(safe.actor_pseudonym if direction == "incoming" else "self"),
            persona_id=persona_id,
            scene="private_message",
            importance=0.5,
            occurred_at=time.time(),
            metadata={"direction": direction, "redacted": True},
            sources=(
                SourceDocument(
                    source_type="private_message",
                    external_id=safe_message_id,
                    full_text=safe.text,
                    data=safe_payload,
                    observations=(
                        Observation(
                            text=safe.text,
                            modality="private_message",
                            actor_id=(safe.actor_pseudonym if direction == "incoming" else "self"),
                            external_id=safe_message_id,
                        ),
                    ),
                ),
            ),
        )
        result = await self.archive_observation_async(envelope)
        return safe, result

    # ------------------------------------------------------------------
    # Compatibility facade. These methods write only memory_brain.db.
    # ------------------------------------------------------------------

    def write_atom(
        self,
        content: str,
        category: str = "other",
        metadata: Mapping[str, Any] | None = None,
        user_id: str = "self",
        username: str = "",
        session_id: str = "",
        persona_id: str = "",
        importance: str = "medium",
        importance_score: float = 0.5,
        **_: Any,
    ) -> str:
        payload = {
            "content": str(content),
            "category": category,
            "metadata": dict(metadata or {}),
            "user_id": str(user_id),
            "session_id": str(session_id),
        }
        digest = _stable_digest(payload)
        result = self.archive_observation(
            ObservationEnvelope(
                idempotency_key=f"compat:{self.account_id}:{category}:{digest}",
                account_id=self.account_id,
                source_type=category,
                event_type=category,
                event_summary=str(content) if len(str(content)) <= 500 else "",
                speaker_actor_id=str(user_id or "self"),
                persona_id=persona_id,
                importance=max(0.0, min(1.0, float(importance_score))),
                metadata={**dict(metadata or {}), "session_id": session_id, "username": username},
                sources=(
                    SourceDocument(
                        source_type=category,
                        external_id=session_id,
                        full_text=str(content),
                        data=dict(metadata or {}),
                    ),
                ),
            )
        )
        return result.event_id

    async def save_conversation_as_memory(
        self,
        conversation_history: Sequence[Mapping[str, Any]],
        user_id: str = "",
        username: str = "",
        session_id: str = "",
        bot_name: str = "",
        persona_id: str = "",
        **_: Any,
    ) -> str:
        observations = tuple(
            Observation(
                text=str(item.get("content") or ""),
                modality="conversation_turn",
                actor_id=(
                    "self" if str(item.get("role")) == "assistant" else str(item.get("user_id") or user_id)
                ),
                data={"role": str(item.get("role") or "")},
            )
            for item in conversation_history
            if item.get("content")
        )
        text = "\n".join(
            f"{item.get('role', 'unknown')}: {item.get('content', '')}"
            for item in conversation_history
            if item.get("content")
        )
        digest = _stable_digest([session_id, text])
        result = await self.archive_observation_async(
            ObservationEnvelope(
                idempotency_key=f"conversation:{self.account_id}:{session_id}:{digest}",
                account_id=self.account_id,
                source_type="comment",
                event_type="conversation",
                event_title=f"与 {username or user_id} 的对话",
                speaker_actor_id=str(user_id),
                persona_id=persona_id,
                scene="reply_comment",
                importance=0.5,
                metadata={"session_id": session_id, "bot_name": bot_name},
                sources=(
                    SourceDocument(
                        source_type="conversation",
                        external_id=session_id,
                        full_text=text,
                        observations=observations,
                    ),
                ),
            )
        )
        return result.event_id

    def get_user_memories(
        self, user_id: str, limit: int = 20, persona_id: str = ""
    ) -> list[dict[str, Any]]:
        # persona_id is provenance in V6, never an account-internal recall filter.
        return self.store.recent_events(limit=limit, speaker_actor_id=str(user_id))

    async def get_user_memories_async(
        self, user_id: str, limit: int = 20, persona_id: str = ""
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.get_user_memories, user_id, limit, persona_id)

    async def search_memories(
        self, query: str, user_id: str = "", limit: int = 5, **kwargs: Any
    ) -> list[dict[str, Any]]:
        result = await self.recall(
            RecallQuery(
                current_message=str(query),
                account_id=self.account_id,
                speaker_actor_id=str(user_id),
                scene=str(kwargs.get("scene") or "reply_comment"),
            )
        )
        return list(result.events[: max(0, int(limit))])

    def has_identifier(self, *identifiers: str) -> bool:
        values = [str(item) for item in identifiers if str(item)]
        return bool(self.store.find_events_by_identifiers(values, limit=1)) if values else False

    def find_by_identifiers(self, identifiers: Sequence[str], limit: int = 20):
        return self.store.find_events_by_identifiers(identifiers, limit=limit)

    def list_events(self, **kwargs: Any):
        return self.store.list_events(**kwargs)

    def get_event(self, event_id: str, chunks_per_event: int | None = None):
        return self.store.get_event(event_id, chunks_per_event=chunks_per_event)

    def stats(self):
        return self.store.stats()

    def hard_delete_event(self, event_id: str, **kwargs: Any) -> bool:
        return self.store.hard_delete_event(event_id, **kwargs)

    def list_jobs(self, **kwargs: Any):
        return self.store.list_jobs(**kwargs)

    def retry_dead_letter(self, job_id: str) -> bool:
        return self.store.retry_dead_letter(job_id)

    def list_recall_traces(self, **kwargs: Any):
        return self.store.list_recall_traces(**kwargs)

    def get_recall_trace(self, trace_id: str):
        return self.store.get_recall_trace(trace_id)

    def reindex_all(self):
        method = getattr(self.store, "reindex_all", None)
        if not callable(method):
            raise NotImplementedError("store does not provide reindex_all")
        return method()

    def backup_to(self, destination: str | Path):
        return self.store.backup_to(destination)

    def upsert_bangumi_watch_state(self, *args: Any, **kwargs: Any) -> None:
        self.store.upsert_bangumi_watch_state(*args, **kwargs)

    def get_bangumi_watch_state(self, *args: Any, **kwargs: Any):
        return self.store.get_bangumi_watch_state(*args, **kwargs)

    def list_bangumi_watch_state(self, *args: Any, **kwargs: Any):
        return self.store.list_bangumi_watch_state(*args, **kwargs)

    # V5 automatic deletion hooks are intentionally inert in V6.
    def cleanup_expired(self) -> int:
        return 0

    async def cleanup_expired_async(self) -> int:
        return 0

    def forget_low_importance(self) -> int:
        return 0

    async def forget_low_importance_async(self) -> int:
        return 0

    def update_user_affection(self, *_: Any, **__: Any) -> None:
        return None


__all__ = ["MemoryBrainService"]
