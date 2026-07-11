"""
tests/test_rep_502_text_persistence.py - REP-502 生成文本持久化与重发测试

PRD-V5 §6.2：
1. 生成有效文本后、安全检查之前持久化 generation_result + hash + persona + audit
2. publish_pending / retry_wait / published 保留同一 generation_hash
3. 发布失败重试使用原始文本，不重新生成
4. 幂等键含 generation_revision
5. 仅 invalidate_generation 或新一次 save 推进 revision

覆盖：
- save_generation_result 写入字段 + revision=1
- retry_wait 保留 generation_result 和 hash
- publish_pending 保留 generation hash
- published 保留 generation hash
- retry 使用原始文本（不重新生成）
- 空 generation_result → deferred
- hash 不匹配 → deferred
- idempotency key 含 generation_revision
- invalidate_generation → 新 revision
- mark_retry_wait 保留 generation_result（不丢失）
- compute_generation_hash 一致性
- 旧库迁移：新列自动补齐
"""
import hashlib
import os
import sqlite3

import pytest

from bilibot.services.reply_state import ReplyStateStore


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def store(tmp_data_dir):
    """ReplyStateStore 实例（临时 DB）"""
    db_path = os.path.join(tmp_data_dir, "test_reply_state.db")
    return ReplyStateStore(db_path, account_id="acc_test", max_attempts=5)


@pytest.fixture
def store_with_reply(store):
    """已有一条 discovered 记录的 store"""
    store.upsert(1, "rpid_100", "discovered",
                 notification={"item": {"subject_id": 999, "source_id": 100}})
    return store


# ═══════════════════════════════════════════════════════
#  save_generation_result
# ═══════════════════════════════════════════════════════

class TestSaveGenerationResult:
    """save_generation_result 持久化生成文本"""

    def test_writes_all_fields(self, store):
        """保存后所有 generation 字段均写入"""
        store.upsert(1, "rpid_1", "generation_pending")
        store.save_generation_result(
            1, "rpid_1",
            text="你好世界",
            persona_id="persona_a",
            audit_id="audit_001",
        )
        state = store.get_state(1, "rpid_1")
        assert state["generation_result"] == "你好世界"
        assert state["generation_hash"] == hashlib.sha256("你好世界".encode()).hexdigest()
        assert state["generation_persona_id"] == "persona_a"
        assert state["generation_audit_id"] == "audit_001"
        assert state["generation_revision"] == 1

    def test_revision_starts_at_1(self, store):
        """首次保存 revision=1"""
        store.save_generation_result(1, "rpid_2", text="hello", persona_id="p1")
        state = store.get_state(1, "rpid_2")
        assert state["generation_revision"] == 1

    def test_same_text_same_revision(self, store):
        """相同文本再次保存不推进 revision"""
        store.save_generation_result(1, "rpid_3", text="same", persona_id="p1")
        rev1 = store.get_state(1, "rpid_3")["generation_revision"]
        store.save_generation_result(1, "rpid_3", text="same", persona_id="p1")
        rev2 = store.get_state(1, "rpid_3")["generation_revision"]
        assert rev1 == rev2 == 1

    def test_different_text_increments_revision(self, store):
        """不同文本推进 revision"""
        store.save_generation_result(1, "rpid_4", text="text_a", persona_id="p1")
        rev1 = store.get_state(1, "rpid_4")["generation_revision"]
        store.save_generation_result(1, "rpid_4", text="text_b", persona_id="p1")
        rev2 = store.get_state(1, "rpid_4")["generation_revision"]
        assert rev2 == rev1 + 1

    def test_empty_text_noop(self, store):
        """空文本不写入"""
        store.upsert(1, "rpid_5", "generation_pending")
        result = store.save_generation_result(1, "rpid_5", text="", persona_id="p1")
        state = store.get_state(1, "rpid_5")
        assert state["generation_result"] == "" or state["generation_result"] is None
        assert state["generation_revision"] == 0

    def test_preserves_state(self, store):
        """save_generation_result 不改变当前 state"""
        store.upsert(1, "rpid_6", "safety_pending")
        store.save_generation_result(1, "rpid_6", text="hi", persona_id="p1")
        state = store.get_state(1, "rpid_6")
        assert state["state"] == "safety_pending"

    def test_hash_matches_compute(self, store):
        """保存的 hash 与 compute_generation_hash 一致"""
        text = "测试文本内容"
        store.save_generation_result(1, "rpid_7", text=text, persona_id="p1")
        state = store.get_state(1, "rpid_7")
        assert state["generation_hash"] == ReplyStateStore.compute_generation_hash(text)


