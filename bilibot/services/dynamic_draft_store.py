"""
动态草稿存储 - DynamicDraftStore（PRD-V5 §4.1 DYN-501）

动态审核强制生效：
- review_before_publish=true 时，生成的动态内容写入草稿（awaiting_review），
  不得调用任何 B站 publish / image upload API。
- 管理员审核通过后，独立 publish 任务执行一次实际发布。
- 拒绝/过期/撤销的草稿永不发布。
- 编辑草稿 → 重新安全检查 + 自增 revision。
- 审核通过使用 draft_id + expected_revision 乐观锁。

状态机：
    generating → awaiting_review (safety pass) | rejected (safety deny) | failed (gen fail)
    awaiting_review → approved (admin approve) | rejected (admin reject) | expired (timeout)
    awaiting_review → awaiting_review (edit, increment revision)
    approved → publishing (publish task claim)
    approved → awaiting_review (rollback: publish task 创建失败, DYN-601)
    publishing → published | retry_wait | result_unknown
    retry_wait → publishing
    result_unknown → reconciled → published | failed

幂等：approval 使用 (draft_id, expected_revision) 乐观锁，同一 revision 只能审核通过一次。
"""
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bilibot.dynamic_draft_store")

# ═══════════════════════════════════════════════════════
#  状态常量
# ═══════════════════════════════════════════════════════

STATUS_GENERATING = "generating"
STATUS_AWAITING_REVIEW = "awaiting_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_RESULT_UNKNOWN = "result_unknown"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

# 可发布状态（publish 任务可 claim）
PUBLISHABLE_STATUSES = frozenset({STATUS_APPROVED, STATUS_RETRY_WAIT})

# 可重试状态（retry 接口可重新入队）
RETRYABLE_STATUSES = frozenset({
    STATUS_RETRY_WAIT, STATUS_FAILED, STATUS_RESULT_UNKNOWN,
})

# 终态
TERMINAL_STATUSES = frozenset({
    STATUS_PUBLISHED, STATUS_REJECTED, STATUS_EXPIRED, STATUS_FAILED,
})

# 可编辑状态（PATCH 接口允许）
EDITABLE_STATUSES = frozenset({STATUS_AWAITING_REVIEW})


@dataclass
class DynamicDraft:
    """动态草稿数据模型（对应 dynamic_drafts 表一行）"""
    draft_id: str
    account_id: str
    persona_id: str
    task_id: str
    content: str
    image_refs_json: str = "[]"
    status: str = STATUS_GENERATING
    revision: int = 1
    safety_snapshot_json: str = "{}"
    created_by: str = ""
    reviewed_by_session_hash: Optional[str] = None
    review_note: Optional[str] = None
    expires_at: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_publish_error: str = ""

    @property
    def image_refs(self) -> List[str]:
        try:
            return json.loads(self.image_refs_json or "[]")
        except Exception:
            return []

    @property
    def safety_snapshot(self) -> Dict[str, Any]:
        try:
            return json.loads(self.safety_snapshot_json or "{}")
        except Exception:
            return {}

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # 解析 JSON 字段为对象，方便 API 返回
        try:
            d["image_refs"] = json.loads(d.pop("image_refs_json") or "[]")
        except Exception:
            d["image_refs"] = []
            d.pop("image_refs_json", None)
        try:
            d["safety_snapshot"] = json.loads(d.pop("safety_snapshot_json") or "{}")
        except Exception:
            d["safety_snapshot"] = {}
            d.pop("safety_snapshot_json", None)
        return d

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DynamicDraft":
        return cls(
            draft_id=row["draft_id"],
            account_id=row["account_id"],
            persona_id=row["persona_id"],
            task_id=row["task_id"],
            content=row["content"],
            image_refs_json=row["image_refs_json"] or "[]",
            status=row["status"],
            revision=row["revision"],
            safety_snapshot_json=row["safety_snapshot_json"] or "{}",
            created_by=row["created_by"] or "",
            reviewed_by_session_hash=row["reviewed_by_session_hash"],
            review_note=row["review_note"],
            expires_at=row["expires_at"],
            created_at=row["created_at"] or 0.0,
            updated_at=row["updated_at"] or 0.0,
            last_publish_error=row["last_publish_error"] or "",
        )


