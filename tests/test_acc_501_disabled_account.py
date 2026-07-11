"""
tests/test_acc_501_disabled_account.py - ACC-501 禁用账号配置零丢失测试

PRD-V5 §4.2 ACC-501：禁用账号配置零丢失

覆盖：
- enabled + disabled + init-failed 账号混合，PATCH 任意账号，全部保留
- 禁用账号 Cookie 字节不变
- 禁用账号可通过 PATCH enabled=true 重新启用
- DELETE 删除账号并写审计
- PATCH with __REDACTED__ 保留原 Cookie
"""
import pytest
from unittest.mock import MagicMock
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route

from bilibot.account.config_registry import AccountConfigRegistry, REDACTED_PLACEHOLDER
from bilibot.account.manager import AccountManager
from bilibot.app.config_loader import ConfigLoader
from bilibot.services.audit_store import AuditStore
from bilibot.services.persona_store import PersonaStore
from bilibot.api.accounts import create_accounts_routes


# ═══════════════════════════════════════════════════════
#  Fixtures
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
    # LLM-501: resolve_provider 返回 (provider, effective_id, fallback_reason)
    mgr.resolve_provider.return_value = (None, "", "")
    return mgr


@pytest.fixture
def mixed_accounts_config(tmp_data_dir):
    """配置含 3 个账号：enabled / disabled / init-failed（enabled=true 但凭据缺失）"""
    return ConfigLoader(config_dict={
        "accounts": [
            {
                "id": "main_acc",
                "name": "主账号",
                "sessdata": "main-sessdata-secret",
                "bili_jct": "main-jct-secret",
                "dede_user_id": "10001",
                "buvid3": "main-buvid3",
                "refresh_token": "main-refresh",
                "profile_id": "",
                "persona_id": "default",
                "llm_id": "",
                "enabled": True,
            },
            {
                "id": "disabled_acc",
                "name": "已禁用账号",
                "sessdata": "disabled-sessdata-secret",
                "bili_jct": "disabled-jct-secret",
                "dede_user_id": "20002",
                "buvid3": "disabled-buvid3",
                "refresh_token": "disabled-refresh",
                "profile_id": "",
                "persona_id": "default",
                "llm_id": "",
                "enabled": False,
            },
            {
                "id": "broken_acc",
                "name": "初始化失败账号",
                "sessdata": "",
                "bili_jct": "",
                "dede_user_id": "",
                "buvid3": "",
                "refresh_token": "",
                "profile_id": "",
                "persona_id": "",
                "llm_id": "",
                "enabled": True,
            },
        ],
        "default_account": "main_acc",
        "data_dir": tmp_data_dir,
    })


@pytest.fixture
def account_manager(
    mixed_accounts_config,
    tmp_data_dir,
    mock_orchestrator,
    mock_context_builder,
    mock_llm_manager,
):
    """构造 AccountManager（含 enabled / disabled / broken 三个账号）"""
    persona_store = PersonaStore(data_dir=tmp_data_dir)
    audit_store = AuditStore(data_dir=tmp_data_dir)
    mgr = AccountManager(
        persona_store=persona_store,
        llm_manager=mock_llm_manager,
        audit_store=audit_store,
        orchestrator=mock_orchestrator,
        context_builder=mock_context_builder,
        app_config_loader=mixed_accounts_config,
        data_root=tmp_data_dir,
        safety_checker=None,
    )
    mgr.initialize()
    return mgr


@pytest.fixture
def api_client(account_manager, mixed_accounts_config, tmp_data_dir):
    """构造 accounts API 测试客户端"""
    routes = create_accounts_routes(
        account_manager,
        mixed_accounts_config,
        config_path=str(tmp_data_dir + "/test_config.yaml"),
    )
    app = Starlette(routes=routes)
    return TestClient(app)


# ═══════════════════════════════════════════════════════
#  AccountConfigRegistry 单元测试
# ═══════════════════════════════════════════════════════

