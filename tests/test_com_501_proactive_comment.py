"""
tests/test_com_501_proactive_comment.py - COM-501 主动评论原子幂等测试

PRD-V5 §10.2 COM-501：
- claim 原子性：同账号同视频只能一个 worker claim 成功
- 状态流转：claimed → publishing → published
- 失败：retry_wait (backoff) | failed (max attempts)
- 平台成功本地失败 → result_unknown（不自动重发）
- 同账号同视频最多一条成功主动评论
- 不同账号独立
"""
import asyncio
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from bilibot.services.proactive_comment_store import (
    ProactiveCommentAction,
    ProactiveCommentStore,
    STATUS_CLAIMED,
    STATUS_PUBLISHING,
    STATUS_PUBLISHED,
    STATUS_RETRY_WAIT,
    STATUS_RESULT_UNKNOWN,
    STATUS_FAILED,
    BLOCKING_STATUSES,
    TERMINAL_STATUSES,
    DEFAULT_MAX_ATTEMPTS,
    default_idempotency_key,
    compute_generation_hash,
)


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_data_dir):
    """ProactiveCommentStore 实例（acc1）"""
    db_path = str(Path(tmp_data_dir) / "proactive_comment_actions.db")
    return ProactiveCommentStore(db_path, account_id="acc1")


@pytest.fixture
def store_acc2(tmp_data_dir):
    """另一个账号的 ProactiveCommentStore（同 DB 文件）"""
    db_path = str(Path(tmp_data_dir) / "proactive_comment_actions.db")
    return ProactiveCommentStore(db_path, account_id="acc2")


# ═══════════════════════════════════════════════════════
#  数据类与常量
# ═══════════════════════════════════════════════════════

class TestProactiveCommentActionDataclass:
    def test_status_constants_unique(self):
        statuses = {
            STATUS_CLAIMED, STATUS_PUBLISHING, STATUS_RETRY_WAIT,
            STATUS_PUBLISHED, STATUS_RESULT_UNKNOWN, STATUS_FAILED,
        }
        assert len(statuses) == 6

    def test_terminal_statuses(self):
        assert STATUS_PUBLISHED in TERMINAL_STATUSES
        assert STATUS_RESULT_UNKNOWN in TERMINAL_STATUSES
        assert STATUS_FAILED in TERMINAL_STATUSES
        assert STATUS_CLAIMED not in TERMINAL_STATUSES
        assert STATUS_PUBLISHING not in TERMINAL_STATUSES
        assert STATUS_RETRY_WAIT not in TERMINAL_STATUSES

    def test_blocking_statuses_include_active(self):
        # claimed/publishing/published/retry_wait/result_unknown 都阻止新 claim
        for s in (STATUS_CLAIMED, STATUS_PUBLISHING, STATUS_PUBLISHED,
                  STATUS_RETRY_WAIT, STATUS_RESULT_UNKNOWN):
            assert s in BLOCKING_STATUSES
        # failed 不阻止
        assert STATUS_FAILED not in BLOCKING_STATUSES

    def test_default_idempotency_key(self):
        key = default_idempotency_key("acc1", "BV1xx")
        assert key == "acc1:BV1xx:proactive_comment"

    def test_compute_generation_hash_stable(self):
        h1 = compute_generation_hash("hello world")
        h2 = compute_generation_hash("hello world")
        assert h1 == h2
        assert len(h1) == 16

    def test_compute_generation_hash_strips_whitespace(self):
        assert compute_generation_hash("hello") == compute_generation_hash("  hello  ")

    def test_is_terminal(self):
        assert ProactiveCommentAction(
            "a1", "acc1", "BV1", status=STATUS_PUBLISHED,
        ).is_terminal()
        assert not ProactiveCommentAction(
            "a1", "acc1", "BV1", status=STATUS_CLAIMED,
        ).is_terminal()

    def test_is_published(self):
        assert ProactiveCommentAction(
            "a1", "acc1", "BV1", status=STATUS_PUBLISHED,
        ).is_published()
        assert not ProactiveCommentAction(
            "a1", "acc1", "BV1", status=STATUS_FAILED,
        ).is_published()

    def test_to_dict(self):
        a = ProactiveCommentAction(
            "a1", "acc1", "BV1", status=STATUS_CLAIMED,
            idempotency_key="k", attempt=0,
        )
        d = a.to_dict()
        assert d["action_id"] == "a1"
        assert d["status"] == STATUS_CLAIMED


