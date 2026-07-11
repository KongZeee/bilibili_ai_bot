"""
LLM 管理器 — 继承 ModelRouter，保持向后兼容

V3 架构：ModelRouter 统一管理 5 种 Provider（chat/vision/embedding/asr/image）
LLMManager 作为别名层，让现有代码 `llm_manager.get_provider()` / `llm_manager.generate()` 继续工作。

新代码应直接使用 ModelRouter 的 `resolve_xxx()` 方法。
"""
import logging
from typing import Optional, List, Any, AsyncGenerator

from bilibot.llm.router import ModelRouter, CHAT, VISION, EMBEDDING
from bilibot.llm.provider import LLMProvider

logger = logging.getLogger("bilibot.llm")


class LLMManager(ModelRouter):
    """LLM 管理器（继承 ModelRouter，兼容旧接口）"""

    # ══════════════════════════════════════
    #  旧 LLMAdapter 兼容层
    #  现有代码 `llm.generate(...)` / `llm.vision_analyze(...)` 等可直接委托
    # ══════════════════════════════════════

    @property
    def client(self):
        """兼容旧 LLMAdapter.client"""
        p = self.resolve_chat()
        return p.client if p else None

    @property
    def vision_client(self):
        """兼容旧 LLMAdapter.vision_client — 优先用路由的 vision provider"""
        p = self.resolve_vision()
        if p and p.client:
            return p.client
        # 回退到 chat provider 的 vision_client（旧嵌套配置）
        cp = self.resolve_chat()
        return cp.vision_client if cp else None

    @property
    def embedding_client(self):
        """兼容旧 LLMAdapter.embedding_client — 优先用路由的 embedding provider"""
        p = self.resolve_embedding()
        if p and p.client:
            return p.client
        cp = self.resolve_chat()
        return cp.embedding_client if cp else None

    @property
    def config(self):
        """兼容旧 LLMAdapter.config"""
        return self._config_loader

    def get_provider(self, llm_id: Optional[str] = None) -> Optional[LLMProvider]:
        """获取指定 LLM Provider（兼容旧接口）"""
        return self.resolve_chat(llm_id or "")

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        llm_id: Optional[str] = None,
    ) -> Optional[str]:
        """生成文本（委托给指定或默认对话 Provider）"""
        provider = self.resolve_chat(llm_id or "")
        if provider is None:
            logger.warning(f"LLM Provider 不存在: {llm_id or '(默认)'}")
            return None
        return await provider.generate(
            prompt, system_prompt, max_tokens, temperature, model
        )

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        llm_id: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """流式生成（委托给指定或默认对话 Provider）"""
        provider = self.resolve_chat(llm_id or "")
        if provider is None:
            return
        async for chunk in provider.generate_stream(
            prompt, system_prompt, max_tokens, temperature
        ):
            yield chunk

    async def vision_analyze(
        self,
        image_url: str,
        prompt: str,
        max_tokens: int = 250,
        llm_id: Optional[str] = None,
    ) -> Optional[str]:
        """Vision 分析 — 优先用路由的 vision provider，回退到 chat provider"""
        # 优先走 vision 路由
        vp = self.resolve_vision()
        if vp and vp.client and vp.model:
            return await vp.vision_analyze(image_url, prompt, max_tokens)
        # 回退到 chat provider 的嵌套 vision
        provider = self.resolve_chat(llm_id or "")
        if provider is None:
            return None
        return await provider.vision_analyze(image_url, prompt, max_tokens)

    async def get_embedding(
        self, text: str, llm_id: Optional[str] = None
    ) -> Optional[List[float]]:
        """获取 Embedding — 优先用路由的 embedding provider，回退到 chat provider"""
        ep = self.resolve_embedding()
        if ep and ep.client and ep.model:
            return await ep.get_embedding(text)
        provider = self.resolve_chat(llm_id or "")
        if provider is None:
            return None
        return await provider.get_embedding(text)

    async def test(self, llm_id: Optional[str] = None) -> tuple:
        """测试连接（兼容旧接口，返回 (bool, str)）"""
        provider = self.resolve_chat(llm_id or "")
        if provider is None:
            return False, "Provider 不存在"
        return await provider.test()

    # ══════════════════════════════════════
    #  旧 LLMProvider 兼容：resolve_provider
    # ══════════════════════════════════════

    def resolve_provider(
        self,
        llm_id: Optional[str] = None,
        allow_fallback: Optional[bool] = None,
    ) -> tuple:
        """PRD-V5 §5.3 LLM-501：解析有效 LLM Provider（含回退控制）"""
        if allow_fallback is None:
            allow_fallback = self._allow_llm_fallback

        configured_id = (llm_id or "").strip()

        if not configured_id:
            default_provider = self.resolve_chat()
            if default_provider is not None:
                return (default_provider, self._routing.get(CHAT, ""), "")
            return (None, "", "no default LLM provider configured")

        provider = self._pools[CHAT].get(configured_id)
        if provider is not None and provider.enabled:
            return (provider, configured_id, "")

        if provider is None:
            reason = f"configured provider '{configured_id}' not found"
        else:
            reason = f"configured provider '{configured_id}' is disabled"

        if not allow_fallback:
            logger.warning(f"LLM-501: {reason}, allow_llm_fallback=false — account degraded")
            return (None, "", reason)

        default_provider = self.resolve_chat()
        if default_provider is not None:
            logger.warning(f"LLM-501: {reason}, falling back to default")
            return (default_provider, self._routing.get(CHAT, ""), reason)

        return (None, "", reason)

    # ══════════════════════════════════════
    #  静态工具方法
    # ══════════════════════════════════════

    @staticmethod
    def repair_json(text: str) -> str:
        return LLMProvider.repair_json(text)

    @staticmethod
    def parse_json(text: str) -> Optional[Any]:
        return LLMProvider.parse_json(text)

    def __len__(self) -> int:
        return len(self._pools[CHAT])

    def __contains__(self, llm_id: str) -> bool:
        return llm_id in self._pools[CHAT]

    def __repr__(self) -> str:
        return (
            f"<LLMManager chat={len(self._pools[CHAT])} "
            f"vision={len(self._pools[VISION])} "
            f"embed={len(self._pools[EMBEDDING])} "
            f"default={self._routing.get(CHAT, '')!r}>"
        )
