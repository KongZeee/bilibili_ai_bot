"""
PRD V3 §7：profiles 多人格组配置测试

覆盖：
- PersonaStore 读取 profiles 配置
- set_account_profile / set_account_active_persona / get_account_persona_id
- 向后兼容旧格式 account_personas.json
- 可用人格列表 get_available_personas_for_account
- AccountInstance 加载 profile_id
- API 路由 switch-persona / personas / profiles
"""
import json
import os
import tempfile
from unittest.mock import MagicMock, AsyncMock

import pytest


# ═══════════════════════════════════════════════════════
#  PersonaStore profile 测试
# ═══════════════════════════════════════════════════════


@pytest.fixture
def persona_store_with_profiles(tmp_path):
    """构造一个含 profiles 配置的 PersonaStore"""
    from bilibot.services.persona_store import PersonaStore

    # 先创建 personas.json，含多个人格
    personas_data = {
        "current_persona_id": "default",
        "personas": [
            {
                "id": "default", "name": "默认人格", "description": "",
                "base_prompt": "默认", "speaking_style": "", "boundaries": "",
                "relationship_rules": "", "reply_rules": "", "proactive_comment_rules": "",
                "dynamic_rules": "", "weekly_rules": "", "examples": [], "enabled": True,
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
            },
            {
                "id": "tech", "name": "科技博主", "description": "",
                "base_prompt": "科技", "speaking_style": "", "boundaries": "",
                "relationship_rules": "", "reply_rules": "", "proactive_comment_rules": "",
                "dynamic_rules": "", "weekly_rules": "", "examples": [], "enabled": True,
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
            },
            {
                "id": "casual", "name": "休闲人格", "description": "",
                "base_prompt": "休闲", "speaking_style": "", "boundaries": "",
                "relationship_rules": "", "reply_rules": "", "proactive_comment_rules": "",
                "dynamic_rules": "", "weekly_rules": "", "examples": [], "enabled": True,
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
            },
        ],
    }
    with open(os.path.join(tmp_path, "personas.json"), "w", encoding="utf-8") as f:
        json.dump(personas_data, f, ensure_ascii=False)

    # mock config_loader，含 profiles 配置
    config_loader = MagicMock()
    config_loader.get_raw_config.return_value = {
        "profiles": [
            {
                "id": "multi_style",
                "name": "多风格切换",
                "default_persona": "tech",
                "personas": ["tech", "casual"],
            },
            {
                "id": "single",
                "name": "单风格",
                "default_persona": "default",
                "personas": ["default"],
            },
        ]
    }

    store = PersonaStore(data_dir=str(tmp_path), config_loader=config_loader)
    return store


def test_list_profiles(persona_store_with_profiles):
    """测试列出所有 profile"""
    profiles = persona_store_with_profiles.list_profiles()
    assert len(profiles) == 2
    ids = [p["id"] for p in profiles]
    assert "multi_style" in ids
    assert "single" in ids

    # multi_style profile 应该有 2 个可用人格
    multi = next(p for p in profiles if p["id"] == "multi_style")
    assert multi["default_persona"] == "tech"
    assert len(multi["personas"]) == 2
    persona_names = [p["name"] for p in multi["personas"]]
    assert "科技博主" in persona_names
    assert "休闲人格" in persona_names


def test_get_profile(persona_store_with_profiles):
    """测试获取单个 profile"""
    p = persona_store_with_profiles.get_profile("multi_style")
    assert p is not None
    assert p["default_persona"] == "tech"
    assert "tech" in p["personas"]

    # 不存在的 profile
    assert persona_store_with_profiles.get_profile("nonexistent") is None


def test_set_account_profile(persona_store_with_profiles):
    """测试绑定账号到 profile"""
    ok = persona_store_with_profiles.set_account_profile("acc_001", "multi_style")
    assert ok is True

    # 默认激活人格应该是 profile.default_persona = "tech"
    active = persona_store_with_profiles.get_account_persona_id("acc_001")
    assert active == "tech"


def test_set_account_profile_with_initial_persona(persona_store_with_profiles):
    """测试绑定 profile 时指定初始激活人格"""
    ok = persona_store_with_profiles.set_account_profile(
        "acc_001", "multi_style", persona_id="casual"
    )
    assert ok is True
    assert persona_store_with_profiles.get_account_persona_id("acc_001") == "casual"


def test_set_account_profile_invalid_persona(persona_store_with_profiles):
    """测试绑定 profile 时指定不在 personas 列表内的人格应失败"""
    ok = persona_store_with_profiles.set_account_profile(
        "acc_001", "multi_style", persona_id="default"  # default 不在 multi_style.personas
    )
    assert ok is False


def test_set_account_profile_nonexistent_profile(persona_store_with_profiles):
    """测试绑定不存在的 profile 应失败"""
    ok = persona_store_with_profiles.set_account_profile("acc_001", "nonexistent")
    assert ok is False


def test_set_account_active_persona(persona_store_with_profiles):
    """测试切换激活人格"""
    # 先绑定到 profile
    persona_store_with_profiles.set_account_profile("acc_001", "multi_style")
    assert persona_store_with_profiles.get_account_persona_id("acc_001") == "tech"

    # 切换到 casual
    ok = persona_store_with_profiles.set_account_active_persona("acc_001", "casual")
    assert ok is True
    assert persona_store_with_profiles.get_account_persona_id("acc_001") == "casual"

    # 切换回 tech
    ok = persona_store_with_profiles.set_account_active_persona("acc_001", "tech")
    assert ok is True
    assert persona_store_with_profiles.get_account_persona_id("acc_001") == "tech"


