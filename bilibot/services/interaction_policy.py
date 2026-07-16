"""
互动决策策略引擎 - InteractionPolicyEngine / CommentPolicy

PRD V4 §8.7 VID-006 / §10.2 COM-002：
- 模型只输出建议，最终决策由确定性 PolicyEngine 执行
- LLM 失败时所有有副作用动作默认为 false
- 严格校验模型 JSON 类型和取值范围
- 投币必须同时满足开关、评分阈值、日预算和视频未投过
- API 成功后才扣减预算和记录成功
- 每个动作独立记录 planned/result/api_code/failure_reason
- CommentPolicy 检查账号开关、日预算、视频去重、相似度、内容和全局暂停
- 主动评论默认日上限应显著低于回复上限
- 发布前固化 account_id/persona_id/bvid/oid/content_hash
- 同一个账号对同一视频同一内容只能发布一次
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from bilibot.models.interaction import InteractionSuggestion

logger = logging.getLogger("bilibot.policy")


# ═══════════════════════════════════════════
#  互动决策策略引擎（VID-006）
# ═══════════════════════════════════════════

class InteractionPolicyEngine:
    """确定性互动决策引擎

    模型只输出建议（want_like / want_coin / want_favorite / want_comment），
    最终是否执行由本引擎根据开关、日预算、评分阈值和去重状态决定。
    LLM 失败时所有有副作用动作默认为 false。

    VID-501 契约：
    - 互动意图统一通过 InteractionSuggestion DTO 交换（接受 dict 或 DTO）
    - 字段缺失 / 类型错误 → False
    - 评分阈值只能进一步拒绝模型建议（want=True 但 score 不足 → 拒绝），
      绝不能把 False 升级为 True
    - like / coin / favorite / comment 各自独立开关、日预算、per-video 限制和账号级去重
    """

    # 默认评分阈值（PRD §8.7）
    DEFAULT_THRESHOLDS = {
        "like": 6,
        "coin": 8,
        "favorite": 8,
        "comment": 7,
    }

    def __init__(self, config: Optional[Dict] = None, data_dir: str = "./data",
                 account_id: str = ""):
        self.config = config or {}
        self.data_dir = str(data_dir)
        self.account_id = account_id
        # 互动配置：interactions.{like,coin,favorite,comment}.{enabled,max_per_day,...}
        self._interactions_cfg = self.config.get("interactions", {}) or {}
        # 主动评论开关单独由 features.proactive_comment 控制，这里只管互动预算
        self._db_path = str(Path(self.data_dir) / "interaction_budget.db")
        self._lock = threading.Lock()
        self._ensure_db()

    # ── 配置解析 ──

    def _get_action_cfg(self, action: str) -> Dict[str, Any]:
        """获取单个互动动作配置

        VID-501：每个动作独立拥有 enabled / max_per_day / max_per_video / score_threshold。
        """
        cfg = self._interactions_cfg.get(action, {}) or {}
        # Task 22.1：comment 默认日上限与 CommentPolicy.DEFAULT_MAX_PER_DAY 一致，
        # 避免开启 enabled 却未显式配置 max_per_day 时被静默全禁
        default_max_per_day = CommentPolicy.DEFAULT_MAX_PER_DAY if action == "comment" else 0
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "max_per_day": int(cfg.get("max_per_day", default_max_per_day)),
            # 每个动作独立的 per-video 限制（默认 1）；coin 可在配置中放大
            "max_per_video": int(cfg.get("max_per_video", 1)),
            "score_threshold": float(cfg.get("score_threshold",
                                             self.DEFAULT_THRESHOLDS.get(action, 7))),
        }

    def reload_config(self, config: Dict):
        """热重载配置"""
        self.config = config or {}
        self._interactions_cfg = self.config.get("interactions", {}) or {}

    # ── 预算存储 ──

    def _ensure_db(self):
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interaction_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_bvid TEXT,
                    target_oid TEXT,
                    result TEXT NOT NULL,
                    api_code INTEGER,
                    failure_reason TEXT,
                    content_hash TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ilog_acct_action_date "
                "ON interaction_log(account_id, action, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ilog_dedup "
                "ON interaction_log(account_id, target_bvid, action, content_hash)"
            )
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _today_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _count_today(self, action: str, result: str = "success") -> int:
        """统计今日某动作的成功次数"""
        today_prefix = self._today_str()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM interaction_log "
                "WHERE account_id = ? AND action = ? AND result = ? "
                "AND created_at LIKE ?",
                (self.account_id or "", action, result, f"{today_prefix}%"),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    def _count_for_video(self, action: str, bvid: str) -> int:
        """统计某视频的成功互动次数（用于投币去重）"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM interaction_log "
                "WHERE account_id = ? AND action = ? AND target_bvid = ? AND result = 'success'",
                (self.account_id or "", action, bvid),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    # ── 决策接口 ──

    def evaluate(
        self,
        llm_suggestion: Union[Dict[str, Any], InteractionSuggestion, None],
        score: float,
        bvid: str,
        oid: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """对所有互动动作做确定性决策

        Args:
            llm_suggestion: LLM 评价结果（dict / InteractionSuggestion / None）。
                None 表示 LLM 完全失败。dict 会通过 InteractionSuggestion.from_dict()
                做严格类型校验 + want_fav 迁移适配。
            score: 视频评分 0-10
            bvid: 视频 bvid
            oid: 视频 oid

        Returns:
            {action: {planned, reason, ...}} 字典
        """
        # PRD §8.7：LLM 完全失败（llm_suggestion=None）时所有有副作用动作默认为 false
        if llm_suggestion is None:
            results: Dict[str, Dict[str, Any]] = {}
            for action in ("like", "coin", "favorite", "comment"):
                cfg = self._get_action_cfg(action)
                results[action] = {
                    "planned": False,
                    "reason": "llm_failure_default_false",
                    "cfg": cfg,
                }
            return results

        # VID-501：统一转为 InteractionSuggestion 做严格校验
        if isinstance(llm_suggestion, InteractionSuggestion):
            suggestion = llm_suggestion
        else:
            suggestion = InteractionSuggestion.from_dict(llm_suggestion)

        results = {}
        for action in ("like", "coin", "favorite", "comment"):
            results[action] = self._decide_action(action, suggestion, score, bvid, oid)
        return results

    def _decide_action(
        self,
        action: str,
        suggestion: InteractionSuggestion,
        score: float,
        bvid: str,
        oid: str,
    ) -> Dict[str, Any]:
        """单个动作决策

        VID-501 决策顺序：
        1. 开关 → 关闭即拒绝
        2. 模型建议 → 缺失/类型错误/False 一律拒绝（评分阈值不得把 False 升级为 True）
        3. 评分阈值 → 只能进一步拒绝 want=True 的建议
        4. 日预算
        5. 视频级去重（每个动作独立 per-video 限制）
        """
        cfg = self._get_action_cfg(action)

        # 1. 开关
        if not cfg["enabled"]:
            return {"planned": False, "reason": "disabled", "cfg": cfg}

        # 2. 模型建议（已由 InteractionSuggestion 严格校验：缺失/类型错误 → False）
        want = getattr(suggestion, f"want_{action}", False)
        if not want:
            return {"planned": False, "reason": "llm_false", "cfg": cfg}

        # 3. 评分阈值（VID-501：只能进一步拒绝，不能升级）
        if score < cfg["score_threshold"]:
            return {"planned": False, "reason": f"score_below_{cfg['score_threshold']}", "cfg": cfg}

        # 4. 日预算
        # Task 22.2：comment 的日预算交由 CommentPolicy 接管（计 proactive_comments 表），
        # interaction_log 的 comment 计数可能与回复评论混计，故此处跳过 comment 的日预算判断
        if action != "comment":
            used_today = self._count_today(action)
            if used_today >= cfg["max_per_day"]:
                return {"planned": False, "reason": "daily_budget_exhausted", "cfg": cfg, "used": used_today}

        # 5. 视频级去重（VID-501：每个动作独立 per-video 限制 + 账号级去重）
        max_per_video = cfg.get("max_per_video", 1)
        if max_per_video > 0:
            video_used = self._count_for_video(action, bvid)
            if video_used >= max_per_video:
                return {"planned": False, "reason": f"video_already_{action}", "cfg": cfg}

        return {"planned": True, "reason": "approved", "cfg": cfg}

    # ── 结果记录 ──

    def record_result(
        self,
        action: str,
        bvid: str,
        oid: str,
        result: str,
        api_code: Optional[int] = None,
        failure_reason: str = "",
        content_hash: str = "",
    ):
        """记录互动结果

        Args:
            action: like/coin/favorite/comment
            result: success / failed / skipped
            api_code: B站 API 返回码
            failure_reason: 失败原因
            content_hash: 评论内容哈希（仅 comment）
        """
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO interaction_log "
                    "(account_id, action, target_bvid, target_oid, result, api_code, "
                    " failure_reason, content_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.account_id or "", action, bvid, oid or "", result,
                     api_code, failure_reason, content_hash, datetime.now().isoformat()),
                )
                conn.commit()
            finally:
                conn.close()

    async def record_result_async(
        self,
        action: str,
        bvid: str,
        oid: str,
        result: str,
        api_code: Optional[int] = None,
        failure_reason: str = "",
        content_hash: str = "",
    ) -> None:
        """record_result 的 async 包装：放线程池执行，避免阻塞事件循环

        threading.Lock 仍保留以串行化写事务（与 KnowledgeBaseStore 一致）。
        调用方应在 async 上下文中使用 `await record_result_async(...)`。
        """
        await asyncio.to_thread(
            self.record_result, action, bvid, oid, result,
            api_code=api_code, failure_reason=failure_reason,
            content_hash=content_hash,
        )

    def get_today_summary(self) -> Dict[str, Dict[str, int]]:
        """获取今日各动作的预算使用情况"""
        summary: Dict[str, Dict[str, int]] = {}
        for action in ("like", "coin", "favorite", "comment"):
            cfg = self._get_action_cfg(action)
            used = self._count_today(action)
            summary[action] = {
                "enabled": int(cfg["enabled"]),
                "max_per_day": cfg["max_per_day"],
                "used_today": used,
                "remaining": max(0, cfg["max_per_day"] - used),
            }
        return summary

    async def evaluate_async(
        self,
        llm_suggestion: Union[Dict[str, Any], InteractionSuggestion, None],
        score: float,
        bvid: str,
        oid: str = "",
    ) -> Dict[str, Dict[str, Any]]:
        """evaluate 的 async 包装：放线程池执行，避免阻塞事件循环

        evaluate() 内部通过 _count_today / _count_for_video 做同步 SQLite 查询，
        直接在 async 上下文调用会阻塞事件循环。
        """
        return await asyncio.to_thread(
            self.evaluate, llm_suggestion, score, bvid, oid,
        )

    async def get_today_summary_async(self) -> Dict[str, Dict[str, int]]:
        """get_today_summary 的 async 包装：放线程池执行，避免阻塞事件循环

        内部通过 _count_today 做 4 次同步 SQLite 查询。
        """
        return await asyncio.to_thread(self.get_today_summary)


