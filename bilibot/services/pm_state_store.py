"""
私信独立幂等状态机（PRD-V5 §6.3 / PM-501）

状态流转：
    discovered → ignored | context_building
    context_building → generation_pending | deferred
    generation_pending → safety_pending | deferred
    safety_pending → publish_pending | rejected | deferred
    publish_pending → published | retry_wait | result_unknown
    retry_wait → publish_pending | failed

终态（不再处理）：published, ignored, rejected, failed
非终态（可恢复）：deferred, retry_wait, result_unknown（不自动重发）, 以及所有中间态

幂等键：account_id + platform_message_id（PRD-V5 §6.3 / PM-501）
        私信幂等键使用平台消息 ID，不得用 talker_id + 内容前 50 字。

独立退避：PM 拥有独立的 max_attempts 和 backoff_base_seconds 配置，
          与评论回复的退避参数互不影响。

隐私边界（PRD-V5 §6.3 / PM-501）：
    - 私信原文仅存储在本状态库（pm_state_store），不进入 KnowledgeBaseMemory。
    - 本状态库按账号隔离（每账号独立 DB 文件），不写入他账号数据目录。
"""
import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("bilibot.pm_state")

# 终态集合
TERMINAL_STATUSES = frozenset({
    "published", "ignored", "rejected", "failed",
})

# 可重试状态（非终态，可恢复）
RETRYABLE_STATUSES = frozenset({"deferred", "retry_wait"})

# 不自动重发的非终态（平台结果不确定，需人工对账）
NO_AUTOREPUBLISH_STATUSES = frozenset({"result_unknown"})

# 默认重试上限（PM 独立于评论回复）
DEFAULT_PM_MAX_ATTEMPTS = 3

# 默认退避基数（秒）— PM 独立配置
DEFAULT_PM_BACKOFF_BASE_SECONDS = 30

# 退避上限（秒）
_BACKOFF_CAP_SECONDS = 1800  # 30 分钟

# 合法状态转移
_VALID_TRANSITIONS: Dict[str, frozenset] = {
    "discovered": frozenset({
        "ignored", "context_building", "generation_pending",
    }),
    "context_building": frozenset({
        "generation_pending", "deferred", "ignored",
    }),
    "generation_pending": frozenset({
        "safety_pending", "deferred", "ignored",
    }),
    "safety_pending": frozenset({
        "publish_pending", "rejected", "deferred",
    }),
    "publish_pending": frozenset({
        "published", "retry_wait", "result_unknown", "failed",
    }),
    "retry_wait": frozenset({
        "publish_pending", "failed",
    }),
    "deferred": frozenset({
        "discovered", "context_building", "generation_pending", "failed",
    }),
    # 终态不再转移
    "published": frozenset(),
    "ignored": frozenset(),
    "rejected": frozenset(),
    "failed": frozenset(),
    # result_unknown 不自动重发，仅人工触发可转移
    "result_unknown": frozenset({"published", "failed", "publish_pending"}),
}


