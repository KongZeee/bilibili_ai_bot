"""
tests/test_memory_api.py - 记忆 API 测试

PRD V3 §5.6 必需测试文件。
覆盖：
- 空 data_dir 下 /api/memory 返回 200
- 空 data_dir 下 /api/memory/stats 返回 200
- 插入 memory_atoms 后列表可见
- /api/memory/stats 路由不被 /{id} 捕获
- DELETE 后 is_active=0
- JSON 迁移幂等
- category 筛选可用
"""
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from bilibot.api.memory import create_memory_routes, _get_conn, _ensure_schema


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def mem_client(tmp_data_dir):
    """构造只含 memory 路由的 TestClient"""
    routes = create_memory_routes(
        persona_store=None,
        scheduler=None,
        data_dir=tmp_data_dir,
    )
    app = Starlette(routes=routes)
    return TestClient(app)


def _insert_atom(data_dir, content, category="episodic", metadata=None):
    """直接插入一条 memory_atom，返回 id"""
    conn = _get_conn(data_dir)
    _ensure_schema(conn, data_dir)
    now = time.time()
    import json
    cur = conn.execute(
        "INSERT INTO memory_atoms "
        "(content, category, metadata, created_at, last_accessed, is_active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (content, category, json.dumps(metadata or {}), now, now),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


# ═══════════════════════════════════════════════════════
#  空库
# ═══════════════════════════════════════════════════════

class TestEmptyDatabase:
    """空数据库下接口必须正常返回 200"""

    def test_list_empty_returns_200(self, mem_client):
        resp = mem_client.get("/api/memory")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["items"] == []
        assert body["data"]["total"] == 0

    def test_stats_empty_returns_200(self, mem_client):
        resp = mem_client.get("/api/memory/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["total"] == 0
        assert body["data"]["by_category"] == {}
        assert body["data"]["recent_24h"] == 0


# ═══════════════════════════════════════════════════════
#  路由顺序
# ═══════════════════════════════════════════════════════

class TestRouteOrdering:
    """/api/memory/stats 不应被 /api/memory/{id} 捕获"""

    def test_stats_not_captured_by_id(self, mem_client):
        """stats 是固定路由，必须先匹配"""
        resp = mem_client.get("/api/memory/stats")
        assert resp.status_code == 200
        # 不能是 404
        body = resp.json()
        assert body["success"] is True
        # 不能落入 get_memory 把 "stats" 当 id 解析
        # （若被捕获，会返回 404 或 500）
        assert "total" in body["data"]

    def test_search_route_not_captured(self, mem_client):
        """MEM-502：/api/memory/search POST 根级写端点返回 410 Gone（不被 /{id} 捕获）"""
        resp = mem_client.post("/api/memory/search", json={"keyword": "x"})
        assert resp.status_code == 410

    def test_migrate_route_not_captured(self, mem_client):
        """MEM-502：/api/memory/migrate POST 根级写端点返回 410 Gone（不被 /{id} 捕获）"""
        resp = mem_client.post("/api/memory/migrate")
        assert resp.status_code == 410


# ═══════════════════════════════════════════════════════
#  插入后可见
# ═══════════════════════════════════════════════════════

class TestListAfterInsert:
    """插入 memory_atoms 后列表可见"""

    def test_insert_then_list(self, mem_client, tmp_data_dir):
        aid = _insert_atom(tmp_data_dir, "今天看了视频A", category="content_video")
        assert aid > 0

        resp = mem_client.get("/api/memory")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["id"] == aid
        assert items[0]["content"] == "今天看了视频A"
        assert items[0]["category"] == "content_video"
        assert items[0]["is_active"] == 1

    def test_insert_then_stats(self, mem_client, tmp_data_dir):
        _insert_atom(tmp_data_dir, "视频1", category="content_video")
        _insert_atom(tmp_data_dir, "动态1", category="bot_action")
        _insert_atom(tmp_data_dir, "周报1", category="summary")

        resp = mem_client.get("/api/memory/stats")
        body = resp.json()["data"]
        assert body["total"] == 3
        assert body["by_category"]["content_video"] == 1
        assert body["by_category"]["bot_action"] == 1
        assert body["by_category"]["summary"] == 1

    def test_get_single_by_id(self, mem_client, tmp_data_dir):
        aid = _insert_atom(tmp_data_dir, "测试记忆", category="episodic")
        resp = mem_client.get(f"/api/memory/{aid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["id"] == aid
        assert body["data"]["content"] == "测试记忆"


# ═══════════════════════════════════════════════════════
#  MEM-502：根级写端点 410 Gone
# ═══════════════════════════════════════════════════════

class TestRootWriteDeprecated:
    """MEM-502：根级 /api/memory/* 写操作返回 410 Gone"""

    def test_delete_returns_410(self, mem_client, tmp_data_dir):
        """根级 DELETE /api/memory/{id} → 410"""
        aid = _insert_atom(tmp_data_dir, "待删除", category="episodic")
        resp = mem_client.delete(f"/api/memory/{aid}")
        assert resp.status_code == 410
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "GONE"

    def test_graph_query_returns_410(self, mem_client):
        """根级 POST /api/memory/graph/query → 410"""
        resp = mem_client.post("/api/memory/graph/query", json={"query": "test"})
        assert resp.status_code == 410

    def test_root_get_has_deprecation_header(self, mem_client):
        """根级 GET /api/memory/stats 返回 Deprecation header"""
        resp = mem_client.get("/api/memory/stats")
        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "true"
        assert "sunset" in resp.headers
        assert "link" in resp.headers


# ═══════════════════════════════════════════════════════
#  category 筛选
# ═══════════════════════════════════════════════════════

class TestCategoryFilter:
    """category 筛选可用"""

    def test_filter_by_category(self, mem_client, tmp_data_dir):
        _insert_atom(tmp_data_dir, "视频A", category="content_video")
        _insert_atom(tmp_data_dir, "视频B", category="content_video")
        _insert_atom(tmp_data_dir, "动态A", category="bot_action")
        _insert_atom(tmp_data_dir, "周报A", category="summary")

        resp = mem_client.get("/api/memory?category=content_video")
        items = resp.json()["data"]["items"]
        assert len(items) == 2
        for it in items:
            assert it["category"] == "content_video"

        resp = mem_client.get("/api/memory?category=summary")
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["category"] == "summary"

    def test_filter_unknown_category(self, mem_client, tmp_data_dir):
        _insert_atom(tmp_data_dir, "x", category="episodic")
        resp = mem_client.get("/api/memory?category=nonexistent")
        items = resp.json()["data"]["items"]
        assert len(items) == 0


# ═══════════════════════════════════════════════════════
#  MEM-502：根级迁移端点 410 Gone
# ═══════════════════════════════════════════════════════

class TestRootMigrateDeprecated:
    """MEM-502：根级 POST /api/memory/migrate → 410 Gone"""

    def test_migrate_returns_410(self, mem_client):
        """根级 POST /api/memory/migrate → 410"""
        resp = mem_client.post("/api/memory/migrate")
        assert resp.status_code == 410
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "GONE"


# ═══════════════════════════════════════════════════════
#  PRD V3 §9.3 主写入 SQLite 字段
# ═══════════════════════════════════════════════════════

class TestSqliteWriteback:
    """PRD V3 §9.3 主动视频/动态/周总结主写入 SQLite"""

    def test_content_video_metadata(self, mem_client, tmp_data_dir):
        """主动视频记忆应有 kind=video_memory metadata"""
        _insert_atom(
            tmp_data_dir,
            "看过视频《标题》，UP 主 xxx",
            category="content_video",
            metadata={
                "kind": "video_memory",
                "bvid": "BV1xx",
                "title": "标题",
            },
        )
        resp = mem_client.get("/api/memory?category=content_video")
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        import json
        meta = json.loads(items[0]["metadata"])
        assert meta["kind"] == "video_memory"
        assert meta["bvid"] == "BV1xx"

    def test_dynamic_action_metadata(self, mem_client, tmp_data_dir):
        """动态记忆应为 category=bot_action"""
        _insert_atom(
            tmp_data_dir,
            "今天心情不错，发条动态",
            category="bot_action",
            metadata={"kind": "dynamic_post", "audit_id": "gen_xxx"},
        )
        resp = mem_client.get("/api/memory?category=bot_action")
        items = resp.json()["data"]["items"]
        assert len(items) == 1

    def test_weekly_summary_metadata(self, mem_client, tmp_data_dir):
        """周总结记忆应为 category=summary"""
        _insert_atom(
            tmp_data_dir,
            "本周总结：看了 10 个视频，发了 3 条动态",
            category="summary",
            metadata={"kind": "weekly_summary", "week": "2026-W27"},
        )
        resp = mem_client.get("/api/memory?category=summary")
        items = resp.json()["data"]["items"]
        assert len(items) == 1
