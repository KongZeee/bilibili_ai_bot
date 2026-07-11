"""
配置 API 路由

提供配置的完整管理 API：
- GET /api/config/schema - 获取配置 schema
- GET /api/config/full - 获取完整配置（脱敏）
- PATCH /api/config - 更新配置（基于 schema 类型规范化）
- POST /api/config/validate - 验证配置
- POST /api/config/reload - 热重载
"""
import logging
import copy
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("bilibot.api.config")

# 敏感字段列表
SENSITIVE_FIELDS = {
    "bilibili.sessdata",
    "bilibili.bili_jct",
    "bilibili.buvid3",
    "bilibili.refresh_token",
    "llm.api_key",
    "llm.vision.api_key",
    "llm.embedding.api_key",
    "web_search.api_key",
    "web.secret_key",
    "web.admin_password",
    # 图片生成 / 视频理解 ASR
    "image_generation.api_key",
    "video_analysis.asr.api_key",
}

SENSITIVE_PLACEHOLDER = "***已配置***"


class ConfigValidationError(ValueError):
    """配置类型规范化失败"""


# PRD V5 CFG-501：对象数组字段（数组项必须是 dict）
# 这些字段禁止用逗号分隔的字符串数组提交，必须由专用页面/API 管理
OBJECT_ARRAY_FIELDS = {"profiles", "accounts", "llm_providers"}


# PRD V5 CFG-502 §11.3：热重载契约
# 每个配置字段声明的生效级别：
#   immediate       — 立即生效（调用组件 reload_config）
#   next_task       — 下个任务周期生效（调度器重建 TaskRun）
#   restart_account — 需重启账号实例
#   restart_app     — 需重启应用
RELOAD_CONTRACT = {
    "reply": "immediate",
    "web_search": "immediate",
    "safety": "immediate",
    "interactions": "immediate",
    "personality": "immediate",
    "features": "immediate",
    "dynamic_publish": "immediate",
    "memory": "next_task",
    "proactive": "next_task",
    "video_analysis": "next_task",
    "image_generation": "next_task",
    "accounts": "restart_account",
    "llm_providers": "restart_account",
    "profiles": "restart_account",
    "data_dir": "restart_app",
    "web": "restart_app",
    "logging": "immediate",
    "llm": "immediate",
    "bilibili": "immediate",
}

# accounts 子字段级别的特殊契约（覆盖 accounts 整体的 restart_account）
ACCOUNT_FIELD_CONTRACT = {
    "cookie": "immediate",
    "sessdata": "immediate",
    "bili_jct": "immediate",
    "buvid3": "immediate",
    "refresh_token": "immediate",
    "llm_id": "restart_account",
    "persona_id": "restart_account",
    "profile_id": "restart_account",
    "enabled": "restart_account",
}

# web 子字段级别契约
WEB_FIELD_CONTRACT = {
    "host": "restart_app",
    "port": "restart_app",
    "enabled": "restart_app",
    "secret_key": "restart_app",
    "admin_username": "restart_app",
    "admin_password": "restart_app",
    "session_ttl_seconds": "restart_app",
    "cors_origins": "immediate",
    "secure_cookies": "restart_app",
}

# PRD V5 Task 16：memory 子字段级别契约
# - max_today/max_recent/enable_forgetting/forgetting_score：下个任务周期生效
#   （KnowledgeBaseMemory.apply_memory_config 在调度器下个周期拾取）
# - max_long_term：需重启账号实例（涉及记忆存储初始化参数）
MEMORY_FIELD_CONTRACT = {
    "max_today": "next_task",
    "max_recent": "next_task",
    "max_long_term": "restart_account",
    "enable_forgetting": "next_task",
    "forgetting_score": "next_task",
}

# PRD V5 Task 16：memory 配置字段消费映射（schema_path → owner → reload_level → test_id）
# 用于断言每个暴露给用户的配置字段都有真实消费者，避免"幽灵配置"。
MEMORY_CONFIG_FIELD_MAP = {
    "memory.max_today": {
        "owner": "KnowledgeBaseMemory.__init__ / apply_memory_config",
        "reload_level": "next_task",
        "test_id": "test_max_today_consumed_by_knowledge_memory",
    },
    "memory.max_recent": {
        "owner": "KnowledgeBaseMemory.__init__ / apply_memory_config",
        "reload_level": "next_task",
        "test_id": "test_max_recent_consumed_by_knowledge_memory",
    },
    "memory.max_long_term": {
        "owner": "KnowledgeBaseMemory.__init__ / forget_low_importance",
        "reload_level": "restart_account",
        "test_id": "test_max_long_term_consumed_by_knowledge_memory",
    },
    "memory.enable_forgetting": {
        "owner": "KnowledgeBaseMemory.forget_low_importance",
        "reload_level": "next_task",
        "test_id": "test_enable_forgetting_consumed_by_knowledge_memory",
    },
    "memory.forgetting_score": {
        "owner": "KnowledgeBaseMemory.forget_low_importance",
        "reload_level": "next_task",
        "test_id": "test_forgetting_score_consumed_by_knowledge_memory",
    },
}


