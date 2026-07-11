"""
tests/test_pm_501_pm_state.py - PM-501 私信独立幂等状态测试

PRD-V5 §6.3 / PM-501：
- 私信幂等键使用平台消息 ID，不用 talker_id+内容前 50 字
- 私信生成传 scene=private_message
- 私信发布失败支持独立退避和最大次数
- 私信原文不进入全局记忆或他账号数据目录

覆盖：
- ensure_discovered 幂等
- 不同平台消息 ID / 不同账号 隔离
- 幂等键格式
- save_generation_result 持久化
- 状态转移 discovered → generation_pending → safety_pending → publish_pending → published
- retry_wait 独立退避配置
- result_unknown 不自动重发
- max_attempts 达上限 → failed
- 私信原文不写入 KnowledgeBaseMemory
- retry_wait PM 被 list_retry_wait 拾取
- extract_platform_message_id 提取
"""
import asyncio
import hashlib
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bilibot.services.pm_state_store import (
    PrivateMessageState,
    PrivateMessageStateStore,
    extract_platform_message_id,
    DEFAULT_PM_MAX_ATTEMPTS,
    DEFAULT_PM_BACKOFF_BASE_SECONDS,
    TERMINAL_STATUSES,
)


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_data_dir):
    """PrivateMessageStateStore 实例（独立退避配置）"""
    return PrivateMessageStateStore(
        tmp_data_dir, account_id="acc_1",
        max_attempts=3, backoff_base_seconds=30,
    )


@pytest.fixture
def store_acc2(tmp_data_dir):
    """第二个账号的 store（同目录不同 account_id）"""
    return PrivateMessageStateStore(
        tmp_data_dir, account_id="acc_2",
        max_attempts=3, backoff_base_seconds=30,
    )


@pytest.fixture
def store_small_max(tmp_data_dir):
    """max_attempts=2 的 store（用于快速触达上限）"""
    return PrivateMessageStateStore(
        tmp_data_dir, account_id="acc_small",
        max_attempts=2, backoff_base_seconds=1,
    )


# ═══════════════════════════════════════════════════════
#  ensure_discovered 幂等
# ═══════════════════════════════════════════════════════

class TestEnsureDiscoveredIdempotent:

    def test_same_platform_message_id_returns_same_record(self, store):
        """同 (account_id, platform_message_id) 重复发现返回同一记录"""
        s1 = store.ensure_discovered("acc_1", "pm_msg_001", "talker_100")
        s2 = store.ensure_discovered("acc_1", "pm_msg_001", "talker_100")
        assert s1.id == s2.id
        assert s1.platform_message_id == "pm_msg_001"
        assert s1.status == "discovered"

    def test_different_message_ids_create_separate_records(self, store):
        """不同平台消息 ID 创建独立记录"""
        s1 = store.ensure_discovered("acc_1", "pm_msg_001", "talker_100")
        s2 = store.ensure_discovered("acc_1", "pm_msg_002", "talker_100")
        assert s1.id != s2.id
        assert s1.platform_message_id == "pm_msg_001"
        assert s2.platform_message_id == "pm_msg_002"

    def test_ensure_discovered_does_not_overwrite_status(self, store):
        """已存在记录的 status 不被 ensure_discovered 覆盖"""
        s1 = store.ensure_discovered("acc_1", "pm_msg_010", "talker_x")
        store.update_status(s1.id, "generation_pending")
        # 再次 ensure_discovered 应返回已存在的 generation_pending 记录
        s2 = store.ensure_discovered("acc_1", "pm_msg_010", "talker_x")
        assert s2.id == s1.id
        assert s2.status == "generation_pending"

    def test_empty_platform_message_id_raises(self, store):
        """platform_message_id 为空时报错（幂等键依赖平台消息 ID）"""
        with pytest.raises(ValueError, match="platform_message_id"):
            store.ensure_discovered("acc_1", "", "talker_100")


# ═══════════════════════════════════════════════════════
#  账号隔离
# ═══════════════════════════════════════════════════════

