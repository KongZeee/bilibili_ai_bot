"""
tests/test_acc_503_qr_session.py - ACC-503 二维码登录会话闭环测试

PRD-V5 §5.2 ACC-503：
- Create/poll/cancel 必须由同一管理员会话操作
- 不返回 raw key 或 Cookie
- 会话一次性，180s 过期，确认后立即失效
- 旧 /api/bilibili/qrcode/* 写端点 → 410 Gone
- 账号删除 → 不回退 V1
- Cookie 写入仅重载目标账号
- 日志脱敏（不含 raw key / Cookie）
"""
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette

from bilibot.account.manager import AccountManager
from bilibot.app.config_loader import ConfigLoader
from bilibot.services.audit_store import AuditStore
from bilibot.services.persona_store import PersonaStore
from bilibot.api.accounts import create_accounts_routes
from bilibot.api import accounts as accounts_module


# ═══════════════════════════════════════════════════════
#  Mock BilibiliQRLogin
# ═══════════════════════════════════════════════════════

RAW_KEY = "test-raw-key-secret-12345"
RAW_COOKIE_SESSDATA = "test-sessdata-secret-value"


class MockQRLogin:
    """可配置的 BilibiliQRLogin mock"""

    _status_result = {"status": "waiting", "message": "等待扫码..."}
    _qrcode_result = {
        "key": RAW_KEY,
        "url": "https://example.com/qr",
        "data_url": "data:image/png;base64,abc123",
        "expire_ts": int(time.time()) + 180,
    }
    _update_config_result = {"success": True, "message": "ok", "info": {"uid": "99999"}}
    _update_config_calls = []

    def __init__(self, config):
        self.config = config

    async def get_qrcode(self):
        return dict(self._qrcode_result)

    async def check_status(self, qr_key):
        return dict(self._status_result)

    async def close(self):
        pass

    def update_config(self, cookies, account_id=""):
        self._update_config_calls.append({"cookies": dict(cookies), "account_id": account_id})
        return dict(self._update_config_result)

    @classmethod
    def reset(cls):
        cls._status_result = {"status": "waiting", "message": "等待扫码..."}
        cls._qrcode_result = {
            "key": RAW_KEY,
            "url": "https://example.com/qr",
            "data_url": "data:image/png;base64,abc123",
            "expire_ts": int(time.time()) + 180,
        }
        cls._update_config_result = {"success": True, "message": "ok", "info": {"uid": "99999"}}
        cls._update_config_calls = []


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
    mgr.resolve_provider.return_value = (None, "", "")
    return mgr


@pytest.fixture
def accounts_config(tmp_data_dir):
    return ConfigLoader(config_dict={
        "accounts": [
            {
                "id": "main_acc",
                "name": "主账号",
                "sessdata": "old-sessdata",
                "bili_jct": "old-jct",
                "dede_user_id": "10001",
                "buvid3": "main-buvid3",
                "refresh_token": "",
                "profile_id": "",
                "persona_id": "",
                "llm_id": "",
                "enabled": True,
            },
            {
                "id": "second_acc",
                "name": "第二账号",
                "sessdata": "second-old-sessdata",
                "bili_jct": "second-jct",
                "dede_user_id": "20002",
                "buvid3": "",
                "refresh_token": "",
                "profile_id": "",
                "persona_id": "",
                "llm_id": "",
                "enabled": True,
            },
            {
                "id": "disabled_acc",
                "name": "已禁用账号",
                "sessdata": "disabled-sessdata",
                "bili_jct": "disabled-jct",
                "dede_user_id": "30003",
                "buvid3": "",
                "refresh_token": "",
                "profile_id": "",
                "persona_id": "",
                "llm_id": "",
                "enabled": False,
            },
        ],
        "default_account": "main_acc",
        "data_dir": tmp_data_dir,
    })


@pytest.fixture
def account_manager(accounts_config, tmp_data_dir, mock_orchestrator, mock_context_builder, mock_llm_manager):
    persona_store = PersonaStore(data_dir=tmp_data_dir)
    audit_store = AuditStore(data_dir=tmp_data_dir)
    mgr = AccountManager(
        persona_store=persona_store,
        llm_manager=mock_llm_manager,
        audit_store=audit_store,
        orchestrator=mock_orchestrator,
        context_builder=mock_context_builder,
        app_config_loader=accounts_config,
        data_root=tmp_data_dir,
        safety_checker=None,
    )
    mgr.initialize()
    return mgr


