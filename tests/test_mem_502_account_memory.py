"""
tests/test_mem_502_account_memory.py - MEM-502 账号化前端与 DTO 测试

PRD-V5 §9.2 MEM-502：账号化前端与 DTO

覆盖：
- 账号化 GET /api/accounts/{id}/memory/stats 返回该账号统计
- 账号化 GET /api/accounts/{id}/memory 列表返回 category 字段（非 type）
- 账号化 DELETE 软删除
- 账号化 POST search 搜索
- 账号化 GET graph 图谱总览
- 账号化 POST graph/query 关键词子图
- 根级 GET 返回 Deprecation/Sunset header + 默认账号数据
- 根级 POST/DELETE 写操作返回 410 Gone
- 禁用账号 GET 只读可用，POST/DELETE 拒绝 403
- 不存在账号返回 404
- DTO 统一使用 category / by_category
"""
import json
import os
import sqlite3
import time
from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from bilibot.account.config_registry import AccountConfigRegistry
from bilibot.account.manager import AccountManager
from bilibot.api.memory import (
    create_memory_routes,
    create_account_memory_routes,
    _get_conn,
    _ensure_schema,
)


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def mock_orchestrator():
    return MagicMock()


@pytest.fixture
def mock_context_builder():
    return MagicMock()


@pytest.fixture
def mock_llm_manager():
    mgr = MagicMock()
    mgr.get_default.return_value = None
    mgr.get_provider.return_value = None
    mgr.resolve_provider.return_value = (None, "", "")
    return mgr


@pytest.fixture
def mem_accounts_config(tmp_data_dir):
    """配置含 2 个账号：enabled / disabled"""
    from bilibot.app.config_loader import ConfigLoader
    return ConfigLoader(config_dict={
        "accounts": [
            {
                "id": "main_acc",
                "name": "主账号",
                "sessdata": "main-sessdata",
                "bili_jct": "main-jct",
                "dede_user_id": "10001",
                "buvid3": "main-buvid3",
                "refresh_token": "main-refresh",
                "profile_id": "",
                "persona_id": "default",
                "llm_id": "",
                "enabled": True,
            },
            {
                "id": "disabled_acc",
                "name": "已禁用账号",
                "sessdata": "disabled-sessdata",
                "bili_jct": "disabled-jct",
                "dede_user_id": "20002",
                "buvid3": "disabled-buvid3",
                "refresh_token": "disabled-refresh",
                "profile_id": "",
                "persona_id": "default",
                "llm_id": "",
                "enabled": False,
            },
        ],
        "default_account": "main_acc",
        "data_dir": tmp_data_dir,
    })


@pytest.fixture
def account_manager(mem_accounts_config, tmp_data_dir, mock_orchestrator, mock_context_builder, mock_llm_manager):
    """构造 AccountManager（含 enabled / disabled 两个账号）"""
    from bilibot.services.persona_store import PersonaStore
    from bilibot.services.audit_store import AuditStore
    persona_store = PersonaStore(data_dir=tmp_data_dir)
    audit_store = AuditStore(data_dir=tmp_data_dir)
    mgr = AccountManager(
        persona_store=persona_store,
        llm_manager=mock_llm_manager,
        audit_store=audit_store,
        orchestrator=mock_orchestrator,
        context_builder=mock_context_builder,
        app_config_loader=mem_accounts_config,
        data_root=tmp_data_dir,
        safety_checker=None,
    )
    mgr.initialize()
    return mgr


@pytest.fixture
def api_client(account_manager, mem_accounts_config, tmp_data_dir):
    """构造含根级 + 账号化路由的 TestClient"""
    root_routes = create_memory_routes(
        persona_store=None,
        scheduler=None,
        data_dir=tmp_data_dir,
        account_manager=account_manager,
    )
    acc_routes = create_account_memory_routes(account_manager)
    app = Starlette(routes=root_routes + acc_routes)
    return TestClient(app)


