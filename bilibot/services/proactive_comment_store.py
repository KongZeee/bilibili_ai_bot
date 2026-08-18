"""
主动评论原子幂等状态机（PRD-V5 §10.2 COM-501）

为主动评论发布提供原子 claim 生命周期，解决定时与手动并发竞态：

    claimed → publishing → published
    failure → retry_wait (with backoff) | failed (max attempts reached)
    platform success + local failure → result_unknown (NOT auto-republish)

PRD-V5 §10.2 COM-501：
- 同账号同视频最多一条成功主动评论
- 定时与手动并发只能一个 claim 成功（部分唯一索引保证）
- 平台成功本地失败 → result_unknown 不自动重发
- 幂等键：account_id:bvid:proactive_comment

参考：task_store.py / dynamic_draft_store.py 的短连接 + 事务性条件更新模式。
"""
import hashlib
import logging
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

logger = logging.getLogger("bilibot.proactive_comment_store")

# ═══════════════════════════════════════════════════════
#  状态常量
# ═══════════════════════════════════════════════════════

# 非终态
STATUS_CLAIMED = "claimed"
STATUS_PUBLISHING = "publishing"
STATUS_RETRY_WAIT = "retry_wait"

# 终态
STATUS_PUBLISHED = "published"
STATUS_RESULT_UNKNOWN = "result_unknown"
STATUS_FAILED = "failed"

# 阻止新 claim 的状态（部分唯一索引覆盖范围）
# failed 不阻止：max_attempts 用尽后理论上不会再 claim，但保留灵活性
BLOCKING_STATUSES = frozenset({
    STATUS_CLAIMED, STATUS_PUBLISHING, STATUS_PUBLISHED,
    STATUS_RETRY_WAIT, STATUS_RESULT_UNKNOWN,
})

TERMINAL_STATUSES = frozenset({
    STATUS_PUBLISHED, STATUS_RESULT_UNKNOWN, STATUS_FAILED,
})

DEFAULT_MAX_ATTEMPTS = 3

# 默认退避基数上限（秒）
DEFAULT_BACKOFF_CAP = 300

# Task 4：publishing 状态租约时长（秒），超时视为崩溃
DEFAULT_LEASE_SECONDS = 600
# C6：claimed 状态租约（生成/安全检查阶段）；超时释放以便重 claim
DEFAULT_CLAIMED_LEASE_SECONDS = 900


def default_idempotency_key(account_id: str, bvid: str) -> str:
    """COM-501 幂等键：account_id:bvid:proactive_comment"""
    return f"{account_id}:{bvid}:proactive_comment"


def compute_generation_hash(text: str) -> str:
    """计算生成文本哈希（用于校验重试文本完整性）"""
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass
class ProactiveCommentAction:
    """主动评论动作记录（对应 proactive_comment_actions 表一行）"""
    action_id: str
    account_id: str
    bvid: str
    persona_id: str = ""
    task_id: str = ""
    status: str = STATUS_CLAIMED
    generation_text: str = ""
    generation_hash: str = ""
    idempotency_key: str = ""
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    last_error_code: str = ""
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    published_at: Optional[float] = None
    next_retry_at: Optional[float] = None
    lease_until: Optional[float] = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def is_published(self) -> bool:
        return self.status == STATUS_PUBLISHED

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ProactiveCommentAction":
        return cls(
            action_id=row["action_id"],
            account_id=row["account_id"],
            bvid=row["bvid"],
            persona_id=row["persona_id"] or "",
            task_id=row["task_id"] or "",
            status=row["status"],
            generation_text=row["generation_text"] or "",
            generation_hash=row["generation_hash"] or "",
            idempotency_key=row["idempotency_key"] or "",
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            last_error_code=row["last_error_code"] or "",
            last_error=row["last_error"] or "",
            created_at=row["created_at"] or 0.0,
            updated_at=row["updated_at"] or 0.0,
            published_at=row["published_at"],
            next_retry_at=row["next_retry_at"],
            lease_until=row["lease_until"] if "lease_until" in row.keys() else None,
        )