@pytest.fixture(autouse=True)
def reset_state():
    """每个测试前重置 mock 和会话存储"""
    MockQRLogin.reset()
    accounts_module._qr_sessions.clear()
    accounts_module._qr_session_keys.clear()
    yield
    accounts_module._qr_sessions.clear()
    accounts_module._qr_session_keys.clear()


@pytest.fixture
def api_client(account_manager, accounts_config, tmp_data_dir):
    """构造 accounts API 测试客户端"""
    routes = create_accounts_routes(
        account_manager,
        accounts_config,
        config_path=str(tmp_data_dir + "/test_config.yaml"),
    )
    app = Starlette(routes=routes)
    return TestClient(app)


@pytest.fixture
def patched_qr_login(monkeypatch):
    """patch BilibiliQRLogin with MockQRLogin"""
    monkeypatch.setattr("bilibot.bilibili_qrlogin.BilibiliQRLogin", MockQRLogin)
    return MockQRLogin


TOKEN_A = "admin-token-aaa"
TOKEN_B = "admin-token-bbb"


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_session(client, account_id="main_acc", token=TOKEN_A):
    """助手：创建 QR 会话，返回响应"""
    return client.post(
        f"/api/accounts/{account_id}/qr-login",
        headers=_auth(token),
    )


def _poll(client, account_id="main_acc", session_id="", token=TOKEN_A):
    """助手：轮询 QR 会话，返回响应"""
    return client.get(
        f"/api/accounts/{account_id}/qr-login/{session_id}",
        headers=_auth(token),
    )


# ═══════════════════════════════════════════════════════
#  会话创建测试
# ═══════════════════════════════════════════════════════

class TestQRSessionCreation:
    """ACC-503：QR 会话创建"""

    def test_create_returns_session_id_and_url(self, api_client, patched_qr_login):
        """创建会话返回 qr_session_id + qrcode_url"""
        resp = _create_session(api_client)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        d = data["data"]
        assert "qr_session_id" in d
        assert d["qrcode_url"] == "https://example.com/qr"
        assert d["qrcode_data_url"].startswith("data:image/png;base64,")
        assert d["account_id"] == "main_acc"
        assert d["status"] == "created"
        assert d["expire_at"] > int(time.time())

    def test_create_does_not_return_raw_key(self, api_client, patched_qr_login):
        """ACC-503：不返回 raw qrcode_key"""
        resp = _create_session(api_client)
        data = resp.json()["data"]
        # 不应包含 raw key
        assert "key" not in data
        assert "qrcode_key" not in data
        assert RAW_KEY not in resp.text

    def test_create_stores_only_key_hash_not_raw(self, api_client, patched_qr_login):
        """ACC-503：会话模型只存 hash，不存 raw key"""
        resp = _create_session(api_client)
        sid = resp.json()["data"]["qr_session_id"]
        sess = accounts_module._qr_sessions[sid]
        assert "qrcode_key_hash" in sess
        assert "qrcode_key" not in sess
        # raw key 仅在临时存储中
        assert accounts_module._qr_session_keys[sid] == RAW_KEY

    def test_create_binds_creator_session_hash(self, api_client, patched_qr_login):
        """ACC-503：绑定创建者管理员会话 hash"""
        resp = _create_session(api_client, token=TOKEN_A)
        sid = resp.json()["data"]["qr_session_id"]
        sess = accounts_module._qr_sessions[sid]
        import hashlib
        expected = hashlib.sha256(TOKEN_A.encode()).hexdigest()
        assert sess["creator_session_hash"] == expected

    def test_create_nonexistent_account_returns_404(self, api_client, patched_qr_login):
        """ACC-503：账号不存在 → 404，不回退 V1"""
        resp = _create_session(api_client, account_id="nonexistent")
        assert resp.status_code == 404
        data = resp.json()
        assert data["error"]["code"] == "NOT_FOUND"

    def test_create_without_token_returns_401(self, api_client, patched_qr_login):
        """ACC-503：无管理员凭证 → 401"""
        resp = api_client.post("/api/accounts/main_acc/qr-login")
        assert resp.status_code == 401

    def test_create_duplicate_session_blocked(self, api_client, patched_qr_login):
        """ACC-503：同一账号同一时刻最多一个活跃会话"""
        resp1 = _create_session(api_client)
        assert resp1.status_code == 200
        resp2 = _create_session(api_client)
        assert resp2.status_code == 400
        assert resp2.json()["error"]["code"] == "QR_SESSION_EXISTS"