def increment_config_revision(config_loader, config_path: str) -> int:
    """PRD V5 CFG-502：显式递增统一 config_revision 并持久化。

    用于 PATCH /api/config 主端点。专用 API 由 save_config 自动递增。
    """
    raw = config_loader.get_raw_config()
    current = int(raw.get("config_revision", 0) or 0)
    raw["config_revision"] = current + 1
    config_loader.save_config(raw, config_path)
    return raw["config_revision"]


def _resolve_reload_level(field_key: str, sub_field: str = "") -> str:
    """解析某个配置字段的生效级别。

    Args:
        field_key: 顶层字段名（如 "reply", "accounts", "web"）
        sub_field: 子字段名（如 accounts 的 "cookie", web 的 "port", memory 的 "max_today"）
    """
    if field_key == "accounts" and sub_field:
        return ACCOUNT_FIELD_CONTRACT.get(sub_field, "restart_account")
    if field_key == "web" and sub_field:
        return WEB_FIELD_CONTRACT.get(sub_field, "immediate")
    # PRD V5 Task 16：memory 子字段精确生效级别
    if field_key == "memory" and sub_field:
        return MEMORY_FIELD_CONTRACT.get(sub_field, "next_task")
    return RELOAD_CONTRACT.get(field_key, "immediate")


def _find_changed_fields(original: dict, updated: dict) -> list:
    """找出 original → updated 之间变化的字段路径列表。

    返回 (field_key, sub_field) 元组列表：
    - 顶层字段变更：("reply", "")
    - accounts 子字段变更：("accounts", "cookie") / ("accounts", "llm_id")
    - web 子字段变更：("web", "port")
    - memory 子字段变更：("memory", "max_today") / ("memory", "enable_forgetting")
    """
    changed = []
    seen = set()

    for key in updated:
        if key.startswith("_") or key == "config_revision":
            continue
        if original.get(key) != updated.get(key):
            if key == "accounts" and isinstance(updated.get(key), list):
                old_map = {a.get("id"): a for a in original.get(key, []) if isinstance(a, dict)}
                new_list = updated.get(key, [])
                for acc in new_list:
                    if not isinstance(acc, dict):
                        continue
                    acc_id = acc.get("id", "")
                    old_acc = old_map.get(acc_id, {})
                    for sub in acc:
                        if sub in ("id",):
                            continue
                        if old_acc.get(sub) != acc.get(sub):
                            entry = ("accounts", sub)
                            if entry not in seen:
                                changed.append(entry)
                                seen.add(entry)
            elif key == "web" and isinstance(updated.get(key), dict):
                old_web = original.get(key, {}) if isinstance(original.get(key), dict) else {}
                new_web = updated.get(key, {})
                for sub in new_web:
                    if old_web.get(sub) != new_web.get(sub):
                        entry = ("web", sub)
                        if entry not in seen:
                            changed.append(entry)
                            seen.add(entry)
            elif key == "memory" and isinstance(updated.get(key), dict):
                # PRD V5 Task 16：memory 子字段级别变更检测
                old_mem = original.get(key, {}) if isinstance(original.get(key), dict) else {}
                new_mem = updated.get(key, {})
                for sub in new_mem:
                    if old_mem.get(sub) != new_mem.get(sub):
                        entry = ("memory", sub)
                        if entry not in seen:
                            changed.append(entry)
                            seen.add(entry)
            else:
                entry = (key, "")
                if entry not in seen:
                    changed.append(entry)
                    seen.add(entry)
    return changed


def _get_nested_value(data: dict, path: str, default=None):
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
    return current if current is not None else default


def _set_nested_value(data: dict, path: str, value):
    keys = path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def _is_sensitive_path(path: str) -> bool:
    return path in SENSITIVE_FIELDS


