"""
安全与审核服务 - SafetyChecker（PRD §5.9）

集中管理：
- 全局暂停 Bot（SQLite 持久化）
- 账号级风险暂停（SQLite 持久化 + 内存缓存）
- 发布前内容安全检查（长度 / 敏感词 / 重复度 / 人格一致性）
- 发布频率限制（滑动窗口：每分钟 / 每小时 / 每天，account_id+scene 隔离；
  时间戳持久化到 safety.db，重启后恢复近 24h 配额）
- 用户级黑名单（SQLite 存储，add/remove/check/list）
- 敏感词过滤（全文匹配 + 正则匹配，支持运行时加载）

SAFE-501：账号级隔离
- 限流键改为 `account_id:scene`，账号间互不抢占配额
- 全局总配额 `global_quota` 限制所有账号+场景的日发布总量
- `content_check_enabled=false` 仅跳过可选内容规则，不跳过全局暂停/账号风险暂停/硬性长度限制
- 最近内容相似度按账号隔离存储
- `reload_config()` 支持热重载
"""
import logging
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("bilibot.safety")

# 限流事件保留窗口（秒）：覆盖日桶；小时/分钟从中裁剪
_RATE_EVENT_RETENTION_SECS = 86400


class SafetyChecker:
    """安全检查器（PRD §5.9）

    集中管理：全局暂停、账号风险暂停、发布前检查、频率限制、黑名单、敏感词过滤。
    """

    # 默认频率限制（可被 config 覆盖）
    DEFAULT_RATE_LIMITS: Dict[str, int] = {
        "per_minute": 5,
        "per_hour": 50,
        "per_day": 200,
    }

    # 默认内容长度限制
    DEFAULT_MIN_LENGTH = 2
    DEFAULT_MAX_LENGTH = 2000

    # 重复度阈值（与最近 N 条比较，相似度 >= 该值视为重复）
    DEFAULT_DUPLICATE_THRESHOLD = 0.8
    DEFAULT_DUPLICATE_CHECK_N = 10

    # 默认全局日配额（所有账号+场景合计）
    DEFAULT_GLOBAL_QUOTA = 1000

    def __init__(self, data_dir: str = "./data", config: dict = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "safety.db"
        self.config: Dict[str, Any] = config or {}

        # 敏感词列表（全文匹配）与正则列表
        self._sensitive_words: List[str] = []
        self._sensitive_patterns: List[re.Pattern] = []

        # 频率限制：按 account_id:scene 维护滑动窗口时间戳
        # 结构: {account_scene_key: {"minute": deque, "hour": deque, "day": deque}}
        self._rate_buckets: Dict[str, Dict[str, Deque[float]]] = {}

        # 全局频率桶（所有账号+场景合计，仅 day 窗口）
        self._global_bucket: Dict[str, Deque[float]] = {
            "day": deque(),
        }

        # BUG B-002: 为每个 bucket 的 deque 操作添加线程锁，确保 check+record 原子性
        # _rate_locks 按 key 存储各自的锁，_global_lock 保护全局桶
        # _rate_locks_guard 保护锁表本身的创建，避免并发首次访问同一 key 时产生两把锁
        # _db_lock 保护 rate_limit_events / account_pause 的 SQLite 写读与内存同步
        self._rate_locks: Dict[str, threading.Lock] = {}
        self._rate_locks_guard = threading.Lock()
        self._global_lock = threading.Lock()
        self._db_lock = threading.Lock()

        # 账号级风险暂停（内存缓存，DB 为权威来源；启动时从 DB 加载）
        # 结构: {account_id: {"reason": str, "paused_at": str}}
        self._account_paused: Dict[str, Dict[str, Any]] = {}

        # 最近发布内容（用于重复度检查），按 account_id 隔离
        # 结构: {account_id: deque(maxlen=window_size)}
        self._recent_contents: Dict[str, Deque[str]] = {}

        # 从配置加载所有参数
        self._load_config_params(self.config)

        # 初始化数据库并恢复持久化状态
        self._init_db()
        self._load_account_pauses_from_db()
        self._load_rate_buckets_from_db()

        # 从配置加载敏感词（reply.block_keywords）
        self._load_sensitive_words_from_config()

        logger.info("SafetyChecker 初始化完成")

    # ────────────────────── 配置加载 ──────────────────────

    def _load_config_params(self, config: dict) -> None:
        """从配置字典加载所有参数（init 和 reload 共用）"""
        self.config = config or {}

        # 频率限制配置（合并默认值与 config）
        rate_cfg = self.config.get("rate_limit", {}) or {}
        self.rate_limits: Dict[str, int] = {
            "per_minute": int(rate_cfg.get("per_minute", self.DEFAULT_RATE_LIMITS["per_minute"])),
            "per_hour": int(rate_cfg.get("per_hour", self.DEFAULT_RATE_LIMITS["per_hour"])),
            "per_day": int(rate_cfg.get("per_day", self.DEFAULT_RATE_LIMITS["per_day"])),
        }
        # 全局日配额（所有账号+场景合计）
        self.global_quota: int = int(rate_cfg.get("global_quota", self.DEFAULT_GLOBAL_QUOTA))
        # 全局限流开关
        self.rate_limit_enabled: bool = bool(rate_cfg.get("enabled", True))

        # 内容长度配置（硬限制，content_check_enabled=false 时仍然生效）
        content_cfg = self.config.get("content", {}) or {}
        self.min_length: int = int(content_cfg.get("min_length", self.DEFAULT_MIN_LENGTH))
        self.max_length: int = int(content_cfg.get("max_length", self.DEFAULT_MAX_LENGTH))

        # 内容检查总开关（false 时仅跳过可选内容规则，不跳过硬限制）
        self.content_check_enabled: bool = bool(
            self.config.get("content_check_enabled", True)
        )

        # 重复度配置
        dup_cfg = self.config.get("duplicate_check", {}) or {}
        # 兼容旧配置：config.duplicate.threshold / check_n
        if not dup_cfg:
            dup_cfg = self.config.get("duplicate", {}) or {}
        self.duplicate_check_enabled: bool = bool(dup_cfg.get("enabled", True))
        self.duplicate_threshold: float = float(
            dup_cfg.get("similarity_threshold", dup_cfg.get("threshold", self.DEFAULT_DUPLICATE_THRESHOLD))
        )
        self.duplicate_check_n: int = int(
            dup_cfg.get("window_size", dup_cfg.get("check_n", self.DEFAULT_DUPLICATE_CHECK_N))
        )

    # ────────────────────── 数据库初始化 ──────────────────────

    def _init_db(self) -> None:
        """初始化 SQLite 表结构"""
        with sqlite3.connect(str(self.db_path)) as conn:
            # 全局暂停状态表（单行记录，key='global'）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_pause (
                    key TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0,
                    reason TEXT DEFAULT '',
                    paused_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            # 确保存在一行默认记录
            conn.execute(
                "INSERT OR IGNORE INTO bot_pause (key, paused, reason, paused_at, updated_at) "
                "VALUES ('global', 0, '', '', ?)",
                (datetime.now().isoformat(),),
            )

            # 用户黑名单表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_blacklist (
                    user_id TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 账号级风险暂停（持久化，重启后仍生效）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_pause (
                    account_id TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    paused_at TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)

            # 限流事件：滚动窗口时间戳（近 24h），重启后恢复日/时配额
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rate_limit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_scene_key TEXT NOT NULL,
                    scene TEXT DEFAULT '',
                    account_id TEXT DEFAULT '',
                    ts REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_limit_events_ts "
                "ON rate_limit_events(ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rate_limit_events_key_ts "
                "ON rate_limit_events(account_scene_key, ts)"
            )
            conn.commit()

    def _connect_db(self) -> sqlite3.Connection:
        """打开 safety.db 连接（短连接，调用方负责关闭）"""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _load_account_pauses_from_db(self) -> None:
        """从 DB 加载账号风险暂停到内存缓存"""
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT account_id, reason, paused_at FROM account_pause"
                    ).fetchall()
            loaded: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                aid = r["account_id"]
                if not aid:
                    continue
                loaded[aid] = {
                    "reason": r["reason"] or "",
                    "paused_at": r["paused_at"] or "",
                }
            self._account_paused = loaded
            if loaded:
                logger.info(f"已从 DB 恢复账号风险暂停 {len(loaded)} 个")
        except Exception as e:
            logger.warning(f"加载账号风险暂停失败: {e}")

    def _load_rate_buckets_from_db(self) -> None:
        """从 DB 加载近 24h 限流时间戳到内存 deque（日桶 + 小时/分钟裁剪）"""
        cutoff = time.time() - _RATE_EVENT_RETENTION_SECS
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    # 清理过期事件，避免表无限增长
                    conn.execute(
                        "DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,)
                    )
                    conn.commit()
                    rows = conn.execute(
                        "SELECT account_scene_key, ts FROM rate_limit_events "
                        "WHERE ts >= ? ORDER BY ts ASC",
                        (cutoff,),
                    ).fetchall()

            now = time.time()
            per_key: Dict[str, List[float]] = {}
            global_ts: List[float] = []

            for key, ts in rows:
                try:
                    ts_f = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts_f < cutoff:
                    continue
                per_key.setdefault(key or "_global_:_global_", []).append(ts_f)
                global_ts.append(ts_f)

            for key, stamps in per_key.items():
                day_dq: Deque[float] = deque(stamps)
                hour_cutoff = now - 3600
                minute_cutoff = now - 60
                hour_dq: Deque[float] = deque(t for t in stamps if t >= hour_cutoff)
                minute_dq: Deque[float] = deque(t for t in stamps if t >= minute_cutoff)
                self._rate_buckets[key] = {
                    "minute": minute_dq,
                    "hour": hour_dq,
                    "day": day_dq,
                }

            self._global_bucket["day"] = deque(global_ts)

            logger.info(
                f"已从 DB 恢复限流事件: keys={len(per_key)} events={len(global_ts)}"
            )
        except Exception as e:
            logger.warning(f"加载限流事件失败，使用空桶: {e}")

    def _db_insert_rate_event(
        self, key: str, scene: str, account_id: str, ts: float
    ) -> None:
        """写入一条限流事件（调用方应已持有业务锁或接受与 check 同序写）"""
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    conn.execute(
                        "INSERT INTO rate_limit_events "
                        "(account_scene_key, scene, account_id, ts) "
                        "VALUES (?, ?, ?, ?)",
                        (key, scene or "", account_id or "", float(ts)),
                    )
                    # 偶尔清理过期行（约每 50 次插入时触发一次，避免热路径开销）
                    if int(ts * 1000) % 50 == 0:
                        cutoff = time.time() - _RATE_EVENT_RETENTION_SECS
                        conn.execute(
                            "DELETE FROM rate_limit_events WHERE ts < ?", (cutoff,)
                        )
                    conn.commit()
        except Exception as e:
            logger.warning(f"写入 rate_limit_events 失败 key={key}: {e}")

    def _db_delete_rate_event_by_ts(self, key: str, ts: float) -> bool:
        """按 key+精确时间戳删除一条限流事件（refund / 回滚）

        Returns:
            True 若删除了至少一行
        """
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    cur = conn.execute(
                        "DELETE FROM rate_limit_events WHERE id = ("
                        "  SELECT id FROM rate_limit_events "
                        "  WHERE account_scene_key = ? AND ABS(ts - ?) < 1e-6 "
                        "  ORDER BY id DESC LIMIT 1"
                        ")",
                        (key, float(ts)),
                    )
                    deleted = cur.rowcount > 0
                    conn.commit()
            return deleted
        except Exception as e:
            logger.warning(f"删除 rate_limit_events 失败 key={key} ts={ts}: {e}")
            return False

    def _db_delete_latest_rate_event(self, key: str) -> Optional[float]:
        """删除指定 key 最近一条限流事件，返回被删时间戳（无则 None）"""
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    row = conn.execute(
                        "SELECT id, ts FROM rate_limit_events "
                        "WHERE account_scene_key = ? ORDER BY ts DESC, id DESC LIMIT 1",
                        (key,),
                    ).fetchone()
                    if not row:
                        return None
                    event_id, ts = row[0], float(row[1])
                    conn.execute(
                        "DELETE FROM rate_limit_events WHERE id = ?", (event_id,)
                    )
                    conn.commit()
                    return ts
        except Exception as e:
            logger.warning(f"删除最近 rate_limit_events 失败 key={key}: {e}")
            return None

    # ────────────────────── 全局暂停 ──────────────────────

    def is_paused(self) -> bool:
        """是否处于全局暂停状态

        fail-closed：DB 异常时返回 True（视为已暂停），避免安全检查失效导致
        自动发布行为失控。
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT paused FROM bot_pause WHERE key = 'global'"
                ).fetchone()
            return bool(row and row[0])
        except Exception as e:
            logger.warning(f"is_paused DB error, fail-closed to True: {e}")
            return True

    def pause(self, reason: str = "") -> None:
        """暂停 Bot 的所有自动发布行为"""
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE bot_pause SET paused = 1, reason = ?, paused_at = ?, updated_at = ? "
                "WHERE key = 'global'",
                (reason, now, now),
            )
            conn.commit()
        logger.warning(f"Bot 已全局暂停 reason={reason!r}")

    def resume(self) -> None:
        """恢复 Bot 的自动发布行为"""
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE bot_pause SET paused = 0, reason = '', paused_at = '', updated_at = ? "
                "WHERE key = 'global'",
                (now,),
            )
            conn.commit()
        logger.info("Bot 已恢复运行")

    def get_pause_status(self) -> dict:
        """获取暂停状态详情

        fail-closed：DB 异常时返回 paused=True（视为已暂停）。
        """
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT paused, reason, paused_at, updated_at FROM bot_pause WHERE key = 'global'"
                ).fetchone()
            if not row:
                return {"paused": False, "reason": "", "paused_at": "", "updated_at": ""}
            return {
                "paused": bool(row["paused"]),
                "reason": row["reason"] or "",
                "paused_at": row["paused_at"] or "",
                "updated_at": row["updated_at"] or "",
            }
        except Exception as e:
            logger.warning(f"get_pause_status DB error, fail-closed to paused=True: {e}")
            return {
                "paused": True,
                "reason": "DB error - fail-closed",
                "paused_at": "",
                "updated_at": "",
            }

    # ────────────────────── 账号级风险暂停 ──────────────────────

    def is_account_paused(self, account_id: str) -> bool:
        """检查指定账号是否处于风险暂停状态（读内存缓存，启动时从 DB 加载）

        SAFE-501：account_id 非空时检查账号级暂停；
        account_id 为空时返回 False（仅全局暂停由 is_paused() 处理）。
        """
        if not account_id:
            return False
        return account_id in self._account_paused

    def pause_account(self, account_id: str, reason: str = "") -> None:
        """暂停指定账号的自动发布行为（不影响其他账号，SQLite 持久化）

        SAFE-501：账号级风险暂停（如 B站风控 code=-352）只暂停触发的账号。
        """
        if not account_id:
            return
        now = datetime.now().isoformat()
        info = {"reason": reason or "", "paused_at": now}
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    conn.execute(
                        "INSERT INTO account_pause (account_id, reason, paused_at, updated_at) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(account_id) DO UPDATE SET "
                        "reason=excluded.reason, paused_at=excluded.paused_at, "
                        "updated_at=excluded.updated_at",
                        (account_id, reason or "", now, now),
                    )
                    conn.commit()
            self._account_paused[account_id] = info
            logger.warning(f"账号 {account_id} 已风险暂停 reason={reason!r}")
        except Exception as e:
            # DB 失败时仍写入内存，避免风控后继续发帖
            self._account_paused[account_id] = info
            logger.error(f"账号 {account_id} 风险暂停写 DB 失败，已写入内存: {e}")

    def resume_account(self, account_id: str) -> None:
        """恢复指定账号的自动发布行为（同步清除 DB）"""
        if not account_id:
            return
        try:
            with self._db_lock:
                with self._connect_db() as conn:
                    conn.execute(
                        "DELETE FROM account_pause WHERE account_id = ?",
                        (account_id,),
                    )
                    conn.commit()
        except Exception as e:
            logger.warning(f"账号 {account_id} 恢复时删 DB 失败: {e}")
        self._account_paused.pop(account_id, None)
        logger.info(f"账号 {account_id} 已恢复运行")

    def get_account_pause_status(self, account_id: str) -> dict:
        """获取账号暂停状态详情"""
        if not account_id or account_id not in self._account_paused:
            return {"paused": False, "reason": "", "paused_at": ""}
        info = self._account_paused[account_id]
        return {
            "paused": True,
            "reason": info.get("reason", ""),
            "paused_at": info.get("paused_at", ""),
        }

    # ────────────────────── 发布前内容检查 ──────────────────────

    async def check_content(
        self,
        content: str,
        scene: str = "",
        persona_id: str = "",
        account_id: str = "",
    ) -> Tuple[bool, str]:
        """发布前内容安全检查

        SAFE-501：content_check_enabled=false 时仅跳过可选内容规则，
        不跳过全局暂停/账号风险暂停/硬性长度限制。

        检查项（始终执行）：
        1. 硬性长度限制（min_length <= len <= max_length）

        检查项（content_check_enabled=true 时执行）：
        2. 敏感词（全文 + 正则）
        3. 重复度（与该账号最近 N 条比较）
        4. 人格一致性（persona_id 非空校验）

        Args:
            content: 待发布内容
            scene: 场景标识（如 reply_comment / proactive_comment / dynamic / weekly）
            persona_id: 当前人格 id
            account_id: 账号 ID（用于内容相似度隔离）

        Returns:
            (passed, reason) - passed=True 表示通过；passed=False 时 reason 给出失败原因
        """
        # 空内容
        if content is None:
            return False, "内容为空"
        text = str(content)

        # BUG B-001：全局暂停/账号风险暂停必须强制检查（docstring 承诺了但代码没做）
        if self.is_paused():
            return False, "global_paused"
        if account_id and self.is_account_paused(account_id):
            return False, f"account_paused:{account_id}"

        # ── 硬限制（始终执行，不受 content_check_enabled 影响）──
        passed, reason = self._check_hard_limits(text)
        if not passed:
            return False, reason

        # ── 可选内容规则（content_check_enabled=false 时跳过）──
        if not self.content_check_enabled:
            logger.debug("content_check_enabled=false，跳过可选内容规则")
            return True, "ok (content_check disabled, hard limits passed)"

        return self._check_content_rules(text, persona_id, account_id)

    def _check_hard_limits(self, text: str) -> Tuple[bool, str]:
        """硬性限制检查（始终执行，不受 content_check_enabled 影响）

        包括：长度限制（min_length / max_length）
        """
        if len(text) < self.min_length:
            return False, f"内容过短（{len(text)} < {self.min_length}）"
        if len(text) > self.max_length:
            return False, f"内容超长（{len(text)} > {self.max_length}）"
        return True, "ok"

    def _check_content_rules(
        self, text: str, persona_id: str = "", account_id: str = ""
    ) -> Tuple[bool, str]:
        """可选内容规则检查（content_check_enabled=true 时执行）

        包括：敏感词、重复度、人格一致性
        """
        # 敏感词检查
        hit, word = self.contains_sensitive_word(text)
        if hit:
            return False, f"包含敏感词: {word}"

        # 重复度检查（按账号隔离）
        if self.duplicate_check_enabled:
            dup_word = self._check_duplicate(text, account_id)
            if dup_word:
                return False, f"与近期内容重复度过高: {dup_word}"

        # 人格一致性（非空检查）
        if persona_id and not persona_id.strip():
            return False, "人格标识为空字符串"

        return True, "ok"

    def _check_duplicate(self, content: str, account_id: str = "") -> str:
        """检查与该账号最近 N 条内容的相似度

        SAFE-501：按 account_id 隔离，不同账号的最近内容互不影响。

        Returns:
            命中时返回最相似内容的预览（前 30 字符）；未命中返回空串
        """
        recent = self._get_recent_contents(account_id)
        if not recent:
            return ""
        try:
            for prev in recent:
                ratio = SequenceMatcher(None, content, prev).ratio()
                if ratio >= self.duplicate_threshold:
                    return prev[:30]
        except Exception as e:
            logger.warning(f"重复度检查异常: {e}")
        return ""

    # ────────────────────── 频率限制 ──────────────────────

    def _rate_key(self, scene: str, account_id: str = "") -> str:
        """构建限流键：account_id:scene（SAFE-501 账号级隔离）"""
        scene_part = scene or "_global_"
        account_part = account_id or "_global_"
        return f"{account_part}:{scene_part}"

    def _get_bucket(self, scene: str, account_id: str = "") -> Dict[str, Deque[float]]:
        """获取指定 account_id:scene 的频率桶（不存在则创建）"""
        key = self._rate_key(scene, account_id)
        if key not in self._rate_buckets:
            self._rate_buckets[key] = {
                "minute": deque(),
                "hour": deque(),
                "day": deque(),
            }
        return self._rate_buckets[key]

    # BUG B-002: 原子方法——在同一把锁内完成 trim + 判断 + record，
    # 避免 check 与 record 之间因 await 产生的竞态条件导致多协程同时通过。
    def check_and_record_rate_limit(
        self, scene: str = "", account_id: str = ""
    ) -> Tuple[bool, str]:
        """原子检查并记录频率限制（单窗口内 check + trim + record 不可分割）

        SAFE-501：限流键为 account_id:scene，账号间互不抢占配额。
        同时检查全局日配额（global_quota）。
        通过时同步写入 safety.db.rate_limit_events（重启后恢复近 24h 配额）。

        Args:
            scene: 场景标识；为空时使用默认桶 "_global_"
            account_id: 账号 ID；为空时使用 "_global_"

        Returns:
            (passed, reason) - passed=True 表示通过；passed=False 时 reason 给出失败原因
        """
        if not self.rate_limit_enabled:
            return True, "rate_limit_disabled"

        now = time.time()
        key = self._rate_key(scene, account_id)

        # BUG B-002: 获取或创建该 key 的锁（锁表插入需保护）
        with self._rate_locks_guard:
            if key not in self._rate_locks:
                self._rate_locks[key] = threading.Lock()
            bucket_lock = self._rate_locks[key]

        bucket = self._get_bucket(scene, account_id)
        windows = [
            ("minute", 60, self.rate_limits["per_minute"]),
            ("hour", 3600, self.rate_limits["per_hour"]),
            ("day", 86400, self.rate_limits["per_day"]),
        ]

        # 先持 per-key 锁完成 per-account-scene 检查并预占；
        # 再在持 bucket_lock 的同时拿 global_lock 做全局配额，避免全局超额回滚时
        # 无锁 pop per-key 桶的竞态。
        with bucket_lock:
            for name, window_secs, limit in windows:
                dq = bucket[name]
                cutoff = now - window_secs
                while dq and dq[0] < cutoff:
                    dq.popleft()
                if len(dq) >= limit:
                    return False, f"rate_limit:{key}:{name}({len(dq)}/{limit})"

            # 通过 per-key 检查后立即记录（仍在锁内）
            bucket["minute"].append(now)
            bucket["hour"].append(now)
            bucket["day"].append(now)

            # 全局日配额：在仍持有 bucket_lock 时获取 global_lock，
            # 超额回滚 per-key 时无需释放后再抢锁。
            with self._global_lock:
                global_dq = self._global_bucket["day"]
                global_cutoff = now - 86400
                while global_dq and global_dq[0] < global_cutoff:
                    global_dq.popleft()
                if len(global_dq) >= self.global_quota:
                    # 回滚 per-key 预占（已持 bucket_lock）
                    for name in ("minute", "hour", "day"):
                        dq = bucket[name]
                        if dq:
                            try:
                                dq.pop()
                            except IndexError:
                                pass
                    return False, f"global_quota({len(global_dq)}/{self.global_quota})"

                self._global_bucket["day"].append(now)

        # 内存已占额成功后再同步写 DB（失败仅告警，不回滚内存——下次重启可能少计 1 次，
        # 比写失败却让后续请求无限放行更安全；可接受）
        self._db_insert_rate_event(key, scene, account_id, now)

        return True, "ok"

    # BUG B-002: 标记为 deprecated，委托给 check_and_record_rate_limit
    def check_rate_limit(self, scene: str = "", account_id: str = "") -> bool:
        """[DEPRECATED] 检查当前是否允许发布（未超频）

        此方法存在竞态条件：check 与 record_publish 之间隔着 await，
        多个协程可同时通过检查。请改用 check_and_record_rate_limit。

        SAFE-501：限流键为 account_id:scene，账号间互不抢占配额。
        同时检查全局日配额（global_quota）。

        Args:
            scene: 场景标识；为空时使用默认桶 "_global_"
            account_id: 账号 ID；为空时使用 "_global_"

        Returns:
            True=允许发布，False=已超限
        """
        # BUG B-002: 委托给新原子方法（仅做检查，不记录——维持旧语义）
        if not self.rate_limit_enabled:
            return True

        now = time.time()
        key = self._rate_key(scene, account_id)
        bucket = self._get_bucket(scene, account_id)

        # BUG B-002: 在 per-key 锁内检查
        with self._rate_locks_guard:
            if key not in self._rate_locks:
                self._rate_locks[key] = threading.Lock()
        with self._rate_locks[key]:
            windows = [
                ("minute", 60, self.rate_limits["per_minute"]),
                ("hour", 3600, self.rate_limits["per_hour"]),
                ("day", 86400, self.rate_limits["per_day"]),
            ]
            for name, window_secs, limit in windows:
                dq = bucket[name]
                cutoff = now - window_secs
                while dq and dq[0] < cutoff:
                    dq.popleft()
                if len(dq) >= limit:
                    logger.info(
                        f"频率限制触发 key={key} window={name} "
                        f"count={len(dq)}/{limit}"
                    )
                    return False

        # BUG B-002: 在全局锁内检查
        with self._global_lock:
            global_dq = self._global_bucket["day"]
            global_cutoff = now - 86400
            while global_dq and global_dq[0] < global_cutoff:
                global_dq.popleft()
            if len(global_dq) >= self.global_quota:
                logger.info(
                    f"全局配额限制触发 count={len(global_dq)}/{self.global_quota}"
                )
                return False

        return True

    # BUG B-002: 标记为 deprecated，请改用 check_and_record_rate_limit
    def record_publish(self, scene: str = "", account_id: str = "") -> None:
        """[DEPRECATED] 记录一次发布（用于频率统计）

        此方法与 check_rate_limit 分离，存在竞态条件。
        请改用 check_and_record_rate_limit 原子方法。

        SAFE-501：同时记录到 account_id:scene 桶和全局桶，并写 DB。
        """
        key = self._rate_key(scene, account_id)
        with self._rate_locks_guard:
            if key not in self._rate_locks:
                self._rate_locks[key] = threading.Lock()
            bucket_lock = self._rate_locks[key]

        bucket = self._get_bucket(scene, account_id)
        now = time.time()
        with bucket_lock:
            bucket["minute"].append(now)
            bucket["hour"].append(now)
            bucket["day"].append(now)
            with self._global_lock:
                self._global_bucket["day"].append(now)
        self._db_insert_rate_event(key, scene, account_id, now)

    def refund_publish(self, scene: str = "", account_id: str = "") -> None:
        """Task 21.2：退回一次频率配额记录

        用于 check_and_record_rate_limit 预占配额后发布失败的场景。
        check_and_record_rate_limit 是"先扣减后执行"设计，失败时需退回。

        尽量按时间戳精确删除：在持锁下弹出本 key 各窗口最近一条，
        并用该时间戳从全局桶精确移除；同步删除 DB 中对应事件。
        """
        if not self.rate_limit_enabled:
            return
        key = self._rate_key(scene, account_id)
        with self._rate_locks_guard:
            if key not in self._rate_locks:
                self._rate_locks[key] = threading.Lock()
            bucket_lock = self._rate_locks[key]

        bucket = self._get_bucket(scene, account_id)
        refunded_ts: Optional[float] = None

        with bucket_lock:
            # 以 day 桶最近一条时间戳为准做精确退回
            day_dq = bucket["day"]
            if day_dq:
                try:
                    refunded_ts = day_dq.pop()
                except IndexError:
                    refunded_ts = None

            if refunded_ts is not None:
                for name in ("minute", "hour"):
                    dq = bucket[name]
                    # 从右向左找匹配时间戳；找不到则 pop 最近一条（兼容旧内存态）
                    removed = False
                    for i in range(len(dq) - 1, -1, -1):
                        if abs(dq[i] - refunded_ts) < 1e-6:
                            del dq[i]
                            removed = True
                            break
                    if not removed and dq:
                        try:
                            dq.pop()
                        except IndexError:
                            pass

            with self._global_lock:
                global_dq = self._global_bucket["day"]
                if refunded_ts is not None:
                    removed = False
                    for i in range(len(global_dq) - 1, -1, -1):
                        if abs(global_dq[i] - refunded_ts) < 1e-6:
                            del global_dq[i]
                            removed = True
                            break
                    if not removed and global_dq:
                        try:
                            global_dq.pop()
                        except IndexError:
                            pass
                elif global_dq:
                    try:
                        global_dq.pop()
                    except IndexError:
                        pass

        # 同步 DB：优先按精确时间戳删，否则删 key 最近一条
        if refunded_ts is not None:
            if not self._db_delete_rate_event_by_ts(key, refunded_ts):
                self._db_delete_latest_rate_event(key)
        else:
            self._db_delete_latest_rate_event(key)

    # BUG B-002: 原子方法——在同一把全局锁内完成 trim + 判断 + record，
    # 避免 check 与 record 之间因 await 产生的竞态条件。
    def check_and_record_global_quota(self) -> Tuple[bool, str]:
        """原子检查并记录全局日配额（trim + 判断 + record 不可分割）

        注意：此方法仅写全局桶内存，不写入 rate_limit_events（无 account/scene 键）。
        生产路径请使用 check_and_record_rate_limit。

        Returns:
            (passed, reason) - passed=True 表示通过；passed=False 时 reason 给出失败原因
        """
        if not self.rate_limit_enabled:
            return True, "rate_limit_disabled"

        now = time.time()

        # BUG B-002: 全局锁内原子完成 trim + 判断 + record
        with self._global_lock:
            global_dq = self._global_bucket["day"]
            global_cutoff = now - 86400
            while global_dq and global_dq[0] < global_cutoff:
                global_dq.popleft()
            if len(global_dq) >= self.global_quota:
                return False, f"global_quota({len(global_dq)}/{self.global_quota})"
            self._global_bucket["day"].append(now)

        return True, "ok"

    def record_content(self, text: str, account_id: str = "") -> None:
        """记录已发布内容（用于重复度检测）

        SAFE-501：按 account_id 隔离存储。
        """
        if not text:
            return
        recent = self._get_recent_contents(account_id)
        recent.append(text)

    def _get_recent_contents(self, account_id: str = "") -> Deque[str]:
        """获取指定账号的最近内容队列（不存在则创建）

        account_id 为空时使用 "_global_" 键（向后兼容）。
        """
        key = account_id or "_global_"
        if key not in self._recent_contents:
            self._recent_contents[key] = deque(maxlen=self.duplicate_check_n)
        return self._recent_contents[key]

    # ────────────────────── 热重载 ──────────────────────

    def reload_config(self, new_config: dict) -> None:
        """热重载安全配置

        SAFE-501：更新安全服务配置引用和所有派生参数。
        限流桶和最近内容历史保留（避免重载后丢失计数导致突发发布）。

        Args:
            new_config: 新的 SafetyChecker 配置字典（build_safety_config 输出格式）
        """
        old_check_n = self.duplicate_check_n
        self._load_config_params(new_config)

        # 如果 window_size 变化，调整已有 deque 的 maxlen
        if self.duplicate_check_n != old_check_n:
            for acc_id, dq in self._recent_contents.items():
                new_dq = deque(dq, maxlen=self.duplicate_check_n)
                self._recent_contents[acc_id] = new_dq

        # 重新加载敏感词
        self._load_sensitive_words_from_config()

        logger.info(
            f"SafetyChecker 配置已热重载: "
            f"content_check_enabled={self.content_check_enabled}, "
            f"rate_limits={self.rate_limits}, "
            f"global_quota={self.global_quota}, "
            f"duplicate_check_n={self.duplicate_check_n}"
        )

    # ────────────────────── 用户黑名单 ──────────────────────

    def add_to_blacklist(self, user_id: str, reason: str = "") -> None:
        """添加用户到黑名单（已存在则更新原因）"""
        if not user_id:
            return
        now = datetime.now().isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO user_blacklist (user_id, reason, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at",
                (str(user_id), reason, now),
            )
            conn.commit()
        logger.info(f"黑名单添加 user_id={user_id} reason={reason!r}")

    def remove_from_blacklist(self, user_id: str) -> None:
        """从黑名单移除用户"""
        if not user_id:
            return
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("DELETE FROM user_blacklist WHERE user_id = ?", (str(user_id),))
            conn.commit()
        logger.info(f"黑名单移除 user_id={user_id}")

    def is_blacklisted(self, user_id: str) -> bool:
        """判断用户是否在黑名单中

        fail-closed：DB 异常时返回 True（视为已拉黑），避免安全检查失效导致
        黑名单用户绕过限制。
        """
        if not user_id:
            return False
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                row = conn.execute(
                    "SELECT 1 FROM user_blacklist WHERE user_id = ?", (str(user_id),)
                ).fetchone()
            return row is not None
        except Exception as e:
            logger.warning(f"is_blacklisted DB error, fail-closed to True: {e}")
            return True

    def list_blacklist(self) -> List[Dict[str, Any]]:
        """列出黑名单全部记录"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT user_id, reason, created_at FROM user_blacklist ORDER BY created_at DESC"
                ).fetchall()
            return [
                {
                    "user_id": r["user_id"],
                    "reason": r["reason"] or "",
                    "created_at": r["created_at"] or "",
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"list_blacklist 查询失败: {e}")
            return []

    # ────────────────────── 敏感词过滤 ──────────────────────

    def _load_sensitive_words_from_config(self) -> None:
        """从 config.reply.block_keywords 加载敏感词"""
        reply_cfg = self.config.get("reply", {}) or {}
        words = reply_cfg.get("block_keywords", []) or []
        if isinstance(words, str):
            # 兼容逗号分隔字符串
            words = [w.strip() for w in words.replace("，", ",").split(",") if w.strip()]
        if isinstance(words, list):
            self.load_sensitive_words(words)

    def load_sensitive_words(self, words: List[str]) -> None:
        """加载敏感词列表

        自动识别正则：以 `re:` 前缀开头的视为正则模式，否则按全文子串匹配。
        重复调用会覆盖此前加载的列表。
        """
        self._sensitive_words = []
        self._sensitive_patterns = []
        for w in words or []:
            if not isinstance(w, str) or not w.strip():
                continue
            item = w.strip()
            if item.startswith("re:"):
                pattern_str = item[3:]
                try:
                    self._sensitive_patterns.append(re.compile(pattern_str))
                except re.error as e:
                    logger.warning(f"敏感词正则编译失败 {pattern_str!r}: {e}")
            else:
                self._sensitive_words.append(item)
        logger.info(
            f"加载敏感词 {len(self._sensitive_words)} 个，正则 {len(self._sensitive_patterns)} 个"
        )

    def contains_sensitive_word(self, text: str) -> Tuple[bool, str]:
        """检查文本是否包含敏感词

        Args:
            text: 待检查文本

        Returns:
            (hit, word) - hit=True 时 word 为命中的敏感词或正则模式串
        """
        if not text:
            return False, ""
        text_str = str(text)

        # BUG B-009：单字中文词加词边界避免误杀（如"死"不匹配"死亡"）；英文词大小写不敏感
        for word in self._sensitive_words:
            if not word:
                continue
            # 单字中文词用词边界：前后不能是汉字
            if len(word) == 1 and re.search(r'[\u4e00-\u9fff]', word):
                pattern = re.compile(r'(?<![\u4e00-\u9fff])' + re.escape(word) + r'(?![\u4e00-\u9fff])')
                if pattern.search(text_str):
                    return True, word
            # 英文词用大小写不敏感
            elif re.search(r'[a-zA-Z]', word):
                if word.lower() in text_str.lower():
                    return True, word
            # 其他（含 2 字及以上中文词）用子串匹配
            elif word in text_str:
                return True, word

        # BUG B-009：正则匹配加 IGNORECASE
        for pattern in self._sensitive_patterns:
            m = pattern.search(text_str, re.IGNORECASE if not pattern.flags & re.IGNORECASE else 0)
            if m:
                return True, m.group(0)

        return False, ""


def build_safety_config(raw_config: dict) -> dict:
    """从原始配置构建 SafetyChecker 配置字典

    PRD V4 BOOT-003：SafetyChecker 在 App 层创建，此函数统一构建配置，
    避免在 panel.py 和 app.py 中重复逻辑。

    SAFE-501：支持新版嵌套配置（rate_limit / content / duplicate_check）和旧版扁平配置。
    """
    safety_config: dict = {}
    try:
        reply_bkw = raw_config.get("reply", {}).get("block_keywords", [])
        safety_raw = raw_config.get("safety", {}) or {}

        # ── 频率限制配置 ──
        # 新版嵌套：safety.rate_limit.{per_minute, per_hour, per_day, global_quota, enabled}
        rate_limit_raw = safety_raw.get("rate_limit", {}) or {}
        # 旧版扁平：safety.rate_limit_per_minute 等
        per_minute = (
            rate_limit_raw.get("per_minute")
            if rate_limit_raw.get("per_minute") is not None
            else safety_raw.get("rate_limit_per_minute", 5)
        )
        per_hour = (
            rate_limit_raw.get("per_hour")
            if rate_limit_raw.get("per_hour") is not None
            else safety_raw.get("rate_limit_per_hour", 50)
        )
        per_day = (
            rate_limit_raw.get("per_day")
            if rate_limit_raw.get("per_day") is not None
            else safety_raw.get("rate_limit_per_day", 200)
        )
        global_quota = rate_limit_raw.get("global_quota", 1000)
        rate_enabled = rate_limit_raw.get("enabled", True)

        # ── 内容长度配置 ──
        # CFG-601：新版嵌套 safety.content.{min_length, max_length}
        # 旧版扁平 safety.min_content_length / safety.max_content_length（向后兼容）
        content_raw = safety_raw.get("content", {}) or {}
        min_length = content_raw.get("min_length")
        if min_length is None:
            min_length = safety_raw.get("min_content_length", 2)
        max_length = content_raw.get("max_length")
        if max_length is None:
            max_length = safety_raw.get("max_content_length", 2000)

        # ── 内容检查开关 ──
        content_check_enabled = safety_raw.get("content_check_enabled", True)

        # ── 重复度配置 ──
        # 新版嵌套：safety.duplicate_check.{enabled, window_size, similarity_threshold}
        dup_check_raw = safety_raw.get("duplicate_check", {}) or {}
        dup_enabled = dup_check_raw.get("enabled", True)
        dup_window = dup_check_raw.get("window_size", 10)
        dup_threshold = (
            dup_check_raw.get("similarity_threshold")
            if dup_check_raw.get("similarity_threshold") is not None
            else safety_raw.get("similarity_threshold", 0.8)
        )

        safety_config = {
            "rate_limit": {
                "enabled": rate_enabled,
                "per_minute": int(per_minute),
                "per_hour": int(per_hour),
                "per_day": int(per_day),
                "global_quota": int(global_quota),
            },
            "content": {
                "min_length": int(min_length),
                "max_length": int(max_length),
            },
            "content_check_enabled": bool(content_check_enabled),
            "duplicate_check": {
                "enabled": bool(dup_enabled),
                "window_size": int(dup_window),
                "similarity_threshold": float(dup_threshold),
            },
            "reply": {"block_keywords": reply_bkw},
        }
    except Exception as e:
        logger.warning(f"构建安全配置失败: {e}")
    return safety_config