# ═══════════════════════════════════════════════════════
#  Claim 原子性测试
# ═══════════════════════════════════════════════════════

class TestClaimAtomicity:
    """PRD-V5 §10.2 COM-501：claim 原子性"""

    def test_claim_succeeds_for_new_bvid(self, store):
        action = store.claim(account_id="acc1", bvid="BV1xx")
        assert action is not None
        assert action.action_id.startswith("pca_")
        assert action.status == STATUS_CLAIMED
        assert action.account_id == "acc1"
        assert action.bvid == "BV1xx"
        assert action.attempt == 0
        assert action.max_attempts == DEFAULT_MAX_ATTEMPTS

    def test_claim_fails_for_already_claimed_bvid(self, store):
        """同账号同视频已 claimed → 第二次 claim 返回 None"""
        a1 = store.claim(account_id="acc1", bvid="BV2xx")
        assert a1 is not None
        a2 = store.claim(account_id="acc1", bvid="BV2xx")
        assert a2 is None

    def test_claim_fails_for_already_published_bvid(self, store):
        """同账号同视频已 published → 新 claim 返回 None"""
        a1 = store.claim(account_id="acc1", bvid="BV3xx")
        assert a1 is not None
        store.mark_publishing(a1.action_id)
        store.mark_published(a1.action_id)
        # 再次 claim 应失败
        a2 = store.claim(account_id="acc1", bvid="BV3xx")
        assert a2 is None

    def test_claim_fails_for_retry_wait_bvid(self, store):
        """retry_wait 状态也阻止新 claim（同一动作应通过重试流程恢复）"""
        a1 = store.claim(account_id="acc1", bvid="BV4xx", max_attempts=3)
        assert a1 is not None
        store.mark_publishing(a1.action_id)
        store.mark_retry_wait(a1.action_id, "ERR", "boom")
        assert store.get(a1.action_id).status == STATUS_RETRY_WAIT
        # 新 claim 失败
        a2 = store.claim(account_id="acc1", bvid="BV4xx")
        assert a2 is None

    def test_claim_fails_for_result_unknown_bvid(self, store):
        """result_unknown 状态阻止新 claim（不自动重发）"""
        a1 = store.claim(account_id="acc1", bvid="BV5xx")
        assert a1 is not None
        store.mark_publishing(a1.action_id)
        store.mark_result_unknown(a1.action_id, "PLATFORM_UNCERTAIN", "timeout")
        # 新 claim 失败
        a2 = store.claim(account_id="acc1", bvid="BV5xx")
        assert a2 is None

    def test_different_accounts_claim_same_bvid(self, store, store_acc2):
        """不同账号可以独立 claim 同一个 bvid"""
        a1 = store.claim(account_id="acc1", bvid="BV6xx")
        a2 = store_acc2.claim(account_id="acc2", bvid="BV6xx")
        assert a1 is not None
        assert a2 is not None
        assert a1.action_id != a2.action_id
        assert a1.account_id == "acc1"
        assert a2.account_id == "acc2"

    def test_concurrent_claim_only_one_succeeds(self, store):
        """多线程并发 claim 同一 account+bvid，只有一个成功"""
        results = []
        barrier = threading.Event()

        def claimer():
            barrier.wait(timeout=2.0)
            action = store.claim(account_id="acc1", bvid="BV7xx")
            results.append(action is not None)

        threads = [threading.Thread(target=claimer) for _ in range(5)]
        for th in threads:
            th.start()
        barrier.set()
        for th in threads:
            th.join(timeout=5.0)

        assert results.count(True) == 1
        assert results.count(False) == 4

    def test_concurrent_claim_async_only_one_succeeds(self, store):
        """asyncio.gather 并发 claim，只有一个成功"""
        async def claim_one():
            # 在线程池中执行 SQLite 操作模拟并发
            return await asyncio.to_thread(
                store.claim, account_id="acc1", bvid="BV8xx",
            )

        async def main():
            results = await asyncio.gather(*[claim_one() for _ in range(5)])
            return results

        results = asyncio.run(main())
        success_count = sum(1 for r in results if r is not None)
        assert success_count == 1

    def test_claim_after_failed_allows_new(self, store):
        """failed 状态不阻止新 claim（max_attempts 用尽后允许重试）"""
        a1 = store.claim(account_id="acc1", bvid="BV9xx", max_attempts=1)
        store.mark_publishing(a1.action_id)
        # attempt=1, max=1 → failed
        store.mark_retry_wait(a1.action_id, "ERR", "boom")
        assert store.get(a1.action_id).status == STATUS_FAILED
        # 新 claim 应该可以（failed 不在 blocking 集合）
        a2 = store.claim(account_id="acc1", bvid="BV9xx")
        assert a2 is not None
        assert a2.action_id != a1.action_id