def _normalize_scalar(field_type: str, value, path: str, item_type: str = "string"):
    """根据 schema field.type 规范化单个值

    PRD V5 CFG-501：
    - item_type="object" 的数组：禁止字符串/标量项，仅接受 dict 列表
    - item_type="string"（默认）：保持逗号分隔解析行为
    """
    if value is None:
        return None

    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "on", "yes"):
                return True
            if v in ("false", "0", "off", "no", ""):
                return False
        return bool(value)

    if field_type == "array":
        # PRD V5 CFG-501：对象数组禁止字符串/标量化
        if item_type == "object":
            if isinstance(value, str):
                # 字符串（含逗号分隔）禁止直接转对象数组
                raise ConfigValidationError(
                    f"{path} 是对象数组，禁止用字符串提交，请使用专用管理页面"
                )
            if isinstance(value, list):
                for idx, item in enumerate(value):
                    if not isinstance(item, dict):
                        raise ConfigValidationError(
                            f"{path}[{idx}] 必须是对象，得到 {type(item).__name__}，"
                            f"请使用专用管理页面编辑"
                        )
                return value
            if isinstance(value, dict):
                # 单个 dict 包装成单元素数组（兼容前端误传）
                return [value]
            raise ConfigValidationError(
                f"{path} 是对象数组，得到 {type(value).__name__}"
            )

        # 字符串数组：保留旧的逗号分隔行为
        if isinstance(value, list):
            cleaned = []
            for item in value:
                if isinstance(item, str):
                    item = item.strip()
                if item not in (None, ""):
                    cleaned.append(item)
            return cleaned
        if isinstance(value, str):
            s = value.replace("，", ",")
            if s.strip() == "":
                return []
            return [item.strip() for item in s.split(",") if item.strip()]
        return [value]

    if field_type == "number":
        if isinstance(value, bool):
            # bool 是 int 的子类，先排除
            raise ConfigValidationError(f"{path} 必须是数字，得到 boolean")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            s = value.strip()
            if s == "":
                return None
            try:
                if "." in s:
                    return float(s)
                return int(s)
            except ValueError:
                raise ConfigValidationError(f"{path} 不是合法数字: {value!r}")
        raise ConfigValidationError(f"{path} 不是合法数字: {value!r}")

    # string / select / 其它
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_by_schema(updates: dict, schema: dict, prefix: str = "") -> dict:
    """递归根据 schema 规范化 updates 中的字段类型。

    - object：递归
    - sensitive：空字符串 / 占位符 / null 时跳过（保留原值由调用方处理）
    - 其它：按 field.type 规范化
    """
    result: dict = {}
    for key, value in updates.items():
        full_path = f"{prefix}.{key}" if prefix else key
        field_def = schema.get(key) if isinstance(schema, dict) else None

        if value is None:
            # null 不写入，调用方保留原值
            continue

        if isinstance(field_def, dict) and field_def.get("type") == "object" and field_def.get("fields"):
            if isinstance(value, dict):
                result[key] = _normalize_by_schema(value, field_def["fields"], full_path)
            else:
                # 类型不匹配，跳过
                continue
            continue

        if not field_def:
            # schema 未定义的字段：保留原样（不规范化）
            result[key] = value
            continue

        field_type = field_def.get("type", "string")
        is_sensitive = bool(field_def.get("sensitive"))

        if is_sensitive:
            if value == "" or value == SENSITIVE_PLACEHOLDER or value is None:
                # 占位符 / 空值：跳过，保留原值
                continue
            result[key] = value
            continue

        try:
            item_type = field_def.get("itemType", "string")
            result[key] = _normalize_scalar(field_type, value, full_path, item_type)
        except ConfigValidationError:
            raise

    return result


def validate_config_structure(updates: dict) -> None:
    """PRD V5 CFG-501：深结构校验对象数组字段

    检查 OBJECT_ARRAY_FIELDS（profiles / accounts / llm_providers）：
    - 若存在则必须是 list
    - list 中每一项必须是 dict
    - 禁止字符串、字符串数组、逗号分隔文本

    Raises:
        ConfigValidationError: 类型不匹配时抛出，调用方返回 400
    """
    for field_name in OBJECT_ARRAY_FIELDS:
        if field_name not in updates:
            continue
        value = updates[field_name]
        if value is None:
            continue
        if isinstance(value, str):
            raise ConfigValidationError(
                f"{field_name} 是对象数组，禁止用字符串提交，请使用专用管理页面"
            )
        if not isinstance(value, list):
            raise ConfigValidationError(
                f"{field_name} 必须是数组，得到 {type(value).__name__}"
            )
        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                raise ConfigValidationError(
                    f"{field_name}[{idx}] 必须是对象，得到 {type(item).__name__}，"
                    f"请使用专用管理页面编辑"
                )


