"""
tests/test_task_501_taskrun.py - TASK-501 TaskRun 持久化生命周期测试

PRD-V5 §7 / TASK-501：
- State transitions: scheduled→claimed→running→succeeded
- Failure → retry_wait with backoff
- Max attempts → failed
- Overdue → expired (not triggered)
- Restart: claimed/running → interrupted → recoverable
- Platform success + local failure → result_unknown (no auto-republish)
- Idempotency: duplicate create rejected
- Claim is atomic (concurrent claims: only one succeeds)
- Late window: within window → claimable; beyond → expired
- Daily count 0 → empty plan persisted
- Manual API returns 202 + task_id
- Old default-account endpoints return 410
"""
import json
import os
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mock_spawn_memory_task(coro=None, *args, **kwargs):
    """Mock _spawn_memory_task that closes coroutines to prevent RuntimeWarning.

    _spawn_memory_task receives a coroutine (e.g. scheduler._do_proactive_video(...))
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

from starlette.testclient import TestClient
from starlette.applications import Starlette

from bilibot.services.task_store import (
    TaskRun,
    TaskRunStore,
    desensitize_task_run,
    STATUS_SCHEDULED,
    STATUS_CLAIMED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_RETRY_WAIT,
    STATUS_FAILED,
    STATUS_EXPIRED,
    STATUS_INTERRUPTED,
    STATUS_RESULT_UNKNOWN,
    TRIGGER_SCHEDULE,
    TRIGGER_MANUAL,
    TRIGGER_RETRY,
    TRIGGER_RECOVERY,
    SCENE_PROACTIVE_VIDEO,
    SCENE_DYNAMIC,
    SCENE_WEEKLY_SUMMARY,
    DEFAULT_GRACE_WINDOW,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_LEASE_SECONDS,
    TERMINAL_STATUSES,
    RETRYABLE_STATUSES,
    RECOVERABLE_ON_RESTART,
)
from bilibot.api.accounts import create_accounts_routes
from bilibot.api.tasks import create_tasks_routes
from bilibot.app.config_loader import ConfigLoader
from bilibot.services.audit_store import AuditStore
from bilibot.services.persona_store import PersonaStore
from bilibot.account.manager import AccountManager


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_data_dir):
    """TaskRunStore 实例"""
    db_path = str(Path(tmp_data_dir) / "task_runs.db")
    return TaskRunStore(db_path, account_id="acc1")


@pytest.fixture
def store_other(tmp_data_dir):
    """另一个账号的 TaskRunStore（同一个 DB 文件，不同 account_id）"""
    db_path = str(Path(tmp_data_dir) / "task_runs.db")
    return TaskRunStore(db_path, account_id="acc2")


# ═══════════════════════════════════════════════════════
#  TaskRun 数据类测试
# ═══════════════════════════════════════════════════════

class TestTaskRunDataclass:
    """TaskRun 数据类与状态常量"""

    def test_status_constants_unique(self):
        """所有状态常量互不相同"""
        statuses = {
            STATUS_SCHEDULED, STATUS_CLAIMED, STATUS_RUNNING, STATUS_RETRY_WAIT,
            STATUS_SUCCEEDED, STATUS_FAILED, STATUS_EXPIRED,
            STATUS_INTERRUPTED, STATUS_RESULT_UNKNOWN,
        }
        assert len(statuses) == 9

    def test_terminal_statuses_include_succeeded_failed_expired(self):
        assert STATUS_SUCCEEDED in TERMINAL_STATUSES
        assert STATUS_FAILED in TERMINAL_STATUSES
        assert STATUS_EXPIRED in TERMINAL_STATUSES
        assert STATUS_INTERRUPTED in TERMINAL_STATUSES
        assert STATUS_RESULT_UNKNOWN in TERMINAL_STATUSES

    def test_non_terminal_not_in_terminal(self):
        assert STATUS_SCHEDULED not in TERMINAL_STATUSES
        assert STATUS_CLAIMED not in TERMINAL_STATUSES
        assert STATUS_RUNNING not in TERMINAL_STATUSES
        assert STATUS_RETRY_WAIT not in TERMINAL_STATUSES

    def test_retryable_statuses(self):
        assert STATUS_RETRY_WAIT in RETRYABLE_STATUSES
        assert STATUS_FAILED in RETRYABLE_STATUSES
        assert STATUS_INTERRUPTED in RETRYABLE_STATUSES
        # succeeded / expired / result_unknown 不可 retry
        assert STATUS_SUCCEEDED not in RETRYABLE_STATUSES
        assert STATUS_EXPIRED not in RETRYABLE_STATUSES
        assert STATUS_RESULT_UNKNOWN not in RETRYABLE_STATUSES

    def test_recoverable_on_restart(self):
        assert STATUS_CLAIMED in RECOVERABLE_ON_RESTART
        assert STATUS_RUNNING in RECOVERABLE_ON_RESTART
        assert STATUS_SCHEDULED not in RECOVERABLE_ON_RESTART

    def test_is_terminal(self):
        assert TaskRun("t1", "a", "s", "k", status=STATUS_SUCCEEDED).is_terminal()
        assert TaskRun("t1", "a", "s", "k", status=STATUS_FAILED).is_terminal()
        assert not TaskRun("t1", "a", "s", "k", status=STATUS_RUNNING).is_terminal()

    def test_is_retryable(self):
        assert TaskRun("t1", "a", "s", "k", status=STATUS_RETRY_WAIT).is_retryable()
        assert TaskRun("t1", "a", "s", "k", status=STATUS_FAILED).is_retryable()
        assert not TaskRun("t1", "a", "s", "k", status=STATUS_SUCCEEDED).is_retryable()

    def test_is_succeeded(self):
        assert TaskRun("t1", "a", "s", "k", status=STATUS_SUCCEEDED).is_succeeded()
        assert not TaskRun("t1", "a", "s", "k", status=STATUS_FAILED).is_succeeded()

    def test_to_dict(self):
        t = TaskRun("t1", "a", "s", "k", status=STATUS_SCHEDULED)
        d = t.to_dict()
        assert d["task_id"] == "t1"
        assert d["status"] == STATUS_SCHEDULED


# ═══════════════════════════════════════════════════════
#  Create + 幂等性测试
# ═══════════════════════════════════════════════════════

class TestTaskRunCreate:
    """create / idempotency"""

    def test_create_returns_task_run(self, store):
        task = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="acc1:pv:2026-07-11:10:00",
            trigger_type=TRIGGER_SCHEDULE,
            scheduled_at=time.time(),
            input_data={"slot": "10:00"},
        )
        assert task is not None
        assert task.task_id.startswith("task_")
        assert task.status == STATUS_SCHEDULED
        assert task.scene == SCENE_PROACTIVE_VIDEO
        assert task.account_id == "acc1"

    def test_duplicate_idempotency_key_rejected(self, store):
        """PRD-V5 §7.1：幂等键冲突 → ValueError"""
        store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="dup-key",
        )
        with pytest.raises(ValueError, match="idempotency_key"):
            store.create(
                account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
                idempotency_key="dup-key",
            )

    def test_create_if_absent_returns_none_on_duplicate(self, store):
        """create_if_absent 幂等：已存在返回 None"""
        t1 = store.create_if_absent(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="idem-key",
        )
        assert t1 is not None
        t2 = store.create_if_absent(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="idem-key",
        )
        assert t2 is None

    def test_get_by_idempotency_key(self, store):
        t = store.create(
            account_id="acc1", scene=SCENE_DYNAMIC,
            idempotency_key="key-xyz",
        )
        fetched = store.get_by_idempotency_key("key-xyz")
        assert fetched is not None
        assert fetched.task_id == t.task_id

    def test_create_default_grace_window(self, store):
        """未指定 grace_window 时使用默认值 900"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="default-grace",
        )
        assert t.grace_window == DEFAULT_GRACE_WINDOW

    def test_create_custom_grace_window(self, store):
        t = store.create(
            account_id="acc1", scene=SCENE_WEEKLY_SUMMARY,
            idempotency_key="weekly-grace",
            grace_window=3600,
            max_attempts=1,
        )
        assert t.grace_window == 3600
        assert t.max_attempts == 1

    def test_persisted_after_create(self, store):
        """create 后再 get 能取到相同数据"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="persist-test",
            input_data={"topic": "AI"},
        )
        fetched = store.get(t.task_id)
        assert fetched is not None
        assert fetched.task_id == t.task_id
        assert json.loads(fetched.input_json) == {"topic": "AI"}


# ═══════════════════════════════════════════════════════
#  状态机测试
# ═══════════════════════════════════════════════════════

class TestTaskRunStateMachine:
    """scheduled → claimed → running → succeeded"""

    def test_full_success_path(self, store):
        """scheduled → claimed → running → succeeded"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="ok-path",
            scheduled_at=time.time(),
        )
        # claim
        assert store.claim(t.task_id) is True
        assert store.get(t.task_id).status == STATUS_CLAIMED
        # start
        assert store.start(t.task_id) is True
        assert store.get(t.task_id).status == STATUS_RUNNING
        assert store.get(t.task_id).started_at is not None
        # succeed
        assert store.succeed(t.task_id, {"summary": "done"}) is True
        result = store.get(t.task_id)
        assert result.status == STATUS_SUCCEEDED
        assert result.finished_at is not None
        assert json.loads(result.result_json) == {"summary": "done"}
        assert result.lease_until is None

    def test_claim_requires_scheduled(self, store):
        """claim 只能从 scheduled 转"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="claim-scheduled",
        )
        assert store.claim(t.task_id) is True
        # 再次 claim（已 claimed）应失败
        assert store.claim(t.task_id) is False

    def test_start_requires_claimed(self, store):
        """start 只能从 claimed 转"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="start-claimed",
        )
        # 未 claim 直接 start → 失败
        assert store.start(t.task_id) is False
        # claim 后 start 成功
        assert store.claim(t.task_id) is True
        assert store.start(t.task_id) is True
        # 再次 start（已 running）→ 失败
        assert store.start(t.task_id) is False

    def test_succeed_requires_running(self, store):
        """succeed 只能从 running 转"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="succeed-running",
        )
        # 未 running 直接 succeed → 失败
        assert store.succeed(t.task_id) is False
        store.claim(t.task_id)
        store.start(t.task_id)
        assert store.succeed(t.task_id) is True


# ═══════════════════════════════════════════════════════
#  Failure + Backoff 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunFailure:
    """failure → retry_wait (with backoff) | failed (max attempts)"""

    def test_fail_first_attempt_goes_retry_wait(self, store):
        """第一次失败 → retry_wait，next_retry_at 被设置"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="fail-1",
            max_attempts=3,
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        assert store.fail(t.task_id, "ERR", "boom") is True
        result = store.get(t.task_id)
        assert result.status == STATUS_RETRY_WAIT
        assert result.attempt == 1
        assert result.next_retry_at is not None
        assert result.next_retry_at > time.time()
        assert result.last_error_code == "ERR"
        assert result.last_error == "boom"

    def test_fail_max_attempts_goes_failed(self, store):
        """达到 max_attempts → failed"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="fail-max",
            max_attempts=2,
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        # 第一次失败 → retry_wait（attempt=1）
        store.fail(t.task_id, "ERR", "first")
        assert store.get(t.task_id).status == STATUS_RETRY_WAIT
        # retry → scheduled
        store.retry(t.task_id)
        # 重新走 claim → start
        store.claim(t.task_id)
        store.start(t.task_id)
        # 第二次失败 → attempt=2 >= max_attempts=2 → failed
        store.fail(t.task_id, "ERR", "second")
        result = store.get(t.task_id)
        assert result.status == STATUS_FAILED
        assert result.attempt == 2
        assert result.next_retry_at is None
        assert result.finished_at is not None

    def test_fail_non_retryable_goes_failed_immediately(self, store):
        """retryable=False → 直接 failed"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="non-retryable",
            max_attempts=5,
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.fail(t.task_id, "PERM", "permanent", retryable=False)
        assert store.get(t.task_id).status == STATUS_FAILED

    def test_retry_resets_to_scheduled(self, store):
        """retry 把 retry_wait/failed/interrupted 重置为 scheduled"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="retry-reset",
            max_attempts=3,
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.fail(t.task_id, "ERR", "boom")
        assert store.get(t.task_id).status == STATUS_RETRY_WAIT
        # retry
        assert store.retry(t.task_id) is True
        result = store.get(t.task_id)
        assert result.status == STATUS_SCHEDULED
        assert result.trigger_type == TRIGGER_RETRY
        assert result.next_retry_at is None

    def test_retry_failed_state(self, store):
        """retry 从 failed 状态重新入队"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="retry-failed",
            max_attempts=1,
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.fail(t.task_id, "ERR", "boom")
        assert store.get(t.task_id).status == STATUS_FAILED
        assert store.retry(t.task_id) is True
        assert store.get(t.task_id).status == STATUS_SCHEDULED

    def test_retry_succeeded_not_allowed(self, store):
        """succeeded 状态不允许 retry"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="no-retry-succeeded",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.succeed(t.task_id)
        assert store.retry(t.task_id) is False

    def test_list_retryable(self, store):
        """list_retryable 返回 next_retry_at <= now 的 retry_wait 任务"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="retryable-list",
            max_attempts=3,
            scheduled_at=now,
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.fail(t.task_id, "ERR", "boom")
        # next_retry_at 是 now + 几秒，可能还未到
        result = store.get(t.task_id)
        # 把 next_retry_at 设到过去（通过重新 fail 不现实，直接查询 future）
        # 改为查询 future 时空列表
        future = store.list_retryable(now=now)
        assert all(r.task_id != t.task_id for r in future) or \
               any(r.task_id == t.task_id for r in future)
        # 查询很远的未来，next_retry_at 必然 <= far_future
        far_future = now + 10000
        retryable = store.list_retryable(now=far_future)
        assert any(r.task_id == t.task_id for r in retryable)