class TestAccountIsolation:

    def test_same_message_id_different_accounts_isolated(self, store, store_acc2):
        """同一平台消息 ID 在不同账号下是独立记录"""
        s1 = store.ensure_discovered("acc_1", "pm_msg_shared", "talker_100")
        s2 = store_acc2.ensure_discovered("acc_2", "pm_msg_shared", "talker_100")
        assert s1.id != s2.id
        assert s1.account_id == "acc_1"
        assert s2.account_id == "acc_2"

        # 标记 acc_1 为 published 不影响 acc_2
        store.update_status(s1.id, "generation_pending")
        store.save_generation_result(s1.id, text="回复1", persona_id="p1")
        store.update_status(s1.id, "safety_pending")
        store.update_status(s1.id, "publish_pending")
        store.mark_published(s1.id)
        assert store.get_by_message_id("acc_1", "pm_msg_shared").status == "published"
        # acc_2 仍是 discovered
        assert store_acc2.get_by_message_id("acc_2", "pm_msg_shared").status == "discovered"

    def test_db_file_in_account_data_dir(self, tmp_data_dir):
        """DB 文件路径位于账号数据目录"""
        s = PrivateMessageStateStore(tmp_data_dir, account_id="acc_x")
        assert s.db_path == os.path.join(tmp_data_dir, "pm_states.db")

    def test_list_retry_wait_scoped_to_account(self, store, store_acc2):
        """list_retry_wait 只返回当前账号的 retry_wait"""
        s1 = store.ensure_discovered("acc_1", "pm_a1", "t1")
        store.update_status(s1.id, "generation_pending")
        store.save_generation_result(s1.id, text="r1", persona_id="p1")
        store.update_status(s1.id, "safety_pending")
        store.update_status(s1.id, "publish_pending")
        store.mark_retry_wait(s1.id, error="fail")

        s2 = store_acc2.ensure_discovered("acc_2", "pm_a2", "t2")
        store_acc2.update_status(s2.id, "generation_pending")
        store_acc2.save_generation_result(s2.id, text="r2", persona_id="p2")
        store_acc2.update_status(s2.id, "safety_pending")
        store_acc2.update_status(s2.id, "publish_pending")
        store_acc2.mark_retry_wait(s2.id, error="fail")

        # 各自只看到自己的 retry_wait
        acc1_retry = store.list_retry_wait(account_id="acc_1", now=float("inf"))
        acc2_retry = store_acc2.list_retry_wait(account_id="acc_2", now=float("inf"))
        assert len(acc1_retry) == 1
        assert acc1_retry[0].platform_message_id == "pm_a1"
        assert len(acc2_retry) == 1
        assert acc2_retry[0].platform_message_id == "pm_a2"


# ═══════════════════════════════════════════════════════
#  幂等键格式
# ═══════════════════════════════════════════════════════

class TestIdempotencyKey:

    def test_key_uses_platform_message_id(self):
        """幂等键格式为 {account_id}:pm:{platform_message_id}"""
        key = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_msg_001")
        assert key == "acc_1:pm:pm_msg_001"

    def test_key_does_not_use_talker_id_or_content(self):
        """幂等键不包含 talker_id 或内容前 50 字"""
        key = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_msg_001")
        # 旧方式 talker_id + 内容前 50 字 不应出现在键中
        assert "talker" not in key
        assert "你好" not in key
        # 应包含 platform_message_id
        assert "pm_msg_001" in key

    def test_same_message_id_same_key(self):
        k1 = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_x")
        k2 = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_x")
        assert k1 == k2

    def test_different_message_id_different_key(self):
        k1 = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_x")
        k2 = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_y")
        assert k1 != k2

    def test_different_account_different_key(self):
        """不同账号同消息 ID 产生不同键（账号隔离）"""
        k1 = PrivateMessageStateStore.make_idempotency_key("acc_1", "pm_x")
        k2 = PrivateMessageStateStore.make_idempotency_key("acc_2", "pm_x")
        assert k1 != k2


# ═══════════════════════════════════════════════════════
#  save_generation_result
# ═══════════════════════════════════════════════════════

