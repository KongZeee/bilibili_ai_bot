"""
LLM 提供商 — 封装单个 OpenAI 兼容 API 调用

每个 LLMProvider 实例对应一个 LLM 配置（api_key/api_keys/base_url/model），
支持文本生成、流式生成、Vision、Embedding；同一 Provider 可挂多 API Key 并行分发。
"""
import base64
import asyncio
import hashlib
import inspect
import logging
import math
import re
import json
import threading
import time
import weakref
from contextlib import asynccontextmanager
from typing import Optional, List, Any, Tuple, Sequence, Callable, Dict

# 可选依赖：openai 未安装或环境异常时仍允许模块被导入
try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover  (ImportError / SystemError / pydantic 冲突等)
    AsyncOpenAI = None  # type: ignore[assignment]

logger = logging.getLogger("bilibot.llm")

DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 30


def normalize_api_keys(config: Optional[dict]) -> List[str]:
    """Merge api_key + api_keys into a de-duplicated ordered list."""
    if not isinstance(config, dict):
        return []
    keys: List[str] = []
    raw_keys = config.get("api_keys")
    if isinstance(raw_keys, str):
        raw_keys = re.split(r"[\n,]+", raw_keys)
    if isinstance(raw_keys, list):
        for item in raw_keys:
            if isinstance(item, str) and item.strip():
                keys.append(item.strip())
    primary = config.get("api_key")
    if isinstance(primary, str) and primary.strip():
        keys.append(primary.strip())
    seen = set()
    ordered: List[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


class CompletionConcurrencyGate:
    """Loop-local concurrency gate shared by providers using one API quota."""

    def __init__(self, max_concurrency: int = 2):
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphores = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._lock:
            semaphore = self._semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(self.max_concurrency)
                self._semaphores[loop] = semaphore
            return semaphore

    @asynccontextmanager
    async def slot(self):
        async with self._semaphore():
            yield


def completion_endpoint_identity(base_url: str, api_key: str) -> Optional[Tuple[str, bytes]]:
    """Return a non-reversible quota identity without retaining another key copy."""

    if not base_url or not api_key:
        return None
    digest = hashlib.sha256(api_key.encode("utf-8")).digest()
    return (LLMProvider._normalize_base_url(base_url).lower(), digest)


@asynccontextmanager
async def _optional_gate(gate: Optional[CompletionConcurrencyGate]):
    if gate is None:
        yield
        return
    async with gate.slot():
        yield


class _KeySlot:
    """Runtime state for one API key in a pool."""

    __slots__ = ("api_key", "client", "gate", "inflight", "cooldown_until", "disabled")

    def __init__(self, api_key: str, client: Any = None):
        self.api_key = api_key
        self.client = client
        self.gate: Optional[CompletionConcurrencyGate] = None
        self.inflight = 0
        self.cooldown_until = 0.0
        self.disabled = False


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Prefer structured HTTP status; fall back to body/message tokens carefully.

    Avoid bare substring matches like ``tpm``/``rpm`` alone — those appear in
    normal config/error text and caused false cooldown storms.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 429:
        return True
    # OpenAI-compatible error objects may nest response.status_code
    resp = getattr(exc, "response", None)
    resp_status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    try:
        if int(resp_status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    # Strong phrases only — no bare "tpm"/"rpm"/"429" digit-in-config matches
    strong = (
        "rate limit",
        "rate_limit",
        "ratelimit",
        "too many requests",
        "quota exceeded",
        "exceeded your current quota",
        "requests per minute",
        "tokens per minute",
        "http 429",
        "status code 429",
        "error code: 429",
        "\"code\":429",
        "'code':429",
    )
    return any(token in text for token in strong)


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status in (401, 403):
        return True
    resp = getattr(exc, "response", None)
    resp_status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    try:
        if int(resp_status) in (401, 403):
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "incorrect api key",
            "authentication failed",
            "authentication error",
        )
    )


class ApiKeyPool:
    """Least-inflight key selector with 429 cooldown and auth disable."""

    def __init__(
        self,
        slots: List[_KeySlot],
        *,
        cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        name: str = "",
    ):
        self.slots = slots
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.name = name
        self._rr = 0
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        return len(self.slots)

    def primary(self) -> Optional[_KeySlot]:
        return self.slots[0] if self.slots else None

    def set_cooldown_seconds(self, seconds: float) -> None:
        self.cooldown_seconds = max(1.0, float(seconds))

    def set_gates(self, resolve_gate: Callable[[str, str], Optional[CompletionConcurrencyGate]], base_url: str) -> None:
        for slot in self.slots:
            slot.gate = resolve_gate(base_url, slot.api_key)

    def earliest_ready_in(self) -> float:
        """Seconds until any non-disabled key leaves cooldown (0 if one is ready)."""
        if not self.slots:
            return 0.0
        now = time.monotonic()
        waits: List[float] = []
        for s in self.slots:
            if s.disabled:
                continue
            waits.append(max(0.0, float(s.cooldown_until) - now))
        if not waits:
            return 0.0
        return min(waits)

    async def acquire(self) -> Optional[_KeySlot]:
        if not self.slots:
            return None
        async with self._lock:
            now = time.monotonic()
            # Only select keys that are not disabled and not cooling.
            # Never hand out a still-cooling key — that turns 429 into a tight retry storm.
            ready = [
                s for s in self.slots
                if not s.disabled and s.cooldown_until <= now
            ]
            if not ready:
                return None
            ready.sort(key=lambda s: (s.inflight, self.slots.index(s)))
            # round-robin among least-inflight ties
            min_inflight = ready[0].inflight
            ties = [s for s in ready if s.inflight == min_inflight]
            idx = self._rr % len(ties)
            self._rr += 1
            slot = ties[idx]
            slot.inflight += 1
            return slot

    async def release(
        self,
        slot: Optional[_KeySlot],
        *,
        rate_limited: bool = False,
        auth_failed: bool = False,
    ) -> None:
        if slot is None:
            return
        async with self._lock:
            slot.inflight = max(0, slot.inflight - 1)
            if auth_failed:
                slot.disabled = True
                logger.warning(f"[{self.name}] API key disabled after auth failure")
            elif rate_limited:
                slot.cooldown_until = time.monotonic() + self.cooldown_seconds
                logger.warning(
                    f"[{self.name}] API key cooling down {self.cooldown_seconds:g}s after rate limit"
                )

    @asynccontextmanager
    async def checkout(self):
        slot = await self.acquire()
        if slot is None:
            yield None
            return
        rate_limited = False
        auth_failed = False
        try:
            async with _optional_gate(slot.gate):
                yield slot
        except Exception as exc:
            rate_limited = _is_rate_limit_error(exc)
            auth_failed = _is_auth_error(exc)
            raise
        finally:
            await self.release(slot, rate_limited=rate_limited, auth_failed=auth_failed)


