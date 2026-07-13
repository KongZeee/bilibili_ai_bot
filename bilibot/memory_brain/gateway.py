"""Narrow model boundary used by memory enrichment and recall."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .models import ProviderNotConfigured, VectorDimensionError


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


# Background association is intentionally conservative. A language model can
# propose topical links, but only an explicit workflow can establish semantic
# claims such as contradiction, identity, or succession.
_AUTO_LINK_RELATIONS = frozenset({"related_to", "is_about", "supports"})


@dataclass(frozen=True)
class EmbeddingBatch:
    provider: str
    model: str
    vectors: tuple[tuple[float, ...], ...]


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _verified_memory_ids(event: Mapping[str, Any]) -> set[str]:
    values = {
        str(event.get("id") or event.get("event_id") or "").strip(),
    }
    collections = (
        event.get("sources") or (),
        event.get("observations") or (),
        event.get("chunks") or event.get("evidence_chunks") or (),
    )
    for collection in collections:
        if not isinstance(collection, Sequence) or isinstance(
            collection, (str, bytes, bytearray)
        ):
            continue
        for row in collection:
            if not isinstance(row, Mapping):
                continue
            values.add(
                str(
                    row.get("id")
                    or row.get("source_id")
                    or row.get("observation_id")
                    or row.get("chunk_id")
                    or ""
                ).strip()
            )
    values.discard("")
    return values


def _compact_link_evidence_ids(
    event: Mapping[str, Any], limit: int = 6
) -> list[str]:
    """Bound linker prompts while keeping only database-verifiable IDs."""

    allowed = _verified_memory_ids(event)
    event_id = str(event.get("id") or event.get("event_id") or "").strip()
    ordered = [event_id] if event_id and event_id in allowed else []
    ordered.extend(sorted(allowed.difference(ordered)))
    return ordered[: max(1, int(limit))]


def validate_suggested_links(
    source: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    links: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep automatic links whose evidence IDs belong to their source/target rows."""

    candidate_by_id = {
        str(item.get("id") or item.get("event_id") or "").strip(): item
        for item in candidates
        if isinstance(item, Mapping)
    }
    candidate_by_id.pop("", None)
    source_ids = _verified_memory_ids(source)
    result: list[Mapping[str, Any]] = []
    for item in links:
        if not isinstance(item, Mapping):
            continue
        target_id = str(item.get("target_event_id") or "").strip()
        relation_type = str(item.get("relation_type") or "").strip().casefold()
        target = candidate_by_id.get(target_id)
        raw_evidence = item.get("evidence_ids")
        if (
            target is None
            or target_id == str(source.get("id") or source.get("event_id") or "").strip()
            or relation_type not in _AUTO_LINK_RELATIONS
            or not isinstance(raw_evidence, Sequence)
            or isinstance(raw_evidence, (str, bytes, bytearray))
        ):
            continue
        if not raw_evidence or any(not isinstance(value, str) for value in raw_evidence):
            continue
        evidence_ids = list(dict.fromkeys(value.strip() for value in raw_evidence if value.strip()))
        allowed = source_ids | _verified_memory_ids(target)
        if not evidence_ids or not set(evidence_ids).issubset(allowed):
            continue
        result.append(
            {
                **dict(item),
                "target_event_id": target_id,
                "relation_type": relation_type,
                "evidence_ids": evidence_ids,
            }
        )
    return result