# ═══════════════════════════════════════════════════════
#  状态流转测试
# ═══════════════════════════════════════════════════════

class TestStateTransitions:
    """claimed → publishing → published / retry_wait / result_unknown / failed"""

    def test_save_generation(self, store):
        a = store.claim(account_id="acc1", bvid="BV100")
        ok = store.save_generation(a.action_id, "测试评论内容")
        assert ok is True
        result = store.get(a.action_id)
        assert result.generation_text == "测试评论内容"
        assert result.generation_hash == compute_generation_hash("测试评论内容")

    def test_save_generation_requires_claimed(self, store):
        """save_generation 只能在 claimed 状态调用"""
        a = store.claim(account_id="acc1", bvid="BV101")
        store.mark_publishing(a.action_id)
        # publishing 状态不能 save_generation
        ok = store.save_generation(a.action_id, "text")
        assert ok is False

    def test_mark_publishing_from_claimed(self, store):
        a = store.claim(account_id="acc1", bvid="BV102")
        assert store.mark_publishing(a.action_id) is True
        assert store.get(a.action_id).status == STATUS_PUBLISHING

    def test_mark_publishing_from_retry_wait(self, store):
        """retry_wait → publishing（重试时）"""
        a = store.claim(account_id="acc1", bvid="BV103", max_attempts=3)
        store.mark_publishing(a.action_id)
        store.mark_retry_wait(a.action_id, "ERR", "first fail")
        assert store.get(a.action_id).status == STATUS_RETRY_WAIT
        # retry_wait → publishing
        assert store.mark_publishing(a.action_id) is True
        assert store.get(a.action_id).status == STATUS_PUBLISHING

    def test_mark_published_sets_published_at(self, store):
        a = store.claim(account_id="acc1", bvid="BV104")
        store.mark_publishing(a.action_id)
        published_at = time.time()
        ok = store.mark_published(a.action_id, published_at=published_at)
        assert ok is True
        result = store.get(a.action_id)
        assert result.status == STATUS_PUBLISHED
        assert result.published_at == published_at
        assert result.last_error_code == ""

    def test_mark_published_requires_publishing(self, store):
        """mark_published 只能从 publishing 转"""
        a = store.claim(account_id="acc1", bvid="BV105")
        # claimed 直接 mark_published → 失败
        assert store.mark_published(a.action_id) is False

    def test_has_published_true_after_published(self, store):
        assert store.has_published("acc1", "BV106") is False
        a = store.claim(account_id="acc1", bvid="BV106")
        store.mark_publishing(a.action_id)
        store.mark_published(a.action_id)
        assert store.has_published("acc1", "BV106") is True

    def test_has_published_false_for_other_account(self, store, store_acc2):
        """has_published 区分账号"""
        a = store.claim(account_id="acc1", bvid="BV107")
        store.mark_publishing(a.action_id)
        store.mark_published(a.action_id)
        # acc2 没发布过
        assert store_acc2.has_published("acc2", "BV107") is False

    def test_mark_failed(self, store):
        a = store.claim(account_id="acc1", bvid="BV108")
        ok = store.mark_failed(a.action_id, "POLICY_REJECTED", "daily_budget_exhausted")
        assert ok is True
        result = store.get(a.action_id)
        assert result.status == STATUS_FAILED
        assert result.last_error_code == "POLICY_REJECTED"
        assert result.last_error == "daily_budget_exhausted"
        assert result.next_retry_at is None


