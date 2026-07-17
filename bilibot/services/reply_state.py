"""
回复状态机（PRD V4 §9.1 / REP-001，PRD-V5 §6.2 / REP-502 生成文本持久化）

状态流转：
    discovered → ignored | context_building
    context_building → generation_pending | deferred
    generation_pending → safety_pending | deferred
    safety_pending → publish_pending | rejected | deferred
    publish_pending → published | retry_wait | result_unknown
    retry_wait → publish_pending | failed

终态（不再处理）：published, published_legacy, ignored, rejected, failed, result_unknown
非终态（可恢复）：deferred, retry_wait, 以及所有中间态

幂等键：account_id + comment_type + source_rpid + generation_revision
        （PRD-V5 §6.2 / REP-502：generation_revision 加入幂等键，确保文本失效
         重新生成后用新的键，而同文本重试沿用相同键以防止重复发布）

生成文本持久化（REP-502）：
    - generation_result / generation_hash / generation_persona_id /
      generation_revision / generation_audit_id 在 publish_pending / retry_wait /
      published 之间保持不变，确保重试使用原始文本而非重新生成。
    - 仅 invalidate_generation() 或新一次 save_generation_result() 会推进
      generation_revision。
"""
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger("bilibot.reply_state")

# 终态集合
# PRD V4 MIG-003：published_legacy 是旧 replied.json 迁移的终态，不再重试
# result_unknown：平台结果不确定，不自动重发（防超时双发）
TERMINAL_STATES = frozenset({
    "published", "published_legacy", "ignored", "rejected", "failed", "result_unknown",
})

# 可重试状态（非终态，可恢复）
RETRYABLE_STATES = frozenset({"deferred", "retry_wait"})

# 中间态（进行中，非终态也非可重试态）
# REP-601：用于防止重复处理进行中的评论
INTERMEDIATE_STATES = frozenset({
    "context_building",
    "generation_pending",
    "safety_pending",
    "publish_pending",
})

# REP-602：中间态卡住恢复阈值（秒）
DEFAULT_STUCK_TIMEOUT_MINUTES = 10

# 默认重试上限
DEFAULT_MAX_ATTEMPTS = 3


