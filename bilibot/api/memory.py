"""Account-scoped V6 memory brain management API.

The API deliberately has a single persistence boundary: ``MemoryBrainStore``.
It never opens a legacy memory database and never falls back to legacy JSON.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from bilibot.memory_brain import MemoryBrainStore
from bilibot.memory_brain.prompt import render_memory_evidence
from bilibot.memory_brain.recall import RecallEngine, RecallQuery
from bilibot.memory_brain.store import content_hash

from .responses import fail, fail_internal, ok
from .sole_account import _guard_nested_account_id, _inject_sole_path_params

logger = logging.getLogger("bilibot.api.memory")

_ROOT_SUNSET_DATE = "Sat, 31 Dec 2026 23:59:59 GMT"
_MAX_PAGE_SIZE = 100
_MAX_GRAPH_EVENTS = 500
_JOB_TYPES = (
    "summarize_event",
    "embed_event",
    "embed_chunks",
    "extract_entities",
    "link_associations",
)


# Import-only shims for downstream extensions that still import the V5 helper
# names. Direct database access is intentionally unavailable in V6.
def _get_conn(_data_dir: str) -> None:
    raise RuntimeError("direct memory database access was removed in V6")


def _ensure_schema(_connection: Any, _data_dir: str = "") -> None:
    raise RuntimeError("direct memory schema management was removed in V6")


class _DebugRecallStore:
    """Delegate recall reads while suppressing production reinforcement."""

    def __init__(self, store: MemoryBrainStore) -> None:
        self._store = store
        self.account_id = store.account_id

    def reinforce_recall(self, _event_ids: Sequence[str], link_ids: Sequence[str] = ()) -> None:
        del link_ids

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _deprecation_headers() -> dict[str, str]:
    return {
        "Deprecation": "true",
        "Sunset": _ROOT_SUNSET_DATE,
        "Link": '</api/accounts/{account_id}/memory>; rel="successor-version"',
    }


def _gone_response() -> JSONResponse:
    return fail(
        "GONE",
        "该记忆端点已废弃，请使用账号级 V6 记忆 API",
        {"successor": "/api/accounts/{account_id}/memory"},
        status_code=410,
    )


def _with_deprecation(response: JSONResponse) -> JSONResponse:
    response.headers.update(_deprecation_headers())
    return response


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _recall_engine_options(runtime_engine: Any = None) -> dict[str, Any]:
    """Mirror account limits for the non-reinforcing admin recall engine."""

    return {
        "rerank_timeout": float(getattr(runtime_engine, "rerank_timeout", 8.0)),
        "total_timeout": float(getattr(runtime_engine, "total_timeout", 10.0)),
        "prompt_budget": int(getattr(runtime_engine, "prompt_budget", 5000)),
        "max_candidates": int(getattr(runtime_engine, "max_candidates", 20)),
        "max_events": int(getattr(runtime_engine, "max_events", 5)),
        "max_associations": int(getattr(runtime_engine, "max_associations", 2)),
        "relevance_baseline": float(
            getattr(runtime_engine, "relevance_baseline", 0.65)
        ),
        "vector_batch_size": int(getattr(runtime_engine, "vector_batch_size", 2048)),
    }


def _job_index_health(event: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = {
        str(job.get("job_type") or ""): str(job.get("status") or "")
        for job in jobs
        if str(job.get("event_id") or "") == str(event.get("id") or "")
    }
    embedding_states = [statuses.get("embed_event"), statuses.get("embed_chunks")]
    if embedding_states and all(state == "completed" for state in embedding_states):
        embedding = "ready"
    elif any(state == "dead" for state in embedding_states):
        embedding = "degraded"
    elif any(state == "blocked" for state in embedding_states):
        embedding = "blocked"
    else:
        embedding = "pending"
    status = str(event.get("index_status") or "pending")
    return {
        "status": status,
        "healthy": status == "ready",
        "fts": "ready",
        "embedding": embedding,
        "jobs": statuses,
    }


def _event_dto(
    event: Mapping[str, Any],
    *,
    jobs: Sequence[Mapping[str, Any]] = (),
    include_full: bool = False,
    hit_channels: Sequence[str] = (),
    evidence_chunk_ids: Iterable[str] = (),
) -> dict[str, Any]:
    event_id = str(event.get("id") or event.get("event_id") or "")
    summary = str(event.get("summary") or "").strip()
    title = str(event.get("title") or "").strip()
    event_type = str(event.get("event_type") or "observation")
    source_type = str(event.get("source_type") or "")
    sources = list(event.get("sources") or [])
    # 列表预览：视频类事件必须能一眼看出「是哪支视频」。
    # summary 常为 ≤2000 字 video_detail，不能单独当 content，否则标题丢失。
    video_like = event_type in {
        "video_observation",
        "video_metadata_observation",
        "bangumi_episode",
    } or source_type in {"video", "video_metadata", "bangumi"}
    if video_like and title and summary:
        if title in summary or summary.startswith(f"《{title}"):
            content = summary
        else:
            content = f"《{title}》\n{summary}"
    else:
        content = summary or title
    if not content and sources:
        content = str(sources[0].get("full_text") or "")
    chunks = list(event.get("chunks") or [])
    evidence_ids = {str(value) for value in evidence_chunk_ids if value}
    # 方便前端单独渲染标题/摘要
    metadata = event.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    bvid = str(metadata.get("bvid") or "")
    owner = str(metadata.get("owner") or "")
    dto: dict[str, Any] = {
        "id": event_id,
        "event_id": event_id,
        "event_type": event_type,
        "category": event_type,
        "title": title,
        "summary": summary,
        "content": content,
        "source_type": source_type,
        "source": source_type,
        "index_status": str(event.get("index_status") or "pending"),
        "status": str(event.get("index_status") or "pending"),
        "importance": float(event.get("importance") or 0.0),
        "scene": str(event.get("scene") or ""),
        "speaker_actor_id": str(event.get("speaker_actor_id") or ""),
        "persona_id": str(event.get("persona_id") or ""),
        "occurred_at": event.get("occurred_at"),
        "created_at": event.get("created_at"),
        "updated_at": event.get("updated_at"),
        "recall_count": int(event.get("recall_count") or 0),
        "last_recalled_at": event.get("last_recalled_at"),
        "source_count": len(sources),
        "observation_count": len(event.get("observations") or []),
        "chunk_count": len(chunks),
        "entity_count": len(event.get("entities") or []),
        "index_health": _job_index_health(event, jobs),
        "hit_channels": list(dict.fromkeys(str(item) for item in hit_channels if item)),
        "bvid": bvid,
        "owner": owner,
    }
    if include_full:
        dto.update(
            {
                "metadata": _jsonable(metadata),
                "sources": _jsonable(sources),
                "observations": _jsonable(event.get("observations") or []),
                "chunks": _jsonable(chunks),
                "entities": _jsonable(event.get("entities") or []),
                "links": _jsonable(event.get("links") or []),
                "evidence_chunks": _jsonable(
                    [chunk for chunk in chunks if not evidence_ids or str(chunk.get("id")) in evidence_ids]
                ),
            }
        )
    return dto


class _MemoryApi:
    def __init__(self, account_manager: Any, data_root: str | Path) -> None:
        self.account_manager = account_manager
        self.data_root = Path(getattr(account_manager, "data_root", None) or data_root or "./data")
        self._stores: dict[str, MemoryBrainStore] = {}

    def default_account_id(self) -> str:
        if self.account_manager:
            sole = ""
            try:
                sole = str(self.account_manager.sole_id() or "").strip()
            except Exception:
                sole = ""
            if sole:
                return sole
            account_id = str(self.account_manager.get_default_id() or "").strip()
            if account_id:
                return account_id
        return "default"

    def account_exists(self, account_id: str) -> bool:
        if not self.account_manager:
            return False
        try:
            return bool(self.account_manager.has_account(account_id))
        except Exception:
            return False

    def runtime_account(self, account_id: str) -> Any:
        if not self.account_manager:
            return None
        try:
            return self.account_manager.get_account(account_id)
        except Exception:
            return None

    def is_disabled(self, account_id: str) -> bool:
        return self.account_exists(account_id) and self.runtime_account(account_id) is None

    def store(self, account_id: str) -> MemoryBrainStore:
        if account_id not in self._stores:
            self._stores[account_id] = MemoryBrainStore.for_account(self.data_root, account_id)
        return self._stores[account_id]

    def resolve(self, request: Request, *, root: bool = False) -> tuple[str, MemoryBrainStore] | JSONResponse:
        if root:
            account_id = self.default_account_id()
            # Product single-account manager: prefer sole_id when available
            if self.account_manager is not None and hasattr(self.account_manager, "sole_id"):
                try:
                    sole = str(self.account_manager.sole_id() or "").strip()
                except Exception:
                    sole = ""
                if sole:
                    account_id = sole
        else:
            guard = _guard_nested_account_id(
                request, self.account_manager, param="account_id"
            )
            if guard is not None:
                return guard
            account_id = str(request.path_params.get("account_id") or "")
            if not self.account_exists(account_id):
                return fail("NOT_FOUND", f"账号不存在: {account_id}", status_code=404)
        try:
            return account_id, self.store(account_id)
        except ValueError as exc:
            return fail("INVALID_ACCOUNT_ID", str(exc), status_code=400)
        except Exception:
            logger.exception("初始化 V6 记忆库失败", extra={"account_id": account_id})
            return fail_internal("记忆库初始化失败")

    def reject_mutation(self, account_id: str) -> JSONResponse | None:
        if self.is_disabled(account_id):
            return fail(
                "FORBIDDEN",
                f"账号 {account_id} 已禁用，只允许读取记忆",
                status_code=403,
            )
        return None

    async def audit_mutation(
        self,
        action: str,
        account_id: str,
        *,
        object_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a bodyless admin action without changing operation success."""
        audit_store = getattr(self.account_manager, "audit_store", None)
        method = (
            getattr(audit_store, "record_async", None)
            or getattr(audit_store, "record", None)
        )
        if not callable(method):
            return
        target = {
            "kind": "memory_admin",
            "action": str(action),
            "account_id": str(account_id),
        }
        if object_id:
            target["object_id"] = str(object_id)
        target.update(dict(details or {}))
        try:
            result = method(
                scene="memory_admin",
                persona_id="admin",
                input_summary=str(action),
                published=True,
                target=target,
                status="published",
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                "记录记忆管理审计失败: action=%s account=%s error=%s",
                action,
                account_id,
                type(exc).__name__,
            )

    def _all_jobs(self, store: MemoryBrainStore) -> list[dict[str, Any]]:
        return store.list_jobs(limit=500, offset=0)

    def stats(self, store: MemoryBrainStore, account_id: str) -> dict[str, Any]:
        raw = store.stats()
        counts = dict(raw.get("counts") or {})
        # 全表 GROUP BY，避免 list_events(limit=500) 截断导致分类不全
        by_event_type = {
            str(k or "observation"): int(v or 0)
            for k, v in dict(raw.get("event_types") or {}).items()
        }
        index_statuses = {
            str(k or "pending"): int(v or 0)
            for k, v in dict(raw.get("index_statuses") or {}).items()
        }
        # 近 24h / 近 7 日：仅扫最近一批时间戳（展示用，允许近似）
        events = store.list_events(limit=500, offset=0)
        now = time.time()
        health = _jsonable(store.health_check())
        runtime_status: dict[str, Any] = {}
        runtime = self.runtime_account(account_id)
        brain = getattr(runtime, "memory_brain", None) if runtime is not None else None
        status_method = getattr(brain, "operational_status", None)
        if callable(status_method):
            try:
                runtime_status = _jsonable(status_method())
            except Exception:
                logger.debug(
                    "failed to read live memory worker status",
                    exc_info=True,
                    extra={"account_id": account_id},
                )
        companion = getattr(runtime, "companion", None) if runtime is not None else None
        snapshot_method = getattr(companion, "get_self_snapshot", None)
        if callable(snapshot_method):
            try:
                snapshot = snapshot_method()
                payload = _jsonable(
                    snapshot.to_dict() if callable(getattr(snapshot, "to_dict", None)) else snapshot
                )
                salient = [
                    str(item)
                    for item in (payload.get("salient_recent") or [])
                    if str(item or "").strip()
                ]
                threads = [
                    str(item)
                    for item in (payload.get("ongoing_threads") or [])
                    if str(item or "").strip()
                ]
                updated_at = str(payload.get("updated_at") or "")
                freshness_seconds = None
                if updated_at:
                    try:
                        freshness_seconds = max(
                            0.0, time.time() - datetime.fromisoformat(updated_at).timestamp()
                        )
                    except (TypeError, ValueError):
                        freshness_seconds = None
                plan = companion.store.get_daily_plan()
                state = companion.store.get_life_state()
                evidence_bound = sum("|eid=" in item for item in salient)
                runtime_status["companion"] = {
                    "enabled": bool(getattr(companion, "enabled", False)),
                    "life_date": str(getattr(state, "date", "") or ""),
                    "updated_at": updated_at,
                    "freshness_seconds": freshness_seconds,
                    "salient_count": len(salient),
                    "salient_evidence_bound": evidence_bound,
                    "salient_evidence_rate": (
                        evidence_bound / len(salient) if salient else 0.0
                    ),
                    "ongoing_threads_count": len(threads),
                    "daily_plan_date": str(getattr(plan, "date", "") or ""),
                    "daily_plan_source": str(getattr(plan, "source", "") or ""),
                }
            except Exception:
                logger.debug(
                    "failed to read live companion status",
                    exc_info=True,
                    extra={"account_id": account_id},
                )
        total = int(counts.get("memory_events") or 0)
        return {
            "account_id": account_id,
            "total": total,
            "by_category": by_event_type,
            "categories": by_event_type,
            "by_source": dict(raw.get("sources") or {}),
            "sources": dict(raw.get("sources") or {}),
            "recent_24h": sum(1 for event in events if float(event.get("created_at") or 0) > now - 86400),
            "weekly_new": sum(1 for event in events if float(event.get("created_at") or 0) > now - 604800),
            "counts": counts,
            "jobs": dict(raw.get("jobs") or {}),
            "index_statuses": index_statuses,
            "health": health,
            "operations": dict(raw.get("operations") or {}),
            "runtime": runtime_status,
            "schema_version": raw.get("schema_version"),
            "graph_nodes": int(counts.get("memory_events") or 0) + int(counts.get("memory_entities") or 0),
            "graph_edges": int(counts.get("memory_links") or 0),
            "sessions": {},
        }

    def list_events(self, request: Request, store: MemoryBrainStore) -> dict[str, Any]:
        page = _bounded_int(request.query_params.get("page"), 1, 1, 1_000_000)
        page_size = _bounded_int(request.query_params.get("page_size"), 20, 1, _MAX_PAGE_SIZE)
        source_type = str(request.query_params.get("source_type") or "").strip()
        category = str(request.query_params.get("category") or request.query_params.get("type") or "").strip()
        status = str(request.query_params.get("status") or "").strip()
        keyword = str(request.query_params.get("keyword") or "").strip()
        include_intents = str(request.query_params.get("include_intents") or "true").lower()
        exclude_intents = include_intents in {"0", "false", "no"}
        active = str(request.query_params.get("active") or "").lower()
        if active in {"0", "false"}:
            return {"items": [], "page": page, "page_size": page_size, "total": 0}

        if keyword:
            matched = self.search(store, keyword, limit=500)["items"]
            rows = [item["_event"] for item in matched]
        elif category or status:
            rows = store.list_events(
                limit=500,
                offset=0,
                source_type=source_type or None,
                status=status or None,
                exclude_intents=exclude_intents,
            )
        else:
            rows = store.list_events(
                limit=page_size,
                offset=(page - 1) * page_size,
                source_type=source_type or None,
                exclude_intents=exclude_intents,
            )

        if exclude_intents and keyword:
            rows = [
                row
                for row in rows
                if str((row.get("metadata") or {}).get("action_state") or "").casefold()
                != "intent"
            ]

        if category:
            rows = [
                row for row in rows
                if str(row.get("event_type") or "") == category or str(row.get("source_type") or "") == category
            ]
        if source_type:
            rows = [row for row in rows if str(row.get("source_type") or "") == source_type]

        if keyword or category or status:
            total = len(rows)
            rows = rows[(page - 1) * page_size : page * page_size]
        elif source_type:
            total = store.count_events(
                source_type=source_type, exclude_intents=exclude_intents
            )
        else:
            total = store.count_events(exclude_intents=exclude_intents)

        event_ids = [str(row.get("id") or "") for row in rows]
        detailed = store.get_events(event_ids, chunks_per_event=None)
        jobs = self._all_jobs(store)
        return {
            "items": [_event_dto(event, jobs=jobs) for event in detailed],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def search(self, store: MemoryBrainStore, query: str, limit: int = 20) -> dict[str, Any]:
        query = str(query or "").strip()
        limit = max(1, min(int(limit), 100))
        if not query:
            return {"query": "", "items": [], "results": [], "total": 0}

        merged: dict[str, dict[str, Any]] = {}

        def collect(hits: Sequence[Mapping[str, Any]], channel: str) -> None:
            for rank, hit in enumerate(hits, start=1):
                event_id = str(hit.get("event_id") or hit.get("id") or "")
                if not event_id:
                    continue
                item = merged.setdefault(
                    event_id,
                    {"event_id": event_id, "channels": [], "evidence_ids": [], "rank_score": 0.0},
                )
                if channel not in item["channels"]:
                    item["channels"].append(channel)
                chunk_id = str(hit.get("chunk_id") or "")
                if chunk_id and chunk_id not in item["evidence_ids"]:
                    item["evidence_ids"].append(chunk_id)
                item["rank_score"] += 1.0 / rank

        collect(store.find_events_by_identifiers([query], limit=limit), "explicit_id")
        collect(store.search_events_fts(query, limit=limit * 2), "event_fts")
        collect(store.search_chunks_fts(query, limit=limit * 3), "chunk_fts")
        ordered = sorted(merged.values(), key=lambda item: (-item["rank_score"], item["event_id"]))[:limit]
        events = {event["id"]: event for event in store.get_events([item["event_id"] for item in ordered], chunks_per_event=None)}
        jobs = self._all_jobs(store)
        items: list[dict[str, Any]] = []
        for hit in ordered:
            event = events.get(hit["event_id"])
            if not event:
                continue
            dto = _event_dto(
                event,
                jobs=jobs,
                include_full=True,
                hit_channels=hit["channels"],
                evidence_chunk_ids=hit["evidence_ids"],
            )
            dto["lexical_score"] = min(1.0, float(hit["rank_score"]))
            dto["_event"] = event
            items.append(dto)
        public_items = [{key: value for key, value in item.items() if key != "_event"} for item in items]
        return {"query": query, "items": items, "results": public_items, "total": len(items)}

    def detail(self, store: MemoryBrainStore, event_id: str) -> dict[str, Any] | None:
        event = store.get_event(event_id, chunks_per_event=None)
        if not event:
            return None
        jobs = self._all_jobs(store)
        dto = _event_dto(event, jobs=jobs, include_full=True)
        dto["jobs"] = [_jsonable(job) for job in jobs if str(job.get("event_id") or "") == event_id]
        return dto

    def graph(self, store: MemoryBrainStore, event_ids: Sequence[str] | None = None) -> dict[str, Any]:
        if event_ids is None:
            event_ids = [event["id"] for event in store.list_events(limit=_MAX_GRAPH_EVENTS, offset=0)]
        event_ids = list(dict.fromkeys(str(value) for value in event_ids if value))[:_MAX_GRAPH_EVENTS]
        events = store.get_events(event_ids, chunks_per_event=0)
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}
        memories: list[dict[str, Any]] = []
        pending_links: list[Mapping[str, Any]] = []
        type_counts: dict[str, int] = {}
        relation_counts: dict[str, int] = {}

        def add_node(node: dict[str, Any]) -> None:
            node_id = str(node["id"])
            if node_id not in nodes:
                node["degree"] = 0
                nodes[node_id] = node

        def add_edge(edge: dict[str, Any]) -> None:
            edge_id = str(edge["id"])
            if edge_id in edges or str(edge["source"]) not in nodes or str(edge["target"]) not in nodes:
                return
            edges[edge_id] = edge
            nodes[str(edge["source"])]["degree"] += 1
            nodes[str(edge["target"])]["degree"] += 1
            relation = str(edge.get("relation_type") or "related_to")
            relation_counts[relation] = relation_counts.get(relation, 0) + 1

        for event in events:
            event_id = str(event["id"])
            label = str(event.get("title") or event.get("summary") or event_id)
            add_node(
                {
                    "id": event_id,
                    "type": "event",
                    "node_kind": "event",
                    "label": label[:80],
                    "weight": float(event.get("importance") or 0.5),
                    "source_type": str(event.get("source_type") or ""),
                    "index_status": str(event.get("index_status") or "pending"),
                }
            )
            type_counts["event"] = type_counts.get("event", 0) + 1
            memories.append(_event_dto(event))
            for mention in event.get("entities") or []:
                entity_id = str(mention.get("entity_id") or "")
                if not entity_id:
                    continue
                entity_type = str(mention.get("entity_type") or "topic")
                add_node(
                    {
                        "id": entity_id,
                        "type": entity_type,
                        "node_kind": "entity",
                        "label": str(mention.get("canonical_name") or mention.get("surface_text") or entity_id),
                        "weight": float(mention.get("confidence") or 1.0),
                        "entity_type": entity_type,
                    }
                )
                edge_id = f"mention:{event_id}:{entity_id}"
                add_edge(
                    {
                        "id": edge_id,
                        "source": event_id,
                        "target": entity_id,
                        "relation_type": "mentions",
                        "weight": float(mention.get("confidence") or 1.0),
                        "confidence": float(mention.get("confidence") or 1.0),
                    }
                )
            pending_links.extend(event.get("links") or [])

        # Event rows may arrive in any order. Resolve event-to-event links only
        # after every node exists so graph output is deterministic.
        for link in pending_links:
            source = str(link.get("source_event_id") or "")
            target = str(link.get("target_event_id") or "")
            add_edge(
                {
                    "id": str(link.get("id") or f"link:{source}:{target}"),
                    "source": source,
                    "target": target,
                    "relation_type": str(link.get("relation_type") or "related_to"),
                    "weight": float(link.get("weight") or 0.5),
                    "confidence": float(link.get("weight") or 0.5),
                    "evidence_ids": list(link.get("evidence_ids") or []),
                }
            )

        # Entity types are counted only after all mentions have been deduplicated.
        for node in nodes.values():
            if node.get("node_kind") == "entity":
                node_type = str(node.get("type") or "topic")
                type_counts[node_type] = type_counts.get(node_type, 0) + 1
        snapshot = {
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "memories": memories,
            "entries": memories,
        }
        return {
            "snapshot": snapshot,
            "nodes": snapshot["nodes"],
            "edges": snapshot["edges"],
            "graph_nodes": len(nodes),
            "graph_edges": len(edges),
            "total_memories": len(events),
            "summary": {
                "node_type_breakdown": type_counts,
                "relation_breakdown": relation_counts,
            },
        }

    async def _call_recall(
        self, account_id: str, store: MemoryBrainStore, query: RecallQuery
    ) -> tuple[Any, str]:
        account = self.runtime_account(account_id)
        for name in ("memory_brain_service", "memory_service", "memory_brain"):
            target = getattr(account, name, None) if account else None
            method = getattr(target, "recall_debug", None)
            if not callable(method) and getattr(target, "gateway", None) is not None:
                runtime_engine = getattr(target, "recall_engine", None)
                engine = RecallEngine(
                    _DebugRecallStore(store),
                    target.gateway,
                    **_recall_engine_options(runtime_engine),
                )
                return await engine.recall(query), ""
            method = method or getattr(target, "recall", None) or getattr(target, "retrieve", None)
            if target is store or not callable(method):
                continue
            before = {item["id"] for item in store.list_recall_traces(limit=5, offset=0)}
            result = method(query)
            result = await result if inspect.isawaitable(result) else result
            new_traces = [
                item
                for item in store.list_recall_traces(limit=5, offset=0)
                if item["id"] not in before and str(item.get("scene") or "") == query.scene
            ]
            exact = next(
                (
                    item
                    for item in new_traces
                    if item.get("query_hash") == content_hash(query.current_message)
                ),
                None,
            )
            persisted_id = str((exact or (new_traces[0] if new_traces else {})).get("id") or "")
            return result, persisted_id

        gateway = None
        if account:
            gateway = getattr(account, "memory_model_gateway", None) or getattr(account, "model_gateway", None)
        runtime_engine = getattr(account, "recall_engine", None) if account else None
        if gateway is not None:
            engine = RecallEngine(
                _DebugRecallStore(store),
                gateway,
                **_recall_engine_options(runtime_engine),
            )
        else:
            engine = RecallEngine(
                _DebugRecallStore(store),
                chat_provider=getattr(account, "llm", None) if account else None,
                embedding_provider=getattr(account, "embedding_provider", None) if account else None,
                **_recall_engine_options(runtime_engine),
            )
        return await engine.recall(query), ""

    def _trace_candidates(self, trace: Any) -> list[dict[str, Any]]:
        raw = getattr(trace, "candidates", None)
        if raw is None and isinstance(trace, Mapping):
            raw = trace.get("candidates")
        result: list[dict[str, Any]] = []
        for candidate in raw or []:
            item = _jsonable(candidate)
            event_id = str(item.get("candidate_id") or item.get("event_id") or "")
            normalized = {
                **item,
                "event_id": event_id,
                "candidate_id": event_id,
                "channels": list(item.get("channels") or []),
                "D": item.get("deterministic_score", item.get("d", 0.0)),
                "L": item.get("llm_score", item.get("l")),
                "F": item.get("final_score", item.get("f", 0.0)),
                "injected": bool(item.get("accepted") or item.get("injected")),
            }
            result.append(normalized)
        return result

    def recall_result(
        self,
        store: MemoryBrainStore,
        query: RecallQuery,
        result: Any,
        persisted_trace_id: str = "",
    ) -> dict[str, Any]:
        if isinstance(result, Mapping):
            raw = dict(result)
            trace = raw.get("trace") or {}
            events = list(raw.get("events") or raw.get("memories") or [])
            evidence_text = str(raw.get("prompt_evidence") or raw.get("final_prompt") or "")
            existing_trace_id = str(raw.get("trace_id") or persisted_trace_id or "")
        else:
            trace = getattr(result, "trace", {})
            events = list(getattr(result, "events", ()) or getattr(result, "memories", ()))
            evidence = getattr(result, "evidence", None)
            evidence_text = str(getattr(evidence, "text", "") or getattr(result, "prompt_evidence", ""))
            existing_trace_id = str(getattr(result, "trace_id", "") or persisted_trace_id or "")

        trace_data = _jsonable(trace)
        candidates = self._trace_candidates(trace)
        mode = str(trace_data.get("mode") or ("fallback" if trace_data.get("used_fallback") else "llm"))
        if not existing_trace_id:
            persisted_candidates = [
                {
                    "event_id": item["event_id"],
                    "channels": item["channels"],
                    "channel_ranks": item.get("channel_ranks") or {},
                    "rrf_score": item.get("rrf_score") or 0.0,
                    "deterministic_score": item.get("D") or 0.0,
                    "llm_score": item.get("L"),
                    "final_score": item.get("F") or 0.0,
                    "decision": item.get("kind") or "direct",
                    "threshold": item.get("threshold") or 0.0,
                    "reason": item.get("reason") or "",
                    "evidence_ids": item.get("evidence_ids") or [],
                    "injected": item.get("injected", False),
                    "accepted": item.get("accepted", item.get("injected", False)),
                }
                for item in candidates
            ]
            existing_trace_id = store.save_recall_trace(
                query_hash=content_hash(query.current_message),
                query_text=query.current_message,
                scene=query.scene,
                used_fallback=mode == "fallback",
                rerank_status=str(trace_data.get("rerank_status") or ""),
                rerank_calls=int(trace_data.get("rerank_calls") or 0),
                channel_errors=trace_data.get("channel_errors") or {},
                latency_ms=float(trace_data.get("latency_ms") or 0.0),
                prompt_chars=len(evidence_text),
                candidates=persisted_candidates,
            )
        jobs = self._all_jobs(store)
        return {
            "trace_id": existing_trace_id,
            "events": [_event_dto(event, jobs=jobs, include_full=True) for event in events],
            "memories": [_event_dto(event, jobs=jobs) for event in events],
            "final_prompt": evidence_text,
            "prompt_evidence": evidence_text,
            "trace": {**trace_data, "mode": mode, "candidates": candidates},
        }

    def trace_detail(self, store: MemoryBrainStore, trace_id: str) -> dict[str, Any] | None:
        trace = store.get_recall_trace(trace_id)
        if not trace:
            return None
        used_fallback = bool(trace.get("used_fallback"))
        candidates = []
        injected_ids: list[str] = []
        for raw in trace.get("candidates") or []:
            item = dict(raw)
            event_id = str(item.get("event_id") or "")
            decision = str(item.get("decision") or "direct")
            if item.get("injected") and event_id:
                injected_ids.append(event_id)
            candidates.append(
                {
                    **item,
                    "candidate_id": event_id,
                    "D": item.get("deterministic_score"),
                    "L": item.get("llm_score"),
                    "F": item.get("final_score"),
                    "kind": decision,
                    "threshold": item.get("threshold") or (
                        (0.88 if decision == "association" else 0.80)
                        if used_fallback
                        else (0.80 if decision == "association" else 0.72)
                    ),
                }
            )
        selected = store.get_events(injected_ids, chunks_per_event=None)
        candidate_by_event = {
            str(item.get("event_id") or ""): item
            for item in candidates
            if item.get("injected") and item.get("event_id")
        }
        verified_selected = []
        for event in selected:
            event_id = str(event.get("id") or event.get("event_id") or "")
            candidate = candidate_by_event.get(event_id)
            if candidate is None:
                continue
            evidence_ids = set(candidate.get("evidence_ids") or [])
            value = dict(event)
            value["_recall_kind"] = candidate.get("kind") or "direct"
            value["chunks"] = [
                dict(chunk)
                for chunk in event.get("chunks") or []
                if str(chunk.get("id") or chunk.get("chunk_id") or "") in evidence_ids
            ][:2]
            verified_selected.append(value)
        selected = verified_selected
        evidence = render_memory_evidence(selected, max_total_chars=5000, max_events=5, max_associations=2)
        return {
            **trace,
            "used_fallback": used_fallback,
            "mode": (
                "empty"
                if not candidates
                else ("fallback" if used_fallback else "llm")
            ),
            "candidates": candidates,
            "events": [_event_dto(event, include_full=True) for event in selected],
            "final_prompt": evidence.text,
            "prompt_reconstructed": True,
        }