def _insert_atom(data_dir, content, category="episodic", metadata=None, username="", session_id=""):
    """直接插入一条 memory_atom，返回 id"""
    # 确保目录存在（AccountManager.initialize() 不创建物理数据目录，需启动才创建）
    os.makedirs(data_dir, exist_ok=True)
    conn = _get_conn(data_dir)
    _ensure_schema(conn, data_dir)
    now = time.time()
    cur = conn.execute(
        "INSERT INTO memory_atoms "
        "(content, category, metadata, created_at, last_accessed, is_active, username, session_id) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (content, category, json.dumps(metadata or {}), now, now, username, session_id),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def _ensure_account_data_dir(data_root, account_id):
    """确保账号数据目录存在（用于禁用账号测试）"""
    acc_dir = os.path.join(data_root, "accounts", account_id)
    os.makedirs(acc_dir, exist_ok=True)
    return acc_dir


# ═══════════════════════════════════════════════════════
#  账号化 GET stats
# ═══════════════════════════════════════════════════════

class TestAccountMemoryStats:
    """GET /api/accounts/{id}/memory/stats"""

    def test_stats_returns_account_data(self, api_client, account_manager, tmp_data_dir):
        """MEM-502：stats 返回该账号的统计数据"""
        # 向主账号数据目录插入记忆
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "主账号记忆1", category="episodic")
        _insert_atom(main_dir, "主账号记忆2", category="factual")

        resp = api_client.get("/api/accounts/main_acc/memory/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["total"] == 2
        assert body["data"]["by_category"]["episodic"] == 1
        assert body["data"]["by_category"]["factual"] == 1
        assert body["data"]["account_id"] == "main_acc"

    def test_stats_empty_returns_200(self, api_client):
        """空库 stats 返回 200 + 零值"""
        resp = api_client.get("/api/accounts/main_acc/memory/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total"] == 0
        assert body["data"]["by_category"] == {}

    def test_stats_has_graph_fields(self, api_client, account_manager):
        """stats 返回图谱统计字段"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "测试", category="episodic", username="user1")
        resp = api_client.get("/api/accounts/main_acc/memory/stats")
        body = resp.json()["data"]
        assert "graph_nodes" in body
        assert "graph_edges" in body
        assert "sessions" in body
        assert body["graph_nodes"] > 0


# ═══════════════════════════════════════════════════════
#  账号化列表 + DTO category
# ═══════════════════════════════════════════════════════

class TestAccountMemoryList:
    """GET /api/accounts/{id}/memory"""

    def test_list_returns_category_field(self, api_client, account_manager):
        """MEM-502：列表项包含 category 字段"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "测试记忆", category="episodic")

        resp = api_client.get("/api/accounts/main_acc/memory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        items = body["data"]["items"]
        assert len(items) == 1
        assert "category" in items[0]
        assert items[0]["category"] == "episodic"

    def test_list_filter_by_category(self, api_client, account_manager):
        """category 筛选可用"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "视频A", category="content_video")
        _insert_atom(main_dir, "动态A", category="bot_action")

        resp = api_client.get("/api/accounts/main_acc/memory?category=content_video")
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["category"] == "content_video"

    def test_list_filter_by_legacy_type_param(self, api_client, account_manager):
        """MEM-502：旧 type 查询参数也接受（向后兼容）"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "视频A", category="content_video")
        _insert_atom(main_dir, "动态A", category="bot_action")

        resp = api_client.get("/api/accounts/main_acc/memory?type=content_video")
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["category"] == "content_video"

    def test_list_total_correct(self, api_client, account_manager):
        """total 字段正确计算"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "记忆1", category="episodic")
        _insert_atom(main_dir, "记忆2", category="episodic")
        _insert_atom(main_dir, "记忆3", category="factual")

        resp = api_client.get("/api/accounts/main_acc/memory?category=episodic")
        body = resp.json()["data"]
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_list_pagination(self, api_client, account_manager):
        """分页参数生效"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        for i in range(5):
            _insert_atom(main_dir, f"记忆{i}", category="episodic")

        resp = api_client.get("/api/accounts/main_acc/memory?page=1&page_size=2")
        body = resp.json()["data"]
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["page"] == 1

        resp = api_client.get("/api/accounts/main_acc/memory?page=2&page_size=2")
        body = resp.json()["data"]
        assert len(body["items"]) == 2


# ═══════════════════════════════════════════════════════
#  账号化单条查询
# ═══════════════════════════════════════════════════════

class TestAccountMemoryGet:
    """GET /api/accounts/{id}/memory/{mem_id}"""

    def test_get_single(self, api_client, account_manager):
        main_dir = account_manager.get_account("main_acc").account_data_dir
        aid = _insert_atom(main_dir, "单条记忆", category="episodic")
        resp = api_client.get(f"/api/accounts/main_acc/memory/{aid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["id"] == aid
        assert body["data"]["content"] == "单条记忆"
        assert body["data"]["category"] == "episodic"

    def test_get_not_found(self, api_client):
        resp = api_client.get("/api/accounts/main_acc/memory/99999")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════
#  账号化 DELETE 软删除
# ═══════════════════════════════════════════════════════

class TestAccountMemoryDelete:
    """DELETE /api/accounts/{id}/memory/{mem_id}"""

    def test_delete_soft_delete(self, api_client, account_manager, tmp_data_dir):
        """DELETE 后 is_active=0"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        aid = _insert_atom(main_dir, "待删除", category="episodic")

        resp = api_client.delete(f"/api/accounts/main_acc/memory/{aid}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 验证 is_active=0
        conn = _get_conn(main_dir)
        row = conn.execute("SELECT is_active FROM memory_atoms WHERE id = ?", (aid,)).fetchone()
        conn.close()
        assert row["is_active"] == 0

    def test_deleted_not_in_default_list(self, api_client, account_manager):
        """删除后不在默认列表中"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        aid = _insert_atom(main_dir, "要删的", category="episodic")
        _insert_atom(main_dir, "保留的", category="episodic")

        api_client.delete(f"/api/accounts/main_acc/memory/{aid}")

        resp = api_client.get("/api/accounts/main_acc/memory")
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["content"] == "保留的"


# ═══════════════════════════════════════════════════════
#  账号化搜索
# ═══════════════════════════════════════════════════════

class TestAccountMemorySearch:
    """POST /api/accounts/{id}/memory/search"""

    def test_search_by_keyword(self, api_client, account_manager):
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "今天看了视频A", category="episodic")
        _insert_atom(main_dir, "今天发了动态B", category="bot_action")

        resp = api_client.post("/api/accounts/main_acc/memory/search", json={
            "keyword": "视频", "limit": 10,
        })
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert len(items) == 1
        assert "视频" in items[0]["content"]
        assert "category" in items[0]

    def test_search_empty_keyword_returns_empty(self, api_client, account_manager):
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "记忆1", category="episodic")

        resp = api_client.post("/api/accounts/main_acc/memory/search", json={
            "keyword": "", "limit": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_search_filter_by_category(self, api_client, account_manager):
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "视频A", category="content_video")
        _insert_atom(main_dir, "动态A", category="bot_action")

        resp = api_client.post("/api/accounts/main_acc/memory/search", json={
            "keyword": "A", "category": "content_video", "limit": 10,
        })
        items = resp.json()["data"]
        assert len(items) == 1
        assert items[0]["category"] == "content_video"


# ═══════════════════════════════════════════════════════
#  账号化图谱
# ═══════════════════════════════════════════════════════

class TestAccountMemoryGraph:
    """GET /api/accounts/{id}/memory/graph + POST graph/query"""

    def test_graph_overview(self, api_client, account_manager):
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "记忆1", category="episodic", username="user1")
        _insert_atom(main_dir, "记忆2", category="factual", username="user2")

        resp = api_client.get("/api/accounts/main_acc/memory/graph")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["enabled"] is True
        assert body["mode"] == "overview"
        assert body["account_id"] == "main_acc"
        assert body["total_memories"] == 2
        assert body["graph_nodes"] > 0
        snapshot = body["snapshot"]
        assert len(snapshot["nodes"]) > 0
        assert len(snapshot["memories"]) == 2

    def test_graph_query_by_keyword(self, api_client, account_manager):
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "视频A内容", category="episodic")
        _insert_atom(main_dir, "动态B内容", category="bot_action")

        resp = api_client.post("/api/accounts/main_acc/memory/graph/query", json={
            "query": "视频",
        })
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["mode"] == "query"
        assert len(body["matched_node_ids"]) > 0

    def test_graph_memories_have_category(self, api_client, account_manager):
        """MEM-502：图谱 memories 包含 category 字段"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "测试", category="episodic")

        resp = api_client.get("/api/accounts/main_acc/memory/graph")
        memories = resp.json()["data"]["snapshot"]["memories"]
        assert len(memories) == 1
        assert "category" in memories[0]
        assert memories[0]["category"] == "episodic"


# ═══════════════════════════════════════════════════════
#  根级 API 废弃
# ═══════════════════════════════════════════════════════

class TestRootDeprecation:
    """MEM-502：根级 /api/memory/* 废弃行为"""

    def test_root_stats_has_deprecation_header(self, api_client, account_manager):
        """根级 GET /api/memory/stats 返回 Deprecation/Sunset header"""
        # 先向默认账号插入数据
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "根级测试", category="episodic")

        resp = api_client.get("/api/memory/stats")
        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"
        assert "sunset" in resp.headers
        assert "link" in resp.headers

    def test_root_stats_reads_default_account(self, api_client, account_manager):
        """根级 GET stats 从默认账号读取数据"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "默认账号记忆", category="episodic")

        resp = api_client.get("/api/memory/stats")
        body = resp.json()["data"]
        assert body["total"] == 1
        assert body["account_id"] == "main_acc"

    def test_root_list_has_deprecation_header(self, api_client):
        """根级 GET /api/memory 返回 Deprecation header"""
        resp = api_client.get("/api/memory")
        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"

    def test_root_search_returns_410(self, api_client):
        """根级 POST /api/memory/search → 410"""
        resp = api_client.post("/api/memory/search", json={"keyword": "x"})
        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "GONE"

    def test_root_migrate_returns_410(self, api_client):
        """根级 POST /api/memory/migrate → 410"""
        resp = api_client.post("/api/memory/migrate")
        assert resp.status_code == 410

    def test_root_delete_returns_410(self, api_client):
        """根级 DELETE /api/memory/{id} → 410"""
        resp = api_client.delete("/api/memory/1")
        assert resp.status_code == 410

    def test_root_graph_query_returns_410(self, api_client):
        """根级 POST /api/memory/graph/query → 410"""
        resp = api_client.post("/api/memory/graph/query", json={"query": "x"})
        assert resp.status_code == 410

    def test_root_graph_get_has_deprecation(self, api_client):
        """根级 GET /api/memory/graph 返回 Deprecation header"""
        resp = api_client.get("/api/memory/graph")
        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"


# ═══════════════════════════════════════════════════════
#  禁用账号只读
# ═══════════════════════════════════════════════════════

class TestDisabledAccountReadOnly:
    """MEM-502：禁用账号记忆管理员只读"""

    def test_disabled_account_get_stats_works(self, api_client, account_manager, tmp_data_dir):
        """禁用账号 GET stats 可用（从配置注册表解析数据目录）"""
        # 手动创建禁用账号数据目录并插入记忆
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")
        _insert_atom(disabled_dir, "禁用账号记忆", category="episodic")

        resp = api_client.get("/api/accounts/disabled_acc/memory/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["total"] == 1
        assert body["data"]["account_id"] == "disabled_acc"

    def test_disabled_account_get_list_works(self, api_client, tmp_data_dir):
        """禁用账号 GET 列表可用"""
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")
        _insert_atom(disabled_dir, "只读测试", category="factual")

        resp = api_client.get("/api/accounts/disabled_acc/memory")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1

    def test_disabled_account_get_single_works(self, api_client, tmp_data_dir):
        """禁用账号 GET 单条可用"""
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")
        aid = _insert_atom(disabled_dir, "单条只读", category="episodic")

        resp = api_client.get(f"/api/accounts/disabled_acc/memory/{aid}")
        assert resp.status_code == 200
        assert resp.json()["data"]["content"] == "单条只读"

    def test_disabled_account_graph_get_works(self, api_client, tmp_data_dir):
        """禁用账号 GET 图谱可用"""
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")
        _insert_atom(disabled_dir, "图谱只读", category="episodic", username="u1")

        resp = api_client.get("/api/accounts/disabled_acc/memory/graph")
        assert resp.status_code == 200
        assert resp.json()["data"]["total_memories"] == 1

    def test_disabled_account_search_rejected(self, api_client, tmp_data_dir):
        """禁用账号 POST search → 403"""
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")
        _insert_atom(disabled_dir, "测试", category="episodic")

        resp = api_client.post("/api/accounts/disabled_acc/memory/search", json={
            "keyword": "测试",
        })
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    def test_disabled_account_delete_rejected(self, api_client, tmp_data_dir):
        """禁用账号 DELETE → 403"""
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")
        aid = _insert_atom(disabled_dir, "不能删", category="episodic")

        resp = api_client.delete(f"/api/accounts/disabled_acc/memory/{aid}")
        assert resp.status_code == 403
        # 验证记忆仍在
        conn = _get_conn(disabled_dir)
        row = conn.execute("SELECT is_active FROM memory_atoms WHERE id = ?", (aid,)).fetchone()
        conn.close()
        assert row["is_active"] == 1

    def test_disabled_account_migrate_rejected(self, api_client):
        """禁用账号 POST migrate → 403"""
        resp = api_client.post("/api/accounts/disabled_acc/memory/migrate")
        assert resp.status_code == 403

    def test_disabled_account_graph_query_rejected(self, api_client):
        """禁用账号 POST graph/query → 403"""
        resp = api_client.post("/api/accounts/disabled_acc/memory/graph/query", json={
            "query": "test",
        })
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════
#  不存在账号
# ═══════════════════════════════════════════════════════

class TestNonExistentAccount:
    """不存在账号 → 404"""

    def test_stats_404(self, api_client):
        resp = api_client.get("/api/accounts/nonexistent/memory/stats")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_list_404(self, api_client):
        resp = api_client.get("/api/accounts/nonexistent/memory")
        assert resp.status_code == 404

    def test_search_404(self, api_client):
        resp = api_client.post("/api/accounts/nonexistent/memory/search", json={"keyword": "x"})
        assert resp.status_code == 404

    def test_delete_404(self, api_client):
        resp = api_client.delete("/api/accounts/nonexistent/memory/1")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════
#  账号隔离
# ═══════════════════════════════════════════════════════

class TestAccountIsolation:
    """MEM-502：不同账号记忆互不干扰"""

    def test_stats_isolated(self, api_client, account_manager, tmp_data_dir):
        """两账号 stats 互不干扰"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")

        _insert_atom(main_dir, "主账号记忆", category="episodic")
        _insert_atom(main_dir, "主账号记忆2", category="factual")
        _insert_atom(disabled_dir, "禁用账号记忆", category="episodic")

        # 主账号 stats
        resp = api_client.get("/api/accounts/main_acc/memory/stats")
        assert resp.json()["data"]["total"] == 2

        # 禁用账号 stats
        resp = api_client.get("/api/accounts/disabled_acc/memory/stats")
        assert resp.json()["data"]["total"] == 1

    def test_list_isolated(self, api_client, account_manager, tmp_data_dir):
        """两账号列表互不干扰"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        disabled_dir = _ensure_account_data_dir(tmp_data_dir, "disabled_acc")

        _insert_atom(main_dir, "主账号专属", category="episodic")
        _insert_atom(disabled_dir, "禁用账号专属", category="episodic")

        main_items = api_client.get("/api/accounts/main_acc/memory").json()["data"]["items"]
        assert len(main_items) == 1
        assert main_items[0]["content"] == "主账号专属"

        disabled_items = api_client.get("/api/accounts/disabled_acc/memory").json()["data"]["items"]
        assert len(disabled_items) == 1
        assert disabled_items[0]["content"] == "禁用账号专属"


# ═══════════════════════════════════════════════════════
#  DTO 统一性
# ═══════════════════════════════════════════════════════

class TestDTOUnification:
    """MEM-502：DTO 统一使用 category / by_category"""

    def test_stats_uses_by_category(self, api_client, account_manager):
        """stats 返回 by_category（不是 by_type）"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "x", category="episodic")

        resp = api_client.get("/api/accounts/main_acc/memory/stats")
        data = resp.json()["data"]
        assert "by_category" in data
        assert "by_type" not in data

    def test_list_items_have_category(self, api_client, account_manager):
        """列表项有 category 字段"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "x", category="factual")

        resp = api_client.get("/api/accounts/main_acc/memory")
        item = resp.json()["data"]["items"][0]
        assert "category" in item
        assert item["category"] == "factual"

    def test_search_results_have_category(self, api_client, account_manager):
        """搜索结果有 category 字段"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "可搜索", category="episodic")

        resp = api_client.post("/api/accounts/main_acc/memory/search", json={
            "keyword": "可搜索",
        })
        item = resp.json()["data"][0]
        assert "category" in item

    def test_graph_memories_have_category(self, api_client, account_manager):
        """图谱 memories 有 category 字段"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "x", category="content_video")

        resp = api_client.get("/api/accounts/main_acc/memory/graph")
        mem = resp.json()["data"]["snapshot"]["memories"][0]
        assert "category" in mem
        assert mem["category"] == "content_video"

    def test_root_stats_uses_by_category(self, api_client, account_manager):
        """根级 stats 也使用 by_category"""
        main_dir = account_manager.get_account("main_acc").account_data_dir
        _insert_atom(main_dir, "x", category="episodic")

        resp = api_client.get("/api/memory/stats")
        data = resp.json()["data"]
        assert "by_category" in data
        assert "by_type" not in data