class TestAccountConfigRegistry:
    """配置注册表单元测试"""

    def test_load_all_accounts_including_disabled(self):
        """ACC-501：加载 ALL 账号（含 disabled）"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [
                {"id": "a", "enabled": True, "sessdata": "aaa"},
                {"id": "b", "enabled": False, "sessdata": "bbb"},
                {"id": "c", "enabled": True, "sessdata": ""},
            ]
        })
        assert set(registry.list_ids()) == {"a", "b", "c"}
        assert "b" in registry  # disabled 账号也在注册表中
        assert registry.has("b")

    def test_list_enabled_only(self):
        """list_enabled 只返回 enabled=true"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [
                {"id": "a", "enabled": True},
                {"id": "b", "enabled": False},
                {"id": "c"},  # 默认 enabled=true
            ]
        })
        assert set(registry.list_enabled()) == {"a", "c"}

    def test_update_sensitive_field_placeholder_preserves_original(self):
        """ACC-501：敏感字段占位符保留原值"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [{"id": "a", "sessdata": "original-secret", "bili_jct": "original-jct"}]
        })
        # PATCH with __REDACTED__
        registry.update("a", {"sessdata": REDACTED_PLACEHOLDER, "name": "新名称"})
        cfg = registry.get("a")
        assert cfg["sessdata"] == "original-secret"  # 保留原值
        assert cfg["name"] == "新名称"  # 非敏感字段正常更新

    def test_update_sensitive_field_empty_preserves_original(self):
        """ACC-501：敏感字段空字符串保留原值"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [{"id": "a", "sessdata": "original-secret"}]
        })
        registry.update("a", {"sessdata": ""})
        cfg = registry.get("a")
        assert cfg["sessdata"] == "original-secret"

    def test_update_sensitive_field_none_preserves_original(self):
        """ACC-501：敏感字段 None 保留原值"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [{"id": "a", "sessdata": "original-secret"}]
        })
        registry.update("a", {"sessdata": None})
        cfg = registry.get("a")
        assert cfg["sessdata"] == "original-secret"

    def test_update_sensitive_field_real_value_updates(self):
        """ACC-501：敏感字段真实新值正常更新"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [{"id": "a", "sessdata": "old-secret"}]
        })
        registry.update("a", {"sessdata": "new-secret"})
        cfg = registry.get("a")
        assert cfg["sessdata"] == "new-secret"

    def test_update_does_not_modify_id(self):
        """ACC-501：PATCH 不允许修改 ID"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({"accounts": [{"id": "a"}]})
        registry.update("a", {"id": "b"})
        cfg = registry.get("a")
        assert cfg["id"] == "a"
        assert not registry.has("b")

    def test_delete_is_explicit(self):
        """ACC-501：删除是显式操作"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({"accounts": [{"id": "a"}, {"id": "b"}]})
        assert registry.delete("a") is True
        assert not registry.has("a")
        assert registry.has("b")
        # 再次删除已删除的返回 False
        assert registry.delete("a") is False

    def test_update_does_not_delete(self):
        """ACC-501：PATCH 永不删除账号"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({"accounts": [{"id": "a"}, {"id": "b"}]})
        # 即使 patch 为空，账号仍然存在
        registry.update("a", {})
        assert registry.has("a")
        assert registry.has("b")
        assert len(registry) == 2

    def test_add_duplicate_raises(self):
        """重复添加同 ID 报错"""
        registry = AccountConfigRegistry()
        registry.add({"id": "a"})
        with pytest.raises(ValueError, match="已存在"):
            registry.add({"id": "a"})

    def test_sync_from_raw_picks_up_external_changes(self):
        """sync_from_raw 拾取外部写入（如 qrlogin）"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({
            "accounts": [{"id": "a", "sessdata": "old"}]
        })
        # 模拟外部写入（qrlogin 更新了 sessdata）
        registry.sync_from_raw({
            "accounts": [{"id": "a", "sessdata": "new-from-qrlogin"}]
        })
        cfg = registry.get("a")
        assert cfg["sessdata"] == "new-from-qrlogin"

    def test_get_returns_deepcopy(self):
        """get 返回深拷贝，外部修改不影响注册表"""
        registry = AccountConfigRegistry()
        registry.load_from_raw({"accounts": [{"id": "a", "sessdata": "secret"}]})
        cfg = registry.get("a")
        cfg["sessdata"] = "tampered"
        # 注册表中的值不变
        original = registry.get("a")
        assert original["sessdata"] == "secret"


