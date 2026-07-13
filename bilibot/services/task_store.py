"""
TaskRun 持久化生命周期（PRD-V5 §7 / TASK-501）

为主动视频、动态、周总结、主动评论等任务提供持久化状态机。

状态流转：
    scheduled → claimed → running → succeeded
    failure → retry_wait (with backoff) | failed (max attempts reached)
    overdue beyond grace window → expired (do NOT fake triggered)
    restart: claimed/running → interrupted, then recover by scene
    platform result uncertain → result_unknown (do NOT auto-republish)

终态：succeeded, failed, expired, interrupted (recoverable), result_unknown
非终态可恢复：retry_wait, scheduled, claimed, running, interrupted

幂等键：account_id + scene + idempotency_key (UNIQUE)

PRD-V5 §7.3 并发：
- SQLite claim 使用事务性条件更新（UPDATE ... WHERE status='scheduled'）
- 非先读后写
"""
import json
import logging
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("bilibot.task_store")

# ═══════════════════════════════════════════════════════
#  状态常量
# ═══════════════════════════════════════════════════════

# 非终态
STATUS_SCHEDULED = "scheduled"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_RETRY_WAIT = "retry_wait"

# 终态
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_INTERRUPTED = "interrupted"  # 可恢复终态（重启时由 claimed/running 转入）
STATUS_RESULT_UNKNOWN = "result_unknown"

# 触发类型
TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"
TRIGGER_RETRY = "retry"
TRIGGER_RECOVERY = "recovery"

# 场景
SCENE_PROACTIVE_VIDEO = "proactive_video"
SCENE_DYNAMIC = "dynamic"
SCENE_WEEKLY_SUMMARY = "weekly_summary"
SCENE_PROACTIVE_COMMENT = "proactive_comment"

# 默认配置
DEFAULT_GRACE_WINDOW = 900  # 15 分钟
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 600  # 10 分钟

# 终态集合（不再自动处理；interrupted 可通过 recovery 重入）
TERMINAL_STATUSES = frozenset({
    STATUS_SUCCEEDED, STATUS_FAILED, STATUS_EXPIRED,
    STATUS_INTERRUPTED, STATUS_RESULT_UNKNOWN,
})

# 可重试状态（非终态可恢复，可被 retry 接口重新入队）
RETRYABLE_STATUSES = frozenset({
    STATUS_RETRY_WAIT, STATUS_FAILED, STATUS_INTERRUPTED,
})

# 启动时需恢复为 interrupted 的状态（重启前在运行中）
RECOVERABLE_ON_RESTART = frozenset({STATUS_CLAIMED, STATUS_RUNNING})


