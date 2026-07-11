"""
模型路由器 — 统一管理各功能（对话/视觉/Embedding/ASR/文生图）的 Provider 选择

参考 AstrBot 的「功能路由扁平化」设计：
- 每种能力独立 Provider 列表（chat_providers / vision_providers / ...）
- model_routing 映射功能 → Provider ID
- 运行时通过 resolve_xxx() 解析具体 Provider

向后兼容：
- 旧 llm_providers 列表自动迁移为 chat_providers
- 旧 llm_providers[].vision 嵌套配置自动迁移为 vision_providers
- 旧 llm_providers[].embedding 嵌套配置自动迁移为 embedding_providers
- 旧 video_analysis.asr 自动迁移为 asr_providers
- 旧 image_generation 自动迁移为 image_providers
"""
import logging
import uuid
from typing import Dict, List, Optional, Any

from bilibot.llm.provider import LLMProvider

logger = logging.getLogger("bilibot.llm.router")

# Provider 类型常量
CHAT = "chat"
VISION = "vision"
EMBEDDING = "embedding"
ASR = "asr"
IMAGE = "image"

PROVIDER_TYPES = [CHAT, VISION, EMBEDDING, ASR, IMAGE]

# 各类型的配置段名
CONFIG_KEYS = {
    CHAT: "chat_providers",
    VISION: "vision_providers",
    EMBEDDING: "embedding_providers",
    ASR: "asr_providers",
    IMAGE: "image_providers",
}