# ═══════════════════════════════════════════════════════
#  AccountManager 集成测试
# ═══════════════════════════════════════════════════════

class TestAccountManagerDisabledPreservation:
    """AccountManager 禁用账号保留测试"""

    def test_initialize_loads_all_accounts(self, account_manager):
        """ACC-501：initialize 加载 ALL 账号到配置注册表"""
        # 配置注册表含 3 个账号
        ids = account_manager.list_account_ids()
        assert set(ids) == {"main_acc", "disabled_acc", "broken_acc"}

    def test_runtime_only_enabled(self, account_manager):
        """ACC-501：运行时实例仅 enabled 账号（disabled 不创建）"""
        # disabled_acc 无运行时实例
        assert account_manager.get_account("disabled_acc") is None
        # main_acc 和 broken_acc 有运行时实例（enabled=true）
        assert account_manager.get_account("main_acc") is not None
        assert account_manager.get_account("broken_acc") is not None

    def test_save_preserves_all_accounts(self, account_manager):
        """ACC-501：save_to_config 保留全部账号（含 disabled）"""
        result = account_manager.save_to_config()
        accounts = result["accounts"]
        ids = [a["id"] for a in accounts]
        assert set(ids) == {"main_acc", "disabled_acc", "broken_acc"}

    def test_save_preserves_disabled_account_cookie(self, account_manager):
        """ACC-501：禁用账号 Cookie 字节不变"""
        result = account_manager.save_to_config()
        accounts = {a["id"]: a for a in result["accounts"]}
        # disabled 账号的敏感字段完全保留
        assert accounts["disabled_acc"]["sessdata"] == "disabled-sessdata-secret"
        assert accounts["disabled_acc"]["bili_jct"] == "disabled-jct-secret"
        assert accounts["disabled_acc"]["buvid3"] == "disabled-buvid3"
        assert accounts["disabled_acc"]["refresh_token"] == "disabled-refresh"
        assert accounts["disabled_acc"]["enabled"] is False

    def test_patch_preserves_all_accounts(self, account_manager):
        """ACC-501：PATCH 任意账号后全部账号仍保留"""
        # PATCH main_acc（改 name）
        account_manager.update_account_config("main_acc", {"name": "新名称"})
        result = account_manager.save_to_config()
        ids = [a["id"] for a in result["accounts"]]
        # 三个账号仍然都在
        assert set(ids) == {"main_acc", "disabled_acc", "broken_acc"}
        # main_acc 的 name 已更新
        main = {a["id"]: a for a in result["accounts"]}
        assert main["main_acc"]["name"] == "新名称"

    def test_patch_redacted_preserves_original_cookie(self, account_manager):
        """ACC-501：PATCH with __REDACTED__ 保留原 Cookie"""
        # PATCH main_acc with __REDACTED__ for sessdata
        account_manager.update_account_config("main_acc", {
            "sessdata": REDACTED_PLACEHOLDER,
            "bili_jct": REDACTED_PLACEHOLDER,
            "name": "改了名字",
        })
        result = account_manager.save_to_config()
        main = {a["id"]: a for a in result["accounts"]}["main_acc"]
        # 原始 Cookie 保留
        assert main["sessdata"] == "main-sessdata-secret"
        assert main["bili_jct"] == "main-jct-secret"
        # 非敏感字段正常更新
        assert main["name"] == "改了名字"

    def test_disabled_account_can_be_reenabled(self, account_manager):
        """ACC-501：禁用账号可通过 PATCH enabled=true 重新启用"""
        # 初始状态：disabled
        assert account_manager.get_account("disabled_acc") is None
        # PATCH enabled=true
        account_manager.update_account_config("disabled_acc", {"enabled": True})
        # 配置注册表中已更新
        cfg = account_manager.get_config("disabled_acc")
        assert cfg["enabled"] is True
        # 创建运行时实例
        assert account_manager.create_runtime_instance("disabled_acc") is True
        assert account_manager.get_account("disabled_acc") is not None
        # save_to_config 保留 enabled=true
        result = account_manager.save_to_config()
        disabled = {a["id"]: a for a in result["accounts"]}["disabled_acc"]
        assert disabled["enabled"] is True

    def test_patch_does_not_delete_accounts(self, account_manager):
        """ACC-501：PATCH 永不删除账号"""
        # 即使 PATCH 一个不存在的账号，也不会影响已有账号
        result = account_manager.update_account_config("nonexistent", {"name": "x"})
        assert result is False
        # 已有账号仍在
        assert account_manager.has_account("main_acc")
        assert account_manager.has_account("disabled_acc")
        assert account_manager.has_account("broken_acc")

    def test_has_account_includes_disabled(self, account_manager):
        """ACC-501：has_account 含禁用账号"""
        assert account_manager.has_account("main_acc")
        assert account_manager.has_account("disabled_acc")
        assert account_manager.has_account("broken_acc")
        assert not account_manager.has_account("nonexistent")

    def test_len_includes_disabled(self, account_manager):
        """ACC-501：__len__ 含禁用账号"""
        assert len(account_manager) == 3

    def test_contains_includes_disabled(self, account_manager):
        """ACC-501：__contains__ 含禁用账号"""
        assert "disabled_acc" in account_manager

    def test_list_accounts_includes_disabled(self, account_manager):
        """ACC-501：list_accounts 返回全部账号（含 disabled）"""
        statuses = account_manager.list_accounts()
        ids = [s["account_id"] for s in statuses]
        assert set(ids) == {"main_acc", "disabled_acc", "broken_acc"}
        # disabled 账号状态正确
        disabled_status = next(s for s in statuses if s["account_id"] == "disabled_acc")
        assert disabled_status["enabled"] is False
        assert disabled_status["state"] == "disabled"
        assert disabled_status["running"] is False

    def test_get_account_status_for_disabled(self, account_manager):
        """ACC-501：get_account_status 支持禁用账号"""
        status = account_manager.get_account_status("disabled_acc")
        assert status is not None
        assert status["enabled"] is False
        assert status["state"] == "disabled"

    def test_get_account_status_nonexistent_returns_none(self, account_manager):
        """get_account_status 不存在时返回 None"""
        assert account_manager.get_account_status("nonexistent") is None