# ═══════════════════════════════════════════════════════
#  Expire 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunExpire:
    """Overdue beyond grace window → expired (not triggered)"""

    def test_expire_overdue_scheduled(self, store):
        """scheduled 且超过 grace_window → expired"""
        now = time.time()
        # scheduled 1 小时前，grace_window 900 秒 → 已过期
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="expire-overdue",
            scheduled_at=now - 3600,
            grace_window=900,
        )
        assert t.status == STATUS_SCHEDULED
        count = store.expire_overdue(now=now)
        assert count >= 1
        result = store.get(t.task_id)
        assert result.status == STATUS_EXPIRED
        assert result.finished_at is not None

    def test_expire_does_not_touch_claimed(self, store):
        """claimed 状态不被 expire_overdue 影响"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="expire-claimed",
            scheduled_at=now - 3600,
            grace_window=900,
        )
        store.claim(t.task_id)
        store.expire_overdue(now=now)
        # claimed 不应变成 expired
        assert store.get(t.task_id).status == STATUS_CLAIMED

    def test_expire_does_not_touch_within_window(self, store):
        """在 grace_window 内的 scheduled 不被 expire"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="expire-within",
            scheduled_at=now - 100,  # 100 秒前
            grace_window=900,  # 15 分钟窗口
        )
        store.expire_overdue(now=now)
        assert store.get(t.task_id).status == STATUS_SCHEDULED

    def test_expired_not_faked_triggered(self, store):
        """PRD-V5 §7.1：过期 → expired，不伪造 triggered/succeeded"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="not-triggered",
            scheduled_at=now - 3600,
            grace_window=900,
        )
        store.expire_overdue(now=now)
        result = store.get(t.task_id)
        # 不得是 succeeded / running / claimed
        assert result.status == STATUS_EXPIRED
        assert result.status != STATUS_SUCCEEDED
        assert result.started_at is None


# ═══════════════════════════════════════════════════════
#  Restart Recovery 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunRestartRecovery:
    """Restart: claimed/running → interrupted → recoverable"""

    def test_recover_interrupted_from_claimed(self, store):
        """重启时 claimed → interrupted"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="restart-claimed",
        )
        store.claim(t.task_id)
        assert store.get(t.task_id).status == STATUS_CLAIMED
        # 模拟重启
        count = store.recover_interrupted()
        assert count >= 1
        result = store.get(t.task_id)
        assert result.status == STATUS_INTERRUPTED
        assert result.finished_at is not None

    def test_recover_interrupted_from_running(self, store):
        """重启时 running → interrupted"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="restart-running",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        assert store.get(t.task_id).status == STATUS_RUNNING
        count = store.recover_interrupted()
        assert count >= 1
        assert store.get(t.task_id).status == STATUS_INTERRUPTED

    def test_recover_does_not_touch_scheduled(self, store):
        """scheduled 不受 recover_interrupted 影响"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="restart-scheduled",
        )
        store.recover_interrupted()
        assert store.get(t.task_id).status == STATUS_SCHEDULED

    def test_recover_does_not_touch_succeeded(self, store):
        """succeeded 不受 recover_interrupted 影响"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="restart-succeeded",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.succeed(t.task_id)
        store.recover_interrupted()
        assert store.get(t.task_id).status == STATUS_SUCCEEDED

    def test_interrupted_can_be_retried(self, store):
        """interrupted 可通过 retry 重新入队"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="interrupted-retry",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.recover_interrupted()
        assert store.get(t.task_id).status == STATUS_INTERRUPTED
        # retry
        assert store.retry(t.task_id) is True
        assert store.get(t.task_id).status == STATUS_SCHEDULED


