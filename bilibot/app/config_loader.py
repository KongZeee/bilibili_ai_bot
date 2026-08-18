"""
配置加载器

支持：
- 从 YAML 加载配置
- 配置验证
- 敏感字段脱敏
- 热重载
"""
import os
import math
import yaml
import copy
import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, Any, Mapping

logger = logging.getLogger("bilibot.config")


# 敏感字段列表（V1 精确路径）
SENSITIVE_PATHS = [
    "bilibili.sessdata",
    "bilibili.bili_jct",
    "bilibili.buvid3",
    "bilibili.buvid4",
    "bilibili.refresh_token",
    "llm.api_key",
    "llm.vision.api_key",
    "llm.embedding.api_key",
    "web_search.api_key",
    "web.secret_key",
    "web.admin_password",
]

# 敏感字段叶子名（V2 列表结构：accounts[]/llm_providers[] 内的字段，按字段名匹配）
SENSITIVE_LEAF_NAMES = {
    "sessdata",
    "bili_jct",
    "buvid3",
    "buvid4",
    "refresh_token",
    "api_key",
    "api_keys",
    "secret_key",
    "admin_password",
    # admin_username 非密钥：脱敏后面板配置页无法显示/编辑用户名
}

SENSITIVE_PLACEHOLDER = "***已配置***"
LEGACY_SENSITIVE_PLACEHOLDER = "__REDACTED__"
SENSITIVE_PLACEHOLDERS = frozenset({
    SENSITIVE_PLACEHOLDER,
    LEGACY_SENSITIVE_PLACEHOLDER,
})


def is_sensitive_placeholder(value: Any) -> bool:
    """Return whether *value* is one of the supported redaction markers."""
    return isinstance(value, str) and value in SENSITIVE_PLACEHOLDERS


def bili_credentials_are_configured(sessdata: Any, bili_jct: Any) -> bool:
    """Return whether both required Bilibili credentials are real values."""
    return bool(
        sessdata
        and bili_jct
        and not is_sensitive_placeholder(sessdata)
        and not is_sensitive_placeholder(bili_jct)
    )


@dataclass
class BiliConfig:
    """B站账号配置"""
    sessdata: str = ""
    bili_jct: str = ""
    dede_user_id: str = ""
    buvid3: str = ""
    buvid4: str = ""
    refresh_token: str = ""
    
    @property
    def is_authenticated(self) -> bool:
        return bili_credentials_are_configured(self.sessdata, self.bili_jct)


@dataclass
class LLMConfig:
    """LLM配置"""
    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "Qwen/Qwen2.5-72B-Instruct"
    # Default budget for reasoning chat models (agnes-class) that spend many
    # completion tokens before emitting visible content.
    max_tokens: int = 1500
    temperature: float = 0.8
    
    # Vision
    vision_enabled: bool = False
    vision_api_key: str = ""
    vision_base_url: str = "https://api.siliconflow.cn/v1"
    vision_model: str = ""
    
    # Embedding
    embedding_enabled: bool = False
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"


@dataclass
class WebConfig:
    """Web面板配置"""
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080
    secret_key: str = "change-this-to-a-random-string"
    admin_username: str = "admin"
    admin_password: str = "admin123"
    session_ttl_seconds: int = 3600
    cors_origins: list = field(default_factory=list)
    # BUG F-009：生产环境默认 secure=True，HTTP 下明文传输 cookie 有被窃取风险
    secure_cookies: bool = True
    # Task 5：可信代理列表，仅当直连 IP 在此列表中时才信任 X-Forwarded-For
    trusted_proxies: list = field(default_factory=list)


@dataclass
class ProactiveConfig:
    """主动行为配置

    PRD V4 CFG-003/Phase 5 清理：
    - like/coin/favorite/comment 已迁移到 interactions.* 段，运行时由 InteractionPolicyEngine 消费
    - bangumi 迁移到 features.bangumi
    - follow/special_follow/enabled 无运行时消费者，已移除
    - 仅保留调度计划字段（scheduler 从 raw config 读取）
    """
    video_count: int = 2
    video_times: list = field(default_factory=lambda: ["10:00", "18:00", "22:00"])
    dynamic_count: int = 1
    dynamic_times: list = field(default_factory=lambda: ["12:00", "20:00"])