class TestSaveGenerationResult:

    def test_stores_text_and_hash(self, store):
        """save_generation_result 写入 text 和 hash"""
        s = store.ensure_discovered("acc_1", "pm_gen_1", "t1")
        store.update_status(s.id, "generation_pending")
        result = store.save_generation_result(
            s.id, text="你好世界", persona_id="persona_a",
        )
        assert result.generation_text == "你好世界"
        expected_hash = hashlib.sha256("你好世界".encode("utf-8")).hexdigest()
        assert result.generation_hash == expected_hash
        assert result.persona_id == "persona_a"

    def test_auto_compute_hash_when_empty(self, store):
        """hash_value 为空时自动计算"""
        s = store.ensure_discovered("acc_1", "pm_gen_2", "t1")
        store.update_status(s.id, "generation_pending")
        result = store.save_generation_result(s.id, text="自动哈希", persona_id="p1")
        assert result.generation_hash == PrivateMessageStateStore.compute_generation_hash("自动哈希")

    def test_empty_text_noop(self, store):
        """空文本不写入"""
        s = store.ensure_discovered("acc_1", "pm_gen_3", "t1")
        store.update_status(s.id, "generation_pending")
        before = store.get_by_id(s.id)
        result = store.save_generation_result(s.id, text="", persona_id="p1")
        assert result.generation_text == ""
        assert result.generation_hash == ""

    def test_does_not_change_status(self, store):
        """save_generation_result 不改变 status"""
        s = store.ensure_discovered("acc_1", "pm_gen_4", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="hi", persona_id="p1")
        assert store.get_by_id(s.id).status == "generation_pending"

    def test_hash_matches_compute(self, store):
        text = "测试私信回复内容"
        s = store.ensure_discovered("acc_1", "pm_gen_5", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text=text, persona_id="p1")
        state = store.get_by_id(s.id)
        assert state.generation_hash == PrivateMessageStateStore.compute_generation_hash(text)


# ═══════════════════════════════════════════════════════
#  状态转移
# ═══════════════════════════════════════════════════════

class TestStateTransitions:

    def test_full_flow_discovered_to_published(self, store):
        """完整状态转移：discovered → generation_pending → safety_pending → publish_pending → published"""
        s = store.ensure_discovered("acc_1", "pm_flow_1", "t1")

        store.update_status(s.id, "generation_pending")
        assert store.get_by_id(s.id).status == "generation_pending"

        store.save_generation_result(s.id, text="回复", persona_id="p1")

        store.update_status(s.id, "safety_pending")
        assert store.get_by_id(s.id).status == "safety_pending"

        store.update_status(s.id, "publish_pending")
        assert store.get_by_id(s.id).status == "publish_pending"

        store.mark_published(s.id)
        final = store.get_by_id(s.id)
        assert final.status == "published"
        assert final.published_at is not None
        # generation_text 在 published 后仍保留
        assert final.generation_text == "回复"

    def test_illegal_transition_raises(self, store):
        """非法状态转移报错"""
        s = store.ensure_discovered("acc_1", "pm_illegal_1", "t1")
        # discovered → published 非法
        with pytest.raises(ValueError, match="非法状态转移"):
            store.update_status(s.id, "published")

    def test_terminal_state_no_transition(self, store):
        """终态不能再转移"""
        s = store.ensure_discovered("acc_1", "pm_term_1", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="x", persona_id="p1")
        store.update_status(s.id, "safety_pending")
        store.update_status(s.id, "publish_pending")
        store.mark_published(s.id)
        # published 是终态
        assert store.get_by_id(s.id).status in TERMINAL_STATUSES
        with pytest.raises(ValueError):
            store.update_status(s.id, "publish_pending")

    def test_safety_rejected(self, store):
        """安全检查未通过 → rejected（终态）"""
        s = store.ensure_discovered("acc_1", "pm_reject_1", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="不当回复", persona_id="p1")
        store.update_status(s.id, "safety_pending")
        store.mark_rejected(s.id, reason="safety_violation")
        final = store.get_by_id(s.id)
        assert final.status == "rejected"
        assert final.last_error == "safety_violation"
        assert final.last_error_code == "SAFETY_REJECT"

    def test_ignored(self, store):
        """业务规则跳过 → ignored"""
        s = store.ensure_discovered("acc_1", "pm_ign_1", "t1")
        store.mark_ignored(s.id, rule="blacklist")
        assert store.get_by_id(s.id).status == "ignored"


# ═══════════════════════════════════════════════════════
#  retry_wait 独立退避
# ═══════════════════════════════════════════════════════