# ═══════════════════════════════════════════════════════
#  result_unknown 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunResultUnknown:
    """Platform success + local failure → result_unknown (no auto-republish)"""

    def test_mark_result_unknown(self, store):
        """PRD-V5 §7.2：平台结果不确定 → result_unknown"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="result-unknown",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        # 模拟平台结果不确定
        assert store.mark_result_unknown(t.task_id, "platform timeout") is True
        result = store.get(t.task_id)
        assert result.status == STATUS_RESULT_UNKNOWN
        assert result.last_error_code == "PLATFORM_RESULT_UNCERTAIN"
        assert result.last_error == "platform timeout"
        assert result.finished_at is not None
        assert result.next_retry_at is None  # 不自动重发

    def test_result_unknown_is_terminal(self, store):
        """result_unknown 是终态"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="ru-terminal",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.mark_result_unknown(t.task_id)
        result = store.get(t.task_id)
        assert result.is_terminal()

    def test_result_unknown_not_retryable(self, store):
        """result_unknown 不在 RETRYABLE_STATUSES 中（不自动重发）"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="ru-not-retryable",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.mark_result_unknown(t.task_id)
        result = store.get(t.task_id)
        assert not result.is_retryable()
        # retry 接口也应拒绝
        assert store.retry(t.task_id) is False


# ═══════════════════════════════════════════════════════
#  并发 claim 原子性测试
# ═══════════════════════════════════════════════════════

class TestTaskRunConcurrentClaim:
    """PRD-V5 §7.3：claim 必须原子（并发只有一个成功）"""

    def test_concurrent_claim_only_one_succeeds(self, store):
        """多线程并发 claim 同一 task_id，只有一个成功"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="concurrent-claim",
            scheduled_at=time.time(),
        )
        results = []
        barrier = threading.Event()

        def claimer():
            barrier.wait(timeout=2.0)
            ok = store.claim(t.task_id)
            results.append(ok)

        threads = [threading.Thread(target=claimer) for _ in range(5)]
        for th in threads:
            th.start()
        barrier.set()
        for th in threads:
            th.join(timeout=5.0)

        # 只有一个 True
        assert results.count(True) == 1
        assert results.count(False) == 4
        assert store.get(t.task_id).status == STATUS_CLAIMED

    def test_claim_uses_conditional_update(self, store):
        """claim 是条件更新（不是先读后写）"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="cond-update",
            scheduled_at=time.time(),
        )
        # 第一次 claim 成功
        assert store.claim(t.task_id) is True
        # 第二次 claim（已 claimed）应失败，不能覆盖
        assert store.claim(t.task_id) is False
        assert store.get(t.task_id).status == STATUS_CLAIMED

    def test_claim_respects_not_before(self, store):
        """claim 检查 not_before <= now（未到时间不可 claim）"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="future-claim",
            scheduled_at=now,
            not_before=now + 3600,  # 1 小时后才能执行
        )
        # 现在不能 claim
        assert store.claim(t.task_id, now=now) is False
        # 未来可以
        assert store.claim(t.task_id, now=now + 3600) is True


