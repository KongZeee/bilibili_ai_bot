"""
tests/test_acc_502_context_builder_isolation.py - ACC-502 账号级 ContextBuilder 隔离测试

PRD-V5 §5.1 ACC-502：每个账号拥有独立 ContextBuilder，
build() 校验 account_id 隔离，防止跨账号读取 recent behavior。

覆盖：
- 两个账号各自写入可识别的 recent action，交叉 build 100 次无交叉污染
- build() 传入不匹配的 account_id 抛出 IsolationError
- App 不再创建共享 ContextBuilder（__init__ 后为 None；initialize 后指向默认账号实例）
"""
import pytest

from bilibot.context_builder import ContextBuilder, IsolationError
from bilibot.models import ReplyContext
from bilibot.services.persona_store import PersonaStore


# ═══════════════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════════════

class FakeDataStore:
    """假 DataStore，返回预设的 recent actions 用于隔离验证"""

    def __init__(self, actions=None):
        self._actions = actions or []
        # data_dir 字段被 _get_data_dir 等辅助方法读取
        self.data_dir = "/tmp/fake"

    def get_recent_actions(self, limit: int = 5):
        return list(self._actions[:limit])


# ═══════════════════════════════════════════════════════
#  跨账号隔离：recent behavior 不串号
# ═══════════════════════════════════════════════════════