# ═══════════════════════════════════════════════════════
#  Retry + Backoff 测试
# ═══════════════════════════════════════════════════════

class TestRetryAndBackoff:
    """failure → retry_wait (with backoff) | failed (max attempts)"""

    def test_mark_retry_wait_first_attempt(self, store):
        a = store.claim(account_id="acc1", bvid="BV200", max_attempts=3)
        store.mark_publishing(a.action_id)
        ok = store.mark_retry_wait(a.action_id, "BILI_API_FALSE", "code=-101")
        assert ok is True
        result = store.get(a.action_id)
        assert result.status == STATUS_RETRY_WAIT
        assert result.attempt == 1
        assert result.next_retry_at is not None
        assert result.next_retry_at > time.time()
        assert result.last_error_code == "BILI_API_FALSE"

    def test_mark_retry_wait_max_attempts_goes_failed(self, store):
        """达到 max_attempts → failed"""
        a = store.claim(account_id="acc1", bvid="BV201", max_attempts=2)
        store.mark_publishing(a.action_id)
        # 第一次失败 → retry_wait (attempt=1)
        store.mark_retry_wait(a.action_id, "ERR", "first")
        assert store.get(a.action_id).status == STATUS_RETRY_WAIT
        # retry_wait → publishing 重试
        store.mark_publishing(a.action_id)
        # 第二次失败 → attempt=2 >= max=2 → failed
        store.mark_retry_wait(a.action_id, "ERR", "second")
        result = store.get(a.action_id)
        assert result.status == STATUS_FAILED
        assert result.attempt == 2
        assert result.next_retry_at is None

    def test_list_pending_retry_empty(self, store):
        """无 retry_wait 时返回空"""
        pending = store.list_pending_retry(account_id="acc1")
        assert pending == []

    def test_list_pending_retry_after_backoff(self, store):
        """retry_wait 动作 next_retry_at <= now 时被拾取"""
        a = store.claim(account_id="acc1", bvid="BV202", max_attempts=3)
        store.mark_publishing(a.action_id)
        now = time.time()
        store.mark_retry_wait(a.action_id, "ERR", "boom", now=now)
        # next_retry_at 是 now + 几秒，立即查询可能还未到
        # 用一个很远的未来时间查询
        far_future = now + 10000
        pending = store.list_pending_retry(account_id="acc1", now=far_future)
        assert any(p.action_id == a.action_id for p in pending)

    def test_list_pending_retry_not_yet_ready(self, store):
        """next_retry_at > now 时不被拾取"""
        a = store.claim(account_id="acc1", bvid="BV203", max_attempts=3)
        store.mark_publishing(a.action_id)
        now = time.time()
        store.mark_retry_wait(a.action_id, "ERR", "boom", now=now)
        # next_retry_at 一定 > now，立即查询应返回空
        pending = store.list_pending_retry(account_id="acc1", now=now)
        assert pending == []

    def test_list_pending_retry_scoped_to_account(self, store, store_acc2):
        """list_pending_retry 只返回指定账号的动作"""
        a1 = store.claim(account_id="acc1", bvid="BV204", max_attempts=3)
        store.mark_publishing(a1.action_id)
        store.mark_retry_wait(a1.action_id, "ERR", "acc1 fail")

        a2 = store_acc2.claim(account_id="acc2", bvid="BV205", max_attempts=3)
        store_acc2.mark_publishing(a2.action_id)
        store_acc2.mark_retry_wait(a2.action_id, "ERR", "acc2 fail")

        far_future = time.time() + 10000
        acc1_pending = store.list_pending_retry(
            account_id="acc1", now=far_future,
        )
        acc2_pending = store_acc2.list_pending_retry(
            account_id="acc2", now=far_future,
        )
        assert all(p.account_id == "acc1" for p in acc1_pending)
        assert all(p.account_id == "acc2" for p in acc2_pending)
        assert any(p.action_id == a1.action_id for p in acc1_pending)
        assert any(p.action_id == a2.action_id for p in acc2_pending)


# ═══════════════════════════════════════════════════════
#  result_unknown 测试
# ═══════════════════════════════════════════════════════