def test_set_account_active_persona_not_in_profile(persona_store_with_profiles):
    """测试切换到不在 profile.personas 内的人格应失败"""
    persona_store_with_profiles.set_account_profile("acc_001", "multi_style")
    # default 不在 multi_style.personas 中
    ok = persona_store_with_profiles.set_account_active_persona("acc_001", "default")
    assert ok is False


def test_get_available_personas_for_account(persona_store_with_profiles):
    """测试获取账号可用人格列表"""
    persona_store_with_profiles.set_account_profile("acc_001", "multi_style")
    personas = persona_store_with_profiles.get_available_personas_for_account("acc_001")
    assert len(personas) == 2
    ids = [p["id"] for p in personas]
    assert "tech" in ids
    assert "casual" in ids
    # tech 应标记为 is_default
    tech = next(p for p in personas if p["id"] == "tech")
    assert tech["is_default"] is True


def test_get_available_personas_no_profile(persona_store_with_profiles):
    """测试未绑定 profile 的账号返回所有人格"""
    personas = persona_store_with_profiles.get_available_personas_for_account("acc_no_bind")
    # 应返回所有启用人格
    assert len(personas) == 3
    ids = [p["id"] for p in personas]
    assert "default" in ids
    assert "tech" in ids
    assert "casual" in ids


def test_get_account_persona_status(persona_store_with_profiles):
    """测试获取账号人格绑定完整状态"""
    persona_store_with_profiles.set_account_profile("acc_001", "multi_style")
    status = persona_store_with_profiles.get_account_persona_status("acc_001")
    assert status["account_id"] == "acc_001"
    assert status["profile_id"] == "multi_style"
    assert status["active_persona_id"] == "tech"
    assert len(status["available_personas"]) == 2


def test_backward_compatibility_old_binding_format(persona_store_with_profiles):
    """测试旧格式 account_personas.json 兼容（字符串值）"""
    # 手动写一个旧格式文件
    path = persona_store_with_profiles._account_bindings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"acc_old": "casual"}, f)  # 旧格式：字符串

    # 加载应归一化为 dict
    bindings = persona_store_with_profiles._load_account_bindings()
    assert isinstance(bindings["acc_old"], dict)
    assert bindings["acc_old"]["active_persona_id"] == "casual"
    assert bindings["acc_old"]["profile_id"] is None

    # get_account_persona_id 应正确返回
    assert persona_store_with_profiles.get_account_persona_id("acc_old") == "casual"


def test_fallback_to_default_persona(persona_store_with_profiles):
    """测试未绑定账号回退到默认人格"""
    pid = persona_store_with_profiles.get_account_persona_id("acc_no_bind")
    assert pid == "default"  # _current_id


def test_set_account_persona_old_interface(persona_store_with_profiles):
    """测试旧接口 set_account_persona 仍可用（清除 profile 绑定）"""
    # 先绑定 profile
    persona_store_with_profiles.set_account_profile("acc_001", "multi_style")
    # 用旧接口绑定单个人格
    ok = persona_store_with_profiles.set_account_persona("acc_001", "default")
    assert ok is True
    # profile_id 应被清除
    bindings = persona_store_with_profiles._load_account_bindings()
    assert bindings["acc_001"]["profile_id"] is None
    assert bindings["acc_001"]["active_persona_id"] == "default"


# ═══════════════════════════════════════════════════════
#  AccountInstance 加载 profile_id 测试
# ═══════════════════════════════════════════════════════


def test_account_instance_reads_profile_id(tmp_path):
    """测试 AccountInstance 从 account_config 读取 profile_id"""
    from bilibot.account.instance import AccountInstance

    acc_config = {
        "id": "acc_001",
        "name": "测试账号",
        "profile_id": "multi_style",
        "persona_id": "default",  # 向后兼容字段
        "llm_id": "",
        "enabled": True,
    }
    inst = AccountInstance(
        account_id="acc_001",
        account_config=acc_config,
        persona_store=MagicMock(),
        llm_manager=MagicMock(),
        audit_store=MagicMock(),
        orchestrator=MagicMock(),
        context_builder=MagicMock(),
        app_config_loader=MagicMock(),
        data_root=str(tmp_path),
    )
    assert inst.profile_id == "multi_style"
    assert inst.persona_id == "default"


def test_account_instance_initialize_binds_profile(tmp_path, persona_store_with_profiles):
    """测试 AccountInstance.initialize 优先绑定 profile_id"""
    from bilibot.account.instance import AccountInstance

    acc_config = {
        "id": "acc_001",
        "name": "测试账号",
        "profile_id": "multi_style",
        "persona_id": "default",
        "llm_id": "",
        "enabled": True,
        "sessdata": "",
        "bili_jct": "",
    }
    # mock 其他依赖
    llm_mgr = MagicMock()
    llm_mgr.get_default.return_value = None
    llm_mgr.get_provider.return_value = None
    # LLM-501: resolve_provider 返回 (provider, effective_id, fallback_reason)
    llm_mgr.resolve_provider.return_value = (None, "", "")

    inst = AccountInstance(
        account_id="acc_001",
        account_config=acc_config,
        persona_store=persona_store_with_profiles,
        llm_manager=llm_mgr,
        audit_store=MagicMock(),
        orchestrator=MagicMock(),
        context_builder=MagicMock(),
        app_config_loader=MagicMock(),
        data_root=str(tmp_path),
    )

    # 运行 initialize（会触发 set_account_profile）
    import asyncio
    asyncio.run(inst.initialize())

    # 验证：激活人格应该是 profile.default_persona = "tech"
    active = persona_store_with_profiles.get_account_persona_id("acc_001")
    assert active == "tech"