@dataclass
class PrivateMessageState:
    """私信状态记录（PRD-V5 §6.3 / PM-501）"""
    id: int = 0
    account_id: str = ""
    platform_message_id: str = ""
    talker_id: str = ""
    status: str = "discovered"
    generation_text: str = ""
    generation_hash: str = ""
    persona_id: str = ""
    attempt: int = 0
    max_attempts: int = DEFAULT_PM_MAX_ATTEMPTS
    next_retry_at: Optional[float] = None
    last_error_code: str = ""
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    published_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Optional[sqlite3.Row]) -> Optional["PrivateMessageState"]:
        if row is None:
            return None
        d = dict(row)
        import json as _json
        meta = d.get("metadata") or "{}"
        try:
            meta_dict = _json.loads(meta) if isinstance(meta, str) else (meta or {})
        except (ValueError, TypeError):
            meta_dict = {}
        return cls(
            id=int(d.get("id", 0) or 0),
            account_id=str(d.get("account_id", "") or ""),
            platform_message_id=str(d.get("platform_message_id", "") or ""),
            talker_id=str(d.get("talker_id", "") or ""),
            status=str(d.get("status", "discovered") or "discovered"),
            generation_text=str(d.get("generation_text", "") or ""),
            generation_hash=str(d.get("generation_hash", "") or ""),
            persona_id=str(d.get("persona_id", "") or ""),
            attempt=int(d.get("attempt", 0) or 0),
            max_attempts=int(d.get("max_attempts", DEFAULT_PM_MAX_ATTEMPTS) or DEFAULT_PM_MAX_ATTEMPTS),
            next_retry_at=d.get("next_retry_at"),
            last_error_code=str(d.get("last_error_code", "") or ""),
            last_error=str(d.get("last_error", "") or ""),
            created_at=float(d.get("created_at", 0.0) or 0.0),
            updated_at=float(d.get("updated_at", 0.0) or 0.0),
            published_at=d.get("published_at"),
            metadata=meta_dict,
        )

    def to_dict(self) -> Dict[str, Any]:
        import json as _json
        d = {
            "id": self.id,
            "account_id": self.account_id,
            "platform_message_id": self.platform_message_id,
            "talker_id": self.talker_id,
            "status": self.status,
            "generation_text": self.generation_text,
            "generation_hash": self.generation_hash,
            "persona_id": self.persona_id,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "next_retry_at": self.next_retry_at,
            "last_error_code": self.last_error_code,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "published_at": self.published_at,
            "metadata": _json.dumps(self.metadata, ensure_ascii=False),
        }
        return d