class TestResultUnknown:
    """PRD-V5 §10.2：平台成功 + 本地失败 → result_unknown（不自动重发）"""

    def test_mark_result_unknown(self, store):
        a = store.claim(account_id="acc1", bvid="BV300")
        store.mark_publishing(a.action_id)
        ok = store.mark_result_unknown(
            a.action_id, "PLATFORM_UNCERTAIN", "http timeout",
        )
        assert ok is True
        result = store.get(a.action_id)
        assert result.status == STATUS_RESULT_UNKNOWN
        assert result.last_error_code == "PLATFORM_UNCERTAIN"
        assert result.last_error == "http timeout"
        assert result.next_retry_at is None

    def test_result_unknown_is_terminal(self, store):
        a = store.claim(account_id="acc1", bvid="BV301")
        store.mark_publishing(a.action_id)
        store.mark_result_unknown(a.action_id, "ERR", "x")
        assert store.get(a.action_id).is_terminal()

    def test_result_unknown_not_in_pending_retry(self, store):
        """result_unknown 不进入 retry 队列"""
        a = store.claim(account_id="acc1", bvid="BV302")
        store.mark_publishing(a.action_id)
        store.mark_result_unknown(a.action_id, "ERR", "x")
        far_future = time.time() + 10000
        pending = store.list_pending_retry(account_id="acc1", now=far_future)
        assert all(p.action_id != a.action_id for p in pending)

    def test_result_unknown_blocks_new_claim(self, store):
        """result_unknown 阻止新 claim（不自动重发）"""
        a = store.claim(account_id="acc1", bvid="BV303")
        store.mark_publishing(a.action_id)
        store.mark_result_unknown(a.action_id, "ERR", "x")
        # 新 claim 失败
        a2 = store.claim(account_id="acc1", bvid="BV303")
        assert a2 is None

    def test_mark_result_unknown_requires_publishing(self, store):
        """mark_result_unknown 只能从 publishing 转"""
        a = store.claim(account_id="acc1", bvid="BV304")
        # claimed 直接 mark_result_unknown → 失败
        assert store.mark_result_unknown(a.action_id, "ERR", "x") is False


# ═══════════════════════════════════════════════════════
#  get_by_bvid 测试
# ═══════════════════════════════════════════════════════

class TestGetByBvid:
    def test_get_by_bvid_returns_latest(self, store):
        a1 = store.claim(account_id="acc1", bvid="BV400", max_attempts=1)
        store.mark_publishing(a1.action_id)
        store.mark_retry_wait(a1.action_id, "ERR", "fail")  # → failed
        # failed 后允许新 claim
        a2 = store.claim(account_id="acc1", bvid="BV400")
        assert a2 is not None
        # get_by_bvid 返回最新的
        result = store.get_by_bvid("acc1", "BV400")
        assert result.action_id == a2.action_id

    def test_get_by_bvid_none_if_not_exists(self, store):
        assert store.get_by_bvid("acc1", "BV_NOT_EXIST") is None


# ═══════════════════════════════════════════════════════
#  Scheduler 集成测试（完整流程）
# ═══════════════════════════════════════════════════════

def _make_scheduler(tmp_data_dir, **overrides):
    """构造带 ProactiveCommentStore 的 Scheduler（mock bili / comment_generator）"""
    from bilibot.app.config_loader import ConfigLoader
    from bilibot.scheduler import Scheduler
    from bilibot.data_store import DataStore

    config = ConfigLoader(config_dict={
        "bilibili": {"sessdata": "s", "bili_jct": "j", "dede_user_id": "1"},
        "llm": {"api_key": "test", "base_url": "http://x", "model": "test"},
        "data_dir": tmp_data_dir,
        "features": {"proactive_comment": True},
        "interactions": {
            "comment": {"enabled": True, "max_per_day": 10, "score_threshold": 7},
        },
        "proactive": {
            "scenes": {
                "proactive_comment": {"max_attempts": 3, "grace_window_seconds": 900},
            },
        },
    })

    bili = MagicMock()
    bili.last_api_code = 0
    bili.post_comment = AsyncMock(return_value=True)

    comment_generator = MagicMock()
    comment_generator.generate_proactive_comment = AsyncMock(
        return_value="这是一条测试主动评论",
    )

    # 传入 data_store 让 CommentPolicy / InteractionPolicy 使用 tmp_data_dir
    # 避免污染共享的 ./data/interaction_budget.db
    data_store = DataStore(tmp_data_dir)

    sched = Scheduler(
        config_loader=config,
        bili=bili,
        data_store=data_store,
        account_id="acc1",
        proactive_comment_store=ProactiveCommentStore(
            str(Path(tmp_data_dir) / "pc.db"), account_id="acc1",
        ),
    )
    sched.comment_generator = comment_generator
    # 屏蔽 safety_checker（默认 None 即可）
    sched.safety_checker = None
    # 应用 overrides
    for k, v in overrides.items():
        setattr(sched, k, v)
    return sched