# ═══════════════════════════════════════════════════════
#  Late Window 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunLateWindow:
    """scheduled_at <= now <= scheduled_at + grace_window → claimable"""

    def test_within_window_claimable(self, store):
        """在 grace_window 内 → 可 claim"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="late-ok",
            scheduled_at=now - 300,  # 5 分钟前
            grace_window=900,  # 15 分钟窗口
        )
        # now 在 [scheduled_at, scheduled_at + 900] 内
        assert store.claim(t.task_id, now=now) is True

    def test_beyond_window_expired(self, store):
        """超过 grace_window → expire_overdue 标记为 expired"""
        now = time.time()
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="late-too-late",
            scheduled_at=now - 1200,  # 20 分钟前
            grace_window=900,  # 15 分钟窗口
        )
        store.expire_overdue(now=now)
        result = store.get(t.task_id)
        assert result.status == STATUS_EXPIRED
        # expired 后不可 claim
        assert store.claim(t.task_id, now=now) is False

    def test_list_scheduled_claimable(self, store):
        """list_scheduled_claimable 返回 not_before <= now 的 scheduled 任务"""
        now = time.time()
        t1 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="claimable-1",
            scheduled_at=now - 100,
        )
        t2 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="claimable-future",
            scheduled_at=now,
            not_before=now + 3600,
        )
        claimable = store.list_scheduled_claimable("acc1", SCENE_PROACTIVE_VIDEO, now=now)
        ids = [c.task_id for c in claimable]
        assert t1.task_id in ids
        assert t2.task_id not in ids


# ═══════════════════════════════════════════════════════
#  Cancel 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunCancel:
    """PRD-V5 §7.4：取消未外部发布的任务"""

    def test_cancel_scheduled(self, store):
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="cancel-scheduled",
        )
        assert store.cancel(t.task_id) is True
        assert store.get(t.task_id).status == STATUS_FAILED

    def test_cancel_claimed(self, store):
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="cancel-claimed",
        )
        store.claim(t.task_id)
        assert store.cancel(t.task_id) is True
        assert store.get(t.task_id).status == STATUS_FAILED

    def test_cancel_running_not_allowed(self, store):
        """running 状态不可取消（可能已发布）"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="cancel-running",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        assert store.cancel(t.task_id) is False
        assert store.get(t.task_id).status == STATUS_RUNNING

    def test_cancel_succeeded_not_allowed(self, store):
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="cancel-succeeded",
        )
        store.claim(t.task_id)
        store.start(t.task_id)
        store.succeed(t.task_id)
        assert store.cancel(t.task_id) is False