# ═══════════════════════════════════════════════════════
#  轮询鉴权测试
# ═══════════════════════════════════════════════════════

class TestQRSessionPollingAuth:
    """ACC-503：轮询鉴权"""

    def test_poll_correct_admin_success(self, api_client, patched_qr_login):
        """正确管理员轮询 → 成功"""
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        poll_resp = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 200
        assert poll_resp.json()["data"]["status"] == "waiting"

    def test_poll_wrong_admin_403_owner_mismatch(self, api_client, patched_qr_login):
        """ACC-503：错误管理员轮询 → 403 QR_SESSION_OWNER_MISMATCH"""
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        poll_resp = _poll(api_client, session_id=sid, token=TOKEN_B)
        assert poll_resp.status_code == 403
        data = poll_resp.json()
        assert data["error"]["code"] == "QR_SESSION_OWNER_MISMATCH"

    def test_poll_wrong_account_403_mismatch(self, api_client, patched_qr_login):
        """ACC-503：错误账号轮询 → 403 QR_SESSION_MISMATCH"""
        create_resp = _create_session(api_client, account_id="main_acc", token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        # 用 second_acc 的 URL 轮询 main_acc 的会话
        poll_resp = _poll(api_client, account_id="second_acc", session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 403
        assert poll_resp.json()["error"]["code"] == "QR_SESSION_MISMATCH"

    def test_poll_nonexistent_session_404(self, api_client, patched_qr_login):
        """不存在的会话 → 404"""
        poll_resp = _poll(api_client, session_id="fake-sid")
        assert poll_resp.status_code == 404
        assert poll_resp.json()["error"]["code"] == "QR_SESSION_NOT_FOUND"

    def test_poll_without_token_401(self, api_client, patched_qr_login):
        """无管理员凭证 → 401"""
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        poll_resp = api_client.get(f"/api/accounts/main_acc/qr-login/{sid}")
        assert poll_resp.status_code == 401


# ═══════════════════════════════════════════════════════
#  会话生命周期测试
# ═══════════════════════════════════════════════════════

class TestQRSessionLifecycle:
    """ACC-503：会话生命周期"""

    def test_expired_session_returns_410(self, api_client, patched_qr_login):
        """ACC-503：过期会话 → 410"""
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        # 手动设置过期
        accounts_module._qr_sessions[sid]["expire_at"] = time.time() - 1
        poll_resp = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 410
        assert poll_resp.json()["error"]["code"] == "QR_SESSION_EXPIRED"

    def test_duplicate_confirm_returns_410(self, api_client, patched_qr_login):
        """ACC-503：重复确认 → 410（已确认）"""
        # 设置 B站 返回 confirmed
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": RAW_COOKIE_SESSDATA, "bili_jct": "jct", "DedeUserID": "99999"},
        }
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]

        # 第一次轮询 → confirmed
        poll1 = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll1.status_code == 200
        assert poll1.json()["data"]["status"] == "confirmed"

        # 第二次轮询 → 410（会话已终态）
        poll2 = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll2.status_code == 410
        assert poll2.json()["error"]["code"] == "QR_SESSION_GONE"

    def test_cancel_then_poll_returns_410(self, api_client, patched_qr_login):
        """ACC-503：取消后轮询 → 410"""
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]

        # 取消
        cancel_resp = api_client.post(
            f"/api/accounts/main_acc/qr-login/{sid}/cancel",
            headers=_auth(TOKEN_A),
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["data"]["status"] == "cancelled"

        # 轮询 → 410
        poll_resp = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 410
        assert poll_resp.json()["error"]["code"] == "QR_SESSION_GONE"

    def test_cancel_wrong_admin_403(self, api_client, patched_qr_login):
        """ACC-503：错误管理员取消 → 403"""
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        cancel_resp = api_client.post(
            f"/api/accounts/main_acc/qr-login/{sid}/cancel",
            headers=_auth(TOKEN_B),
        )
        assert cancel_resp.status_code == 403
        assert cancel_resp.json()["error"]["code"] == "QR_SESSION_OWNER_MISMATCH"

    def test_scanned_status_updates(self, api_client, patched_qr_login):
        """ACC-503：scanned 状态更新"""
        MockQRLogin._status_result = {"status": "scanned", "message": "已扫码，请确认"}
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        poll_resp = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 200
        assert poll_resp.json()["data"]["status"] == "scanned"
        # 会话状态已更新
        assert accounts_module._qr_sessions[sid]["status"] == "scanned"

    def test_confirmed_does_not_return_cookie(self, api_client, patched_qr_login):
        """ACC-503：确认后不返回 raw Cookie"""
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": RAW_COOKIE_SESSDATA, "bili_jct": "raw-jct-val", "DedeUserID": "99999"},
        }
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        poll_resp = _poll(api_client, session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 200
        body_text = poll_resp.text
        assert RAW_COOKIE_SESSDATA not in body_text
        assert "raw-jct-val" not in body_text

    def test_confirmed_invalidates_session(self, api_client, patched_qr_login):
        """ACC-503：确认后立即失效会话（raw key 移除）"""
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": "sess", "bili_jct": "jct", "DedeUserID": "1"},
        }
        create_resp = _create_session(api_client, token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        _poll(api_client, session_id=sid, token=TOKEN_A)
        # raw key 应已移除
        assert sid not in accounts_module._qr_session_keys
        # 会话状态为 confirmed
        assert accounts_module._qr_sessions[sid]["status"] == "confirmed"


# ═══════════════════════════════════════════════════════
#  账号删除 → 不回退 V1
# ═══════════════════════════════════════════════════════

class TestAccountDeletionNoV1Fallback:
    """ACC-503：账号删除后不回退 V1 配置"""

    def test_poll_deleted_account_returns_404(self, api_client, account_manager, patched_qr_login):
        """ACC-503：账号被删除 → 不回退 V1，返回 404"""
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": "new-sess", "bili_jct": "new-jct", "DedeUserID": "1"},
        }
        create_resp = _create_session(api_client, account_id="second_acc", token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]

        # 删除 second_acc
        del_resp = api_client.delete("/api/accounts/second_acc")
        assert del_resp.status_code == 200

        # 轮询 → ACCOUNT_NOT_FOUND（不回退 V1）
        poll_resp = _poll(api_client, account_id="second_acc", session_id=sid, token=TOKEN_A)
        assert poll_resp.status_code == 404
        assert poll_resp.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

        # update_config 未被调用（不回退 V1）
        assert len(MockQRLogin._update_config_calls) == 0