def _merge_with_preserved_sensitive(original: dict, updates: dict, prefix: str = "") -> dict:
    """合并配置时保留敏感字段原值"""
    result = copy.deepcopy(original)
    for key, value in updates.items():
        full_path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_with_preserved_sensitive(
                result[key], value, full_path
            )
        elif (
            value == "" or
            value == SENSITIVE_PLACEHOLDER or
            value is None
        ):
            # 敏感字段保护：空值或占位符保留原值
            if full_path in SENSITIVE_FIELDS:
                pass  # 保持原值
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def _build_config_schema() -> dict:
    return {
        "bilibili": {
            "type": "object",
            "label": "B站账号",
            "description": "B站登录凭证",
            "fields": {
                "sessdata": {"type": "string", "label": "SESSDATA", "sensitive": True},
                "bili_jct": {"type": "string", "label": "bili_jct", "sensitive": True},
                "dede_user_id": {"type": "string", "label": "UID"},
                "buvid3": {"type": "string", "label": "BUVID3", "sensitive": True},
                "refresh_token": {"type": "string", "label": "刷新令牌", "sensitive": True},
            }
        },
        "llm": {
            "type": "object",
            "label": "LLM 配置",
            "description": "语言模型设置",
            "fields": {
                "api_key": {"type": "string", "label": "API Key", "sensitive": True},
                "base_url": {"type": "string", "label": "API 地址"},
                "model": {"type": "string", "label": "模型名称"},
                "max_tokens": {"type": "number", "label": "最大 Token"},
                "temperature": {"type": "number", "label": "Temperature"},
                "vision": {
                    "type": "object",
                    "label": "视觉模型",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "api_key": {"type": "string", "label": "API Key", "sensitive": True},
                        "base_url": {"type": "string", "label": "API 地址"},
                        "model": {"type": "string", "label": "模型名称"},
                    }
                },
                "embedding": {
                    "type": "object",
                    "label": "Embedding 模型",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "api_key": {"type": "string", "label": "API Key", "sensitive": True},
                        "base_url": {"type": "string", "label": "API 地址"},
                        "model": {"type": "string", "label": "模型名称"},
                    }
                }
            }
        },
        "web_search": {
            "type": "object",
            "label": "联网搜索",
            "fields": {
                "enabled": {"type": "boolean", "label": "启用"},
                "backend": {"type": "select", "label": "后端", "options": ["tavily", "perplexity", "bocha", "custom"]},
                "api_key": {"type": "string", "label": "API Key", "sensitive": True},
                "api_base": {"type": "string", "label": "自定义地址"},
                "model": {"type": "string", "label": "模型"},
                "max_results": {"type": "number", "label": "最大结果数"},
                "daily_budget_per_account": {"type": "number", "label": "每账号日预算"},
                # CFG-603：SEA-502 Custom 后端显式声明联网搜索能力
                "supports_web_search": {"type": "boolean", "label": "显式支持联网搜索"},
                # CFG-603：SEA-002 场景级开关矩阵
                "scenes": {
                    "type": "object",
                    "label": "场景开关矩阵",
                    "fields": {
                        "reply_comment": {
                            "type": "object", "label": "评论回复",
                            "fields": {
                                "enabled": {"type": "boolean", "label": "启用"},
                                "redact_query": {"type": "boolean", "label": "脱敏查询"},
                            }
                        },
                        "private_message": {
                            "type": "object", "label": "私信",
                            "fields": {
                                "enabled": {"type": "boolean", "label": "启用"},
                                "redact_query": {"type": "boolean", "label": "脱敏查询"},
                            }
                        },
                        "proactive_video": {
                            "type": "object", "label": "主动看视频",
                            "fields": {
                                "enabled": {"type": "boolean", "label": "启用"},
                                "redact_query": {"type": "boolean", "label": "脱敏查询"},
                            }
                        },
                        "dynamic_post": {
                            "type": "object", "label": "动态发布",
                            "fields": {
                                "enabled": {"type": "boolean", "label": "启用"},
                                "redact_query": {"type": "boolean", "label": "脱敏查询"},
                            }
                        },
                        "weekly_summary": {
                            "type": "object", "label": "周总结",
                            "fields": {
                                "enabled": {"type": "boolean", "label": "启用"},
                                "redact_query": {"type": "boolean", "label": "脱敏查询"},
                            }
                        },
                    }
                },
                # CFG-603：SEA-006 按 freshness 差异化缓存 TTL
                "cache": {
                    "type": "object",
                    "label": "缓存 TTL",
                    "fields": {
                        "realtime_ttl_seconds": {"type": "number", "label": "实时缓存 TTL (秒)"},
                        "daily_ttl_seconds": {"type": "number", "label": "日常缓存 TTL (秒)"},
                        "stable_ttl_seconds": {"type": "number", "label": "稳定缓存 TTL (秒)"},
                    }
                },
                # CFG-603：SEA-502 指数退避重试
                "retry": {
                    "type": "object",
                    "label": "重试策略",
                    "fields": {
                        "base_delay": {"type": "number", "label": "基础延迟 (秒)"},
                        "factor": {"type": "number", "label": "退避因子"},
                        "max_delay": {"type": "number", "label": "最大延迟 (秒)"},
                        "max_attempts": {"type": "number", "label": "最大重试次数"},
                    }
                },
            }
        },
        "personality": {
            "type": "object",
            "label": "人格基础设置",
            "fields": {
                "bot_name": {"type": "string", "label": "Bot 昵称"},  # PRD 6.2：新增
                "owner_mid": {"type": "string", "label": "主人 UID"},
                "owner_name": {"type": "string", "label": "主人称呼"},
                "enable_mood": {"type": "boolean", "label": "启用心情系统"},
                # PRD V3 §6.3：补全 personality.py 读取的核心人设字段
                "base_prompt": {"type": "string", "label": "基础人设提示词"},
                "speaking_style": {"type": "string", "label": "说话风格"},
                "boundaries": {"type": "string", "label": "禁止事项"},
            }
        },
        "features": {
            "type": "object",
            "label": "功能开关",
            "description": "集中管理所有功能模块的启用/禁用",
            "fields": {
                "reply_comment": {"type": "boolean", "label": "评论回复"},
                "private_message": {"type": "boolean", "label": "私信"},
                "proactive_video": {"type": "boolean", "label": "主动看视频"},
                "proactive_comment": {"type": "boolean", "label": "主动评论"},
                "dynamic_post": {"type": "boolean", "label": "动态发布"},
                "bangumi": {"type": "boolean", "label": "番剧追更"},
                "weekly_summary": {"type": "boolean", "label": "周总结"},
                "web_search": {"type": "boolean", "label": "联网搜索(已迁移到web_search.enabled)", "deprecated": True},
                "affection": {"type": "boolean", "label": "好感度系统"},
                "mood": {"type": "boolean", "label": "心情系统"},
            }
        },
        "dynamic_publish": {
            "type": "object",
            "label": "动态发布",
            "description": "动态发布策略（PRD §5.6）",
            "fields": {
                "topics": {"type": "array", "itemType": "string", "label": "主题池"},
                "with_image": {"type": "boolean", "label": "是否配图"},
                "review_before_publish": {"type": "boolean", "label": "发布前审核"},
                "draft_expiry_seconds": {"type": "number", "label": "草稿过期时间 (秒)"},
            }
        },
        # CFG-602：互动决策预算（PRD V4 §8.7 VID-006 / §10.2 COM-002）
        "interactions": {
            "type": "object",
            "label": "互动决策预算",
            "description": "模型只输出建议，最终决策由确定性 PolicyEngine 执行",
            "fields": {
                "like": {
                    "type": "object",
                    "label": "点赞",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "max_per_day": {"type": "number", "label": "每日上限"},
                        "score_threshold": {"type": "number", "label": "评分阈值"},
                    }
                },
                "coin": {
                    "type": "object",
                    "label": "投币",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "max_per_day": {"type": "number", "label": "每日上限"},
                        "max_per_video": {"type": "number", "label": "每视频上限"},
                        "score_threshold": {"type": "number", "label": "评分阈值"},
                    }
                },
                "favorite": {
                    "type": "object",
                    "label": "收藏",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "max_per_day": {"type": "number", "label": "每日上限"},
                        "score_threshold": {"type": "number", "label": "评分阈值"},
                    }
                },
                "comment": {
                    "type": "object",
                    "label": "评论",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "max_per_day": {"type": "number", "label": "每日上限"},
                        "score_threshold": {"type": "number", "label": "评分阈值"},
                    }
                },
            }
        },
        "safety": {
            "type": "object",
            "label": "安全与审核",
            "description": "发布安全检查与频率限制（PRD §5.9 / SAFE-501 账号级隔离）",
            "fields": {
                # CFG-601：嵌套结构与 YAML / safety.py 对齐，避免 Web 保存的扁平键被
                # build_safety_config 的嵌套读取路径覆盖（静默失效）。
                "rate_limit": {
                    "type": "object",
                    "label": "频率限制",
                    "description": "SAFE-501：account_id+scene 隔离，账号间互不抢占配额",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用限流"},
                        "per_minute": {"type": "number", "label": "每分钟上限"},
                        "per_hour": {"type": "number", "label": "每小时上限"},
                        "per_day": {"type": "number", "label": "每天上限"},
                        "global_quota": {"type": "number", "label": "全局日配额"},
                    }
                },
                "content": {
                    "type": "object",
                    "label": "内容长度限制",
                    "fields": {
                        "min_length": {"type": "number", "label": "最小内容长度"},
                        "max_length": {"type": "number", "label": "最大内容长度"},
                    }
                },
                # content_check_enabled 由 safety.py 读取为 safety 顶层扁平键
                # （_load_config_params: self.config.get("content_check_enabled", True)），
                # 保留为扁平字段以匹配 YAML 与 build_safety_config 的读取路径。
                "content_check_enabled": {"type": "boolean", "label": "启用内容检查"},
                "duplicate_check": {
                    "type": "object",
                    "label": "重复度检查",
                    "description": "SAFE-501：按 account_id 隔离存储最近内容",
                    "fields": {
                        "enabled": {"type": "boolean", "label": "启用"},
                        "window_size": {"type": "number", "label": "窗口大小"},
                        "similarity_threshold": {"type": "number", "label": "相似度阈值"},
                    }
                },
            }
        },
        "memory": {  # PRD 6.1：新增 memory 配置段
            "type": "object",
            "label": "记忆系统",
            "description": "记忆容量与遗忘策略",
            "fields": {
                "max_today": {"type": "number", "label": "今日记忆上限"},
                "max_recent": {"type": "number", "label": "近期记忆上限"},
                "max_long_term": {"type": "number", "label": "长期记忆上限"},
                "enable_forgetting": {"type": "boolean", "label": "启用遗忘"},
                "forgetting_score": {"type": "number", "label": "遗忘阈值"},
                # PRD V3 §6.1：补全缺失的 6 个字段
                "thread_compress_threshold": {"type": "number", "label": "评论线压缩阈值"},
                "oid_compress_threshold": {"type": "number", "label": "评论区压缩阈值"},
                "oid_keep_recent": {"type": "number", "label": "OID保留条数"},
                "user_compress_threshold": {"type": "number", "label": "用户记忆压缩阈值"},
                "user_keep_recent": {"type": "number", "label": "用户记忆保留条数"},
                "max_semantic_results": {"type": "number", "label": "语义搜索结果数"},
                "consolidation": {
                    "type": "object",
                    "label": "日终清算",
                    "fields": {
                        "discard_threshold": {"type": "number", "label": "丢弃阈值"},
                        "recent_promote_days": {"type": "number", "label": "近期提升天数"},
                        "long_term_age_days": {"type": "number", "label": "长期记忆天数"},
                        "hour": {"type": "number", "label": "清算时间（小时）"},
                        "batch_size": {"type": "number", "label": "批量大小"},
                    }
                }
            }
        },
        "reply": {
            "type": "object",
            "label": "评论回复",
            "fields": {
                "auto_reply": {"type": "boolean", "label": "自动回复"},
                "batch_size": {"type": "number", "label": "每批处理条数"},  # PRD 6.2：新增
                "block_keywords": {"type": "array", "itemType": "string", "label": "屏蔽关键词"},
                "min_comment_length": {"type": "number", "label": "最小评论长度"},
                "reply_own": {"type": "boolean", "label": "回复自己的评论"},
            }
        },
        # PRD V3 §7：profiles 多人格组配置
        "profiles": {
            "type": "array",
            "itemType": "object",  # PRD V5 CFG-501：对象数组，禁止逗号分隔文本
            "label": "人格配置组",
            "description": "一组人格的集合，账号可通过 profile_id 引用，运行时切换激活人格。请在「人格」页管理。",
            "item_fields": {
                "id": {"type": "string", "label": "Profile ID"},
                "name": {"type": "string", "label": "Profile 名称"},
                "default_persona": {"type": "string", "label": "主人格 ID"},
                "personas": {"type": "array", "itemType": "string", "label": "可用人格列表"},
            }
        },
        "proactive": {
            "type": "object",
            "label": "主动行为",
            "fields": {
                "video_count": {"type": "number", "label": "每日看视频次数"},
                # PRD 6.3：video_times/dynamic_times 已废弃，从 schema 移除
                "dynamic_count": {"type": "number", "label": "每日发动态次数"},
                "interest_keywords": {"type": "array", "itemType": "string", "label": "兴趣关键词"},  # PRD 6.2：新增
                # CFG-604：番剧追更 / 特别关注（V2 中为 bool 类型）
                "bangumi": {"type": "boolean", "label": "番剧追更"},
                "special_follow": {"type": "boolean", "label": "特别关注"},
                # CFG-604：TASK-501 TaskRun 持久化生命周期
                "grace_window_seconds": {"type": "number", "label": "默认迟到窗口 (秒)"},
                "scenes": {
                    "type": "object",
                    "label": "场景级配置",
                    "fields": {
                        "proactive_video": {
                            "type": "object", "label": "主动看视频",
                            "fields": {
                                "grace_window_seconds": {"type": "number", "label": "迟到窗口 (秒)"},
                                "max_attempts": {"type": "number", "label": "最大尝试次数"},
                            }
                        },
                        "dynamic": {
                            "type": "object", "label": "动态发布",
                            "fields": {
                                "grace_window_seconds": {"type": "number", "label": "迟到窗口 (秒)"},
                                "max_attempts": {"type": "number", "label": "最大尝试次数"},
                            }
                        },
                        "weekly_summary": {
                            "type": "object", "label": "周总结",
                            "fields": {
                                "grace_window_seconds": {"type": "number", "label": "迟到窗口 (秒)"},
                                "max_attempts": {"type": "number", "label": "最大尝试次数"},
                            }
                        },
                        "proactive_comment": {
                            "type": "object", "label": "主动评论",
                            "fields": {
                                "grace_window_seconds": {"type": "number", "label": "迟到窗口 (秒)"},
                                "max_attempts": {"type": "number", "label": "最大尝试次数"},
                            }
                        },
                    }
                },
            }
        },
        "web": {
            "type": "object",
            "label": "Web 管理面板",
            "fields": {
                "enabled": {"type": "boolean", "label": "启用"},
                "host": {"type": "string", "label": "监听地址"},
                "port": {"type": "number", "label": "端口"},
                "secret_key": {"type": "string", "label": "会话密钥", "sensitive": True},
                "admin_username": {"type": "string", "label": "管理员用户名"},
                "admin_password": {"type": "string", "label": "管理员密码", "sensitive": True},
                "session_ttl_seconds": {"type": "number", "label": "会话 TTL (秒)"},
                "cors_origins": {"type": "array", "itemType": "string", "label": "CORS 允许来源"},
                "secure_cookies": {"type": "boolean", "label": "安全 Cookie"},
            }
        },
        "logging": {
            "type": "object",
            "label": "日志设置",
            "fields": {
                "level": {"type": "select", "label": "日志级别", "options": ["DEBUG", "INFO", "WARNING", "ERROR"]},
                "file": {"type": "string", "label": "日志文件"},
                "max_bytes": {"type": "number", "label": "单文件大小"},
                "backup_count": {"type": "number", "label": "保留份数"},
            }
        },
        "data_dir": {
            "type": "string",
            "label": "数据目录",
        },
        # CFG-608：全局默认（虚拟分组，实际为 config.yaml 顶层键）
        # get_config_full 注入此分组供前端渲染，patch_config 展平回顶层键
        "global_defaults": {
            "type": "object",
            "label": "全局默认",
            "description": "全局默认 LLM / 账号 / 回退策略（对应 config.yaml 顶层键）",
            "fields": {
                "allow_llm_fallback": {"type": "boolean", "label": "允许 LLM 回退"},
                "default_llm": {"type": "string", "label": "默认 LLM Provider ID"},
                "default_account": {"type": "string", "label": "默认账号 ID"},
            }
        },
    }