class DynamicDraftStore:
    """动态草稿持久化存储（SQLite，短连接）

    参考 task_store.py / reply_state.py 的短连接模式。
    """

    def __init__(self, db_path: str, account_id: str = ""):
        self.db_path = db_path
        self.account_id = account_id
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dynamic_drafts (
                    draft_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    image_refs_json TEXT DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'generating',
                    revision INTEGER NOT NULL DEFAULT 1,
                    safety_snapshot_json TEXT DEFAULT '{}',
                    created_by TEXT DEFAULT '',
                    reviewed_by_session_hash TEXT,
                    review_note TEXT,
                    expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_publish_error TEXT DEFAULT '',
                    UNIQUE(account_id, task_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_account ON dynamic_drafts(account_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_status ON dynamic_drafts(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_account_status ON dynamic_drafts(account_id, status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_draft_expires ON dynamic_drafts(expires_at) "
                "WHERE expires_at IS NOT NULL"
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
        persona_id: str,
        task_id: str,
        content: str,
        image_refs: Optional[List[str]] = None,
        safety_snapshot: Optional[Dict[str, Any]] = None,
        created_by: str = "scheduler",
        status: str = STATUS_AWAITING_REVIEW,
        expires_at: Optional[float] = None,
    ) -> str:
        """创建草稿，返回 draft_id

        Args:
            status: 初始状态，默认 awaiting_review（安全检查通过后）。
                    安全检查失败时传 rejected，生成失败时传 failed。
            image_refs: 图片引用列表（base64 字符串），不传 B站 上传结果。
            safety_snapshot: 安全检查快照（审计依据）。
            expires_at: 过期时间戳（秒），None 表示不过期。
        """
        now = time.time()
        draft_id = f"draft_{uuid.uuid4().hex[:16]}"
        image_refs_json = json.dumps(image_refs or [], ensure_ascii=False)
        safety_json = json.dumps(safety_snapshot or {}, ensure_ascii=False)

        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO dynamic_drafts
                    (draft_id, account_id, persona_id, task_id, content,
                     image_refs_json, status, revision, safety_snapshot_json,
                     created_by, reviewed_by_session_hash, review_note, expires_at,
                     created_at, updated_at, last_publish_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL, ?, ?, ?, '')
            """, (
                draft_id, account_id, persona_id, task_id, content,
                image_refs_json, status, safety_json,
                created_by, expires_at,
                now, now,
            ))
            conn.commit()
        finally:
            conn.close()
        logger.info(f"动态草稿已创建: {draft_id} status={status} account={account_id}")
        return draft_id

    # ═══════════════════════════════════════════════════════
    #  Read
    # ═══════════════════════════════════════════════════════

    def get(self, draft_id: str) -> Optional[DynamicDraft]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM dynamic_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            return DynamicDraft.from_row(row) if row else None
        finally:
            conn.close()

    def get_by_task_id(self, task_id: str) -> Optional[DynamicDraft]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM dynamic_drafts WHERE task_id=?", (task_id,)
            ).fetchone()
            return DynamicDraft.from_row(row) if row else None
        finally:
            conn.close()

    def list_by_account(
        self,
        account_id: str,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> List[DynamicDraft]:
        """分页列出账号下的草稿（按 updated_at 倒序）"""
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        offset = (page - 1) * page_size
        conn = self._get_conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM dynamic_drafts WHERE account_id=? AND status=? "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (account_id, status, page_size, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM dynamic_drafts WHERE account_id=? "
                    "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (account_id, page_size, offset),
                ).fetchall()
            return [DynamicDraft.from_row(r) for r in rows]
        finally:
            conn.close()

    def count_by_account(
        self, account_id: str, status: Optional[str] = None,
    ) -> int:
        conn = self._get_conn()
        try:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) FROM dynamic_drafts WHERE account_id=? AND status=?",
                    (account_id, status),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM dynamic_drafts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  State transitions（乐观锁 / 条件更新）
    # ═══════════════════════════════════════════════════════

    def update(
        self,
        draft_id: str,
        content: Optional[str] = None,
        image_refs: Optional[List[str]] = None,
        expected_revision: Optional[int] = None,
        safety_snapshot: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """编辑草稿内容 + 自增 revision（乐观锁）

        PRD-V5 §4.1：编辑草稿 → 重新安全检查 + 自增 revision。
        必须传入 expected_revision 做乐观锁校验，不匹配则返回 False。
        只允许 awaiting_review 状态编辑。
        """
        if expected_revision is None:
            return False
        now = time.time()
        sets = []
        params: list = []
        if content is not None:
            sets.append("content=?")
            params.append(content)
        if image_refs is not None:
            sets.append("image_refs_json=?")
            params.append(json.dumps(image_refs, ensure_ascii=False))
        if safety_snapshot is not None:
            sets.append("safety_snapshot_json=?")
            params.append(json.dumps(safety_snapshot, ensure_ascii=False))
        if not sets:
            # 没有字段需要更新，但仍校验 revision
            sets.append("updated_at=?")
            params.append(now)
        sets.append("revision=revision+1")
        sets.append("updated_at=?")
        params.append(now)
        params.append(draft_id)
        params.append(expected_revision)
        params.append(STATUS_AWAITING_REVIEW)
        sql = (
            "UPDATE dynamic_drafts SET " + ", ".join(sets) +
            " WHERE draft_id=? AND revision=? AND status=?"
        )
        conn = self._get_conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def approve(
        self,
        draft_id: str,
        expected_revision: int,
        reviewer_session_hash: str = "",
    ) -> bool:
        """awaiting_review → approved（乐观锁：draft_id + expected_revision）

        同一 revision 只能审核通过一次（revision 不变，但状态变为 approved 后
        不可再次 approve）。返回 True 表示审核成功，False 表示 revision 不匹配
        或状态非 awaiting_review。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, reviewed_by_session_hash=?, "
                "updated_at=? WHERE draft_id=? AND revision=? AND status=?",
                (STATUS_APPROVED, reviewer_session_hash or "", now,
                 draft_id, expected_revision, STATUS_AWAITING_REVIEW),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reject(
        self,
        draft_id: str,
        reviewer_session_hash: str = "",
        note: Optional[str] = None,
        expected_revision: Optional[int] = None,
    ) -> bool:
        """awaiting_review → rejected

        可选 expected_revision 做乐观锁（传 None 时不校验 revision）。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            if expected_revision is not None:
                cur = conn.execute(
                    "UPDATE dynamic_drafts SET status=?, reviewed_by_session_hash=?, "
                    "review_note=?, updated_at=? "
                    "WHERE draft_id=? AND revision=? AND status=?",
                    (STATUS_REJECTED, reviewer_session_hash or "", note or "",
                     now, draft_id, expected_revision, STATUS_AWAITING_REVIEW),
                )
            else:
                cur = conn.execute(
                    "UPDATE dynamic_drafts SET status=?, reviewed_by_session_hash=?, "
                    "review_note=?, updated_at=? "
                    "WHERE draft_id=? AND status=?",
                    (STATUS_REJECTED, reviewer_session_hash or "", note or "",
                     now, draft_id, STATUS_AWAITING_REVIEW),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_publishing(self, draft_id: str) -> bool:
        """approved → publishing（publish 任务 claim）

        只允许 approved / retry_wait 状态转入 publishing。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, updated_at=? "
                "WHERE draft_id=? AND status IN (?, ?)",
                (STATUS_PUBLISHING, now, draft_id,
                 STATUS_APPROVED, STATUS_RETRY_WAIT),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_published(self, draft_id: str) -> bool:
        """publishing → published"""
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, updated_at=?, last_publish_error='' "
                "WHERE draft_id=? AND status=?",
                (STATUS_PUBLISHED, now, draft_id, STATUS_PUBLISHING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_retry_wait(self, draft_id: str, error: str = "") -> bool:
        """publishing → retry_wait（可重试）"""
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, last_publish_error=?, updated_at=? "
                "WHERE draft_id=? AND status=?",
                (STATUS_RETRY_WAIT, error, now, draft_id, STATUS_PUBLISHING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_result_unknown(self, draft_id: str, error: str = "") -> bool:
        """publishing → result_unknown（平台结果不确定，不自动重发）"""
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, last_publish_error=?, updated_at=? "
                "WHERE draft_id=? AND status=?",
                (STATUS_RESULT_UNKNOWN, error, now, draft_id, STATUS_PUBLISHING),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def mark_failed(self, draft_id: str, error: str = "") -> bool:
        """→ failed（永久失败）"""
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, last_publish_error=?, updated_at=? "
                "WHERE draft_id=? AND status IN (?, ?, ?)",
                (STATUS_FAILED, error, now, draft_id,
                 STATUS_PUBLISHING, STATUS_RETRY_WAIT, STATUS_RESULT_UNKNOWN),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reset_to_approved(self, draft_id: str) -> bool:
        """retry_wait/failed/result_unknown → approved（retry 接口重新入队）

        供 API retry 端点使用：重置为可发布状态，revision 不变。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, last_publish_error='', updated_at=? "
                "WHERE draft_id=? AND status IN (?, ?, ?)",
                (STATUS_APPROVED, now, draft_id,
                 STATUS_RETRY_WAIT, STATUS_FAILED, STATUS_RESULT_UNKNOWN),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reset_to_awaiting_review(self, draft_id: str) -> bool:
        """approved → awaiting_review（publish 任务创建失败时回滚，DYN-601）

        供 API approve 端点使用：当 create_draft_publish_task 失败时，将草稿从
        approved 回滚到 awaiting_review，使管理员可重新审核通过。revision 不变
        （仍满足原 expected_revision 乐观锁条件）。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, updated_at=? "
                "WHERE draft_id=? AND status=?",
                (STATUS_AWAITING_REVIEW, now, draft_id, STATUS_APPROVED),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    #  Maintenance
    # ═══════════════════════════════════════════════════════

    def expire_overdue(self, now: Optional[float] = None) -> int:
        """标记过期的草稿为 expired（仅 awaiting_review 状态）

        expires_at < now → expired
        返回受影响行数。
        """
        now = now or time.time()
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "UPDATE dynamic_drafts SET status=?, updated_at=? "
                "WHERE status=? AND expires_at IS NOT NULL AND expires_at < ?",
                (STATUS_EXPIRED, now, STATUS_AWAITING_REVIEW, now),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def reconcile(
        self,
        draft_id: str,
        success: bool,
        error: Optional[str] = None,
    ) -> bool:
        """result_unknown → published（成功）或 failed（失败）

        人工对账后调用。
        """
        now = time.time()
        conn = self._get_conn()
        try:
            if success:
                cur = conn.execute(
                    "UPDATE dynamic_drafts SET status=?, last_publish_error='', updated_at=? "
                    "WHERE draft_id=? AND status IN (?, ?)",
                    (STATUS_PUBLISHED, now, draft_id,
                     STATUS_RESULT_UNKNOWN, STATUS_PUBLISHING),
                )
            else:
                cur = conn.execute(
                    "UPDATE dynamic_drafts SET status=?, last_publish_error=?, updated_at=? "
                    "WHERE draft_id=? AND status IN (?, ?)",
                    (STATUS_FAILED, error or "", now, draft_id,
                     STATUS_RESULT_UNKNOWN, STATUS_PUBLISHING),
                )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def close(self):
        """短连接模式无需关闭"""
        pass
