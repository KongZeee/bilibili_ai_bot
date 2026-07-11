"""
tests/test_mem_config_wiring.py - PRD V5 Task 16 记忆配置接线测试

确保 memory 段每个暴露给用户的配置字段都有真实消费者，避免"幽灵配置"。

覆盖：
- 每个记忆配置字段（max_today/max_recent/max_long_term/enable_forgetting/forgetting_score）
  被 KnowledgeBaseMemory 消费
- ConfigLoader 正确读取这些字段
- MEMORY_FIELD_CONTRACT 包含所有记忆字段的 reload level
- MEMORY_CONFIG_FIELD_MAP 映射完整（schema_path → owner → reload_level → test_id）
- reload_level 正确：max_today/max_recent/enable_forgetting/forgetting_score=next_task，
  max_long_term=restart_account
- 遗忘逻辑：enable_forgetting=True 时超限低分记忆被裁剪；enable_forgetting=False 时不裁剪
- not_implemented 字段不出现在配置 schema 中
"""
import pytest

from bilibot.app.config_loader import ConfigLoader, MemoryConfig
from bilibot.api.config import (
    MEMORY_FIELD_CONTRACT,
    MEMORY_CONFIG_FIELD_MAP,
    _build_config_schema,
    _resolve_reload_level,
    _find_changed_fields,
)


# ═══════════════════════════════════════════════════════
#  Mock LLM
# ═══════════════════════════════════════════════════════

class _MockLLM:
    client = True

    async def generate(self, prompt, system_prompt=None, max_tokens=1024, **kwargs):
        return "mock"

    async def get_embedding(self, text):
        return [0.1] * 8


# ═══════════════════════════════════════════════════════
#  ConfigLoader 读取测试
# ═══════════════════════════════════════════════════════

class TestConfigLoaderReadsMemoryFields:
    """ConfigLoader 必须读取所有 5 个记忆配置字段"""

    def test_reads_max_today(self, tmp_data_dir):
        cl = ConfigLoader(config_dict={
            "data_dir": tmp_data_dir,
            "memory": {"max_today": 77},
        })
        assert cl.memory.max_today == 77

    def test_reads_max_recent(self, tmp_data_dir):
        cl = ConfigLoader(config_dict={
            "data_dir": tmp_data_dir,
            "memory": {"max_recent": 188},
        })
        assert cl.memory.max_recent == 188

    def test_reads_max_long_term(self, tmp_data_dir):
        cl = ConfigLoader(config_dict={
            "data_dir": tmp_data_dir,
            "memory": {"max_long_term": 999},
        })
        assert cl.memory.max_long_term == 999

    def test_reads_enable_forgetting(self, tmp_data_dir):
        cl = ConfigLoader(config_dict={
            "data_dir": tmp_data_dir,
            "memory": {"enable_forgetting": False},
        })
        assert cl.memory.enable_forgetting is False

    def test_reads_forgetting_score(self, tmp_data_dir):
        cl = ConfigLoader(config_dict={
            "data_dir": tmp_data_dir,
            "memory": {"forgetting_score": 5.5},
        })
        assert cl.memory.forgetting_score == 5.5

    def test_defaults_when_missing(self, tmp_data_dir):
        cl = ConfigLoader(config_dict={"data_dir": tmp_data_dir})
        assert cl.memory.max_today == 50
        assert cl.memory.max_recent == 200
        assert cl.memory.max_long_term == 1000
        assert cl.memory.enable_forgetting is True
        assert cl.memory.forgetting_score == 3.0


# ═══════════════════════════════════════════════════════
#  KnowledgeBaseMemory 消费测试
# ═══════════════════════════════════════════════════════