@dataclass
class ReplyConfig:
    """评论回复配置"""
    auto_reply: bool = True
    block_keywords: list = field(default_factory=lambda: [
        "傻逼", "草泥马", "滚", "死", "废物", "智障", "脑残"
    ])
    min_comment_length: int = 2
    reply_own: bool = False


@dataclass
class MemoryConfig:
    """V6 账号级统一记忆大脑配置。

    V5 的容量、TTL 和遗忘字段只保留用于读取旧配置；V6 不消费这些
    字段，也不会自动停用、裁剪或删除任何记忆。
    """
    # V5 deprecated compatibility fields
    max_today: int = 50            # today 级别上限
    max_recent: int = 200          # recent 级别上限
    max_long_term: int = 1000      # long_term 级别上限
    enable_forgetting: bool = True  # 启用遗忘
    forgetting_score: float = 3.0   # 遗忘阈值（1-10，importance_score < 该值/10 时优先遗忘）
    # 压缩与检索参数
    thread_compress_threshold: int = 8
    oid_compress_threshold: int = 20
    oid_keep_recent: int = 8
    user_compress_threshold: int = 20
    user_keep_recent: int = 5
    max_semantic_results: int = 3
    consolidation_discard_threshold: int = 3  # 日终清算丢弃阈值
    recent_promote_days: int = 14  # recent → long_term 升级阈值
    long_term_age_days: int = 180  # MEM-606：长期记忆最大保留天数（过期清理阈值）
    # V6 archive and recall contract
    recall_candidate_limit: int = 12
    recall_inject_limit: int = 5
    recall_association_limit: int = 2
    rerank_timeout_seconds: float = 8.0
    recall_total_timeout_seconds: float = 10.0
    rerank_relevance_baseline: float = 0.65
    # Fallback-only deterministic gates. Conversational Chinese queries with
    # multiple distinctive terms often land ~0.43 lexical_coverage, so the
    # default is intentionally lower than PRD-V6's illustrative 0.80/0.88;
    # operators can raise them back through these explicit knobs.
    fallback_direct_threshold: float = 0.40
    fallback_association_threshold: float = 0.55
    # Dedicated cross-encoder (BGE-style) baseline. Its 0..1 score scale is
    # softer than the chat-JSON 0..100-normalized scores, so it gets its own knob.
    rerank_model_relevance_baseline: float = 0.20
    enrichment_chat_timeout_seconds: float = 12.0
    link_enrichment_timeout_seconds: float = 60.0
    enrichment_worker_concurrency: int = 2
    link_candidate_limit: int = 12
    link_job_max_attempts: int = 3
    prompt_char_budget: int = 5000
    chunk_target_chars: int = 600
    chunk_hard_chars: int = 900
    chunk_target_tokens: int = 450
    chunk_hard_tokens: int = 700
    chunk_overlap_chars: int = 100
    job_max_attempts: int = 8
    vector_cache_limit: int = 50000
    # 向量全表扫描行数预算：超过时向量通道报错降级 FTS，避免 100GB 级库拖死在线回复
    vector_full_scan_row_limit: int = 50_000
    vector_batch_size: int = 2048
    # 大库召回策略：只对 FTS/标识符/图扩展先命中的事件做向量精排（默认关闭）
    vector_candidate_prefilter: bool = False