class ReplyStateStore:
    """结构化回复状态存储（SQLite，每账号一个实例）

    PRD V4 REP-001：
    - 幂等键为 account_id + comment_type + source_rpid
    - 原始通知、生成结果、发布尝试和最终状态分别保存
    - 只有 published/ignored/rejected 属于终态
    - LLM 空结果、LLM 超时、搜索失败、429、限流和安全服务异常均不是 ignored
    """

    def __init__(self, db_path: str, account_id: str = "", max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        self.db_path = db_path
        self.account_id = account_id
        self.max_attempts = max_attempts
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS reply_states (
                    account_id TEXT NOT NULL,
                    comment_type INTEGER NOT NULL DEFAULT 1,
                    source_rpid TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'discovered',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    last_error_code TEXT,
                    notification_json TEXT,
                    generation_result TEXT,
                    generation_hash TEXT,
                    generation_revision INTEGER NOT NULL DEFAULT 0,
                    generation_audit_id TEXT,
                    generation_persona_id TEXT,
                    persona_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_retry_at REAL,
                    metadata TEXT DEFAULT '{}',
                    PRIMARY KEY (account_id, comment_type, source_rpid)
                )
            """)
            # PRD-V5 §6.2 / REP-502：旧库迁移新增列（CREATE TABLE IF NOT EXISTS
            # 不会为已存在的表添加新列）
            self._ensure_column(conn, "generation_hash", "TEXT")
            self._ensure_column(conn, "generation_revision", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "generation_audit_id", "TEXT")
            self._ensure_column(conn, "generation_persona_id", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reply_state ON reply_states(state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reply_retry ON reply_states(next_retry_at) "
                "WHERE next_retry_at IS NOT NULL"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, name: str, decl: str):
        """为已存在的表补列（向后兼容旧库），忽略已存在的情况"""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(reply_states)")}
        if name not in cols:
            conn.execute(f"ALTER TABLE reply_states ADD COLUMN {name} {decl}")

    @staticmethod
    def make_key(account_id: str, comment_type: int, source_rpid: str) -> Tuple:
        """构造基础幂等键（不含 revision）"""
        return (str(account_id), int(comment_type), str(source_rpid))

    @staticmethod
    def make_idempotency_key(
        account_id: str,
        comment_type: int,
        source_rpid: str,
        generation_revision: int = 0,
    ) -> Tuple:
        """构造幂等键（PRD-V5 §6.2 / REP-502）

        account_id + comment_type + source_rpid + generation_revision：
        - 同一文本重试（同 revision）→ 相同键 → 防止重复发布
        - 文本失效后重新生成（新 revision）→ 新键 → 允许发布新文本
        """
        return (
            str(account_id),
            int(comment_type),
            str(source_rpid),
            int(generation_revision),
        )

    def get_state(self, comment_type: int, source_rpid: str) -> Optional[Dict]:
        """查询回复状态"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM reply_states WHERE account_id=? AND comment_type=? AND source_rpid=?",
                (self.account_id, int(comment_type), str(source_rpid))
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def is_terminal(self, comment_type: int, source_rpid: str) -> bool:
        """是否处于终态（不再处理）"""
        row = self.get_state(comment_type, source_rpid)
        if not row:
            return False
        return row["state"] in TERMINAL_STATES

    def is_processed(self, comment_type: int, source_rpid: str) -> bool:
        """是否已有记录（终态或进行中，用于去重）"""
        return self.get_state(comment_type, source_rpid) is not None

    def is_in_progress(self, comment_type: int, source_rpid: str) -> bool:
        """是否处于中间态（REP-601）

        中间态：context_building / generation_pending / safety_pending / publish_pending
        - 与 is_terminal（终态）区分：终态不再处理
        - 与 is_processed（任意记录）区分：is_processed 还包含 deferred/retry_wait
        - 用于防止重复处理进行中的评论（如已在生成中又被重新拉入队列）
        """
        row = self.get_state(comment_type, source_rpid)
        if not row:
            return False
        return row["state"] in INTERMEDIATE_STATES

    def is_active_or_terminal(self, comment_type: int, source_rpid: str) -> bool:
        """是否存在任意记录（活跃或终态）（REP-601）

        返回 True 表示该评论曾被见过（有任何状态记录），
        用于与从未见过的评论区分。语义等价于 is_processed。
        """
        return self.get_state(comment_type, source_rpid) is not None

    def upsert(self, comment_type: int, source_rpid: str, state: str,
               notification: Dict = None, persona_id: str = "",
               metadata: Dict = None, error: str = "", error_code: str = "",
               generation_result: str = "",
               *,
               increment_attempt: bool = True) -> Dict:
        """创建或更新状态记录

        PRD V4 §9.1：
        - discovered → 新发现评论
        - ignored → 永久业务规则（黑名单、过短、自己评论）
        - rejected → 安全检查未通过
        - published → 发布成功
        - deferred → 临时依赖失败（LLM超时、搜索失败、限流、安全异常）
        - retry_wait → 发布失败，等待重试
        - failed → 超过重试上限

        increment_attempt=False：条件性延期（限流/安全未就绪/归档临时失败等）
        不消耗 attempt 预算，避免 3 次条件失败后永久 failed。
        真实发布失败（post_comment False）应保持默认 True。

        Read-modify-write of attempts is done under a single BEGIN IMMEDIATE
        connection to avoid concurrent workers under-counting attempts.
        """
        now = time.time()
        notif_json = json.dumps(notification, ensure_ascii=False) if notification else ""
        # metadata=None means "leave existing metadata alone" on conflict
        meta_json = (
            json.dumps(metadata, ensure_ascii=False)
            if metadata is not None
            else None
        )
        ct = int(comment_type)
        rpid = str(source_rpid)

        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM reply_states "
                "WHERE account_id=? AND comment_type=? AND source_rpid=?",
                (self.account_id, ct, rpid),
            ).fetchone()
            existing = dict(row) if row else None
            attempts = (existing["attempts"] if existing else 0)
            max_att = (existing["max_attempts"] if existing else self.max_attempts)

            # retry_wait / deferred：默认递增 attempts；条件失败可关闭
            if state in ("retry_wait", "deferred") and increment_attempt:
                attempts += 1
                # 超过重试上限 → failed（终态）
                if attempts >= max_att:
                    state = "failed"

            # BUG A-005：计算 next_retry_at（指数退避 + 抖动）
            # 条件失败（不烧 attempt）用更长底数，限流尤其拉长
            next_retry_at = None
            if state in ("retry_wait", "deferred"):
                import random
                base = min(2 ** max(attempts, 1), 300)  # 2, 4, 8... 最大 300 秒
                if not increment_attempt:
                    code = (error_code or "").upper()
                    if code in ("RATE_LIMIT", "RATE_LIMITED", "PM_RATE_LIMITED"):
                        # S9：评论限流更长退避（2~5 分钟）
                        base = min(max(base, 120), 300)
                    else:
                        # 其它条件失败：30~120s，避免热循环
                        base = min(max(base, 30), 120)
                jitter = random.uniform(0, base * 0.1)
                next_retry_at = now + base + jitter

            insert_meta = meta_json if meta_json is not None else "{}"
            conn.execute("""
                INSERT INTO reply_states
                    (account_id, comment_type, source_rpid, state, attempts, max_attempts,
                     last_error, last_error_code, notification_json, generation_result,
                     persona_id, created_at, updated_at, next_retry_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, comment_type, source_rpid) DO UPDATE SET
                    state=excluded.state,
                    attempts=excluded.attempts,
                    last_error=excluded.last_error,
                    last_error_code=excluded.last_error_code,
                    notification_json=COALESCE(NULLIF(excluded.notification_json, ''), reply_states.notification_json),
                    generation_result=COALESCE(NULLIF(excluded.generation_result, ''), reply_states.generation_result),
                    persona_id=COALESCE(NULLIF(excluded.persona_id, ''), reply_states.persona_id),
                    updated_at=excluded.updated_at,
                    next_retry_at=excluded.next_retry_at,
                    metadata=CASE
                        WHEN excluded.metadata IS NULL OR excluded.metadata = ''
                             OR excluded.metadata = '{}'
                        THEN reply_states.metadata
                        ELSE excluded.metadata
                    END
            """, (
                self.account_id, ct, rpid,
                state, attempts, max_att,
                error, error_code,
                notif_json,
                generation_result,
                persona_id,
                existing["created_at"] if existing else now,
                now, next_retry_at,
                insert_meta if meta_json is not None else (existing.get("metadata") if existing else "{}"),
            ))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

        return self.get_state(ct, rpid)

    # ───────────────────────────────────────────────────────
    # PRD-V5 §6.2 / REP-502：生成文本持久化
    # ───────────────────────────────────────────────────────

    @staticmethod
    def compute_generation_hash(text: str) -> str:
        """计算生成文本的内容哈希（SHA-256）"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def save_generation_result(
        self,
        comment_type: int,
        source_rpid: str,
        text: str,
        persona_id: str = "",
        audit_id: Optional[str] = None,
    ) -> Dict:
        """持久化生成文本（PRD-V5 §6.2 / REP-502）

        在获取到有效文本后、安全检查之前调用：
        - 写入 generation_result / generation_hash / generation_persona_id /
          generation_audit_id
        - 推进 generation_revision（首次为 1，之后每次重新生成递增）
        - 不改变 state（由调用方在安全检查后自行推进到 publish_pending）

        幂等：对同一回复再次调用（文本失效后重新生成）会推进 revision。
        """
        if not text:
            return self.get_state(comment_type, source_rpid) or {}

        now = time.time()
        gen_hash = self.compute_generation_hash(text)
        existing = self.get_state(comment_type, source_rpid)
        # revision 策略（PRD-V5 §6.2）：
        # - 首次保存（prev_rev==0）→ revision=1
        # - 相同文本（hash 匹配）→ 保持当前 revision
        # - 文本已被 invalidate（prev_hash 为空但 prev_rev>0）→ 使用 invalidate
        #   已推进的 revision（不再 +1）
        # - 不同文本且未经 invalidate（prev_hash 非空但不匹配）→ revision+1
        prev_rev = 0
        prev_hash = ""
        if existing:
            try:
                prev_rev = int(existing.get("generation_revision") or 0)
            except (TypeError, ValueError):
                prev_rev = 0
            prev_hash = existing.get("generation_hash") or ""

        if prev_rev == 0:
            new_rev = 1
        elif prev_hash and prev_hash == gen_hash:
            new_rev = prev_rev
        elif not prev_hash:
            # invalidate 已推进 revision，直接使用
            new_rev = prev_rev
        else:
            # 不同文本（隐式失效）→ 推进 revision
            new_rev = prev_rev + 1

        conn = self._get_conn()
        try:
            # 确保行存在（state 保持原值或默认 discovered）
            cur_state = existing["state"] if existing else "generation_pending"
            created_at = existing["created_at"] if existing else now
            conn.execute("""
                INSERT INTO reply_states
                    (account_id, comment_type, source_rpid, state, attempts, max_attempts,
                     last_error, last_error_code, notification_json, generation_result,
                     generation_hash, generation_revision, generation_audit_id,
                     generation_persona_id, persona_id,
                     created_at, updated_at, next_retry_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, comment_type, source_rpid) DO UPDATE SET
                    generation_result=excluded.generation_result,
                    generation_hash=excluded.generation_hash,
                    generation_revision=excluded.generation_revision,
                    generation_audit_id=excluded.generation_audit_id,
                    generation_persona_id=COALESCE(NULLIF(excluded.generation_persona_id, ''), reply_states.generation_persona_id),
                    persona_id=COALESCE(NULLIF(excluded.persona_id, ''), reply_states.persona_id),
                    updated_at=excluded.updated_at
            """, (
                self.account_id, int(comment_type), str(source_rpid),
                cur_state,
                existing["attempts"] if existing else 0,
                existing["max_attempts"] if existing else self.max_attempts,
                existing["last_error"] if existing else "",
                existing["last_error_code"] if existing else "",
                existing["notification_json"] if existing else "",
                text,
                gen_hash,
                new_rev,
                audit_id or "",
                persona_id or "",
                persona_id or "",
                created_at,
                now,
                existing["next_retry_at"] if existing else None,
                existing["metadata"] if existing else "{}",
            ))
            conn.commit()
        finally:
            conn.close()

        return self.get_state(comment_type, source_rpid)

    def invalidate_generation(self, comment_type: int, source_rpid: str) -> Dict:
        """使生成文本失效（PRD-V5 §6.2 / REP-502）

        仅由管理员操作或安全策略变更调用：
        - 清空 generation_result / generation_hash / generation_audit_id
        - 推进 generation_revision（下次 save_generation_result 会再 +1，
          实际产生新 revision 的幂等键）
        - 清空后若处于 retry_wait，应转为 deferred 重新走生成流程
        """
        existing = self.get_state(comment_type, source_rpid)
        if not existing:
            return {}

        now = time.time()
        try:
            prev_rev = int(existing.get("generation_revision") or 0)
        except (TypeError, ValueError):
            prev_rev = 0

        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE reply_states SET
                    generation_result='',
                    generation_hash='',
                    generation_audit_id='',
                    generation_revision=?,
                    updated_at=?
                WHERE account_id=? AND comment_type=? AND source_rpid=?
            """, (
                prev_rev + 1,
                now,
                self.account_id, int(comment_type), str(source_rpid),
            ))
            conn.commit()
        finally:
            conn.close()

        return self.get_state(comment_type, source_rpid)

    def mark_published(self, comment_type: int, source_rpid: str,
                       generation_result: str = "") -> Dict:
        """标记为已发布（终态）"""
        return self.upsert(comment_type, source_rpid, "published",
                           generation_result=generation_result)

    def mark_ignored(self, comment_type: int, source_rpid: str,
                     rule: str = "", notification: Dict = None) -> Dict:
        """标记为忽略（终态 - 业务规则）"""
        return self.upsert(comment_type, source_rpid, "ignored",
                           notification=notification, error=rule,
                           error_code="BUSINESS_RULE")

    def mark_rejected(self, comment_type: int, source_rpid: str,
                      reason: str = "") -> Dict:
        """标记为拒绝（终态 - 安全/策略）"""
        return self.upsert(comment_type, source_rpid, "rejected",
                           error=reason, error_code="SAFETY_REJECT")

    def mark_failed(
        self,
        comment_type: int,
        source_rpid: str,
        reason: str = "",
        error_code: str = "FAILED",
    ) -> Dict:
        """标记为失败（终态 - 配置/永久生成错误，非安全策略拒绝）

        与 mark_rejected 区分：rejected = SAFETY_REJECT；failed = 无法恢复的业务/配置失败。
        """
        return self.upsert(
            comment_type,
            source_rpid,
            "failed",
            error=reason,
            error_code=error_code or "FAILED",
            increment_attempt=False,
        )

    def mark_deferred(
        self,
        comment_type: int,
        source_rpid: str,
        reason: str = "",
        error_code: str = "TEMP_FAILURE",
        *,
        increment_attempt: bool = True,
    ) -> Dict:
        """标记为延迟（非终态 - 临时失败，可恢复）

        increment_attempt=False：条件失败（限流/安全未就绪/归档临时失败等），
        不消耗 attempt 预算。
        """
        return self.upsert(
            comment_type, source_rpid, "deferred",
            error=reason, error_code=error_code,
            increment_attempt=increment_attempt,
        )

    def mark_retry_wait(
        self,
        comment_type: int,
        source_rpid: str,
        reason: str = "",
        error_code: str = "PUBLISH_FAILED",
        *,
        increment_attempt: bool = True,
    ) -> Dict:
        """标记为等待重试（非终态 - 发布失败，将重试）

        PRD-V5 §6.2 / REP-502：generation_result / generation_hash /
        generation_revision 等字段由 upsert 的 ON CONFLICT 子句自动保留
        （不在 SET 列表中的列保持原值），重试时可读出原始文本。

        increment_attempt=False：仅延期不烧 attempt（与 proactive 一致）。
        真实 post_comment False 应保持默认 True。
        """
        return self.upsert(
            comment_type, source_rpid, "retry_wait",
            error=reason, error_code=error_code,
            increment_attempt=increment_attempt,
        )

    def mark_manual_retry(
        self,
        comment_type: int,
        source_rpid: str,
        reason: str = "manual_retry",
        error_code: str = "MANUAL_RETRY",
    ) -> Dict:
        """UI 手动重试：设为 retry_wait 且立即可调度，不消耗 attempts 预算。

        与 mark_retry_wait 区别：
        - 不递增 attempts（避免一点就变 failed）
        - next_retry_at = now（下一轮主循环立即拾取）
        - 允许从 failed / result_unknown 拉回（人工覆盖）
        """
        now = time.time()
        ct = int(comment_type)
        rpid = str(source_rpid)
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM reply_states "
                "WHERE account_id=? AND comment_type=? AND source_rpid=?",
                (self.account_id, ct, rpid),
            ).fetchone()
            existing = dict(row) if row else None
            attempts = int(existing["attempts"]) if existing else 0
            max_att = int(existing["max_attempts"]) if existing else self.max_attempts
            # 若已达上限，手动重试时抬高 max_attempts，允许再试一次
            if attempts >= max_att:
                max_att = attempts + 1
            created_at = float(existing["created_at"]) if existing else now
            meta = (existing.get("metadata") if existing else None) or "{}"
            gen = (existing.get("generation_result") if existing else None) or ""
            gen_hash = (existing.get("generation_hash") if existing else None) or ""
            try:
                gen_rev = int(existing.get("generation_revision") or 0) if existing else 0
            except (TypeError, ValueError):
                gen_rev = 0
            gen_audit = (existing.get("generation_audit_id") if existing else None) or ""
            gen_persona = (existing.get("generation_persona_id") if existing else None) or ""
            persona = (existing.get("persona_id") if existing else None) or ""
            notif = (existing.get("notification_json") if existing else None) or ""
            # 保留 generation_*，确保 retry_wait 可复用原文发布（REP-502）
            conn.execute(
                """
                INSERT INTO reply_states
                    (account_id, comment_type, source_rpid, state, attempts, max_attempts,
                     last_error, last_error_code, notification_json, generation_result,
                     generation_hash, generation_revision, generation_audit_id,
                     generation_persona_id, persona_id, created_at, updated_at,
                     next_retry_at, metadata)
                VALUES (?, ?, ?, 'retry_wait', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, comment_type, source_rpid) DO UPDATE SET
                    state='retry_wait',
                    max_attempts=excluded.max_attempts,
                    last_error=excluded.last_error,
                    last_error_code=excluded.last_error_code,
                    generation_result=COALESCE(
                        NULLIF(excluded.generation_result, ''),
                        reply_states.generation_result
                    ),
                    generation_hash=COALESCE(
                        NULLIF(excluded.generation_hash, ''),
                        reply_states.generation_hash
                    ),
                    generation_revision=CASE
                        WHEN excluded.generation_revision > 0
                        THEN excluded.generation_revision
                        ELSE reply_states.generation_revision
                    END,
                    generation_audit_id=COALESCE(
                        NULLIF(excluded.generation_audit_id, ''),
                        reply_states.generation_audit_id
                    ),
                    generation_persona_id=COALESCE(
                        NULLIF(excluded.generation_persona_id, ''),
                        reply_states.generation_persona_id
                    ),
                    updated_at=excluded.updated_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    self.account_id, ct, rpid,
                    attempts, max_att,
                    reason, error_code,
                    notif, gen, gen_hash, gen_rev, gen_audit, gen_persona, persona,
                    created_at, now, now,  # next_retry_at = now → 立即可调度
                    meta,
                ),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return self.get_state(ct, rpid) or {}

    def mark_result_unknown(
        self,
        comment_type: int,
        source_rpid: str,
        reason: str = "",
        error_code: str = "RESULT_UNKNOWN",
    ) -> Dict:
        """平台结果不确定（超时/网络/5xx）→ 终态，不自动重发，防双发。

        generation_* 字段保留，便于人工对账 / 楼中楼幂等确认。
        """
        return self.upsert(
            comment_type,
            source_rpid,
            "result_unknown",
            error=reason,
            error_code=error_code,
        )

    def get_retryable(self, now: float = None) -> List[Dict]:
        """获取可重试的回复（retry_wait/deferred 且 next_retry_at <= now）

        REP-602：同时返回卡在中间态超过阈值的记录（updated_at < 阈值），
        阈值为 10 分钟前。若调度器主循环已定期调用 recover_stuck_intermediate()，
        这些记录会被先转为 deferred 再被本方法拾取；本子句作为安全网，
        确保即使 recover_stuck_intermediate 未调用也能暴露卡住的记录。
        """
        now = now or time.time()
        stuck_threshold = now - DEFAULT_STUCK_TIMEOUT_MINUTES * 60
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM reply_states "
                "WHERE account_id=? AND ("
                "  (state IN ('retry_wait','deferred') "
                "   AND next_retry_at IS NOT NULL AND next_retry_at <= ?) "
                "  OR (state IN ('generation_pending','safety_pending',"
                "                'publish_pending','context_building') "
                "      AND updated_at < ?)"
                ") "
                "ORDER BY next_retry_at ASC LIMIT 50",
                (self.account_id, now, stuck_threshold)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def recover_stuck_intermediate(
        self, timeout_minutes: int = DEFAULT_STUCK_TIMEOUT_MINUTES
    ) -> List[Dict]:
        """恢复卡在中间态的评论（REP-602）

        若进程在中间态（context_building / generation_pending /
        safety_pending / publish_pending）崩溃，这些评论既不是终态也不在
        retry_wait/deferred 中，永远不会被 get_retryable 恢复。

        本方法找到 updated_at 早于阈值的中间态记录，将其转为 deferred 状态
        （next_retry_at 设为当前时间，立即可被拾取），generation_* 字段保持不变。

        应在调度器主循环中定期调用（例如每次轮询前），调用示例::

            self.reply_state_store.recover_stuck_intermediate()

        注意：本方法不应在持有其它数据库连接事务时调用。

        Returns:
            被恢复的记录列表（已含更新后的 state='deferred'）
        """
        now = time.time()
        threshold = now - timeout_minutes * 60
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM reply_states "
                "WHERE account_id=? AND state IN "
                "  ('generation_pending','safety_pending',"
                "   'publish_pending','context_building') "
                "AND updated_at < ?",
                (self.account_id, threshold)
            ).fetchall()
            recovered = []
            for row in rows:
                r = dict(row)
                prev_state = r.get("state") or ""
                gen_result = (r.get("generation_result") or "").strip()
                # publish_pending + 已有生成文本：优先进 retry_wait 复用原文，
                # 避免 deferred 全量重生后二次 post_comment 造成重复回复。
                if prev_state == "publish_pending" and gen_result:
                    conn.execute(
                        "UPDATE reply_states SET "
                        "  state='retry_wait', next_retry_at=?, updated_at=?, "
                        "  last_error_code='STUCK_PUBLISH_PENDING', "
                        "  last_error=? "
                        "WHERE account_id=? AND comment_type=? AND source_rpid=? "
                        "  AND state='publish_pending'",
                        (now, now, "stuck recovery from publish_pending",
                         self.account_id, r["comment_type"], r["source_rpid"])
                    )
                    r["state"] = "retry_wait"
                    r["last_error_code"] = "STUCK_PUBLISH_PENDING"
                else:
                    # 其它中间态 / 无生成文本：deferred 重新生成
                    conn.execute(
                        "UPDATE reply_states SET "
                        "  state='deferred', next_retry_at=?, updated_at=?, "
                        "  last_error_code='STUCK_RECOVERY' "
                        "WHERE account_id=? AND comment_type=? AND source_rpid=? "
                        "  AND state IN ('generation_pending','safety_pending',"
                        "               'publish_pending','context_building')",
                        (now, now,
                         self.account_id, r["comment_type"], r["source_rpid"])
                    )
                    r["state"] = "deferred"
                    r["last_error_code"] = "STUCK_RECOVERY"
                r["next_retry_at"] = now
                r["updated_at"] = now
                recovered.append(r)
            conn.commit()
            if recovered:
                logger.info(
                    f"REP-602: 恢复 {len(recovered)} 条卡在中间态的评论为 deferred"
                )
            return recovered
        finally:
            conn.close()

    def get_stats(self) -> Dict[str, int]:
        """获取状态统计"""
        conn = self._get_conn()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM reply_states WHERE account_id=?",
                (self.account_id,)
            ).fetchone()[0]
            by_state = {}
            for row in conn.execute(
                "SELECT state, COUNT(*) as cnt FROM reply_states WHERE account_id=? GROUP BY state",
                (self.account_id,)
            ).fetchall():
                by_state[row["state"]] = row["cnt"]
            return {"total": total, "by_state": by_state}
        finally:
            conn.close()

    def migrate_from_replied_json(self, replied_list: list):
        """PRD V4 MIG-003：从旧 replied.json 迁移

        旧 replied.json 条目迁移为 published_legacy 终态，仅对对应账号生效。
        无法确认账号归属的旧状态不得复制到所有账号（由 account_id 隔离保证）。
        """
        if not replied_list:
            return
        migrated = 0
        for rpid in replied_list:
            rpid_str = str(rpid)
            if not rpid_str:
                continue
            # 默认 comment_type=1（视频评论），无法从旧格式确定
            if not self.is_processed(1, rpid_str):
                # PRD V4 MIG-003：使用 published_legacy 而非 published
                self.upsert(
                    1,
                    rpid_str,
                    "published_legacy",
                    error="legacy_migration",
                    error_code="LEGACY_MIGRATION",
                )
                migrated += 1
        if migrated:
            logger.info(f"从 replied.json 迁移 {migrated} 条记录为 published_legacy")

    def close(self):
        """短连接模式无需关闭"""
        pass