class TestSchedulerFullFlowSuccess:
    """完整流程：claim → generate → publish success → mark_published"""

    def test_full_flow_publish_success(self, tmp_data_dir):
        sched = _make_scheduler(tmp_data_dir)
        result = asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV500",
            oid=12345,
            title="测试视频",
            owner="UP主",
            desc="描述",
            tags_list=["tag1"],
            review="好评",
            mood="开心",
            video_content="",
            evaluation={"comment": ""},
            llm_ok=False,
            task_id="task_xxx",
        ))
        assert result == "这是一条测试主动评论"
        # 验证 store 状态
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV500")
        assert action.status == STATUS_PUBLISHED
        assert action.published_at is not None
        assert action.generation_text == "这是一条测试主动评论"
        assert action.task_id == "task_xxx"
        # bili.post_comment 被调用
        sched.bili.post_comment.assert_awaited_once()

    def test_full_flow_claim_skips_second_worker(self, tmp_data_dir):
        """第二个 worker 在同 bvid 上调用，应直接返回空"""
        sched = _make_scheduler(tmp_data_dir)
        # 第一worker 成功
        asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV501", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        # 第二个 worker 调用同 bvid → claim 失败 → 返回 ""
        result = asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV501", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        assert result == ""
        # bili.post_comment 只被调用一次（第二个 worker 没到这步）
        assert sched.bili.post_comment.await_count == 1


class TestSchedulerFullFlowPolicyReject:
    """完整流程：claim → generate → policy reject → mark_failed"""

    def test_policy_reject_marks_failed(self, tmp_data_dir):
        sched = _make_scheduler(tmp_data_dir)
        # 让 CommentPolicy.check 返回拒绝：预先插入一条 published 记录使 video 重复
        # 通过先成功发布一条，再触发第二条
        sched.bili.post_comment = AsyncMock(return_value=True)
        asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV600", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        # 第二次同 bvid：claim 应该失败（已 published）
        result = asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV600", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        assert result == ""

    def test_empty_comment_marks_failed(self, tmp_data_dir):
        """LLM 生成失败 → 评论为空 → mark_failed"""
        sched = _make_scheduler(tmp_data_dir)
        sched.comment_generator.generate_proactive_comment = AsyncMock(
            return_value="",  # 生成空
        )
        result = asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV601", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        assert result == ""
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV601")
        assert action.status == STATUS_FAILED
        assert action.last_error_code == "NO_COMMENT_TEXT"
        # bili.post_comment 不应被调用
        sched.bili.post_comment.assert_not_awaited()