def validate_memory_config_values(config: MemoryConfig | Mapping[str, Any]) -> None:
    """Reject V6 memory settings that cannot satisfy the runtime contract."""

    def value(name: str, default: Any) -> Any:
        if isinstance(config, Mapping):
            return config.get(name, default)
        return getattr(config, name, default)

    defaults = MemoryConfig()

    def integer(name: str, *, minimum: int, maximum: int | None = None) -> int:
        raw = value(name, getattr(defaults, name))
        if isinstance(raw, bool):
            raise ValueError(f"memory.{name} must be an integer")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"memory.{name} must be an integer") from exc
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"memory.{name} must be an integer")
        result = int(number)
        if result < minimum or (maximum is not None and result > maximum):
            suffix = f"..{maximum}" if maximum is not None else " or greater"
            raise ValueError(f"memory.{name} must be {minimum}{suffix}")
        return result

    def number(name: str, *, minimum: float, maximum: float | None = None) -> float:
        raw = value(name, getattr(defaults, name))
        if isinstance(raw, bool):
            raise ValueError(f"memory.{name} must be a number")
        try:
            result = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"memory.{name} must be a number") from exc
        if not math.isfinite(result) or result < minimum or (
            maximum is not None and result > maximum
        ):
            suffix = f"..{maximum:g}" if maximum is not None else " or greater"
            raise ValueError(f"memory.{name} must be {minimum:g}{suffix}")
        return result

    candidate_limit = integer("recall_candidate_limit", minimum=1, maximum=12)
    inject_limit = integer("recall_inject_limit", minimum=1, maximum=5)
    association_limit = integer("recall_association_limit", minimum=0, maximum=2)
    if inject_limit > candidate_limit:
        raise ValueError("memory.recall_inject_limit cannot exceed recall_candidate_limit")
    if association_limit > inject_limit:
        raise ValueError("memory.recall_association_limit cannot exceed recall_inject_limit")

    rerank_timeout = number("rerank_timeout_seconds", minimum=0.5, maximum=60.0)
    total_timeout = number("recall_total_timeout_seconds", minimum=1.0, maximum=120.0)
    if rerank_timeout > total_timeout:
        raise ValueError(
            "memory.rerank_timeout_seconds cannot exceed recall_total_timeout_seconds"
        )
    number("enrichment_chat_timeout_seconds", minimum=1.0, maximum=600.0)
    number("link_enrichment_timeout_seconds", minimum=1.0, maximum=600.0)
    integer("enrichment_worker_concurrency", minimum=1, maximum=4)
    integer("link_candidate_limit", minimum=4, maximum=24)

    baseline = value("rerank_relevance_baseline", defaults.rerank_relevance_baseline)
    if isinstance(baseline, bool):
        raise ValueError("memory.rerank_relevance_baseline must be a number from 0 to 1")
    try:
        baseline = float(baseline)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "memory.rerank_relevance_baseline must be a number from 0 to 1"
        ) from exc
    if not math.isfinite(baseline) or not 0 <= baseline <= 1:
        raise ValueError("memory.rerank_relevance_baseline must be a number from 0 to 1")

    model_baseline = value(
        "rerank_model_relevance_baseline",
        defaults.rerank_model_relevance_baseline,
    )
    if isinstance(model_baseline, bool):
        raise ValueError(
            "memory.rerank_model_relevance_baseline must be a number from 0 to 1"
        )
    try:
        model_baseline = float(model_baseline)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "memory.rerank_model_relevance_baseline must be a number from 0 to 1"
        ) from exc
    if not math.isfinite(model_baseline) or not 0 <= model_baseline <= 1:
        raise ValueError(
            "memory.rerank_model_relevance_baseline must be a number from 0 to 1"
        )

    fallback_direct = number(
        "fallback_direct_threshold", minimum=0.0, maximum=1.0
    )
    fallback_association = number(
        "fallback_association_threshold", minimum=0.0, maximum=1.0
    )
    if fallback_association < fallback_direct:
        raise ValueError(
            "memory.fallback_association_threshold cannot be smaller than "
            "memory.fallback_direct_threshold"
        )

    integer("prompt_char_budget", minimum=512, maximum=5000)
    target_chars = integer("chunk_target_chars", minimum=1)
    hard_chars = integer("chunk_hard_chars", minimum=1)
    target_tokens = integer("chunk_target_tokens", minimum=1)
    hard_tokens = integer("chunk_hard_tokens", minimum=1)
    overlap_chars = integer("chunk_overlap_chars", minimum=0)
    if hard_chars < target_chars:
        raise ValueError("memory.chunk_hard_chars cannot be smaller than chunk_target_chars")
    if hard_tokens < target_tokens:
        raise ValueError("memory.chunk_hard_tokens cannot be smaller than chunk_target_tokens")
    if overlap_chars * 5 > target_chars:
        raise ValueError("memory.chunk_overlap_chars cannot exceed 20% of chunk_target_chars")

    integer("job_max_attempts", minimum=1)
    link_attempts = integer("link_job_max_attempts", minimum=1)
    if link_attempts > integer("job_max_attempts", minimum=1):
        raise ValueError("memory.link_job_max_attempts cannot exceed job_max_attempts")
    integer("vector_cache_limit", minimum=0)
    integer("vector_full_scan_row_limit", minimum=0)
    integer("vector_batch_size", minimum=1)
    prefilter = value("vector_candidate_prefilter", defaults.vector_candidate_prefilter)
    if not isinstance(prefilter, bool):
        raise ValueError("memory.vector_candidate_prefilter must be a boolean")