class ProactiveCommentStore:
    """主动评论原子幂等存储（SQLite，短连接）

    PRD-V5 §10.2 COM-501：
    - claim 通过部分唯一索引 (account_id, bvid) WHERE status IN BLOCKING 实现
    - 同账号同视频已有 claimed/publishing/published/retry_wait/result_unknown 时，
      新 claim 直接 IntegrityError → 返回 None
    - failed 不阻止新 claim（max_attempts 由调用方控制）
    """

    def __init__(self, db_path: str, account_id: str = ""):
        self.db_path = db_path
        self.account_id = account_id
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """短连接模式（参考 task_store.py / reply_state.py）"""
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proactive_comment_actions (
                    action_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    bvid TEXT NOT NULL,
                    persona_id TEXT,
                    task_id TEXT,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    generation_text TEXT DEFAULT '',
                    generation_hash TEXT DEFAULT '',
                    idempotency_key TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error_code TEXT DEFAULT '',
                    last_error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    published_at REAL,
                    next_retry_at REAL,
                    lease_until REAL
                )
            """)
            # Task 4：为旧库补 lease_until 列（publishing 状态租约，崩溃恢复用）
            cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(proactive_comment_actions)"
            ).fetchall()}
            if "lease_until" not in cols:
                conn.execute(
                    "ALTER TABLE proactive_comment_actions ADD COLUMN lease_until REAL"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pca_account_bvid "
                "ON proactive_comment_actions(account_id, bvid)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pca_status "
                "ON proactive_comment_actions(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pca_retry "
                "ON proactive_comment_actions(next_retry_at) "
                "WHERE next_retry_at IS NOT NULL"
            )
            # COM-501 核心：部分唯一索引
            # 同账号同视频只要存在 claimed/publishing/published/retry_wait/result_unknown
            # 任意一个，新 INSERT 就会冲突 → claim 失败
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_pca_active "
                "ON proactive_comment_actions(account_id, bvid) "
                "WHERE status IN ('claimed', 'publishing', 'published', "
                "'retry_wait', 'result_unknown')"
            )
            conn.commit()
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  Create / Claim
    # ═══════════════════════════════════════════════════════

    def claim(
        self,
        account_id: str,
        bvid: str,
        task_id: str = "",
        idempotency_key: Optional[str] = None,
        persona_id: str = "",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: Optional[float] = None,
        lease_seconds: int = DEFAULT_CLAIMED_LEASE_SECONDS,
    ) -> Optional[ProactiveCommentAction]:
        """原子 claim：创建一条 claimed 状态的动作记录

        PRD-V5 §10.2 COM-501：
        - 部分唯一索引保证同账号同视频只能有一个活跃动作
        - 冲突时返回 None（调用方跳过，另一 worker 已在处理）
        - C6：写入 lease_until，崩溃卡在 claimed 时可被 recover_stuck_claimed 释放
        """
        now = now or time.time()
        action_id = f"pca_{uuid.uuid4().hex[:16]}"
        if idempotency_key is None:
            idempotency_key = default_idempotency_key(account_id, bvid)
        lease_until = now + max(60, int(lease_seconds or DEFAULT_CLAIMED_LEASE_SECONDS))

        conn = self._get_conn()
        try:
            try:
                conn.execute(
                    """
                    INSERT INTO proactive_comment_actions
                        (action_id, account_id, bvid, persona_id, task_id, status,
                         generation_text, generation_hash, idempotency_key,
                         attempt, max_attempts, last_error_code, last_error,
                         created_at, updated_at, published_at, next_retry_at,
                         lease_until)
                    VALUES (?, ?, ?, ?, ?, ?, '', '', ?, 0, ?, '', '', ?, ?, NULL, NULL, ?)
                    """,
                    (
                        action_id, account_id, bvid, persona_id, task_id,
                        STATUS_CLAIMED, idempotency_key, max_attempts,
                        now, now, lease_until,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # 部分唯一索引冲突：已存在同 account+bvid 的活跃动作
                conn.rollback()
                return None
        finally:
            conn.close()

        return self.get(action_id)

    # ═══════════════════════════════════════════════════════
    #  Read
    # ═══════════════════════════════════════════════════════

    def get(self, action_id: str) -> Optional[ProactiveCommentAction]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM proactive_comment_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            return ProactiveCommentAction.from_row(row) if row else None
        finally:
            conn.close()

    def get_by_bvid(
        self, account_id: str, bvid: str,
    ) -> Optional[ProactiveCommentAction]:
        """取同账号同视频的（任意状态）最新一条动作记录"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM proactive_comment_actions "
                "WHERE account_id=? AND bvid=? "
                "ORDER BY created_at DESC LIMIT 1",
                (account_id, bvid),
            ).fetchone()
            return ProactiveCommentAction.from_row(row) if row else None
        finally:
            conn.close()

    def delete(self, action_id: str) -> bool:
        """删除一条动作记录（用于 failed 状态的手动重试，释放 claim 锁）"""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM proactive_comment_actions WHERE action_id=?",
                (action_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reset_for_immediate_retry(self, action_id: str) -> bool:
        """将 retry_wait 的动作重置为立即可重试（next_retry_at=0）。

        result_unknown 需 force 时请用 schedule_manual_retry。
        """
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET next_retry_at=0, updated_at=? "
                "WHERE action_id=? AND status=?",
                (time.time(), action_id, STATUS_RETRY_WAIT),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def schedule_manual_retry(
        self,
        account_id: str,
        bvid: str,
        *,
        force: bool = False,
        generation_text: str = "",
        persona_id: str = "",
    ) -> Tuple[bool, str, Optional[str]]:
        """控制台/API 手动重试：只改 store，不直接发帖（防与调度并发双发）。

        Returns:
            (ok, code, action_id)
            code: scheduled | already_published | in_progress | needs_force |
                  not_found | invalid
        """
        account_id = str(account_id or self.account_id or "")
        bvid = str(bvid or "").strip()
        if not account_id or not bvid:
            return False, "invalid", None
        now = time.time()
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM proactive_comment_actions "
                "WHERE account_id=? AND bvid=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (account_id, bvid),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False, "not_found", None
            status = row["status"]
            action_id = row["action_id"]
            if status == STATUS_PUBLISHED:
                conn.rollback()
                return True, "already_published", action_id
            if status in (STATUS_CLAIMED, STATUS_PUBLISHING):
                conn.rollback()
                return False, "in_progress", action_id
            if status == STATUS_RESULT_UNKNOWN and not force:
                conn.rollback()
                return False, "needs_force", action_id
            # retry_wait / failed / result_unknown(+force) → retry_wait 立即拾取
            if status not in (
                STATUS_RETRY_WAIT, STATUS_FAILED, STATUS_RESULT_UNKNOWN,
            ):
                conn.rollback()
                return False, "invalid", action_id
            gen = generation_text or (row["generation_text"] or "")
            gen_hash = compute_generation_hash(gen) if gen else (row["generation_hash"] or "")
            persona = persona_id or (row["persona_id"] or "")
            # failed 时抬高 max_attempts 以便再试一次
            max_att = int(row["max_attempts"] or DEFAULT_MAX_ATTEMPTS)
            attempt = int(row["attempt"] or 0)
            if status == STATUS_FAILED and attempt >= max_att:
                max_att = attempt + 1
            conn.execute(
                "UPDATE proactive_comment_actions SET "
                "status=?, next_retry_at=0, updated_at=?, "
                "generation_text=?, generation_hash=?, persona_id=?, "
                "max_attempts=?, last_error_code='MANUAL_RETRY', last_error='manual_retry', "
                "lease_until=NULL "
                "WHERE action_id=? AND status=?",
                (
                    STATUS_RETRY_WAIT, now, gen, gen_hash, persona, max_att,
                    action_id, status,
                ),
            )
            conn.commit()
            return True, "scheduled", action_id
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def has_published(self, account_id: str, bvid: str) -> bool:
        """是否已存在 published 动作（同账号同视频最多一条成功）"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM proactive_comment_actions "
                "WHERE account_id=? AND bvid=? AND status=? LIMIT 1",
                (account_id, bvid, STATUS_PUBLISHED),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def list_pending_retry(
        self, account_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> List[ProactiveCommentAction]:
        """获取可重试的 retry_wait 动作（next_retry_at <= now）"""
        now = now or time.time()
        conn = self._get_conn()
        try:
            if account_id:
                rows = conn.execute(
                    "SELECT * FROM proactive_comment_actions "
                    "WHERE account_id=? AND status=? "
                    "AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
                    "ORDER BY next_retry_at ASC LIMIT 50",
                    (account_id, STATUS_RETRY_WAIT, now),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM proactive_comment_actions "
                    "WHERE status=? "
                    "AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
                    "ORDER BY next_retry_at ASC LIMIT 50",
                    (STATUS_RETRY_WAIT, now),
                ).fetchall()
            return [ProactiveCommentAction.from_row(r) for r in rows]
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  State transitions（事务性条件更新）
    # ═══════════════════════════════════════════════════════

    def save_generation(
        self, action_id: str, text: str,
        now: Optional[float] = None,
    ) -> bool:
        """保存生成的评论文本 + hash（claim 后、publishing 前）"""
        now = now or time.time()
        gen_hash = compute_generation_hash(text)
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET generation_text=?, generation_hash=?, updated_at=? "
                "WHERE action_id=? AND status=?",
                (text, gen_hash, now, action_id, STATUS_CLAIMED),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_publishing(self, action_id: str, now: Optional[float] = None) -> bool:
        """claimed/retry_wait → publishing（API 调用前）

        Task 4：写入 lease_until（now + DEFAULT_LEASE_SECONDS），
        若进程在此之后崩溃，recover_stuck_publishing 会将其转为 result_unknown。
        """
        now = now or time.time()
        lease_until = now + DEFAULT_LEASE_SECONDS
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, updated_at=?, next_retry_at=NULL, lease_until=? "
                "WHERE action_id=? AND status IN (?, ?)",
                (STATUS_PUBLISHING, now, lease_until, action_id,
                 STATUS_CLAIMED, STATUS_RETRY_WAIT),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_published(
        self, action_id: str, published_at: Optional[float] = None,
        now: Optional[float] = None,
    ) -> bool:
        """publishing → published（API 成功）"""
        now = now or time.time()
        published_at = published_at or now
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, published_at=?, updated_at=?, "
                "last_error_code='', last_error='', next_retry_at=NULL, lease_until=NULL "
                "WHERE action_id=? AND status=?",
                (STATUS_PUBLISHED, published_at, now, action_id,
                 STATUS_PUBLISHING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def ensure_published(
        self,
        account_id: str,
        bvid: str,
        *,
        generation_text: str = "",
        persona_id: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """手动重试成功后：确保同账号同视频存在 published 动作。

        - 已有 published → 直接 True
        - 已有其它状态记录 → 强制改为 published（不限来源状态）
        - 无记录 → 插入一条 published 记录
        注意：ux_pca_active 部分唯一索引下，若库中已有 published 而最新行是 failed，
        直接 UPDATE failed→published 会 IntegrityError，需先 has_published。
        """
        now = now or time.time()
        if self.has_published(account_id, bvid):
            return True

        existing = self.get_by_bvid(account_id, bvid)
        if existing is not None:
            if existing.status == STATUS_PUBLISHED:
                return True
            conn = self._get_conn()
            try:
                try:
                    cur = conn.execute(
                        "UPDATE proactive_comment_actions "
                        "SET status=?, published_at=?, updated_at=?, "
                        "last_error_code='', last_error='', next_retry_at=NULL, lease_until=NULL "
                        "WHERE action_id=?",
                        (STATUS_PUBLISHED, now, now, existing.action_id),
                    )
                    conn.commit()
                    return cur.rowcount > 0 or self.has_published(account_id, bvid)
                except sqlite3.IntegrityError:
                    conn.rollback()
                    # 并发下已有另一条 published；当前行保持即可
                    return self.has_published(account_id, bvid)
            finally:
                conn.close()

        action_id = f"pca_{uuid.uuid4().hex[:16]}"
        idem_key = default_idempotency_key(account_id, bvid)
        gen_hash = compute_generation_hash(generation_text) if generation_text else ""
        conn = self._get_conn()
        try:
            try:
                conn.execute(
                    """
                    INSERT INTO proactive_comment_actions
                        (action_id, account_id, bvid, persona_id, task_id, status,
                         generation_text, generation_hash, idempotency_key,
                         attempt, max_attempts, last_error_code, last_error,
                         created_at, updated_at, published_at, next_retry_at)
                    VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, 0, ?, '', '', ?, ?, ?, NULL)
                    """,
                    (
                        action_id, account_id, bvid, persona_id,
                        STATUS_PUBLISHED, generation_text or "", gen_hash, idem_key,
                        DEFAULT_MAX_ATTEMPTS, now, now, now,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                again = self.get_by_bvid(account_id, bvid)
                if again is None:
                    return self.has_published(account_id, bvid)
                if again.status == STATUS_PUBLISHED:
                    return True
                if self.has_published(account_id, bvid):
                    return True
                conn2 = self._get_conn()
                try:
                    try:
                        cur = conn2.execute(
                            "UPDATE proactive_comment_actions "
                            "SET status=?, published_at=?, updated_at=?, "
                            "last_error_code='', last_error='', next_retry_at=NULL, lease_until=NULL "
                            "WHERE action_id=?",
                            (STATUS_PUBLISHED, now, now, again.action_id),
                        )
                        conn2.commit()
                        return cur.rowcount > 0 or self.has_published(account_id, bvid)
                    except sqlite3.IntegrityError:
                        conn2.rollback()
                        return self.has_published(account_id, bvid)
                finally:
                    conn2.close()
        finally:
            conn.close()

    def mark_retry_wait(
        self, action_id: str, error_code: str, error: str,
        now: Optional[float] = None,
        *,
        increment_attempt: bool = True,
        from_statuses: Optional[tuple] = None,
    ) -> bool:
        """publishing/retry_wait → retry_wait（可重试失败，带指数退避）

        达到 max_attempts → 直接 failed。
        increment_attempt=False：仅延期（如限流），不消耗 attempt 预算。
        from_statuses：可选限制来源状态（默认仅限 claimed/publishing/retry_wait，
        避免终态被迟到调用拉回队列）。
        """
        now = now or time.time()
        if from_statuses is None:
            from_statuses = (STATUS_CLAIMED, STATUS_PUBLISHING, STATUS_RETRY_WAIT)
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT attempt, max_attempts, status FROM proactive_comment_actions "
                "WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                return False
            if from_statuses is not None and row["status"] not in from_statuses:
                return False
            attempt = row["attempt"]
            max_att = row["max_attempts"]

            if increment_attempt:
                new_attempt = attempt + 1
            else:
                new_attempt = attempt

            if increment_attempt and new_attempt >= max_att:
                # 达到上限 → failed
                conn.execute(
                    "UPDATE proactive_comment_actions "
                    "SET status=?, attempt=?, last_error_code=?, last_error=?, "
                    "updated_at=?, next_retry_at=NULL, lease_until=NULL "
                    "WHERE action_id=?",
                    (STATUS_FAILED, new_attempt, error_code, error,
                     now, action_id),
                )
                conn.commit()
                return True
            # 还能重试 → retry_wait（指数退避；限流延期用较小基数）
            base = min(2 ** max(new_attempt, 1), DEFAULT_BACKOFF_CAP)
            if not increment_attempt:
                base = min(max(base, 30), 120)
            jitter = random.uniform(0, base * 0.1)
            next_retry_at = now + base + jitter
            conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, attempt=?, last_error_code=?, last_error=?, "
                "next_retry_at=?, updated_at=?, lease_until=NULL "
                "WHERE action_id=?",
                (STATUS_RETRY_WAIT, new_attempt, error_code, error,
                 next_retry_at, now, action_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def mark_result_unknown(
        self, action_id: str, error_code: str, error: str,
        now: Optional[float] = None,
    ) -> bool:
        """PRD-V5 §10.2：平台成功 + 本地失败 → result_unknown（不自动重发）

        publish 阶段发生不确定错误（如 HTTP 超时、200 但解析失败）时使用。
        终态，不进入 retry 队列。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, last_error_code=?, last_error=?, "
                "updated_at=?, next_retry_at=NULL, lease_until=NULL "
                "WHERE action_id=? AND status=?",
                (STATUS_RESULT_UNKNOWN, error_code, error,
                 now, action_id, STATUS_PUBLISHING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_failed(
        self, action_id: str, error_code: str, error: str,
        now: Optional[float] = None,
    ) -> bool:
        """→ failed（永久失败，如策略拒绝、安全检查拒绝）"""
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, last_error_code=?, last_error=?, "
                "updated_at=?, next_retry_at=NULL, lease_until=NULL "
                "WHERE action_id=?",
                (STATUS_FAILED, error_code, error, now, action_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def recover_stuck_publishing(self, now: Optional[float] = None) -> int:
        """Task 4：恢复卡在 publishing 状态的动作

        若进程在 publishing 阶段崩溃（mark_publishing 后、mark_published 前），
        该动作会永久卡在 publishing，导致同账号同视频再也收不到主动评论
        （部分唯一索引 ux_pca_active 阻止新 claim）。

        本方法将 lease_until 已过期的 publishing 动作转为 result_unknown
        （不自动重发，与"平台结果不确定"语义一致 —— 无法判断评论是否已发出）。

        应在调度器启动时 + 主循环中周期性调用。

        Returns:
            被恢复的行数
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, last_error_code='STUCK_PUBLISHING', "
                "last_error='publishing 超过 lease_until，疑似崩溃', "
                "updated_at=?, next_retry_at=NULL, lease_until=NULL "
                "WHERE status=? AND lease_until IS NOT NULL AND lease_until < ?",
                (STATUS_RESULT_UNKNOWN, now, STATUS_PUBLISHING, now),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def recover_stuck_claimed(self, now: Optional[float] = None) -> int:
        """C6：恢复卡在 claimed 状态的动作

        claim 之后、mark_publishing 之前若进程崩溃，行会永久停在 claimed，
        部分唯一索引阻止同 bvid 再次 claim。

        - 已有 generation_text：转 retry_wait，允许重试发布
        - 无 generation_text：转 failed，释放锁以便重新 claim
        兼容旧数据：lease_until IS NULL 且 created_at 超过默认租约也视为过期。
        """
        now = now or time.time()
        legacy_cutoff = now - DEFAULT_CLAIMED_LEASE_SECONDS
        recovered = 0
        conn = self._get_conn()
        try:
            # 有生成文本 → retry_wait（可重试发布，不自动重发到平台直到主循环拾取）
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, last_error_code='STUCK_CLAIMED', "
                "last_error='claimed 超过 lease_until，已有生成文本，转 retry_wait', "
                "next_retry_at=?, updated_at=?, lease_until=NULL "
                "WHERE status=? AND generation_text IS NOT NULL AND TRIM(generation_text) != '' "
                "AND ("
                "  (lease_until IS NOT NULL AND lease_until < ?) "
                "  OR (lease_until IS NULL AND created_at < ?)"
                ")",
                (STATUS_RETRY_WAIT, now, now, STATUS_CLAIMED, now, legacy_cutoff),
            )
            recovered += cur.rowcount
            # 无生成文本 → failed，释放唯一索引
            cur = conn.execute(
                "UPDATE proactive_comment_actions "
                "SET status=?, last_error_code='STUCK_CLAIMED', "
                "last_error='claimed 超过 lease_until，生成前崩溃，释放锁', "
                "updated_at=?, next_retry_at=NULL, lease_until=NULL "
                "WHERE status=? AND (generation_text IS NULL OR TRIM(generation_text) = '') "
                "AND ("
                "  (lease_until IS NOT NULL AND lease_until < ?) "
                "  OR (lease_until IS NULL AND created_at < ?)"
                ")",
                (STATUS_FAILED, now, STATUS_CLAIMED, now, legacy_cutoff),
            )
            recovered += cur.rowcount
            conn.commit()
            return recovered
        finally:
            conn.close()

    def close(self):
        """短连接模式无需关闭"""
        pass
