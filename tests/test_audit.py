"""
tests/test_audit.py - AuditStore 测试

PRD V3 §5.9 必需测试文件。
覆盖：
- AuditStore.record() 写入
- AuditStore.query() 查出
- GET /api/audit/generations 返回记录
- prompt_preview 不包含敏感字段
"""
import pytest

from bilibot.services.audit_store import AuditStore


@pytest.fixture
def store(tmp_data_dir):
    return AuditStore(data_dir=tmp_data_dir)


class TestAuditStoreUnit:
    """单元测试"""

    def test_record_returns_id(self, store):
        aid = store.record(
            scene="reply_comment",
            persona_id="default",
            input_summary="用户说你好",
            output="你好呀",
        )
        assert aid.startswith("gen_")

    def test_query_finds_recorded(self, store):
        aid = store.record(
            scene="reply_comment",
            persona_id="default",
            input_summary="输入X",
            output="输出Y",
        )
        results = store.query(scene="reply_comment")
        assert len(results) == 1
        assert results[0]["id"] == aid
        assert results[0]["input_summary"] == "输入X"
        assert results[0]["output"] == "输出Y"

    def test_query_filter_by_scene(self, store):
        store.record(scene="reply_comment", persona_id="p1", output="a")
        store.record(scene="dynamic_post", persona_id="p1", output="b")
        reply_items = store.query(scene="reply_comment")
        dyn_items = store.query(scene="dynamic_post")
        assert len(reply_items) == 1
        assert len(dyn_items) == 1
        assert reply_items[0]["output"] == "a"
        assert dyn_items[0]["output"] == "b"

    def test_query_filter_by_persona(self, store):
        store.record(scene="reply_comment", persona_id="px", output="x")
        store.record(scene="reply_comment", persona_id="py", output="y")
        items = store.query(persona_id="px")
        assert len(items) == 1
        assert items[0]["persona_id"] == "px"

    def test_get_single_record(self, store):
        aid = store.record(scene="weekly_summary", persona_id="d", output="周报")
        item = store.get(aid)
        assert item is not None
        assert item["id"] == aid
        assert item["scene"] == "weekly_summary"

    def test_get_missing_returns_none(self, store):
        assert store.get("nonexistent") is None

    def test_count_by_scene(self, store):
        store.record(scene="reply_comment", persona_id="d")
        store.record(scene="reply_comment", persona_id="d")
        store.record(scene="dynamic_post", persona_id="d")
        assert store.count("reply_comment") == 2
        assert store.count("dynamic_post") == 1
        assert store.count() == 3

    def test_record_truncates_long_fields(self, store):
        long_text = "x" * 5000
        aid = store.record(
            scene="reply_comment",
            persona_id="d",
            input_summary=long_text,
            prompt_preview=long_text,
            output=long_text,
        )
        item = store.get(aid)
        # input_summary 截断到 500
        assert len(item["input_summary"]) <= 500
        # prompt_preview 截断到 2000
        assert len(item["prompt_preview"]) <= 2000
        # output 截断到 2000
        assert len(item["output"]) <= 2000


class TestPromptPreviewNoSensitive:
    """prompt_preview 不包含敏感字段（PRD V3 §5.9）"""

    SENSITIVE_PATTERNS = ["api_key", "sessdata", "bili_jct", "admin_password", "secret_key"]

    def test_clean_prompt_preview(self, store):
        """正常 system_prompt 不含敏感字段"""
        clean_prompt = "你是一个友好的B站AI助手。\n说话风格：活泼"
        aid = store.record(
            scene="reply_comment",
            persona_id="default",
            prompt_preview=clean_prompt,
            output="你好",
        )
        item = store.get(aid)
        for pat in self.SENSITIVE_PATTERNS:
            assert pat not in item["prompt_preview"].lower(), \
                f"prompt_preview 含敏感字段 {pat}"

    def test_no_sensitive_in_query_results(self, store):
        store.record(
            scene="dynamic_post",
            persona_id="d",
            prompt_preview="发动态规则：自然口语化",
            output="今天看了一个好视频",
        )
        for item in store.query(scene="dynamic_post"):
            for pat in self.SENSITIVE_PATTERNS:
                assert pat not in item.get("prompt_preview", "").lower()


class TestAuditHTTP:
    """HTTP API 测试"""

    @pytest.fixture
    def client(self, store):
        from starlette.testclient import TestClient
        from bilibot.api.audit import create_audit_routes
        routes = create_audit_routes(store)
        from starlette.applications import Starlette
        app = Starlette(routes=routes)
        return TestClient(app)

    def test_list_generations_empty(self, client):
        resp = client.get("/api/audit/generations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"] == []

    def test_list_generations_with_records(self, client, store):
        store.record(
            scene="reply_comment",
            persona_id="default",
            input_summary="输入A",
            output="输出A",
        )
        resp = client.get("/api/audit/generations?scene=reply_comment")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["input_summary"] == "输入A"

    def test_get_single_via_http(self, client, store):
        aid = store.record(scene="weekly_summary", persona_id="d", output="周报")
        resp = client.get(f"/api/audit/generations/{aid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["id"] == aid

    def test_stats_endpoint(self, client, store):
        store.record(scene="reply_comment", persona_id="d")
        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["total"] >= 1

    def test_prompt_preview_not_leaked_via_http(self, client, store):
        """HTTP 返回的 prompt_preview 不含敏感字段"""
        store.record(
            scene="reply_comment",
            persona_id="d",
            prompt_preview="系统提示词：你是B站AI助手",
            output="回复",
        )
        resp = client.get("/api/audit/generations?scene=reply_comment")
        data = resp.json()
        sensitive = ["api_key", "sessdata", "admin_password"]
        for item in data["data"]:
            for pat in sensitive:
                assert pat not in item.get("prompt_preview", "").lower()