class RateLimitExhaustedError(RuntimeError):
    """All API keys in the pool are cooling down / disabled; no request was sent."""

    def __init__(
        self,
        message: str = "all API keys are rate-limited or unavailable",
        *,
        retry_after: float = 0.0,
        pool_name: str = "",
    ):
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.pool_name = pool_name or ""
        super().__init__(message)


class ASRResponseError(ValueError):
    """An HTTP-successful ASR response that cannot yield a transcript."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


def _asr_field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _asr_text(value: Any) -> str:
    """Read text from OpenAI objects and common compatible dict payloads."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(filter(None, (_asr_text(part) for part in value))).strip()
    if value is None:
        return ""
    for name in ("transcript", "text", "content"):
        nested = _asr_field(value, name)
        if nested is value:
            continue
        text = _asr_text(nested)
        if text:
            return text
    return ""


def asr_response_signals_no_speech(response: Any) -> bool:
    """Return True only for an explicit provider no-speech/silence marker."""

    def _marked(value: Any) -> bool:
        if value is True:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {
                "no_speech",
                "no-speech",
                "silence",
                "silent",
            }
        return False

    choices = _asr_field(response, "choices") or []
    try:
        choice = choices[0] if choices else None
    except (KeyError, IndexError, TypeError):
        choice = None
    message = _asr_field(choice, "message")
    audio = _asr_field(message, "audio")
    containers = [
        response,
        _asr_field(response, "metadata"),
        choice,
        message,
        _asr_field(message, "metadata"),
        audio,
    ]
    for container in containers:
        for name in ("no_speech", "silence", "status", "finish_reason"):
            if _marked(_asr_field(container, name)):
                return True
    return False