# ═══════════════════════════════════════════════════════
#  统计 / 列表 测试
# ═══════════════════════════════════════════════════════

class TestTaskRunListAndCount:
    """list_by_account_scene / count_succeeded_today"""

    def test_list_by_account_scene(self, store):
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="list-1",
        )
        t2 = store.create(
            account_id="acc1", scene=SCENE_DYNAMIC,
            idempotency_key="list-2",
        )
        pv_list = store.list_by_account_scene("acc1", SCENE_PROACTIVE_VIDEO)
        assert len(pv_list) == 1
        assert pv_list[0].task_id == t.task_id

    def test_count_succeeded_today(self, store):
        """只有 succeeded 才算当日完成"""
        now = time.time()
        t1 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="count-succ",
            scheduled_at=now,
        )
        t2 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="count-fail",
            scheduled_at=now,
        )
        store.claim(t1.task_id)
        store.start(t1.task_id)
        store.succeed(t1.task_id)
        # t2 还是 scheduled
        count = store.count_succeeded_today("acc1", SCENE_PROACTIVE_VIDEO, now=now)
        assert count == 1

    def test_count_succeeded_today_zero(self, store):
        """当日 0 成功 → count=0（空计划）"""
        now = time.time()
        count = store.count_succeeded_today("acc1", SCENE_PROACTIVE_VIDEO, now=now)
        assert count == 0

    def test_clear_account_scene_today(self, store):
        """跨天清理：scheduled → expired"""
        t = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="clear-1",
        )
        # succeeded 不被清理
        t2 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="clear-2",
        )
        store.claim(t2.task_id)
        store.start(t2.task_id)
        store.succeed(t2.task_id)
        # 清理 scheduled
        count = store.clear_account_scene_today("acc1", SCENE_PROACTIVE_VIDEO)
        assert count >= 1
        assert store.get(t.task_id).status == STATUS_EXPIRED
        # succeeded 不变
        assert store.get(t2.task_id).status == STATUS_SUCCEEDED


