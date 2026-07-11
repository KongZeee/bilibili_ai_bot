"""
tests/test_persona_store.py - 人格存储测试
"""
import json
import os
import pytest

from bilibot.models import Persona, PersonaExample
from bilibot.services.persona_store import PersonaStore


class TestPersonaCRUD:
    """CRUD 操作"""

    def test_create_default(self, tmp_data_dir):
        """自动创建默认人格"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        current = ps.get_current()
        assert current is not None
        assert current.id == "default"
        assert current.name == "默认人格"

    def test_list_personas(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        personas = ps.list_personas()
        assert len(personas) >= 1
        assert any(p["id"] == "default" for p in personas)
        assert personas[0]["is_current"] is True

    def test_create_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        data = {
            "name": "测试人格",
            "description": "测试用",
            "base_prompt": "你是测试",
        }
        result = ps.create_persona(data)
        assert result["name"] == "测试人格"
        assert result["id"]  # 有ID

    def test_create_duplicate_name_generates_new_id(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        d1 = ps.create_persona({"name": "同名"})
        d2 = ps.create_persona({"name": "同名"})
        assert d1["id"] != d2["id"]

    def test_get_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        p = ps.get_persona("default")
        assert p is not None
        assert p["id"] == "default"
        assert p["is_current"] is True

    def test_get_missing_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        assert ps.get_persona("nonexistent") is None

    def test_update_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        updated = ps.update_persona("default", {"name": "已更新"})
        assert updated is not None
        assert updated["name"] == "已更新"

    def test_update_nonexistent(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        assert ps.update_persona("nonexistent", {"name": "X"}) is None

    def test_delete_non_current(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        pid = ps.create_persona({"name": "待删"})["id"]
        assert ps.delete_persona(pid) is True

    def test_cannot_delete_current(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        assert ps.delete_persona("default") is False

    def test_copy_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        copied = ps.copy_persona("default", "副本人格")
        assert copied is not None
        assert copied["name"] == "副本人格"
        assert copied["id"] != "default"
        assert ps.get_persona(copied["id"]) is not None

    def test_copy_nonexistent(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        assert ps.copy_persona("nonexistent") is None


class TestPersonaSwitch:
    """人格切换"""

    def test_switch_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        pid = ps.create_persona({"name": "B人格"})["id"]
        assert ps.set_current(pid) is True
        assert ps.get_current().id == pid

    def test_switch_disabled_fails(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        pid = ps.create_persona({"name": "禁用人格", "enabled": False})["id"]
        assert ps.set_current(pid) is False

    def test_switch_nonexistent_fails(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        assert ps.set_current("nonexistent") is False

    def test_get_current_after_delete(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        pid = ps.create_persona({"name": "B人格"})["id"]
        ps.set_current(pid)
        ps.delete_persona(pid)
        # Should fallback to first enabled
        current = ps.get_current()
        assert current is not None


class TestPersonaSerialization:
    """序列化"""

    def test_roundtrip(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        d = ps.export_persona("default")
        assert d is not None
        # Reimport
        imported = ps.import_persona(d)
        assert imported["id"] != "default"  # 新ID
        assert ps.get_persona(imported["id"]) is not None

    def test_persistence(self, tmp_data_dir):
        ps1 = PersonaStore(data_dir=tmp_data_dir)
        pid = ps1.create_persona({"name": "持久化测试"})["id"]
        # 新实例应该能看到
        ps2 = PersonaStore(data_dir=tmp_data_dir)
        assert ps2.get_persona(pid) is not None
        assert ps2.get_current().id == "default"  # 当前人格不变


class TestPersonaExamples:
    """示例对话"""

    def test_examples_in_persona(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        examples = [
            {"input": "你好", "output": "嗨～"},
            {"input": "在吗", "output": "在呢"},
        ]
        updated = ps.update_persona("default", {"examples": examples})
        assert len(updated["examples"]) == 2
        assert updated["examples"][0]["input"] == "你好"