# ═══════════════════════════════════════════
#  主动评论策略（COM-002）
# ═══════════════════════════════════════════

class CommentPolicy:
    """主动评论发布策略

    PRD V4 §10.2 COM-002：
    - 视频评价可以产生 comment_candidate，但不能直接决定发布
    - 检查账号开关、日预算、视频去重、相似度、安全和全局暂停
    - 主动评论默认日上限应显著低于回复上限
    - 发布前固化 account_id/persona_id/bvid/oid/content_hash
    - 同一个账号对同一视频同一内容只能发布一次
    """

    # 默认主动评论日上限（显著低于回复上限 200）
    DEFAULT_MAX_PER_DAY = 10

    def __init__(
        self,
        config: Optional[Dict] = None,
        data_dir: str = "./data",
        account_id: str = "",
        safety_checker=None,
    ):
        self.config = config or {}
        self.data_dir = str(data_dir)
        self.account_id = account_id
        # B7：全局/账号暂停闸门（可选注入；未注入时仅做预算/去重）
        self.safety_checker = safety_checker
        self._interactions_cfg = self.config.get("interactions", {}) or {}
        self._db_path = str(Path(self.data_dir) / "interaction_budget.db")
        self._lock = threading.Lock()
        self._ensure_db()

    def reload_config(self, config: Dict):
        self.config = config or {}
        self._interactions_cfg = self.config.get("interactions", {}) or {}

    def set_safety_checker(self, safety_checker) -> None:
        """运行时注入/替换 SafetyChecker（账号 reload 时使用）。"""
        self.safety_checker = safety_checker

    def _ensure_db(self):
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    persona_id TEXT,
                    bvid TEXT NOT NULL,
                    oid TEXT,
                    content_hash TEXT NOT NULL,
                    content_preview TEXT,
                    published INTEGER DEFAULT 0,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, bvid, content_hash)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pc_acct_date "
                "ON proactive_comments(account_id, created_at)"
            )
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def content_hash(content: str) -> str:
        """计算评论内容哈希（COM-002 固化字段）"""
        normalized = content.strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _max_per_day(self) -> int:
        cfg = self._interactions_cfg.get("comment", {}) or {}
        return int(cfg.get("max_per_day", self.DEFAULT_MAX_PER_DAY))

    def _count_today_published(self) -> int:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM proactive_comments "
                "WHERE account_id = ? AND published = 1 AND created_at LIKE ?",
                (self.account_id or "", f"{today_prefix}%"),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    def _already_published_same_content(self, bvid: str, content_hash: str) -> bool:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM proactive_comments "
                "WHERE account_id = ? AND bvid = ? AND content_hash = ? AND published = 1 "
                "LIMIT 1",
                (self.account_id or "", bvid, content_hash),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _already_published_any_for_video(self, bvid: str) -> int:
        """统计某视频已发布的主动评论数"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM proactive_comments "
                "WHERE account_id = ? AND bvid = ? AND published = 1",
                (self.account_id or "", bvid),
            ).fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()

    def check(
        self,
        bvid: str,
        oid: str,
        content: str,
        persona_id: str = "",
        max_per_video: int = 1,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """发布前检查

        Returns:
            (allowed, reason, meta)
            meta 包含 content_hash / used_today / max_per_day 等
        """
        if not content or not content.strip():
            return False, "empty_content", {}

        c_hash = self.content_hash(content)

        # 0. 全局/账号暂停（COM-002 / B-008：策略层必须 fail-closed）
        sc = self.safety_checker
        if sc is not None:
            try:
                if hasattr(sc, "is_paused") and sc.is_paused():
                    return False, "global_paused", {"content_hash": c_hash}
                if (
                    self.account_id
                    and hasattr(sc, "is_account_paused")
                    and sc.is_account_paused(self.account_id)
                ):
                    return False, f"account_paused:{self.account_id}", {
                        "content_hash": c_hash,
                    }
            except Exception as e:
                logger.warning("CommentPolicy 暂停检查异常，fail-closed: %s", e)
                return False, "safety_check_error", {"content_hash": c_hash}

        # 1. 日预算
        max_day = self._max_per_day()
        used_today = self._count_today_published()
        if used_today >= max_day:
            return False, f"daily_budget_exhausted({used_today}/{max_day})", {
                "content_hash": c_hash, "used_today": used_today, "max_per_day": max_day,
            }

        # 2. 视频级去重（同一视频最多 N 条主动评论）
        if max_per_video > 0 and self._already_published_any_for_video(bvid) >= max_per_video:
            return False, "video_comment_limit_reached", {
                "content_hash": c_hash, "bvid": bvid,
            }

        # 3. 同内容去重（account_id + bvid + content_hash）
        if self._already_published_same_content(bvid, c_hash):
            return False, "duplicate_content", {
                "content_hash": c_hash, "bvid": bvid,
            }

        return True, "approved", {
            "content_hash": c_hash,
            "used_today": used_today,
            "max_per_day": max_day,
            "persona_id": persona_id,
            "bvid": bvid,
            "oid": oid,
        }

    async def check_async(
        self,
        bvid: str,
        oid: str,
        content: str,
        persona_id: str = "",
        max_per_video: int = 1,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """check 的 async 包装：放线程池执行 SQLite 读，避免阻塞事件循环

        读操作依赖 WAL 并发，无需加锁。
        """
        return await asyncio.to_thread(
            self.check, bvid, oid, content,
            persona_id=persona_id, max_per_video=max_per_video,
        )

    def record(
        self,
        bvid: str,
        oid: str,
        content: str,
        persona_id: str = "",
        published: bool = False,
        failure_reason: str = "",
    ) -> str:
        """记录主动评论发布尝试（COM-004 审计）"""
        c_hash = self.content_hash(content)
        with self._lock:
            conn = self._get_conn()
            try:
                # 使用 INSERT OR IGNORE 保证 (account_id, bvid, content_hash) 唯一
                conn.execute(
                    "INSERT OR IGNORE INTO proactive_comments "
                    "(account_id, persona_id, bvid, oid, content_hash, content_preview, "
                    " published, failure_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.account_id or "", persona_id, bvid, oid or "", c_hash,
                     content[:200], int(published), failure_reason,
                     datetime.now().isoformat()),
                )
                # 如果已存在且本次成功，更新为已发布
                if published:
                    conn.execute(
                        "UPDATE proactive_comments SET published = 1, failure_reason = '' "
                        "WHERE account_id = ? AND bvid = ? AND content_hash = ?",
                        (self.account_id or "", bvid, c_hash),
                    )
                conn.commit()
            finally:
                conn.close()
        return c_hash

    async def record_async(
        self,
        bvid: str,
        oid: str,
        content: str,
        persona_id: str = "",
        published: bool = False,
        failure_reason: str = "",
    ) -> str:
        """record 的 async 包装：放线程池执行，避免阻塞事件循环

        threading.Lock 仍保留以串行化写事务（与 KnowledgeBaseStore 一致）。
        """
        return await asyncio.to_thread(
            self.record, bvid, oid, content,
            persona_id=persona_id, published=published,
            failure_reason=failure_reason,
        )

    def get_today_summary(self) -> Dict[str, int]:
        return {
            "max_per_day": self._max_per_day(),
            "used_today": self._count_today_published(),
            "remaining": max(0, self._max_per_day() - self._count_today_published()),
        }
