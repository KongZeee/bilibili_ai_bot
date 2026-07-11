"""
LLM 提供商 — 封装单个 OpenAI 兼容 API 调用

每个 LLMProvider 实例对应一个 LLM 配置（api_key/base_url/model），
支持文本生成、流式生成、Vision、Embedding。
"""
import logging
import re
import json
from typing import Optional, List, Any

# 可选依赖：openai 未安装或环境异常时仍允许模块被导入
try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover  (ImportError / SystemError / pydantic 冲突等)
    AsyncOpenAI = None  # type: ignore[assignment]

logger = logging.getLogger("bilibot.llm")


class LLMProvider:
    """单个 LLM 提供商 — 封装 OpenAI 兼容 API"""

    def __init__(self, llm_id: str, config: dict):
        self.llm_id = llm_id
        self.api_key: str = config.get("api_key", "")
        self.base_url: str = config.get("base_url", "https://api.siliconflow.cn/v1")
        self.model: str = config.get("model", "Qwen/Qwen2.5-72B-Instruct")
        self.max_tokens: int = config.get("max_tokens", 1024)
        self.temperature: float = config.get("temperature", 0.8)
        self.name: str = config.get("name", llm_id)
        self.enabled: bool = config.get("enabled", True)

        # Vision 配置（可选）
        vision = config.get("vision", {})
        self.vision_enabled: bool = vision.get("enabled", False)
        self.vision_api_key: str = vision.get("api_key", "")
        self.vision_base_url: str = vision.get("base_url", self.base_url)
        self.vision_model: str = vision.get("model", "")

        # Embedding 配置（可选）
        embedding = config.get("embedding", {})
        self.embedding_enabled: bool = embedding.get("enabled", False)
        self.embedding_api_key: str = embedding.get("api_key", "")
        self.embedding_base_url: str = embedding.get("base_url", self.base_url)
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

    async def test(self) -> bool:
        """测试连接"""
        if not self.client:
            return False
        try:
            result = await self.generate("你好", max_tokens=10)
            return result is not None
        except Exception:
            return False

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
            "embedding_enabled": self.embedding_enabled,
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