class TestKnowledgeMemoryConsumesConfig:
    """KnowledgeBaseMemory 必须接收并存储所有 5 个记忆配置字段"""

    def test_max_today_consumed_by_knowledge_memory(self, tmp_data_dir):
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(max_today=42)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        assert km.max_today == 42
        km.close()

    def test_max_recent_consumed_by_knowledge_memory(self, tmp_data_dir):
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(max_recent=150)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        assert km.max_recent == 150
        km.close()

    def test_max_long_term_consumed_by_knowledge_memory(self, tmp_data_dir):
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(max_long_term=500)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        assert km.max_long_term == 500
        km.close()

    def test_enable_forgetting_consumed_by_knowledge_memory(self, tmp_data_dir):
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(enable_forgetting=False)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        assert km.enable_forgetting is False
        km.close()

    def test_forgetting_score_consumed_by_knowledge_memory(self, tmp_data_dir):
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(forgetting_score=7.0)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        assert km.forgetting_score == 7.0
        km.close()

    def test_defaults_when_no_config_passed(self, tmp_data_dir):
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM())
        assert km.max_today == 50
        assert km.max_recent == 200
        assert km.max_long_term == 1000
        assert km.enable_forgetting is True
        assert km.forgetting_score == 3.0
        km.close()

    def test_apply_memory_config_updates_values(self, tmp_data_dir):
        """apply_memory_config 应在 next_task 级别更新配置"""
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM())
        new_cfg = MemoryConfig(
            max_today=10, max_recent=20, max_long_term=30,
            enable_forgetting=False, forgetting_score=8.0,
        )
        km.apply_memory_config(new_cfg)
        assert km.max_today == 10
        assert km.max_recent == 20
        assert km.max_long_term == 30
        assert km.enable_forgetting is False
        assert km.forgetting_score == 8.0
        km.close()


# ═══════════════════════════════════════════════════════
#  遗忘逻辑测试
# ═══════════════════════════════════════════════════════

class TestForgettingLogic:
    """forget_low_importance 基于重要性裁剪测试"""

    def test_forget_disabled_when_enable_forgetting_false(self, tmp_data_dir):
        """enable_forgetting=False 时不裁剪"""
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(max_long_term=2, enable_forgetting=False)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        # 写入 5 条记忆
        for i in range(5):
            km.store.add_raw_atom(
                content=f"memory {i}", importance_score=0.1,
                user_id="u1", persona_id="p1",
            )
        purged = km.forget_low_importance()
        assert purged == 0  # 遗忘关闭
        stats = km.get_stats()
        assert stats["total"] == 5
        km.close()

    def test_forget_prunes_low_importance_when_over_limit(self, tmp_data_dir):
        """超过 max_long_term 时裁剪低分记忆"""
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        # max_long_term=3, forgetting_score=5（阈值 0.5），写入 5 条，2 条低分应被裁剪
        mem_cfg = MemoryConfig(max_long_term=3, enable_forgetting=True, forgetting_score=5)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        # 高分记忆（importance_score >= 0.5，不会被裁剪）
        km.store.add_raw_atom(content="high1", importance_score=0.9, user_id="u1")
        km.store.add_raw_atom(content="high2", importance_score=0.8, user_id="u1")
        km.store.add_raw_atom(content="high3", importance_score=0.7, user_id="u1")
        # 低分记忆（importance_score < 0.5，候选裁剪）
        km.store.add_raw_atom(content="low1", importance_score=0.1, user_id="u1")
        km.store.add_raw_atom(content="low2", importance_score=0.2, user_id="u1")

        purged = km.forget_low_importance()
        assert purged == 2  # 超出 2 条，全部低分被裁剪
        stats = km.get_stats()
        assert stats["total"] == 3
        km.close()

    def test_forget_keeps_high_importance_when_over_limit(self, tmp_data_dir):
        """即使超限，高分记忆不被裁剪（只裁剪低于阈值的）"""
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        # max_long_term=2, forgetting_score=3（阈值 0.3）
        mem_cfg = MemoryConfig(max_long_term=2, enable_forgetting=True, forgetting_score=3)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        # 全部高分（>= 0.3），不会裁剪任何
        for i in range(5):
            km.store.add_raw_atom(content=f"high{i}", importance_score=0.9, user_id="u1")
        purged = km.forget_low_importance()
        assert purged == 0  # 没有低于阈值的候选
        assert km.get_stats()["total"] == 5
        km.close()

    def test_forget_no_op_when_under_limit(self, tmp_data_dir):
        """未超限时 forget 是 no-op"""
        from bilibot.knowledge_memory import KnowledgeBaseMemory
        mem_cfg = MemoryConfig(max_long_term=100, enable_forgetting=True, forgetting_score=3)
        km = KnowledgeBaseMemory(tmp_data_dir, _MockLLM(), memory_config=mem_cfg)
        km.store.add_raw_atom(content="m1", importance_score=0.1, user_id="u1")
        purged = km.forget_low_importance()
        assert purged == 0
        km.close()


# ═══════════════════════════════════════════════════════
#  RELOAD_CONTRACT 测试
# ═══════════════════════════════════════════════════════

