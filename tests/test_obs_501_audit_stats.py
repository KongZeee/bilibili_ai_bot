"""
tests/test_obs_501_audit_stats.py - PRD-V5 §13.1 OBS-501 审计统计语义测试

覆盖：
- stats() 返回全部 10 个语义化状态键
- 每个状态的计数准确
- 不再使用 total - published 推算 draft
- 空库返回全 0
- 混合状态正确分组统计
- /api/audit/stats 端点返回新格式
- set_status() 可独立更新状态
- mark_published() 同步更新 status 字段
"""
import pytest

from bilibot.services.audit_store import AuditStore, STATUS_VALUES


REQUIRED_STATUS_KEYS = (
    "generated",
    "awaiting_review",
    "approved",
    "rejected",
    "publishing",
    "published",
    "retry_wait",
    "result_unknown",
    "failed",
    "expired",
)


@pytest.fixture
def store(tmp_data_dir):
    return AuditStore(data_dir=tmp_data_dir)


# ═══════════════════════════════════════════════════════
#  状态键完整性
# ═══════════════════════════════════════════════════════

class TestStatsStatusKeys:
    """OBS-501：stats 必须返回全部 10 个状态键"""

    def test_stats_returns_all_10_status_keys(self, store):
        stats = store.stats()
        for key in REQUIRED_STATUS_KEYS:
            assert key in stats, f"stats 缺少状态键: {key}"

    def test_status_values_constant_matches_required(self):
        assert set(STATUS_VALUES) == set(REQUIRED_STATUS_KEYS)
        assert len(STATUS_VALUES) == 10

    def test_empty_db_returns_all_zeros(self, store):
        stats = store.stats()
        for key in REQUIRED_STATUS_KEYS:
            assert stats[key] == 0, f"空库 {key} 应为 0，实际 {stats[key]}"
        assert stats["total"] == 0


# ═══════════════════════════════════════════════════════
#  不再用 total - published 推算 draft
# ═══════════════════════════════════════════════════════

class TestNoTotalMinusPublished:
    """OBS-501：禁止 total - published 推算 draft"""

    def test_no_draft_key_in_stats(self, store):
        store.record(scene="reply_comment", persona_id="d", output="x")
        stats = store.stats()
        assert "draft" not in stats, "stats 不应再返回 draft（total - published）"

    def test_published_count_not_derived(self, store):
        """published 计数应来自 status='published'，而非旧的 published=1"""
        a1 = store.record(scene="dynamic_post", persona_id="d", output="x")
        store.mark_published(a1, published=True)
        stats = store.stats()
        # published 状态计数 = 1
        assert stats["published"] == 1
        # generated 仍为 0（a1 已转为 published）
        assert stats["generated"] == 0

    def test_total_equals_sum_of_statuses(self, store):
        """total 应等于各状态计数之和（不再有 draft 差值）"""
        a1 = store.record(scene="s1", persona_id="d", output="a")
        a2 = store.record(scene="s2", persona_id="d", output="b")
        a3 = store.record(scene="s3", persona_id="d", output="c")
        store.mark_published(a1, published=True)
        store.set_status(a2, "failed")
        store.set_status(a3, "expired")

        stats = store.stats()
        status_sum = sum(stats[k] for k in REQUIRED_STATUS_KEYS)
        assert stats["total"] == status_sum
        assert stats["total"] == 3


# ═══════════════════════════════════════════════════════
#  混合状态计数准确性
# ═══════════════════════════════════════════════════════

