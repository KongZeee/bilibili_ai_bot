"""Narrow model boundary used by memory enrichment and recall."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .models import ProviderNotConfigured, VectorDimensionError


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# rebind_providers 区分「未传」与「显式 None」
_UNSET = object()


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

    def __init__(
        self,
        chat_provider: Any = None,
        embedding_provider: Any = None,
        *,
        account_id: str = "",
    ) -> None:
        self.chat_provider = chat_provider
        self.embedding_provider = embedding_provider
        # 用于 token usage_context 归因（未传则为空，兼容旧调用）
        self.account_id = str(account_id or "")

    def rebind_providers(
        self,
        *,
        chat_provider: Any = _UNSET,
        embedding_provider: Any = _UNSET,
    ) -> None:
        """热重载：替换 chat/embedding provider（不重建 worker/store）。

        未传的侧保持不变；显式传 None 清空该侧。
        例：rebind_providers(embedding_provider=ep)
        """
        if chat_provider is not _UNSET:
            self.chat_provider = chat_provider
        if embedding_provider is not _UNSET:
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

        def _invoke():
            try:
                return method(
                    prompt,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except TypeError:
                return method(prompt)

        # usage_context 仅包一层；失败不得二次 invoke（超时/429 会双倍烧配额）
        try:
            from bilibot.services.token_usage import usage_context

            _usage_cm = usage_context(
                scene="memory_brain",
                account_id=getattr(self, "account_id", "") or "",
            )
        except Exception:
            _usage_cm = None

        if _usage_cm is not None:
            with _usage_cm:
                result = await asyncio.wait_for(
                    _resolve(_invoke()), timeout=max(0.1, float(timeout))
                )
        else:
            result = await asyncio.wait_for(
                _resolve(_invoke()), timeout=max(0.1, float(timeout))
            )
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
        # 容错：LLM 可能返回带前后多余文本、尾随逗号、单引号、数组 JSON 等
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Prefer full array when present (entity/link jobs expect list payloads).
        # Do not collapse an array to a single object by taking first `{`…last `}`.
        candidates: list[str] = []
        arr_start = cleaned.find("[")
        arr_end = cleaned.rfind("]")
        if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
            candidates.append(cleaned[arr_start : arr_end + 1])
        obj_start = cleaned.find("{")
        obj_end = cleaned.rfind("}")
        if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
            candidates.append(cleaned[obj_start : obj_end + 1])

        last_err: Optional[Exception] = None
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_err = exc
                # Reuse provider-side repair if available (trailing commas etc.)
                try:
                    from bilibot.llm.provider import LLMProvider

                    repaired = LLMProvider.repair_json(candidate)
                    if repaired and repaired != candidate:
                        return json.loads(repaired)
                except Exception as repair_exc:
                    last_err = repair_exc
        if last_err is not None:
            raise last_err
        raise json.JSONDecodeError("No JSON object/array found", cleaned, 0)

    async def embed_texts(
        self, texts: Sequence[str], *, timeout: float = 60.0
    ) -> EmbeddingBatch:
        provider = self.embedding_provider
        if not self.embedding_configured:
            raise ProviderNotConfigured("embedding provider is not configured")
        values = [str(text) for text in texts]
        if not values:
            return EmbeddingBatch(self.embedding_provider_name, self.embedding_model_name, ())
        # usage_context 覆盖 embedding 调用（provider 内部 record 会读 scene）
        try:
            from bilibot.services.token_usage import usage_context
            _usage_cm = usage_context(
                scene="memory_embedding",
                account_id=self.account_id or "",
            )
        except Exception:
            _usage_cm = None

        async def _run_embed() -> Any:
            if callable(getattr(provider, "embed_many", None)):
                return await asyncio.wait_for(
                    _resolve(provider.embed_many(values)), timeout=max(0.1, float(timeout))
                )
            if callable(getattr(provider, "get_embeddings", None)):
                return await asyncio.wait_for(
                    _resolve(provider.get_embeddings(values)), timeout=max(0.1, float(timeout))
                )
            if callable(getattr(provider, "embed", None)):
                try:
                    return await asyncio.wait_for(
                        _resolve(provider.embed(values)), timeout=max(0.1, float(timeout))
                    )
                except (TypeError, ValueError):
                    return [
                        await asyncio.wait_for(
                            _resolve(provider.embed(text)), timeout=max(0.1, float(timeout))
                        )
                        for text in values
                    ]
            return [
                await asyncio.wait_for(
                    _resolve(provider.get_embedding(text)), timeout=max(0.1, float(timeout))
                )
                for text in values
            ]

        if _usage_cm is not None:
            with _usage_cm:
                vectors = await _run_embed()
        else:
            vectors = await _run_embed()
        if vectors is None:
            # 未配置侧返回 None → 阻塞 job；已配置路径应上抛而非 None
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
            # 空/None 向量是 API 异常或瞬时故障，必须 ValueError 走 fail/retry，
            # 不得 ProviderNotConfigured（否则 block 成「只进不出」）。
            if vector is None:
                raise ValueError("embedding provider returned an empty vector")
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

    VIDEO_DETAIL_MAX_CHARS = 2000

    @staticmethod
    def _prefer_audiovisual_source_text(sources: Sequence[Any], limit: int = 12000) -> str:
        """Prefer behavior_log / asr / visual over metadata JSON for summarization."""

        preferred_order = (
            "video_detail",
            "behavior_log",
            "asr",
            "subtitle",
            "visual_description",
            "ocr",
            "video_hot_comments",
            "web_reference",
            "video_metadata",
        )
        by_type: dict[str, list[str]] = {}
        for item in sources or ():
            if not isinstance(item, Mapping):
                continue
            source_type = str(item.get("source_type") or "").strip() or "unknown"
            text = str(item.get("full_text") or item.get("text") or "").strip()
            if not text:
                continue
            by_type.setdefault(source_type, []).append(text)
        parts: list[str] = []
        used = 0
        for source_type in preferred_order:
            for text in by_type.get(source_type, ()):
                remaining = max(0, int(limit) - used)
                if remaining <= 0:
                    break
                piece = text if len(text) <= remaining else text[:remaining]
                parts.append(f"[{source_type}]\n{piece}")
                used += len(piece)
            if used >= int(limit):
                break
        # Any leftover source types not listed above.
        for source_type, texts in by_type.items():
            if source_type in preferred_order:
                continue
            for text in texts:
                remaining = max(0, int(limit) - used)
                if remaining <= 0:
                    break
                piece = text if len(text) <= remaining else text[:remaining]
                parts.append(f"[{source_type}]\n{piece}")
                used += len(piece)
        return "\n\n".join(parts)

    @staticmethod
    def heuristic_video_detail(
        *,
        title: str = "",
        owner: str = "",
        behavior_log: str = "",
        max_chars: int = 2000,
    ) -> str:
        """Deterministic \u2264max_chars digest when the chat model is unavailable."""

        budget = max(200, int(max_chars))
        header_bits = []
        if title:
            header_bits.append(f"\u89c6\u9891\u300a{title}\u300b")
        if owner:
            header_bits.append(f"UP\u4e3b {owner}")
        header = "\uff0c".join(header_bits)
        body = " ".join(str(behavior_log or "").split())
        if not body:
            return (header or "\u89c6\u9891\u89c2\u5bdf")[:budget]
        # Head / mid / tail sampling keeps late plot points without dumping JSON.
        if len(body) <= budget - len(header) - 4:
            text = f"{header}\u3002{body}" if header else body
            return text[:budget]
        avail = max(80, budget - len(header) - 20)
        head_n = avail // 2
        mid_n = avail // 4
        tail_n = avail - head_n - mid_n
        mid_start = max(0, (len(body) - mid_n) // 2)
        pieces = [
            body[:head_n].rstrip(),
            body[mid_start : mid_start + mid_n].strip(),
            body[-tail_n:].lstrip(),
        ]
        body_text = " \u2026 ".join(p for p in pieces if p)
        text = f"{header}\u3002{body_text}" if header else body_text
        return text[:budget]

    async def summarize_video_detail(
        self,
        *,
        title: str = "",
        owner: str = "",
        behavior_log: str = "",
        extra_context: str = "",
        max_chars: int = VIDEO_DETAIL_MAX_CHARS,
        allow_heuristic: bool = True,
    ) -> str:
        """Compress a raw audiovisual log into a recall-ready video detail note.

        Target length is ``max_chars`` (default 2000). The result should let a later
        reply answer "what is this video about?" without re-reading the full log.

        When ``allow_heuristic`` is False, model failures return "" so the caller can
        retry or skip the video instead of silently using a low-quality truncation.
        """

        budget = max(400, min(int(max_chars), 2000))
        log = str(behavior_log or "").strip()
        if not log and not extra_context:
            if allow_heuristic:
                return self.heuristic_video_detail(
                    title=title, owner=owner, behavior_log="", max_chars=budget
                )
            return ""
        # Cap model input; long logs still retain head/mid/tail.
        # Real behavior_log p75≈10k / max≈24k — 12k keeps more mid-plot signal.
        log_for_model = log
        input_budget = 12000
        if len(log_for_model) > input_budget:
            log_for_model = self.heuristic_video_detail(
                title="", owner="", behavior_log=log, max_chars=input_budget
            )
        system = (
            "\u4f60\u662f\u89c6\u9891\u5185\u5bb9\u6574\u7406\u5668\u3002\u6839\u636e\u89c6\u542c\u5206\u6790\u65e5\u5fd7\u5199\u4e00\u4efd\u4e2d\u6587\u300c\u89c6\u9891\u8be6\u7ec6\u5185\u5bb9\u300d\u7b14\u8bb0\uff0c"
            "\u4f9b\u65e5\u540e\u56de\u5fc6\u4f7f\u7528\u3002\u53ea\u8f93\u51fa\u6b63\u6587\uff0c\u4e0d\u8981\u6807\u9898\u524d\u7f00\uff0c\u4e0d\u8981 markdown \u4ee3\u7801\u5757\u3002"
        )
        # Python 3.11 不允许 f-string 表达式里出现反斜杠转义；Docker
        # 生产镜像正是 3.11，因此先计算回退文本再插值。
        display_title = title or "\u672a\u77e5"
        display_owner = owner or "\u672a\u77e5"
        prompt = (
            f"\u8bf7\u628a\u4e0b\u9762\u7684\u89c6\u9891\u89c6\u542c\u65e5\u5fd7\u6574\u7406\u6210\u4e0d\u8d85\u8fc7 {budget} \u5b57\u7684\u4e2d\u6587\u8be6\u7ec6\u5185\u5bb9\u3002"
            "\u8981\u6c42\uff1a\n"
            "1) \u8bf4\u660e\u89c6\u9891\u4e3b\u9898\u3001\u5173\u952e\u60c5\u8282/\u77e5\u8bc6\u70b9/\u6b65\u9aa4\u3001\u91cd\u8981\u53f0\u8bcd\u6216\u5b57\u5e55\u3001\u753b\u9762\u91cc\u7684\u5173\u952e\u4fe1\u606f\uff1b\n"
            "2) \u6309\u65f6\u95f4\u987a\u5e8f\u6216\u903b\u8f91\u987a\u5e8f\u7ec4\u7ec7\uff0c\u53ef\u5206\u77ed\u6bb5\u843d\uff1b\n"
            "3) \u4e0d\u8981\u5199\u6210\u5f39\u5e55\u53e3\u543b\uff0c\u4e0d\u8981\u7f16\u9020\u65e5\u5fd7\u91cc\u6ca1\u6709\u7684\u4fe1\u606f\uff1b\n"
            "4) \u8fd9\u662f Bot \u89c2\u770b/\u5206\u6790\u522b\u4eba\u7684\u89c6\u9891\uff0c\u7981\u6b62\u5199\u300c\u53d1\u5e03\u4e86/\u4e0a\u4f20\u4e86/\u6295\u7a3f\u4e86\u89c6\u9891\u300d\uff1b\n"
            f"5) \u603b\u5b57\u6570\u5fc5\u987b \u2264 {budget}\u3002\n\n"
            f"\u6807\u9898\uff1a{display_title}\n"
            f"UP\u4e3b\uff1a{display_owner}\n"
        )
        if extra_context:
            prompt += f"\n\u8865\u5145\u4e0a\u4e0b\u6587\uff1a\n{str(extra_context)[:1500]}\n"
        prompt += f"\n\u89c6\u542c\u65e5\u5fd7\uff1a\n{log_for_model}"
        try:
            raw = await self.generate(
                prompt,
                system_prompt=system,
                max_tokens=min(1400, max(400, budget // 1 + 200)),
                temperature=0.1,
                timeout=120.0,
            )
        except Exception:
            if allow_heuristic:
                return self.heuristic_video_detail(
                    title=title, owner=owner, behavior_log=log, max_chars=budget
                )
            return ""
        # Preserve paragraph structure for recall readability; only collapse
        # runs of spaces/tabs and trim empty lines \u2014 do NOT flatten newlines.
        raw_text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for line in raw_text.split("\n"):
            cleaned = " ".join(line.split())
            if cleaned:
                lines.append(cleaned)
        text = "\n".join(lines).strip()
        if not text:
            if allow_heuristic:
                return self.heuristic_video_detail(
                    title=title, owner=owner, behavior_log=log, max_chars=budget
                )
            return ""
        # Soft re-cap; keep sentence/paragraph boundary when possible.
        if len(text) > budget:
            cut = text[:budget]
            for sep in ("\u3002", "\uff01", "\uff1f", "\n", "\uff1b", " "):
                pos = cut.rfind(sep)
                if pos >= int(budget * 0.7):
                    cut = cut[: pos + (0 if sep == " " else 1)]
                    break
            text = cut.rstrip()
        # Reject model outputs that just dump the raw structured log.
        if self.looks_like_heuristic_video_detail(text):
            if allow_heuristic:
                return self.heuristic_video_detail(
                    title=title, owner=owner, behavior_log=log, max_chars=budget
                )
            return ""
        return text

    @staticmethod
    def looks_like_heuristic_video_detail(text: str) -> bool:
        """Detect raw log slices that are not a real natural-language digest."""
        sample = str(text or "")
        if not sample:
            return True
        if "### \u89c6\u9891\u7ed3\u6784\u5316\u884c\u4e3a\u65e5\u5fd7" in sample:
            return True
        if sample.count("\u3010\u542c\u5230\u58f0\u97f3\u3011") >= 4:
            return True
        if sample.count("\u3010\u770b\u5230\u753b\u9762\u3011") >= 4 and "\u4e3b\u9898" not in sample[:120]:
            return True
        # Dense timestamp / bracket markers \u21d2 still a structured log dump.
        ts_hits = len(re.findall(r"\d{1,2}:\d{2}(?::\d{2})?", sample))
        bracket_hits = sample.count("\u3010")
        if ts_hits >= 8 and bracket_hits >= 4:
            return True
        if bracket_hits >= 10 and len(sample) > 400:
            # High density of \u3010\u2026\u3011 markers without prose framing.
            return True
        return False

    async def summarize_event(self, event: Mapping[str, Any]) -> str:
        sources = event.get("sources") or []
        event_type = str(event.get("event_type") or "")
        source_type = str(event.get("source_type") or "")
        is_video_watch = (
            event_type in {"video_observation", "video_metadata_observation", "bangumi_episode"}
            or source_type in {"video", "video_metadata"}
        )
        # Video watches: produce a \u22642000-char detailed content note that recall
        # can inject as the primary evidence instead of raw metadata/log slices.
        if is_video_watch:
            behavior_parts = []
            for item in sources:
                if not isinstance(item, Mapping):
                    continue
                st = str(item.get("source_type") or "")
                text = str(item.get("full_text") or "").strip()
                if not text:
                    continue
                if st in {"behavior_log", "asr", "subtitle", "visual_description", "video_detail"}:
                    behavior_parts.append(text)
            behavior_log = "\n\n".join(behavior_parts)
            # Prefer existing dedicated video_detail source if present.
            for item in sources:
                if isinstance(item, Mapping) and item.get("source_type") == "video_detail":
                    existing = str(item.get("full_text") or "").strip()
                    if existing:
                        return self._sanitize_event_summary(event, existing[: self.VIDEO_DETAIL_MAX_CHARS])
            metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
            detail = await self.summarize_video_detail(
                title=str(event.get("title") or ""),
                owner=str((metadata or {}).get("owner") or ""),
                behavior_log=behavior_log or self._prefer_audiovisual_source_text(sources),
                max_chars=self.VIDEO_DETAIL_MAX_CHARS,
            )
            return self._sanitize_event_summary(event, detail)

        source = self._prefer_audiovisual_source_text(sources) or "\n\n".join(
            str(item.get("full_text") or "") for item in sources if isinstance(item, Mapping)
        )
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