class PrivateMessageStateStore:
    """私信状态存储（SQLite，每账号一个实例）

    PRD-V5 §6.3 / PM-501：
    - 幂等键为 account_id + platform_message_id（B站平台消息 ID）
    - 独立的 max_attempts 和 backoff_base_seconds，与评论回复互不影响
    - 私信原文仅存于此库，不进入 KnowledgeBaseMemory
    - 按账号隔离（DB 文件位于账号数据目录）
    """

    def __init__(
        self,
        data_dir: str,
        account_id: str = "",
        max_attempts: int = DEFAULT_PM_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_PM_BACKOFF_BASE_SECONDS,
    ):
        self.data_dir = str(data_dir)
        self.account_id = str(account_id)
        self.max_attempts = int(max_attempts)
        self.backoff_base_seconds = float(backoff_base_seconds)
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(self.data_dir) / "pm_states.db")
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """PRD V4 MEM-002：短连接模式"""
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pm_states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    platform_message_id TEXT NOT NULL,
                    talker_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'discovered',
                    generation_text TEXT NOT NULL DEFAULT '',
                    generation_hash TEXT NOT NULL DEFAULT '',
                    persona_id TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_retry_at REAL,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    published_at REAL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(account_id, platform_message_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_status ON pm_states(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_retry ON pm_states(next_retry_at) "
                "WHERE next_retry_at IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_account ON pm_states(account_id)"
            )
            conn.commit()
        finally:
            conn.close()

    # ───────────────────────────────────────────────────────
    # 幂等键
    # ───────────────────────────────────────────────────────

    @staticmethod
    def make_idempotency_key(account_id: str, platform_message_id: str) -> str:
        """构造幂等键（PRD-V5 §6.3 / PM-501）

        account_id + platform_message_id：
        - 平台消息 ID 唯一标识一条私信，不用 talker_id+内容前 50 字
        - account_id 隔离不同账号
        """
        return f"{account_id}:pm:{platform_message_id}"

    # ───────────────────────────────────────────────────────
    # 查询
    # ───────────────────────────────────────────────────────

    def _row_to_state(self, row) -> Optional[PrivateMessageState]:
        return PrivateMessageState.from_row(row)

    def get_by_id(self, state_id: int) -> Optional[PrivateMessageState]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM pm_states WHERE id=?",
                (int(state_id),),
            ).fetchone()
            return self._row_to_state(row)
        finally:
            conn.close()

    def get_by_message_id(
        self, account_id: str, platform_message_id: str,
    ) -> Optional[PrivateMessageState]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM pm_states "
                "WHERE account_id=? AND platform_message_id=?",
                (str(account_id), str(platform_message_id)),
            ).fetchone()
            return self._row_to_state(row)
        finally:
            conn.close()

    def is_terminal(self, account_id: str, platform_message_id: str) -> bool:
        state = self.get_by_message_id(account_id, platform_message_id)
        if not state:
            return False
        return state.status in TERMINAL_STATUSES

    def is_processed(self, account_id: str, platform_message_id: str) -> bool:
        return self.get_by_message_id(account_id, platform_message_id) is not None

    # ───────────────────────────────────────────────────────
    # 幂等发现
    # ───────────────────────────────────────────────────────

    def ensure_discovered(
        self,
        account_id: str,
        platform_message_id: str,
        talker_id: str,
    ) -> PrivateMessageState:
        """幂等发现私信（PRD-V5 §6.3 / PM-501）

        - 同一 (account_id, platform_message_id) 只创建一条记录
        - 已存在则返回原记录（不覆盖 status / generation_text 等）
        - 新记录 status='discovered'
        """
        if not platform_message_id:
            raise ValueError("platform_message_id 不能为空（PM-501 幂等键依赖平台消息 ID）")
        account_id = str(account_id)
        platform_message_id = str(platform_message_id)
        talker_id = str(talker_id or "")
        now = time.time()

        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO pm_states
                    (account_id, platform_message_id, talker_id, status,
                     attempt, max_attempts, created_at, updated_at)
                VALUES (?, ?, ?, 'discovered', 0, ?, ?, ?)
                ON CONFLICT(account_id, platform_message_id) DO NOTHING
            """, (
                account_id, platform_message_id, talker_id,
                self.max_attempts, now, now,
            ))
            conn.commit()
        finally:
            conn.close()
        state = self.get_by_message_id(account_id, platform_message_id)
        return state

    # ───────────────────────────────────────────────────────
    # 状态转移
    # ───────────────────────────────────────────────────────

    def _validate_transition(self, old_status: str, new_status: str) -> None:
        allowed = _VALID_TRANSITIONS.get(old_status)
        if allowed is None:
            raise ValueError(f"未知状态: {old_status!r}")
        if new_status not in allowed:
            raise ValueError(
                f"非法状态转移: {old_status!r} → {new_status!r}，"
                f"允许: {sorted(allowed)}"
            )

    def update_status(
        self,
        state_id: int,
        new_status: str,
        **fields,
    ) -> PrivateMessageState:
        """状态转移（带校验）

        可选字段：last_error, last_error_code, persona_id, next_retry_at,
                 attempt, published_at, metadata
        """
        existing = self.get_by_id(state_id)
        if existing is None:
            raise ValueError(f"状态记录不存在: id={state_id}")

        # result_unknown 不自动重发（PRD-V5 §6.3 / PM-501）：
        # 仅当调用方显式传入 force=True 才允许从 result_unknown 转出。
        force = bool(fields.pop("force", False))
        self._validate_transition(existing.status, new_status)
        if existing.status == "result_unknown" and not force:
            raise ValueError(
                f"result_unknown 状态不自动重发（PM-501），"
                f"如需转 {new_status!r} 请显式传入 force=True"
            )

        now = time.time()
        allowed_cols = {
            "last_error", "last_error_code", "persona_id", "next_retry_at",
            "attempt", "published_at", "metadata",
        }
        sets = ["status=?", "updated_at=?"]
        params: list = [new_status, now]

        for k, v in fields.items():
            if k not in allowed_cols:
                continue
            if k == "metadata":
                import json as _json
                v = _json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else str(v)
            sets.append(f"{k}=?")
            params.append(v)

        if new_status == "published" and "published_at" not in fields:
            sets.append("published_at=?")
            params.append(now)

        params.append(int(state_id))
        conn = self._get_conn()
        try:
            conn.execute(
                f"UPDATE pm_states SET {', '.join(sets)} WHERE id=?",
                params,
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_by_id(state_id)

    # ───────────────────────────────────────────────────────
    # 生成文本持久化
    # ───────────────────────────────────────────────────────

    @staticmethod
    def compute_generation_hash(text: str) -> str:
        """计算生成文本的内容哈希（SHA-256）"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def save_generation_result(
        self,
        state_id: int,
        text: str,
        hash_value: str = "",
        persona_id: str = "",
    ) -> PrivateMessageState:
        """持久化生成文本（安全检查之前调用）

        - 写入 generation_text / generation_hash / persona_id
        - 不改变 status（由调用方在安全检查后自行推进）
        - hash_value 为空时自动计算
        """
        if not text:
            return self.get_by_id(state_id)
        gen_hash = hash_value or self.compute_generation_hash(text)
        now = time.time()
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE pm_states SET
                    generation_text=?,
                    generation_hash=?,
                    persona_id=COALESCE(NULLIF(?, ''), persona_id),
                    updated_at=?
                WHERE id=?
            """, (
                text, gen_hash,
                persona_id or "",
                now,
                int(state_id),
            ))
            conn.commit()
        finally:
            conn.close()
        return self.get_by_id(state_id)

    # ───────────────────────────────────────────────────────
    # 终态 / 重试 标记
    # ───────────────────────────────────────────────────────

    def mark_published(self, state_id: int) -> PrivateMessageState:
        return self.update_status(state_id, "published")

    def mark_ignored(self, state_id: int, rule: str = "") -> PrivateMessageState:
        return self.update_status(
            state_id, "ignored",
            last_error=rule, last_error_code="BUSINESS_RULE",
        )

    def mark_rejected(self, state_id: int, reason: str = "") -> PrivateMessageState:
        return self.update_status(
            state_id, "rejected",
            last_error=reason, last_error_code="SAFETY_REJECT",
        )

    def mark_failed(
        self, state_id: int, error_code: str = "PM_FAILED", error: str = "",
    ) -> PrivateMessageState:
        return self.update_status(
            state_id, "failed",
            last_error_code=error_code, last_error=error,
        )

    def mark_result_unknown(
        self, state_id: int, error_code: str = "PM_RESULT_UNKNOWN", error: str = "",
    ) -> PrivateMessageState:
        """平台成功本地失败 → result_unknown（不自动重发）

        PRD-V5 §6.3 / PM-501：result_unknown 状态不会自动重发，
        需人工对账后通过 force=True 显式触发转移。
        """
        return self.update_status(
            state_id, "result_unknown",
            last_error_code=error_code, last_error=error,
        )

    def mark_retry_wait(
        self,
        state_id: int,
        error_code: str = "PM_PUBLISH_FAILED",
        error: str = "",
    ) -> PrivateMessageState:
        """标记为等待重试（PM 独立退避）

        - attempt 自动递增
        - 超过 max_attempts → failed
        - next_retry_at = now + backoff_base * 2^attempt + jitter（独立于评论回复）
        """
        existing = self.get_by_id(state_id)
        if existing is None:
            raise ValueError(f"状态记录不存在: id={state_id}")

        new_attempt = existing.attempt + 1
        if new_attempt >= existing.max_attempts:
            # 超过上限 → failed（终态）
            return self.update_status(
                state_id, "failed",
                attempt=new_attempt,
                last_error_code=error_code,
                last_error=error or f"超过最大重试次数 {existing.max_attempts}",
            )

        now = time.time()
        import random
        base = self.backoff_base_seconds * (2 ** new_attempt)
        base = min(base, _BACKOFF_CAP_SECONDS)
        jitter = random.uniform(0, base * 0.1)
        next_retry_at = now + base + jitter

        return self.update_status(
            state_id, "retry_wait",
            attempt=new_attempt,
            next_retry_at=next_retry_at,
            last_error_code=error_code,
            last_error=error,
        )

    def mark_deferred(
        self, state_id: int, reason: str = "", error_code: str = "PM_TEMP_FAILURE",
    ) -> PrivateMessageState:
        """标记为延迟（非终态 - 临时失败，可恢复）"""
        now = time.time()
        return self.update_status(
            state_id, "deferred",
            next_retry_at=now + 60,
            last_error=reason, last_error_code=error_code,
        )

    # ───────────────────────────────────────────────────────
    # 重试列表
    # ───────────────────────────────────────────────────────

    def list_retry_wait(
        self, account_id: str = "", now: float = None,
    ) -> List[PrivateMessageState]:
        """获取可重试的私信（retry_wait 且 next_retry_at <= now）

        PRD-V5 §6.3 / PM-501：PM 独立退避，与评论回复列表互不影响。
        """
        now = now or time.time()
        acc = str(account_id or self.account_id)
        conn = self._get_conn()
        try:
            if acc:
                rows = conn.execute(
                    "SELECT * FROM pm_states "
                    "WHERE account_id=? AND status='retry_wait' "
                    "AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
                    "ORDER BY next_retry_at ASC LIMIT 50",
                    (acc, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pm_states "
                    "WHERE status='retry_wait' "
                    "AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
                    "ORDER BY next_retry_at ASC LIMIT 50",
                    (now,),
                ).fetchall()
            return [self._row_to_state(r) for r in rows]
        finally:
            conn.close()

    def list_result_unknown(self, account_id: str = "") -> List[PrivateMessageState]:
        """获取 result_unknown 状态的私信（人工对账用，不自动重发）"""
        acc = str(account_id or self.account_id)
        conn = self._get_conn()
        try:
            if acc:
                rows = conn.execute(
                    "SELECT * FROM pm_states "
                    "WHERE account_id=? AND status='result_unknown' "
                    "ORDER BY updated_at ASC LIMIT 50",
                    (acc,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pm_states "
                    "WHERE status='result_unknown' "
                    "ORDER BY updated_at ASC LIMIT 50",
                ).fetchall()
            return [self._row_to_state(r) for r in rows]
        finally:
            conn.close()

    # ───────────────────────────────────────────────────────
    # 统计
    # ───────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, int]:
        conn = self._get_conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM pm_states WHERE account_id=?",
                (self.account_id,),
            ).fetchone()[0]
            by_status: Dict[str, int] = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM pm_states "
                "WHERE account_id=? GROUP BY status",
                (self.account_id,),
            ).fetchall():
                by_status[row["status"]] = row["cnt"]
            return {"total": total, "by_status": by_status}
        finally:
            conn.close()

    def close(self):
        """短连接模式无需关闭"""
        pass


def extract_platform_message_id(last_msg: Dict[str, Any]) -> str:
    """从 B站 PM last_msg 中提取平台消息 ID（PM-501 幂等键）

    B站私信 API 返回的 last_msg 可能含以下字段（按优先级）：
    - msg_id / msg_key / msg_seq / seq_id：平台消息唯一标识
    - 退化为 sender_uid + msg_timestamp：极端兜底，保证幂等键存在
    """
    if not isinstance(last_msg, dict):
        return ""
    for key in ("msg_id", "msg_key", "msg_seq", "seq_id"):
        val = last_msg.get(key)
        if val is not None and str(val) != "":
            return str(val)
    # 兜底：sender_uid + timestamp（仍优于 talker_id+内容前 50 字）
    sender = last_msg.get("sender_uid", "") or last_msg.get("receiver_id", "")
    ts = last_msg.get("timestamp", "") or last_msg.get("msg_timestamp", "")
    if sender and ts:
        return f"fallback:{sender}:{ts}"
    return ""
