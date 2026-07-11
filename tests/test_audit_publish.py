"""
tests/test_audit_publish.py - 审计发布状态准确性测试

PRD V4 §4.5 / §7.4 必需测试文件。
覆盖：
- AuditStore.mark_published() 单元测试
  - 动态发布成功 audit published=1
  - 评论回复发布成功 audit published=1
  - 发布失败 published=0 + failure_reason
  - target 合并覆盖
- Scheduler 发布后调用 mark_published（动态 / 评论回复两条路径）
- /api/audit/generations 返回真实 published 状态
"""
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from bilibot.services.audit_store import AuditStore


def _make_safety_checker_mock():
    """构造 SafetyChecker mock（DYN-604 fail-closed 要求 safety_checker 非 None）

    SafetyService 接口：
    - is_paused / is_account_paused / check_rate_limit / record_publish / record_content → 同步
    - check_content → 异步，返回 (passed, reason)
    """
    sc = MagicMock()
    sc.is_paused = MagicMock(return_value=False)
    sc.is_account_paused = MagicMock(return_value=False)
    sc.check_rate_limit = MagicMock(return_value=True)
    sc.check_content = AsyncMock(return_value=(True, ""))
    sc.record_publish = MagicMock()
    sc.record_content = MagicMock()
    return sc


# ═══════════════════════════════════════════════════════
#  AuditStore.mark_published 单元测试
# ═══════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_data_dir):
    return AuditStore(data_dir=tmp_data_dir)


class TestMarkPublishedUnit:
    """PRD V4 §4.5.1：mark_published 单元测试"""

    def test_mark_published_true(self, store):
        """成功发布：published=1"""
        aid = store.record(
            scene="dynamic_post",
            persona_id="default",
            output="动态内容",
            published=False,
        )
        ok = store.mark_published(aid, published=True, target={"kind": "dynamic"})
        assert ok is True

        item = store.get(aid)
        assert item["published"] == 1
        assert json.loads(item["target"])["kind"] == "dynamic"

    def test_mark_published_false_with_failure_reason(self, store):
        """发布失败：published=0 + failure_reason"""
        aid = store.record(
            scene="reply_comment",
            persona_id="default",
            output="回复内容",
            published=False,
        )
        ok = store.mark_published(
            aid, published=False,
            target={"kind": "reply_comment", "rpid": "999"},
            failure_reason="bili.post_comment 返回 False",
        )
        assert ok is True

        item = store.get(aid)
        assert item["published"] == 0
        target = json.loads(item["target"])
        assert target["kind"] == "reply_comment"
        assert target["rpid"] == "999"
        assert "返回 False" in target["failure_reason"]

    def test_mark_published_merges_target(self, store):
        """target 合并：原 target 字段保留，新字段覆盖"""
        aid = store.record(
            scene="dynamic_post",
            persona_id="default",
            output="动态内容",
            published=False,
            target={"kind": "dynamic", "draft": "草稿"},
        )
        store.mark_published(
            aid, published=True,
            target={"published_at": "2026-01-01", "platform": "bilibili"},
        )

        item = store.get(aid)
        target = json.loads(item["target"])
        # 原字段保留
        assert target["kind"] == "dynamic"
        assert target["draft"] == "草稿"
        # 新字段写入
        assert target["published_at"] == "2026-01-01"
        assert target["platform"] == "bilibili"

    def test_mark_published_nonexistent_returns_false(self, store):
        """不存在的 audit_id 返回 False"""
        ok = store.mark_published("gen_nonexistent", published=True)
        assert ok is False

    def test_update_result_alias(self, store):
        """update_result 是 mark_published 的别名"""
        aid = store.record(scene="dynamic_post", persona_id="default", output="x")
        ok = store.update_result(aid, published=True, target={"k": "v"})
        assert ok is True
        item = store.get(aid)
        assert item["published"] == 1


# ═══════════════════════════════════════════════════════
#  Scheduler 动态发布后调用 mark_published
# ═══════════════════════════════════════════════════════