# ═══════════════════════════════════════════════════════
#  DELETE 审计测试
# ═══════════════════════════════════════════════════════

class TestDeleteWithAudit:
    """DELETE 删除账号并写审计测试"""

    def test_delete_removes_from_registry(self, account_manager):
        """ACC-501：DELETE 从配置注册表删除"""
        assert account_manager.has_account("broken_acc")
        # 同步删除（remove_account）
        result = account_manager.remove_account("broken_acc")
        assert result is True
        assert not account_manager.has_account("broken_acc")

    @pytest.mark.asyncio
    async def test_delete_async_removes_and_audits(self, account_manager):
        """ACC-501：DELETE async 删除并写审计"""
        # 记录删除前的审计数
        audit_store = account_manager.audit_store
        before_count = audit_store.count()

        result = await account_manager.remove_account_async("broken_acc")
        assert result is True
        assert not account_manager.has_account("broken_acc")

        # 审计记录已写入
        after_count = audit_store.count()
        assert after_count == before_count + 1

        # 查询删除审计
        delete_audits = audit_store.query(scene="account_delete")
        assert len(delete_audits) >= 1
        latest = delete_audits[0]
        assert "broken_acc" in latest["input_summary"]

    @pytest.mark.asyncio
    async def test_delete_disabled_account(self, account_manager):
        """ACC-501：DELETE 能删除禁用账号（无运行时实例）"""
        result = await account_manager.remove_account_async("disabled_acc")
        assert result is True
        assert not account_manager.has_account("disabled_acc")
        # save_to_config 不再包含该账号
        saved = account_manager.save_to_config()
        ids = [a["id"] for a in saved["accounts"]]
        assert "disabled_acc" not in ids

    def test_delete_nonexistent_returns_false(self, account_manager):
        """删除不存在的账号返回 False"""
        result = account_manager.remove_account("nonexistent")
        assert result is False


# ═══════════════════════════════════════════════════════
#  API HTTP 端点测试
# ═══════════════════════════════════════════════════════

