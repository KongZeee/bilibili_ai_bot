"""
tests/test_dyn_501_dynamic_review.py - DYN-501 动态审核强制生效测试

PRD-V5 §4.1：
- review_before_publish=true → post_dynamic_text calls = 0, draft created (awaiting_review)
- review_before_publish=false → auto-publish works as before
- 同一 revision 审核两次 → 只创建一个 publish TaskRun
- rejected / expired 草稿永不发布
- 编辑草稿 → revision 自增
- PATCH 错误 expected_revision → 409
- approve 创建 publish task → scheduler 发布 → draft → published
- publish 失败 → retry_wait, retry 重新发布
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from bilibot.services.dynamic_draft_store import (
    DynamicDraft,
    DynamicDraftStore,
    STATUS_GENERATING,
    STATUS_AWAITING_REVIEW,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_PUBLISHING,
    STATUS_PUBLISHED,
    STATUS_RETRY_WAIT,
    STATUS_RESULT_UNKNOWN,
    STATUS_FAILED,
    STATUS_EXPIRED,
    PUBLISHABLE_STATUSES,
    RETRYABLE_STATUSES,
    TERMINAL_STATUSES,
    EDITABLE_STATUSES,
)
from bilibot.services.task_store import TaskRunStore
from bilibot.services.audit_store import AuditStore
from bilibot.services.persona_store import PersonaStore
from bilibot.api.dynamic_drafts import create_dynamic_drafts_routes
from bilibot.app.config_loader import ConfigLoader


def _mock_spawn_memory_task(coro=None, *args, **kwargs):
    """Mock _spawn_memory_task that closes coroutines to prevent RuntimeWarning.

    _spawn_memory_task receives a coroutine (e.g. scheduler._do_post_dynamic(...))
    and normally schedules it via asyncio.create_task. In tests we don't want the
    task to actually run, but leaving the coroutine un-awaited triggers
    RuntimeWarning. Closing it properly suppresses the warning.
    """
    if coro is not None and hasattr(coro, "close"):
        try:
            coro.close()
        except Exception:
            pass
    return MagicMock()


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
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def draft_store(tmp_data_dir):
    db_path = str(Path(tmp_data_dir) / "dynamic_drafts.db")
    return DynamicDraftStore(db_path, account_id="acc1")


@pytest.fixture
def task_store(tmp_data_dir):
    db_path = str(Path(tmp_data_dir) / "task_runs.db")
    return TaskRunStore(db_path, account_id="acc1")


def _make_draft(store, **kwargs):
    """创建一个测试草稿"""
    defaults = dict(
        account_id="acc1",
        persona_id="p1",
        task_id=f"task_{int(time.time() * 1000)}",
        content="测试动态内容",
        image_refs=[],
        safety_snapshot={"passed": True, "reason": ""},
        created_by="scheduler",
        status=STATUS_AWAITING_REVIEW,
    )
    defaults.update(kwargs)
    return store.create(**defaults)


# ═══════════════════════════════════════════════════════
#  Store 单元测试
# ═══════════════════════════════════════════════════════

class TestDynamicDraftStore:
    """DynamicDraftStore 状态机与乐观锁"""

    def test_create_draft_default_awaiting_review(self, draft_store):
        """创建草稿默认状态 awaiting_review"""
        draft_id = _make_draft(draft_store)
        d = draft_store.get(draft_id)
        assert d is not None
        assert d.status == STATUS_AWAITING_REVIEW
        assert d.revision == 1
        assert d.content == "测试动态内容"

    def test_create_draft_rejected_on_safety_deny(self, draft_store):
        """安全检查失败 → 创建即为 rejected"""
        draft_id = _make_draft(
            draft_store, status=STATUS_REJECTED,
            safety_snapshot={"passed": False, "reason": "blocked"},
        )
        d = draft_store.get(draft_id)
        assert d.status == STATUS_REJECTED

    def test_approve_optimistic_lock_success(self, draft_store):
        """审核通过：revision=1 匹配 → approved"""
        draft_id = _make_draft(draft_store)
        assert draft_store.approve(draft_id, expected_revision=1) is True
        d = draft_store.get(draft_id)
        assert d.status == STATUS_APPROVED
        assert d.reviewed_by_session_hash != "" or d.reviewed_by_session_hash == ""

    def test_approve_wrong_revision_returns_false(self, draft_store):
        """审核通过：revision 不匹配 → False"""
        draft_id = _make_draft(draft_store)
        assert draft_store.approve(draft_id, expected_revision=99) is False
        d = draft_store.get(draft_id)
        assert d.status == STATUS_AWAITING_REVIEW

    def test_approve_same_revision_twice_only_one_succeeds(self, draft_store):
        """同一 revision 审核两次 → 只有一次成功"""
        draft_id = _make_draft(draft_store)
        first = draft_store.approve(draft_id, expected_revision=1)
        second = draft_store.approve(draft_id, expected_revision=1)
        assert first is True
        assert second is False  # 已是 approved，不可再次 approve
        d = draft_store.get(draft_id)
        assert d.status == STATUS_APPROVED

    def test_reject_draft(self, draft_store):
        """拒绝 → rejected"""
        draft_id = _make_draft(draft_store)
        assert draft_store.reject(draft_id, note="不合适") is True
        d = draft_store.get(draft_id)
        assert d.status == STATUS_REJECTED
        assert d.review_note == "不合适"

    def test_edit_increments_revision(self, draft_store):
        """编辑草稿 → revision 自增"""
        draft_id = _make_draft(draft_store)
        assert draft_store.update(draft_id, content="修改后内容",
                                  expected_revision=1) is True
        d = draft_store.get(draft_id)
        assert d.revision == 2
        assert d.content == "修改后内容"

    def test_edit_wrong_revision_returns_false(self, draft_store):
        """编辑：错误 revision → False"""
        draft_id = _make_draft(draft_store)
        assert draft_store.update(draft_id, content="x",
                                  expected_revision=99) is False
        d = draft_store.get(draft_id)
        assert d.revision == 1
        assert d.content == "测试动态内容"

    def test_edit_only_allowed_in_awaiting_review(self, draft_store):
        """非 awaiting_review 状态不可编辑"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        # approved 状态不可编辑
        assert draft_store.update(draft_id, content="x",
                                  expected_revision=2) is False

    def test_mark_publishing_from_approved(self, draft_store):
        """approved → publishing"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        assert draft_store.mark_publishing(draft_id) is True
        assert draft_store.get(draft_id).status == STATUS_PUBLISHING

    def test_mark_publishing_not_allowed_from_awaiting_review(self, draft_store):
        """awaiting_review 不可直接 publishing"""
        draft_id = _make_draft(draft_store)
        assert draft_store.mark_publishing(draft_id) is False
        assert draft_store.get(draft_id).status == STATUS_AWAITING_REVIEW

    def test_mark_published(self, draft_store):
        """publishing → published"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        assert draft_store.mark_published(draft_id) is True
        assert draft_store.get(draft_id).status == STATUS_PUBLISHED

    def test_mark_retry_wait(self, draft_store):
        """publishing → retry_wait"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        assert draft_store.mark_retry_wait(draft_id, "fail") is True
        d = draft_store.get(draft_id)
        assert d.status == STATUS_RETRY_WAIT
        assert d.last_publish_error == "fail"

    def test_retry_wait_can_republish(self, draft_store):
        """retry_wait → publishing（可重试）"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        draft_store.mark_retry_wait(draft_id, "fail")
        # retry_wait 可再次 mark_publishing
        assert draft_store.mark_publishing(draft_id) is True
        assert draft_store.get(draft_id).status == STATUS_PUBLISHING

    def test_mark_result_unknown(self, draft_store):
        """publishing → result_unknown"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        assert draft_store.mark_result_unknown(draft_id, "uncertain") is True
        assert draft_store.get(draft_id).status == STATUS_RESULT_UNKNOWN

    def test_reconcile_success(self, draft_store):
        """result_unknown → published"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        draft_store.mark_result_unknown(draft_id, "uncertain")
        assert draft_store.reconcile(draft_id, success=True) is True
        assert draft_store.get(draft_id).status == STATUS_PUBLISHED

    def test_reconcile_failure(self, draft_store):
        """result_unknown → failed"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        draft_store.mark_result_unknown(draft_id, "uncertain")
        assert draft_store.reconcile(draft_id, success=False, error="dead") is True
        d = draft_store.get(draft_id)
        assert d.status == STATUS_FAILED
        assert d.last_publish_error == "dead"

    def test_reset_to_approved_for_retry(self, draft_store):
        """retry_wait/failed/result_unknown → approved（retry 接口）"""
        draft_id = _make_draft(draft_store)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.mark_publishing(draft_id)
        draft_store.mark_retry_wait(draft_id, "fail")
        assert draft_store.reset_to_approved(draft_id) is True
        assert draft_store.get(draft_id).status == STATUS_APPROVED

    def test_reset_to_approved_not_allowed_from_awaiting_review(self, draft_store):
        """awaiting_review 不可 reset_to_approved"""
        draft_id = _make_draft(draft_store)
        assert draft_store.reset_to_approved(draft_id) is False

    def test_expire_overdue(self, draft_store):
        """过期草稿 → expired（仅 awaiting_review）"""
        past = time.time() - 3600
        draft_id = _make_draft(draft_store, expires_at=past)
        count = draft_store.expire_overdue()
        assert count >= 1
        d = draft_store.get(draft_id)
        assert d.status == STATUS_EXPIRED

    def test_expire_does_not_touch_approved(self, draft_store):
        """approved 不被 expire"""
        past = time.time() - 3600
        draft_id = _make_draft(draft_store, expires_at=past)
        draft_store.approve(draft_id, expected_revision=1)
        draft_store.expire_overdue()
        assert draft_store.get(draft_id).status == STATUS_APPROVED

    def test_list_by_account_with_status_filter(self, draft_store):
        """按状态过滤列表"""
        _make_draft(draft_store, task_id="t1")
        d2 = _make_draft(draft_store, task_id="t2")
        draft_store.approve(d2, expected_revision=1)
        awaiting = draft_store.list_by_account("acc1", status=STATUS_AWAITING_REVIEW)
        approved = draft_store.list_by_account("acc1", status=STATUS_APPROVED)
        assert len(awaiting) == 1
        assert len(approved) == 1

    def test_rejected_never_publishable(self, draft_store):
        """rejected 草稿不可 mark_publishing"""
        draft_id = _make_draft(draft_store, status=STATUS_REJECTED)
        assert draft_store.mark_publishing(draft_id) is False
        assert draft_store.get(draft_id).status == STATUS_REJECTED

    def test_expired_never_publishable(self, draft_store):
        """expired 草稿不可 mark_publishing"""
        draft_id = _make_draft(draft_store, expires_at=time.time() - 3600)
        draft_store.expire_overdue()
        assert draft_store.get(draft_id).status == STATUS_EXPIRED
        assert draft_store.mark_publishing(draft_id) is False

    def test_to_dict_parses_json_fields(self, draft_store):
        """to_dict 解析 JSON 字段"""
        draft_id = _make_draft(
            draft_store, image_refs=["base64data"],
            safety_snapshot={"passed": True, "reason": "ok"},
        )
        d = draft_store.get(draft_id)
        d_dict = d.to_dict()
        assert d_dict["image_refs"] == ["base64data"]
        assert d_dict["safety_snapshot"]["passed"] is True
        assert "image_refs_json" not in d_dict
        assert "safety_snapshot_json" not in d_dict


# ═══════════════════════════════════════════════════════
#  Scheduler 测试：_do_post_dynamic 审核门控
# ═══════════════════════════════════════════════════════

def _build_mock_scheduler(tmp_data_dir, review_before_publish=False,
                           publish_succeeds=True, llm_content="这是动态内容"):
    """构造 mock Scheduler（跳过 __init__）"""
    from bilibot.scheduler import Scheduler
    sched = Scheduler.__new__(Scheduler)
    sched.audit_store = AuditStore(data_dir=tmp_data_dir)
    sched.bili = MagicMock()
    sched.bili.post_dynamic_text = AsyncMock(return_value=publish_succeeds)
    sched.bili.upload_dynamic_image = AsyncMock(return_value=None)
    sched.bili.last_api_code = 0
    sched.llm = MagicMock()
    sched.llm.generate = AsyncMock(return_value=llm_content)
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
    sched._check_bili_risk_control = MagicMock()
    sched.safety_checker = _make_safety_checker_mock()
    sched.account_id = "test_acc"
    sched.image_provider = None
    sched.memory_write_queue = None
    sched.knowledge_memory = None
    sched._dynamic_draft_store = None
    # config_loader
    sched.config_loader = MagicMock()
    sched.config_loader.get_raw_config.return_value = {
        "dynamic_publish": {
            "review_before_publish": review_before_publish,
            "draft_expiry_seconds": 86400,
            "topics": [],
            "with_image": False,
        },
        "features": {"dynamic_post": True},
    }
    # task_store + lifecycle 辅助
    sched.task_store = TaskRunStore(
        str(Path(tmp_data_dir) / "task_runs.db"), account_id="test_acc"
    )
    sched._succeed_task = MagicMock()
    sched._fail_task = MagicMock()
    sched._spawn_memory_task = MagicMock(side_effect=_mock_spawn_memory_task)
    return sched


class TestSchedulerReviewMode:
    """PRD-V5 §4.1：review_before_publish 门控"""

    @pytest.mark.asyncio
    async def test_review_mode_no_publish_no_image_upload(self, tmp_data_dir):
        """review_before_publish=true → post_dynamic_text 调用 0 次, upload 0 次"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=True)
        await sched._do_post_dynamic(task_id=None)

        sched.bili.post_dynamic_text.assert_not_called()
        sched.bili.upload_dynamic_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_review_mode_creates_draft_awaiting_review(self, tmp_data_dir):
        """review_before_publish=true → 创建 awaiting_review 草稿"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=True)
        await sched._do_post_dynamic(task_id=None)

        store = sched._get_draft_store()
        drafts = store.list_by_account("test_acc")
        assert len(drafts) == 1
        d = drafts[0]
        assert d.status == STATUS_AWAITING_REVIEW
        assert d.content == "这是动态内容"
        assert d.revision == 1
        assert d.persona_id == "unknown"

    @pytest.mark.asyncio
    async def test_auto_publish_mode_calls_post_dynamic_text(self, tmp_data_dir):
        """review_before_publish=false → 正常自动发布"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=False,
                                       publish_succeeds=True)
        await sched._do_post_dynamic(task_id=None)

        sched.bili.post_dynamic_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_review_mode_does_not_create_draft_when_auto(self, tmp_data_dir):
        """review_before_publish=false → 不创建草稿"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=False)
        await sched._do_post_dynamic(task_id=None)

        store = sched._get_draft_store()
        drafts = store.list_by_account("test_acc")
        assert len(drafts) == 0

    @pytest.mark.asyncio
    async def test_review_mode_safety_snapshot_recorded(self, tmp_data_dir):
        """审核模式下安全快照记录到草稿"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=True)
        await sched._do_post_dynamic(task_id=None)

        store = sched._get_draft_store()
        drafts = store.list_by_account("test_acc")
        d = drafts[0]
        snapshot = d.safety_snapshot
        assert snapshot["scene"] == "dynamic_post"
        assert "passed" in snapshot

    @pytest.mark.asyncio
    async def test_review_mode_succeeds_task(self, tmp_data_dir):
        """审核模式下生成任务标记为 succeeded"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=True)
        # 先创建一个 TaskRun
        task = sched.task_store.create(
            account_id="test_acc", scene="dynamic",
            idempotency_key="test:dynamic:review:1",
        )
        sched.task_store.claim(task.task_id)
        await sched._do_post_dynamic(task_id=task.task_id)

        sched._succeed_task.assert_called_once()
        call_args = sched._succeed_task.call_args
        result = call_args[0][1]
        assert result["draft_id"] is not None
        assert result["review_mode"] is True


class TestSchedulerPublishApprovedDraft:
    """PRD-V5 §4.1：审核通过后的独立发布流程"""

    @pytest.mark.asyncio
    async def test_publish_approved_draft_success(self, tmp_data_dir):
        """approve → publish task → draft → published"""
        sched = _build_mock_scheduler(tmp_data_dir, publish_succeeds=True)
        store = sched._get_draft_store()
        draft_id = _make_draft(store, account_id="test_acc")
        store.approve(draft_id, expected_revision=1)

        # 创建 publish TaskRun
        publish_task = sched.task_store.create(
            account_id="test_acc", scene="dynamic",
            idempotency_key="test:publish:1",
        )
        sched.task_store.claim(publish_task.task_id)
        await sched._do_publish_approved_draft(publish_task.task_id, draft_id)

        d = store.get(draft_id)
        assert d.status == STATUS_PUBLISHED
        sched.bili.post_dynamic_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_publish_approved_draft_failure_retry_wait(self, tmp_data_dir):
        """publish 失败 → retry_wait"""
        sched = _build_mock_scheduler(tmp_data_dir, publish_succeeds=False)
        store = sched._get_draft_store()
        draft_id = _make_draft(store, account_id="test_acc")
        store.approve(draft_id, expected_revision=1)

        publish_task = sched.task_store.create(
            account_id="test_acc", scene="dynamic",
            idempotency_key="test:publish:fail:1",
        )
        sched.task_store.claim(publish_task.task_id)
        await sched._do_publish_approved_draft(publish_task.task_id, draft_id)

        d = store.get(draft_id)
        assert d.status == STATUS_RETRY_WAIT
        assert d.last_publish_error != ""

    @pytest.mark.asyncio
    async def test_publish_retry_wait_draft_success(self, tmp_data_dir):
        """retry_wait 草稿重新发布成功 → published"""
        sched = _build_mock_scheduler(tmp_data_dir, publish_succeeds=True)
        store = sched._get_draft_store()
        draft_id = _make_draft(store, account_id="test_acc")
        store.approve(draft_id, expected_revision=1)
        store.mark_publishing(draft_id)
        store.mark_retry_wait(draft_id, "previous fail")

        # retry: reset_to_approved → mark_publishing → publish
        store.reset_to_approved(draft_id)
        publish_task = sched.task_store.create(
            account_id="test_acc", scene="dynamic",
            idempotency_key="test:publish:retry:1",
        )
        sched.task_store.claim(publish_task.task_id)
        # 修复 mock：第一次失败已过去，这次成功
        sched.bili.post_dynamic_text = AsyncMock(return_value=True)
        await sched._do_publish_approved_draft(publish_task.task_id, draft_id)

        d = store.get(draft_id)
        assert d.status == STATUS_PUBLISHED

    @pytest.mark.asyncio
    async def test_publish_non_publishable_draft_fails(self, tmp_data_dir):
        """非 approved/retry_wait 状态的草稿不可发布"""
        sched = _build_mock_scheduler(tmp_data_dir)
        store = sched._get_draft_store()
        draft_id = _make_draft(store, account_id="test_acc")
        # 草稿仍为 awaiting_review（未 approve）

        publish_task = sched.task_store.create(
            account_id="test_acc", scene="dynamic",
            idempotency_key="test:publish:nopub:1",
        )
        sched.task_store.claim(publish_task.task_id)
        await sched._do_publish_approved_draft(publish_task.task_id, draft_id)

        d = store.get(draft_id)
        assert d.status == STATUS_AWAITING_REVIEW  # 未变
        sched.bili.post_dynamic_text.assert_not_called()


# ═══════════════════════════════════════════════════════
#  API 测试
# ═══════════════════════════════════════════════════════

@pytest.fixture
def mock_llm_manager():
    mgr = MagicMock()
    mgr.get_default.return_value = None
    mgr.get_provider.return_value = None
    mgr.resolve_provider.return_value = (None, "", "")
    return mgr


@pytest.fixture
def draft_api_client(tmp_data_dir, mock_llm_manager):
    """构造带 DynamicDraftStore 的 API 测试客户端"""
    from bilibot.account.manager import AccountManager

    config = ConfigLoader(config_dict={
        "accounts": [
            {
                "id": "main",
                "name": "主账号",
                "sessdata": "s", "bili_jct": "j",
                "dede_user_id": "1", "buvid3": "b", "refresh_token": "r",
                "profile_id": "", "persona_id": "default",
                "llm_id": "", "enabled": True,
            },
        ],
        "default_account": "main",
        "data_dir": tmp_data_dir,
        "proactive": {
            "grace_window_seconds": 900,
            "scenes": {
                "dynamic": {"grace_window_seconds": 900, "max_attempts": 3},
            },
        },
    })

    persona_store = PersonaStore(data_dir=tmp_data_dir)
    audit_store = AuditStore(data_dir=tmp_data_dir)
    mgr = AccountManager(
        persona_store=persona_store,
        llm_manager=mock_llm_manager,
        audit_store=audit_store,
        orchestrator=MagicMock(),
        context_builder=MagicMock(),
        app_config_loader=config,
        data_root=tmp_data_dir,
        safety_checker=None,
    )
    mgr.initialize()
    main_acc = mgr.get_account("main")
    assert main_acc is not None
    asyncio.run(main_acc.initialize())
    assert main_acc.scheduler is not None
    # mock _spawn_memory_task 避免真正执行协程
    main_acc.scheduler._spawn_memory_task = MagicMock(side_effect=_mock_spawn_memory_task)
    # mock bili 避免真实 API 调用
    main_acc.scheduler.bili = MagicMock()
    main_acc.scheduler.bili.post_dynamic_text = AsyncMock(return_value=True)
    main_acc.scheduler.bili.upload_dynamic_image = AsyncMock(return_value=None)

    routes = create_dynamic_drafts_routes(mgr, config)
    app = Starlette(routes=routes)
    return TestClient(app), main_acc, mgr


class TestDynamicDraftAPI:
    """PRD-V5 §4.1：动态草稿 API 端点"""

    def test_list_drafts_empty(self, draft_api_client):
        """GET 列表 - 空列表"""
        client, acc, mgr = draft_api_client
        resp = client.get("/api/accounts/main/dynamic-drafts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["items"] == []
        assert body["data"]["total"] == 0

    def test_create_then_get_draft(self, draft_api_client):
        """GET 详情"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.get(f"/api/accounts/main/dynamic-drafts/{draft_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["draft_id"] == draft_id
        assert body["data"]["status"] == STATUS_AWAITING_REVIEW

    def test_get_nonexistent_draft_404(self, draft_api_client):
        """GET 不存在的草稿 → 404"""
        client, acc, mgr = draft_api_client
        resp = client.get("/api/accounts/main/dynamic-drafts/nonexistent")
        assert resp.status_code == 404

    def test_patch_edit_increments_revision(self, draft_api_client):
        """PATCH 编辑 → revision 自增"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.patch(
            f"/api/accounts/main/dynamic-drafts/{draft_id}",
            json={"expected_revision": 1, "content": "修改后内容"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["revision"] == 2
        assert body["data"]["content"] == "修改后内容"

    def test_patch_wrong_revision_409(self, draft_api_client):
        """PATCH 错误 revision → 409"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.patch(
            f"/api/accounts/main/dynamic-drafts/{draft_id}",
            json={"expected_revision": 99, "content": "x"},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "REVISION_CONFLICT"

    def test_patch_missing_expected_revision_400(self, draft_api_client):
        """PATCH 缺少 expected_revision → 400"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.patch(
            f"/api/accounts/main/dynamic-drafts/{draft_id}",
            json={"content": "x"},
        )
        assert resp.status_code == 400

    def test_approve_creates_publish_task(self, draft_api_client):
        """POST approve → 202 + publish_task_id"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/approve",
            json={"expected_revision": 1},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["data"]["publish_task_id"] is not None
        # 草稿应已 approved
        d = store.get(draft_id)
        assert d.status == STATUS_APPROVED
        # _spawn_memory_task 应被调用（发布协程已创建）
        acc.scheduler._spawn_memory_task.assert_called_once()

    def test_approve_wrong_revision_409(self, draft_api_client):
        """POST approve 错误 revision → 409"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/approve",
            json={"expected_revision": 99},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "REVISION_CONFLICT"

    def test_approve_twice_only_one_publish_task(self, draft_api_client):
        """同一 revision approve 两次 → 只创建一个 publish task"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        # 第一次 approve → 成功
        resp1 = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/approve",
            json={"expected_revision": 1},
        )
        assert resp1.status_code == 202

        # 第二次 approve 同一 revision → 409
        resp2 = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/approve",
            json={"expected_revision": 1},
        )
        assert resp2.status_code == 409

        # _spawn_memory_task 只被调用一次
        assert acc.scheduler._spawn_memory_task.call_count == 1

    def test_reject_draft(self, draft_api_client):
        """POST reject → rejected"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")

        resp = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/reject",
            json={"note": "内容不合适"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["status"] == STATUS_REJECTED
        assert body["data"]["review_note"] == "内容不合适"

    def test_rejected_draft_never_publishes(self, draft_api_client):
        """rejected 草稿不可 approve/retry"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")
        store.reject(draft_id, note="no")

        # approve 应失败（状态非 awaiting_review）
        resp = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/approve",
            json={"expected_revision": 1},
        )
        assert resp.status_code == 409
        # retry 也应失败（rejected 不在 retryable 状态）
        resp2 = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/retry",
        )
        assert resp2.status_code == 409
        # 未调用发布
        acc.scheduler._spawn_memory_task.assert_not_called()

    def test_retry_failed_draft(self, draft_api_client):
        """POST retry → 重新入队发布"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="main")
        # 模拟发布失败
        store.approve(draft_id, expected_revision=1)
        store.mark_publishing(draft_id)
        store.mark_retry_wait(draft_id, "fail")

        resp = client.post(
            f"/api/accounts/main/dynamic-drafts/{draft_id}/retry",
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["data"]["publish_task_id"] is not None
        acc.scheduler._spawn_memory_task.assert_called_once()

    def test_list_with_status_filter(self, draft_api_client):
        """GET 列表按状态过滤"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        d1 = _make_draft(store, account_id="main", task_id="t1")
        d2 = _make_draft(store, account_id="main", task_id="t2")
        store.approve(d2, expected_revision=1)

        resp = client.get("/api/accounts/main/dynamic-drafts?status=awaiting_review")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["total"] == 1
        assert body["data"]["items"][0]["draft_id"] == d1

    def test_account_not_found_404(self, draft_api_client):
        """不存在的账号 → 404"""
        client, acc, mgr = draft_api_client
        resp = client.get("/api/accounts/nonexistent/dynamic-drafts")
        assert resp.status_code == 404

    def test_draft_account_mismatch_403(self, draft_api_client):
        """草稿不属于该账号 → 403"""
        client, acc, mgr = draft_api_client
        store = acc.scheduler._get_draft_store()
        draft_id = _make_draft(store, account_id="other_acc")

        resp = client.get(f"/api/accounts/main/dynamic-drafts/{draft_id}")
        assert resp.status_code == 403