# ═══════════════════════════════════════════════════════
#  Cookie 写入仅重载目标账号
# ═══════════════════════════════════════════════════════

class TestCookieWriteTargetedReload:
    """ACC-503：Cookie 写入仅重载目标账号"""

    def test_cookie_write_calls_update_config_with_account_id(self, api_client, patched_qr_login):
        """ACC-503：update_config 使用正确的 account_id"""
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": "new-sess", "bili_jct": "new-jct", "DedeUserID": "99999"},
        }
        create_resp = _create_session(api_client, account_id="main_acc", token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        _poll(api_client, account_id="main_acc", session_id=sid, token=TOKEN_A)

        assert len(MockQRLogin._update_config_calls) == 1
        call = MockQRLogin._update_config_calls[0]
        assert call["account_id"] == "main_acc"

    def test_only_target_account_reloaded(self, api_client, account_manager, patched_qr_login):
        """ACC-503：仅重载目标账号，不 reload_all"""
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": "new-sess", "bili_jct": "new-jct", "DedeUserID": "99999"},
        }

        acc_main = account_manager.get_account("main_acc")
        acc_second = account_manager.get_account("second_acc")

        # mock reload 以跟踪调用
        reload_main = AsyncMock(return_value={"reloaded": True})
        reload_second = AsyncMock(return_value={"reloaded": True})
        acc_main.reload = reload_main
        acc_second.reload = reload_second

        # mock reload_all 以确保不被调用
        reload_all_spy = AsyncMock()
        account_manager.reload_all = reload_all_spy

        create_resp = _create_session(api_client, account_id="main_acc", token=TOKEN_A)
        sid = create_resp.json()["data"]["qr_session_id"]
        _poll(api_client, account_id="main_acc", session_id=sid, token=TOKEN_A)

        # 仅 main_acc 被 reload
        reload_main.assert_called_once()
        reload_second.assert_not_called()
        # reload_all 未被调用
        reload_all_spy.assert_not_called()


