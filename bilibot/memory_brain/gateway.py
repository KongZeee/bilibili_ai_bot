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

    @staticmethod
    def _sanitize_event_summary(event: Mapping[str, Any], summary: str) -> str:
        """Guard against common LLM role confusions (e.g. watch -> publish)."""
        text = str(summary or "").strip()
        original = str(event.get("summary") or event.get("event_summary") or "").strip()
        event_type = str(event.get("event_type") or "").strip()
        source_type = str(event.get("source_type") or "").strip()
        title = str(event.get("title") or event.get("event_title") or "").strip()
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        owner = str((metadata or {}).get("owner") or "").strip()

        def _looks_like_publish(s: str) -> bool:
            return any(
                p in s
                for p in (
                    "发布了视频",
                    "发布视频",
                    "上传了视频",
                    "投稿了视频",
                    "投稿视频",
                    "发布了该视频",
                )
            )

        def _fallback_watch() -> str:
            if (
                original
                and any(token in original for token in ("观察", "观看", "看了"))
                and not _looks_like_publish(original)
            ):
                return original
            if title:
                base = f"观察了视频《{title}》"
                return f"{base}，UP主 {owner}" if owner else base
            if original and not _looks_like_publish(original):
                return original
            return "观察了视频"

        is_video_watch = (
            event_type in {"video_observation", "video_metadata_observation", "bangumi_episode"}
            or source_type in {"video", "video_metadata"}
        )
        is_video_experience = (
            event_type == "bot_experience" or source_type == "video_experience"
        )
        if is_video_watch or is_video_experience:
            bad_publish = (
                "发布了视频",
                "发布视频",
                "上传了视频",
                "投稿了视频",
                "投稿视频",
                "发布了该视频",
            )
            # "UP主XXX发布的视频" is OK as attribution; "亚托莉/自己发布了视频" is not.
            if any(p in text for p in bad_publish):
                # Keep only if it clearly attributes to UP, not the bot.
                botish = any(
                    token in text[:40]
                    for token in ("亚托莉", "自己", "Bot", "bot", "本人")
                )
                upish = "UP主" in text or "up主" in text
                if botish or not upish:
                    return _fallback_watch() if is_video_watch else (
                        original
                        or (
                            f"观看并评价了视频《{title}》"
                            if title
                            else (text or original)
                        )
                    )
        return text or original

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
        event_type = str(event.get("event_type") or "")
        source_type = str(event.get("source_type") or "")
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
            "4) Do not invent unseen platform titles. "
            "5) CRITICAL video role rules: "
            "If event_type is video_observation / video_metadata_observation / bangumi_episode, "
            "or source_type is video / video_metadata, this is Bot WATCHING or analyzing someone else's video. "
            "You MUST write \u89c2\u770b/\u89c2\u5bdf/\u770b\u4e86\u89c6\u9891, NEVER \u53d1\u5e03\u4e86\u89c6\u9891/\u4e0a\u4f20\u4e86\u89c6\u9891/\u6295\u7a3f. "
            "The UP\u4e3b may have published the video; Bot did not publish it. "
            "If event_type is bot_experience or source_type is video_experience, "
            "summarize as Bot watched and evaluated the video (score/mood/review/actions), "
            "still NEVER claim Bot published/uploaded the video. "
            "6) Prefer the existing event summary phrasing when present and correct.\n\n"
            f"event_type={event_type}; source_type={source_type}\n"
            "Event metadata JSON:\n"
            + json.dumps(event_meta, ensure_ascii=False, default=str)
            + "\n\nRaw sources:\n"
            + source
        )
        raw = await self.generate(prompt, max_tokens=600, temperature=0.0)
        return self._sanitize_event_summary(event, raw or "")

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