class TestSchedulerDynamicPublish:
    """PRD V4 §4.5.2：动态发布后更新 audit published 状态"""

    @pytest.mark.asyncio
    async def test_dynamic_publish_success_marks_published_true(
        self, tmp_data_dir,
    ):
        """动态发布成功：audit published=1"""
        # 构造 Scheduler 但跳过 __init__（避免初始化所有依赖）
        from bilibot.scheduler import Scheduler

        sched = Scheduler.__new__(Scheduler)
        sched.audit_store = AuditStore(data_dir=tmp_data_dir)
        sched.bili = MagicMock()
        sched.bili.post_dynamic_text = AsyncMock(return_value=True)
        sched.llm = MagicMock()
        sched.llm.generate = AsyncMock(return_value="动态内容")
        sched.personality = MagicMock()
        sched.personality.get_system_prompt = MagicMock(return_value="系统提示")
        sched.persona_store = None
        sched.orchestrator = MagicMock()
        sched.orchestrator.build_dynamic_prompt = MagicMock(
            return_value={"system": "sys", "user": "user"}
        )
        sched.ds = MagicMock()
        sched.ds.load_json = MagicMock(return_value=[])
        sched.ds.save_json = MagicMock()
        sched._get_data_dir = MagicMock(return_value=tmp_data_dir)
        sched._get_current_persona_id = MagicMock(return_value="default")
        sched.account_id = "test_acc"
        sched.memory = MagicMock()
        sched.memory.save_self_memory = AsyncMock()
        sched.safety_checker = _make_safety_checker_mock()
        # MEM-501：scheduler 不再直接 import memory_writer，改为通过队列写入
        sched.memory_write_queue = None
        sched.knowledge_memory = None

        await sched._do_post_dynamic()

        # 找到刚才写入的 audit
        items = sched.audit_store.query(scene="dynamic_post")
        assert len(items) == 1
        assert items[0]["published"] == 1
        target = json.loads(items[0]["target"])
        assert target.get("kind") == "dynamic"
        assert "published_at" in target

    @pytest.mark.asyncio
    async def test_dynamic_publish_failure_keeps_published_false(
        self, tmp_data_dir,
    ):
        """动态发布失败：audit published=0 + failure_reason"""
        from bilibot.scheduler import Scheduler

        sched = Scheduler.__new__(Scheduler)
        sched.audit_store = AuditStore(data_dir=tmp_data_dir)
        sched.bili = MagicMock()
        sched.bili.post_dynamic_text = AsyncMock(return_value=False)  # 失败
        sched.llm = MagicMock()
        sched.llm.generate = AsyncMock(return_value="失败的动态内容")
        sched.personality = MagicMock()
        sched.personality.get_system_prompt = MagicMock(return_value="系统提示")
        sched.persona_store = None
        sched.orchestrator = MagicMock()
        sched.orchestrator.build_dynamic_prompt = MagicMock(
            return_value={"system": "sys", "user": "user"}
        )
        sched.ds = MagicMock()
        sched._get_data_dir = MagicMock(return_value=tmp_data_dir)
        sched._get_current_persona_id = MagicMock(return_value="default")
        sched.account_id = "test_acc"
        sched.memory = None
        sched.safety_checker = _make_safety_checker_mock()
        # MEM-501：scheduler 不再直接 import memory_writer，改为通过队列写入
        sched.memory_write_queue = None
        sched.knowledge_memory = None

        await sched._do_post_dynamic()

        items = sched.audit_store.query(scene="dynamic_post")
        assert len(items) == 1
        assert items[0]["published"] == 0
        target = json.loads(items[0]["target"])
        assert "failure_reason" in target


# ═══════════════════════════════════════════════════════
#  Scheduler 评论回复发布后调用 mark_published
# ═══════════════════════════════════════════════════════