class TestRecentActionsIsolation:
    """ACC-502：每个账号的 ContextBuilder 只读本账号 recent behavior"""

    def test_cross_account_no_contamination(self, tmp_data_dir):
        """两个账号各写一条可识别 recent action，交叉 build 100 次无串号"""
        ps = PersonaStore(data_dir=tmp_data_dir)

        ds_a = FakeDataStore(actions=[{"summary": "ACCOUNT_A_ACTION_TAG"}])
        ds_b = FakeDataStore(actions=[{"summary": "ACCOUNT_B_ACTION_TAG"}])

        cb_a = ContextBuilder(
            data_store=ds_a, persona_store=ps, account_id="acc_a",
        )
        cb_b = ContextBuilder(
            data_store=ds_b, persona_store=ps, account_id="acc_b",
        )

        ctx = ReplyContext()

        for _ in range(100):
            built_a = cb_a.build(ctx, account_id="acc_a")
            built_b = cb_b.build(ctx, account_id="acc_b")
            assert "ACCOUNT_A_ACTION_TAG" in built_a["text"]
            assert "ACCOUNT_B_ACTION_TAG" not in built_a["text"]
            assert "ACCOUNT_B_ACTION_TAG" in built_b["text"]
            assert "ACCOUNT_A_ACTION_TAG" not in built_b["text"]

    def test_account_with_no_actions_returns_empty_recent(self, tmp_data_dir):
        """账号 A 有 action，账号 B 无 action —— B 的 build 不含 A 的 action"""
        ps = PersonaStore(data_dir=tmp_data_dir)

        ds_a = FakeDataStore(actions=[{"summary": "ONLY_IN_A"}])
        ds_b = FakeDataStore(actions=[])

        cb_a = ContextBuilder(data_store=ds_a, persona_store=ps, account_id="acc_a")
        cb_b = ContextBuilder(data_store=ds_b, persona_store=ps, account_id="acc_b")

        ctx = ReplyContext()
        built_a = cb_a.build(ctx, account_id="acc_a")
        built_b = cb_b.build(ctx, account_id="acc_b")

        assert "ONLY_IN_A" in built_a["text"]
        assert "ONLY_IN_A" not in built_b["text"]
        assert "recent_actions" not in built_b["meta"].get("sources", [])

    def test_recent_actions_respect_limit(self, tmp_data_dir):
        """_get_recent_actions 尊重 limit 参数"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        actions = [{"summary": f"action_{i}"} for i in range(20)]
        ds = FakeDataStore(actions=actions)
        cb = ContextBuilder(data_store=ds, persona_store=ps, account_id="acc_x")

        ctx = ReplyContext()
        built = cb.build(ctx, account_id="acc_x")
        # ContextBuilder 内部 limit=5
        assert "action_0" in built["text"]
        assert "action_4" in built["text"]
        assert "action_5" not in built["text"]


# ═══════════════════════════════════════════════════════
#  account_id 隔离校验
# ═══════════════════════════════════════════════════════

class TestAccountIdValidation:
    """ACC-502：build() 校验 account_id 与实例绑定一致"""

    def test_mismatched_account_id_raises_isolation_error(self, tmp_data_dir):
        """实例 acc_a 调用 build(account_id='acc_b') 抛 IsolationError"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        cb = ContextBuilder(
            data_store=FakeDataStore(), persona_store=ps, account_id="acc_a",
        )
        ctx = ReplyContext()
        with pytest.raises(IsolationError, match="mismatch"):
            cb.build(ctx, account_id="acc_b")

    def test_matched_account_id_does_not_raise(self, tmp_data_dir):
        """实例 acc_a 调用 build(account_id='acc_a') 正常"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        cb = ContextBuilder(
            data_store=FakeDataStore(), persona_store=ps, account_id="acc_a",
        )
        ctx = ReplyContext()
        result = cb.build(ctx, account_id="acc_a")
        assert "text" in result

    def test_instance_without_account_id_accepts_any(self, tmp_data_dir):
        """未绑定 account_id 的实例（向后兼容）不校验"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        cb = ContextBuilder(data_store=FakeDataStore(), persona_store=ps)
        ctx = ReplyContext()
        # 任意 account_id 都不应抛异常
        result = cb.build(ctx, account_id="any_account")
        assert "text" in result

    def test_build_without_account_id_on_bound_instance_does_not_raise(self, tmp_data_dir):
        """已绑定 account_id 的实例，build() 不传 account_id 时不校验（兼容旧调用）"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        cb = ContextBuilder(
            data_store=FakeDataStore(), persona_store=ps, account_id="acc_a",
        )
        ctx = ReplyContext()
        # 不传 account_id（默认空字符串）不应抛异常
        result = cb.build(ctx)
        assert "text" in result

    def test_isolation_error_message_contains_both_ids(self, tmp_data_dir):
        """IsolationError 消息包含实例账号和调用方账号，便于排查"""
        ps = PersonaStore(data_dir=tmp_data_dir)
        cb = ContextBuilder(
            data_store=FakeDataStore(), persona_store=ps, account_id="acc_alpha",
        )
        ctx = ReplyContext()
        with pytest.raises(IsolationError) as exc_info:
            cb.build(ctx, account_id="acc_beta")
        msg = str(exc_info.value)
        assert "acc_alpha" in msg
        assert "acc_beta" in msg


# ═══════════════════════════════════════════════════════
#  App 不再创建共享 ContextBuilder
# ═══════════════════════════════════════════════════════

class TestAppNoSharedContextBuilder:
    """ACC-502：App 不再创建共享 ContextBuilder"""

    def test_app_init_does_not_create_shared_context_builder(self, tmp_data_dir):
        """BiliBotApp.__init__ 后 self.context_builder 为 None（未创建共享实例）"""
        from bilibot.app.config_loader import ConfigLoader
        from bilibot.app.app import BiliBotApp

        config_loader = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
            "llm": {"api_key": "test-key", "base_url": "http://localhost:8000/v1", "model": "test"},
            "web": {"enabled": False},
            "data_dir": tmp_data_dir,
            "accounts": [],
        })
        app = BiliBotApp(config_loader.get_raw_config(), config_path="config.yaml")
        # ACC-502：不再创建应用级共享 ContextBuilder
        assert app.context_builder is None

    def test_app_sync_legacy_attrs_uses_default_account_context_builder(self, tmp_data_dir):
        """initialize 后 self.context_builder 指向默认账号的账号级 ContextBuilder"""
        from bilibot.app.config_loader import ConfigLoader
        from bilibot.app.app import BiliBotApp
        from bilibot.context_builder import ContextBuilder

        config_loader = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
            "llm": {"api_key": "test-key", "base_url": "http://localhost:8000/v1", "model": "test"},
            "web": {"enabled": False},
            "data_dir": tmp_data_dir,
            "accounts": [
                {
                    "id": "default",
                    "name": "默认账号",
                    "sessdata": "s",
                    "bili_jct": "j",
                    "dede_user_id": "1",
                    "buvid3": "",
                    "refresh_token": "",
                    "persona_id": "",
                    "llm_id": "",
                    "enabled": True,
                },
            ],
            "default_account": "default",
        })
        app = BiliBotApp(config_loader.get_raw_config(), config_path="config.yaml")

        import asyncio
        asyncio.run(app.initialize())

        # ACC-502：指向默认账号的 ContextBuilder（账号级实例）
        assert app.context_builder is not None
        assert isinstance(app.context_builder, ContextBuilder)
        # 实例绑定的 account_id 等于默认账号 id
        assert app.context_builder.account_id == "default"
        # 实例的 DataStore 是默认账号的 DataStore（非 None）
        assert app.context_builder.ds is app.account_manager.get_default().data_store


# ═══════════════════════════════════════════════════════
#  AccountInstance 创建账号级 ContextBuilder
# ═══════════════════════════════════════════════════════

class TestAccountInstanceContextBuilder:
    """ACC-502：AccountInstance.initialize() 创建账号级 ContextBuilder"""

    def test_each_account_has_own_context_builder(self, tmp_data_dir):
        """两个账号 initialize 后各有独立的 ContextBuilder，绑定各自 account_id"""
        import asyncio
        from bilibot.account.instance import AccountInstance
        from unittest.mock import MagicMock

        mock_llm_mgr = MagicMock()
        mock_llm_mgr.get_default.return_value = None
        mock_llm_mgr.get_provider.return_value = None
        # LLM-501: resolve_provider 返回 (provider, effective_id, fallback_reason)
        mock_llm_mgr.resolve_provider.return_value = (None, "", "")
        ps = PersonaStore(data_dir=tmp_data_dir)

        acc_a = AccountInstance(
            account_id="acc_a",
            account_config={"name": "A", "sessdata": "s", "bili_jct": "j", "dede_user_id": "1"},
            persona_store=ps,
            llm_manager=mock_llm_mgr,
            audit_store=None,
            orchestrator=MagicMock(),
            context_builder=None,  # ACC-502: 不再依赖外部传入
            app_config_loader=ConfigLoader_with_data_dir(tmp_data_dir),
            data_root=tmp_data_dir,
        )
        acc_b = AccountInstance(
            account_id="acc_b",
            account_config={"name": "B", "sessdata": "s", "bili_jct": "j", "dede_user_id": "2"},
            persona_store=ps,
            llm_manager=mock_llm_mgr,
            audit_store=None,
            orchestrator=MagicMock(),
            context_builder=None,
            app_config_loader=ConfigLoader_with_data_dir(tmp_data_dir),
            data_root=tmp_data_dir,
        )

        asyncio.run(acc_a.initialize())
        asyncio.run(acc_b.initialize())

        # 各自的 context_builder 绑定各自 account_id
        assert acc_a.context_builder is not None
        assert acc_a.context_builder.account_id == "acc_a"
        assert acc_b.context_builder is not None
        assert acc_b.context_builder.account_id == "acc_b"
        # 不是同一个实例
        assert acc_a.context_builder is not acc_b.context_builder
        # 各自的 DataStore 不同（账号隔离）
        assert acc_a.context_builder.ds is acc_a.data_store
        assert acc_b.context_builder.ds is acc_b.data_store
        assert acc_a.data_store is not acc_b.data_store


def ConfigLoader_with_data_dir(tmp_data_dir):
    """构造带最小配置的 ConfigLoader"""
    from bilibot.app.config_loader import ConfigLoader
    return ConfigLoader(config_dict={
        "bilibili": {"sessdata": "s", "bili_jct": "j", "dede_user_id": "1"},
        "llm": {"api_key": "k", "base_url": "http://localhost:8000/v1", "model": "m"},
        "web": {"enabled": False},
        "data_dir": tmp_data_dir,
    })
