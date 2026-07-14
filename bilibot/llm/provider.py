"""
LLM 提供商 — 封装单个 OpenAI 兼容 API 调用

每个 LLMProvider 实例对应一个 LLM 配置（api_key/api_keys/base_url/model），
支持文本生成、流式生成、Vision、Embedding；同一 Provider 可挂多 API Key 并行分发。
"""
import base64
import asyncio
import hashlib
import logging
import re
import json
import threading
import time
import weakref
from contextlib import asynccontextmanager
from typing import Optional, List, Any, Tuple, Sequence, Callable

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
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "429",
            "rate limit",
            "rate_limit",
            "too many requests",
            "quota exceeded",
            "tpm",
            "rpm",
        )
    )


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (401, 403):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in ("unauthorized", "invalid api key", "invalid_api_key", "authentication")
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

    async def acquire(self) -> Optional[_KeySlot]:
        if not self.slots:
            return None
        async with self._lock:
            now = time.monotonic()
            # Clients may be lazy-created after acquire; select by key availability.
            ready = [
                s for s in self.slots
                if not s.disabled and s.cooldown_until <= now
            ]
            if not ready:
                candidates = [s for s in self.slots if not s.disabled]
                if not candidates:
                    return None
                slot = min(candidates, key=lambda s: (s.cooldown_until, s.inflight))
            else:
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
    _URL_SUFFIX_TRIM = ("/embeddings", "/embedding", "/audio/transcriptions",
                        "/audio", "/images/generations", "/images", "/chat/completions", "/chat")

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
        self.vision_base_url: str = self._normalize_base_url(vision.get("base_url", self.base_url))
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
        self.embedding_base_url: str = self._normalize_base_url(embedding.get("base_url", self.base_url))
        self.embedding_model: str = embedding.get("model", "BAAI/bge-m3")

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
        return None

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

            async def _call(client):
                return await client.chat.completions.create(
                    model=model or self.model,
                    messages=messages,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=temperature if temperature is not None else self.temperature,
                )

            response = await self._chat_with_pool(_call)
            if response and response.choices:
                result = response.choices[0].message.content
                if result:
                    logger.debug(f"[{self.llm_id}] LLM 生成成功: {len(result)} 字符")
                    return result.strip()
                return None
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

        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            # Streaming holds one connection; pick one key without mid-stream switch.
            async with self._chat_pool.checkout() as slot:
                if slot is None:
                    return
                client = self._ensure_client(slot, self.base_url)
                if client is None:
                    return
                stream = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=temperature if temperature is not None else self.temperature,
                    stream=True,
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

        except Exception as e:
            logger.error(f"[{self.llm_id}] LLM 流式生成失败: {e}")
            raise

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
                            max_tokens=max_tokens,
                        )
                        if response.choices:
                            return response.choices[0].message.content.strip()
                        return None
                    except Exception as exc:
                        last_exc = exc
                        if _is_rate_limit_error(exc) and attempts > 1:
                            continue
                        raise
            if last_exc is not None:
                raise last_exc
            return None

        except Exception as e:
            logger.error(f"[{self.llm_id}] Vision 分析失败: {e}")
            return None

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
                        if response.data:
                            return response.data[0].embedding
                        return None
                    except Exception as exc:
                        last_exc = exc
                        if _is_rate_limit_error(exc) and attempts > 1:
                            continue
                        raise
            if last_exc is not None:
                raise last_exc
            return None
        except Exception as e:
            logger.error(f"[{self.llm_id}] Embedding 获取失败: {e}")
            return None

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
                data = list(response.data or ())
                if len(data) != len(values):
                    return None
                ordered: List[Optional[List[float]]] = [None] * len(values)
                for position, item in enumerate(data):
                    index = getattr(item, "index", position)
                    if index is None:
                        index = position
                    if not isinstance(index, int) or not 0 <= index < len(values):
                        return None
                    if ordered[index] is not None:
                        return None
                    ordered[index] = list(item.embedding)
                if any(vector is None for vector in ordered):
                    return None
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
            return None

        except Exception as e:
            logger.error(f"[{self.llm_id}] Embedding 批量获取失败: {e}")
            return None

    async def test(self) -> Tuple[bool, str]:
        """测试对话连接，返回 (是否成功, 错误信息)"""
        if not self._chat_pool.slots:
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        try:
            result = await self.generate("你好", max_tokens=10)
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
                    return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
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
                    "response_format": "url",
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
        """修复 LLM 返回的 JSON"""
        if not text:
            return ""
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text.strip())
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group()
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
