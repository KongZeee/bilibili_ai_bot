"""
LLM 提供商 — 封装单个 OpenAI 兼容 API 调用

每个 LLMProvider 实例对应一个 LLM 配置（api_key/base_url/model），
支持文本生成、流式生成、Vision、Embedding。
"""
import base64
import logging
import re
import json
from typing import Optional, List, Any, Tuple

# 可选依赖：openai 未安装或环境异常时仍允许模块被导入
try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover  (ImportError / SystemError / pydantic 冲突等)
    AsyncOpenAI = None  # type: ignore[assignment]

logger = logging.getLogger("bilibot.llm")


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
        self.api_key: str = config.get("api_key", "")
        self.base_url: str = self._normalize_base_url(config.get("base_url", "https://api.siliconflow.cn/v1"))
        self.model: str = config.get("model", "Qwen/Qwen2.5-72B-Instruct")
        self.max_tokens: int = config.get("max_tokens", 1024)
        self.temperature: float = config.get("temperature", 0.8)
        self.name: str = config.get("name", llm_id)
        self.enabled: bool = config.get("enabled", True)

        # 文生图扩展字段（仅 image 类型 Provider 使用）
        self.default_size: str = config.get("default_size", "1024x768")
        self.timeout: int = int(config.get("timeout", 120))

        # Vision 配置（可选）
        vision = config.get("vision", {})
        self.vision_enabled: bool = vision.get("enabled", False)
        self.vision_api_key: str = vision.get("api_key", "")
        self.vision_base_url: str = self._normalize_base_url(vision.get("base_url", self.base_url))
        self.vision_model: str = vision.get("model", "")

        # Embedding 配置（可选）
        embedding = config.get("embedding", {})
        self.embedding_enabled: bool = embedding.get("enabled", False)
        self.embedding_api_key: str = embedding.get("api_key", "")
        self.embedding_base_url: str = self._normalize_base_url(embedding.get("base_url", self.base_url))
        self.embedding_model: str = embedding.get("model", "BAAI/bge-m3")

        # OpenAI 客户端
        self.client = None
        self.vision_client = None
        self.embedding_client = None

        if AsyncOpenAI is None:
            logger.warning(f"[{llm_id}] openai 库未安装，跳过客户端初始化")
            return

        if self.api_key:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
            logger.info(f"[{llm_id}] LLM 客户端已初始化: {self.model}")

        if self.vision_enabled and self.vision_api_key:
            self.vision_client = AsyncOpenAI(
                api_key=self.vision_api_key, base_url=self.vision_base_url
            )
            logger.info(f"[{llm_id}] Vision 客户端已初始化")

        if self.embedding_enabled and self.embedding_api_key:
            self.embedding_client = AsyncOpenAI(
                api_key=self.embedding_api_key, base_url=self.embedding_base_url
            )
            logger.info(f"[{llm_id}] Embedding 客户端已初始化")

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """生成文本回复"""
        if not self.client:
            logger.warning(f"[{self.llm_id}] LLM 客户端未初始化")
            return None

        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = await self.client.chat.completions.create(
                model=model or self.model,
                messages=messages,
                max_tokens=max_tokens or self.max_tokens,
                temperature=temperature if temperature is not None else self.temperature,
            )

            if response.choices:
                result = response.choices[0].message.content
                logger.debug(f"[{self.llm_id}] LLM 生成成功: {len(result)} 字符")
                return result.strip()
            return None

        except Exception as e:
            logger.error(f"[{self.llm_id}] LLM 生成失败: {e}")
            return None

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ):
        """流式生成（异步生成器）"""
        if not self.client:
            return

        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            stream = await self.client.chat.completions.create(
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

    async def vision_analyze(
        self,
        image_url: str,
        prompt: str,
        max_tokens: int = 250,
    ) -> Optional[str]:
        """Vision 模型分析图片"""
        if not self.vision_client or not self.vision_model:
            logger.warning(f"[{self.llm_id}] Vision 客户端未初始化")
            return None

        try:
            content = [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ]

            response = await self.vision_client.chat.completions.create(
                model=self.vision_model,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
            )

            if response.choices:
                return response.choices[0].message.content.strip()
            return None

        except Exception as e:
            logger.error(f"[{self.llm_id}] Vision 分析失败: {e}")
            return None

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """获取文本的 embedding 向量"""
        if not self.embedding_client or not self.embedding_model:
            logger.warning(f"[{self.llm_id}] Embedding 客户端未初始化")
            return None

        try:
            response = await self.embedding_client.embeddings.create(
                model=self.embedding_model,
                input=text,
            )

            if response.data:
                return response.data[0].embedding
            return None

        except Exception as e:
            logger.error(f"[{self.llm_id}] Embedding 获取失败: {e}")
            return None

    async def test(self) -> Tuple[bool, str]:
        """测试对话连接，返回 (是否成功, 错误信息)"""
        if not self.client:
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
        if not self.client:
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        if not self.model:
            return False, "未配置视觉模型名"
        try:
            # 用 1x1 红色 PNG base64 测试
            test_img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
            content = [
                {"type": "image_url", "image_url": {"url": test_img}},
                {"type": "text", "text": "这是什么颜色？"},
            ]
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                max_tokens=20,
            )
            if response.choices:
                return True, ""
            return False, "API 返回空响应"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def test_embedding(self) -> Tuple[bool, str]:
        """测试 Embedding 连接"""
        if not self.client:
            return False, "客户端未初始化（api_key 为空或 openai 库未安装）"
        if not self.model:
            return False, "未配置 Embedding 模型名"
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input="test",
            )
            if response.data and response.data[0].embedding:
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
        if not self.client:
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

            # 方式 1：标准 OpenAI /audio/transcriptions（如 Whisper、SenseVoice）
            try:
                dummy = _io.BytesIO(buf.getvalue())
                dummy.name = "test.wav"
                await self.client.audio.transcriptions.create(
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
                response = await self.client.chat.completions.create(
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
                if response.choices:
                    return True, ""
                return False, "API 返回空响应"
            except Exception as inner2:
                msg2 = str(inner2).lower()
                # 模型不支持、音频相关错误但鉴权通过 → 连接正常
                if any(k in msg2 for k in ("audio", "format", "unsupported", "multimodal", "tensor", "reshape", "duration")):
                    return True, ""
                return False, f"{type(inner2).__name__}: {inner2}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    async def test_image(self) -> Tuple[bool, str]:
        """测试文生图连接（用最小尺寸请求）"""
        if not self.api_key:
            return False, "api_key 未配置"
        if not self.model:
            return False, "未配置文生图模型名"
        try:
            import aiohttp
            headers = {
                "Authorization": f"Bearer {self.api_key}",
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
                    return False, f"HTTP {resp.status}: {text[:200]}"
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
            "has_api_key": bool(self.api_key),
            "vision_enabled": self.vision_enabled,
            "vision_model": self.vision_model,
            "vision_base_url": self.vision_base_url,
            "has_vision_api_key": bool(self.vision_api_key),
            "embedding_enabled": self.embedding_enabled,
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "has_embedding_api_key": bool(self.embedding_api_key),
            "default_size": self.default_size,
            "timeout": self.timeout,
        }

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
