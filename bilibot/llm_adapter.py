"""
LLM 适配器

封装 OpenAI 兼容 API，提供统一的 LLM 调用接口。
支持文本生成、Vision 图片理解、Embedding 向量生成。
"""
import logging
import re
import json
from typing import Optional, List, Any

# 可选依赖：openai 未安装时仍允许模块被导入（仅在不调用真实 API 时使用）
try:
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore[assignment]

logger = logging.getLogger("bilibot.llm")


class LLMAdapter:
    """LLM 适配器，封装 OpenAI 兼容 API"""
    
    def __init__(self, config):
        self.config = config
        self.client: Optional["AsyncOpenAI"] = None
        self.vision_client: Optional["AsyncOpenAI"] = None
        self.embedding_client: Optional["AsyncOpenAI"] = None

        # openai 未安装时无法创建真实客户端，但模块仍可被导入
        if AsyncOpenAI is None:
            logger.warning("openai 库未安装，LLMAdapter 跳过客户端初始化")
            return

        # BUG B-006：timeout 必须传入，避免 chat/vision/embedding 无限挂起
        timeout = self._resolve_timeout(config)

        # 初始化主客户端
        if config.llm.api_key:
            self.client = AsyncOpenAI(
                api_key=config.llm.api_key,
                base_url=config.llm.base_url,
                timeout=timeout,
            )
            logger.info(f"LLM客户端已初始化: {config.llm.model} (timeout={timeout}s)")

        # 初始化Vision客户端
        if config.llm.vision_enabled and config.llm.vision_api_key:
            self.vision_client = AsyncOpenAI(
                api_key=config.llm.vision_api_key,
                base_url=config.llm.vision_base_url,
                timeout=timeout,
            )
            logger.info("Vision客户端已初始化")

        # 初始化Embedding客户端
        if config.llm.embedding_enabled and config.llm.embedding_api_key:
            self.embedding_client = AsyncOpenAI(
                api_key=config.llm.embedding_api_key,
                base_url=config.llm.embedding_base_url,
                timeout=timeout,
            )
            logger.info("Embedding客户端已初始化")

    @staticmethod
    def _resolve_timeout(config) -> int:
        """从 config 解析 HTTP 超时（秒），缺省 120，夹紧到 [5, 600]。"""
        candidates = []
        try:
            candidates.append(getattr(config.llm, "timeout", None))
        except Exception:
            pass
        try:
            if hasattr(config, "get"):
                candidates.append(config.get("llm.timeout", None))
        except Exception:
            pass
        try:
            raw = getattr(config, "get_raw_config", None)
            if callable(raw):
                llm_raw = (raw() or {}).get("llm") or {}
                if isinstance(llm_raw, dict):
                    candidates.append(llm_raw.get("timeout"))
        except Exception:
            pass
        for value in candidates:
            if value is None or value == "":
                continue
            try:
                return max(5, min(int(value), 600))
            except (TypeError, ValueError):
                continue
        return 120
    
    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """
        生成文本回复
        
        Args:
            prompt: 用户提示
            system_prompt: 系统提示
            max_tokens: 最大token数
            temperature: 温度参数
            model: 模型名称（可选，覆盖默认）
            
        Returns:
            生成的文本，失败返回None
        """
        if not self.client:
            logger.warning("LLM客户端未初始化")
            return None
        
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            response = await self.client.chat.completions.create(
                model=model or self.config.llm.model,
                messages=messages,
                max_tokens=max_tokens or self.config.llm.max_tokens,
                temperature=temperature if temperature is not None else self.config.llm.temperature,
            )
            
            if response.choices:
                message = response.choices[0].message
                result = message.content
                if result:
                    logger.debug(f"LLM生成成功: {len(result)} 字符")
                    return result.strip()
                # Reasoning models may put usable text only in reasoning_content
                # when the budget is consumed by chain-of-thought. Prefer an
                # extracted JSON object/array, then a short trailing answer line.
                reasoning = getattr(message, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning.strip():
                    salvaged = self._salvage_from_reasoning(reasoning)
                    if salvaged:
                        logger.warning(
                            "LLM content empty; salvaged %s chars from reasoning_content",
                            len(salvaged),
                        )
                        return salvaged
                    logger.warning(
                        "LLM returned empty content with non-empty reasoning "
                        "(likely max_tokens too low for reasoning model)"
                    )
                return None
            return None

        except Exception as e:
            logger.error(f"LLM生成失败: {e}")
            raise

    @staticmethod
    def _salvage_from_reasoning(reasoning: str) -> Optional[str]:
        """Best-effort extract usable assistant text from reasoning_content."""
        text = str(reasoning or "").strip()
        if not text:
            return None
        # Prefer a JSON object/array if present (rerank / structured jobs).
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end != -1 and end > start:
                candidate = text[start : end + 1]
                try:
                    import json

                    json.loads(candidate)
                    return candidate
                except Exception:
                    fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
                    try:
                        import json

                        json.loads(fixed)
                        return fixed
                    except Exception:
                        pass
        # Fall back to a short final line that looks like an answer.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for line in reversed(lines[-8:]):
            if len(line) < 4:
                continue
            if line.startswith(("1.", "2.", "**", "#", "-", "*")):
                continue
            low = line.casefold()
            if any(
                marker in low
                for marker in (
                    "thinking",
                    "analyze",
                    "user says",
                    "constraint",
                    "language:",
                    "[done",
                    "done.",
                    "output generation",
                    "proceeds.",
                )
            ):
                continue
            if line.strip() in {"[Done.]", "Done.", "DONE", "OK", "ok"}:
                continue
            if len(line) <= 400:
                return line
        return None
    
    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ):
        """
        流式生成（异步生成器）
        
        Yields:
            每次生成的文本块
        """
        if not self.client:
            return
        
        try:
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            
            stream = await self.client.chat.completions.create(
                model=self.config.llm.model,
                messages=messages,
                max_tokens=max_tokens or self.config.llm.max_tokens,
                temperature=temperature if temperature is not None else self.config.llm.temperature,
                stream=True,
            )
            
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
                    
        except Exception as e:
            logger.error(f"LLM流式生成失败: {e}")
            raise

    async def vision_analyze(
        self,
        image_url: str,
        prompt: str,
        max_tokens: int = 250,
    ) -> Optional[str]:
        """
        Vision模型分析图片
        
        Args:
            image_url: 图片URL或base64
            prompt: 分析提示
            max_tokens: 最大token数
            
        Returns:
            分析结果文本
        """
        client = self.vision_client
        if not client or not self.config.llm.vision_model:
            logger.warning("Vision客户端未初始化")
            return None
        
        try:
            content = [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ]
            
            response = await client.chat.completions.create(
                model=self.config.llm.vision_model,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_tokens,
            )
            
            if response.choices:
                return response.choices[0].message.content.strip()
            return None
            
        except Exception as e:
            logger.error(f"Vision分析失败: {e}")
            return None
    
    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """
        获取文本的 embedding 向量。

        客户端未配置时返回 None（调用方可降级）。
        已配置但请求失败（超时/429/5xx）时上抛，避免记忆路径把瞬时故障
        当成「无向量」永久跳过（与 LLMProvider.get_embedding 一致）。
        """
        client = self.embedding_client
        if not client or not self.config.llm.embedding_model:
            logger.warning("Embedding客户端未初始化")
            return None

        try:
            response = await client.embeddings.create(
                model=self.config.llm.embedding_model,
                input=text,
            )
            if response.data:
                return response.data[0].embedding
            # 已配置但返回空向量：上抛，禁止记忆路径当「无 embedding」永久跳过
            raise RuntimeError("embedding API returned empty data")
        except Exception as e:
            logger.error(f"Embedding获取失败: {e}")
            raise
    
    @staticmethod
    def repair_json(text: str) -> str:
        """
        修复LLM返回的JSON
        
        - 去除markdown代码块标记
        - 去除尾随逗号
        - 尝试提取JSON对象
        """
        if not text:
            return ""
        
        # 去除markdown标记
        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text.strip())
        
        # 尝试提取JSON对象
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group()
        
        # 去除尾随逗号
        text = re.sub(r',\s*([}\]])', r'\1', text)
        
        return text
    
    @staticmethod
    def parse_json(text: str) -> Optional[Any]:
        """
        解析JSON字符串
        
        Args:
            text: JSON字符串
            
        Returns:
            解析后的对象，失败返回None
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试修复后再次解析
            fixed = LLMAdapter.repair_json(text)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败: {e}, 原文: {text[:200]}")
                return None
