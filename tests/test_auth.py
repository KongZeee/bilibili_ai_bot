"""
tests/test_auth.py - 鉴权 HTTP 测试

PRD V3 §5.4 必需测试文件。
覆盖：
- 未登录访问 /api/config/full 返回 401
- 未登录访问 /api/personas 返回 401
- 未登录访问 /api/memory 返回 401
- 登录成功返回 200
- 登录后访问上述接口返回 200
- logout 后接口重新返回 401
- 登录失败返回 401
- 会话过期返回 401
"""
import time

import pytest
from starlette.testclient import TestClient


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def web_client(tmp_data_dir):
    """构造完整 Web 应用并返回 TestClient

    每个测试独立一份 _sessions，避免互相干扰。
    """
    # 清空 panel 模块级会话表
    from bilibot.web import panel as panel_mod
    panel_mod._sessions.clear()

    from bilibot.app.config_loader import ConfigLoader
    from bilibot.services.persona_store import PersonaStore
    from bilibot.prompts import PromptOrchestrator
    from bilibot.services.audit_store import AuditStore
    from bilibot.web.panel import create_web_app

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

    app = create_web_app(
        config_loader=config_loader,
        persona_store=persona_store,
        orchestrator=orchestrator,
        scheduler=None,
        config_path=str(tmp_data_dir + "/config.yaml"),
        audit_store=audit_store,
        context_builder=None,
    )
    client = TestClient(app)
    yield client
    # teardown
    panel_mod._sessions.clear()


def _login(client, username="admin", password="test123"):
    """登录助手，返回响应"""
    return client.post("/api/login", json={"username": username, "password": password})


# ═══════════════════════════════════════════════════════
#  未登录访问受保护接口
# ═══════════════════════════════════════════════════════

class TestUnauthenticatedAccess:
    """未登录访问受保护接口必须返回 401"""

    def test_config_full_returns_401(self, web_client):
        resp = web_client.get("/api/config/full")
        assert resp.status_code == 401
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "UNAUTHORIZED"

    def test_personas_returns_401(self, web_client):
        resp = web_client.get("/api/personas")
        assert resp.status_code == 401

    def test_memory_returns_401(self, web_client):
        resp = web_client.get("/api/memory")
        assert resp.status_code == 401

    def test_status_protected_returns_401(self, web_client):
        """非公开 status 也需要登录"""
        resp = web_client.get("/api/status")
        assert resp.status_code == 401

    def test_status_public_anonymous_ok(self, web_client):
        """公开 status 不需要登录"""
        resp = web_client.get("/api/status/public")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════
#  登录
# ═══════════════════════════════════════════════════════

class TestLogin:
    """登录流程"""

    def test_login_success_returns_200(self, web_client):
        resp = _login(web_client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "token" in body

    def test_login_wrong_password_returns_401(self, web_client):
        resp = _login(web_client, password="wrong")
        assert resp.status_code == 401
        body = resp.json()
        assert body["success"] is False

    def test_login_wrong_username_returns_401(self, web_client):
        resp = _login(web_client, username="notexist")
        assert resp.status_code == 401

    def test_login_sets_cookie(self, web_client):
        """登录成功必须设置 token cookie"""
        resp = _login(web_client)
        # TestClient 会自动把 cookie 存进 client.cookies
        assert "token" in web_client.cookies


# ═══════════════════════════════════════════════════════
#  登录后访问
# ═══════════════════════════════════════════════════════

class TestAuthenticatedAccess:
    """登录后访问受保护接口"""

    @pytest.fixture(autouse=True)
    def _do_login(self, web_client):
        _login(web_client)

    def test_config_full_after_login(self, web_client):
        resp = web_client.get("/api/config/full")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

    def test_personas_after_login(self, web_client):
        resp = web_client.get("/api/personas")
        assert resp.status_code == 200

    def test_memory_after_login(self, web_client):
        resp = web_client.get("/api/memory")
        assert resp.status_code == 200

    def test_memory_stats_after_login(self, web_client):
        resp = web_client.get("/api/memory/stats")
        assert resp.status_code == 200

    def test_audit_generations_after_login(self, web_client):
        resp = web_client.get("/api/audit/generations")
        assert resp.status_code == 200

    def test_status_after_login(self, web_client):
        resp = web_client.get("/api/status")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════
#  Logout
# ═══════════════════════════════════════════════════════

class TestLogout:
    """登出后接口重新返回 401"""

    def test_logout_then_protected_returns_401(self, web_client):
        # 登录
        _login(web_client)
        assert web_client.get("/api/config/full").status_code == 200

        # 登出
        resp = web_client.post("/api/logout")
        assert resp.status_code == 200

        # 清掉 client cookie 模拟浏览器登出后状态
        web_client.cookies.clear()

        # 受保护接口重新 401
        assert web_client.get("/api/config/full").status_code == 401
        assert web_client.get("/api/personas").status_code == 401
        assert web_client.get("/api/memory").status_code == 401


# ═══════════════════════════════════════════════════════
#  会话过期
# ═══════════════════════════════════════════════════════

class TestSessionExpiry:
    """会话过期返回 401"""

    def test_expired_session_returns_401(self, web_client):
        from bilibot.web import panel as panel_mod

        # 登录
        _login(web_client)

        # 直接把 login_time 调到 2 小时前
        for tok, sess in panel_mod._sessions.items():
            sess["login_time"] = time.time() - 7200

        # 任何受保护接口应返回 401（SESSION_EXPIRED）
        resp = web_client.get("/api/config/full")
        assert resp.status_code == 401
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "SESSION_EXPIRED"