class MemoryModelGateway:
    """Keep chat and embedding providers independent and explicit."""

    def __init__(self, chat_provider: Any = None, embedding_provider: Any = None) -> None:
        self.chat_provider = chat_provider
        self.embedding_provider = embedding_provider

    @staticmethod
    def _enabled(provider: Any) -> bool:
        return provider is not None and getattr(provider, "enabled", True) is not False

    @property
    def chat_configured(self) -> bool:
        return self._enabled(self.chat_provider) and any(
            callable(getattr(self.chat_provider, name, None))
            for name in ("generate", "chat", "complete")
        )

    @property
    def embedding_configured(self) -> bool:
        return self._enabled(self.embedding_provider) and any(
            callable(getattr(self.embedding_provider, name, None))
            for name in ("embed_many", "embed", "get_embeddings", "get_embedding")
        )

    def available_job_types(self) -> tuple[str, ...]:
        result: list[str] = []
        if self.chat_configured:
            result.extend(("summarize_event", "extract_entities", "link_associations"))
        if self.embedding_configured:
            result.extend(("embed_event", "embed_chunks"))
        return tuple(result)

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_tokens: int = 600,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> str:
        provider = self.chat_provider
        if not self.chat_configured:
            raise ProviderNotConfigured("chat provider is not configured")
        method = (
            getattr(provider, "generate", None)
            or getattr(provider, "chat", None)
            or getattr(provider, "complete", None)
        )
        try:
            value = method(
                prompt,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except TypeError:
            value = method(prompt)
        result = await asyncio.wait_for(_resolve(value), timeout=max(0.1, float(timeout)))
        if result is None or not str(result).strip():
            raise ProviderNotConfigured("chat provider returned no result")
        return str(result).strip()

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_tokens: int = 600,
        temperature: float = 0.0,
        timeout: float = 30.0,
    ) -> Any:
        raw = await self.generate(
            prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        cleaned = _FENCE_RE.sub("", raw).strip()
        # 容错：LLM 可能返回带前后多余文本、尾随逗号、单引号等非标准 JSON
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # 尝试提取第一个 { 到最后一个 } 之间的内容
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(cleaned[start : end + 1])
            raise

    async def embed_texts(
        self, texts: Sequence[str], *, timeout: float = 60.0
    ) -> EmbeddingBatch:
        provider = self.embedding_provider
        if not self.embedding_configured:
            raise ProviderNotConfigured("embedding provider is not configured")
        values = [str(text) for text in texts]
        if not values:
            return EmbeddingBatch(self.embedding_provider_name, self.embedding_model_name, ())
        vectors: Any
        if callable(getattr(provider, "embed_many", None)):
            vectors = await asyncio.wait_for(
                _resolve(provider.embed_many(values)), timeout=max(0.1, float(timeout))
            )
        elif callable(getattr(provider, "get_embeddings", None)):
            vectors = await asyncio.wait_for(
                _resolve(provider.get_embeddings(values)), timeout=max(0.1, float(timeout))
            )
        elif callable(getattr(provider, "embed", None)):
            try:
                vectors = await asyncio.wait_for(
                    _resolve(provider.embed(values)), timeout=max(0.1, float(timeout))
                )
            except (TypeError, ValueError):
                vectors = [
                    await asyncio.wait_for(
                        _resolve(provider.embed(text)), timeout=max(0.1, float(timeout))
                    )
                    for text in values
                ]
        else:
            vectors = [
                await asyncio.wait_for(
                    _resolve(provider.get_embedding(text)), timeout=max(0.1, float(timeout))
                )
                for text in values
            ]
        if vectors is None:
            raise ProviderNotConfigured("embedding provider returned no result")
        if len(values) == 1 and vectors and isinstance(vectors[0], (int, float)):
            vectors = [vectors]
        if len(vectors) != len(values):
            raise ValueError(
                f"embedding provider returned {len(vectors)} vectors for {len(values)} texts"
            )
        normalized: list[tuple[float, ...]] = []
        dimension: int | None = None
        for vector in vectors:
            if vector is None:
                raise ProviderNotConfigured("embedding provider returned an empty vector")
            item = tuple(float(value) for value in vector)
            if not item:
                raise ValueError("embedding vector is empty")
            if dimension is None:
                dimension = len(item)
            elif dimension != len(item):
                raise VectorDimensionError("embedding provider returned mixed dimensions")
            normalized.append(item)
        return EmbeddingBatch(
            provider=self.embedding_provider_name,
            model=self.embedding_model_name,
            vectors=tuple(normalized),
        )

    @property
    def embedding_provider_name(self) -> str:
        provider = self.embedding_provider
        return str(
            getattr(provider, "llm_id", "")
            or getattr(provider, "provider_id", "")
            or getattr(provider, "name", "")
            or type(provider).__name__
        )

    @property
    def embedding_model_name(self) -> str:
        provider = self.embedding_provider
        return str(
            getattr(provider, "embedding_model", "")
            or getattr(provider, "model", "")
            or "unknown"
        )

    async def summarize_event(self, event: Mapping[str, Any]) -> str:
        sources = event.get("sources") or []
        source = "\n\n".join(str(item.get("full_text") or "") for item in sources)
        source_meta = [
            {
                "source_type": item.get("source_type"),
                "external_id": item.get("external_id"),
                "data": item.get("data") or {},
            }
            for item in sources
            if isinstance(item, Mapping)
        ]
        event_meta = {
            "event_type": event.get("event_type"),
            "source_type": event.get("source_type"),
            "title": event.get("title"),
            "speaker_actor_id": event.get("speaker_actor_id"),
            "persona_id": event.get("persona_id"),
            "scene": event.get("scene"),
            "metadata": event.get("metadata") or {},
            "sources": source_meta,
        }
        prompt = (
            "Summarize this observed event faithfully in concise Chinese. "
            "Treat user claims as reported statements, not verified facts. "
            "Use the event metadata below as authoritative role/context. "
            "Rules: "
            "1) If source_type is comment, summarize it as a user comment. "
            "The event title is only an internal UI label, never a platform title. "
            "Do not say it was under a title unless metadata or source data has dynamic_title, video_title, or target_title. "
            "2) If source_type is bot_action or speaker_actor_id is self, summarize it as Bot/\u4e9a\u6258\u8389/\u81ea\u5df1 performing or saying the action. "
            "Never write \u7528\u6237\u8bc4\u8bba\u79f0/\u7528\u6237\u8868\u793a for Bot's own reply text. "
            "3) For reply_comment bot actions, say \u4e9a\u6258\u8389\u56de\u590d\u4e86\u8bc4\u8bba\uff0c\u5185\u5bb9\u4e3a... "
            "4) Do not invent unseen platform titles.\n\n"
            "Event metadata JSON:\n"
            + json.dumps(event_meta, ensure_ascii=False, default=str)
            + "\n\nRaw sources:\n"
            + source
        )
        return await self.generate(prompt, max_tokens=600, temperature=0.0)

    async def extract_entities(self, event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        source = "\n\n".join(str(item.get("full_text") or "") for item in event["sources"])
        payload = await self.generate_json(
            "Return a JSON array of important named entities. Each item has name, type, "
            "aliases, confidence. Do not invent entities.\n\n" + source,
            max_tokens=600,
            temperature=0.0,
        )
        if not isinstance(payload, list):
            raise ValueError("entity extractor must return a JSON array")
        return [item for item in payload if isinstance(item, Mapping)]

    async def suggest_links(
        self, event: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        compact = [
            {
                "event_id": item.get("id"),
                "title": item.get("title"),
                "summary": item.get("summary"),
                "allowed_evidence_ids": _compact_link_evidence_ids(item),
            }
            for item in candidates
        ]
        source = {
            "id": event.get("id"),
            "summary": event.get("summary"),
            "allowed_evidence_ids": _compact_link_evidence_ids(event),
        }
        payload = await self.generate_json(
            "Choose supported associations for the source event from the candidate IDs only. "
            "relation_type must be one of related_to, is_about, supports. "
            "Return a JSON array with target_event_id, relation_type, weight, and a non-empty "
            "evidence_ids list using only allowed_evidence_ids from the source and selected target.\n"
            + json.dumps(
                {"source": source, "candidates": compact},
                ensure_ascii=False,
            ),
            max_tokens=600,
            temperature=0.0,
        )
        if not isinstance(payload, list):
            raise ValueError("linker must return a JSON array")
        return validate_suggested_links(event, candidates, payload)