def extract_asr_transcript(response: Any) -> str:
    """Extract an ASR transcript or raise a stable, diagnosable error."""
    if response is None:
        raise ASRResponseError("ASR_RESPONSE_MISSING", "API returned no response object")

    choices = _asr_field(response, "choices")
    if not choices:
        raise ASRResponseError("ASR_EMPTY_CHOICES", "API returned no completion choices")

    try:
        first_choice = choices[0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ASRResponseError(
            "ASR_INVALID_CHOICES", "completion choices are not an indexable sequence"
        ) from exc

    message = _asr_field(first_choice, "message")
    if message is None:
        raise ASRResponseError("ASR_MISSING_MESSAGE", "first choice has no message")

    text = _asr_text(_asr_field(message, "content"))
    if not text:
        # Xiaomi MiMo responses normally contain transcript text in content and
        # explicitly return audio=null. Other compatible APIs may put it here.
        text = _asr_text(_asr_field(message, "audio"))
    if not text:
        if asr_response_signals_no_speech(response):
            return ""
        raise ASRResponseError(
            "ASR_EMPTY_TRANSCRIPT",
            "first choice message contains no transcript in content or audio",
        )
    return text


class LLMProvider:
    """单个 LLM 提供商 — 封装 OpenAI 兼容 API"""

    # base_url 末尾冗余路径后缀（OpenAI SDK 会自动追加）
    _URL_SUFFIX_TRIM = (
        "/embeddings",
        "/embedding",
        "/rerank",
        "/audio/transcriptions",
        "/audio",
        "/images/generations",
        "/images",
        "/chat/completions",
        "/chat",
    )

    @classmethod
    def _normalize_base_url(cls, url: str) -> str:
        """去除 base_url 末尾冗余路径后缀，避免 SDK 重复拼接"""
        if not url:
            return url
        url = url.rstrip("/")
        changed = True
        while changed:
            changed = False
            for suf in cls._URL_SUFFIX_TRIM:
                if url.endswith(suf):
                    url = url[: -len(suf)].rstrip("/")
                    changed = True
                    break
        return url

    def __init__(self, llm_id: str, config: dict):
        self.llm_id = llm_id
        self._config = dict(config or {})
        self.api_keys: List[str] = normalize_api_keys(config)
        self.api_key: str = self.api_keys[0] if self.api_keys else (config.get("api_key", "") or "")
        self.base_url: str = self._normalize_base_url(config.get("base_url", "https://api.siliconflow.cn/v1"))
        self.model: str = config.get("model", "Qwen/Qwen2.5-72B-Instruct")
        self.max_tokens: int = config.get("max_tokens", 1024)
        self.temperature: float = config.get("temperature", 0.8)
        self.name: str = config.get("name", llm_id)
        self.enabled: bool = config.get("enabled", True)
        self.max_retries: int = max(0, int(config.get("max_retries", 2)))
        try:
            self.rate_limit_cooldown_seconds: float = float(
                config.get("rate_limit_cooldown_seconds", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)
            )
        except (TypeError, ValueError):
            self.rate_limit_cooldown_seconds = float(DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)
        self.rate_limit_cooldown_seconds = max(1.0, self.rate_limit_cooldown_seconds)

        # 文生图扩展字段（仅 image 类型 Provider 使用）
        self.default_size: str = config.get("default_size", "1024x768")
        self.timeout: int = int(config.get("timeout", 120))

        # Vision 配置（可选）
        # BUG 修复：迁移路径（B-003 子段包装）只写入 model/api_key/base_url，
        # 经常遗漏 enabled 字段，导致 vision_enabled 恒为 False、客户端不初始化。
        # 改为「有 model 且有 api_key（可回退到主 api_key）即视为启用」，向后兼容。
        vision = config.get("vision") or {}
        vision_keys = normalize_api_keys(vision) or list(self.api_keys)
        _vision_key = vision_keys[0] if vision_keys else (vision.get("api_key") or self.api_key)
        self.vision_enabled: bool = bool(vision.get("enabled", False)) or bool(
            vision.get("model") and _vision_key
        )
        self.vision_api_keys: List[str] = vision_keys if self.vision_enabled else []
        self.vision_api_key: str = _vision_key
        self.vision_base_url: str = self._normalize_base_url(vision.get("base_url") or self.base_url)
        self.vision_model: str = vision.get("model", "")

        # Embedding 配置（可选）——同样修复遗漏 enabled 的问题
        embedding = config.get("embedding") or {}
        emb_keys = normalize_api_keys(embedding) or list(self.api_keys)
        _emb_key = emb_keys[0] if emb_keys else (embedding.get("api_key") or self.api_key)
        self.embedding_enabled: bool = bool(embedding.get("enabled", False)) or bool(
            embedding.get("model") and _emb_key
        )
        self.embedding_api_keys: List[str] = emb_keys if self.embedding_enabled else []
        self.embedding_api_key: str = _emb_key
        self.embedding_base_url: str = self._normalize_base_url(embedding.get("base_url") or self.base_url)
        self.embedding_model: str = embedding.get("model", "BAAI/bge-m3")

        # Rerank 429 冷却（独立小表，避免同 key 风暴）
        self._rerank_key_cooldown_until: Dict[str, float] = {}

        # OpenAI 客户端 / 密钥池（客户端按 key 懒创建，避免多实例初始化卡死）
        self._chat_completion_gate: Optional[CompletionConcurrencyGate] = None
        self._vision_completion_gate: Optional[CompletionConcurrencyGate] = None
        self._client_lock = threading.Lock()
        self._chat_pool = ApiKeyPool(
            [_KeySlot(key) for key in self.api_keys] if AsyncOpenAI is not None else [],
            cooldown_seconds=self.rate_limit_cooldown_seconds,
            name=f"{llm_id}:chat",
        )
        self._vision_pool = ApiKeyPool(
            [_KeySlot(key) for key in self.vision_api_keys]
            if (AsyncOpenAI is not None and self.vision_enabled and self.vision_api_keys)
            else [],
            cooldown_seconds=self.rate_limit_cooldown_seconds,
            name=f"{llm_id}:vision",
        )
        self._embedding_pool = ApiKeyPool(
            [_KeySlot(key) for key in self.embedding_api_keys]
            if (AsyncOpenAI is not None and self.embedding_enabled and self.embedding_api_keys)
            else [],
            cooldown_seconds=self.rate_limit_cooldown_seconds,
            name=f"{llm_id}:embedding",
        )

        if AsyncOpenAI is None:
            logger.warning(f"[{llm_id}] openai 库未安装，跳过客户端初始化")
            return

        if self.api_keys:
            logger.info(
                f"[{llm_id}] LLM 密钥池已就绪: {self.model} "
                f"(keys={len(self.api_keys)}, timeout={self.timeout}s)"
            )
        if self._vision_pool.slots:
            logger.info(f"[{llm_id}] Vision 密钥池已就绪 (keys={len(self.vision_api_keys)})")
        if self._embedding_pool.slots:
            logger.info(
                f"[{llm_id}] Embedding 密钥池已就绪 "
                f"(keys={len(self.embedding_api_keys)}, timeout={self.timeout}s)"
            )

    def _ensure_client(self, slot: _KeySlot, base_url: str):
        if slot.client is not None:
            return slot.client
        if AsyncOpenAI is None:
            return None
        with self._client_lock:
            if slot.client is None:
                slot.client = AsyncOpenAI(
                    api_key=slot.api_key,
                    base_url=base_url,
                    timeout=self.timeout,
                    max_retries=self.max_retries,
                )
            return slot.client

    async def aclose(self) -> None:
        """Close all lazy AsyncOpenAI clients (chat/vision/embedding pools).

        Call on provider replace/remove and app shutdown to avoid FD/socket leaks.
        """
        pools = (self._chat_pool, self._vision_pool, self._embedding_pool)
        clients = []
        with self._client_lock:
            for pool in pools:
                for slot in getattr(pool, "slots", []) or []:
                    client = getattr(slot, "client", None)
                    if client is not None:
                        clients.append(client)
                        slot.client = None
        for client in clients:
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.warning(f"[{self.llm_id}] close LLM client failed: {e}")

    def _primary_client(self, pool: ApiKeyPool, base_url: str):
        slot = pool.primary()
        if slot is None:
            return None
        return self._ensure_client(slot, base_url)

    @property
    def client(self):
        return self._primary_client(self._chat_pool, self.base_url)

    @client.setter
    def client(self, value):
        # Allow __init__ and tests to assign None / mock
        if value is None:
            return
        slot = self._chat_pool.primary()
        if slot is not None:
            slot.client = value

    @property
    def vision_client(self):
        if self._vision_pool.slots:
            return self._primary_client(self._vision_pool, self.vision_base_url)
        return self.client if self.vision_enabled else None

    @vision_client.setter
    def vision_client(self, value):
        if value is None:
            return
        slot = self._vision_pool.primary()
        if slot is not None:
            slot.client = value

    @property
    def embedding_client(self):
        if self._embedding_pool.slots:
            return self._primary_client(self._embedding_pool, self.embedding_base_url)
        return self.client if self.embedding_enabled else None

    @embedding_client.setter
    def embedding_client(self, value):
        if value is None:
            return
        slot = self._embedding_pool.primary()
        if slot is not None:
            slot.client = value

    def set_rate_limit_cooldown(self, seconds: float) -> None:
        try:
            value = max(1.0, float(seconds))
        except (TypeError, ValueError):
            value = float(DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)
        self.rate_limit_cooldown_seconds = value
        self._chat_pool.set_cooldown_seconds(value)
        self._vision_pool.set_cooldown_seconds(value)
        self._embedding_pool.set_cooldown_seconds(value)

    def set_completion_gates(
        self,
        *,
        chat: Optional[CompletionConcurrencyGate] = None,
        vision: Optional[CompletionConcurrencyGate] = None,
        resolve_gate: Optional[Callable[[str, str], Optional[CompletionConcurrencyGate]]] = None,
    ) -> None:
        """Assign concurrency gates.

        Prefer resolve_gate(base_url, api_key) so each key gets its own quota gate.
        Legacy single chat/vision gates remain supported for callers that still pass them.
        """
        self._chat_completion_gate = chat
        self._vision_completion_gate = vision
        if resolve_gate is not None:
            self._chat_pool.set_gates(resolve_gate, self.base_url)
            if self.vision_enabled:
                self._vision_pool.set_gates(resolve_gate, self.vision_base_url)
            if self.embedding_enabled:
                self._embedding_pool.set_gates(resolve_gate, self.embedding_base_url)
        else:
            for slot in self._chat_pool.slots:
                slot.gate = chat
            for slot in self._vision_pool.slots:
                slot.gate = vision

    @property
    def api_key_count(self) -> int:
        return max(len(self.api_keys), 1 if self.api_key else 0)

    @property
    def vision_api_key_count(self) -> int:
        if self.vision_api_keys:
            return len(self.vision_api_keys)
        return 1 if self.vision_api_key else 0

    def _raise_pool_exhausted(self, pool: ApiKeyPool) -> None:
        retry_after = pool.earliest_ready_in()
        name = pool.name or self.llm_id
        raise RateLimitExhaustedError(
            f"[{name}] all API keys cooling or disabled"
            + (f"; retry_after≈{retry_after:.1f}s" if retry_after > 0 else ""),
            retry_after=retry_after,
            pool_name=name,
        )

    async def _chat_with_pool(self, call):
        """Run call(client) with key pool + limited 429 failover."""
        if not self._chat_pool.slots:
            return None

        attempts = max(1, min(len(self._chat_pool.slots), 4))
        last_exc: Optional[BaseException] = None
        for _ in range(attempts):
            async with self._chat_pool.checkout() as slot:
                if slot is None:
                    break
                client = self._ensure_client(slot, self.base_url)
                if client is None:
                    break
                try:
                    return await call(client)
                except Exception as exc:
                    last_exc = exc
                    if _is_rate_limit_error(exc) and attempts > 1:
                        continue
                    raise
        if last_exc is not None:
            raise last_exc
        # All keys cooling/disabled with no request exception — do not silent-None.
        self._raise_pool_exhausted(self._chat_pool)

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """生成文本回复"""
        if not self._chat_pool.slots:
            logger.warning(f"[{self.llm_id}] LLM 客户端未初始化")
            return None

        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            async def _call(client, budget: int):
                return await client.chat.completions.create(
                    model=model or self.model,
                    messages=messages,
                    max_tokens=budget,
                    temperature=temperature if temperature is not None else self.temperature,
                )

            requested_budget = max_tokens or self.max_tokens
            response = await self._chat_with_pool(lambda client: _call(client, requested_budget))

            def _extract(resp):
                if resp and resp.choices:
                    message = resp.choices[0].message
                    result = message.content
                    # Reasoning-style models may return the answer only in
                    # reasoning_content when the API does not populate content.
                    if not result and getattr(resp.choices[0], "finish_reason", "stop") != "length":
                        result = getattr(message, "reasoning_content", None)
                    if result:
                        return str(result).strip()
                return None

            result = _extract(response)
            # Reasoning models can spend the whole budget on reasoning tokens
            # and leave content empty. Small classification callers (80-120
            # tokens), medium JSON callers (linker/entities use 2048) and the
            # most stubborn link prompts (verified up to 16k reasoning tokens)
            # all hit this in production. Retry with an escalating budget
            # instead of returning None — which the memory worker would treat
            # as a job failure and eventually dead-letter.
            finish = getattr(response.choices[0], "finish_reason", "stop") if response and response.choices else ""
            reasoning = getattr(response.choices[0].message, "reasoning_content", None) if response and response.choices else None
            # Small callers get one intermediate step; medium/large JSON callers
            # (linker/entities at 2048) skip straight to 16k. Three escalating
            # calls take 100s+ on a reasoning endpoint and outrun the caller's
            # asyncio timeout even though the final answer is reachable.
            if requested_budget < 1024:
                ladder = (max(1024, requested_budget * 2), 16384)
            else:
                ladder = (16384,)
            previous_budget = requested_budget
            for retry_budget in ladder:
                if retry_budget <= previous_budget:
                    continue
                if result is not None or finish != "length":
                    break
                if not reasoning and requested_budget >= 512:
                    break
                response = await self._chat_with_pool(
                    lambda client: _call(client, retry_budget)
                )
                previous_budget = retry_budget
                result = _extract(response)
                finish = getattr(response.choices[0], "finish_reason", "stop") if response and response.choices else ""
                reasoning = getattr(response.choices[0].message, "reasoning_content", None) if response and response.choices else None

            try:
                from bilibot.services.token_usage import record_response_safe
                record_response_safe(
                    response,
                    provider_id=self.llm_id,
                    model=model or self.model,
                    kind="chat",
                    scene="",
                )
            except Exception:
                pass
            if result:
                logger.debug(f"[{self.llm_id}] LLM 生成成功: {len(result)} 字符")
                return result
            return None

        except Exception as e:
            logger.error(f"[{self.llm_id}] LLM 生成失败: {e}")
            raise

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ):
        """流式生成（异步生成器）"""
        if not self._chat_pool.slots:
            return

        stream_completed = False
        usage = None
        chars = 0
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            # Streaming holds one connection; pick one key without mid-stream switch.
            async with self._chat_pool.checkout() as slot:
                if slot is None:
                    self._raise_pool_exhausted(self._chat_pool)
                client = self._ensure_client(slot, self.base_url)
                if client is None:
                    self._raise_pool_exhausted(self._chat_pool)
                stream = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=temperature if temperature is not None else self.temperature,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        chars += len(content)
                        yield content
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
            stream_completed = True
        except Exception as e:
            logger.error(f"[{self.llm_id}] LLM 流式生成失败: {e}")
            raise
        finally:
            # 仅在完整消费成功后记账；usage 不可得时至少记一次调用
            if stream_completed:
                try:
                    from bilibot.services.token_usage import record_usage_safe

                    record_usage_safe(
                        provider_id=self.llm_id,
                        model=self.model,
                        kind="chat",
                        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                        completion_tokens=int(
                            getattr(usage, "completion_tokens", 0) or 0
                        ),
                        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
                        meta={"stream": True, "chars": chars},
                    )
                except Exception:
                    pass

    async def vision_analyze(
        self,
        image_url: str,
        prompt: str,
        max_tokens: int = 250,
    ) -> Optional[str]:
        """Vision 模型分析图片"""
        # V3 vision providers store model at top-level; nested vision keeps vision_model.
        vision_model = self.vision_model or self.model
        use_pool = self._vision_pool.slots or self._chat_pool.slots
        if not use_pool:
            logger.warning(f"[{self.llm_id}] Vision 客户端未初始化")
            return None
        if not vision_model:
            logger.warning(f"[{self.llm_id}] Vision 模型未配置")
            return None

        try:
            content = [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ]
            pool = self._vision_pool if self._vision_pool.slots else self._chat_pool
            base = self.vision_base_url if self._vision_pool.slots else self.base_url
            attempts = max(1, min(pool.key_count or 1, 4))
            last_exc: Optional[BaseException] = None
            requested_budget = max(1, int(max_tokens or 250))

            def _extract(resp):
                if resp and resp.choices:
                    message = resp.choices[0].message
                    result = getattr(message, "content", None)
                    # Reasoning-style vision models (e.g. qwen-vl routes behind
                    # agnes) can return the answer only in reasoning_content
                    # with an empty content — identical to generate().
                    if not result and getattr(resp.choices[0], "finish_reason", "stop") != "length":
                        result = getattr(message, "reasoning_content", None)
                    if result:
                        return str(result).strip()
                return None

            for _ in range(attempts):
                async with pool.checkout() as slot:
                    if slot is None:
                        break
                    client = self._ensure_client(slot, base)
                    if client is None:
                        break
                    try:
                        response = await client.chat.completions.create(
                            model=vision_model,
                            messages=[{"role": "user", "content": content}],
                            max_tokens=requested_budget,
                        )
                        result = _extract(response)
                        # A small vision budget can be spent entirely on
                        # reasoning tokens, leaving an empty content. Retry once
                        # with a comfortable budget instead of dropping the frame.
                        if result is None and requested_budget < 512:
                            finish = getattr(response.choices[0], "finish_reason", "stop") if response and response.choices else ""
                            reasoning = getattr(response.choices[0].message, "reasoning_content", None) if response and response.choices else None
                            if finish == "length" and reasoning:
                                response = await client.chat.completions.create(
                                    model=vision_model,
                                    messages=[{"role": "user", "content": content}],
                                    max_tokens=512,
                                )
                                result = _extract(response)
                        try:
                            from bilibot.services.token_usage import record_response_safe
                            record_response_safe(
                                response,
                                provider_id=self.llm_id,
                                model=vision_model,
                                kind="vision",
                            )
                        except Exception:
                            pass
                        return result
                    except Exception as exc:
                        last_exc = exc
                        if _is_rate_limit_error(exc) and attempts > 1:
                            continue
                        raise
            if last_exc is not None:
                raise last_exc
            self._raise_pool_exhausted(pool)

        except Exception as e:
            # 与 generate() 一致：网络/429/5xx 必须向上抛，供调用方分类重试。
            # 吞掉异常会把临时故障伪装成「空描述」，导致视频理解静默丢帧。
            logger.error(f"[{self.llm_id}] Vision 分析失败: {e}")
            raise

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """获取文本的 embedding 向量"""
        emb_model = self.embedding_model or self.model
        pool = self._embedding_pool if self._embedding_pool.slots else self._chat_pool
        if not pool.slots and not (self.embedding_client or self.client):
            logger.warning(f"[{self.llm_id}] Embedding 客户端未初始化")
            return None
        try:
            async def _call(client):
                return await client.embeddings.create(model=emb_model, input=text)

            if not pool.slots:
                return None
            base = self.embedding_base_url if self._embedding_pool.slots else self.base_url
            attempts = max(1, min(pool.key_count, 4))
            last_exc: Optional[BaseException] = None
            for _ in range(attempts):
                async with pool.checkout() as slot:
                    if slot is None:
                        break
                    client = self._ensure_client(slot, base)
                    if client is None:
                        break
                    try:
                        response = await _call(client)
                        try:
                            from bilibot.services.token_usage import record_response_safe
                            # embeddings usage often only has total/prompt
                            record_response_safe(
                                response,
                                provider_id=self.llm_id,
                                model=emb_model,
                                kind="embedding",
                            )
                        except Exception:
                            pass
                        if response.data:
                            return response.data[0].embedding
                        # 已配置但空 data：上抛，避免记忆 job 当 unconfigured 永久跳过
                        raise RuntimeError(
                            f"[{self.llm_id}] embedding API returned empty data"
                        )
                    except Exception as exc:
                        last_exc = exc
                        if _is_rate_limit_error(exc) and attempts > 1:
                            continue
                        raise
            if last_exc is not None:
                raise last_exc
            self._raise_pool_exhausted(pool)
        except Exception as e:
            # 与 generate() 一致：临时故障上抛，避免记忆索引把 429/超时当「无向量」永久跳过
            logger.error(f"[{self.llm_id}] Embedding 获取失败: {e}")
            raise

    async def get_embeddings(
        self, texts: Sequence[str]
    ) -> Optional[List[List[float]]]:
        """Embed a batch in one request while preserving input order."""
        values = [str(text) for text in texts]
        if not values:
            return []
        emb_model = self.embedding_model or self.model
        pool = self._embedding_pool if self._embedding_pool.slots else self._chat_pool
        if not pool.slots and not (self.embedding_client or self.client):
            logger.warning(f"[{self.llm_id}] Embedding 客户端未初始化")
            return None

        try:
            async def _once(client):
                response = await client.embeddings.create(model=emb_model, input=values)
                try:
                    from bilibot.services.token_usage import record_response_safe
                    record_response_safe(
                        response,
                        provider_id=self.llm_id,
                        model=emb_model,
                        kind="embedding",
                        meta={"batch_size": len(values)},
                    )
                except Exception:
                    pass
                data = list(response.data or ())
                if len(data) != len(values):
                    raise RuntimeError(
                        f"[{self.llm_id}] embedding batch size mismatch: "
                        f"got {len(data)} for {len(values)} inputs"
                    )
                ordered: List[Optional[List[float]]] = [None] * len(values)
                for position, item in enumerate(data):
                    index = getattr(item, "index", position)
                    if index is None:
                        index = position
                    if not isinstance(index, int) or not 0 <= index < len(values):
                        raise RuntimeError(
                            f"[{self.llm_id}] embedding batch invalid index: {index}"
                        )
                    if ordered[index] is not None:
                        raise RuntimeError(
                            f"[{self.llm_id}] embedding batch duplicate index: {index}"
                        )
                    ordered[index] = list(item.embedding)
                if any(vector is None for vector in ordered):
                    raise RuntimeError(
                        f"[{self.llm_id}] embedding batch missing vectors"
                    )
                return [vector for vector in ordered if vector is not None]

            if not pool.slots:
                return None
            base = self.embedding_base_url if self._embedding_pool.slots else self.base_url
            attempts = max(1, min(pool.key_count, 4))
            last_exc: Optional[BaseException] = None
            for _ in range(attempts):
                async with pool.checkout() as slot:
                    if slot is None:
                        break
                    client = self._ensure_client(slot, base)
                    if client is None:
                        break
                    try:
                        return await _once(client)
                    except Exception as exc:
                        last_exc = exc
                        if _is_rate_limit_error(exc) and attempts > 1:
                            continue
                        raise
            if last_exc is not None:
                raise last_exc
            self._raise_pool_exhausted(pool)

        except Exception as e:
            logger.error(f"[{self.llm_id}] Embedding 批量获取失败: {e}")
            raise

    async def test(self) -> Tuple[bool, str]:
        """测试对话连接，返回 (是否成功, 错误信息)"""
        if not self._chat_pool.slots:
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        try:
            result = await self.generate("你好", max_tokens=200)
            if result is not None:
                return True, ""
            return False, "API 返回空响应"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def test_vision(self) -> Tuple[bool, str]:
        """测试视觉模型连接（用 1x1 测试图）"""
        vision_model = self.vision_model or self.model
        if not (self._vision_pool.slots or self._chat_pool.slots):
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        if not vision_model:
            return False, "未配置视觉模型名"
        try:
            # 用 1x1 红色 PNG base64 测试
            test_img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
            result = await self.vision_analyze(test_img, "这是什么颜色？", max_tokens=20)
            if result is not None:
                return True, ""
            return False, "API 返回空响应"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def test_embedding(self) -> Tuple[bool, str]:
        """测试 Embedding 连接"""
        emb_model = self.embedding_model or self.model
        if not (self._embedding_pool.slots or self._chat_pool.slots):
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        if not emb_model:
            return False, "未配置 Embedding 模型名"
        try:
            vector = await self.get_embedding("test")
            if vector:
                return True, ""
            return False, "API 返回空响应"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def test_asr(self) -> Tuple[bool, str]:
        """测试 ASR 连接（发送 1 秒静音 WAV 验证可达性）

        兼容两类 ASR API：
        - 标准 OpenAI Whisper：/v1/audio/transcriptions 端点
        - 小米 MiMo-V2.5-ASR：走 chat/completions + input_audio 多模态格式
        先尝试标准端点，404 时回退到 chat completions 方式。
        """
        if not self._chat_pool.slots:
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        if not self.model:
            return False, "未配置 ASR 模型名"
        try:
            import io as _io
            import wave

            # 生成 1 秒静音 WAV（8000Hz, 16bit, mono）—— 最小有效音频
            buf = _io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000)
            wav_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            async def _with_client(client):
                # 方式 1：标准 OpenAI /audio/transcriptions（如 Whisper、SenseVoice）
                try:
                    dummy = _io.BytesIO(buf.getvalue())
                    dummy.name = "test.wav"
                    await client.audio.transcriptions.create(
                        model=self.model,
                        file=dummy,
                    )
                    return True, ""
                except Exception as inner:
                    msg = str(inner).lower()
                    # 404 / not found → 端点不存在，回退到 chat completions 方式
                    if not (any(k in msg for k in ("404", "not found", "no route"))):
                        # 鉴权通过、模型存在，只是音频无效 → 连接正常
                        if any(k in msg for k in ("audio", "format", "file", "duration", "empty", "too short", "decoding")):
                            return True, ""
                        # 其他错误（auth/model）→ 真实失败
                        return False, f"{type(inner).__name__}: {inner}"

                # 方式 2：小米 MiMo ASR — chat/completions + input_audio 多模态
                try:
                    response = await client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_audio",
                                        "input_audio": {
                                            "data": f"data:audio/wav;base64,{wav_b64}",
                                        },
                                    }
                                ],
                            }
                        ],
                        max_tokens=20,
                    )
                    try:
                        extract_asr_transcript(response)
                        return True, ""
                    except ASRResponseError as response_error:
                        # The probe audio is silence, so an empty transcript still
                        # proves auth, routing, and model availability.
                        if response_error.code == "ASR_EMPTY_TRANSCRIPT":
                            return True, ""
                        return False, str(response_error)
                except Exception as inner2:
                    msg2 = str(inner2).lower()
                    # 模型不支持、音频相关错误但鉴权通过 → 连接正常
                    if any(k in msg2 for k in ("audio", "format", "unsupported", "multimodal", "tensor", "reshape", "duration")):
                        return True, ""
                    return False, f"{type(inner2).__name__}: {inner2}"

            async with self._chat_pool.checkout() as slot:
                if slot is None:
                    retry_after = self._chat_pool.earliest_ready_in()
                    return (
                        False,
                        "RateLimitExhaustedError: all keys cooling"
                        + (f" (retry_after≈{retry_after:.1f}s)" if retry_after > 0 else ""),
                    )
                client = self._ensure_client(slot, self.base_url)
                if client is None:
                    return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
                return await _with_client(client)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def test_image(self) -> Tuple[bool, str]:
        """测试文生图连接（用最小尺寸请求）"""
        keys = self.api_keys or ([self.api_key] if self.api_key else [])
        if not keys:
            return False, "api_key 未配置"
        if not self.model:
            return False, "未配置文生图模型名"
        try:
            import aiohttp
            last_err = ""
            for key in keys[:4]:
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.model,
                    "prompt": "test",
                    "n": 1,
                    "size": "1024x1024",
                }
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                    async with session.post(
                        f"{self.base_url.rstrip('/')}/images/generations",
                        json=payload,
                    ) as resp:
                        if resp.status == 200:
                            return True, ""
                        text = await resp.text()
                        # 某些 API 会因 "test" prompt 太短返回 400，但鉴权通过
                        if resp.status in (400, 422) and not any(k in text.lower() for k in ("unauthorized", "api_key", "forbidden", "auth")):
                            return True, f"API 可达（prompt 被拒绝: {text[:100]}）"
                        if resp.status == 429:
                            last_err = f"HTTP 429: {text[:200]}"
                            continue
                        last_err = f"HTTP {resp.status}: {text[:200]}"
                        if resp.status in (401, 403):
                            continue
                        return False, last_err
            return False, last_err or "连接失败"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    @staticmethod
    def normalize_rerank_score(score: float) -> float:
        """Map provider relevance_score into [0, 1].

        SiliconFlow / BGE style APIs return 0..1 floats; some gateways emit 0..100.
        """
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("rerank score is not finite")
        if value > 1.0 and value <= 100.0:
            value = value / 100.0
        return max(0.0, min(1.0, value))

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_n: Optional[int] = None,
    ) -> List[dict]:
        """Call SiliconFlow-style POST {base_url}/rerank.

        Returns list of ``{"index": int, "relevance_score": float}`` (scores in 0..1).
        """
        docs = [str(d) for d in documents]
        if not docs:
            return []
        keys = self.api_keys or ([self.api_key] if self.api_key else [])
        if not keys:
            raise RuntimeError(f"[{self.llm_id}] rerank api_key 未配置")
        if not self.model:
            raise RuntimeError(f"[{self.llm_id}] rerank model 未配置")

        n = len(docs) if top_n is None else max(1, min(int(top_n), len(docs)))
        payload = {
            "model": self.model,
            "query": str(query or ""),
            "documents": docs,
            "top_n": n,
            "return_documents": False,
        }
        import aiohttp

        now = time.monotonic()
        ready_keys = [
            key
            for key in keys
            if float(self._rerank_key_cooldown_until.get(key, 0.0) or 0.0) <= now
        ]
        if not ready_keys:
            soonest = min(
                float(self._rerank_key_cooldown_until.get(key, 0.0) or 0.0)
                for key in keys
            )
            wait = max(0.0, soonest - now)
            raise RuntimeError(
                f"[{self.llm_id}] rerank 所有密钥均在 429 冷却中（约 {wait:.0f}s 后可重试）"
            )

        last_err: Optional[BaseException] = None
        for key in ready_keys[:4]:
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=max(5, int(self.timeout or 30)))
            try:
                async with aiohttp.ClientSession(
                    headers=headers, timeout=timeout
                ) as session:
                    async with session.post(
                        f"{self.base_url.rstrip('/')}/rerank",
                        json=payload,
                    ) as resp:
                        text = await resp.text()
                        if resp.status == 429:
                            self._rerank_key_cooldown_until[key] = (
                                time.monotonic() + self.rate_limit_cooldown_seconds
                            )
                            last_err = RuntimeError(f"HTTP 429: {text[:200]}")
                            continue
                        if resp.status in (401, 403):
                            last_err = RuntimeError(
                                f"HTTP {resp.status}: {text[:200]}"
                            )
                            continue
                        if resp.status != 200:
                            raise RuntimeError(
                                f"[{self.llm_id}] rerank HTTP {resp.status}: {text[:300]}"
                            )
                        try:
                            body = json.loads(text) if text else {}
                        except json.JSONDecodeError as exc:
                            raise RuntimeError(
                                f"[{self.llm_id}] rerank invalid JSON: {text[:200]}"
                            ) from exc
                        rows = body.get("results") if isinstance(body, dict) else None
                        if not isinstance(rows, list):
                            raise RuntimeError(
                                f"[{self.llm_id}] rerank response missing results[]"
                            )
                        out: List[dict] = []
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            idx = row.get("index")
                            score = row.get("relevance_score")
                            if not isinstance(idx, int) or not 0 <= idx < len(docs):
                                continue
                            if isinstance(score, bool) or not isinstance(
                                score, (int, float)
                            ):
                                continue
                            try:
                                norm = self.normalize_rerank_score(float(score))
                            except (TypeError, ValueError):
                                continue
                            out.append({"index": idx, "relevance_score": norm})
                        return out
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_err = exc
                continue
        if last_err is not None:
            raise RuntimeError(
                f"[{self.llm_id}] rerank failed: {type(last_err).__name__}: {last_err}"
            ) from last_err
        raise RuntimeError(f"[{self.llm_id}] rerank failed: no usable API key")

    async def test_rerank(self) -> Tuple[bool, str]:
        """Probe dedicated rerank endpoint with a minimal query/document pair."""
        keys = self.api_keys or ([self.api_key] if self.api_key else [])
        if not keys:
            return False, "api_key 未配置"
        if not self.model:
            return False, "未配置 Rerank 模型名"
        try:
            rows = await self.rerank("test query", ["test document about query"], top_n=1)
            if rows:
                return True, ""
            return False, "API 返回空 results"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def get_info(self) -> dict:
        """获取提供商信息（脱敏）"""
        return {
            "id": self.llm_id,
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "enabled": self.enabled,
            "has_api_key": bool(self.api_key or self.api_keys),
            "api_key_count": self.api_key_count,
            "vision_enabled": self.vision_enabled,
            "vision_model": self.vision_model,
            "vision_base_url": self.vision_base_url,
            "has_vision_api_key": bool(self.vision_enabled and (self.vision_api_key or self.vision_api_keys)),
            "vision_api_key_count": self.vision_api_key_count if self.vision_enabled else 0,
            "embedding_enabled": self.embedding_enabled,
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "has_embedding_api_key": bool(self.embedding_enabled and (self.embedding_api_key or self.embedding_api_keys)),
            "default_size": self.default_size,
            "timeout": self.timeout,
        }

    def export_keys_for_config(self) -> dict:
        """Serialize keys for config persistence (plain text, never log)."""
        data = {
            "api_key": self.api_key or "",
            "api_keys": list(self.api_keys),
        }
        return data

    # ══════════════════════════════════════
    #  静态工具方法（兼容旧 LLMAdapter）
    # ══════════════════════════════════════

    @staticmethod
    def repair_json(text: str) -> str:
        """修复 LLM 返回的 JSON（对象或数组）"""
        if not text:
            return ""
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text.strip())
        # Prefer array slice when the payload is list-shaped (entity extraction etc.)
        arr_match = re.search(r'\[.*\]', text, re.DOTALL)
        obj_match = re.search(r'\{.*\}', text, re.DOTALL)
        if arr_match and (not obj_match or arr_match.start() <= obj_match.start()):
            text = arr_match.group()
        elif obj_match:
            text = obj_match.group()
        text = re.sub(r',\s*([}\]])', r'\1', text)
        return text

    @staticmethod
    def parse_json(text: str) -> Optional[Any]:
        """解析 JSON 字符串"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            fixed = LLMProvider.repair_json(text)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError as e:
                logger.error(f"JSON 解析失败: {e}, 原文: {text[:200]}")
                return None