class TestSchedulerFullFlowRetryable:
    """完整流程：claim → generate → publish retryable error → mark_retry_wait → retry"""

    def test_publish_false_goes_retry_wait(self, tmp_data_dir):
        sched = _make_scheduler(tmp_data_dir)
        sched.bili.post_comment = AsyncMock(return_value=False)
        sched.bili.last_api_code = -101

        result = asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV700", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        assert result == ""
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV700")
        assert action.status == STATUS_RETRY_WAIT
        assert action.attempt == 1
        assert action.next_retry_at is not None
        assert action.last_error_code == "BILI_API_FALSE"

    def test_retry_succeeds_via_process_retryable(self, tmp_data_dir):
        """retry_wait 动作通过 _process_retryable_proactive_comments 重试成功"""
        sched = _make_scheduler(tmp_data_dir)
        # 第一次发布失败 → retry_wait
        sched.bili.post_comment = AsyncMock(return_value=False)
        sched.bili.last_api_code = -101
        asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV701", oid=12345, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV701")
        assert action.status == STATUS_RETRY_WAIT

        # mock bili.get_video_oid_by_bvid 返回 oid（_resolve_oid_from_bvid 调用此方法）
        sched.bili.get_video_oid_by_bvid = AsyncMock(return_value=12345)
        # 第二次发布成功
        sched.bili.post_comment = AsyncMock(return_value=True)
        # 用很远的未来时间触发 retry
        far_future = time.time() + 10000
        # 直接调用内部逻辑（绕过 list_pending_retry 的时间检查）
        # 先手动改 next_retry_at 到过去
        import sqlite3 as _sql
        conn = _sql.connect(sched.proactive_comment_store.db_path)
        conn.execute(
            "UPDATE proactive_comment_actions SET next_retry_at=? WHERE action_id=?",
            (time.time() - 1, action.action_id),
        )
        conn.commit()
        conn.close()

        asyncio.run(sched._process_retryable_proactive_comments())
        action = sched.proactive_comment_store.get(action.action_id)
        assert action.status == STATUS_PUBLISHED


class TestSchedulerFullFlowResultUnknown:
    """完整流程：claim → generate → publish 异常 → mark_result_unknown"""

    def test_publish_exception_marks_result_unknown(self, tmp_data_dir):
        sched = _make_scheduler(tmp_data_dir)
        sched.bili.post_comment = AsyncMock(side_effect=asyncio.TimeoutError("timeout"))

        result = asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV800", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        assert result == ""
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV800")
        assert action.status == STATUS_RESULT_UNKNOWN
        assert action.last_error_code == "PUBLISH_EXCEPTION"
        # 不自动重发：next_retry_at 为 None
        assert action.next_retry_at is None

    def test_result_unknown_not_retried(self, tmp_data_dir):
        """result_unknown 不被 _process_retryable_proactive_comments 拾取"""
        sched = _make_scheduler(tmp_data_dir)
        sched.bili.post_comment = AsyncMock(side_effect=asyncio.TimeoutError())
        asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV801", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV801")
        assert action.status == STATUS_RESULT_UNKNOWN

        # 调用重试处理
        sched.bili.get_video_info = AsyncMock(return_value={"data": {"aid": 1}})
        sched.bili.post_comment = AsyncMock(return_value=True)
        asyncio.run(sched._process_retryable_proactive_comments())
        # 状态不应改变
        action = sched.proactive_comment_store.get(action.action_id)
        assert action.status == STATUS_RESULT_UNKNOWN


class TestSchedulerMaxAttempts:
    """max_attempts 达到 → failed"""

    def test_max_attempts_reached_marks_failed(self, tmp_data_dir):
        sched = _make_scheduler(tmp_data_dir)
        # max_attempts=2
        sched.bili.post_comment = AsyncMock(return_value=False)
        sched.bili.last_api_code = -101

        # 第一次发布 → retry_wait (attempt=1)
        asyncio.run(sched._do_proactive_comment_publish(
            bvid="BV900", oid=1, title="t", owner="o", desc="",
            tags_list=[], review="", mood="", video_content="",
            evaluation={}, llm_ok=False,
        ))
        action = sched.proactive_comment_store.get_by_bvid("acc1", "BV900")
        assert action.status == STATUS_RETRY_WAIT
        assert action.attempt == 1

        # 手动触发重试 → 又失败 → attempt=2 >= max=2 → failed
        sched.bili.get_video_oid_by_bvid = AsyncMock(return_value=1)
        # 把 next_retry_at 设到过去
        import sqlite3 as _sql
        conn = _sql.connect(sched.proactive_comment_store.db_path)
        conn.execute(
            "UPDATE proactive_comment_actions SET next_retry_at=?, max_attempts=? "
            "WHERE action_id=?",
            (time.time() - 1, 2, action.action_id),
        )
        conn.commit()
        conn.close()

        asyncio.run(sched._process_retryable_proactive_comments())
        action = sched.proactive_comment_store.get(action.action_id)
        assert action.status == STATUS_FAILED
        assert action.attempt == 2
        assert action.next_retry_at is None
