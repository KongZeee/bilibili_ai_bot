"""
记忆 API 路由 - 修复版

修复：
- 表字段使用 category / is_active（与 knowledge_memory.py 一致）
- 路由顺序：stats/search/migrate 在 /api/memory/{id} 前
- 空数据库自动建表，返回 200
- DELETE 软删除用 is_active = 0
"""
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("bilibot.api.memory")


# ═══════════════════════════════════════════════════════
#  数据库 Schema
# ═══════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_atoms (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  content         TEXT    NOT NULL,
  category        TEXT    NOT NULL DEFAULT 'episodic',
  importance      TEXT    NOT NULL DEFAULT 'medium',
  importance_score REAL   DEFAULT 0.5,
  user_id         TEXT,
  username        TEXT,
  session_id      TEXT,
  persona_id      TEXT,
  created_at      REAL    NOT NULL,
  last_accessed   REAL    NOT NULL,
  access_count    INTEGER DEFAULT 0,
  ttl_days        REAL    DEFAULT 30.0,
  decay_type      TEXT    DEFAULT 'exponential',
  metadata        TEXT    DEFAULT '{}',
  is_active       INTEGER DEFAULT 1
)
"""

INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_mem_category ON memory_atoms(category)",
    "CREATE INDEX IF NOT EXISTS idx_mem_user     ON memory_atoms(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_mem_created  ON memory_atoms(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_mem_active   ON memory_atoms(is_active)",
]


def _get_conn(data_dir: str) -> sqlite3.Connection:
    path = Path(data_dir) / "knowledge_base.db"
    # MEM-502：账号数据目录可能尚未创建（enabled 账号未启动 / 新建账号），
    # 自动创建父目录以匹配 SQLite 自动创建 db 文件的行为，避免 500 错误。
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


# PRD 5.2：建表幂等缓存，避免每次请求都 executescript
_schema_initialized: set = set()


def _ensure_schema(conn: sqlite3.Connection, data_dir: str = ""):
    """建表 + 索引（幂等，首次后跳过）"""
    key = data_dir or str(conn)
    if key in _schema_initialized:
        return
    conn.executescript(SCHEMA_SQL)
    for idx_sql in INDEXES_SQL:
        try:
            conn.execute(idx_sql)
        except Exception:
            pass
    conn.commit()
    _schema_initialized.add(key)


def _now_ts() -> float:
    return time.time()


# ═══════════════════════════════════════════════════════
#  PRD-V5 §9.2 MEM-502：DTO 统一 & 根级 API 废弃
# ═══════════════════════════════════════════════════════

# Sunset 日期（一个过渡版本后移除根级 /api/memory/* 读端点）
_ROOT_SUNSET_DATE = "Sat, 31 Dec 2026 23:59:59 GMT"


def _deprecation_headers() -> Dict[str, str]:
    """根级 /api/memory/* GET 端点的废弃提示头"""
    return {
        "Deprecation": "true",
        "Sunset": _ROOT_SUNSET_DATE,
        "Link": '</api/accounts/{account_id}/memory>; rel="successor-version"',
    }


def _gone_response() -> JSONResponse:
    """根级 /api/memory/* 写端点 → 410 Gone"""
    return JSONResponse(
        {
            "success": False,
            "error": {
                "code": "GONE",
                "message": "根级 /api/memory/* 写操作已废弃，请改用 /api/accounts/{account_id}/memory/*",
                "details": {"successor": "/api/accounts/{account_id}/memory/*"},
            },
        },
        status_code=410,
    )


def _map_memory_dto(row: Dict[str, Any]) -> Dict[str, Any]:
    """MEM-502：统一 DTO 字段 — category / by_category，不再混用 type / by_type

    底层 SQLite 表使用 category 字段，此函数确保响应中始终包含 category，
    并将旧字段 type（如有）映射为 category 以保持向后兼容。
    """
    if not row or not isinstance(row, dict):
        return row
    # 确保 category 字段存在
    if "category" not in row and "type" in row:
        row["category"] = row["type"]
    return row


def _map_memory_list(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量映射 memory DTO"""
    return [_map_memory_dto(dict(r)) for r in (rows or [])]


# ═══════════════════════════════════════════════════════
#  路由工厂
# ═══════════════════════════════════════════════════════

def create_memory_routes(persona_store, scheduler=None, data_dir: str = "./data", account_manager=None):
    """创建记忆相关路由

    PRD-V5 §9.2 MEM-502：
    - 根级 /api/memory/* GET 端点标记废弃，返回 Deprecation/Sunset 头，
      从配置的默认账号读取数据（若 account_manager 可用）。
    - 根级 /api/memory/* 写端点（POST/PUT/DELETE）返回 410 Gone。
    - 新实现请使用 /api/accounts/{account_id}/memory/*。

    Args:
        persona_store: PersonaStore 实例（保留参数以兼容老调用）
        scheduler: Scheduler 实例（可选；仅用于 JSON 迁移时读取 DataStore）
        data_dir: 数据目录（PRD V3 §9.2 要求显式参数，不再隐式从 scheduler.ds 推断）
        account_manager: AccountManager 实例（MEM-502：用于解析默认账号数据目录）
    """
    from starlette.routing import Route

    # 兼容旧调用：若未传 data_dir，则回退到 scheduler.ds.data_dir
    if not data_dir:
        ds = getattr(scheduler, "ds", None)
        data_dir = ds.data_dir if ds else "./data"

    def _resolve_default_data_dir() -> str:
        """MEM-502：从 account_manager 解析默认账号数据目录，回退到 data_dir"""
        if account_manager:
            default_id = account_manager.get_default_id()
            if default_id:
                dd = _resolve_account_data_dir(account_manager, default_id)
                if dd:
                    return dd
        return data_dir

    def _deprecated_json(data: Any, status_code: int = 200) -> JSONResponse:
        """返回带 Deprecation/Sunset 头的 JSON 响应"""
        return JSONResponse(
            {"success": True, "data": data},
            status_code=status_code,
            headers=_deprecation_headers(),
        )

    # ── 固定路由（必须在 /api/memory/{id} 前）──

    async def search_memories(request: Request) -> JSONResponse:
        # MEM-502：根级写端点 → 410 Gone
        return _gone_response()

    async def migrate_json_to_sqlite(request: Request) -> JSONResponse:
        # MEM-502：根级写端点 → 410 Gone
        return _gone_response()

    async def get_memory_stats(request: Request) -> JSONResponse:
        try:
            dd = _resolve_default_data_dir()
            conn = _get_conn(dd)
            _ensure_schema(conn, dd)
            conn.row_factory = sqlite3.Row

            total = conn.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE is_active = 1"
            ).fetchone()[0]

            by_cat = dict(conn.execute(
                "SELECT category, COUNT(*) FROM memory_atoms WHERE is_active = 1 GROUP BY category"
            ).fetchall())

            recent_24h = conn.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE is_active = 1 AND created_at > ?",
                (_now_ts() - 86400,),
            ).fetchone()[0]

            # 图谱统计：节点数 = 记忆数 + 独立用户数 + 类别数，边数 ≈ 记忆数 × 平均连接数
            unique_users = conn.execute(
                "SELECT COUNT(DISTINCT username) FROM memory_atoms WHERE is_active = 1 AND username IS NOT NULL AND username != ''"
            ).fetchone()[0]
            graph_nodes = total + unique_users + len(by_cat)
            graph_edges = conn.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE is_active = 1 AND username IS NOT NULL AND username != ''"
            ).fetchone()[0] + total  # mentions_user + categorized_as

            # 会话统计
            sessions = dict(conn.execute(
                "SELECT session_id, COUNT(*) FROM memory_atoms WHERE is_active = 1 AND session_id IS NOT NULL AND session_id != '' GROUP BY session_id"
            ).fetchall())

            conn.close()
            return _deprecated_json({
                "total": total,
                "by_category": by_cat,
                "recent_24h": recent_24h,
                "graph_nodes": graph_nodes,
                "graph_edges": graph_edges,
                "sessions": sessions,
                "account_id": account_manager.get_default_id() if account_manager else "",
            })
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    # ── /api/memory ──

    async def list_memories(request: Request) -> JSONResponse:
        try:
            dd = _resolve_default_data_dir()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
            # MEM-502：统一接受 category（也接受旧 type 参数以向后兼容）
            category = request.query_params.get("category", "") or request.query_params.get("type", "")
            user_id = request.query_params.get("user_id", "")
            keyword = request.query_params.get("keyword", "")
            active = request.query_params.get("active", "1")

            conn = _get_conn(dd)
            _ensure_schema(conn, dd)
            conn.row_factory = sqlite3.Row

            sql = "SELECT * FROM memory_atoms WHERE 1=1"
            params = []

            if category:
                sql += " AND category = ?"
                params.append(category)
            if user_id:
                sql += " AND user_id = ?"
                params.append(user_id)
            if keyword:
                sql += " AND content LIKE ?"
                params.append(f"%{keyword}%")
            if active in ("0", "false"):
                sql += " AND is_active = 0"
            else:
                sql += " AND is_active = 1"

            total_row = conn.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE 1=1"
                + (" AND category = ?" if category else "")
                + (" AND user_id = ?" if user_id else "")
                + (" AND content LIKE ?" if keyword else "")
                + (" AND is_active = 0" if active in ("0", "false") else " AND is_active = 1"),
                params,
            ).fetchone()
            total = total_row[0] if total_row else 0

            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([page_size, (page - 1) * page_size])

            rows = conn.execute(sql, params).fetchall()
            conn.close()

            return _deprecated_json({
                "items": _map_memory_list([dict(r) for r in rows]),
                "page": page,
                "page_size": page_size,
                "total": total,
            })
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    async def get_memory(request: Request) -> JSONResponse:
        try:
            mem_id = request.path_params.get("id")
            dd = _resolve_default_data_dir()
            conn = _get_conn(dd)
            _ensure_schema(conn, dd)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM memory_atoms WHERE id = ?", (mem_id,)
            ).fetchone()
            conn.close()
            if not row:
                return JSONResponse({
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": "记忆不存在", "details": {"id": mem_id}},
                }, status_code=404)
            return _deprecated_json(_map_memory_dto(dict(row)))
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    async def delete_memory(request: Request) -> JSONResponse:
        # MEM-502：根级写端点 → 410 Gone
        return _gone_response()

    # ═══════════════════════════════════════════════════════
    #  图谱可视化 API
    # ═══════════════════════════════════════════════════════

    def _build_graph_snapshot(rows: List[sqlite3.Row]) -> Dict[str, Any]:
        """从 memory_atoms 行构建图谱快照（节点 + 边 + 记忆 + 条目）

        节点类型：
        - summary: 每条记忆本身
        - person:  从 username 字段提取的用户节点
        - topic:   从 category 字段提取的类别节点
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        memories: List[Dict[str, Any]] = []
        entries: List[Dict[str, Any]] = []

        # 实体节点 ID 用负数避免与记忆 ID 冲突
        person_id_map: Dict[str, int] = {}  # username → node_id
        topic_id_map: Dict[str, int] = {}   # category → node_id
        _next_person_id = -1
        _next_topic_id = -100000

        node_type_breakdown: Dict[str, int] = {"summary": 0, "person": 0, "topic": 0}
        relation_breakdown: Dict[str, int] = {}

        edge_counter = 0

        def _add_edge(src: int, tgt: int, relation: str, mem_id: int):
            nonlocal edge_counter
            edge_counter += 1
            edges.append({
                "id": edge_counter,
                "source": src,
                "target": tgt,
                "relation_type": relation,
                "memory_id": mem_id,
                "weight": 1,
                "confidence": 0.9,
            })
            relation_breakdown[relation] = relation_breakdown.get(relation, 0) + 1

        for row in rows:
            mem_id = int(row["id"])
            content = str(row["content"] or "")
            category = str(row["category"] or "episodic")
            username = str(row["username"] or "") if row["username"] else ""
            session_id = str(row["session_id"] or "") if row["session_id"] else ""

            # 记忆节点
            nodes.append({
                "id": mem_id,
                "type": "summary",
                "label": content[:40] + ("…" if len(content) > 40 else ""),
                "weight": float(row["importance_score"] or 0.5),
                "memory_count": 1,
                "degree": 0,
                "entry_count": 1,
            })
            node_type_breakdown["summary"] += 1

            memories.append({
                "memory_id": mem_id,
                "summary": content[:80],
                "content": content,
                "memory_type": category,
                "category": category,
                "importance": float(row["importance_score"] or 0.5),
                "status": "active" if row["is_active"] else "archived",
                "session_id": session_id,
            })

            entry_node_ids = [mem_id]

            # 用户节点
            if username:
                if username not in person_id_map:
                    person_id_map[username] = _next_person_id
                    _next_person_id -= 1
                    pid = person_id_map[username]
                    nodes.append({
                        "id": pid,
                        "type": "person",
                        "label": username,
                        "weight": 1,
                        "memory_count": 0,
                        "degree": 0,
                        "entry_count": 0,
                    })
                    node_type_breakdown["person"] += 1
                pid = person_id_map[username]
                _add_edge(mem_id, pid, "mentions_user", mem_id)
                entry_node_ids.append(pid)

            # 类别节点
            if category:
                if category not in topic_id_map:
                    topic_id_map[category] = _next_topic_id
                    _next_topic_id -= 1
                    tid = topic_id_map[category]
                    nodes.append({
                        "id": tid,
                        "type": "topic",
                        "label": category,
                        "weight": 1,
                        "memory_count": 0,
                        "degree": 0,
                        "entry_count": 0,
                    })
                    node_type_breakdown["topic"] += 1
                tid = topic_id_map[category]
                _add_edge(mem_id, tid, "categorized_as", mem_id)
                entry_node_ids.append(tid)

            entries.append({
                "memory_id": mem_id,
                "node_ids": entry_node_ids,
            })

        # 更新节点 degree
        node_degree: Dict[int, int] = {}
        for edge in edges:
            node_degree[edge["source"]] = node_degree.get(edge["source"], 0) + 1
            node_degree[edge["target"]] = node_degree.get(edge["target"], 0) + 1
        for node in nodes:
            node["degree"] = node_degree.get(node["id"], 0)

        return {
            "snapshot": {
                "nodes": nodes,
                "edges": edges,
                "memories": memories,
                "entries": entries,
            },
            "summary": {
                "node_type_breakdown": node_type_breakdown,
                "relation_breakdown": relation_breakdown,
            },
            "total_memories": len(memories),
            "graph_nodes": len(nodes),
            "graph_edges": len(edges),
        }

    async def get_memory_graph(request: Request) -> JSONResponse:
        """GET /api/memory/graph — 图谱总览（废弃，从默认账号读取）"""
        try:
            dd = _resolve_default_data_dir()
            session_id = request.query_params.get("session_id", "")
            limit = min(int(request.query_params.get("limit", 200)), 500)

            conn = _get_conn(dd)
            _ensure_schema(conn, dd)
            conn.row_factory = sqlite3.Row

            sql = "SELECT * FROM memory_atoms WHERE is_active = 1"
            params: list = []
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            conn.close()

            graph = _build_graph_snapshot(rows)
            graph["enabled"] = True
            graph["mode"] = "overview"
            graph["sessions"] = {}

            return _deprecated_json(graph)
        except Exception as e:
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL_ERROR", "message": str(e), "details": {}},
            }, status_code=500)

    async def query_memory_graph(request: Request) -> JSONResponse:
        """POST /api/memory/graph/query — MEM-502：根级写端点 → 410 Gone"""
        return _gone_response()

    # ── 路由列表（固定路由在前）──
    # PRD-V5 §9.2 MEM-502：旧路由 /api/memory/* 标记为废弃，
    # 新实现请使用 /api/accounts/{account_id}/memory/* 系列端点
    return [
        Route("/api/memory/search", search_memories, methods=["POST"]),
        Route("/api/memory/migrate", migrate_json_to_sqlite, methods=["POST"]),
        Route("/api/memory/stats", get_memory_stats, methods=["GET"]),
        Route("/api/memory/graph", get_memory_graph, methods=["GET"]),
        Route("/api/memory/graph/query", query_memory_graph, methods=["POST"]),
        Route("/api/memory", list_memories, methods=["GET"]),
        Route("/api/memory/{id}", get_memory, methods=["GET"]),
        Route("/api/memory/{id}", delete_memory, methods=["DELETE"]),
    ]


# ═══════════════════════════════════════════════════════
#  PRD V4 MEM-009：账号化记忆 API
#  /api/accounts/{account_id}/memory/*
#  每个请求从 account_manager 解析账号数据目录，
#  确保页面看到的统计来自 data/accounts/{account_id}
# ═══════════════════════════════════════════════════════

def _resolve_account_data_dir(account_manager, account_id: str) -> Optional[str]:
    """MEM-502：从 account_manager 解析账号数据目录

    解析顺序：
    1. 运行时实例（enabled 账号）→ account_data_dir
    2. 配置注册表（disabled / init-failed 账号）→ {data_root}/accounts/{account_id}
       不要求运行实例存在，admin 可只读访问禁用账号记忆数据。
    """
    if not account_manager:
        return None
    # 1. 运行时实例（enabled 账号）
    acc = account_manager.get_account(account_id)
    if acc:
        data_dir = getattr(acc, "account_data_dir", None)
        if data_dir:
            return data_dir
        ds = getattr(acc, "data_store", None)
        if ds:
            data_dir = getattr(ds, "data_dir", None)
            if data_dir:
                return data_dir
    # 2. 配置注册表（disabled / init-failed 账号）— 不要求运行实例
    if account_manager.has_account(account_id):
        data_root = getattr(account_manager, "data_root", "./data")
        return os.path.join(str(data_root), "accounts", account_id)
    return None


def _is_account_disabled(account_manager, account_id: str) -> bool:
    """MEM-502：判断账号是否为禁用状态（无运行时实例但存在于配置注册表）

    Returns:
        True 如果账号在配置中存在但没有运行时实例（disabled / init-failed）
    """
    if not account_manager:
        return False
    acc = account_manager.get_account(account_id)
    if acc:
        return False  # 运行时实例存在 → enabled
    return account_manager.has_account(account_id)


def create_account_memory_routes(account_manager):
    """创建账号化记忆路由

    PRD-V5 §9.2 MEM-502：
    - GET    /api/accounts/{id}/memory/stats
    - POST   /api/accounts/{id}/memory/search
    - GET    /api/accounts/{id}/memory          (列表)
    - GET    /api/accounts/{id}/memory/{mem_id}
    - DELETE /api/accounts/{id}/memory/{mem_id}
    - POST   /api/accounts/{id}/memory/migrate
    - GET    /api/accounts/{id}/memory/graph     (图谱总览)
    - POST   /api/accounts/{id}/memory/graph/query (关键词搜索子图)

    禁用账号（无运行时实例但存在于配置注册表）：
    - GET（只读）允许访问，数据目录从 AccountConfigRegistry 解析
    - POST/PUT/DELETE（写操作）拒绝，返回 403
    """
    from starlette.routing import Route
    from .responses import ok, fail, fail_internal

    async def _resolve_or_fail(request: Request):
        """解析账号数据目录，失败时返回 JSONResponse

        Returns:
            (data_dir, error_response) — 二者之一为 None
        """
        acc_id = request.path_params.get("account_id", "")
        if not account_manager or not account_manager.has_account(acc_id):
            return None, fail("NOT_FOUND", f"账号不存在: {acc_id}", status_code=404)
        data_dir = _resolve_account_data_dir(account_manager, acc_id)
        if not data_dir:
            return None, fail("NOT_FOUND", f"账号数据目录未初始化: {acc_id}", status_code=404)
        return data_dir, None

    def _reject_if_disabled(request: Request):
        """禁用账号写操作拒绝。返回 JSONResponse 或 None"""
        acc_id = request.path_params.get("account_id", "")
        if _is_account_disabled(account_manager, acc_id):
            return fail(
                "FORBIDDEN",
                f"账号 {acc_id} 已禁用，只支持只读访问（GET），写操作请先启用账号",
                status_code=403,
            )
        return None

    async def account_memory_stats(request: Request) -> JSONResponse:
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE is_active = 1").fetchone()[0]
            by_cat = dict(conn.execute(
                "SELECT category, COUNT(*) FROM memory_atoms WHERE is_active = 1 GROUP BY category"
            ).fetchall())
            recent_24h = conn.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE is_active = 1 AND created_at > ?",
                (_now_ts() - 86400,),
            ).fetchone()[0]
            # 图谱统计
            unique_users = conn.execute(
                "SELECT COUNT(DISTINCT username) FROM memory_atoms WHERE is_active = 1 AND username IS NOT NULL AND username != ''"
            ).fetchone()[0]
            graph_nodes = total + unique_users + len(by_cat)
            graph_edges = conn.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE is_active = 1 AND username IS NOT NULL AND username != ''"
            ).fetchone()[0] + total
            sessions = dict(conn.execute(
                "SELECT session_id, COUNT(*) FROM memory_atoms WHERE is_active = 1 AND session_id IS NOT NULL AND session_id != '' GROUP BY session_id"
            ).fetchall())
            conn.close()
            return ok({
                "total": total,
                "by_category": by_cat,
                "recent_24h": recent_24h,
                "graph_nodes": graph_nodes,
                "graph_edges": graph_edges,
                "sessions": sessions,
                "account_id": request.path_params.get("account_id", ""),
            })
        except Exception as e:
            return fail_internal(str(e))

    async def account_memory_search(request: Request) -> JSONResponse:
        # POST — 禁用账号写操作拒绝（search 是 POST 读操作，但遵循 POST→写拒绝规则）
        disabled_err = _reject_if_disabled(request)
        if disabled_err:
            return disabled_err
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            body = await request.json()
            keyword = body.get("keyword", "")
            limit = int(body.get("limit", 20))
            # MEM-502：统一接受 category（也接受旧 type 参数）
            category = body.get("category", "") or body.get("type", "")
            user_id = body.get("user_id", "")
            if not keyword:
                return ok([])
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM memory_atoms WHERE content LIKE ? AND is_active = 1"
            params: list = [f"%{keyword}%"]
            if category:
                sql += " AND category = ?"
                params.append(category)
            if user_id:
                sql += " AND user_id = ?"
                params.append(str(user_id))
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return ok(_map_memory_list([dict(r) for r in rows]))
        except Exception as e:
            return fail_internal(str(e))

    async def account_memory_list(request: Request) -> JSONResponse:
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
            # MEM-502：统一接受 category（也接受旧 type 参数）
            category = request.query_params.get("category", "") or request.query_params.get("type", "")
            user_id = request.query_params.get("user_id", "")
            keyword = request.query_params.get("keyword", "")
            active = request.query_params.get("active", "1")
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM memory_atoms WHERE 1=1"
            params: list = []
            if category:
                sql += " AND category = ?"
                params.append(category)
            if user_id:
                sql += " AND user_id = ?"
                params.append(str(user_id))
            if keyword:
                sql += " AND content LIKE ?"
                params.append(f"%{keyword}%")
            if active in ("0", "false"):
                sql += " AND is_active = 0"
            else:
                sql += " AND is_active = 1"
            # total
            count_sql = "SELECT COUNT(*) FROM memory_atoms WHERE 1=1"
            count_params: list = []
            if category:
                count_sql += " AND category = ?"
                count_params.append(category)
            if user_id:
                count_sql += " AND user_id = ?"
                count_params.append(str(user_id))
            if keyword:
                count_sql += " AND content LIKE ?"
                count_params.append(f"%{keyword}%")
            if active in ("0", "false"):
                count_sql += " AND is_active = 0"
            else:
                count_sql += " AND is_active = 1"
            total = conn.execute(count_sql, count_params).fetchone()[0]
            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([page_size, (page - 1) * page_size])
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            return ok({
                "items": _map_memory_list([dict(r) for r in rows]),
                "page": page,
                "page_size": page_size,
                "total": total,
            })
        except Exception as e:
            return fail_internal(str(e))

    async def account_memory_get(request: Request) -> JSONResponse:
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            mem_id = request.path_params.get("mem_id")
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM memory_atoms WHERE id = ?", (mem_id,)).fetchone()
            conn.close()
            if not row:
                return fail("NOT_FOUND", "记忆不存在", status_code=404)
            return ok(_map_memory_dto(dict(row)))
        except Exception as e:
            return fail_internal(str(e))

    async def account_memory_delete(request: Request) -> JSONResponse:
        # DELETE — 禁用账号写操作拒绝
        disabled_err = _reject_if_disabled(request)
        if disabled_err:
            return disabled_err
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            mem_id = request.path_params.get("mem_id")
            conn = _get_conn(data_dir)
            conn.execute("UPDATE memory_atoms SET is_active = 0 WHERE id = ?", (mem_id,))
            conn.commit()
            conn.close()
            return ok(message="记忆已删除")
        except Exception as e:
            return fail_internal(str(e))

    async def account_memory_migrate(request: Request) -> JSONResponse:
        # POST — 禁用账号写操作拒绝
        disabled_err = _reject_if_disabled(request)
        if disabled_err:
            return disabled_err
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            acc_id = request.path_params.get("account_id", "")
            acc = account_manager.get_account(acc_id)
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            existing = conn.execute("SELECT COUNT(*) FROM memory_atoms").fetchone()[0]
            if existing > 0:
                conn.close()
                return ok({"skipped": True, "existing_count": existing}, "already migrated, skip")
            ds = getattr(acc, "data_store", None)
            migrated = 0
            sources = {
                "memory.json": ("episodic", "text"),
                "permanent_memory.json": ("factual", "text"),
                "watch_log.json": ("episodic", "title"),
                "dynamic_log.json": ("episodic", "text"),
                "weekly_summary.json": ("episodic", "summary"),
            }
            for filename, (category, content_key) in sources.items():
                if not ds:
                    continue
                try:
                    raw = ds.load_json(filename, [])
                    if not isinstance(raw, list):
                        continue
                    for item in raw:
                        try:
                            content_val = str(item.get(content_key, item.get("text", "")))[:1000]
                            now = _now_ts()
                            meta = {"source_file": filename, "migrated": True}
                            conn.execute(
                                "INSERT INTO memory_atoms (category, content, metadata, created_at, last_accessed, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                                (category, content_val, json.dumps(meta, ensure_ascii=False, default=str), now, now),
                            )
                            rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                            try:
                                conn.execute("INSERT INTO memory_fts(rowid, content) VALUES (?, ?)", (rowid, content_val[:2000]))
                            except Exception:
                                pass
                            migrated += 1
                        except Exception:
                            pass
                except Exception:
                    pass
            conn.commit()
            conn.close()
            return ok({"migrated": migrated}, f"migrated {migrated} records")
        except Exception as e:
            return fail_internal(str(e))

    # ═══════════════════════════════════════════════════════
    #  MEM-502：账号化图谱 API
    # ═══════════════════════════════════════════════════════

    async def account_memory_graph(request: Request) -> JSONResponse:
        """GET /api/accounts/{id}/memory/graph — 账号图谱总览"""
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            session_id = request.query_params.get("session_id", "")
            limit = min(int(request.query_params.get("limit", 200)), 500)
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM memory_atoms WHERE is_active = 1"
            params: list = []
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            graph = _build_graph_snapshot_shared(rows)
            graph["enabled"] = True
            graph["mode"] = "overview"
            graph["sessions"] = {}
            graph["account_id"] = request.path_params.get("account_id", "")
            return ok(graph)
        except Exception as e:
            return fail_internal(str(e))

    async def account_memory_graph_query(request: Request) -> JSONResponse:
        """POST /api/accounts/{id}/memory/graph/query — 账号关键词搜索子图"""
        # POST — 禁用账号写操作拒绝（graph query 是 POST 读操作，但遵循 POST→写拒绝规则）
        disabled_err = _reject_if_disabled(request)
        if disabled_err:
            return disabled_err
        data_dir, err = await _resolve_or_fail(request)
        if err:
            return err
        try:
            body = await request.json()
            keyword = str(body.get("query", "")).strip()
            memory_id = body.get("memory_id")
            session_id = str(body.get("session_id", "")).strip()
            limit = min(int(body.get("limit", 100)), 300)
            conn = _get_conn(data_dir)
            _ensure_schema(conn, data_dir)
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM memory_atoms WHERE is_active = 1"
            params: list = []
            if memory_id:
                sql += " AND id = ?"
                params.append(int(memory_id))
            elif keyword:
                sql += " AND content LIKE ?"
                params.append(f"%{keyword}%")
            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            conn.close()
            graph = _build_graph_snapshot_shared(rows)
            graph["enabled"] = True
            graph["mode"] = "query"
            graph["sessions"] = {}
            graph["account_id"] = request.path_params.get("account_id", "")
            if memory_id and rows:
                graph["matched_node_ids"] = [int(rows[0]["id"])]
            elif keyword:
                graph["matched_node_ids"] = [int(r["id"]) for r in rows[:10]]
            else:
                graph["matched_node_ids"] = []
            return ok(graph)
        except Exception as e:
            return fail_internal(str(e))

    return [
        Route("/api/accounts/{account_id}/memory/stats", account_memory_stats, methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/search", account_memory_search, methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/migrate", account_memory_migrate, methods=["POST"]),
        Route("/api/accounts/{account_id}/memory/graph", account_memory_graph, methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/graph/query", account_memory_graph_query, methods=["POST"]),
        Route("/api/accounts/{account_id}/memory", account_memory_list, methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/{mem_id}", account_memory_get, methods=["GET"]),
        Route("/api/accounts/{account_id}/memory/{mem_id}", account_memory_delete, methods=["DELETE"]),
    ]


def _build_graph_snapshot_shared(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    """MEM-502：共享图谱快照构建（供根级和账号级路由复用）

    节点类型：
    - summary: 每条记忆本身
    - person:  从 username 字段提取的用户节点
    - topic:   从 category 字段提取的类别节点
    """
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    memories: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []

    person_id_map: Dict[str, int] = {}
    topic_id_map: Dict[str, int] = {}
    _next_person_id = -1
    _next_topic_id = -100000

    node_type_breakdown: Dict[str, int] = {"summary": 0, "person": 0, "topic": 0}
    relation_breakdown: Dict[str, int] = {}
    edge_counter = 0

    def _add_edge(src: int, tgt: int, relation: str, mem_id: int):
        nonlocal edge_counter
        edge_counter += 1
        edges.append({
            "id": edge_counter,
            "source": src,
            "target": tgt,
            "relation_type": relation,
            "memory_id": mem_id,
            "weight": 1,
            "confidence": 0.9,
        })
        relation_breakdown[relation] = relation_breakdown.get(relation, 0) + 1

    for row in rows:
        mem_id = int(row["id"])
        content = str(row["content"] or "")
        category = str(row["category"] or "episodic")
        username = str(row["username"] or "") if row["username"] else ""
        session_id = str(row["session_id"] or "") if row["session_id"] else ""

        nodes.append({
            "id": mem_id,
            "type": "summary",
            "label": content[:40] + ("…" if len(content) > 40 else ""),
            "weight": float(row["importance_score"] or 0.5),
            "memory_count": 1,
            "degree": 0,
            "entry_count": 1,
        })
        node_type_breakdown["summary"] += 1

        memories.append({
            "memory_id": mem_id,
            "summary": content[:80],
            "content": content,
            "memory_type": category,
            "category": category,
            "importance": float(row["importance_score"] or 0.5),
            "status": "active" if row["is_active"] else "archived",
            "session_id": session_id,
        })

        entry_node_ids = [mem_id]

        if username:
            if username not in person_id_map:
                person_id_map[username] = _next_person_id
                _next_person_id -= 1
                pid = person_id_map[username]
                nodes.append({
                    "id": pid,
                    "type": "person",
                    "label": username,
                    "weight": 1,
                    "memory_count": 0,
                    "degree": 0,
                    "entry_count": 0,
                })
                node_type_breakdown["person"] += 1
            pid = person_id_map[username]
            _add_edge(mem_id, pid, "mentions_user", mem_id)
            entry_node_ids.append(pid)

        if category:
            if category not in topic_id_map:
                topic_id_map[category] = _next_topic_id
                _next_topic_id -= 1
                tid = topic_id_map[category]
                nodes.append({
                    "id": tid,
                    "type": "topic",
                    "label": category,
                    "weight": 1,
                    "memory_count": 0,
                    "degree": 0,
                    "entry_count": 0,
                })
                node_type_breakdown["topic"] += 1
            tid = topic_id_map[category]
            _add_edge(mem_id, tid, "categorized_as", mem_id)
            entry_node_ids.append(tid)

        entries.append({
            "memory_id": mem_id,
            "node_ids": entry_node_ids,
        })

    node_degree: Dict[int, int] = {}
    for edge in edges:
        node_degree[edge["source"]] = node_degree.get(edge["source"], 0) + 1
        node_degree[edge["target"]] = node_degree.get(edge["target"], 0) + 1
    for node in nodes:
        node["degree"] = node_degree.get(node["id"], 0)

    return {
        "snapshot": {
            "nodes": nodes,
            "edges": edges,
            "memories": memories,
            "entries": entries,
        },
        "summary": {
            "node_type_breakdown": node_type_breakdown,
            "relation_breakdown": relation_breakdown,
        },
        "total_memories": len(memories),
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
    }
