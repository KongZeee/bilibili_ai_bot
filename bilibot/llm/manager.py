"""
LLM 管理器 — 管理多个 LLMProvider 实例

职责：
- 从 config 的 `llm_providers` 列表加载多个 LLMProvider 实例
- 兼容 V1 配置（单组 `llm` 配置自动转为单个 provider）
- 提供 get_provider / add_provider / remove_provider / set_default / list_providers 管理方法
- 兼容旧代码：提供类似 LLMAdapter 的接口（generate 等方法委托给默认 provider）

V2 配置结构：
```yaml
llm_providers:
  - id: siliconflow
    name: 硅基流动
    api_key: sk-xxx
    base_url: https://api.siliconflow.cn/v1
    model: Qwen/Qwen2.5-72B-Instruct
    enabled: true
    vision:
      enabled: true
      api_key: sk-xxx
      model: Qwen/Qwen2-VL-72B
    embedding:
      enabled: true
      api_key: sk-xxx
      model: BAAI/bge-m3
default_llm: siliconflow
```

V1 兼容：
```yaml
llm:
  api_key: sk-xxx
  base_url: https://api.siliconflow.cn/v1
  model: Qwen/Qwen2.5-72B-Instruct
  vision: {...}
  embedding: {...}
```
"""
import logging
import uuid
from typing import Dict, List, Optional, Any, AsyncGenerator

from bilibot.llm.provider import LLMProvider

logger = logging.getLogger("bilibot.llm")