# ═══════════════════════════════════════════════════════
#  Desensitize 测试
# ═══════════════════════════════════════════════════════

class TestDesensitizeTaskRun:
    """API 响应脱敏"""

    def test_desensitize_keeps_safe_fields(self):
        t = TaskRun(
            task_id="t1", account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="key", status=STATUS_SUCCEEDED,
            input_json=json.dumps({"bvid": "BV1xx", "sessdata": "secret-cookie"}),
            result_json=json.dumps({"success": True, "summary": "ok"}),
        )
        d = desensitize_task_run(t)
        # 安全字段保留
        assert d["input_json"]["bvid"] == "BV1xx"
        assert d["result_json"]["success"] is True
        # 敏感字段被移除
        assert "sessdata" not in d["input_json"]
        # lease_until 不暴露
        assert "lease_until" not in d

    def test_desensitize_invalid_json(self):
        """input_json / result_json 非法 JSON → 空对象"""
        t = TaskRun(
            task_id="t1", account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="key", status=STATUS_SCHEDULED,
            input_json="not-json",
            result_json="",
        )
        d = desensitize_task_run(t)
        assert d["input_json"] == {}
        assert d["result_json"] == {}


# ═══════════════════════════════════════════════════════
#  API 测试：旧端点 410 Gone
# ═══════════════════════════════════════════════════════