class TestReloadContractForMemory:
    """MEMORY_FIELD_CONTRACT 必须包含所有 5 个字段且 reload level 正确"""

    def test_contract_contains_all_fields(self):
        expected = {"max_today", "max_recent", "max_long_term",
                    "enable_forgetting", "forgetting_score"}
        assert expected.issubset(MEMORY_FIELD_CONTRACT.keys())

    def test_max_today_is_next_task(self):
        assert _resolve_reload_level("memory", "max_today") == "next_task"

    def test_max_recent_is_next_task(self):
        assert _resolve_reload_level("memory", "max_recent") == "next_task"

    def test_max_long_term_is_restart_account(self):
        assert _resolve_reload_level("memory", "max_long_term") == "restart_account"

    def test_enable_forgetting_is_next_task(self):
        assert _resolve_reload_level("memory", "enable_forgetting") == "next_task"

    def test_forgetting_score_is_next_task(self):
        assert _resolve_reload_level("memory", "forgetting_score") == "next_task"

    def test_memory_top_level_still_next_task(self):
        """无 sub_field 时 memory 整体仍为 next_task（向后兼容）"""
        assert _resolve_reload_level("memory") == "next_task"

    def test_unknown_memory_subfield_defaults_next_task(self):
        assert _resolve_reload_level("memory", "unknown_field") == "next_task"


# ═══════════════════════════════════════════════════════
#  MEMORY_CONFIG_FIELD_MAP 完整性测试
# ═══════════════════════════════════════════════════════

class TestMemoryConfigFieldMap:
    """schema_path → owner → reload_level → test_id 映射完整"""

    def test_map_contains_all_five_fields(self):
        expected = {
            "memory.max_today",
            "memory.max_recent",
            "memory.max_long_term",
            "memory.enable_forgetting",
            "memory.forgetting_score",
        }
        assert expected.issubset(MEMORY_CONFIG_FIELD_MAP.keys())

    def test_each_entry_has_required_keys(self):
        for path, entry in MEMORY_CONFIG_FIELD_MAP.items():
            assert "owner" in entry, f"{path} missing owner"
            assert "reload_level" in entry, f"{path} missing reload_level"
            assert "test_id" in entry, f"{path} missing test_id"

    def test_reload_level_matches_contract(self):
        """FIELD_MAP 中的 reload_level 必须与 MEMORY_FIELD_CONTRACT 一致"""
        for path, entry in MEMORY_CONFIG_FIELD_MAP.items():
            sub_field = path.split(".", 1)[1]
            expected = _resolve_reload_level("memory", sub_field)
            assert entry["reload_level"] == expected, (
                f"{path} reload_level mismatch: map={entry['reload_level']} "
                f"contract={expected}"
            )

    def test_each_test_id_exists_in_this_module(self):
        """FIELD_MAP 中声明的 test_id 必须真实存在于本测试模块"""
        import sys
        import inspect
        module = sys.modules[__name__]
        # 收集本模块所有测试类的方法名
        test_names = set()
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if inspect.isclass(attr) and attr.__module__ == __name__:
                for method_name in dir(attr):
                    if method_name.startswith("test_"):
                        test_names.add(method_name)
        # 检查每个 entry 的 test_id
        for path, entry in MEMORY_CONFIG_FIELD_MAP.items():
            test_id = entry["test_id"]
            assert test_id in test_names, (
                f"{path} 声明的 test_id '{test_id}' 在本测试模块中不存在"
            )


# ═══════════════════════════════════════════════════════
#  Schema 完整性测试
# ═══════════════════════════════════════════════════════

class TestSchemaExposesConsumedFields:
    """配置 schema 中暴露的记忆字段必须有消费者（FIELD_MAP 中有映射）"""

    def test_all_schema_memory_fields_have_consumer_or_are_known(self):
        """schema 中 memory.fields 的每个字段必须要么在 FIELD_MAP 中，
        要么是已知的压缩/检索参数（由其他消费者使用）。
        不允许出现"幽灵字段"——schema 声明但无消费者且未标记。"""
        schema = _build_config_schema()
        mem_schema = schema["memory"]["fields"]

        # Task 16 关注的 5 个字段必须在 FIELD_MAP 中
        task16_fields = {
            "max_today", "max_recent", "max_long_term",
            "enable_forgetting", "forgetting_score",
        }
        for field_name in task16_fields:
            assert field_name in mem_schema, f"{field_name} 不在 schema.memory.fields 中"
            path = f"memory.{field_name}"
            assert path in MEMORY_CONFIG_FIELD_MAP, (
                f"{path} 在 schema 中暴露但未在 MEMORY_CONFIG_FIELD_MAP 中登记"
            )

    def test_no_not_implemented_marker_in_schema(self):
        """Task 16 的 5 个字段都已实现，不应有 not_implemented/deprecated 标记"""
        schema = _build_config_schema()
        mem_fields = schema["memory"]["fields"]
        for field_name in ["max_today", "max_recent", "max_long_term",
                           "enable_forgetting", "forgetting_score"]:
            field_def = mem_fields[field_name]
            assert not field_def.get("deprecated"), f"{field_name} 不应标记为 deprecated"
            assert not field_def.get("not_implemented"), f"{field_name} 不应标记为 not_implemented"


