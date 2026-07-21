"""
配置迁移服务（PRD V4 §19.1 MIG-001）

启动时只执行可回滚迁移：
1. 备份原 config.yaml（带时间戳）
2. 将重复开关迁移到规范字段
3. 保留未知字段
4. 写入 config_version: 5，并补齐 V6 记忆大脑参数
5. 输出迁移报告，不输出敏感值

迁移规则（CFG-003 单一开关迁移）：
| 旧冲突 | V4 规范字段 | 迁移策略 |
|---|---|---|
| reply.auto_reply=false | features.reply_comment=false | 读取旧值一次并迁移 |
| features.web_search=true | web_search.enabled=true | 删除旧运行时消费者 |
| personality.enable_mood=false | features.mood=false | 旧字段只兼容迁移 |
"""
import copy
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("bilibot.migrator")

TARGET_CONFIG_VERSION = 5


def migrate_config(config: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """执行配置迁移（不修改原 dict，返回新 dict 和迁移报告）

    Args:
        config: 原始配置 dict

    Returns:
        (migrated_config, report_lines)
        report_lines 是人类可读的迁移报告（不包含敏感值）
    """
    migrated = copy.deepcopy(config)
    report: List[str] = []
    changed = False

    # 确保顶层段存在
    features = migrated.setdefault("features", {})

    # ── 迁移 1: reply.auto_reply → features.reply_comment ──
    reply_cfg = migrated.get("reply", {}) or {}
    if "auto_reply" in reply_cfg:
        old_val = reply_cfg["auto_reply"]
        # 只有当 features.reply_comment 未显式设置时才迁移
        if "reply_comment" not in features:
            features["reply_comment"] = bool(old_val)
            report.append(f"reply.auto_reply={old_val} → features.reply_comment={bool(old_val)}")
            changed = True
        # 保留旧字段（兼容期不删除，但运行时不再消费）

    # ── 迁移 2: features.web_search → web_search.enabled ──
    if "web_search" in features:
        old_ws = features["web_search"]
        ws_cfg = migrated.setdefault("web_search", {})
        if "enabled" not in ws_cfg:
            ws_cfg["enabled"] = bool(old_ws)
            report.append(f"features.web_search={old_ws} → web_search.enabled={bool(old_ws)}")
            changed = True
        # 保留旧字段标记 deprecated（运行时不再消费）

    # ── 迁移 3: personality.enable_mood → features.mood ──
    personality_cfg = migrated.get("personality", {}) or {}
    if "enable_mood" in personality_cfg:
        old_mood = personality_cfg["enable_mood"]
        if "mood" not in features:
            features["mood"] = bool(old_mood)
            report.append(f"personality.enable_mood={old_mood} → features.mood={bool(old_mood)}")
            changed = True

    # ── 迁移 4: 旧 proactive 互动字段 → interactions 段 ──
    # proactive.like/coin/favorite 已被 interactions.* 替代
    # 如果 interactions 段不存在但旧字段存在，创建默认 interactions
    proactive_cfg = migrated.get("proactive", {}) or {}
    if "interactions" not in migrated and (
        "like" in proactive_cfg or "coin" in proactive_cfg or "favorite" in proactive_cfg
    ):
        migrated["interactions"] = {
            "like": {"enabled": bool(proactive_cfg.get("like", False)), "max_per_day": 10},
            "coin": {"enabled": bool(proactive_cfg.get("coin", False)),
                      "max_per_day": 0, "max_per_video": 1},
            "favorite": {"enabled": bool(proactive_cfg.get("favorite", False)), "max_per_day": 5},
            "comment": {"enabled": bool(proactive_cfg.get("comment", True)), "max_per_day": 10},
        }
        report.append("proactive.{like,coin,favorite,comment} → interactions.* (旧字段保留兼容)")
        changed = True

    # ── 迁移 5: V6 记忆大脑参数 ──
    # 旧容量/遗忘字段保留，便于配置回滚和审计，但 V6 运行时不再消费。
    memory_cfg = migrated.setdefault("memory", {})
    v6_memory_defaults = {
        "recall_candidate_limit": 12,
        "recall_inject_limit": 5,
        "recall_association_limit": 2,
        "rerank_timeout_seconds": 8.0,
        "recall_total_timeout_seconds": 10.0,
        "rerank_relevance_baseline": 0.65,
        "enrichment_chat_timeout_seconds": 12.0,
        "link_candidate_limit": 12,
        "link_job_max_attempts": 3,
        "prompt_char_budget": 5000,
        "chunk_target_chars": 600,
        "chunk_hard_chars": 900,
        "chunk_target_tokens": 450,
        "chunk_hard_tokens": 700,
        "chunk_overlap_chars": 100,
        "job_max_attempts": 8,
        "vector_cache_limit": 50000,
        "vector_batch_size": 2048,
    }
    added_memory_fields = []
    for key, value in v6_memory_defaults.items():
        if key not in memory_cfg:
            memory_cfg[key] = value
            added_memory_fields.append(key)
            changed = True
    if added_memory_fields:
        report.append(
            "memory: 已补齐 V6 账号级记忆大脑参数（旧容量/遗忘字段仅保留兼容）"
        )

    # ── 写入 config_version ──
    old_version = migrated.get("config_version", 0)
    if int(old_version) < TARGET_CONFIG_VERSION:
        migrated["config_version"] = TARGET_CONFIG_VERSION
        report.append(f"config_version: {old_version} → {TARGET_CONFIG_VERSION}")
        changed = True

    if not changed:
        report.append("配置已是最新版本，无需迁移")

    return migrated, report


def backup_config(config_path: str) -> str:
    """备份配置文件（带时间戳）

    Returns:
        备份文件路径
    """
    if not os.path.exists(config_path):
        return ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{config_path}.backup_{timestamp}"
    shutil.copy2(config_path, backup_path)
    logger.info(f"配置已备份到: {backup_path}")
    return backup_path


def run_migration(config_path: str, dry_run: bool = False) -> Tuple[bool, List[str]]:
    """执行完整的配置迁移流程

    Args:
        config_path: config.yaml 路径
        dry_run: 只报告不写入

    Returns:
        (success, report_lines)
    """
    import yaml

    if not os.path.exists(config_path):
        logger.warning(f"配置文件不存在: {config_path}")
        return False, ["配置文件不存在"]

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"读取配置失败: {e}")
        return False, [f"读取配置失败: {e}"]

    migrated, report = migrate_config(config)

    if dry_run:
        logger.info("[dry-run] 配置迁移报告:")
        for line in report:
            logger.info(f"  {line}")
        return True, report

    # 检查是否有实际变更
    if migrated == config:
        logger.info("配置无需迁移")
        return True, report

    # 1. 备份
    backup_path = backup_config(config_path)

    # 2. 原子写入
    tmp_path = config_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(migrated, f, allow_unicode=True, default_flow_style=False)
        os.replace(tmp_path, config_path)
        logger.info(f"配置已迁移并保存到: {config_path}")
    except Exception as e:
        logger.error(f"配置写入失败: {e}")
        # 清理临时文件
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False, report + [f"写入失败: {e}"]

    # 3. 输出迁移报告（不输出敏感值）
    logger.info("配置迁移报告:")
    for line in report:
        logger.info(f"  {line}")
    if backup_path:
        logger.info(f"  备份位置: {backup_path}")

    return True, report