# ═══════════════════════════════════════════════════════
#  retry_wait 保留 generation 字段
# ═══════════════════════════════════════════════════════

class TestRetryWaitPreservesGeneration:
    """mark_retry_wait 不丢失 generation_result 和 hash"""

    def test_retry_wait_preserves_text_and_hash(self, store):
        """retry_wait 后 generation_result 和 generation_hash 仍在"""
        store.save_generation_result(1, "rpid_10", text="原始回复", persona_id="p1")
        original_hash = store.get_state(1, "rpid_10")["generation_hash"]

        store.mark_retry_wait(1, "rpid_10", reason="publish_failed",
                              error_code="PUBLISH_FAILED")
        state = store.get_state(1, "rpid_10")
        assert state["state"] == "retry_wait"
        assert state["generation_result"] == "原始回复"
        assert state["generation_hash"] == original_hash

    def test_retry_wait_preserves_revision(self, store):
        """retry_wait 后 generation_revision 不变"""
        store.save_generation_result(1, "rpid_11", text="rev_test", persona_id="p1")
        rev_before = store.get_state(1, "rpid_11")["generation_revision"]

        store.mark_retry_wait(1, "rpid_11", reason="fail")
        rev_after = store.get_state(1, "rpid_11")["generation_revision"]
        assert rev_before == rev_after

    def test_retry_wait_preserves_audit_id(self, store):
        """retry_wait 后 generation_audit_id 不丢失"""
        store.save_generation_result(1, "rpid_12", text="audit_test",
                                     persona_id="p1", audit_id="aud_123")
        store.mark_retry_wait(1, "rpid_12", reason="fail")
        state = store.get_state(1, "rpid_12")
        assert state["generation_audit_id"] == "aud_123"

    def test_retry_wait_preserves_persona_id(self, store):
        """retry_wait 后 generation_persona_id 不丢失"""
        store.save_generation_result(1, "rpid_13", text="persona_test",
                                     persona_id="persona_xyz")
        store.mark_retry_wait(1, "rpid_13", reason="fail")
        state = store.get_state(1, "rpid_13")
        assert state["generation_persona_id"] == "persona_xyz"


# ═══════════════════════════════════════════════════════
#  publish_pending / published 保留 generation hash
# ═══════════════════════════════════════════════════════

class TestPublishStatesPreserveHash:

    def test_publish_pending_preserves_hash(self, store):
        """publish_pending 保留 generation_hash"""
        store.save_generation_result(1, "rpid_20", text="发布中文本", persona_id="p1")
        gen_hash = store.get_state(1, "rpid_20")["generation_hash"]

        store.upsert(1, "rpid_20", "publish_pending")
        state = store.get_state(1, "rpid_20")
        assert state["state"] == "publish_pending"
        assert state["generation_hash"] == gen_hash
        assert state["generation_result"] == "发布中文本"

    def test_published_preserves_hash(self, store):
        """published 保留 generation_hash"""
        store.save_generation_result(1, "rpid_21", text="已发布文本", persona_id="p1")
        gen_hash = store.get_state(1, "rpid_21")["generation_hash"]

        store.mark_published(1, "rpid_21")
        state = store.get_state(1, "rpid_21")
        assert state["state"] == "published"
        assert state["generation_hash"] == gen_hash
        assert state["generation_result"] == "已发布文本"

    def test_published_preserves_revision(self, store):
        """published 保留 generation_revision"""
        store.save_generation_result(1, "rpid_22", text="rev_pub", persona_id="p1")
        rev = store.get_state(1, "rpid_22")["generation_revision"]

        store.mark_published(1, "rpid_22")
        state = store.get_state(1, "rpid_22")
        assert state["generation_revision"] == rev

    def test_full_flow_preserves_hash(self, store):
        """完整流程 save → safety_pending → publish_pending → published 保留 hash"""
        text = "完整流程文本"
        store.save_generation_result(1, "rpid_23", text=text, persona_id="p1")
        expected_hash = ReplyStateStore.compute_generation_hash(text)

        store.upsert(1, "rpid_23", "safety_pending")
        assert store.get_state(1, "rpid_23")["generation_hash"] == expected_hash

        store.upsert(1, "rpid_23", "publish_pending")
        assert store.get_state(1, "rpid_23")["generation_hash"] == expected_hash

        store.mark_retry_wait(1, "rpid_23", reason="temp_fail")
        assert store.get_state(1, "rpid_23")["generation_hash"] == expected_hash

        store.mark_published(1, "rpid_23")
        final = store.get_state(1, "rpid_23")
        assert final["generation_hash"] == expected_hash
        assert final["generation_result"] == text


