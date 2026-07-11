"""
tests/test_config_api.py - 配置 API 测试

PRD V3 §5.5 必需测试文件。
覆盖：
- /api/config/full 脱敏
- ***已配置*** 不覆盖真实密钥
- boolean false 能保存
- array string 自动规范化为 list
- array list 保持 list
- number string 自动转数字
- 非法 number 返回 CONFIG_VALIDATION_ERROR
- 默认密码风险可从 /api/status.security.default_password 获取
"""
import pytest
from starlette.testclient import TestClient


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def web_client(tmp_data_dir):
    """构造完整 Web 应用，使用默认密码 admin123 以便测试默认密码检测"""
    from bilibot.web import panel as panel_mod
    panel_mod._sessions.clear()

    from bilibot.app.config_loader import ConfigLoader
    from bilibot.services.persona_store import PersonaStore
    from bilibot.prompts import PromptOrchestrator
    from bilibot.services.audit_store import AuditStore
    from bilibot.web.panel import create_web_app

    # 这里故意使用默认 admin123 以便测试 status.security.default_password
    config_loader = ConfigLoader(config_dict={
        "bilibili": {
            "sessdata": "real-sessdata",
            "bili_jct": "real-jct",
            "dede_user_id": "12345",
        },
        "llm": {
            "api_key": "sk-real-key",
            "base_url": "http://localhost:8000/v1",
            "model": "test",
            "max_tokens": 1024,
            "temperature": 0.8,
        },
        "web": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "secret_key": "real-secret",
            "admin_username": "admin",
            "admin_password": "admin123",  # 默认密码
            "session_ttl_seconds": 3600,
            "cors_origins": [],
            "secure_cookies": False,
        },
        "reply": {
            "auto_reply": True,
            "block_keywords": ["a", "b"],
            "min_comment_length": 2,
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
    panel_mod._sessions.clear()


@pytest.fixture
def authed_client(web_client):
    """已登录客户端"""
    web_client.post("/api/login", json={"username": "admin", "password": "admin123"})
    return web_client


# ═══════════════════════════════════════════════════════
#  脱敏
# ═══════════════════════════════════════════════════════

class TestMaskSensitive:
    """/api/config/full 必须脱敏"""

    def test_sessdata_masked(self, authed_client):
        resp = authed_client.get("/api/config/full")
        assert resp.status_code == 200
        cfg = resp.json()["data"]
        assert cfg["bilibili"]["sessdata"] == "***已配置***"
        assert cfg["bilibili"]["bili_jct"] == "***已配置***"

    def test_api_key_masked(self, authed_client):
        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        assert cfg["llm"]["api_key"] == "***已配置***"

    def test_admin_password_masked(self, authed_client):
        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        assert cfg["web"]["admin_password"] == "***已配置***"

    def test_secret_key_masked(self, authed_client):
        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        assert cfg["web"]["secret_key"] == "***已配置***"

    def test_non_sensitive_visible(self, authed_client):
        """非敏感字段正常显示"""
        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        assert cfg["bilibili"]["dede_user_id"] == "12345"
        assert cfg["llm"]["model"] == "test"
        assert cfg["web"]["admin_username"] == "admin"


# ═══════════════════════════════════════════════════════
#  敏感字段保护
# ═══════════════════════════════════════════════════════

class TestSensitivePreserved:
    """PATCH ***已配置*** 不覆盖真实密钥"""

    def test_placeholder_keeps_real_api_key(self, authed_client, tmp_data_dir):
        # 用占位符 PATCH
        resp = authed_client.patch("/api/config", json={"llm": {"api_key": "***已配置***"}})
        assert resp.status_code == 200

        # 重新登录（配置热重载后 session 仍在）
        # 直接读取磁盘上的配置文件验证
        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["llm"]["api_key"] == "sk-real-key"

    def test_placeholder_keeps_real_password(self, authed_client, tmp_data_dir):
        resp = authed_client.patch("/api/config", json={
            "web": {"admin_password": "***已配置***"}
        })
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["web"]["admin_password"] == "admin123"

    def test_empty_string_keeps_real_secret(self, authed_client, tmp_data_dir):
        """空字符串也保留原值"""
        resp = authed_client.patch("/api/config", json={"llm": {"api_key": ""}})
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["llm"]["api_key"] == "sk-real-key"


# ═══════════════════════════════════════════════════════
#  类型规范化
# ═══════════════════════════════════════════════════════

class TestTypeNormalization:
    """配置类型规范化"""

    def test_boolean_false_can_be_saved(self, authed_client, tmp_data_dir):
        """checkbox false 必须能保存为 False（PRD V3 §6.5）"""
        resp = authed_client.patch("/api/config", json={"reply": {"auto_reply": False}})
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["auto_reply"] is False
        assert isinstance(saved["reply"]["auto_reply"], bool)

    def test_boolean_string_normalized(self, authed_client, tmp_data_dir):
        """字符串 'false' 也规范化为 False"""
        resp = authed_client.patch("/api/config", json={"reply": {"auto_reply": "false"}})
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["auto_reply"] is False

    def test_array_string_normalized_to_list(self, authed_client, tmp_data_dir):
        """字符串 'a,b,c' 必须保存为 list（PRD V3 §6.5）"""
        resp = authed_client.patch("/api/config", json={
            "reply": {"block_keywords": "a,b,c"}
        })
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["block_keywords"] == ["a", "b", "c"]
        assert isinstance(saved["reply"]["block_keywords"], list)

    def test_array_list_stays_list(self, authed_client, tmp_data_dir):
        """list 输入保持 list"""
        resp = authed_client.patch("/api/config", json={
            "proactive": {"video_times": ["10:00", "18:00"]}
        })
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["proactive"]["video_times"] == ["10:00", "18:00"]
        assert isinstance(saved["proactive"]["video_times"], list)

    def test_array_string_with_chinese_comma(self, authed_client, tmp_data_dir):
        """中文逗号也能识别"""
        resp = authed_client.patch("/api/config", json={
            "reply": {"block_keywords": "傻逼，草泥马，滚"}
        })
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["block_keywords"] == ["傻逼", "草泥马", "滚"]

    def test_number_string_normalized(self, authed_client, tmp_data_dir):
        """字符串 '2048' 转数字"""
        resp = authed_client.patch("/api/config", json={"llm": {"max_tokens": "2048"}})
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["llm"]["max_tokens"] == 2048
        assert isinstance(saved["llm"]["max_tokens"], int)

    def test_invalid_number_returns_400(self, authed_client):
        """非法 number 返回 CONFIG_VALIDATION_ERROR（PRD V3 §6.5）"""
        resp = authed_client.patch("/api/config", json={"llm": {"max_tokens": "abc"}})
        assert resp.status_code == 400
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_ERROR"

    def test_array_empty_string_becomes_empty_list(self, authed_client, tmp_data_dir):
        """空字符串数组变空 list"""
        resp = authed_client.patch("/api/config", json={
            "reply": {"block_keywords": ""}
        })
        assert resp.status_code == 200

        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["block_keywords"] == []


# ═══════════════════════════════════════════════════════
#  默认密码风险
# ═══════════════════════════════════════════════════════

class TestDefaultPasswordStatus:
    """默认密码风险可从 /api/status.security.default_password 获取"""

    def test_status_returns_default_password_flag(self, authed_client):
        resp = authed_client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "security" in body
        assert "default_password" in body["security"]
        # 当前 fixture 用 admin123，应当为 True
        assert body["security"]["default_password"] is True

    def test_status_default_password_false_after_change(self, authed_client):
        """修改密码后 default_password 应变 False"""
        # 改成非默认密码
        resp = authed_client.patch("/api/config", json={
            "web": {"admin_password": "new-strong-pwd-456"}
        })
        assert resp.status_code == 200

        # 重新登录（旧密码失效）—— 直接读 status 即可
        # 注意：热重载后 config_loader 已更新，但旧 session 仍有效
        resp = authed_client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["security"]["default_password"] is False

    def test_cors_open_flag(self, authed_client):
        """CORS 开放标志可读"""
        resp = authed_client.get("/api/status")
        body = resp.json()
        assert "security" in body
        assert "cors_open" in body["security"]
        # 默认 cors_origins=[]，cors_open=False
        assert body["security"]["cors_open"] is False