@dataclass
class PersonalityConfig:
    """人格基础配置（UserStateSystem 等模块依赖）"""
    owner_mid: str = ""
    owner_name: str = "主人"
    enable_mood: bool = True


@dataclass
class FeaturesConfig:
    """功能开关（PRD §5.2 集中管理）"""
    reply_comment: bool = True       # 评论回复
    private_message: bool = False    # 私信
    proactive_video: bool = True     # 主动看视频
    proactive_comment: bool = True   # 主动评论
    dynamic_post: bool = True        # 动态发布
    bangumi: bool = False            # 番剧追更
    weekly_summary: bool = True      # 周总结
    web_search: bool = False         # 联网搜索
    affection: bool = True           # 好感度系统
    mood: bool = True                # 心情系统


@dataclass
class DynamicPublishConfig:
    """动态发布配置（PRD §5.2 / §5.6）"""
    topics: list = field(default_factory=list)       # 主题池
    with_image: bool = False                          # 是否配图
    image_style: str = "cinematic"                   # 动态配图风格
    image_style_custom: str = ""                     # 自定义风格描述
    review_before_publish: bool = False               # 发布前审核


@dataclass
class SafetyConfig:
    """安全与审核配置（PRD §5.9）"""
    rate_limit_per_minute: int = 5
    rate_limit_per_hour: int = 50
    rate_limit_per_day: int = 200
    content_check_enabled: bool = True
    min_content_length: int = 2
    max_content_length: int = 2000
    similarity_threshold: float = 0.8