# ═══════════════════════════════════════════════════════
#  retry 使用原始文本（不重新生成）
# ═══════════════════════════════════════════════════════

class TestRetryUsesOriginalText:
    """重试读取已保存的 generation_result，不重新生成"""

    def test_retry_reads_saved_text(self, store):
        """retry_wait 状态下 get_retryable 返回的记录含 generation_result"""
        store.save_generation_result(1, "rpid_30", text="重试文本", persona_id="p1")
        store.mark_retry_wait(1, "rpid_30", reason="publish_fail")

        # 用足够大的 now 确保 next_retry_at <= now（mark_retry_wait 设置了未来时间）
        retryable = store.get_retryable(now=float("inf"))
        found = [r for r in retryable if r["source_rpid"] == "rpid_30"]
        assert len(found) == 1
        assert found[0]["generation_result"] == "重试文本"
        assert found[0]["generation_hash"] == ReplyStateStore.compute_generation_hash("重试文本")

    def test_empty_generation_result_falls_to_deferred(self, store):
        """无 generation_result 的 retry_wait 应在重试逻辑中转为 deferred"""
        # 直接 upsert 到 retry_wait，不经过 save_generation_result
        store.upsert(1, "rpid_31", "discovered",
                     notification={"item": {"subject_id": 1, "source_id": 31}})
        store.mark_retry_wait(1, "rpid_31", reason="no_gen")

        state = store.get_state(1, "rpid_31")
        assert state["state"] == "retry_wait"
        assert not state.get("generation_result")

        # 模拟 scheduler 重试逻辑：无文本 → mark_deferred
        gen_result = state.get("generation_result", "")
        if not gen_result:
            store.mark_deferred(1, "rpid_31", reason="no_generation_result",
                                error_code="RETRY_NO_GEN")
        after = store.get_state(1, "rpid_31")
        assert after["state"] == "deferred"

    def test_hash_mismatch_falls_to_deferred(self, store):
        """hash 不匹配时应转为 deferred（模拟 scheduler 逻辑）"""
        store.save_generation_result(1, "rpid_32", text="原始文本", persona_id="p1")
        # 手动篡改 generation_result（不改 hash）
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE reply_states SET generation_result='篡改文本' "
            "WHERE source_rpid='rpid_32'"
        )
        conn.commit()
        conn.close()

        store.mark_retry_wait(1, "rpid_32", reason="fail")
        state = store.get_state(1, "rpid_32")

        gen_result = state.get("generation_result", "")
        gen_hash = state.get("generation_hash", "")
        reply_text = gen_result if gen_result else ""

        text_valid = bool(reply_text)
        if text_valid and gen_hash:
            expected = ReplyStateStore.compute_generation_hash(reply_text)
            if expected != gen_hash:
                text_valid = False

        assert not text_valid, "hash 不匹配应判定为无效"

        # 模拟 scheduler：text 无效 → deferred
        if not text_valid:
            store.mark_deferred(1, "rpid_32", reason="hash_mismatch",
                                error_code="RETRY_NO_GEN")
        assert store.get_state(1, "rpid_32")["state"] == "deferred"

    def test_valid_text_retries_without_regeneration(self, store):
        """有效文本 + hash 匹配 → 可直接重试（不需要重新生成）"""
        original_text = "可以直接重试的文本"
        store.save_generation_result(1, "rpid_33", text=original_text, persona_id="p1")
        store.mark_retry_wait(1, "rpid_33", reason="publish_fail")

        state = store.get_state(1, "rpid_33")
        gen_result = state.get("generation_result", "")
        gen_hash = state.get("generation_hash", "")

        # 验证：文本存在且 hash 匹配
        assert gen_result == original_text
        expected = ReplyStateStore.compute_generation_hash(gen_result)
        assert gen_hash == expected
        # 模拟 scheduler：text 有效 → 直接发布（不调 generate_reply）
        # 这里只验证数据可用性，不实际调用 bili API