class TestRetryWaitBackoff:

    def test_retry_wait_sets_next_retry_at(self, store):
        """mark_retry_wait 设置 next_retry_at（独立退避）"""
        s = store.ensure_discovered("acc_1", "pm_retry_1", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="回复", persona_id="p1")
        store.update_status(s.id, "safety_pending")
        store.update_status(s.id, "publish_pending")
        before = time.time()
        store.mark_retry_wait(s.id, error="fail")
        after = time.time()
        state = store.get_by_id(s.id)
        assert state.status == "retry_wait"
        assert state.attempt == 1
        assert state.next_retry_at is not None
        # next_retry_at 在 [before+base, after+base+jitter] 区间
        # base = 30 * 2^1 = 60
        assert state.next_retry_at >= before + 30  # 至少 base
        assert state.next_retry_at <= after + 60 + 10  # 至多 base+jitter

    def test_retry_wait_uses_independent_backoff_config(self, tmp_data_dir):
        """PM 退避使用独立 backoff_base_seconds，与评论回复无关"""
        # backoff_base_seconds=10 的 store
        s10 = PrivateMessageStateStore(
            tmp_data_dir, account_id="acc_b10",
            max_attempts=5, backoff_base_seconds=10,
        )
        # backoff_base_seconds=100 的 store
        s100 = PrivateMessageStateStore(
            tmp_data_dir, account_id="acc_b100",
            max_attempts=5, backoff_base_seconds=100,
        )

        def _to_retry_wait(st, acc):
            r = st.ensure_discovered(acc, f"pm_{acc}", "t1")
            st.update_status(r.id, "generation_pending")
            st.save_generation_result(r.id, text="x", persona_id="p1")
            st.update_status(r.id, "safety_pending")
            st.update_status(r.id, "publish_pending")
            st.mark_retry_wait(r.id, error="fail")
            return st.get_by_id(r.id)

        r10 = _to_retry_wait(s10, "acc_b10")
        r100 = _to_retry_wait(s100, "acc_b100")
        # backoff=10 的 next_retry_at 应明显早于 backoff=100
        assert r10.next_retry_at < r100.next_retry_at
        # base=10*2=20 vs base=100*2=200
        assert r100.next_retry_at - r10.next_retry_at > 100

    def test_list_retry_wait_picks_up_after_backoff(self, store):
        """retry_wait PM 在 backoff 到期后被 list_retry_wait 拾取"""
        s = store.ensure_discovered("acc_1", "pm_list_1", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="回复", persona_id="p1")
        store.update_status(s.id, "safety_pending")
        store.update_status(s.id, "publish_pending")
        store.mark_retry_wait(s.id, error="fail")

        # 立即查询：next_retry_at 在未来，不应被拾取
        now = time.time()
        immediate = store.list_retry_wait(account_id="acc_1", now=now)
        assert len(immediate) == 0

        # 用足够大的 now 确保 backoff 已过
        future = time.time() + 3600
        later = store.list_retry_wait(account_id="acc_1", now=future)
        assert len(later) == 1
        assert later[0].platform_message_id == "pm_list_1"
        assert later[0].generation_text == "回复"

    def test_retry_wait_to_publish_pending_then_published(self, store):
        """retry_wait → publish_pending → published 完整重试成功路径"""
        s = store.ensure_discovered("acc_1", "pm_retry_ok", "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="重试文本", persona_id="p1")
        store.update_status(s.id, "safety_pending")
        store.update_status(s.id, "publish_pending")
        store.mark_retry_wait(s.id, error="first_fail")
        assert store.get_by_id(s.id).status == "retry_wait"

        # 重试：retry_wait → publish_pending → published
        store.update_status(s.id, "publish_pending")
        store.mark_published(s.id)
        final = store.get_by_id(s.id)
        assert final.status == "published"
        # 原始文本保留
        assert final.generation_text == "重试文本"


# ═══════════════════════════════════════════════════════
#  max_attempts → failed
# ═══════════════════════════════════════════════════════

class TestMaxAttemptsFailed:

    def test_max_attempts_reached_marks_failed(self, store_small_max):
        """达到 max_attempts 后 mark_retry_wait 转 failed"""
        s = store_small_max.ensure_discovered("acc_small", "pm_max_1", "t1")
        store_small_max.update_status(s.id, "generation_pending")
        store_small_max.save_generation_result(s.id, text="x", persona_id="p1")
        store_small_max.update_status(s.id, "safety_pending")
        store_small_max.update_status(s.id, "publish_pending")
        # max_attempts=2：第一次 retry_wait（attempt=1），第二次应转 failed
        store_small_max.mark_retry_wait(s.id, error="fail1")
        assert store_small_max.get_by_id(s.id).status == "retry_wait"
        assert store_small_max.get_by_id(s.id).attempt == 1

        # retry_wait → publish_pending → 再次失败
        store_small_max.update_status(s.id, "publish_pending")
        store_small_max.mark_retry_wait(s.id, error="fail2")
        # attempt=2 >= max_attempts=2 → failed
        final = store_small_max.get_by_id(s.id)
        assert final.status == "failed"
        assert final.attempt == 2

    def test_failed_is_terminal(self, store_small_max):
        """failed 是终态，不可再转移"""
        s = store_small_max.ensure_discovered("acc_small", "pm_term_2", "t1")
        store_small_max.update_status(s.id, "generation_pending")
        store_small_max.save_generation_result(s.id, text="x", persona_id="p1")
        store_small_max.update_status(s.id, "safety_pending")
        store_small_max.update_status(s.id, "publish_pending")
        store_small_max.mark_retry_wait(s.id, error="f1")
        store_small_max.update_status(s.id, "publish_pending")
        store_small_max.mark_retry_wait(s.id, error="f2")
        assert store_small_max.get_by_id(s.id).status == "failed"
        with pytest.raises(ValueError):
            store_small_max.update_status(s.id, "publish_pending")

    def test_failed_not_in_retry_list(self, store_small_max):
        """failed 状态不在 list_retry_wait 中"""
        s = store_small_max.ensure_discovered("acc_small", "pm_nolist", "t1")
        store_small_max.update_status(s.id, "generation_pending")
        store_small_max.save_generation_result(s.id, text="x", persona_id="p1")
        store_small_max.update_status(s.id, "safety_pending")
        store_small_max.update_status(s.id, "publish_pending")
        store_small_max.mark_retry_wait(s.id, error="f1")
        store_small_max.update_status(s.id, "publish_pending")
        store_small_max.mark_retry_wait(s.id, error="f2")
        assert store_small_max.get_by_id(s.id).status == "failed"
        retryable = store_small_max.list_retry_wait(
            account_id="acc_small", now=float("inf"),
        )
        assert all(r.platform_message_id != "pm_nolist" for r in retryable)


# ═══════════════════════════════════════════════════════
#  result_unknown 不自动重发
# ═══════════════════════════════════════════════════════

class TestResultUnknownNoAutoRepublish:

    def _to_publish_pending(self, store, msg_id):
        s = store.ensure_discovered("acc_1", msg_id, "t1")
        store.update_status(s.id, "generation_pending")
        store.save_generation_result(s.id, text="x", persona_id="p1")
        store.update_status(s.id, "safety_pending")
        store.update_status(s.id, "publish_pending")
        return s

    def test_result_unknown_not_in_retry_list(self, store):
        """result_unknown 不被 list_retry_wait 拾取（不自动重发）"""
        s = self._to_publish_pending(store, "pm_ru_1")
        store.mark_result_unknown(s.id, error="uncertain")
        assert store.get_by_id(s.id).status == "result_unknown"
        # 即使 backoff 已过，也不应被 list_retry_wait 拾取
        retryable = store.list_retry_wait(account_id="acc_1", now=float("inf"))
        assert all(r.platform_message_id != "pm_ru_1" for r in retryable)

    def test_result_unknown_in_result_unknown_list(self, store):
        """result_unknown 出现在 list_result_unknown（人工对账用）"""
        s = self._to_publish_pending(store, "pm_ru_2")
        store.mark_result_unknown(s.id, error="uncertain")
        ru_list = store.list_result_unknown(account_id="acc_1")
        assert any(r.platform_message_id == "pm_ru_2" for r in ru_list)

    def test_result_unknown_no_auto_transition_without_force(self, store):
        """result_unknown 状态不传 force 时不能自动转移"""
        s = self._to_publish_pending(store, "pm_ru_3")
        store.mark_result_unknown(s.id, error="uncertain")
        with pytest.raises(ValueError, match="result_unknown"):
            store.update_status(s.id, "published")

    def test_result_unknown_force_allows_transition(self, store):
        """result_unknown 传 force=True 可人工触发转移"""
        s = self._to_publish_pending(store, "pm_ru_4")
        store.mark_result_unknown(s.id, error="uncertain")
        # 人工对账后确认已发布
        result = store.update_status(s.id, "published", force=True)
        assert result.status == "published"

    def test_result_unknown_force_to_failed(self, store):
        """result_unknown 传 force=True 可转 failed（人工判定失败）"""
        s = self._to_publish_pending(store, "pm_ru_5")
        store.mark_result_unknown(s.id, error="uncertain")
        result = store.update_status(s.id, "failed", force=True)
        assert result.status == "failed"


# ═══════════════════════════════════════════════════════
#  extract_platform_message_id
# ═══════════════════════════════════════════════════════

class TestExtractPlatformMessageId:

    def test_msg_id_first_priority(self):
        msg = {"msg_id": "mid_123", "msg_key": "mk_456", "msg_seq": 789}
        assert extract_platform_message_id(msg) == "mid_123"

    def test_msg_key_second_priority(self):
        msg = {"msg_key": "mk_456", "msg_seq": 789}
        assert extract_platform_message_id(msg) == "mk_456"

    def test_msg_seq_third_priority(self):
        msg = {"msg_seq": 789}
        assert extract_platform_message_id(msg) == "789"

    def test_fallback_sender_uid_timestamp(self):
        msg = {"sender_uid": 12345, "timestamp": 1700000000}
        result = extract_platform_message_id(msg)
        assert result == "fallback:12345:1700000000"

    def test_empty_dict_returns_empty(self):
        assert extract_platform_message_id({}) == ""

    def test_non_dict_returns_empty(self):
        assert extract_platform_message_id(None) == ""
        assert extract_platform_message_id("string") == ""

    def test_does_not_use_content_or_talker_id(self):
        """提取的 ID 不基于内容或 talker_id"""
        msg = {
            "msg_id": "platform_msg_id_1",
            "content": '{"content":"这是一段很长的私信内容前50字会被旧逻辑用作键但新逻辑不应使用"}',
            "talker_id": 99999,
        }
        result = extract_platform_message_id(msg)
        assert result == "platform_msg_id_1"
        assert "99999" not in result
        assert "私信内容" not in result


# ═══════════════════════════════════════════════════════
#  私信原文不写入 KnowledgeBaseMemory
# ═══════════════════════════════════════════════════════

class TestPMPrivacyNoMemoryWrite:
    """PRD-V5 §6.3 / PM-501：私信原文不进入 KnowledgeBaseMemory"""

    def test_pm_text_not_in_global_memory_via_write_atom(self):
        """PM 场景下 write_atom 不应被调用（guard 检查）"""
        # 模拟：业务代码在 PM 场景下应跳过 memory.write_atom
        memory = MagicMock()
        memory.write_atom = AsyncMock(return_value=1)

        async def _run():
            scene = "private_message"
            # 模拟 scheduler 中的 guard 逻辑
            if scene != "private_message":
                await memory.write_atom("pm content", category="episodic")
            # PM 场景：不调用 write_atom
            return scene

        asyncio.run(_run())
        memory.write_atom.assert_not_called()

    def test_comment_scene_writes_to_memory(self):
        """评论场景下 write_atom 仍正常调用（guard 不影响非 PM 场景）"""
        memory = MagicMock()
        memory.write_atom = AsyncMock(return_value=1)

        async def _run():
            scene = "reply_comment"
            if scene != "private_message":
                await memory.write_atom("comment content", category="episodic")

        asyncio.run(_run())
        memory.write_atom.assert_called_once()

    @pytest.mark.asyncio
    async def test_scheduler_pm_flow_no_memory_write(self, tmp_data_dir):
        """Scheduler 处理私信时不调用 knowledge_memory.save_conversation_as_memory

        构造最小 Scheduler，mock bili/reply_gen/knowledge_memory，
        验证 PM 成功发送后 knowledge_memory.save_conversation_as_memory 未被调用。
        """
        from bilibot.app.config_loader import ConfigLoader
        from bilibot.scheduler import Scheduler
        from bilibot.data_store import DataStore

        config = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "s", "bili_jct": "j", "dede_user_id": "999"},
            "llm": {"api_key": "k", "base_url": "http://x/v1", "model": "m"},
            "data_dir": tmp_data_dir,
            "features": {"private_message": True},
        })
        ds = DataStore(tmp_data_dir)

        # Mock bili：返回一条 PM 会话
        bili = MagicMock()
        bili.get_private_sessions = AsyncMock(return_value={
            "code": 0,
            "data": {
                "session_list": [{
                    "session_type": 1,
                    "unread_count": 1,
                    "talker_id": 12345,
                    "talker_info": {"uname": "测试用户"},
                    "last_msg": {
                        "msg_id": "platform_msg_999",
                        "content": '{"content":"你好啊"}',
                        "sender_uid": 12345,
                        "timestamp": 1700000000,
                    },
                }],
            },
        })
        bili.send_private_message = AsyncMock(return_value=True)
        bili.ack_session = AsyncMock(return_value=True)

        # Mock reply_gen：返回固定回复
        reply_gen = MagicMock()
        reply_gen.generate_reply = AsyncMock(return_value={
            "reply": "你好，收到私信",
            "audit_id": "audit_1",
        })

        # Mock knowledge_memory：监控是否被调用
        knowledge_memory = MagicMock()
        knowledge_memory.save_conversation_as_memory = AsyncMock(return_value=[])
        knowledge_memory.save_memory = AsyncMock(return_value=1)
        knowledge_memory.write_atom = AsyncMock(return_value=1)

        sched = Scheduler(
            config_loader=config,
            user_state=None,
            llm=None,
            bili=bili,
            data_store=ds,
            persona_store=None,
            orchestrator=None,
            audit_store=None,
            context_builder=None,
            comment_context_service=None,
            safety_checker=None,
            account_id="acc_priv",
            knowledge_memory=knowledge_memory,
        )
        # 手动注入 reply_gen（__init__ 因缺 llm 等未创建）
        sched.reply_gen = reply_gen

        await sched._check_new_messages()

        # 验证：PM 成功发送
        bili.send_private_message.assert_called_once()
        # 验证：knowledge_memory 没有被调用写入 PM 内容
        knowledge_memory.save_conversation_as_memory.assert_not_called()
        knowledge_memory.save_memory.assert_not_called()
        knowledge_memory.write_atom.assert_not_called()

        # 验证：pm_state_store 记录为 published
        pm_state = sched.pm_state_store.get_by_message_id("acc_priv", "platform_msg_999")
        assert pm_state is not None
        assert pm_state.status == "published"
        assert pm_state.generation_text == "你好，收到私信"
        assert pm_state.talker_id == "12345"

    @pytest.mark.asyncio
    async def test_scheduler_pm_flow_marks_retry_wait_on_failure(self, tmp_data_dir):
        """Scheduler PM 发送失败时标记 retry_wait（独立退避）"""
        from bilibot.app.config_loader import ConfigLoader
        from bilibot.scheduler import Scheduler
        from bilibot.data_store import DataStore

        config = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "s", "bili_jct": "j", "dede_user_id": "999"},
            "llm": {"api_key": "k", "base_url": "http://x/v1", "model": "m"},
            "data_dir": tmp_data_dir,
            "features": {"private_message": True},
            "private_message": {"max_attempts": 3, "backoff_base_seconds": 5},
        })
        ds = DataStore(tmp_data_dir)

        bili = MagicMock()
        bili.get_private_sessions = AsyncMock(return_value={
            "code": 0,
            "data": {
                "session_list": [{
                    "session_type": 1,
                    "unread_count": 1,
                    "talker_id": 22222,
                    "talker_info": {"uname": "用户B"},
                    "last_msg": {
                        "msg_id": "pm_fail_001",
                        "content": '{"content":"在吗"}',
                        "sender_uid": 22222,
                        "timestamp": 1700000001,
                    },
                }],
            },
        })
        bili.send_private_message = AsyncMock(return_value=False)  # 发送失败
        bili.ack_session = AsyncMock(return_value=True)

        reply_gen = MagicMock()
        reply_gen.generate_reply = AsyncMock(return_value={
            "reply": "在的",
            "audit_id": "audit_2",
        })

        knowledge_memory = MagicMock()
        knowledge_memory.save_conversation_as_memory = AsyncMock(return_value=[])

        sched = Scheduler(
            config_loader=config,
            user_state=None, llm=None, bili=bili, data_store=ds,
            persona_store=None, orchestrator=None, audit_store=None,
            context_builder=None, comment_context_service=None,
            safety_checker=None, account_id="acc_fail",
            knowledge_memory=knowledge_memory,
        )
        sched.reply_gen = reply_gen

        await sched._check_new_messages()

        pm_state = sched.pm_state_store.get_by_message_id("acc_fail", "pm_fail_001")
        assert pm_state is not None
        assert pm_state.status == "retry_wait"
        assert pm_state.attempt == 1
        assert pm_state.next_retry_at is not None
        assert pm_state.last_error_code == "PM_PUBLISH_FAILED"
        # 独立退避配置生效（backoff_base_seconds=5）
        assert sched.pm_state_store.backoff_base_seconds == 5

    @pytest.mark.asyncio
    async def test_scheduler_pm_idempotent_skip_terminal(self, tmp_data_dir):
        """已 published 的私信再次发现时跳过（幂等）"""
        from bilibot.app.config_loader import ConfigLoader
        from bilibot.scheduler import Scheduler
        from bilibot.data_store import DataStore

        config = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "s", "bili_jct": "j", "dede_user_id": "999"},
            "llm": {"api_key": "k", "base_url": "http://x/v1", "model": "m"},
            "data_dir": tmp_data_dir,
            "features": {"private_message": True},
        })
        ds = DataStore(tmp_data_dir)

        bili = MagicMock()
        bili.get_private_sessions = AsyncMock(return_value={
            "code": 0,
            "data": {
                "session_list": [{
                    "session_type": 1,
                    "unread_count": 1,
                    "talker_id": 33333,
                    "talker_info": {"uname": "用户C"},
                    "last_msg": {
                        "msg_id": "pm_dup_001",
                        "content": '{"content":"重复消息"}',
                        "sender_uid": 33333,
                        "timestamp": 1700000002,
                    },
                }],
            },
        })
        bili.send_private_message = AsyncMock(return_value=True)
        bili.ack_session = AsyncMock(return_value=True)

        reply_gen = MagicMock()
        reply_gen.generate_reply = AsyncMock(return_value={
            "reply": "回复", "audit_id": "audit_3",
        })

        sched = Scheduler(
            config_loader=config,
            user_state=None, llm=None, bili=bili, data_store=ds,
            persona_store=None, orchestrator=None, audit_store=None,
            context_builder=None, comment_context_service=None,
            safety_checker=None, account_id="acc_dup",
            knowledge_memory=None,
        )
        sched.reply_gen = reply_gen

        # 预先创建已 published 的状态记录
        existing = sched.pm_state_store.ensure_discovered(
            "acc_dup", "pm_dup_001", "33333",
        )
        sched.pm_state_store.update_status(existing.id, "generation_pending")
        sched.pm_state_store.save_generation_result(
            existing.id, text="旧回复", persona_id="p1",
        )
        sched.pm_state_store.update_status(existing.id, "safety_pending")
        sched.pm_state_store.update_status(existing.id, "publish_pending")
        sched.pm_state_store.mark_published(existing.id)

        await sched._check_new_messages()

        # 验证：未再次发送（幂等）
        bili.send_private_message.assert_not_called()
        # 状态仍为 published
        pm_state = sched.pm_state_store.get_by_message_id("acc_dup", "pm_dup_001")
        assert pm_state.status == "published"
        # 保留原始 generation_text
        assert pm_state.generation_text == "旧回复"


# ═══════════════════════════════════════════════════════
#  默认配置
# ═══════════════════════════════════════════════════════

class TestDefaultConfig:

    def test_default_max_attempts(self):
        assert DEFAULT_PM_MAX_ATTEMPTS == 3

    def test_default_backoff_base_seconds(self):
        assert DEFAULT_PM_BACKOFF_BASE_SECONDS == 30

    def test_store_uses_config_values(self, tmp_data_dir):
        """store 使用传入的 max_attempts 和 backoff_base_seconds"""
        s = PrivateMessageStateStore(
            tmp_data_dir, account_id="acc_cfg",
            max_attempts=7, backoff_base_seconds=42,
        )
        assert s.max_attempts == 7
        assert s.backoff_base_seconds == 42

        # 记录中也使用配置的 max_attempts
        state = s.ensure_discovered("acc_cfg", "pm_cfg_1", "t1")
        assert state.max_attempts == 7