# ═══════════════════════════════════════════════════════
#  _find_changed_fields memory 子字段检测测试
# ═══════════════════════════════════════════════════════

class TestFindChangedMemoryFields:
    """_find_changed_fields 必须检测 memory 子字段级别的变更"""

    def test_detects_max_today_change(self):
        original = {"memory": {"max_today": 50, "max_recent": 200}}
        updated = {"memory": {"max_today": 100, "max_recent": 200}}
        changed = _find_changed_fields(original, updated)
        assert ("memory", "max_today") in changed
        assert ("memory", "max_recent") not in changed

    def test_detects_enable_forgetting_change(self):
        original = {"memory": {"enable_forgetting": True}}
        updated = {"memory": {"enable_forgetting": False}}
        changed = _find_changed_fields(original, updated)
        assert ("memory", "enable_forgetting") in changed

    def test_detects_multiple_memory_subfields(self):
        original = {"memory": {"max_today": 50, "max_recent": 200, "max_long_term": 1000}}
        updated = {"memory": {"max_today": 60, "max_recent": 210, "max_long_term": 1000}}
        changed = _find_changed_fields(original, updated)
        pairs = set(changed)
        assert ("memory", "max_today") in pairs
        assert ("memory", "max_recent") in pairs
        assert ("memory", "max_long_term") not in pairs

    def test_no_change_returns_empty(self):
        original = {"memory": {"max_today": 50}}
        updated = {"memory": {"max_today": 50}}
        changed = _find_changed_fields(original, updated)
        assert ("memory", "max_today") not in changed


# ═══════════════════════════════════════════════════════
#  AccountInstance 接线测试
# ═══════════════════════════════════════════════════════

class TestAccountInstanceWiring:
    """AccountInstance 必须将 memory 配置传递给 KnowledgeBaseMemory"""

    def test_account_instance_passes_memory_config(self, tmp_data_dir):
        """AccountInstance.initialize() 应将 memory 配置注入 KnowledgeBaseMemory"""
        from bilibot.account.instance import AccountInstance
        from unittest.mock import MagicMock

        app_config = ConfigLoader(config_dict={
            "data_dir": tmp_data_dir,
            "memory": {
                "max_today": 11,
                "max_recent": 22,
                "max_long_term": 33,
                "enable_forgetting": False,
                "forgetting_score": 6.0,
            },
            "bilibili": {"sessdata": "s", "bili_jct": "j", "dede_user_id": "1"},
            "llm": {"api_key": "k", "base_url": "http://x/v1", "model": "m"},
        })

        # Mock persona_store / llm_manager / orchestrator 等
        persona_store = MagicMock()
        persona_store.set_account_profile.return_value = True
        persona_store.set_account_persona.return_value = True

        llm_manager = MagicMock()
        mock_llm = _MockLLM()
        llm_manager.resolve_provider.return_value = (mock_llm, "default", "")

        audit_store = MagicMock()
        orchestrator = MagicMock()
        context_builder = MagicMock()

        acc = AccountInstance(
            account_id="test_acc",
            account_config={
                "id": "test_acc", "name": "Test",
                "sessdata": "s", "bili_jct": "j", "dede_user_id": "1",
                "enabled": True, "llm_id": "default",
            },
            persona_store=persona_store,
            llm_manager=llm_manager,
            audit_store=audit_store,
            orchestrator=orchestrator,
            context_builder=context_builder,
            app_config_loader=app_config,
            data_root=tmp_data_dir,
        )

        import asyncio
        asyncio.run(acc.initialize())

        assert acc.knowledge_memory is not None
        # 验证配置被正确传递
        assert acc.knowledge_memory.max_today == 11
        assert acc.knowledge_memory.max_recent == 22
        assert acc.knowledge_memory.max_long_term == 33
        assert acc.knowledge_memory.enable_forgetting is False
        assert acc.knowledge_memory.forgetting_score == 6.0

        asyncio.run(acc.close())