@dataclass
class TaskRun:
    """TaskRun 数据模型（对应 task_runs 表一行）"""
    task_id: str
    account_id: str
    scene: str
    idempotency_key: str
    trigger_type: str = TRIGGER_SCHEDULE
    status: str = STATUS_SCHEDULED
    scheduled_at: float = 0.0
    not_before: float = 0.0
    lease_until: Optional[float] = None
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    next_retry_at: Optional[float] = None
    input_json: str = "{}"
    result_json: str = "{}"
    last_error_code: str = ""
    last_error: str = ""
    created_at: float = 0.0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    updated_at: float = 0.0
    grace_window: int = DEFAULT_GRACE_WINDOW

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def is_retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    def is_succeeded(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRun":
        return cls(
            task_id=row["task_id"],
            account_id=row["account_id"],
            scene=row["scene"],
            idempotency_key=row["idempotency_key"],
            trigger_type=row["trigger_type"] or TRIGGER_SCHEDULE,
            status=row["status"],
            scheduled_at=row["scheduled_at"] or 0.0,
            not_before=row["not_before"] or 0.0,
            lease_until=row["lease_until"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            next_retry_at=row["next_retry_at"],
            input_json=row["input_json"] or "{}",
            result_json=row["result_json"] or "{}",
            last_error_code=row["last_error_code"] or "",
            last_error=row["last_error"] or "",
            created_at=row["created_at"] or 0.0,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=row["updated_at"] or 0.0,
            grace_window=row["grace_window"] if row["grace_window"] is not None else DEFAULT_GRACE_WINDOW,
        )


class TaskRunStore:
    """TaskRun 持久化存储（SQLite，短连接）

    PRD-V5 §7.1 / §7.2 / §7.3：
    - 幂等键 idempotency_key UNIQUE
    - claim 使用事务性条件更新
    - 短连接模式（参考 reply_state.py）
    """

    def __init__(self, db_path: str, account_id: str = ""):
        self.db_path = db_path
        self.account_id = account_id
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """短连接模式（PRD V4 MEM-002）"""
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_runs (
                    task_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    trigger_type TEXT,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    scheduled_at REAL,
                    not_before REAL,
                    lease_until REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_retry_at REAL,
                    input_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    last_error_code TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL,
                    grace_window INTEGER DEFAULT 900,
                    UNIQUE(idempotency_key)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_status ON task_runs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_account_scene ON task_runs(account_id, scene)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_retry ON task_runs(next_retry_at) "
                "WHERE next_retry_at IS NOT NULL"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_scheduled ON task_runs(scheduled_at)"
            )
            conn.commit()
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  Create
    # ═══════════════════════════════════════════════════════

    def create(
        self,
        account_id: str,
        scene: str,
        idempotency_key: str,
        trigger_type: str = TRIGGER_SCHEDULE,
        scheduled_at: Optional[float] = None,
        input_data: Optional[Dict[str, Any]] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        grace_window: int = DEFAULT_GRACE_WINDOW,
        not_before: Optional[float] = None,
    ) -> TaskRun:
        """创建 TaskRun 记录

        幂等键冲突时抛出 ValueError（调用方决定是否返回已存在记录）。
        """
        now = time.time()
        if scheduled_at is None:
            scheduled_at = now
        if not_before is None:
            not_before = scheduled_at
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        input_json = json.dumps(input_data or {}, ensure_ascii=False)

        conn = self._get_conn()
        try:
            try:
                conn.execute("""
                    INSERT INTO task_runs
                        (task_id, account_id, scene, idempotency_key, trigger_type, status,
                         scheduled_at, not_before, lease_until, attempt, max_attempts,
                         next_retry_at, input_json, result_json, last_error_code, last_error,
                         created_at, started_at, finished_at, updated_at, grace_window)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, NULL, ?, '{}', '', '', ?, NULL, NULL, ?, ?)
                """, (
                    task_id, account_id, scene, idempotency_key, trigger_type, STATUS_SCHEDULED,
                    scheduled_at, not_before, max_attempts,
                    input_json,
                    now, now, grace_window,
                ))
                conn.commit()
            except sqlite3.IntegrityError as e:
                # 幂等键冲突
                conn.rollback()
                raise ValueError(
                    f"idempotency_key already exists: {idempotency_key}"
                ) from e
        finally:
            conn.close()

        return self.get(task_id)

    def create_if_absent(
        self,
        account_id: str,
        scene: str,
        idempotency_key: str,
        **kwargs,
    ) -> Optional[TaskRun]:
        """幂等创建：若已存在同 idempotency_key 的记录，返回 None（不创建）"""
        try:
            return self.create(account_id, scene, idempotency_key, **kwargs)
        except ValueError:
            return None

    # ═══════════════════════════════════════════════════════
    #  Read
    # ═══════════════════════════════════════════════════════

    def get(self, task_id: str) -> Optional[TaskRun]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_runs WHERE task_id=?", (task_id,)
            ).fetchone()
            return TaskRun.from_row(row) if row else None
        finally:
            conn.close()

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[TaskRun]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            return TaskRun.from_row(row) if row else None
        finally:
            conn.close()

    def list_by_account_scene(
        self,
        account_id: str,
        scene: str,
        date_str: Optional[str] = None,
        limit: int = 100,
    ) -> List[TaskRun]:
        """列出某账号某场景的 TaskRun（可选按日期过滤 scheduled_at）"""
        conn = self._get_conn()
        try:
            if date_str:
                # date_str: YYYY-MM-DD；用 scheduled_at 范围过滤
                from datetime import datetime, timedelta
                d = datetime.strptime(date_str, "%Y-%m-%d")
                start = d.timestamp()
                end = (d + timedelta(days=1)).timestamp()
                rows = conn.execute(
                    "SELECT * FROM task_runs WHERE account_id=? AND scene=? "
                    "AND scheduled_at >= ? AND scheduled_at < ? "
                    "ORDER BY scheduled_at ASC LIMIT ?",
                    (account_id, scene, start, end, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_runs WHERE account_id=? AND scene=? "
                    "ORDER BY scheduled_at ASC LIMIT ?",
                    (account_id, scene, limit),
                ).fetchall()
            return [TaskRun.from_row(r) for r in rows]
        finally:
            conn.close()

    def list_by_account(
        self,
        account_id: str,
        limit: int = 100,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> List[TaskRun]:
        """列出某账号的所有 TaskRun（跨场景，按 scheduled_at 倒序）

        支持分页（limit/offset）与可选状态过滤，用于
        GET /api/accounts/{id}/tasks 列表端点。
        """
        conn = self._get_conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM task_runs WHERE account_id=? AND status=? "
                    "ORDER BY scheduled_at DESC LIMIT ? OFFSET ?",
                    (account_id, status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_runs WHERE account_id=? "
                    "ORDER BY scheduled_at DESC LIMIT ? OFFSET ?",
                    (account_id, limit, offset),
                ).fetchall()
            return [TaskRun.from_row(r) for r in rows]
        finally:
            conn.close()

    def count_by_account(
        self,
        account_id: str,
        status: Optional[str] = None,
    ) -> int:
        """统计某账号的 TaskRun 总数（可选按状态过滤）"""
        conn = self._get_conn()
        try:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) FROM task_runs WHERE account_id=? AND status=?",
                    (account_id, status),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM task_runs WHERE account_id=?",
                    (account_id,),
                ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def count_succeeded_today(
        self, account_id: str, scene: str, now: Optional[float] = None,
    ) -> int:
        """统计今日已成功的任务数（PRD-V5：只有 succeeded 算当日完成）"""
        from datetime import datetime, timedelta
        now = now or time.time()
        d = datetime.fromtimestamp(now)
        start = datetime(d.year, d.month, d.day).timestamp()
        end = (datetime(d.year, d.month, d.day) + timedelta(days=1)).timestamp()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM task_runs "
                "WHERE account_id=? AND scene=? AND status=? "
                "AND scheduled_at >= ? AND scheduled_at < ?",
                (account_id, scene, STATUS_SUCCEEDED, start, end),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def list_scheduled_claimable(
        self, account_id: str, scene: str, now: Optional[float] = None,
    ) -> List[TaskRun]:
        """列出可被 claim 的任务（scheduled 且在 late window 内）"""
        now = now or time.time()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM task_runs "
                "WHERE account_id=? AND scene=? AND status=? "
                "AND not_before <= ? "
                "ORDER BY scheduled_at ASC",
                (account_id, scene, STATUS_SCHEDULED, now),
            ).fetchall()
            return [TaskRun.from_row(r) for r in rows]
        finally:
            conn.close()

    def list_retryable(self, now: Optional[float] = None) -> List[TaskRun]:
        """获取可重试的任务（retry_wait 且 next_retry_at <= now）"""
        now = now or time.time()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM task_runs "
                "WHERE status=? AND next_retry_at IS NOT NULL AND next_retry_at <= ? "
                "ORDER BY next_retry_at ASC LIMIT 50",
                (STATUS_RETRY_WAIT, now),
            ).fetchall()
            return [TaskRun.from_row(r) for r in rows]
        finally:
            conn.close()

    def list_interrupted(self) -> List[TaskRun]:
        """Task 5：列出所有 interrupted 状态的任务（启动恢复用）"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE status=? ORDER BY updated_at ASC",
                (STATUS_INTERRUPTED,),
            ).fetchall()
            return [TaskRun.from_row(r) for r in rows]
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  State transitions（事务性条件更新）
    # ═══════════════════════════════════════════════════════

    def claim(self, task_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
              now: Optional[float] = None) -> bool:
        """PRD-V5 §7.3：事务性条件 claim

        UPDATE ... WHERE status='scheduled' AND not_before <= ?
        只有一行会被更新（SQLite 行锁）。返回是否成功。
        """
        now = now or time.time()
        lease_until = now + lease_seconds
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, lease_until=?, updated_at=? "
                "WHERE task_id=? AND status=? AND not_before <= ?",
                (STATUS_CLAIMED, lease_until, now, task_id, STATUS_SCHEDULED, now),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def start(self, task_id: str, now: Optional[float] = None) -> bool:
        """claimed → running"""
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, started_at=?, updated_at=? "
                "WHERE task_id=? AND status=?",
                (STATUS_RUNNING, now, now, task_id, STATUS_CLAIMED),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def succeed(
        self, task_id: str, result: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """running → succeeded（PRD-V5：只有真正成功才写 succeeded）"""
        now = now or time.time()
        result_json = json.dumps(result or {}, ensure_ascii=False)
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, result_json=?, finished_at=?, "
                "updated_at=?, lease_until=NULL, next_retry_at=NULL "
                "WHERE task_id=? AND status=?",
                (STATUS_SUCCEEDED, result_json, now, now, task_id, STATUS_RUNNING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def fail(
        self, task_id: str, error_code: str, error: str,
        retryable: bool = True, now: Optional[float] = None,
    ) -> bool:
        """running → retry_wait (with backoff) 或 failed (max attempts reached)

        PRD-V5 §7.1：失败时根据是否还能重试决定状态。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT attempt, max_attempts FROM task_runs WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            attempt = row["attempt"]
            max_att = row["max_attempts"]

            if not retryable or attempt + 1 >= max_att:
                # 达到上限或不可重试 → failed
                conn.execute(
                    "UPDATE task_runs SET status=?, attempt=?, last_error_code=?, "
                    "last_error=?, finished_at=?, updated_at=?, lease_until=NULL, "
                    "next_retry_at=NULL WHERE task_id=?",
                    (STATUS_FAILED, attempt + 1, error_code, error, now, now, task_id),
                )
                conn.commit()
                return True
            # 还能重试 → retry_wait（指数退避）
            new_attempt = attempt + 1
            base = min(2 ** new_attempt, 300)
            jitter = random.uniform(0, base * 0.1)
            next_retry_at = now + base + jitter
            conn.execute(
                "UPDATE task_runs SET status=?, attempt=?, last_error_code=?, "
                "last_error=?, next_retry_at=?, updated_at=?, lease_until=NULL "
                "WHERE task_id=?",
                (STATUS_RETRY_WAIT, new_attempt, error_code, error,
                 next_retry_at, now, task_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def mark_result_unknown(
        self, task_id: str, error: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """PRD-V5 §7.2：平台成功 + 本地失败 → result_unknown（不自动重发）"""
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, last_error_code=?, last_error=?, "
                "finished_at=?, updated_at=?, lease_until=NULL, next_retry_at=NULL "
                "WHERE task_id=?",
                (STATUS_RESULT_UNKNOWN, "PLATFORM_RESULT_UNCERTAIN", error,
                 now, now, task_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def cancel(self, task_id: str, now: Optional[float] = None) -> bool:
        """PRD-V5 §7.4：取消未外部发布的任务

        只允许 scheduled / claimed 状态取消；running 可能已发布，不可取消。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, finished_at=?, updated_at=? "
                "WHERE task_id=? AND status IN (?, ?)",
                (STATUS_FAILED, now, now, task_id,
                 STATUS_SCHEDULED, STATUS_CLAIMED),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def retry(self, task_id: str, now: Optional[float] = None) -> bool:
        """PRD-V5 §7.4：手动重试，将 retry_wait/failed/interrupted 重新入队

        重置为 scheduled，attempt 不变（避免无限重试）。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, not_before=?, trigger_type=?, "
                "next_retry_at=NULL, lease_until=NULL, finished_at=NULL, "
                "started_at=NULL, updated_at=? "
                "WHERE task_id=? AND status IN (?, ?, ?)",
                (STATUS_SCHEDULED, now, TRIGGER_RETRY, now, task_id,
                 STATUS_RETRY_WAIT, STATUS_FAILED, STATUS_INTERRUPTED),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  Maintenance
    # ═══════════════════════════════════════════════════════

    def expire_overdue(self, now: Optional[float] = None) -> int:
        """PRD-V5 §7.1：标记过期的任务为 expired（不伪造 triggered）

        scheduled_at + grace_window < now → expired
        返回受影响行数。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            # 只把 scheduled 且超过 grace_window 的标记 expired
            # grace_window 存在列里，用 scheduled_at + grace_window < now
            cur = conn.execute(
                "UPDATE task_runs SET status=?, finished_at=?, updated_at=? "
                "WHERE status=? AND (scheduled_at + grace_window) < ?",
                (STATUS_EXPIRED, now, now, STATUS_SCHEDULED, now),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def recover_interrupted(self, now: Optional[float] = None) -> int:
        """PRD-V5 §7.2 / §6.3：重启时把 claimed/running → interrupted

        返回受影响行数。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, finished_at=?, updated_at=?, "
                "lease_until=NULL "
                "WHERE status IN (?, ?)",
                (STATUS_INTERRUPTED, now, now,
                 STATUS_CLAIMED, STATUS_RUNNING),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def clear_account_scene_today(
        self, account_id: str, scene: str, now: Optional[float] = None,
    ) -> int:
        """PRD-V5：跨天时清理当日内存计划前，先把旧的 scheduled 标记 expired

        注意：只清理 scheduled 状态，已完成/失败的历史保留。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE task_runs SET status=?, finished_at=?, updated_at=? "
                "WHERE account_id=? AND scene=? AND status=?",
                (STATUS_EXPIRED, now, now, account_id, scene, STATUS_SCHEDULED),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def close(self):
        """短连接模式无需关闭"""
        pass


def desensitize_task_run(task: TaskRun) -> Dict[str, Any]:
    """脱敏 TaskRun（用于 API 响应）

    PRD-V5 §7.4：input_json / result_json 脱敏后返回。
    """
    d = task.to_dict()
    # input_json 可能包含凭据，只保留关键字段
    try:
        input_obj = json.loads(task.input_json or "{}")
    except Exception:
        input_obj = {}
    try:
        result_obj = json.loads(task.result_json or "{}")
    except Exception:
        result_obj = {}

    # 脱敏：只暴露非敏感字段
    safe_input = {}
    for k in ("scene_hint", "topic", "bvid", "title", "trigger_source"):
        if k in input_obj:
            safe_input[k] = input_obj[k]

    safe_result = {}
    for k in ("success", "summary", "published_at", "platform", "kind"):
        if k in result_obj:
            safe_result[k] = result_obj[k]

    d["input_json"] = safe_input
    d["result_json"] = safe_result
    # 不暴露 lease_until（内部调度字段）
    d.pop("lease_until", None)
    return d