class TestAccountsAPI:
    """账号管理 API HTTP 测试"""

    def test_get_list_includes_disabled(self, api_client):
        """ACC-501：GET /api/accounts 返回全部账号（含 disabled）"""
        resp = api_client.get("/api/accounts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        ids = [a["account_id"] for a in data["data"]]
        assert set(ids) == {"main_acc", "disabled_acc", "broken_acc"}

    def test_patch_disabled_account_reenable(self, api_client, account_manager):
        """ACC-501：PATCH 禁用账号 enabled=true 可重新启用"""
        resp = api_client.patch("/api/accounts/disabled_acc", json={"enabled": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["enabled"] is True
        # 配置注册表已更新
        cfg = account_manager.get_config("disabled_acc")
        assert cfg["enabled"] is True

    def test_patch_redacted_preserves_cookie(self, api_client, account_manager):
        """ACC-501：PATCH with __REDACTED__ 保留原 Cookie"""
        resp = api_client.patch("/api/accounts/main_acc", json={
            "sessdata": REDACTED_PLACEHOLDER,
            "bili_jct": REDACTED_PLACEHOLDER,
            "name": "PATCH后的名字",
        })
        assert resp.status_code == 200
        # 配置注册表中原 Cookie 保留
        cfg = account_manager.get_config("main_acc")
        assert cfg["sessdata"] == "main-sessdata-secret"
        assert cfg["bili_jct"] == "main-jct-secret"
        assert cfg["name"] == "PATCH后的名字"

    def test_patch_nonexistent_returns_not_found(self, api_client):
        """PATCH 不存在的账号返回 NOT_FOUND"""
        resp = api_client.patch("/api/accounts/nonexistent", json={"name": "x"})
        assert resp.status_code == 400  # fail() 默认 400
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "NOT_FOUND"

    def test_delete_account_via_api(self, api_client, account_manager):
        """ACC-501：DELETE /api/accounts/{id} 删除账号"""
        resp = api_client.delete("/api/accounts/broken_acc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        # 已从注册表删除
        assert not account_manager.has_account("broken_acc")
        # 审计已写入
        delete_audits = account_manager.audit_store.query(scene="account_delete")
        assert any("broken_acc" in a["input_summary"] for a in delete_audits)

    def test_delete_last_default_account_blocked(self, api_client, account_manager):
        """ACC-501：不能删除最后一个默认账号"""
        # 先删除 broken_acc 和 disabled_acc，只剩 main_acc（默认）
        api_client.delete("/api/accounts/broken_acc")
        api_client.delete("/api/accounts/disabled_acc")
        # 尝试删除最后一个默认账号
        resp = api_client.delete("/api/accounts/main_acc")
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "LAST_ACCOUNT"

    def test_add_disabled_account(self, api_client, account_manager):
        """ACC-501：添加 enabled=false 账号，不在运行时但保留在配置"""
        resp = api_client.post("/api/accounts", json={
            "id": "new_disabled",
            "name": "新禁用账号",
            "sessdata": "new-secret",
            "bili_jct": "new-jct",
            "enabled": False,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["enabled"] is False
        assert data["data"]["state"] == "disabled"
        # 在配置注册表中
        assert account_manager.has_account("new_disabled")
        # 无运行时实例
        assert account_manager.get_account("new_disabled") is None
        # save_to_config 保留
        saved = account_manager.save_to_config()
        ids = [a["id"] for a in saved["accounts"]]
        assert "new_disabled" in ids

    def test_save_after_patch_preserves_all(self, api_client, account_manager):
        """ACC-501：PATCH 后 save_to_config 保留全部账号"""
        # PATCH main_acc
        api_client.patch("/api/accounts/main_acc", json={"name": "改名了"})
        # 验证全部账号仍在
        saved = account_manager.save_to_config()
        ids = [a["id"] for a in saved["accounts"]]
        assert set(ids) == {"main_acc", "disabled_acc", "broken_acc"}
        # disabled 账号 Cookie 不变
        accounts_map = {a["id"]: a for a in saved["accounts"]}
        assert accounts_map["disabled_acc"]["sessdata"] == "disabled-sessdata-secret"
        assert accounts_map["disabled_acc"]["enabled"] is False