class ModelRouter:
    """模型路由器 — 管理所有类型的 Provider 并按功能路由"""

    def __init__(self, config_loader=None):
        self._config_loader = config_loader
        # 各类型 Provider 池: {type: {id: LLMProvider}}
        self._pools: Dict[str, Dict[str, LLMProvider]] = {t: {} for t in PROVIDER_TYPES}
        # 功能路由: {type: provider_id}
        self._routing: Dict[str, str] = {t: "" for t in PROVIDER_TYPES}
        # 本地 Whisper 配置
        self._local_whisper: dict = {}
        self._allow_llm_fallback: bool = False

    # ══════════════════════════════════════
    #  初始化（含 V2→V3 迁移）
    # ══════════════════════════════════════

    def initialize(self):
        """从配置加载所有 Provider 并建立路由"""
        for t in PROVIDER_TYPES:
            self._pools[t].clear()
        self._routing = {t: "" for t in PROVIDER_TYPES}

        if self._config_loader is None:
            logger.warning("ModelRouter 未绑定 config_loader，跳过初始化")
            return

        raw = self._config_loader.get_raw_config()
        self._allow_llm_fallback = bool(raw.get("allow_llm_fallback", False))

        # 读取 model_routing
        routing = raw.get("model_routing", {}) or {}
        for t in PROVIDER_TYPES:
            self._routing[t] = routing.get(t, "")

        # 加载各类型 Provider 列表
        self._load_chat_providers(raw)
        self._load_vision_providers(raw)
        self._load_embedding_providers(raw)
        self._load_asr_providers(raw)
        self._load_image_providers(raw)

        # 自动补全路由（未配置时取第一个启用的）
        for t in PROVIDER_TYPES:
            if not self._routing[t] or self._routing[t] not in self._pools[t]:
                for pid, p in self._pools[t].items():
                    if p.enabled:
                        self._routing[t] = pid
                        break
                if not self._routing[t] and self._pools[t]:
                    self._routing[t] = next(iter(self._pools[t]))

        # 汇总日志
        for t in PROVIDER_TYPES:
            count = len(self._pools[t])
            routed = self._routing[t] or "(无)"
            logger.info(f"[router] {t}: {count} 个 Provider, 路由 → {routed}")

    def _load_chat_providers(self, raw: dict):
        """加载对话模型 Provider"""
        chat_list = raw.get("chat_providers", [])
        if chat_list:
            for i, cfg in enumerate(chat_list):
                if not isinstance(cfg, dict):
                    continue
                pid = cfg.get("id") or f"chat_{i}"
                try:
                    self._pools[CHAT][pid] = LLMProvider(pid, cfg)
                    logger.info(f"[router] 已加载对话 Provider: {pid} ({cfg.get('model', '')})")
                except Exception as e:
                    logger.error(f"[router] 加载对话 Provider {pid} 失败: {e}")
            return

        # V2 兼容：从 llm_providers 迁移
        v2_list = raw.get("llm_providers", [])
        if v2_list:
            v1_llm = raw.get("llm", {}) or {}
            for i, cfg in enumerate(v2_list):
                if not isinstance(cfg, dict):
                    continue
                pid = cfg.get("id") or f"chat_{i}"
                # V1 api_key 回退
                if not cfg.get("api_key") and v1_llm.get("api_key"):
                    cfg = {**cfg, "api_key": v1_llm["api_key"]}
                try:
                    self._pools[CHAT][pid] = LLMProvider(pid, cfg)
                    logger.info(f"[router] V2迁移对话 Provider: {pid}")
                except Exception as e:
                    logger.error(f"[router] V2迁移对话 Provider {pid} 失败: {e}")
            # 迁移 routing
            if not self._routing[CHAT]:
                self._routing[CHAT] = raw.get("default_llm", "")
            return

        # V1 兼容：从 llm 迁移
        v1_llm = raw.get("llm", {}) or {}
        if v1_llm.get("api_key"):
            cfg = {
                "id": "default",
                "name": "默认 LLM",
                "api_key": v1_llm.get("api_key", ""),
                "base_url": v1_llm.get("base_url", "https://api.siliconflow.cn/v1"),
                "model": v1_llm.get("model", "Qwen/Qwen2.5-72B-Instruct"),
                "max_tokens": v1_llm.get("max_tokens", 1024),
                "temperature": v1_llm.get("temperature", 0.8),
                "enabled": True,
            }
            try:
                self._pools[CHAT]["default"] = LLMProvider("default", cfg)
                self._routing[CHAT] = "default"
                logger.info("[router] V1迁移对话 Provider: default")
            except Exception as e:
                logger.error(f"[router] V1迁移失败: {e}")

    def _load_vision_providers(self, raw: dict):
        """加载视觉模型 Provider"""
        vision_list = raw.get("vision_providers", [])
        if vision_list:
            for i, cfg in enumerate(vision_list):
                if not isinstance(cfg, dict):
                    continue
                pid = cfg.get("id") or f"vision_{i}"
                # api_key 留空回退到默认对话 Provider
                cfg = self._fallback_api_key(raw, cfg)
                try:
                    self._pools[VISION][pid] = LLMProvider(pid, cfg)
                    logger.info(f"[router] 已加载视觉 Provider: {pid} ({cfg.get('model', '')})")
                except Exception as e:
                    logger.error(f"[router] 加载视觉 Provider {pid} 失败: {e}")
            return

        # V2/V1 兼容：从 llm_providers[].vision 提取
        v2_list = raw.get("llm_providers", [])
        v1_llm = raw.get("llm", {}) or {}
        for i, pcfg in enumerate(v2_list):
            if not isinstance(pcfg, dict):
                continue
            vcfg = pcfg.get("vision", {}) or {}
            if vcfg.get("enabled") and (vcfg.get("model") or vcfg.get("api_key")):
                pid = f"{pcfg.get('id', f'chat_{i}')}-vision"
                merged = {
                    "id": pid,
                    "name": f"{pcfg.get('name', pid)} 视觉",
                    "api_key": vcfg.get("api_key", "") or pcfg.get("api_key", "") or v1_llm.get("api_key", ""),
                    "base_url": vcfg.get("base_url", "") or pcfg.get("base_url", ""),
                    "model": vcfg.get("model", ""),
                    "enabled": True,
                }
                try:
                    self._pools[VISION][pid] = LLMProvider(pid, merged)
                    self._routing[VISION] = pid
                    logger.info(f"[router] V2迁移视觉 Provider: {pid}")
                except Exception:
                    pass

        # V1 llm.vision
        if not self._pools[VISION]:
            v1_v = v1_llm.get("vision", {}) or {}
            if v1_v.get("enabled") and (v1_v.get("model") or v1_v.get("api_key")):
                pid = "default-vision"
                merged = {
                    "id": pid,
                    "name": "默认视觉",
                    "api_key": v1_v.get("api_key", "") or v1_llm.get("api_key", ""),
                    "base_url": v1_v.get("base_url", "") or v1_llm.get("base_url", ""),
                    "model": v1_v.get("model", ""),
                    "enabled": True,
                }
                try:
                    self._pools[VISION][pid] = LLMProvider(pid, merged)
                    self._routing[VISION] = pid
                    logger.info("[router] V1迁移视觉 Provider: default-vision")
                except Exception:
                    pass

    def _load_embedding_providers(self, raw: dict):
        """加载 Embedding 模型 Provider"""
        emb_list = raw.get("embedding_providers", [])
        if emb_list:
            for i, cfg in enumerate(emb_list):
                if not isinstance(cfg, dict):
                    continue
                pid = cfg.get("id") or f"embedding_{i}"
                cfg = self._fallback_api_key(raw, cfg)
                try:
                    self._pools[EMBEDDING][pid] = LLMProvider(pid, cfg)
                    logger.info(f"[router] 已加载 Embedding Provider: {pid} ({cfg.get('model', '')})")
                except Exception as e:
                    logger.error(f"[router] 加载 Embedding Provider {pid} 失败: {e}")
            return

        # V2/V1 兼容：从 llm_providers[].embedding 提取
        v2_list = raw.get("llm_providers", [])
        v1_llm = raw.get("llm", {}) or {}
        for i, pcfg in enumerate(v2_list):
            if not isinstance(pcfg, dict):
                continue
            ecfg = pcfg.get("embedding", {}) or {}
            if ecfg.get("enabled") and (ecfg.get("model") or ecfg.get("api_key")):
                pid = f"{pcfg.get('id', f'chat_{i}')}-embed"
                merged = {
                    "id": pid,
                    "name": f"{pcfg.get('name', pid)} Embedding",
                    "api_key": ecfg.get("api_key", "") or pcfg.get("api_key", "") or v1_llm.get("api_key", ""),
                    "base_url": ecfg.get("base_url", "") or pcfg.get("base_url", ""),
                    "model": ecfg.get("model", "BAAI/bge-m3"),
                    "enabled": True,
                }
                try:
                    self._pools[EMBEDDING][pid] = LLMProvider(pid, merged)
                    self._routing[EMBEDDING] = pid
                    logger.info(f"[router] V2迁移 Embedding Provider: {pid}")
                except Exception:
                    pass

        if not self._pools[EMBEDDING]:
            v1_e = v1_llm.get("embedding", {}) or {}
            if v1_e.get("enabled") and (v1_e.get("model") or v1_e.get("api_key")):
                pid = "default-embed"
                merged = {
                    "id": pid,
                    "name": "默认 Embedding",
                    "api_key": v1_e.get("api_key", "") or v1_llm.get("api_key", ""),
                    "base_url": v1_e.get("base_url", "") or v1_llm.get("base_url", ""),
                    "model": v1_e.get("model", "BAAI/bge-m3"),
                    "enabled": True,
                }
                try:
                    self._pools[EMBEDDING][pid] = LLMProvider(pid, merged)
                    self._routing[EMBEDDING] = pid
                    logger.info("[router] V1迁移 Embedding Provider: default-embed")
                except Exception:
                    pass

    def _load_asr_providers(self, raw: dict):
        """加载 ASR 模型 Provider"""
        asr_list = raw.get("asr_providers", [])
        has_asr_list = False
        if asr_list:
            for item in asr_list:
                if not isinstance(item, dict):
                    continue
                # local_whisper 子段
                if "model_size" in item or "whisper_device" in item:
                    self._local_whisper = item
                    continue
                pid = item.get("id") or f"asr_{len(self._pools[ASR])}"
                cfg = self._fallback_api_key(raw, item)
                try:
                    self._pools[ASR][pid] = LLMProvider(pid, cfg)
                    logger.info(f"[router] 已加载 ASR Provider: {pid} ({cfg.get('model', '')})")
                except Exception as e:
                    logger.error(f"[router] 加载 ASR Provider {pid} 失败: {e}")
            has_asr_list = True

        # 本地 Whisper 配置
        lw = raw.get("asr_providers", [])
        for item in (lw if isinstance(lw, list) else []):
            if isinstance(item, dict) and ("model_size" in item or "whisper_device" in item):
                self._local_whisper = item
                break
        if not self._local_whisper:
            # V1 兼容：video_analysis.asr + local_whisper_enabled
            va = raw.get("video_analysis", {}) or {}
            asr_old = va.get("asr", {}) or {}
            if asr_old:
                if asr_old.get("model"):
                    pid = "default-asr"
                    cfg = {
                        "id": pid,
                        "name": "默认 ASR",
                        "api_key": asr_old.get("api_key", "") or self._get_chat_api_key(raw),
                        "base_url": asr_old.get("base_url", "") or self._get_chat_base_url(raw),
                        "model": asr_old.get("model", ""),
                        "enabled": True,
                    }
                    try:
                        self._pools[ASR][pid] = LLMProvider(pid, cfg)
                        self._routing[ASR] = pid
                        logger.info("[router] V1迁移 ASR Provider: default-asr")
                    except Exception:
                        pass
                if va.get("local_whisper_enabled"):
                    self._local_whisper = {
                        "enabled": True,
                        "model_size": asr_old.get("whisper_model_size", "base"),
                        "device": asr_old.get("whisper_device", "cpu"),
                        "compute_type": asr_old.get("whisper_compute_type", "int8"),
                    }

    def _load_image_providers(self, raw: dict):
        """加载文生图 Provider"""
        img_list = raw.get("image_providers", [])
        if img_list:
            for i, cfg in enumerate(img_list):
                if not isinstance(cfg, dict):
                    continue
                pid = cfg.get("id") or f"image_{i}"
                try:
                    self._pools[IMAGE][pid] = LLMProvider(pid, cfg)
                    logger.info(f"[router] 已加载文生图 Provider: {pid} ({cfg.get('model', '')})")
                except Exception as e:
                    logger.error(f"[router] 加载文生图 Provider {pid} 失败: {e}")
            return

        # V1 兼容：从 image_generation 迁移
        old_img = raw.get("image_generation", {}) or {}
        if old_img.get("model") or old_img.get("api_key"):
            pid = "default-image"
            cfg = {
                "id": pid,
                "name": "默认文生图",
                "api_key": old_img.get("api_key", ""),
                "base_url": old_img.get("base_url", "https://apihub.agnes-ai.com/v1"),
                "model": old_img.get("model", "agnes-image-2.1-flash"),
                "default_size": old_img.get("default_size", "1024x768"),
                "timeout": old_img.get("timeout", 120),
                "enabled": old_img.get("enabled", False),
            }
            try:
                self._pools[IMAGE][pid] = LLMProvider(pid, cfg)
                self._routing[IMAGE] = pid
                logger.info("[router] V1迁移文生图 Provider: default-image")
            except Exception:
                pass

    def _fallback_api_key(self, raw: dict, cfg: dict) -> dict:
        """api_key 留空时回退到默认对话 Provider"""
        if not cfg.get("api_key"):
            chat_key = self._get_chat_api_key(raw)
            if chat_key:
                cfg = {**cfg, "api_key": chat_key}
            if not cfg.get("base_url"):
                chat_url = self._get_chat_base_url(raw)
                if chat_url:
                    cfg = {**cfg, "base_url": chat_url}
        return cfg

    def _get_chat_api_key(self, raw: dict) -> str:
        """获取默认对话 Provider 的 api_key（用于回退）"""
        # 优先从 chat_providers
        for cfg in (raw.get("chat_providers") or []):
            if isinstance(cfg, dict) and cfg.get("api_key"):
                return cfg["api_key"]
        # V2 llm_providers
        for cfg in (raw.get("llm_providers") or []):
            if isinstance(cfg, dict) and cfg.get("api_key"):
                return cfg["api_key"]
        # V1 llm
        return (raw.get("llm") or {}).get("api_key", "")

    def _get_chat_base_url(self, raw: dict) -> str:
        """获取默认对话 Provider 的 base_url"""
        for cfg in (raw.get("chat_providers") or []):
            if isinstance(cfg, dict) and cfg.get("base_url"):
                return cfg["base_url"]
        for cfg in (raw.get("llm_providers") or []):
            if isinstance(cfg, dict) and cfg.get("base_url"):
                return cfg["base_url"]
        return (raw.get("llm") or {}).get("base_url", "")

    # ══════════════════════════════════════
    #  路由解析
    # ══════════════════════════════════════

    def resolve(self, ptype: str, provider_id: str = "") -> Optional[LLMProvider]:
        """解析指定类型的 Provider"""
        pool = self._pools.get(ptype, {})
        if provider_id and provider_id in pool:
            return pool[provider_id]
        routed_id = self._routing.get(ptype, "")
        if routed_id and routed_id in pool:
            return pool[routed_id]
        # 兜底：取第一个启用的
        for p in pool.values():
            if p.enabled:
                return p
        return None

    def resolve_chat(self, provider_id: str = "") -> Optional[LLMProvider]:
        return self.resolve(CHAT, provider_id)

    def resolve_vision(self) -> Optional[LLMProvider]:
        return self.resolve(VISION)

    def resolve_embedding(self) -> Optional[LLMProvider]:
        return self.resolve(EMBEDDING)

    def resolve_asr(self) -> Optional[LLMProvider]:
        return self.resolve(ASR)

    def resolve_image(self) -> Optional[LLMProvider]:
        return self.resolve(IMAGE)

    def get_routing(self) -> dict:
        """获取当前路由配置"""
        return dict(self._routing)

    def set_routing(self, ptype: str, provider_id: str) -> bool:
        """设置功能路由"""
        if ptype not in PROVIDER_TYPES:
            return False
        if provider_id and provider_id not in self._pools[ptype]:
            return False
        self._routing[ptype] = provider_id
        logger.info(f"[router] 路由变更: {ptype} → {provider_id or '(空)'}")
        return True

    @property
    def local_whisper(self) -> dict:
        return self._local_whisper

    @property
    def allow_llm_fallback(self) -> bool:
        return self._allow_llm_fallback

    # ══════════════════════════════════════
    #  Provider 管理（运行时增删改）
    # ══════════════════════════════════════

    def list_providers(self, ptype: str) -> List[dict]:
        """列出指定类型的所有 Provider（脱敏）"""
        pool = self._pools.get(ptype, {})
        return [p.get_info() for p in pool.values()]

    def get_provider_by_type(self, ptype: str, pid: str) -> Optional[LLMProvider]:
        """获取指定类型的指定 Provider"""
        return self._pools.get(ptype, {}).get(pid)

    def add_provider(self, ptype: str, config: dict) -> str:
        if ptype not in PROVIDER_TYPES:
            raise ValueError(f"未知 Provider 类型: {ptype}")
        pid = config.get("id") or f"{ptype}_{uuid.uuid4().hex[:8]}"
        if pid in self._pools[ptype]:
            raise ValueError(f"Provider ID 已存在: {pid}")
        self._pools[ptype][pid] = LLMProvider(pid, config)
        if not self._routing[ptype]:
            self._routing[ptype] = pid
        logger.info(f"[router] 已添加 {ptype} Provider: {pid}")
        return pid

    def update_provider(self, ptype: str, pid: str, config: dict) -> bool:
        pool = self._pools.get(ptype, {})
        old = pool.get(pid)
        if old is None:
            return False
        # 敏感字段留空时从旧实例继承
        new_config = {**config, "id": pid}
        if not new_config.get("api_key"):
            new_config["api_key"] = old.api_key
        if not new_config.get("base_url"):
            new_config["base_url"] = old.base_url
        pool[pid] = LLMProvider(pid, new_config)
        logger.info(f"[router] 已更新 {ptype} Provider: {pid}")
        return True

    def remove_provider(self, ptype: str, pid: str) -> bool:
        pool = self._pools.get(ptype, {})
        if pid not in pool:
            return False
        del pool[pid]
        if self._routing[ptype] == pid:
            self._routing[ptype] = next(iter(pool), "")
        logger.info(f"[router] 已删除 {ptype} Provider: {pid}")
        return True

    # ══════════════════════════════════════
    #  持久化
    # ══════════════════════════════════════

    def save_to_config(self) -> dict:
        """序列化为配置字典"""
        result = {}
        for ptype in PROVIDER_TYPES:
            key = CONFIG_KEYS[ptype]
            items = []
            for pid, p in self._pools[ptype].items():
                item = {
                    "id": pid,
                    "name": p.name,
                    "api_key": p.api_key,
                    "base_url": p.base_url,
                    "model": p.model,
                    "enabled": p.enabled,
                }
                if ptype == CHAT:
                    item["max_tokens"] = p.max_tokens
                    item["temperature"] = p.temperature
                if ptype == IMAGE:
                    item["default_size"] = getattr(p, "default_size", "1024x768")
                    item["timeout"] = getattr(p, "timeout", 120)
                items.append(item)
            # ASR 列表末尾追加 local_whisper
            if ptype == ASR and self._local_whisper:
                items.append(self._local_whisper)
            result[key] = items
        result["model_routing"] = dict(self._routing)
        result["allow_llm_fallback"] = self._allow_llm_fallback
        return result

    # ══════════════════════════════════════
    #  兼容层：让 LLMManager 旧代码继续工作
    # ══════════════════════════════════════

    def get_default(self) -> Optional[LLMProvider]:
        """兼容 LLMManager.get_default()"""
        return self.resolve_chat()

    def get_provider_by_id(self, llm_id: Optional[str] = None) -> Optional[LLMProvider]:
        """兼容 LLMManager.get_provider()"""
        return self.resolve_chat(llm_id or "")

    def list_chat_providers(self) -> List[dict]:
        """兼容 LLMManager.list_providers()"""
        return self.list_providers(CHAT)

    def get_default_id(self) -> str:
        return self._routing.get(CHAT, "")

    @property
    def _providers(self):
        """兼容 LLMManager._providers"""
        return self._pools[CHAT]

    @property
    def _default_id(self) -> str:
        return self._routing.get(CHAT, "")

    @_default_id.setter
    def _default_id(self, v: str):
        self._routing[CHAT] = v