class TestSchedulerReplyPublish:
    """PRD V4 §4.5.3：评论回复发布后更新 audit published 状态

    通过直接调用 ReplyGenerator.generate_reply 验证 audit_id 返回，
    再调用 audit_store.mark_published 模拟 scheduler 流程。
    """

    def test_reply_publish_success_marks_published_true(self, tmp_data_dir):
        """评论回复成功：audit published=1"""
        audit_store = AuditStore(data_dir=tmp_data_dir)
        # 模拟 ReplyGenerator 写入 audit（不依赖真实 LLM）
        audit_id = audit_store.record(
            scene="reply_comment",
            persona_id="default",
            input_summary="用户评论",
            output="回复内容",
            published=False,
            target={
                "oid": "12345",
                "thread_id": "999999",
                "user_id": "88888",
                "username": "小明",
            },
        )

        # scheduler 发布成功后调用 mark_published
        audit_store.mark_published(
            audit_id, published=True,
            target={
                "kind": "reply_comment",
                "rpid": "999999",
                "oid": "12345",
                "published_at": datetime.now().isoformat(),
            },
        )

        item = audit_store.get(audit_id)
        assert item["published"] == 1
        target = json.loads(item["target"])
        assert target["kind"] == "reply_comment"
        assert target["rpid"] == "999999"
        # 原 target 字段也保留
        assert target["username"] == "小明"

    def test_reply_publish_failure_keeps_published_false(self, tmp_data_dir):
        """评论回复失败：audit published=0 + failure_reason"""
        audit_store = AuditStore(data_dir=tmp_data_dir)
        audit_id = audit_store.record(
            scene="reply_comment",
            persona_id="default",
            output="回复内容",
            published=False,
        )
        audit_store.mark_published(
            audit_id, published=False,
            target={"kind": "reply_comment", "rpid": "999"},
            failure_reason="bili.post_comment 返回 False",
        )

        item = audit_store.get(audit_id)
        assert item["published"] == 0
        target = json.loads(item["target"])
        assert target["failure_reason"]
        assert "返回 False" in target["failure_reason"]


# ═══════════════════════════════════════════════════════
#  HTTP API 返回真实 published 状态
# ═══════════════════════════════════════════════════════

class TestAuditHTTPPublished:
    """/api/audit/generations 返回真实 published 状态"""

    def test_http_returns_published_field(self, tmp_data_dir):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from bilibot.api.audit import create_audit_routes

        store = AuditStore(data_dir=tmp_data_dir)
        aid1 = store.record(
            scene="dynamic_post", persona_id="default",
            output="动态1", published=False,
        )
        store.mark_published(aid1, published=True)

        aid2 = store.record(
            scene="reply_comment", persona_id="default",
            output="回复1", published=False,
        )
        # aid2 保持 published=False

        app = Starlette(routes=create_audit_routes(store))
        client = TestClient(app)

        resp = client.get("/api/audit/generations")
        assert resp.status_code == 200
        data = resp.json()
        items = data["data"]

        # 应有 2 条记录
        assert len(items) == 2
        published_map = {i["id"]: i["published"] for i in items}
        assert published_map[aid1] == 1
        assert published_map[aid2] == 0

    def test_http_stats_reflects_published(self, tmp_data_dir):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from bilibot.api.audit import create_audit_routes

        store = AuditStore(data_dir=tmp_data_dir)
        # 3 条：1 published, 2 generated（待发布）
        a1 = store.record(scene="dynamic_post", persona_id="d", output="x")
        store.mark_published(a1, published=True)
        store.record(scene="reply_comment", persona_id="d", output="y")
        store.record(scene="reply_comment", persona_id="d", output="z")

        app = Starlette(routes=create_audit_routes(store))
        client = TestClient(app)
        resp = client.get("/api/audit/stats")
        assert resp.status_code == 200
        stats = resp.json()["data"]
        assert stats["total"] == 3
        # OBS-501：按语义化状态分别统计，不再用 total - published 推算 draft
        assert stats["published"] == 1
        assert stats["generated"] == 2
        assert "draft" not in stats
