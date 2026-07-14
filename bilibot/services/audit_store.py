"""
生成审计存储 - AuditStore

记录每次文本生成的完整审计信息：
- scene / persona_id / input_summary / context_summary
- prompt_preview / output / published / target / created_at
"""
import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bilibot.audit")


# PRD-V5 §13.1 OBS-501：审计记录的语义化状态枚举
# 统计必须按这些状态分别返回，不得用 total - published 推算草稿数
STATUS_VALUES = (
    "generated",         # 内容已生成
    "awaiting_review",   # 等待管理员审核
    "approved",          # 管理员已通过
    "rejected",          # 管理员已拒绝
    "publishing",        # 发布流程中
    "published",         # 已成功发布
    "retry_wait",        # 等待重试
    "result_unknown",    # 平台结果不确定
    "failed",            # 永久失败
    "expired",           # 已过期（草稿或任务超时）
)


class AuditStore:
    """生成审计记录存储（SQLite + JSON 备份）"""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "audit.db"
        # 线程本地连接缓存：同一线程复用连接，避免每次操作都新建/关闭 SQLite 连接
        self._tls = threading.local()
        self._init_db()

    # BUG C-002：统一连接参数（WAL + busy_timeout + check_same_thread），
    # 与 pm_state_store/task_store/reply_state 对齐
    def _get_conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._tls.conn = conn
        return conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_audits (
                id TEXT PRIMARY KEY,
                scene TEXT NOT NULL,
                persona_id TEXT NOT NULL,
                input_summary TEXT DEFAULT '',
                context_summary TEXT DEFAULT '',
                prompt_preview TEXT DEFAULT '',
                output TEXT DEFAULT '',
                published INTEGER DEFAULT 0,
                target TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'generated'
            )
        """)
        # PRD-V5 §13.1 OBS-501：为旧库补 status 列并回填语义化状态
        cols = {row[1] for row in conn.execute("PRAGMA table_info(generation_audits)").fetchall()}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE generation_audits ADD COLUMN status TEXT DEFAULT 'generated'"
            )
            # 旧记录按 published 字段回填：已发布 -> published，未发布 -> generated
            conn.execute(
                "UPDATE generation_audits SET status = 'published' WHERE published = 1"
            )
            conn.execute(
                "UPDATE generation_audits SET status = 'generated' WHERE published = 0"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audits_scene ON generation_audits(scene)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audits_created ON generation_audits(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audits_persona ON generation_audits(persona_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audits_status ON generation_audits(status)")
        # PRD-V5 §4.3 SEA-501：外部数据披露审计（第三方搜索调用前记录）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS external_disclosures (
                id TEXT PRIMARY KEY,
                scene TEXT NOT NULL,
                backend TEXT NOT NULL,
                query_hash TEXT DEFAULT '',
                redacted_preview TEXT DEFAULT '',
                field_types TEXT DEFAULT '',
                account_id TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_scene ON external_disclosures(scene)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_created ON external_disclosures(created_at)")
        conn.commit()

    def record(
        self,
        scene: str,
        persona_id: str,
        input_summary: str = "",
        context_summary: str = "",
        prompt_preview: str = "",
        output: str = "",
        published: bool = False,
        target: Optional[Dict[str, Any]] = None,
        audit_id: Optional[str] = None,
        status: str = "generated",
    ) -> str:
        """记录一次生成审计

        Args:
            status: PRD-V5 §13.1 OBS-501 语义化状态，默认 generated。
                    取值见 STATUS_VALUES。
        """
        aid = audit_id or f"gen_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now().isoformat()
        target_json = json.dumps(target or {}, ensure_ascii=False)
        if status not in STATUS_VALUES:
            logger.warning(f"record: 未知 status={status}，回退为 generated")
            status = "generated"

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO generation_audits (id, scene, persona_id, input_summary, context_summary, prompt_preview, output, published, target, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (aid, scene, persona_id, input_summary[:500], context_summary[:500],
             prompt_preview[:2000], output[:2000], int(published), target_json, created_at, status),
        )
        conn.commit()

        # JSON backup
        try:
            backup = self.data_dir / "audit_backup.json"
            records = []
            if backup.exists():
                with open(backup, "r", encoding="utf-8") as f:
                    records = json.load(f)
            records.append({
                "id": aid, "scene": scene, "persona_id": persona_id,
                "input_summary": input_summary, "context_summary": context_summary,
                "prompt_preview": prompt_preview, "output": output,
                "published": published, "target": target or {}, "created_at": created_at,
                "status": status,
            })
            # 原子写：临时文件 + os.replace，避免崩溃导致文件损坏
            fd, tmp_path = tempfile.mkstemp(dir=str(self.data_dir), suffix=".json.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(records[-500:], f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(backup))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            # L10：JSON 备份失败不影响主流程（SQLite 已写入），仅记录 warning
            logger.warning(f"审计 JSON 备份写入失败: {e}")

        logger.debug(f"审计记录: {aid} scene={scene} status={status}")
        return aid

    async def record_async(self, *args, **kwargs) -> str:
        """record() 的异步包装——将 SQLite 写入和 JSON 备份的同步 I/O 卸载到线程。

        Task 25 SubTask 25.3：JSON 备份读写用 asyncio.to_thread 包装，
        避免在事件循环中阻塞。
        """
        return await asyncio.to_thread(self.record, *args, **kwargs)

    def record_external_disclosure(
        self,
        scene: str,
        backend: str,
        query_hash: str = "",
        redacted_preview: str = "",
        field_types: str = "",
        account_id: str = "",
    ) -> str:
        """PRD-V5 §4.3 SEA-501：记录外部数据披露事件

        在调用第三方搜索后端前记录，用于审计追踪。
        仅记录 query_hash 和 redacted_preview，绝不存储原始查询文本。

        Args:
            scene: 调用场景（如 private_message）
            backend: 搜索后端名称（tavily / perplexity / bocha / custom）
            query_hash: 查询文本的 sha256 哈希（十六进制）
            redacted_preview: 脱敏后查询的前 50 字符预览
            field_types: 检测到的敏感字段类型，逗号分隔（如 "phone,email"）
            account_id: 账号 ID

        Returns:
            disclosure_id
        """
        did = f"disc_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now().isoformat()
        try:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO external_disclosures "
                "(id, scene, backend, query_hash, redacted_preview, field_types, account_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (did, scene, backend, query_hash,
                 redacted_preview[:200], field_types[:200],
                 account_id or "", created_at),
            )
            conn.commit()
            logger.debug(f"外部披露记录: {did} scene={scene} backend={backend}")
        except Exception as e:
            logger.error(f"record_external_disclosure 失败: {e}")
        return did

    def query_external_disclosures(
        self,
        scene: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询外部数据披露记录"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM external_disclosures WHERE 1=1"
        params: list = []
        if scene:
            sql += " AND scene = ?"
            params.append(scene)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def query(
        self,
        scene: Optional[str] = None,
        persona_id: Optional[str] = None,
        published: Optional[bool] = None,
        keyword: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """查询审计记录"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row

        sql = "SELECT * FROM generation_audits WHERE 1=1"
        params = []

        if scene:
            sql += " AND scene = ?"
            params.append(scene)
        if persona_id:
            sql += " AND persona_id = ?"
            params.append(persona_id)
        if published is not None:
            sql += " AND published = ?"
            params.append(int(published))
        if keyword:
            sql += " AND (input_summary LIKE ? OR context_summary LIKE ? OR output LIKE ?)"
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])

        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get(self, audit_id: str) -> Optional[Dict[str, Any]]:
        """获取单条审计记录"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM generation_audits WHERE id = ?", (audit_id,)).fetchone()
        return dict(row) if row else None

    def list_by_status(
        self,
        scene: Any = "",
        status: str = "",
        page: int = 1,
        page_size: int = 20,
        keyword: str = "",
    ) -> Dict[str, Any]:
        """UI-606：按状态分页查询审计记录（SQL 层过滤）

        替代旧实现中拉取全部记录再内存过滤的模式。

        Args:
            scene: 场景过滤。支持：
                - str：单个 scene（如 reply_comment）
                - Sequence[str]：多个 scene（IN 查询，如回复 + 主动评论）
            status: 状态筛选，取值：
                - "" / "all"：全部
                - "pending"：未发布且无 failure_reason
                - "replied"：已发布（published=1）
                - "failed"：未发布且 target.failure_reason 非空
            page: 页码（1-based）
            page_size: 每页条数
            keyword: 可选关键词（匹配 input_summary / context_summary / output）

        Returns:
            {"items": [...], "total": int, "page": int, "page_size": int}
        """
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        offset = (page - 1) * page_size
        keyword = (keyword or "").strip()

        if isinstance(scene, (list, tuple, set)):
            scenes = [str(s).strip() for s in scene if str(s).strip()]
        elif scene:
            scenes = [str(scene).strip()]
        else:
            scenes = []

        conn = self._get_conn()
        conn.row_factory = sqlite3.Row
        if not scenes:
            base_sql = "FROM generation_audits WHERE 1=1"
            params: list = []
        elif len(scenes) == 1:
            base_sql = "FROM generation_audits WHERE scene = ?"
            params = [scenes[0]]
        else:
            placeholders = ",".join("?" for _ in scenes)
            base_sql = f"FROM generation_audits WHERE scene IN ({placeholders})"
            params = list(scenes)

        if status == "pending":
            # 未发布 且 无 failure_reason
            base_sql += (
                " AND published = 0"
                " AND (json_extract(target, '$.failure_reason') IS NULL"
                "      OR json_extract(target, '$.failure_reason') = '')"
            )
        elif status == "replied":
            base_sql += " AND published = 1"
        elif status == "failed":
            # 未发布 且 failure_reason 非空
            base_sql += (
                " AND published = 0"
                " AND json_extract(target, '$.failure_reason') IS NOT NULL"
                " AND json_extract(target, '$.failure_reason') != ''"
            )
        # else: status 为空或未知 → 不附加条件，返回全部

        if keyword:
            base_sql += (
                " AND (input_summary LIKE ? OR context_summary LIKE ?"
                " OR output LIKE ? OR prompt_preview LIKE ?)"
            )
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw])

        total = conn.execute(
            f"SELECT COUNT(*) {base_sql}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"SELECT * {base_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        items = [dict(r) for r in rows]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def mark_published(
        self,
        audit_id: str,
        published: bool = True,
        target: Optional[Dict[str, Any]] = None,
        failure_reason: Optional[str] = None,
    ) -> bool:
        """更新审计记录的发布状态（PRD V4 §4.5.1）

        PRD-V5 §13.1 OBS-501：同步更新语义化 status 字段。
        - published=True  -> status='published'
        - published=False 且有 failure_reason -> status='failed'
        - published=False 且无 failure_reason -> status 保持不变（视为回退）

        Args:
            audit_id: 审计 id
            published: 是否已发布
            target: 可选，合并到现有 target JSON 中（覆盖同 key）
            failure_reason: 可选，发布失败原因（写入 target.failure_reason）

        Returns:
            True 表示更新成功，False 表示记录不存在或写入失败
        """
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row

            # 构造 JSON merge patch（包含 target 与 failure_reason）
            patch: Dict[str, Any] = {}
            if target:
                try:
                    patch.update(target)
                except Exception:
                    pass
            if failure_reason:
                patch["failure_reason"] = failure_reason
            patch_json = json.dumps(patch, ensure_ascii=False)

            # OBS-501：根据发布结果推导语义化状态
            if published:
                new_status = "published"
            elif failure_reason:
                new_status = "failed"
            else:
                new_status = None  # 保持原状

            # 优先使用 SQLite json_patch 在 SQL 层原子合并 target，
            # 避免非原子读-改-写导致并发更新丢失
            try:
                if new_status is not None:
                    cur = conn.execute(
                        "UPDATE generation_audits "
                        "SET target = json_patch(COALESCE(target, '{}'), ?), "
                        "    published = ?, status = ? "
                        "WHERE id = ?",
                        (patch_json, int(published), new_status, audit_id),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE generation_audits "
                        "SET target = json_patch(COALESCE(target, '{}'), ?), "
                        "    published = ? "
                        "WHERE id = ?",
                        (patch_json, int(published), audit_id),
                    )
                conn.commit()
                if cur.rowcount == 0:
                    logger.warning(f"mark_published: 审计记录不存在 {audit_id}")
                    return False
                logger.debug(f"审计 {audit_id} 发布状态更新为 published={published}")
                return True
            except sqlite3.OperationalError:
                # SQLite 版本不支持 json_patch，回退到 Python 层合并
                conn.rollback()
                row = conn.execute(
                    "SELECT target FROM generation_audits WHERE id = ?", (audit_id,)
                ).fetchone()
                if row is None:
                    logger.warning(f"mark_published: 审计记录不存在 {audit_id}")
                    return False

                try:
                    existing_target = json.loads(row["target"] or "{}")
                except Exception:
                    existing_target = {}
                if target:
                    try:
                        existing_target.update(target)
                    except Exception:
                        pass
                if failure_reason:
                    existing_target["failure_reason"] = failure_reason
                target_json = json.dumps(existing_target, ensure_ascii=False)

                if new_status is not None:
                    conn.execute(
                        "UPDATE generation_audits SET published = ?, target = ?, status = ? WHERE id = ?",
                        (int(published), target_json, new_status, audit_id),
                    )
                else:
                    conn.execute(
                        "UPDATE generation_audits SET published = ?, target = ? WHERE id = ?",
                        (int(published), target_json, audit_id),
                    )
                conn.commit()
                logger.debug(f"审计 {audit_id} 发布状态更新为 published={published}")
                return True
        except Exception as e:
            logger.error(f"mark_published 失败 audit_id={audit_id}: {e}")
            return False

    def set_status(self, audit_id: str, status: str) -> bool:
        """PRD-V5 §13.1 OBS-501：直接设置审计记录的语义化状态

        Args:
            audit_id: 审计 id
            status: 目标状态，取值见 STATUS_VALUES

        Returns:
            True 表示更新成功，False 表示记录不存在或状态非法
        """
        if status not in STATUS_VALUES:
            logger.warning(f"set_status: 非法 status={status}")
            return False
        try:
            conn = self._get_conn()
            cur = conn.execute(
                "UPDATE generation_audits SET status = ? WHERE id = ?",
                (status, audit_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                logger.warning(f"set_status: 审计记录不存在 {audit_id}")
                return False
            logger.debug(f"审计 {audit_id} 状态更新为 {status}")
            return True
        except Exception as e:
            logger.error(f"set_status 失败 audit_id={audit_id}: {e}")
            return False

    # 向后兼容别名
    def update_result(
        self,
        audit_id: str,
        published: Optional[bool] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """update_result 别名（PRD V4 §4.5.1 备选命名）"""
        if published is None:
            published = True
        return self.mark_published(audit_id, published=published, target=target)

    def count(self, scene: Optional[str] = None) -> int:
        """统计记录数"""
        conn = self._get_conn()
        if scene:
            row = conn.execute("SELECT COUNT(*) FROM generation_audits WHERE scene = ?", (scene,)).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) FROM generation_audits").fetchone()
        return row[0] if row else 0

    def stats(self) -> Dict[str, Any]:
        """获取统计信息（PRD-V5 §13.1 OBS-501）

        按语义化状态分别返回计数，不得用 total - published 推算草稿数。
        返回的 by_status 字典保证包含全部 STATUS_VALUES 中的 10 个状态键
        （无记录时为 0）。
        """
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row

        total = conn.execute("SELECT COUNT(*) FROM generation_audits").fetchone()[0]
        by_scene = dict(conn.execute(
            "SELECT scene, COUNT(*) FROM generation_audits GROUP BY scene"
        ).fetchall())
        # OBS-501：按 status 分组统计，保证全部状态键都存在
        status_rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM generation_audits GROUP BY status"
        ).fetchall()

        by_status: Dict[str, int] = {s: 0 for s in STATUS_VALUES}
        for r in status_rows:
            key = r["status"] or "generated"
            if key in by_status:
                by_status[key] = r["cnt"]
            else:
                # 未知状态归入 result_unknown，避免漏计
                by_status["result_unknown"] += r["cnt"]

        result: Dict[str, Any] = {"total": total, "by_scene": by_scene}
        result.update(by_status)
        return result

    def stats_by_day(self, days: int = 30) -> list[dict]:
        """按天统计生成量（用于趋势图）"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row

        cutoff = datetime.fromtimestamp(
            datetime.now().timestamp() - days * 86400
        ).isoformat()

        rows = conn.execute(
            "SELECT DATE(created_at) as day, COUNT(*) as cnt "
            "FROM generation_audits WHERE created_at >= ? "
            "GROUP BY DATE(created_at) ORDER BY day",
            (cutoff,),
        ).fetchall()
        return [{"day": r["day"], "count": r["cnt"]} for r in rows]

    def stats_by_persona(self) -> list[dict]:
        """按人格统计（含平均输出长度）"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT persona_id, COUNT(*) as cnt, "
            "AVG(LENGTH(output)) as avg_len, "
            "SUM(CASE WHEN published=1 THEN 1 ELSE 0 END) as pub_cnt "
            "FROM generation_audits GROUP BY persona_id ORDER BY cnt DESC"
        ).fetchall()
        return [
            {
                "persona_id": r["persona_id"],
                "count": r["cnt"],
                "avg_output_len": round(r["avg_len"], 1) if r["avg_len"] else 0,
                "published": r["pub_cnt"],
            }
            for r in rows
        ]

    def stats_by_hour(self, days: int = 7) -> list[dict]:
        """按小时统计（24 小时热度分布）"""
        conn = self._get_conn()
        conn.row_factory = sqlite3.Row

        cutoff = datetime.fromtimestamp(
            datetime.now().timestamp() - days * 86400
        ).isoformat()

        rows = conn.execute(
            "SELECT CAST(STRFTIME('%H', created_at) AS INT) as hour, "
            "COUNT(*) as cnt FROM generation_audits "
            "WHERE created_at >= ? GROUP BY hour ORDER BY hour",
            (cutoff,),
        ).fetchall()
        return [{"hour": r["hour"], "count": r["cnt"]} for r in rows]

    def analytics(self) -> Dict[str, Any]:
        """综合统计数据（供 /api/audit/analytics 端点）"""
        return {
            "overview": self.stats(),
            "by_day": self.stats_by_day(days=30),
            "by_persona": self.stats_by_persona(),
            "by_hour": self.stats_by_hour(days=7),
        }