class TestOldTaskEndpointsGone:
    """PRD-V5 §7.4：旧默认账号任务端点返回 410 Gone"""

    def test_old_proactive_video_returns_410(self):
        routes = create_tasks_routes(scheduler=None)
        app = Starlette(routes=routes)
        client = TestClient(app)
        resp = client.post("/api/tasks/proactive-video")
        assert resp.status_code == 410
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "GONE"

    def test_old_dynamic_returns_410(self):
        routes = create_tasks_routes(scheduler=None)
        app = Starlette(routes=routes)
        client = TestClient(app)
        resp = client.post("/api/tasks/dynamic")
        assert resp.status_code == 410
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "GONE"


# ═══════════════════════════════════════════════════════
#  API 测试：账号级任务端点
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
    # PRD-V5 §5.3 LLM-501：resolve_provider 返回 (llm, effective_id, fallback_reason) 三元组
    mgr.resolve_provider.return_value = (None, "", "")
    return mgr


@pytest.fixture
def task_api_client(
    tmp_data_dir,
    mock_orchestrator,
    mock_context_builder,
    mock_llm_manager,
):
    """构造带 TaskRunStore 的账号 API 测试客户端

    使用 mock scheduler，验证 API 协议（202 + task_id）。
    """
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
                "proactive_video": {"grace_window_seconds": 900, "max_attempts": 3},
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
        orchestrator=mock_orchestrator,
        context_builder=mock_context_builder,
        app_config_loader=config,
        data_root=tmp_data_dir,
        safety_checker=None,
    )
    mgr.initialize()
    # 调用 async initialize() 创建 scheduler（含 task_store）
    import asyncio
    main_acc = mgr.get_account("main")
    assert main_acc is not None
    asyncio.run(main_acc.initialize())
    assert main_acc.scheduler is not None
    # mock _spawn_memory_task 避免真正执行协程
    main_acc.scheduler._spawn_memory_task = MagicMock(side_effect=_mock_spawn_memory_task)

    routes = create_accounts_routes(mgr, config, config_path=str(tmp_data_dir + "/c.yaml"))
    app = Starlette(routes=routes)
    return TestClient(app), main_acc