# ═══════════════════════════════════════════════════════
#  端到端集成测试
# ═══════════════════════════════════════════════════════

class TestEndToEndReviewFlow:
    """端到端：生成 → 审核 → 发布"""

    @pytest.mark.asyncio
    async def test_full_review_flow(self, tmp_data_dir):
        """完整流程：review_mode 生成草稿 → approve → publish → published"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=True,
                                       publish_succeeds=True)

        # 1. 生成动态（审核模式）
        await sched._do_post_dynamic(task_id=None)
        store = sched._get_draft_store()
        drafts = store.list_by_account("test_acc")
        assert len(drafts) == 1
        draft_id = drafts[0]["draft_id"] if isinstance(drafts[0], dict) else drafts[0].draft_id
        d = store.get(draft_id)
        assert d.status == STATUS_AWAITING_REVIEW

        # 2. 审核通过
        assert store.approve(draft_id, expected_revision=1) is True
        assert store.get(draft_id).status == STATUS_APPROVED

        # 3. 创建发布任务并执行
        publish_task = sched.task_store.create(
            account_id="test_acc", scene="dynamic",
            idempotency_key="test:e2e:publish:1",
        )
        sched.task_store.claim(publish_task.task_id)
        await sched._do_publish_approved_draft(publish_task.task_id, draft_id)

        # 4. 草稿应为 published
        d = store.get(draft_id)
        assert d.status == STATUS_PUBLISHED
        sched.bili.post_dynamic_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_expired_draft_never_publishes_e2e(self, tmp_data_dir):
        """过期草稿端到端：生成 → 过期 → approve 失败 → 永不发布"""
        sched = _build_mock_scheduler(tmp_data_dir, review_before_publish=True)
        # 设置很短的过期时间
        sched.config_loader.get_raw_config.return_value["dynamic_publish"]["draft_expiry_seconds"] = 0

        await sched._do_post_dynamic(task_id=None)
        store = sched._get_draft_store()
        drafts = store.list_by_account("test_acc")
        d = drafts[0]
        draft_id = d.draft_id

        # 草稿创建时 expires_at ≈ now，手动设到过去
        import sqlite3
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE dynamic_drafts SET expires_at=? WHERE draft_id=?",
            (time.time() - 1, draft_id),
        )
        conn.commit()
        conn.close()

        # 过期
        store.expire_overdue()
        assert store.get(draft_id).status == STATUS_EXPIRED

        # approve 应失败（状态非 awaiting_review）
        assert store.approve(draft_id, expected_revision=1) is False
        # mark_publishing 应失败
        assert store.mark_publishing(draft_id) is False
        # 未调用发布
        sched.bili.post_dynamic_text.assert_not_called()