def create_config_routes(config_loader, config_file_path: str = "config.yaml", account_manager=None,
                         safety_checker=None, web_search_service=None, policy_engine=None):
    from starlette.routing import Route

    async def get_config_schema(request: Request) -> JSONResponse:
        schema = _build_config_schema()
        return JSONResponse({"success": True, "data": schema})

    async def get_config_full(request: Request) -> JSONResponse:
        raw_config = config_loader.get_raw_config()
        masked_config = config_loader.mask_sensitive(raw_config)
        # PRD V4 CFG-005：返回当前 config_revision
        config_revision = int(raw_config.get("config_revision", 0) or 0)
        # CFG-608：注入 global_defaults 虚拟分组供前端 schema 渲染
        # 实际配置为顶层键，此处聚合后返回
        masked_config = dict(masked_config)
        masked_config["global_defaults"] = {
            "allow_llm_fallback": masked_config.get("allow_llm_fallback", False),
            "default_llm": masked_config.get("default_llm", ""),
            "default_account": masked_config.get("default_account", ""),
        }
        return JSONResponse({
            "success": True,
            "data": masked_config,
            "config": masked_config,  # 兼容旧前端
            "config_revision": config_revision,
        })

    def _build_applied_response(original: dict, updated: dict) -> dict:
        """PRD V5 CFG-502 §11.3：构建逐字段热重载状态响应。

        遍历变更字段，按 RELOAD_CONTRACT 声明的生效级别返回状态：
        - immediate：调用对应组件 reload_config()，status="applied"
        - next_task：status="pending_next_task"（调度器下个周期拾取）
        - restart_account：status="requires_restart"
        - restart_app：status="requires_restart"
        """
        changed_fields = _find_changed_fields(original, updated)
        applied: dict = {}

        for field_key, sub_field in changed_fields:
            level = _resolve_reload_level(field_key, sub_field)
            label = f"{field_key}.{sub_field}" if sub_field else field_key

            if level == "immediate":
                status = _apply_immediate_reload(field_key, sub_field, updated)
            elif level == "next_task":
                status = "pending_next_task"
            elif level == "restart_account":
                status = "requires_restart"
            elif level == "restart_app":
                status = "requires_restart"
            else:
                status = "unknown"

            applied[label] = {"level": level, "status": status}

        return applied

    def _apply_immediate_reload(field_key: str, sub_field: str, config: dict) -> str:
        """对 immediate 字段调用对应组件的 reload_config()。

        Returns:
            "applied" — 组件 reload_config 成功调用
            "applied_via_config_loader" — 无专用组件，由 config_loader.reload() 统一处理
            "reload_failed" — reload_config 抛异常
        """
        try:
            if field_key == "safety" and safety_checker is not None:
                from ..services.safety import build_safety_config
                safety_checker.reload_config(build_safety_config(config))
                return "applied"
            if field_key == "web_search" and web_search_service is not None:
                web_search_service.reload_config(config)
                return "applied"
            if field_key == "interactions" and policy_engine is not None:
                policy_engine.reload_config(config)
                return "applied"
            if field_key == "accounts" and sub_field in ACCOUNT_FIELD_CONTRACT:
                # accounts.*.cookie 等 immediate 子字段由 account_manager.reload_all() 处理
                return "applied"
            # 无专用组件持有的 immediate 字段（reply/personality/features 等）
            # config_loader.reload() 已在 PATCH 主流程中调用，统一生效
            return "applied_via_config_loader"
        except Exception as e:
            logger.warning(f"即时热重载失败 [{field_key}.{sub_field}]: {e}")
            return "reload_failed"

    async def patch_config(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            schema = _build_config_schema()

            # PRD V4 CFG-005：config_revision 冲突检测
            raw_config = config_loader.get_raw_config()
            current_revision = int(raw_config.get("config_revision", 0) or 0)
            expected_revision = body.get("_expected_revision")
            if expected_revision is not None:
                if int(expected_revision) != current_revision:
                    return JSONResponse({
                        "success": False,
                        "error": {
                            "code": "CONFIG_REVISION_CONFLICT",
                            "message": "配置已被其他会话修改，请刷新后重试",
                            "retryable": False,
                            "details": {
                                "expected": int(expected_revision),
                                "actual": current_revision,
                            },
                        },
                    }, status_code=409)

            # 1. PRD V5 CFG-501：深结构校验对象数组字段（在规范化前）
            try:
                validate_config_structure(body)
            except ConfigValidationError as e:
                return JSONResponse({
                    "success": False,
                    "error": {
                        "code": "CONFIG_VALIDATION_ERROR",
                        "message": str(e),
                        "details": {},
                    },
                }, status_code=400)

            # 2. 类型规范化
            try:
                normalized = _normalize_by_schema(body, schema)
            except ConfigValidationError as e:
                return JSONResponse({
                    "success": False,
                    "error": {
                        "code": "CONFIG_VALIDATION_ERROR",
                        "message": str(e),
                        "details": {},
                    },
                }, status_code=400)

            # CFG-608：展平 global_defaults 虚拟分组到顶层键
            # （schema 用 global_defaults 分组渲染，实际 config.yaml 为顶层键）
            if "global_defaults" in normalized and isinstance(normalized["global_defaults"], dict):
                gd = normalized.pop("global_defaults")
                for k, v in gd.items():
                    normalized[k] = v

            # 3. 合并（保留敏感原值）
            merged = _merge_with_preserved_sensitive(raw_config, normalized)

            # PRD V4 CFG-005：递增 config_revision
            merged["config_revision"] = current_revision + 1

            # 4. 保存（ConfigLoader 内部使用原子写入）
            config_loader.save_config(merged, config_file_path)

            # 5. 热重载
            reload_result = {"reloaded": False}
            try:
                config_loader.reload(merged)
                reload_result = {"reloaded": True}
            except Exception as e:
                logger.warning(f"热重载失败: {e}")
                reload_result = {"reloaded": False, "warning": str(e)}

            # PRD V3 §3.3：热重载后触发账号凭据更新（不重启调度器）
            if account_manager is not None:
                try:
                    await account_manager.reload_all()
                except Exception as e:
                    logger.warning(f"账号凭据热重载失败: {e}")

            # PRD V5 CFG-502 §11.3：构建逐字段热重载状态
            applied = _build_applied_response(raw_config, merged)

            masked = config_loader.mask_sensitive(merged)
            return JSONResponse({
                "success": True,
                "message": "配置已保存",
                "data": masked,
                "config": masked,
                "reload": reload_result,
                "config_revision": merged["config_revision"],
                "applied": applied,
            })
        except ConfigValidationError as e:
            return JSONResponse({
                "success": False,
                "error": {
                    "code": "CONFIG_VALIDATION_ERROR",
                    "message": str(e),
                    "details": {},
                },
            }, status_code=400)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "SAVE_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    async def validate_config(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            errors = []
            warnings = []
            # 合并现有配置（敏感字段未发送时用现有值兜底）
            raw = config_loader.get_raw_config()
            if "llm" in body:
                llm_cfg = body["llm"]
                # 敏感字段未在 payload 中时，用现有配置兜底
                api_key = llm_cfg.get("api_key") or raw.get("llm", {}).get("api_key", "")
                base_url = llm_cfg.get("base_url") or raw.get("llm", {}).get("base_url", "")
                if not api_key:
                    errors.append({"field": "llm.api_key", "message": "LLM API Key 不能为空"})
                if not base_url:
                    errors.append({"field": "llm.base_url", "message": "API 地址不能为空"})
            if "bilibili" in body:
                bili_cfg = body["bilibili"]
                sessdata = bili_cfg.get("sessdata") or raw.get("bilibili", {}).get("sessdata", "")
                bili_jct = bili_cfg.get("bili_jct") or raw.get("bilibili", {}).get("bili_jct", "")
                if not sessdata:
                    warnings.append({"field": "bilibili.sessdata", "message": "未配置 B站 SESSDATA"})
                if not bili_jct:
                    warnings.append({"field": "bilibili.bili_jct", "message": "未配置 B站 bili_jct"})
            if "web" in body:
                if body["web"].get("admin_password") == "admin123":
                    warnings.append({"field": "web.admin_password", "message": "使用了默认密码，请修改"})
            valid = len(errors) == 0
            return JSONResponse({
                "success": valid,
                "data": {"valid": valid, "errors": errors, "warnings": warnings},
            })
        except Exception as e:
            logger.error(f"验证配置失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "VALIDATION_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    async def reload_config(request: Request) -> JSONResponse:
        try:
            config_loader.reload()
            return JSONResponse({"success": True, "message": "配置已热重载"})
        except Exception as e:
            logger.error(f"热重载失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "RELOAD_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    return [
        Route("/api/config/schema", get_config_schema, methods=["GET"]),
        Route("/api/config/full", get_config_full, methods=["GET"]),
        Route("/api/config", patch_config, methods=["PATCH"]),
        Route("/api/config/validate", validate_config, methods=["POST"]),
        Route("/api/config/reload", reload_config, methods=["POST"]),
    ]