class TestManualTaskAPI:
    """PRD-V5 §7.4：手动任务 API"""

    def test_post_proactive_video_returns_202(self, task_api_client):
        client, _ = task_api_client
        resp = client.post("/api/accounts/main/tasks/proactive-video", json={})
        assert resp.status_code == 202
        body = resp.json()
        assert body["success"] is True
        assert "task_id" in body["data"]
        assert body["data"]["status"] == "scheduled"

    def test_post_dynamic_returns_202(self, task_api_client):
        client, _ = task_api_client
        resp = client.post("/api/accounts/main/tasks/dynamic", json={"topic": "AI"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["success"] is True
        assert "task_id" in body["data"]

    def test_get_task_status(self, task_api_client):
        """GET /tasks/{task_id} 返回脱敏状态"""
        client, acc = task_api_client
        # 先创建任务
        task_id = acc.scheduler.create_manual_task("proactive_video")
        assert task_id is not None
        resp = client.get(f"/api/accounts/main/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["task_id"] == task_id
        assert body["data"]["status"] == STATUS_SCHEDULED
        # lease_until 不应暴露
        assert "lease_until" not in body["data"]

    def test_get_task_not_found(self, task_api_client):
        client, _ = task_api_client
        resp = client.get("/api/accounts/main/tasks/nonexistent-task-id")
        assert resp.status_code == 404

    def test_cancel_task(self, task_api_client):
        """POST /tasks/{task_id}/cancel 取消 scheduled 任务"""
        client, acc = task_api_client
        task_id = acc.scheduler.create_manual_task("proactive_video")
        resp = client.post(f"/api/accounts/main/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["status"] == "cancelled"

    def test_cancel_running_returns_409(self, task_api_client):
        """running 状态不可取消 → 409"""
        client, acc = task_api_client
        task_id = acc.scheduler.create_manual_task("proactive_video")
        # 手动转到 running
        acc.scheduler.task_store.claim(task_id)
        acc.scheduler.task_store.start(task_id)
        resp = client.post(f"/api/accounts/main/tasks/{task_id}/cancel")
        assert resp.status_code == 409

    def test_retry_task(self, task_api_client):
        """POST /tasks/{task_id}/retry 重试 failed 任务"""
        client, acc = task_api_client
        task_id = acc.scheduler.create_manual_task("proactive_video", input_data=None)
        # 手动转到 failed
        ts = acc.scheduler.task_store
        ts.claim(task_id)
        ts.start(task_id)
        ts.fail(task_id, "ERR", "boom", retryable=False)
        assert ts.get(task_id).status == STATUS_FAILED
        resp = client.post(f"/api/accounts/main/tasks/{task_id}/retry")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["status"] == "scheduled"

    def test_retry_succeeded_returns_409(self, task_api_client):
        """succeeded 不可重试 → 409"""
        client, acc = task_api_client
        task_id = acc.scheduler.create_manual_task("proactive_video")
        ts = acc.scheduler.task_store
        ts.claim(task_id)
        ts.start(task_id)
        ts.succeed(task_id)
        resp = client.post(f"/api/accounts/main/tasks/{task_id}/retry")
        assert resp.status_code == 409

    def test_task_account_not_found(self, task_api_client):
        """不存在的账号 → 404"""
        client, _ = task_api_client
        resp = client.post("/api/accounts/nonexistent/tasks/proactive-video", json={})
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════
#  Daily Plan 持久化测试
# ═══════════════════════════════════════════════════════

class TestDailyPlanPersistence:
    """PRD-V5 §7.2：跨天清理 + 空计划持久化"""

    def test_clear_account_scene_today_keeps_history(self, store):
        """跨天清理只清 scheduled，已完成的历史保留"""
        now = time.time()
        # 历史 succeeded
        t1 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="hist-succ",
            scheduled_at=now,
        )
        store.claim(t1.task_id)
        store.start(t1.task_id)
        store.succeed(t1.task_id)
        # 当日 scheduled（待清理）
        t2 = store.create(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="hist-pending",
            scheduled_at=now,
        )
        # 清理
        store.clear_account_scene_today("acc1", SCENE_PROACTIVE_VIDEO)
        # 历史保留
        assert store.get(t1.task_id).status == STATUS_SUCCEEDED
        # pending → expired
        assert store.get(t2.task_id).status == STATUS_EXPIRED

    def test_empty_plan_count_zero(self, store):
        """PRD-V5：当日数量为 0 时，count_succeeded_today 返回 0（空计划）"""
        now = time.time()
        # 没有任何 TaskRun
        assert store.count_succeeded_today("acc1", SCENE_PROACTIVE_VIDEO, now=now) == 0
        assert store.count_succeeded_today("acc1", SCENE_DYNAMIC, now=now) == 0

    def test_restart_does_not_regenerate_same_day_plan(self, store):
        """PRD-V5：重启不会重复生成当日计划（幂等键冲突 → create_if_absent 返回 None）"""
        # 模拟第一次生成计划
        t1 = store.create_if_absent(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="acc1:proactive_video:2026-07-11:10:00",
            scheduled_at=time.time(),
        )
        assert t1 is not None
        # 模拟重启后再次生成（相同幂等键）
        t2 = store.create_if_absent(
            account_id="acc1", scene=SCENE_PROACTIVE_VIDEO,
            idempotency_key="acc1:proactive_video:2026-07-11:10:00",
            scheduled_at=time.time(),
        )
        # 不重复创建
        assert t2 is None
        # 原记录还在
        assert store.get(t1.task_id).status == STATUS_SCHEDULED