def _body_query(body: Mapping[str, Any]) -> str:
    return str(body.get("query") or body.get("keyword") or body.get("current_message") or body.get("message") or "").strip()


def _account_handlers(api: _MemoryApi) -> dict[str, Any]:
    async def resolved(request: Request) -> tuple[str, MemoryBrainStore] | JSONResponse:
        return api.resolve(request)

    async def stats(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            data = await asyncio.to_thread(api.stats, store, account_id)
            return ok(data)
        except Exception:
            logger.exception("读取记忆统计失败", extra={"account_id": account_id})
            return fail_internal("读取记忆统计失败")

    async def listing(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            data = await asyncio.to_thread(api.list_events, request, store)
            return ok(data)
        except Exception:
            logger.exception("读取记忆列表失败", extra={"account_id": account_id})
            return fail_internal("读取记忆列表失败")

    async def detail(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            mem_id = str(request.path_params.get("mem_id") or "")
            item = await asyncio.to_thread(api.detail, store, mem_id)
            return ok(item) if item else fail("NOT_FOUND", "记忆不存在", status_code=404)
        except Exception:
            logger.exception("读取记忆详情失败", extra={"account_id": account_id})
            return fail_internal("读取记忆详情失败")

    async def search(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            body = await request.json() if request.method == "POST" else dict(request.query_params)
            if not isinstance(body, Mapping):
                return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
            query = _body_query(body)
            limit = _bounded_int(body.get("limit"), 20, 1, 100)
            result = await asyncio.to_thread(api.search, store, query, limit)
            result["items"] = [{key: val for key, val in item.items() if key != "_event"} for item in result["items"]]
            return ok(result)
        except Exception:
            logger.exception("搜索记忆失败", extra={"account_id": account_id})
            return fail_internal("搜索记忆失败")

    async def recall(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
            message = _body_query(body)
            if not message:
                return fail("INVALID_INPUT", "query 不能为空", status_code=400)
            recent_turns = body.get("recent_turns") or []
            if not isinstance(recent_turns, list):
                return fail("INVALID_INPUT", "recent_turns 必须是数组", status_code=400)
            query = RecallQuery(
                current_message=message,
                recent_turns=recent_turns,
                account_id=account_id,
                speaker_actor_id=str(body.get("speaker_actor_id") or ""),
                title=str(body.get("title") or ""),
                bvid=str(body.get("bvid") or ""),
                oid=str(body.get("oid") or ""),
                scene=str(body.get("scene") or "memory_debug"),
                explicit_ids=tuple(body.get("explicit_ids") or ()),
                entity_hints=tuple(body.get("entity_hints") or ()),
            )
            result, persisted_trace_id = await api._call_recall(account_id, store, query)
            return ok(api.recall_result(store, query, result, persisted_trace_id))
        except Exception:
            logger.exception("记忆召回调试失败", extra={"account_id": account_id})
            return fail_internal("记忆召回调试失败")

    async def delete(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        rejected = api.reject_mutation(account_id)
        if rejected:
            return rejected
        event_id = str(request.path_params.get("mem_id") or "")
        try:
            deleted = store.hard_delete_event(event_id, reason="admin_api", deleted_by="admin")
            if not deleted:
                return fail("NOT_FOUND", "记忆不存在", status_code=404)
            await api.audit_mutation("hard_delete", account_id, object_id=event_id)
            return ok({"id": event_id, "deleted": True, "tombstone": True}, "记忆已永久删除")
        except Exception:
            logger.exception("永久删除记忆失败", extra={"account_id": account_id, "event_id": event_id})
            return fail_internal("永久删除记忆失败")

    async def graph(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            return ok(api.graph(store))
        except Exception:
            logger.exception("读取记忆图谱失败", extra={"account_id": account_id})
            return fail_internal("读取记忆图谱失败")

    async def graph_query(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            body = await request.json()
            if not isinstance(body, Mapping):
                return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
            query = _body_query(body)
            if not query:
                return ok(api.graph(store))
            hits = api.search(store, query, limit=50)["items"]
            event_ids = [str(item["id"]) for item in hits]
            related = store.related_events(event_ids, limit=50)
            event_ids.extend(str(item.get("event_id") or "") for item in related)
            return ok(api.graph(store, event_ids))
        except Exception:
            logger.exception("查询记忆图谱失败", extra={"account_id": account_id})
            return fail_internal("查询记忆图谱失败")

    async def traces(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            limit = _bounded_int(request.query_params.get("limit"), 100, 1, 500)
            offset = _bounded_int(request.query_params.get("offset"), 0, 0, 1_000_000)
            items = store.list_recall_traces(limit=limit, offset=offset)
            return ok({"items": items, "limit": limit, "offset": offset})
        except Exception:
            logger.exception("读取召回记录失败", extra={"account_id": account_id})
            return fail_internal("读取召回记录失败")

    async def trace_detail(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            item = api.trace_detail(store, str(request.path_params.get("trace_id") or ""))
            return ok(item) if item else fail("NOT_FOUND", "召回记录不存在", status_code=404)
        except Exception:
            logger.exception("读取召回记录详情失败", extra={"account_id": account_id})
            return fail_internal("读取召回记录详情失败")

    async def jobs(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        try:
            status = str(request.query_params.get("status") or "").strip() or None
            limit = _bounded_int(request.query_params.get("limit"), 100, 1, 500)
            offset = _bounded_int(request.query_params.get("offset"), 0, 0, 1_000_000)
            items = store.list_jobs(status=status, limit=limit, offset=offset)
            return ok({"items": items, "limit": limit, "offset": offset, "status": status})
        except Exception:
            logger.exception("读取记忆任务失败", extra={"account_id": account_id})
            return fail_internal("读取记忆任务失败")

    async def retry_job(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        rejected = api.reject_mutation(account_id)
        if rejected:
            return rejected
        job_id = str(request.path_params.get("job_id") or "")
        try:
            if not store.retry_dead_letter(job_id):
                return fail("NOT_FOUND", "死信任务不存在或当前不可重试", status_code=404)
            await api.audit_mutation("retry_dead_letter", account_id, object_id=job_id)
            return ok({"id": job_id, "status": "pending"}, "任务已重新排队")
        except Exception:
            logger.exception("重试记忆任务失败", extra={"account_id": account_id, "job_id": job_id})
            return fail_internal("重试记忆任务失败")

    async def retry_dead_jobs(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        rejected = api.reject_mutation(account_id)
        if rejected:
            return rejected
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, Mapping):
            return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
        job_type = str(body.get("job_type") or "").strip() or None
        event_id = str(body.get("event_id") or "").strip() or None
        if job_type and job_type not in _JOB_TYPES:
            return fail("INVALID_INPUT", "不支持的任务类型", status_code=400)
        if event_id and len(event_id) > 160:
            return fail("INVALID_INPUT", "event_id 过长", status_code=400)
        limit = _bounded_int(body.get("limit"), 100, 1, 500)
        try:
            count = await asyncio.to_thread(
                store.retry_dead_letters,
                job_type=job_type,
                event_id=event_id,
                limit=limit,
            )
            await api.audit_mutation(
                "retry_dead_letters",
                account_id,
                details={
                    "job_type": job_type,
                    "event_id": event_id,
                    "limit": limit,
                    "count": count,
                },
            )
            return ok(
                {
                    "count": count,
                    "job_type": job_type,
                    "event_id": event_id,
                    "status": "pending",
                }
            )
        except Exception:
            logger.exception("批量重试记忆死信失败", extra={"account_id": account_id})
            return fail_internal("批量重试失败")

    async def dead_job_report(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        _account_id, store = value
        limit = _bounded_int(request.query_params.get("limit"), 20, 1, 100)
        try:
            return ok(await asyncio.to_thread(store.dead_letter_report, limit=limit))
        except Exception:
            logger.exception("读取记忆死信报告失败")
            return fail_internal("读取死信报告失败")

    async def reindex(request: Request) -> JSONResponse:
        value = await resolved(request)
        if isinstance(value, JSONResponse):
            return value
        account_id, store = value
        rejected = api.reject_mutation(account_id)
        if rejected:
            return rejected
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, Mapping):
            return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
        try:
            event_id = str(body.get("event_id") or "").strip()
            if event_id and not store.get_event(event_id, chunks_per_event=0):
                return fail("NOT_FOUND", "记忆不存在", status_code=404)
            if not event_id:
                report = store.reindex_all(
                    clear_enrichment=bool(body.get("clear_enrichment", True))
                )
                await api.audit_mutation(
                    "reindex",
                    account_id,
                    details={"events_requeued": int(report.get("events") or 0)},
                )
                return ok({**report, "event_id": None})

            fts = store.rebuild_fts()
            events = [store.get_event(event_id, chunks_per_event=0)]
            requeued = 0
            for event in events:
                if not event:
                    continue
                changed = False
                for job_type in _JOB_TYPES:
                    changed = store.requeue_event_job(str(event["id"]), job_type) or changed
                if changed:
                    store.set_event_index_status(str(event["id"]), "pending")
                    requeued += 1
            await api.audit_mutation(
                "reindex",
                account_id,
                object_id=event_id,
                details={"events_requeued": requeued},
            )
            return ok({"fts": fts, "events_requeued": requeued, "event_id": event_id or None})
        except Exception:
            logger.exception("重建记忆索引失败", extra={"account_id": account_id})
            return fail_internal("重建记忆索引失败")

    return {
        "stats": stats,
        "listing": listing,
        "detail": detail,
        "search": search,
        "recall": recall,
        "delete": delete,
        "graph": graph,
        "graph_query": graph_query,
        "traces": traces,
        "trace_detail": trace_detail,
        "jobs": jobs,
        "retry_job": retry_job,
        "retry_dead_jobs": retry_dead_jobs,
        "dead_job_report": dead_job_report,
        "reindex": reindex,
    }


def create_account_memory_routes(account_manager: Any) -> list[Route]:
    """Create the authoritative account-scoped V6 memory routes."""

    api = _MemoryApi(account_manager, getattr(account_manager, "data_root", "./data"))
    handlers = _account_handlers(api)

    async def migrate(_request: Request) -> JSONResponse:
        return _gone_response()

    def _as_flat(handler):
        async def _flat(request: Request) -> JSONResponse:
            _, err = _inject_sole_path_params(
                request, account_manager, id_keys=("account_id",)
            )
            if err is not None:
                return err
            return await handler(request)

        return _flat

    nested = [
        Route("/api/accounts/{account_id}/memory/stats", handlers["stats"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/search", handlers["search"], methods=["GET", "POST"]),
        Route("/api/accounts/{account_id}/memory/recall", handlers["traces"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/recall", handlers["recall"], methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/recall/{trace_id}", handlers["trace_detail"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/recall-traces", handlers["traces"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/recall-traces/{trace_id}", handlers["trace_detail"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/graph", handlers["graph"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/graph/query", handlers["graph_query"], methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/reindex", handlers["reindex"], methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/jobs", handlers["jobs"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/jobs/dead-report", handlers["dead_job_report"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/jobs/retry-dead", handlers["retry_dead_jobs"], methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/jobs/{job_id}/retry", handlers["retry_job"], methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/migrate", migrate, methods=["POST"]),
        Route("/api/accounts/{account_id}/memory", handlers["listing"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/{mem_id}", handlers["detail"], methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/{mem_id}", handlers["delete"], methods=["DELETE"]),
    ]
    # Flat primary shell: /api/memory/* (writes + full surface via sole inject)
    flat = [
        Route("/api/memory/stats", _as_flat(handlers["stats"]), methods=["GET"]),
        Route("/api/memory/search", _as_flat(handlers["search"]), methods=["GET", "POST"]),
        Route("/api/memory/recall", _as_flat(handlers["traces"]), methods=["GET"]),
        Route("/api/memory/recall", _as_flat(handlers["recall"]), methods=["POST"]),
        Route("/api/memory/recall/{trace_id}", _as_flat(handlers["trace_detail"]), methods=["GET"]),
        Route("/api/memory/recall-traces", _as_flat(handlers["traces"]), methods=["GET"]),
        Route("/api/memory/recall-traces/{trace_id}", _as_flat(handlers["trace_detail"]), methods=["GET"]),
        Route("/api/memory/graph", _as_flat(handlers["graph"]), methods=["GET"]),
        Route("/api/memory/graph/query", _as_flat(handlers["graph_query"]), methods=["POST"]),
        Route("/api/memory/reindex", _as_flat(handlers["reindex"]), methods=["POST"]),
        Route("/api/memory/jobs", _as_flat(handlers["jobs"]), methods=["GET"]),
        Route("/api/memory/jobs/dead-report", _as_flat(handlers["dead_job_report"]), methods=["GET"]),
        Route("/api/memory/jobs/retry-dead", _as_flat(handlers["retry_dead_jobs"]), methods=["POST"]),
        Route("/api/memory/jobs/{job_id}/retry", _as_flat(handlers["retry_job"]), methods=["POST"]),
        Route("/api/memory", _as_flat(handlers["listing"]), methods=["GET"]),
        Route("/api/memory/{mem_id}", _as_flat(handlers["detail"]), methods=["GET"]),
        Route("/api/memory/{mem_id}", _as_flat(handlers["delete"]), methods=["DELETE"]),
    ]
    return flat + nested


def create_memory_routes(
    persona_store: Any,
    scheduler: Any = None,
    data_dir: str = "./data",
    account_manager: Any = None,
) -> list[Route]:
    """Create flat primary V6 memory routes via sole-id resolution.

    When ``account_manager`` is present, delegates to
    ``create_account_memory_routes`` (flat + nested). Otherwise keeps a
    minimal deprecated root-read surface for legacy callers without manager.
    """

    del persona_store, scheduler
    # Product AccountManager (has sole_id): flat primary + nested aliases
    if account_manager is not None and hasattr(account_manager, "sole_id"):
        return create_account_memory_routes(account_manager)

    # Legacy / test doubles: deprecated root reads + optional nested registered separately
    api = _MemoryApi(account_manager, data_dir)

    async def root_call(request: Request, operation: str) -> JSONResponse:
        value = api.resolve(request, root=True)
        if isinstance(value, JSONResponse):
            return _with_deprecation(value)
        account_id, store = value
        try:
            if operation == "stats":
                response = ok(api.stats(store, account_id))
            elif operation == "search":
                query = _body_query(dict(request.query_params))
                result = api.search(
                    store,
                    query,
                    _bounded_int(request.query_params.get("limit"), 20, 1, 100),
                )
                result["items"] = [
                    {key: val for key, val in item.items() if key != "_event"}
                    for item in result["items"]
                ]
                response = ok(result)
            elif operation == "listing":
                response = ok(api.list_events(request, store))
            elif operation == "detail":
                item = api.detail(store, str(request.path_params.get("id") or ""))
                response = ok(item) if item else fail("NOT_FOUND", "记忆不存在", status_code=404)
            elif operation == "graph":
                response = ok(api.graph(store))
            elif operation == "traces":
                limit = _bounded_int(request.query_params.get("limit"), 100, 1, 500)
                offset = _bounded_int(request.query_params.get("offset"), 0, 0, 1_000_000)
                response = ok({"items": store.list_recall_traces(limit=limit, offset=offset), "limit": limit, "offset": offset})
            elif operation == "trace_detail":
                item = api.trace_detail(store, str(request.path_params.get("trace_id") or ""))
                response = ok(item) if item else fail("NOT_FOUND", "召回记录不存在", status_code=404)
            elif operation == "jobs":
                status = str(request.query_params.get("status") or "").strip() or None
                response = ok({"items": store.list_jobs(status=status, limit=100, offset=0)})
            else:
                response = fail("NOT_FOUND", "端点不存在", status_code=404)
            return _with_deprecation(response)
        except Exception:
            logger.exception("读取默认账号 V6 记忆失败", extra={"account_id": account_id})
            return _with_deprecation(fail_internal("读取记忆失败"))

    async def stats(request: Request) -> JSONResponse:
        return await root_call(request, "stats")

    async def listing(request: Request) -> JSONResponse:
        return await root_call(request, "listing")

    async def search(request: Request) -> JSONResponse:
        return await root_call(request, "search")

    async def detail(request: Request) -> JSONResponse:
        return await root_call(request, "detail")

    async def graph(request: Request) -> JSONResponse:
        return await root_call(request, "graph")

    async def traces(request: Request) -> JSONResponse:
        return await root_call(request, "traces")

    async def trace_detail(request: Request) -> JSONResponse:
        return await root_call(request, "trace_detail")

    async def jobs(request: Request) -> JSONResponse:
        return await root_call(request, "jobs")

    async def gone(_request: Request) -> JSONResponse:
        return _gone_response()

    return [
        Route("/api/memory/stats", stats, methods=["GET"]),
        Route("/api/memory/search", search, methods=["GET"]),
        Route("/api/memory/recall", traces, methods=["GET"]),
        Route("/api/memory/recall/{trace_id}", trace_detail, methods=["GET"]),
        Route("/api/memory/recall-traces", traces, methods=["GET"]),
        Route("/api/memory/recall-traces/{trace_id}", trace_detail, methods=["GET"]),
        Route("/api/memory/graph", graph, methods=["GET"]),
        Route("/api/memory/jobs", jobs, methods=["GET"]),
        Route("/api/memory/search", gone, methods=["POST"]),
        Route("/api/memory/recall", gone, methods=["POST"]),
        Route("/api/memory/graph/query", gone, methods=["POST"]),
        Route("/api/memory/reindex", gone, methods=["POST"]),
        Route("/api/memory/jobs/{job_id}/retry", gone, methods=["POST"]),
        Route("/api/memory/migrate", gone, methods=["POST"]),
        Route("/api/memory", listing, methods=["GET"]),
        Route("/api/memory/{id}", detail, methods=["GET"]),
        Route("/api/memory/{id}", gone, methods=["DELETE", "PATCH", "PUT", "POST"]),
    ]
