"""
回复状态机（PRD V4 §9.1 / REP-001，PRD-V5 §6.2 / REP-502 生成文本持久化）

状态流转：
    discovered → ignored | context_building
    context_building → generation_pending | deferred
    generation_pending → safety_pending | deferred
    safety_pending → publish_pending | rejected | deferred
    publish_pending → published | retry_wait
    retry_wait → publish_pending | failed

终态（不再处理）：published, published_legacy, ignored, rejected, failed
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
TERMINAL_STATES = frozenset({"published", "published_legacy", "ignored", "rejected", "failed"})

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
               generation_result: str = "") -> Dict:
        """创建或更新状态记录

        PRD V4 §9.1：
        - discovered → 新发现评论
        - ignored → 永久业务规则（黑名单、过短、自己评论）
        - rejected → 安全检查未通过
        - published → 发布成功
        - deferred → 临时依赖失败（LLM超时、搜索失败、限流、安全异常）
        - retry_wait → 发布失败，等待重试
        - failed → 超过重试上限
        """
        now = time.time()
        existing = self.get_state(comment_type, source_rpid)
        attempts = (existing["attempts"] if existing else 0)
        max_att = (existing["max_attempts"] if existing else self.max_attempts)

        # 状态递增 attempts（仅对 retry_wait → publish_pending 转换）
        if state == "retry_wait":
            attempts += 1
            # 超过重试上限 → failed
            if attempts >= max_att:
                state = "failed"

        # 计算 next_retry_at（指数退避 + 抖动）
        next_retry_at = None
        if state == "retry_wait":
            import random
            base = min(2 ** attempts, 300)  # 2, 4, 8... 最大 300 秒
            jitter = random.uniform(0, base * 0.1)
            next_retry_at = now + base + jitter
        elif state == "deferred":
            # deferred 下次轮询自然恢复
            next_retry_at = now + 60  # 60 秒后可恢复

        conn = self._get_conn()
        try:
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
                    metadata=excluded.metadata
            """, (
                self.account_id, int(comment_type), str(source_rpid),
                state, attempts, max_att,
                error, error_code,
                json.dumps(notification, ensure_ascii=False) if notification else "",
                generation_result,
                persona_id,
                existing["created_at"] if existing else now,
                now, next_retry_at,
                json.dumps(metadata or {}, ensure_ascii=False),
            ))
            conn.commit()
        finally:
            conn.close()

        return self.get_state(comment_type, source_rpid)

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

    def mark_deferred(self, comment_type: int, source_rpid: str,
                      reason: str = "", error_code: str = "TEMP_FAILURE") -> Dict:
        """标记为延迟（非终态 - 临时失败，可恢复）"""
        return self.upsert(comment_type, source_rpid, "deferred",
                           error=reason, error_code=error_code)

    def mark_retry_wait(self, comment_type: int, source_rpid: str,
                        reason: str = "", error_code: str = "PUBLISH_FAILED") -> Dict:
        """标记为等待重试（非终态 - 发布失败，将重试）

        PRD-V5 §6.2 / REP-502：generation_result / generation_hash /
        generation_revision 等字段由 upsert 的 ON CONFLICT 子句自动保留
        （不在 SET 列表中的列保持原值），重试时可读出原始文本。
        """
        return self.upsert(comment_type, source_rpid, "retry_wait",
                           error=reason, error_code=error_code)

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
                # 转为 deferred，next_retry_at 设为当前时间立即可被拾取。
                # attempts/max_attempts/generation_* 字段保持原值。
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
                self.upsert(1, rpid_str, "published_legacy",
                            rule="legacy_migration")
                migrated += 1
        if migrated:
            logger.info(f"从 replied.json 迁移 {migrated} 条记录为 published_legacy")

    def close(self):
        """短连接模式无需关闭"""
        pass