# ═══════════════════════════════════════════════════════
#  旧端点 410 Gone
# ═══════════════════════════════════════════════════════

class TestOldEndpointsGone:
    """ACC-503：旧 /api/bilibili/qrcode/* 端点返回 410 Gone"""

    @pytest.fixture
    def web_client(self, tmp_data_dir):
        """构造完整 Web 应用（含旧 qrcode 端点）"""
        from bilibot.web import panel as panel_mod
        panel_mod._sessions.clear()

        from bilibot.prompts import PromptOrchestrator
        config_loader = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
            "llm": {"api_key": "test-key", "base_url": "http://localhost:8000/v1", "model": "test"},
            "web": {
                "enabled": True, "host": "127.0.0.1", "port": 8080,
                "secret_key": "test-secret",
                "admin_username": "admin",
                "admin_password": "test123",
                "session_ttl_seconds": 3600,
                "cors_origins": [],
                "secure_cookies": False,
            },
            "data_dir": tmp_data_dir,
        })
        persona_store = PersonaStore(data_dir=tmp_data_dir)
        orchestrator = PromptOrchestrator(persona_store)
        audit_store = AuditStore(data_dir=tmp_data_dir)

        app = panel_mod.create_web_app(
            config_loader=config_loader,
            persona_store=persona_store,
            orchestrator=orchestrator,
            scheduler=None,
            config_path=str(tmp_data_dir + "/config.yaml"),
            audit_store=audit_store,
            context_builder=None,
        )
        client = TestClient(app)
        # 登录
        client.post("/api/login", json={"username": "admin", "password": "test123"})
        yield client
        panel_mod._sessions.clear()

    def test_old_get_qrcode_returns_410(self, web_client):
        """GET /api/bilibili/qrcode → 410"""
        resp = web_client.get("/api/bilibili/qrcode")
        assert resp.status_code == 410
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "GONE"
        assert "new_endpoint" in body["error"]["details"]

    def test_old_get_qrcode_status_returns_410(self, web_client):
        """GET /api/bilibili/qrcode/status → 410"""
        resp = web_client.get("/api/bilibili/qrcode/status?key=foo")
        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "GONE"

    def test_old_post_qrcode_status_returns_410(self, web_client):
        """POST /api/bilibili/qrcode/status → 410"""
        resp = web_client.post("/api/bilibili/qrcode/status", json={"key": "foo"})
        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "GONE"


# ═══════════════════════════════════════════════════════
#  日志脱敏
# ═══════════════════════════════════════════════════════

class TestLogRedaction:
    """ACC-503：日志不含 raw key / Cookie"""

    def test_logs_no_raw_key_or_cookie(self, api_client, patched_qr_login, caplog):
        """ACC-503：日志中不出现 raw key 或 Cookie 值"""
        MockQRLogin._status_result = {
            "status": "confirmed",
            "message": "登录成功！",
            "cookies": {"SESSDATA": RAW_COOKIE_SESSDATA, "bili_jct": "raw-jct-val", "DedeUserID": "99999"},
        }

        with caplog.at_level(logging.DEBUG, logger="bilibot.api.accounts"):
            create_resp = _create_session(api_client, token=TOKEN_A)
            sid = create_resp.json()["data"]["qr_session_id"]
            _poll(api_client, session_id=sid, token=TOKEN_A)

        # 检查所有日志记录
        for record in caplog.records:
            msg = record.getMessage()
            assert RAW_KEY not in msg, f"raw key leaked in log: {msg}"
            assert RAW_COOKIE_SESSDATA not in msg, f"cookie leaked in log: {msg}"
            assert "raw-jct-val" not in msg, f"cookie leaked in log: {msg}"

    def test_logs_contain_truncated_session_id(self, api_client, patched_qr_login, caplog):
        """ACC-503：日志中 session_id 被截断"""
        with caplog.at_level(logging.INFO, logger="bilibot.api.accounts"):
            create_resp = _create_session(api_client, token=TOKEN_A)
            sid = create_resp.json()["data"]["qr_session_id"]

        # 找到 created 日志
        created_msgs = [r.getMessage() for r in caplog.records if "created" in r.getMessage()]
        assert len(created_msgs) > 0
        # 完整 session_id 不应出现在日志中
        for msg in created_msgs:
            assert sid not in msg, f"full session id leaked in log: {msg}"