class TestMixedStatusCounts:
    """OBS-501：混合状态正确分组统计"""

    def test_mixed_statuses_counted_correctly(self, store):
        # generated x2
        g1 = store.record(scene="s", persona_id="d", output="g1")
        g2 = store.record(scene="s", persona_id="d", output="g2")
        # published x1（通过 mark_published 转换）
        p1 = store.record(scene="s", persona_id="d", output="p1")
        store.mark_published(p1, published=True)
        # failed x1（通过 mark_published + failure_reason）
        f1 = store.record(scene="s", persona_id="d", output="f1")
        store.mark_published(f1, published=False, failure_reason="timeout")
        # 其余状态通过 set_status 设置
        ar1 = store.record(scene="s", persona_id="d", output="ar1")
        store.set_status(ar1, "awaiting_review")

        ap1 = store.record(scene="s", persona_id="d", output="ap1")
        store.set_status(ap1, "approved")

        rj1 = store.record(scene="s", persona_id="d", output="rj1")
        store.set_status(rj1, "rejected")

        pub_ing = store.record(scene="s", persona_id="d", output="pi")
        store.set_status(pub_ing, "publishing")

        rw1 = store.record(scene="s", persona_id="d", output="rw1")
        store.set_status(rw1, "retry_wait")

        ru1 = store.record(scene="s", persona_id="d", output="ru1")
        store.set_status(ru1, "result_unknown")

        ex1 = store.record(scene="s", persona_id="d", output="ex1")
        store.set_status(ex1, "expired")

        stats = store.stats()
        assert stats["generated"] == 2
        assert stats["published"] == 1
        assert stats["failed"] == 1
        assert stats["awaiting_review"] == 1
        assert stats["approved"] == 1
        assert stats["rejected"] == 1
        assert stats["publishing"] == 1
        assert stats["retry_wait"] == 1
        assert stats["result_unknown"] == 1
        assert stats["expired"] == 1
        assert stats["total"] == 11

    def test_counts_accurate_per_status(self, store):
        """同一状态多条记录计数准确"""
        for _ in range(3):
            store.record(scene="s", persona_id="d", output="g", status="generated")
        for _ in range(2):
            aid = store.record(scene="s", persona_id="d", output="p")
            store.set_status(aid, "published")
        for _ in range(4):
            aid = store.record(scene="s", persona_id="d", output="f")
            store.set_status(aid, "failed")

        stats = store.stats()
        assert stats["generated"] == 3
        assert stats["published"] == 2
        assert stats["failed"] == 4
        # 其余状态为 0
        assert stats["awaiting_review"] == 0
        assert stats["approved"] == 0
        assert stats["rejected"] == 0
        assert stats["publishing"] == 0
        assert stats["retry_wait"] == 0
        assert stats["result_unknown"] == 0
        assert stats["expired"] == 0

    def test_record_with_explicit_status(self, store):
        """record() 可直接指定 status"""
        store.record(scene="s", persona_id="d", output="x", status="awaiting_review")
        stats = store.stats()
        assert stats["awaiting_review"] == 1
        assert stats["generated"] == 0

    def test_record_unknown_status_falls_back_to_generated(self, store):
        """record() 传入非法 status 时回退为 generated"""
        store.record(scene="s", persona_id="d", output="x", status="bogus_state")
        stats = store.stats()
        assert stats["generated"] == 1


# ═══════════════════════════════════════════════════════
#  set_status / mark_published 状态更新
# ═══════════════════════════════════════════════════════

class TestStatusUpdate:
    """OBS-501：状态更新方法"""

    def test_set_status_updates_count(self, store):
        aid = store.record(scene="s", persona_id="d", output="x")
        assert store.stats()["generated"] == 1

        assert store.set_status(aid, "approved") is True
        stats = store.stats()
        assert stats["generated"] == 0
        assert stats["approved"] == 1

    def test_set_status_invalid_returns_false(self, store):
        aid = store.record(scene="s", persona_id="d", output="x")
        assert store.set_status(aid, "bogus") is False
        # 原状态不变
        assert store.stats()["generated"] == 1

    def test_set_status_nonexistent_returns_false(self, store):
        assert store.set_status("gen_nope", "published") is False

    def test_mark_published_sets_published_status(self, store):
        aid = store.record(scene="s", persona_id="d", output="x")
        store.mark_published(aid, published=True)
        item = store.get(aid)
        assert item["status"] == "published"
        assert store.stats()["published"] == 1

    def test_mark_published_failure_sets_failed_status(self, store):
        aid = store.record(scene="s", persona_id="d", output="x")
        store.mark_published(aid, published=False, failure_reason="api error")
        item = store.get(aid)
        assert item["status"] == "failed"
        assert store.stats()["failed"] == 1


# ═══════════════════════════════════════════════════════
#  HTTP API 端点
# ═══════════════════════════════════════════════════════

class TestAuditStatsHTTP:
    """/api/audit/stats 返回新格式"""

    def test_http_stats_returns_all_status_keys(self, tmp_data_dir):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from bilibot.api.audit import create_audit_routes

        store = AuditStore(data_dir=tmp_data_dir)
        app = Starlette(routes=create_audit_routes(store))
        client = TestClient(app)

        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        data = resp.json()["data"]
        for key in REQUIRED_STATUS_KEYS:
            assert key in data, f"HTTP stats 缺少状态键: {key}"
            assert data[key] == 0

    def test_http_stats_mixed_counts(self, tmp_data_dir):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from bilibot.api.audit import create_audit_routes

        store = AuditStore(data_dir=tmp_data_dir)
        store.record(scene="s", persona_id="d", output="g1")
        store.record(scene="s", persona_id="d", output="g2")
        p1 = store.record(scene="s", persona_id="d", output="p1")
        store.mark_published(p1, published=True)
        f1 = store.record(scene="s", persona_id="d", output="f1")
        store.mark_published(f1, published=False, failure_reason="err")

        app = Starlette(routes=create_audit_routes(store))
        client = TestClient(app)
        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        stats = resp.json()["data"]
        assert stats["generated"] == 2
        assert stats["published"] == 1
        assert stats["failed"] == 1
        assert stats["total"] == 4
        assert "draft" not in stats

    def test_http_stats_empty_db(self, tmp_data_dir):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from bilibot.api.audit import create_audit_routes

        store = AuditStore(data_dir=tmp_data_dir)
        app = Starlette(routes=create_audit_routes(store))
        client = TestClient(app)

        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        stats = resp.json()["data"]
        for key in REQUIRED_STATUS_KEYS:
            assert stats[key] == 0
        assert stats["total"] == 0