# ═══════════════════════════════════════════════════════
#  幂等键含 generation_revision
# ═══════════════════════════════════════════════════════

class TestIdempotencyKey:

    def test_key_includes_revision(self):
        """make_idempotency_key 返回 4 元组含 revision"""
        key = ReplyStateStore.make_idempotency_key("acc1", 1, "rpid_40", 1)
        assert len(key) == 4
        assert key == ("acc1", 1, "rpid_40", 1)

    def test_different_revisions_different_keys(self):
        """不同 revision 产生不同键"""
        key1 = ReplyStateStore.make_idempotency_key("acc1", 1, "rpid_41", 1)
        key2 = ReplyStateStore.make_idempotency_key("acc1", 1, "rpid_41", 2)
        assert key1 != key2

    def test_same_revision_same_key(self):
        """同 revision 产生相同键（防止重复发布）"""
        key1 = ReplyStateStore.make_idempotency_key("acc1", 1, "rpid_42", 1)
        key2 = ReplyStateStore.make_idempotency_key("acc1", 1, "rpid_42", 1)
        assert key1 == key2

    def test_make_key_still_3_tuple(self):
        """make_key 保持 3 元组（向后兼容）"""
        key = ReplyStateStore.make_key("acc1", 1, "rpid_43")
        assert len(key) == 3
        assert key == ("acc1", 1, "rpid_43")

    def test_revision_from_state_in_key(self, store):
        """从 state 读取 revision 构造幂等键"""
        store.save_generation_result(1, "rpid_44", text="key_test", persona_id="p1")
        state = store.get_state(1, "rpid_44")
        rev = state["generation_revision"]

        key = ReplyStateStore.make_idempotency_key(
            "acc_test", 1, "rpid_44", rev
        )
        assert key == ("acc_test", 1, "rpid_44", 1)


# ═══════════════════════════════════════════════════════
#  invalidate_generation
# ═══════════════════════════════════════════════════════

class TestInvalidateGeneration:

    def test_clears_text_and_hash(self, store):
        """invalidate 后文本和 hash 清空"""
        store.save_generation_result(1, "rpid_50", text="要失效的文本", persona_id="p1")
        store.invalidate_generation(1, "rpid_50")
        state = store.get_state(1, "rpid_50")
        assert state["generation_result"] == ""
        assert state["generation_hash"] == ""
        assert state["generation_audit_id"] == ""

    def test_bumps_revision(self, store):
        """invalidate 后 revision 递增"""
        store.save_generation_result(1, "rpid_51", text="v1", persona_id="p1")
        rev1 = store.get_state(1, "rpid_51")["generation_revision"]
        store.invalidate_generation(1, "rpid_51")
        rev2 = store.get_state(1, "rpid_51")["generation_revision"]
        assert rev2 == rev1 + 1

    def test_next_save_after_invalidate_uses_new_revision(self, store):
        """invalidate 后再 save 使用新 revision"""
        store.save_generation_result(1, "rpid_52", text="first", persona_id="p1")
        rev1 = store.get_state(1, "rpid_52")["generation_revision"]

        store.invalidate_generation(1, "rpid_52")
        store.save_generation_result(1, "rpid_52", text="second", persona_id="p1")
        rev2 = store.get_state(1, "rpid_52")["generation_revision"]

        assert rev2 == rev1 + 1
        assert store.get_state(1, "rpid_52")["generation_result"] == "second"

    def test_invalidate_nonexistent_returns_empty(self, store):
        """invalidate 不存在的记录返回空 dict"""
        result = store.invalidate_generation(1, "nonexistent_rpid")
        assert result == {}

    def test_idempotency_key_changes_after_invalidate(self, store):
        """invalidate 后幂等键变化（新 revision）"""
        store.save_generation_result(1, "rpid_53", text="key_change", persona_id="p1")
        state1 = store.get_state(1, "rpid_53")
        key1 = ReplyStateStore.make_idempotency_key(
            "acc_test", 1, "rpid_53", state1["generation_revision"]
        )

        store.invalidate_generation(1, "rpid_53")
        store.save_generation_result(1, "rpid_53", text="new_text", persona_id="p1")
        state2 = store.get_state(1, "rpid_53")
        key2 = ReplyStateStore.make_idempotency_key(
            "acc_test", 1, "rpid_53", state2["generation_revision"]
        )

        assert key1 != key2


# ═══════════════════════════════════════════════════════
#  compute_generation_hash
# ═══════════════════════════════════════════════════════