class LLMManager:
    """管理多个 LLMProvider 实例"""

    def __init__(self, config_loader=None):
        """
        Args:
            config_loader: ConfigLoader 实例（V2 架构）或 None（手动管理）
        """
        self._config_loader = config_loader
        self._providers: Dict[str, LLMProvider] = {}
        self._default_id: str = ""
        # PRD-V5 §5.3 LLM-501：生产模式不允许静默回退
        self._allow_llm_fallback: bool = False

    # ══════════════════════════════════════
    #  初始化
    # ══════════════════════════════════════

    def initialize(self):
        """从配置加载所有 LLM Provider"""
        self._providers.clear()
        self._default_id = ""

        if self._config_loader is None:
            logger.warning("LLMManager 未绑定 config_loader，跳过初始化")
            return

        raw = self._config_loader.get_raw_config()

        # PRD-V5 §5.3 LLM-501：读取 allow_llm_fallback（默认 false，生产模式无静默回退）
        self._allow_llm_fallback = bool(raw.get("allow_llm_fallback", False))

        # V2 配置：llm_providers 列表
        providers_list = raw.get("llm_providers", [])
        if providers_list:
            default_llm = raw.get("default_llm", "")
            for i, p_config in enumerate(providers_list):
                if not isinstance(p_config, dict):
                    continue
                llm_id = p_config.get("id") or f"llm_{i}"
                try:
                    provider = LLMProvider(llm_id, p_config)
                    self._providers[llm_id] = provider
                    logger.info(f"已加载 LLM Provider: {llm_id} ({provider.model})")
                except Exception as e:
                    logger.error(f"加载 LLM Provider {llm_id} 失败: {e}")

            # 默认 LLM
            if default_llm and default_llm in self._providers:
                self._default_id = default_llm
            elif self._providers:
                # 取第一个启用的作为默认
                for pid, p in self._providers.items():
                    if p.enabled:
                        self._default_id = pid
                        break
                if not self._default_id:
                    self._default_id = next(iter(self._providers))
            logger.info(f"默认 LLM: {self._default_id or '(无)'}")
            return

        # V1 兼容：单组 llm 配置自动迁移为单个 provider
        v1_llm = raw.get("llm", {})
        if v1_llm and v1_llm.get("api_key"):
            v1_config = {
                "id": "default",
                "name": "默认 LLM",
                "api_key": v1_llm.get("api_key", ""),
                "base_url": v1_llm.get("base_url", "https://api.siliconflow.cn/v1"),
                "model": v1_llm.get("model", "Qwen/Qwen2.5-72B-Instruct"),
                "max_tokens": v1_llm.get("max_tokens", 1024),
                "temperature": v1_llm.get("temperature", 0.8),
                "enabled": True,
            }
            # 迁移 vision / embedding
            if v1_llm.get("vision", {}).get("enabled"):
                v1_config["vision"] = {
                    "enabled": True,
                    "api_key": v1_llm["vision"].get("api_key", ""),
                    "base_url": v1_llm["vision"].get("base_url", v1_config["base_url"]),
                    "model": v1_llm["vision"].get("model", ""),
                }
            if v1_llm.get("embedding", {}).get("enabled"):
                v1_config["embedding"] = {
                    "enabled": True,
                    "api_key": v1_llm["embedding"].get("api_key", ""),
                    "base_url": v1_llm["embedding"].get("base_url", v1_config["base_url"]),
                    "model": v1_llm["embedding"].get("model", "BAAI/bge-m3"),
                }
            try:
                provider = LLMProvider("default", v1_config)
                self._providers["default"] = provider
                self._default_id = "default"
                logger.info("V1 配置已迁移为默认 LLM Provider: default")
            except Exception as e:
                logger.error(f"V1 配置迁移失败: {e}")
        else:
            logger.warning("未检测到 LLM 配置（既无 llm_providers 也无 llm）")

    # ══════════════════════════════════════
    #  Provider 访问
    # ══════════════════════════════════════

    def get_provider(self, llm_id: Optional[str] = None) -> Optional[LLMProvider]:
        """
        获取指定 LLM Provider，未指定则返回默认

        Args:
            llm_id: Provider ID，None 表示默认

        Returns:
            LLMProvider 实例，不存在返回 None
        """
        if llm_id:
            return self._providers.get(llm_id)
        if self._default_id:
            return self._providers.get(self._default_id)
        # 兜底：返回第一个
        if self._providers:
            return next(iter(self._providers.values()))
        return None

    def get_default(self) -> Optional[LLMProvider]:
        """获取默认 Provider"""
        return self._providers.get(self._default_id) if self._default_id else None

    def list_providers(self) -> List[dict]:
        """列出所有 Provider 信息（脱敏）"""
        return [p.get_info() for p in self._providers.values()]

    def get_default_id(self) -> str:
        """获取默认 Provider ID"""
        return self._default_id

    @property
    def allow_llm_fallback(self) -> bool:
        """PRD-V5 §5.3 LLM-501：是否允许 LLM 静默回退"""
        return self._allow_llm_fallback

    def resolve_provider(
        self,
        llm_id: Optional[str] = None,
        allow_fallback: Optional[bool] = None,
    ) -> tuple:
        """PRD-V5 §5.3 LLM-501：解析有效 LLM Provider（含回退控制）

        Args:
            llm_id: 账号配置的 llm_id，空字符串或 None 表示用默认
            allow_fallback: 是否允许回退到默认。None 时用 self._allow_llm_fallback。

        Returns:
            (provider, effective_id, fallback_reason)
            - provider: LLMProvider 实例，不可用时为 None
            - effective_id: 实际使用的 Provider ID
            - fallback_reason: 回退原因（空字符串表示无回退）
        """
        if allow_fallback is None:
            allow_fallback = self._allow_llm_fallback

        configured_id = (llm_id or "").strip()

        # 无指定 llm_id → 直接用默认（非回退）
        if not configured_id:
            default_provider = self.get_default()
            if default_provider is not None:
                return (default_provider, self._default_id, "")
            return (None, "", "no default LLM provider configured")

        # 有指定 llm_id → 校验存在且 enabled
        provider = self._providers.get(configured_id)
        if provider is not None and provider.enabled:
            return (provider, configured_id, "")

        # 配置的 Provider 不可用
        if provider is None:
            reason = f"configured provider '{configured_id}' not found"
        else:
            reason = f"configured provider '{configured_id}' is disabled"

        if not allow_fallback:
            logger.warning(
                f"LLM-501: {reason}, allow_llm_fallback=false — account degraded"
            )
            return (None, "", reason)

        # 允许回退到默认
        default_provider = self.get_default()
        if default_provider is not None:
            logger.warning(f"LLM-501: {reason}, falling back to default '{self._default_id}'")
            return (default_provider, self._default_id, reason)

        logger.warning(f"LLM-501: {reason}, no default available for fallback")
        return (None, "", reason)

    # ══════════════════════════════════════
    #  Provider 管理（运行时增删改）
    # ══════════════════════════════════════

    def add_provider(self, config: dict) -> str:
        """
        添加 Provider

        Args:
            config: Provider 配置（含 api_key/base_url/model 等）

        Returns:
            新 Provider 的 ID
        """
        llm_id = config.get("id") or f"llm_{uuid.uuid4().hex[:8]}"
        if llm_id in self._providers:
            raise ValueError(f"LLM Provider ID 已存在: {llm_id}")
        provider = LLMProvider(llm_id, config)
        self._providers[llm_id] = provider
        # 若无默认，自动设为默认
        if not self._default_id:
            self._default_id = llm_id
        logger.info(f"已添加 LLM Provider: {llm_id}")
        return llm_id

    def remove_provider(self, llm_id: str) -> bool:
        """
        删除 Provider

        Args:
            llm_id: Provider ID

        Returns:
            是否删除成功
        """
        if llm_id not in self._providers:
            return False
        if llm_id == self._default_id:
            # 默认被删除，自动切换到第一个
            del self._providers[llm_id]
            self._default_id = next(iter(self._providers), "")
            logger.warning(f"默认 LLM {llm_id} 已删除，新默认: {self._default_id or '(无)'}")
        else:
            del self._providers[llm_id]
        logger.info(f"已删除 LLM Provider: {llm_id}")
        return True

    def update_provider(self, llm_id: str, config: dict) -> bool:
        """
        更新 Provider 配置（重建实例）

        Args:
            llm_id: Provider ID
            config: 新配置

        Returns:
            是否更新成功
        """
        if llm_id not in self._providers:
            return False
        new_config = {**config, "id": llm_id}
        provider = LLMProvider(llm_id, new_config)
        self._providers[llm_id] = provider
        logger.info(f"已更新 LLM Provider: {llm_id}")
        return True

    def set_default(self, llm_id: str) -> bool:
        """设置默认 Provider"""
        if llm_id not in self._providers:
            return False
        self._default_id = llm_id
        logger.info(f"默认 LLM 已设置为: {llm_id}")
        return True

    # ══════════════════════════════════════
    #  持久化
    # ══════════════════════════════════════

    def save_to_config(self) -> dict:
        """
        将当前所有 Provider 序列化为 V2 配置字典

        Returns:
            dict: {"llm_providers": [...], "default_llm": "..."}
        """
        providers_list = []
        for pid, p in self._providers.items():
            # 完整配置（含 api_key，用于保存）
            item = {
                "id": pid,
                "name": p.name,
                "api_key": p.api_key,
                "base_url": p.base_url,
                "model": p.model,
                "max_tokens": p.max_tokens,
                "temperature": p.temperature,
                "enabled": p.enabled,
            }
            if p.vision_enabled:
                item["vision"] = {
                    "enabled": True,
                    "api_key": p.vision_api_key,
                    "base_url": p.vision_base_url,
                    "model": p.vision_model,
                }
            if p.embedding_enabled:
                item["embedding"] = {
                    "enabled": True,
                    "api_key": p.embedding_api_key,
                    "base_url": p.embedding_base_url,
                    "model": p.embedding_model,
                }
            providers_list.append(item)
        return {
            "llm_providers": providers_list,
            "default_llm": self._default_id,
        }

    # ══════════════════════════════════════
    #  旧 LLMAdapter 兼容层
    #  现有代码 `llm.generate(...)` / `llm.vision_analyze(...)` 等可直接委托给默认 provider
    # ══════════════════════════════════════

    @property
    def client(self):
        """兼容旧 LLMAdapter.client"""
        p = self.get_default()
        return p.client if p else None

    @property
    def vision_client(self):
        """兼容旧 LLMAdapter.vision_client"""
        p = self.get_default()
        return p.vision_client if p else None

    @property
    def embedding_client(self):
        """兼容旧 LLMAdapter.embedding_client"""
        p = self.get_default()
        return p.embedding_client if p else None

    @property
    def config(self):
        """兼容旧 LLMAdapter.config（返回 config_loader）"""
        return self._config_loader

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        llm_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        生成文本（委托给指定或默认 Provider）

        Args:
            llm_id: 指定 Provider，None 用默认
            其余参数同 LLMProvider.generate
        """
        provider = self.get_provider(llm_id)
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
        """流式生成（委托给指定或默认 Provider）"""
        provider = self.get_provider(llm_id)
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
        """Vision 分析（委托给指定或默认 Provider）"""
        provider = self.get_provider(llm_id)
        if provider is None:
            return None
        return await provider.vision_analyze(image_url, prompt, max_tokens)

    async def get_embedding(
        self, text: str, llm_id: Optional[str] = None
    ) -> Optional[List[float]]:
        """获取 Embedding（委托给指定或默认 Provider）"""
        provider = self.get_provider(llm_id)
        if provider is None:
            return None
        return await provider.get_embedding(text)

    async def test(self, llm_id: Optional[str] = None) -> bool:
        """测试连接"""
        provider = self.get_provider(llm_id)
        if provider is None:
            return False
        return await provider.test()

    # ══════════════════════════════════════
    #  静态工具方法（兼容旧 LLMAdapter）
    # ══════════════════════════════════════

    @staticmethod
    def repair_json(text: str) -> str:
        """修复 LLM 返回的 JSON"""
        return LLMProvider.repair_json(text)

    @staticmethod
    def parse_json(text: str) -> Optional[Any]:
        """解析 JSON 字符串"""
        return LLMProvider.parse_json(text)

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, llm_id: str) -> bool:
        return llm_id in self._providers

    def __repr__(self) -> str:
        return (
            f"<LLMManager providers={len(self._providers)} "
            f"default={self._default_id!r}>"
        )
