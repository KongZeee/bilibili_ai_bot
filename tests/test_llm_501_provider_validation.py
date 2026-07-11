"""
tests/test_llm_501_provider_validation.py - LLM-501 Provider 绑定校验测试

PRD-V5 §5.3 LLM-501：Provider 绑定校验

覆盖：
- 账号创建/更新时 llm_id 校验（存在 + enabled）
- Provider 删除前引用检查（含 force=true 清除引用）
- 状态 API 返回 configured_llm_id / effective_llm_id / fallback_reason
- 生产模式 allow_llm_fallback=false → 不可用 Provider 不回退，账号 degraded
- allow_llm_fallback=true → 回退到默认并记录原因
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from starlette.testclient import TestClient
from starlette.applications import Starlette

from bilibot.llm.manager import LLMManager
from bilibot.account.manager import AccountManager
from bilibot.account.instance import AccountInstance
from bilibot.app.config_loader import ConfigLoader
from bilibot.services.audit_store import AuditStore
from bilibot.services.persona_store import PersonaStore
from bilibot.api.accounts import create_accounts_routes
from bilibot.api.llm_providers import create_llm_providers_routes


# ═══════════════════════════════════════════════════════
#  Config & Fixtures
# ═══════════════════════════════════════════════════════

def _make_config(tmp_data_dir, allow_llm_fallback=False, accounts=None):
    """构造含多 LLM Provider + 多账号的测试配置"""
    cfg = {
        "llm_providers": [
            {
                "id": "primary",
                "name": "主LLM",
                "api_key": "sk-primary-test",
                "base_url": "http://localhost:8000/v1",
                "model": "test-model-primary",
                "enabled": True,
            },
            {
                "id": "secondary",
                "name": "备LLM",
                "api_key": "sk-secondary-test",
                "base_url": "http://localhost:8000/v1",
                "model": "test-model-secondary",
                "enabled": True,
            },
            {
                "id": "disabled_provider",
                "name": "已禁用LLM",
                "api_key": "sk-disabled-test",
                "base_url": "http://localhost:8000/v1",
                "model": "test-model-disabled",
                "enabled": False,
            },
        ],
        "default_llm": "primary",
        "allow_llm_fallback": allow_llm_fallback,
        "accounts": accounts or [
            {
                "id": "acc_main",
                "name": "主账号",
                "sessdata": "",
                "bili_jct": "",
                "dede_user_id": "",
                "llm_id": "primary",
                "enabled": False,
            },
        ],
        "default_account": "acc_main",
        "data_dir": tmp_data_dir,
    }
    return cfg


@pytest.fixture
def llm_config_loader(tmp_data_dir):
    return ConfigLoader(config_dict=_make_config(tmp_data_dir))


@pytest.fixture
def llm_manager(llm_config_loader):
    """真实 LLMManager（含 primary/secondary/disabled_provider 三个 Provider）"""
    mgr = LLMManager(llm_config_loader)
    mgr.initialize()
    return mgr


@pytest.fixture
def fallback_llm_manager(tmp_data_dir):
    """allow_llm_fallback=true 的 LLMManager"""
    loader = ConfigLoader(config_dict=_make_config(tmp_data_dir, allow_llm_fallback=True))
    mgr = LLMManager(loader)
    mgr.initialize()
    return mgr


@pytest.fixture
def account_manager(llm_manager, llm_config_loader, tmp_data_dir):
    """AccountManager（使用真实 LLMManager）"""
    persona_store = PersonaStore(data_dir=tmp_data_dir)
    audit_store = AuditStore(data_dir=tmp_data_dir)
    mgr = AccountManager(
        persona_store=persona_store,
        llm_manager=llm_manager,
        audit_store=audit_store,
        orchestrator=MagicMock(),
        context_builder=MagicMock(),
        app_config_loader=llm_config_loader,
        data_root=tmp_data_dir,
        safety_checker=None,
    )
    mgr.initialize()
    return mgr


@pytest.fixture
def config_path(tmp_data_dir):
    return str(tmp_data_dir + "/test_config.yaml")


@pytest.fixture
def accounts_api(account_manager, llm_config_loader, config_path):
    routes = create_accounts_routes(account_manager, llm_config_loader, config_path)
    return TestClient(Starlette(routes=routes))


@pytest.fixture
def llm_api(llm_manager, llm_config_loader, account_manager, config_path):
    routes = create_llm_providers_routes(
        llm_manager, llm_config_loader, config_path, account_manager=account_manager
    )
    return TestClient(Starlette(routes=routes))


@pytest.fixture
def llm_api_no_am(llm_manager, llm_config_loader, config_path):
    """LLM API 不传 account_manager（向后兼容）"""
    routes = create_llm_providers_routes(llm_manager, llm_config_loader, config_path)
    return TestClient(Starlette(routes=routes))


# ═══════════════════════════════════════════════════════
#  LLMManager.resolve_provider 单元测试
# ═══════════════════════════════════════════════════════

class TestResolveProvider:
    """LLMManager.resolve_provider 核心解析逻辑"""

    def test_valid_llm_id_returns_provider(self, llm_manager):
        """有效 llm_id → 返回对应 provider"""
        provider, eff_id, reason = llm_manager.resolve_provider("primary")
        assert provider is not None
        assert provider.llm_id == "primary"
        assert eff_id == "primary"
        assert reason == ""

    def test_empty_llm_id_returns_default(self, llm_manager):
        """空 llm_id → 返回默认 provider（非回退）"""
        provider, eff_id, reason = llm_manager.resolve_provider("")
        assert provider is not None
        assert eff_id == "primary"  # default_llm
        assert reason == ""  # 无回退原因

    def test_none_llm_id_returns_default(self, llm_manager):
        """None llm_id → 返回默认 provider"""
        provider, eff_id, reason = llm_manager.resolve_provider(None)
        assert provider is not None
        assert eff_id == "primary"
        assert reason == ""

    def test_nonexistent_no_fallback(self, llm_manager):
        """不存在的 llm_id + allow_fallback=false → 返回 None"""
        provider, eff_id, reason = llm_manager.resolve_provider("nonexistent")
        assert provider is None
        assert eff_id == ""
        assert "not found" in reason

    def test_nonexistent_with_fallback(self, fallback_llm_manager):
        """不存在的 llm_id + allow_fallback=true → 回退到默认"""
        provider, eff_id, reason = fallback_llm_manager.resolve_provider("nonexistent")
        assert provider is not None
        assert eff_id == "primary"  # 回退到默认
        assert "not found" in reason

    def test_disabled_no_fallback(self, llm_manager):
        """已禁用 llm_id + allow_fallback=false → 返回 None"""
        provider, eff_id, reason = llm_manager.resolve_provider("disabled_provider")
        assert provider is None
        assert eff_id == ""
        assert "disabled" in reason

    def test_disabled_with_fallback(self, fallback_llm_manager):
        """已禁用 llm_id + allow_fallback=true → 回退到默认"""
        provider, eff_id, reason = fallback_llm_manager.resolve_provider("disabled_provider")
        assert provider is not None
        assert eff_id == "primary"
        assert "disabled" in reason

    def test_explicit_allow_fallback_overrides_config(self, llm_manager):
        """显式 allow_fallback=True 覆盖配置中的 false"""
        # llm_manager 的 allow_llm_fallback=False（来自配置）
        assert llm_manager.allow_llm_fallback is False
        # 但显式传 allow_fallback=True
        provider, eff_id, reason = llm_manager.resolve_provider(
            "nonexistent", allow_fallback=True
        )
        assert provider is not None
        assert eff_id == "primary"
        assert "not found" in reason

    def test_explicit_no_fallback_overrides_config(self, fallback_llm_manager):
        """显式 allow_fallback=False 覆盖配置中的 true"""
        assert fallback_llm_manager.allow_llm_fallback is True
        provider, eff_id, reason = fallback_llm_manager.resolve_provider(
            "nonexistent", allow_fallback=False
        )
        assert provider is None
        assert eff_id == ""
        assert "not found" in reason

    def test_allow_llm_fallback_property(self, llm_manager, fallback_llm_manager):
        """allow_llm_fallback 属性正确反映配置"""
        assert llm_manager.allow_llm_fallback is False
        assert fallback_llm_manager.allow_llm_fallback is True


# ═══════════════════════════════════════════════════════
#  账号创建/更新 llm_id 校验测试
# ═══════════════════════════════════════════════════════

class TestAccountLLMValidation:
    """POST/PATCH /api/accounts 的 llm_id 校验"""

    def test_create_with_valid_llm_id(self, accounts_api):
        """有效 llm_id → 创建成功"""
        resp = accounts_api.post("/api/accounts", json={
            "id": "valid_acc",
            "name": "有效账号",
            "llm_id": "primary",
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_create_with_nonexistent_llm_id(self, accounts_api):
        """不存在的 llm_id → 400 LLM_PROVIDER_NOT_FOUND"""
        resp = accounts_api.post("/api/accounts", json={
            "id": "bad_acc",
            "name": "无效账号",
            "llm_id": "nonexistent_provider",
            "enabled": False,
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "LLM_PROVIDER_NOT_FOUND"

    def test_create_with_disabled_provider(self, accounts_api):
        """已禁用 Provider 的 llm_id → 400 LLM_PROVIDER_NOT_FOUND"""
        resp = accounts_api.post("/api/accounts", json={
            "id": "disabled_acc",
            "name": "禁用Provider账号",
            "llm_id": "disabled_provider",
            "enabled": False,
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "LLM_PROVIDER_NOT_FOUND"
        assert "disabled" in data["error"]["message"]

    def test_create_with_empty_llm_id(self, accounts_api):
        """空 llm_id → 创建成功（使用默认）"""
        resp = accounts_api.post("/api/accounts", json={
            "id": "default_llm_acc",
            "name": "默认LLM账号",
            "llm_id": "",
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_create_with_no_llm_id_field(self, accounts_api):
        """不传 llm_id 字段 → 创建成功"""
        resp = accounts_api.post("/api/accounts", json={
            "id": "no_llm_field_acc",
            "name": "无LLM字段账号",
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_update_with_invalid_llm_id(self, accounts_api):
        """PATCH 无效 llm_id → 400"""
        # 先创建一个有效账号
        accounts_api.post("/api/accounts", json={
            "id": "update_target",
            "name": "待更新账号",
            "llm_id": "primary",
            "enabled": False,
        })
        # PATCH 无效 llm_id
        resp = accounts_api.patch("/api/accounts/update_target", json={
            "llm_id": "nonexistent_provider",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "LLM_PROVIDER_NOT_FOUND"

    def test_update_with_valid_llm_id(self, accounts_api):
        """PATCH 有效 llm_id → 成功"""
        accounts_api.post("/api/accounts", json={
            "id": "update_ok",
            "name": "待更新账号2",
            "llm_id": "primary",
            "enabled": False,
        })
        resp = accounts_api.patch("/api/accounts/update_ok", json={
            "llm_id": "secondary",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_update_with_disabled_provider(self, accounts_api):
        """PATCH 已禁用 Provider → 400"""
        accounts_api.post("/api/accounts", json={
            "id": "update_disabled",
            "name": "待更新账号3",
            "llm_id": "primary",
            "enabled": False,
        })
        resp = accounts_api.patch("/api/accounts/update_disabled", json={
            "llm_id": "disabled_provider",
        })
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "LLM_PROVIDER_NOT_FOUND"

    def test_update_llm_id_to_empty(self, accounts_api):
        """PATCH llm_id 为空 → 成功（清除绑定，用默认）"""
        accounts_api.post("/api/accounts", json={
            "id": "update_to_empty",
            "name": "清空LLM账号",
            "llm_id": "primary",
            "enabled": False,
        })
        resp = accounts_api.patch("/api/accounts/update_to_empty", json={
            "llm_id": "",
        })
        assert resp.status_code == 200

    def test_update_without_llm_id_field(self, accounts_api):
        """PATCH 不含 llm_id 字段 → 跳过校验，正常更新"""
        accounts_api.post("/api/accounts", json={
            "id": "update_no_llm",
            "name": "原名",
            "llm_id": "primary",
            "enabled": False,
        })
        resp = accounts_api.patch("/api/accounts/update_no_llm", json={
            "name": "新名",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True


# ═══════════════════════════════════════════════════════
#  Provider 删除引用检查测试
# ═══════════════════════════════════════════════════════

class TestProviderDeletionReferenceCheck:
    """DELETE /api/llm-providers/{id} 引用检查"""

    def test_delete_no_references(self, accounts_api, llm_api):
        """删除无引用的 Provider → 成功"""
        # secondary 没有被任何账号引用
        resp = llm_api.delete("/api/llm-providers/secondary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    def test_delete_with_references(self, accounts_api, llm_api):
        """删除有引用的 Provider → 409 LLM_PROVIDER_IN_USE"""
        # 创建引用 secondary 的账号
        accounts_api.post("/api/accounts", json={
            "id": "ref_acc",
            "name": "引用账号",
            "llm_id": "secondary",
            "enabled": False,
        })
        # 尝试删除 secondary
        resp = llm_api.delete("/api/llm-providers/secondary")
        assert resp.status_code == 409
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "LLM_PROVIDER_IN_USE"
        assert "ref_acc" in data["error"]["details"]["referencing_accounts"]

    def test_delete_with_force_clears_references(self, accounts_api, llm_api, account_manager):
        """force=true 删除 → 成功，清除引用"""
        accounts_api.post("/api/accounts", json={
            "id": "force_ref_acc",
            "name": "强制删除引用账号",
            "llm_id": "secondary",
            "enabled": False,
        })
        # 确认引用存在
        assert account_manager.get_config("force_ref_acc")["llm_id"] == "secondary"
        # force=true 删除
        resp = llm_api.delete("/api/llm-providers/secondary?force=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "force_ref_acc" in data["data"]["cleared_references"]
        # 引用已清除
        cfg = account_manager.get_config("force_ref_acc")
        assert cfg["llm_id"] == ""

    def test_delete_nonexistent_provider(self, llm_api):
        """删除不存在的 Provider → 404"""
        resp = llm_api.delete("/api/llm-providers/nonexistent")
        assert resp.status_code == 400
        data = resp.json()
        assert data["error"]["code"] == "NOT_FOUND"

    def test_delete_last_default_provider_blocked(self, llm_api):
        """不能删除最后一个默认 Provider"""
        # 删除 secondary 和 disabled_provider，只剩 primary（默认）
        llm_api.delete("/api/llm-providers/secondary")
        llm_api.delete("/api/llm-providers/disabled_provider")
        # 尝试删除最后一个默认
        resp = llm_api.delete("/api/llm-providers/primary")
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "LAST_PROVIDER"

    def test_delete_without_account_manager_backward_compat(self, llm_api_no_am):
        """不传 account_manager → 跳过引用检查（向后兼容）"""
        # 即使有引用也不会被检查（但此 fixture 没有引用 secondary）
        resp = llm_api_no_am.delete("/api/llm-providers/secondary")
        assert resp.status_code == 200

    def test_delete_with_multiple_references(self, accounts_api, llm_api):
        """多个账号引用同一 Provider → 409 含全部引用列表"""
        accounts_api.post("/api/accounts", json={
            "id": "multi_ref_1", "name": "引用1", "llm_id": "secondary", "enabled": False,
        })
        accounts_api.post("/api/accounts", json={
            "id": "multi_ref_2", "name": "引用2", "llm_id": "secondary", "enabled": False,
        })
        resp = llm_api.delete("/api/llm-providers/secondary")
        assert resp.status_code == 409
        data = resp.json()
        refs = data["error"]["details"]["referencing_accounts"]
        assert "multi_ref_1" in refs
        assert "multi_ref_2" in refs


# ═══════════════════════════════════════════════════════
#  状态 API 字段测试
# ═══════════════════════════════════════════════════════

class TestStatusAPIFields:
    """状态 API 返回 configured_llm_id / effective_llm_id / fallback_reason"""

    def test_list_accounts_has_llm_fields(self, accounts_api):
        """GET /api/accounts 每个账号状态含 LLM 字段"""
        resp = accounts_api.get("/api/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        for acc in data["data"]:
            assert "configured_llm_id" in acc
            assert "effective_llm_id" in acc
            assert "fallback_reason" in acc

    def test_create_returns_llm_fields(self, accounts_api):
        """POST /api/accounts 返回状态含 LLM 字段"""
        resp = accounts_api.post("/api/accounts", json={
            "id": "status_acc",
            "name": "状态账号",
            "llm_id": "primary",
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        status = data["data"]
        assert status["configured_llm_id"] == "primary"

    def test_update_returns_llm_fields(self, accounts_api):
        """PATCH /api/accounts/{id} 返回状态含 LLM 字段"""
        accounts_api.post("/api/accounts", json={
            "id": "patch_status_acc",
            "name": "原名",
            "llm_id": "primary",
            "enabled": False,
        })
        resp = accounts_api.patch("/api/accounts/patch_status_acc", json={"name": "新名"})
        assert resp.status_code == 200
        status = resp.json()["data"]
        assert "configured_llm_id" in status
        assert "effective_llm_id" in status
        assert "fallback_reason" in status

    def test_configured_llm_id_matches_config(self, accounts_api):
        """configured_llm_id 等于账号配置中的 llm_id"""
        accounts_api.post("/api/accounts", json={
            "id": "cfg_llm_acc",
            "name": "配置LLM账号",
            "llm_id": "secondary",
            "enabled": False,
        })
        resp = accounts_api.get("/api/accounts")
        accounts = resp.json()["data"]
        acc = next(a for a in accounts if a["account_id"] == "cfg_llm_acc")
        assert acc["configured_llm_id"] == "secondary"


# ═══════════════════════════════════════════════════════
#  生产模式回退控制测试
# ═══════════════════════════════════════════════════════

class TestFallbackControl:
    """allow_llm_fallback 回退控制"""

    def test_production_mode_no_fallback(self, llm_manager):
        """生产模式（allow_llm_fallback=false）+ 不可用 Provider → 不回退"""
        assert llm_manager.allow_llm_fallback is False
        provider, eff_id, reason = llm_manager.resolve_provider("nonexistent")
        assert provider is None
        assert eff_id == ""
        assert reason != ""  # 有原因说明

    def test_production_mode_disabled_no_fallback(self, llm_manager):
        """生产模式 + 已禁用 Provider → 不回退"""
        provider, eff_id, reason = llm_manager.resolve_provider("disabled_provider")
        assert provider is None
        assert "disabled" in reason

    def test_allow_fallback_true_returns_default(self, fallback_llm_manager):
        """allow_llm_fallback=true + 不可用 Provider → 回退到默认"""
        provider, eff_id, reason = fallback_llm_manager.resolve_provider("nonexistent")
        assert provider is not None
        assert eff_id == "primary"  # default
        assert "not found" in reason

    def test_allow_fallback_true_disabled_returns_default(self, fallback_llm_manager):
        """allow_llm_fallback=true + 已禁用 Provider → 回退到默认"""
        provider, eff_id, reason = fallback_llm_manager.resolve_provider("disabled_provider")
        assert provider is not None
        assert eff_id == "primary"
        assert "disabled" in reason


# ═══════════════════════════════════════════════════════
#  AccountInstance 状态字段测试
# ═══════════════════════════════════════════════════════

class TestAccountInstanceStatus:
    """AccountInstance.get_status() 的 LLM-501 字段"""

    def _make_instance(self, llm_manager, tmp_data_dir, llm_id=""):
        """创建 AccountInstance（不调用 initialize）"""
        return AccountInstance(
            account_id="test_acc",
            account_config={
                "id": "test_acc",
                "name": "测试账号",
                "llm_id": llm_id,
                "enabled": True,
                "sessdata": "",
                "bili_jct": "",
            },
            persona_store=MagicMock(),
            llm_manager=llm_manager,
            audit_store=MagicMock(),
            orchestrator=MagicMock(),
            context_builder=MagicMock(),
            app_config_loader=MagicMock(),
            data_root=tmp_data_dir,
            safety_checker=None,
        )

    def test_status_has_llm_fields(self, llm_manager, tmp_data_dir):
        """get_status() 返回 configured/effective/fallback 字段"""
        acc = self._make_instance(llm_manager, tmp_data_dir, llm_id="primary")
        status = acc.get_status()
        assert "configured_llm_id" in status
        assert "effective_llm_id" in status
        assert "fallback_reason" in status

    def test_degraded_state_when_llm_unavailable(self, llm_manager, tmp_data_dir):
        """LLM 不可用 + 账号运行中 → state=degraded"""
        acc = self._make_instance(llm_manager, tmp_data_dir, llm_id="nonexistent")
        # 模拟 LLM 解析失败
        acc._configured_llm_id = "nonexistent"
        acc._effective_llm_id = ""
        acc._fallback_reason = "configured provider 'nonexistent' not found"
        acc._llm_config_error = True
        # 模拟运行中
        acc._started = True
        acc.scheduler = MagicMock()
        acc._scheduler_task = MagicMock()
        acc._scheduler_task.done.return_value = False

        status = acc.get_status()
        assert status["state"] == "degraded"
        assert status["configured_llm_id"] == "nonexistent"
        assert status["effective_llm_id"] == ""
        assert "not found" in status["fallback_reason"]
        assert status["has_llm"] is False  # llm is None (not initialized)

    def test_running_state_when_llm_available(self, llm_manager, tmp_data_dir):
        """LLM 可用 + 账号运行中 → state=running"""
        acc = self._make_instance(llm_manager, tmp_data_dir, llm_id="primary")
        # 模拟 LLM 解析成功
        provider, eff_id, reason = llm_manager.resolve_provider("primary")
        acc.llm = provider
        acc._configured_llm_id = "primary"
        acc._effective_llm_id = eff_id
        acc._fallback_reason = reason
        acc._llm_config_error = False
        # 模拟运行中
        acc._started = True
        acc.scheduler = MagicMock()
        acc._scheduler_task = MagicMock()
        acc._scheduler_task.done.return_value = False

        status = acc.get_status()
        assert status["state"] == "running"
        assert status["configured_llm_id"] == "primary"
        assert status["effective_llm_id"] == "primary"
        assert status["fallback_reason"] == ""
        assert status["has_llm"] is True

    def test_fallback_state_when_allow_fallback(self, fallback_llm_manager, tmp_data_dir):
        """allow_llm_fallback=true + 不可用 Provider → 回退到默认，state=running"""
        acc = self._make_instance(fallback_llm_manager, tmp_data_dir, llm_id="nonexistent")
        # 模拟 LLM 回退解析
        provider, eff_id, reason = fallback_llm_manager.resolve_provider("nonexistent")
        acc.llm = provider
        acc._configured_llm_id = "nonexistent"
        acc._effective_llm_id = eff_id  # "primary"
        acc._fallback_reason = reason
        acc._llm_config_error = False  # 有回退所以不算错误
        # 模拟运行中
        acc._started = True
        acc.scheduler = MagicMock()
        acc._scheduler_task = MagicMock()
        acc._scheduler_task.done.return_value = False

        status = acc.get_status()
        assert status["state"] == "running"
        assert status["configured_llm_id"] == "nonexistent"
        assert status["effective_llm_id"] == "primary"
        assert "not found" in status["fallback_reason"]
        assert status["has_llm"] is True

    def test_stopped_state_no_llm_error(self, llm_manager, tmp_data_dir):
        """账号未运行 → state=stopped（不因 LLM 错误变为 degraded）"""
        acc = self._make_instance(llm_manager, tmp_data_dir, llm_id="nonexistent")
        acc._llm_config_error = True
        # 不模拟运行中（默认 _started=False）
        status = acc.get_status()
        assert status["state"] == "stopped"

    def test_update_config_syncs_configured_llm_id(self, llm_manager, tmp_data_dir):
        """update_config 同步 configured_llm_id"""
        acc = self._make_instance(llm_manager, tmp_data_dir, llm_id="primary")
        assert acc._configured_llm_id == "primary"
        # 更新配置
        acc.update_config({
            "id": "test_acc",
            "name": "测试账号",
            "llm_id": "secondary",
            "enabled": True,
        })
        assert acc._configured_llm_id == "secondary"
        assert acc.llm_id == "secondary"