class ConfigLoader:
    """配置管理器"""
    
    def __init__(self, config_dict: dict = None, filepath: str = None):
        self._raw_config: Dict[str, Any] = config_dict or {}
        self._original_config: Dict[str, Any] = copy.deepcopy(self._raw_config)
        # Task 23：记录配置文件路径，供 reload() 无参时从磁盘重新读取
        self.filepath = filepath
        # 多账号凭据写盘串行化（buvid/cookie 刷新回调与 Web PATCH 共用）
        self._write_lock = threading.RLock()
        self._apply_config()
    
    def _apply_config(self):
        """应用配置到各子模块"""
        bili = self._raw_config.get("bilibili", {})
        self.bilibili = BiliConfig(
            sessdata=bili.get("sessdata", ""),
            bili_jct=bili.get("bili_jct", ""),
            dede_user_id=bili.get("dede_user_id", ""),
            buvid3=bili.get("buvid3", ""),
            buvid4=bili.get("buvid4", ""),
            refresh_token=bili.get("refresh_token", ""),
        )
        
        llm = self._raw_config.get("llm", {})
        vision = llm.get("vision", {})
        embedding = llm.get("embedding", {})
        
        self.llm = LLMConfig(
            api_key=llm.get("api_key", ""),
            base_url=llm.get("base_url", "https://api.siliconflow.cn/v1"),
            model=llm.get("model", "Qwen/Qwen2.5-72B-Instruct"),
            max_tokens=llm.get("max_tokens", 1024),
            temperature=llm.get("temperature", 0.8),
            vision_enabled=vision.get("enabled", False),
            vision_api_key=vision.get("api_key", ""),
            vision_base_url=vision.get("base_url", "https://api.siliconflow.cn/v1"),
            vision_model=vision.get("model", ""),
            embedding_enabled=embedding.get("enabled", False),
            embedding_api_key=embedding.get("api_key", ""),
            embedding_base_url=embedding.get("base_url", "https://api.siliconflow.cn/v1"),
            embedding_model=embedding.get("model", "BAAI/bge-m3"),
        )
        
        web = self._raw_config.get("web", {})
        self.web = WebConfig(
            enabled=web.get("enabled", True),
            host=web.get("host", "127.0.0.1"),
            port=web.get("port", 8080),
            secret_key=web.get("secret_key", "change-this-to-a-random-string"),
            admin_username=web.get("admin_username", "admin"),
            admin_password=web.get("admin_password", "admin123"),
            session_ttl_seconds=web.get("session_ttl_seconds", 3600),
            cors_origins=web.get("cors_origins", []),
            # BUG F-009：默认 secure=True（生产应 HTTPS），但允许显式关闭
            secure_cookies=web.get("secure_cookies", True),
            # Task 5：可信代理列表
            trusted_proxies=web.get("trusted_proxies", []),
        )
        
        prov = self._raw_config.get("proactive", {})
        self.proactive = ProactiveConfig(
            video_count=prov.get("video_count", 2),
            video_times=prov.get("video_times", ["10:00", "18:00", "22:00"]),
            dynamic_count=prov.get("dynamic_count", 1),
            dynamic_times=prov.get("dynamic_times", ["12:00", "20:00"]),
        )
        
        reply = self._raw_config.get("reply", {})
        # block_keywords 类型守卫：兼容前端传来的逗号分隔字符串
        _raw_bkw = reply.get("block_keywords", ["傻逼", "草泥马", "滚", "死", "废物", "智障", "脑残"])
        _bkw = _raw_bkw if isinstance(_raw_bkw, list) else str(_raw_bkw).replace("，", ",").split(",")
        self.reply = ReplyConfig(
            auto_reply=reply.get("auto_reply", True),
            block_keywords=_bkw,
            min_comment_length=reply.get("min_comment_length", 2),
            reply_own=reply.get("reply_own", False),
        )
        
        mem = self._raw_config.get("memory", {})
        # 兼容旧版嵌套结构：memory.consolidation.recent_promote_days
        consol = mem.get("consolidation", {}) if isinstance(mem.get("consolidation"), dict) else {}
        self.memory = MemoryConfig(
            # PRD V5 Task 16：容量与遗忘策略
            max_today=mem.get("max_today", 50),
            max_recent=mem.get("max_recent", 200),
            max_long_term=mem.get("max_long_term", 1000),
            enable_forgetting=mem.get("enable_forgetting", True),
            forgetting_score=mem.get("forgetting_score", 3.0),
            # 压缩与检索参数
            thread_compress_threshold=mem.get("thread_compress_threshold", 8),
            oid_compress_threshold=mem.get("oid_compress_threshold", 20),
            oid_keep_recent=mem.get("oid_keep_recent", 8),
            user_compress_threshold=mem.get("user_compress_threshold", 20),
            user_keep_recent=mem.get("user_keep_recent", 5),
            max_semantic_results=mem.get("max_semantic_results", 3),
            consolidation_discard_threshold=mem.get("consolidation_discard_threshold", consol.get("discard_threshold", 3)),
            recent_promote_days=mem.get("recent_promote_days", consol.get("recent_promote_days", 14)),
            long_term_age_days=mem.get("long_term_age_days", consol.get("long_term_age_days", 180)),
            recall_candidate_limit=mem.get("recall_candidate_limit", 12),
            recall_inject_limit=mem.get("recall_inject_limit", 5),
            recall_association_limit=mem.get("recall_association_limit", 2),
            rerank_timeout_seconds=mem.get("rerank_timeout_seconds", 8.0),
            recall_total_timeout_seconds=mem.get("recall_total_timeout_seconds", 10.0),
            rerank_relevance_baseline=mem.get("rerank_relevance_baseline", 0.65),
            fallback_direct_threshold=mem.get("fallback_direct_threshold", 0.40),
            fallback_association_threshold=mem.get(
                "fallback_association_threshold", 0.55
            ),
            rerank_model_relevance_baseline=mem.get(
                "rerank_model_relevance_baseline", 0.20
            ),
            enrichment_chat_timeout_seconds=mem.get(
                "enrichment_chat_timeout_seconds", 12.0
            ),
            link_enrichment_timeout_seconds=mem.get(
                "link_enrichment_timeout_seconds", 60.0
            ),
            enrichment_worker_concurrency=mem.get(
                "enrichment_worker_concurrency", 2
            ),
            link_candidate_limit=mem.get("link_candidate_limit", 12),
            link_job_max_attempts=mem.get("link_job_max_attempts", 3),
            prompt_char_budget=mem.get("prompt_char_budget", 5000),
            chunk_target_chars=mem.get("chunk_target_chars", 600),
            chunk_hard_chars=mem.get("chunk_hard_chars", 900),
            chunk_target_tokens=mem.get("chunk_target_tokens", 450),
            chunk_hard_tokens=mem.get("chunk_hard_tokens", 700),
            chunk_overlap_chars=mem.get("chunk_overlap_chars", 100),
            job_max_attempts=mem.get("job_max_attempts", 8),
            vector_cache_limit=mem.get("vector_cache_limit", 50000),
            vector_full_scan_row_limit=mem.get("vector_full_scan_row_limit", 50_000),
            vector_batch_size=mem.get("vector_batch_size", 2048),
            vector_candidate_prefilter=mem.get("vector_candidate_prefilter", False),
        )
        validate_memory_config_values(self.memory)

        pers = self._raw_config.get("personality", {})
        self.personality = PersonalityConfig(
            owner_mid=pers.get("owner_mid", ""),
            owner_name=pers.get("owner_name", "主人"),
            enable_mood=pers.get("enable_mood", True),
        )
        
        # 功能开关（PRD §5.2 集中管理）
        feat = self._raw_config.get("features", {})
        self.features = FeaturesConfig(
            reply_comment=feat.get("reply_comment", True),
            private_message=feat.get("private_message", False),
            proactive_video=feat.get("proactive_video", True),
            proactive_comment=feat.get("proactive_comment", True),
            dynamic_post=feat.get("dynamic_post", True),
            bangumi=feat.get("bangumi", False),
            weekly_summary=feat.get("weekly_summary", True),
            web_search=feat.get("web_search", False),
            affection=feat.get("affection", True),
            mood=feat.get("mood", True),
        )
        
        # 动态发布（PRD §5.2 / §5.6）
        dyn = self._raw_config.get("dynamic_publish", {})
        self.dynamic_publish = DynamicPublishConfig(
            topics=dyn.get("topics", []),
            with_image=dyn.get("with_image", False),
            image_style=dyn.get("image_style", "cinematic"),
            image_style_custom=dyn.get("image_style_custom", ""),
            review_before_publish=dyn.get("review_before_publish", False),
        )
        
        # 安全与审核（PRD §5.9）
        safe = self._raw_config.get("safety", {})
        self.safety = SafetyConfig(
            rate_limit_per_minute=safe.get("rate_limit_per_minute", 5),
            rate_limit_per_hour=safe.get("rate_limit_per_hour", 50),
            rate_limit_per_day=safe.get("rate_limit_per_day", 200),
            content_check_enabled=safe.get("content_check_enabled", True),
            min_content_length=safe.get("min_content_length", 2),
            max_content_length=safe.get("max_content_length", 2000),
            similarity_threshold=safe.get("similarity_threshold", 0.8),
        )
        
        self.data_dir = self._raw_config.get("data_dir", "./data")
    
    def get_raw_config(self) -> Dict[str, Any]:
        """获取原始配置字典（深拷贝，调用方修改不影响内存配置）"""
        return copy.deepcopy(self._raw_config)

    def get(self, path: str, default: Any = None) -> Any:
        """通过点分路径读取配置值，例如 get("web.port", 8080)"""
        if not path:
            return default
        current: Any = self._raw_config
        for key in path.split("."):
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    def patch_account_credentials(
        self,
        account_id: str,
        patch: Dict[str, Any],
        filepath: str = None,
    ) -> bool:
        """原子更新账号凭据并写盘（多账号安全）

        在锁内直接改 _raw_config，再 save，避免 get_raw_config 深拷贝导致
        并发刷新时后写覆盖先写。

        Args:
            account_id: 目标账号 id；空或找不到时写 V1 bilibili 段
            patch: 要合并的字段（sessdata/bili_jct/buvid*/refresh_token 等）
            filepath: 配置路径；默认 self.filepath。两者都为空时仅更新内存，
                禁止猜测当前工作目录下的 config.yaml。

        Returns:
            True 如果至少写入了一个字段
        """
        if not isinstance(patch, dict) or not patch:
            return False
        path = filepath or self.filepath
        with self._write_lock:
            written = False
            accounts = self._raw_config.get("accounts")
            if account_id and isinstance(accounts, list):
                for acc in accounts:
                    if isinstance(acc, dict) and acc.get("id") == account_id:
                        for k, v in patch.items():
                            if v is None or v == "":
                                continue
                            acc[k] = v
                            written = True
                        break
            if not written:
                bili = self._raw_config.setdefault("bilibili", {})
                if not isinstance(bili, dict):
                    bili = {}
                    self._raw_config["bilibili"] = bili
                for k, v in patch.items():
                    if v is None or v == "":
                        continue
                    bili[k] = v
                    written = True
            if not written:
                return False
            # ConfigLoader 也可作为纯内存配置用于测试、嵌入式调用和预览。
            # 此时猜测 cwd/config.yaml 会覆盖调用者的真实生产配置；只同步内存。
            if not path:
                self._original_config = copy.deepcopy(self._raw_config)
                self._apply_config()
                logger.warning(
                    "配置加载器未绑定文件路径，账号凭据仅更新内存，未写入磁盘"
                )
                return True
            # 在锁内保存：传入当前内存配置的快照，避免 save 再被并发改
            self.save_config(copy.deepcopy(self._raw_config), path)
            return True

    def save_config(self, config: Dict[str, Any], filepath: str):
        """保存配置到文件（原子写入：先写临时文件再替换）

        PRD V5 CFG-502：统一 config_revision 递增。
        - 若调用方已显式设置 config_revision（如 PATCH /api/config），保留其值。
        - 若调用方未设置（如专用 API accounts/llm_providers/video_analysis 等），
          自动递增，确保所有配置变更都推进统一版本号，不绕过乐观锁。
        """
        with self._write_lock:
            current_rev = self._raw_config.get("config_revision", 0)
            new_rev = config.get("config_revision", current_rev)
            if new_rev == current_rev:
                config["config_revision"] = int(current_rev) + 1

            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            tmp_path = filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            # 原子替换（Windows 下 os.replace 同样可用）
            os.replace(tmp_path, filepath)
            # BUG A-003：限制配置文件权限，防止凭证泄露给同主机其他用户
            try:
                os.chmod(filepath, 0o600)
            except Exception:
                logger.warning("无法设置配置文件权限为 0o600，凭证可能被其他用户读取")
            self._raw_config = config
            self._original_config = copy.deepcopy(config)
            # Task 23：同步记录配置文件路径，供后续 reload() 无参时从磁盘重新读取
            self.filepath = filepath
            self._apply_config()

    def atomic_update(self, filepath: str, mutator) -> Dict[str, Any]:
        """线程安全的读-改-写：在写锁内深拷贝 → mutator(raw) → save_config。

        专用 API（video_analysis / model_routing / accounts 等）应使用本方法，
        避免并发 PATCH 各自 get_raw_config 后后写覆盖先写导致配置段丢失。

        mutator(raw) 可就地修改 raw。若 mutator 抛出异常，不写盘、不改内存。
        返回最终写入的 raw 副本。
        """
        if not callable(mutator):
            raise TypeError("mutator must be callable")
        with self._write_lock:
            raw = copy.deepcopy(self._raw_config)
            mutator(raw)
            self.save_config(raw, filepath)
            return copy.deepcopy(self._raw_config)

    def reload(self, config: Dict[str, Any] = None):
        """热重载配置

        Task 23：无参时从 self.filepath 重新读取 YAML 文件，
        使 /api/config/reload 真正从磁盘加载最新配置，而非仅重新应用内存配置。
        """
        if config:
            self._raw_config = config
            self._original_config = copy.deepcopy(config)
        else:
            # 无参时从磁盘重新加载
            with open(self.filepath, "r", encoding="utf-8") as f:
                self._raw_config = yaml.safe_load(f) or {}
            self._original_config = copy.deepcopy(self._raw_config)
        self._apply_config()
        logging.getLogger("bilibot").info("配置已热重载")
    
    def mask_sensitive(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """脱敏敏感字段

        支持两种匹配方式：
        1. V1 精确路径匹配（SENSITIVE_PATHS）
        2. V2 列表结构按叶子字段名匹配（SENSITIVE_LEAF_NAMES），用于 accounts[]/llm_providers[]
        """
        if config is None:
            config = self._raw_config

        def mask_recursive(data, prefix: str = ""):
            # dict 递归
            if isinstance(data, dict):
                result = {}
                for key, value in data.items():
                    path = f"{prefix}.{key}" if prefix else key
                    if key == "api_keys" and isinstance(value, list):
                        # Preserve count; never leak key material.
                        result[key] = [
                            SENSITIVE_PLACEHOLDER
                            for item in value
                            if isinstance(item, str) and item.strip()
                            and item not in SENSITIVE_PLACEHOLDERS
                        ]
                    elif path in SENSITIVE_PATHS or key in SENSITIVE_LEAF_NAMES:
                        if value and value != SENSITIVE_PLACEHOLDER:
                            result[key] = SENSITIVE_PLACEHOLDER
                        else:
                            result[key] = value
                    elif isinstance(value, dict):
                        result[key] = mask_recursive(value, path)
                    elif isinstance(value, list):
                        result[key] = [mask_recursive(item, path) for item in value]
                    else:
                        result[key] = value
                return result
            # list 递归（V2 列表项）
            if isinstance(data, list):
                return [mask_recursive(item, prefix) for item in data]
            return data

        return mask_recursive(config)