class TestComputeGenerationHash:

    def test_hash_is_sha256_hex(self):
        """hash 为 64 字符的 SHA-256 十六进制"""
        h = ReplyStateStore.compute_generation_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_text_same_hash(self):
        """相同文本产生相同 hash"""
        assert ReplyStateStore.compute_generation_hash("abc") == \
               ReplyStateStore.compute_generation_hash("abc")

    def test_different_text_different_hash(self):
        """不同文本产生不同 hash"""
        assert ReplyStateStore.compute_generation_hash("abc") != \
               ReplyStateStore.compute_generation_hash("abd")

    def test_unicode_text(self):
        """中文文本正确哈希"""
        text = "你好世界🌟"
        h = ReplyStateStore.compute_generation_hash(text)
        assert h == hashlib.sha256(text.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════
#  旧库迁移 / 列自动补齐
# ═══════════════════════════════════════════════════════

class TestSchemaMigration:

    def test_old_db_gets_new_columns(self, tmp_data_dir):
        """旧库（无新列）打开后自动补齐 generation_hash 等列"""
        db_path = os.path.join(tmp_data_dir, "legacy.db")
        # 手动创建旧 schema（无 generation_hash / generation_revision 等新列）
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE reply_states (
                account_id TEXT NOT NULL,
                comment_type INTEGER NOT NULL DEFAULT 1,
                source_rpid TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'discovered',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                last_error TEXT,
                last_error_code TEXT,
                notification_json TEXT,
                generation_result TEXT,
                persona_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                next_retry_at REAL,
                metadata TEXT DEFAULT '{}',
                PRIMARY KEY (account_id, comment_type, source_rpid)
            )
        """)
        conn.execute(
            "INSERT INTO reply_states (account_id, comment_type, source_rpid, state, "
            "attempts, max_attempts, created_at, updated_at) "
            "VALUES ('acc_old', 1, 'legacy_rpid', 'published', 0, 3, 0, 0)"
        )
        conn.commit()
        conn.close()

        # 用 ReplyStateStore 打开 → 应自动补列
        store = ReplyStateStore(db_path, account_id="acc_old")
        state = store.get_state(1, "legacy_rpid")
        assert state is not None
        assert state["state"] == "published"
        # 新列存在且默认值正确
        assert state["generation_hash"] is None or state["generation_hash"] == ""
        assert state["generation_revision"] == 0

        # 可以正常 save_generation_result
        store.save_generation_result(1, "legacy_rpid", text="新文本", persona_id="p1")
        state = store.get_state(1, "legacy_rpid")
        assert state["generation_hash"] == ReplyStateStore.compute_generation_hash("新文本")
        assert state["generation_revision"] == 1


# ═══════════════════════════════════════════════════════
#  mark_retry_wait 保留 generation_result（集成验证）
# ═══════════════════════════════════════════════════════

class TestMarkRetryWaitWithGeneration:

    def test_mark_retry_wait_does_not_clear_generation(self, store):
        """mark_retry_wait 不会清空已保存的 generation_result"""
        text = "不应被清空的文本"
        store.save_generation_result(1, "rpid_60", text=text, persona_id="p1")
        assert store.get_state(1, "rpid_60")["generation_result"] == text

        store.mark_retry_wait(1, "rpid_60", reason="fail", error_code="PUBLISH_FAILED")
        assert store.get_state(1, "rpid_60")["generation_result"] == text

    def test_multiple_retry_waits_preserve_text(self, store):
        """多次 retry_wait 仍保留原始文本"""
        text = "多次重试文本"
        store.save_generation_result(1, "rpid_61", text=text, persona_id="p1")
        original_hash = store.get_state(1, "rpid_61")["generation_hash"]

        for i in range(3):
            store.mark_retry_wait(1, "rpid_61", reason=f"fail_{i}")
            state = store.get_state(1, "rpid_61")
            assert state["generation_result"] == text
            assert state["generation_hash"] == original_hash

    def test_get_state_returns_generation_fields(self, store):
        """get_state 返回所有 generation 字段"""
        store.save_generation_result(
            1, "rpid_62", text="全字段", persona_id="p_full", audit_id="aud_full"
        )
        state = store.get_state(1, "rpid_62")
        assert "generation_result" in state
        assert "generation_hash" in state
        assert "generation_revision" in state
        assert "generation_audit_id" in state
        assert "generation_persona_id" in state
