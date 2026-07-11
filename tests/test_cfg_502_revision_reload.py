"""
tests/test_cfg_502_revision_reload.py - PRD V5 CFG-502 乐观锁与热重载契约

覆盖：
- GET /api/config/full 返回 config_revision
- PATCH 不带 _expected_revision → 200（现有行为：跳过冲突检测）
- PATCH 带正确 _expected_revision → 200，返回新 revision
- PATCH 带过期 _expected_revision → 409 CONFIG_REVISION_CONFLICT
- 两次并发 PATCH（相同 revision）→ 一个 200，一个 409
- 专用 API（PATCH /api/video-analysis）递增统一 config_revision
- PATCH 响应包含逐字段热重载状态（applied dict）
- immediate 字段调用对应组件 reload_config()
- restart_account 字段返回 requires_restart
- restart_app 字段返回 requires_restart
- next_task 字段返回 pending_next_task
- save_config 自动递增 revision（专用 API 统一版本号）
- _resolve_reload_level / _find_changed_fields 单元测试
"""
import pytest
from starlette.testclient import TestClient


# ═══════════════════════════════════════════════════════
#  Mock SafetyChecker
# ═══════════════════════════════════════════════════════

class MockSafetyChecker:
    """跟踪 reload_config 调用的 SafetyChecker 替身"""

    def __init__(self):
        self.reload_calls = []

    def reload_config(self, config):
        self.reload_calls.append(config)

    def get_pause_status(self):
        return {"paused": False, "reason": "", "paused_at": None}

    def pause(self, reason=""):
        pass

    def resume(self):
        pass

    def list_blacklist(self):
        return []

    def add_to_blacklist(self, user_id, reason=""):
        pass

    def remove_from_blacklist(self, user_id):
        pass


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def mock_safety():
    return MockSafetyChecker()


@pytest.fixture
def web_client(tmp_data_dir, mock_safety):
    """构造完整 Web 应用，注入 mock_safety_checker"""
    from bilibot.web import panel as panel_mod
    panel_mod._sessions.clear()

    from bilibot.app.config_loader import ConfigLoader
    from bilibot.services.persona_store import PersonaStore
    from bilibot.prompts import PromptOrchestrator
    from bilibot.services.audit_store import AuditStore
    from bilibot.web.panel import create_web_app

    config_loader = ConfigLoader(config_dict={
        "bilibili": {"sessdata": "x", "bili_jct": "y", "dede_user_id": "1"},
        "llm": {"api_key": "k", "base_url": "http://localhost/v1", "model": "m"},
        "web": {
            "enabled": True, "host": "127.0.0.1", "port": 8080,
            "secret_key": "s", "admin_username": "admin",
            "admin_password": "admin123", "session_ttl_seconds": 3600,
            "cors_origins": [], "secure_cookies": False,
        },
        "safety": {
            "rate_limit_per_minute": 5,
            "rate_limit_per_hour": 50,
            "rate_limit_per_day": 200,
            "content_check_enabled": True,
            "min_content_length": 2,
            "max_content_length": 2000,
            "similarity_threshold": 0.8,
        },
        "reply": {"block_keywords": ["a", "b"]},
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
        safety_checker=mock_safety,
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
#  单元测试：_resolve_reload_level
# ═══════════════════════════════════════════════════════

class TestResolveReloadLevel:
    def test_reply_is_immediate(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("reply") == "immediate"

    def test_safety_is_immediate(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("safety") == "immediate"

    def test_web_search_is_immediate(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("web_search") == "immediate"

    def test_interactions_is_immediate(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("interactions") == "immediate"

    def test_video_analysis_is_next_task(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("video_analysis") == "next_task"

    def test_proactive_is_next_task(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("proactive") == "next_task"

    def test_memory_is_next_task(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("memory") == "next_task"

    def test_accounts_cookie_is_immediate(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("accounts", "sessdata") == "immediate"
        assert _resolve_reload_level("accounts", "cookie") == "immediate"
        assert _resolve_reload_level("accounts", "buvid3") == "immediate"

    def test_accounts_llm_id_is_restart_account(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("accounts", "llm_id") == "restart_account"

    def test_accounts_persona_id_is_restart_account(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("accounts", "persona_id") == "restart_account"

    def test_accounts_default_is_restart_account(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("accounts", "some_unknown_field") == "restart_account"

    def test_web_host_is_restart_app(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("web", "host") == "restart_app"

    def test_web_port_is_restart_app(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("web", "port") == "restart_app"

    def test_data_dir_is_restart_app(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("data_dir") == "restart_app"

    def test_web_cors_origins_is_immediate(self):
        from bilibot.api.config import _resolve_reload_level
        assert _resolve_reload_level("web", "cors_origins") == "immediate"


# ═══════════════════════════════════════════════════════
#  单元测试：_find_changed_fields
# ═══════════════════════════════════════════════════════

class TestFindChangedFields:
    def test_detects_top_level_change(self):
        from bilibot.api.config import _find_changed_fields
        original = {"reply": {"auto_reply": True}, "safety": {"rate": 5}}
        updated = {"reply": {"auto_reply": False}, "safety": {"rate": 5}}
        changed = _find_changed_fields(original, updated)
        keys = [c[0] for c in changed]
        assert "reply" in keys
        assert "safety" not in keys

    def test_detects_accounts_sub_field(self):
        from bilibot.api.config import _find_changed_fields
        original = {"accounts": [{"id": "main", "llm_id": "old", "sessdata": "s"}]}
        updated = {"accounts": [{"id": "main", "llm_id": "new", "sessdata": "s"}]}
        changed = _find_changed_fields(original, updated)
        pairs = set(changed)
        assert ("accounts", "llm_id") in pairs
        assert ("accounts", "sessdata") not in pairs

    def test_detects_web_sub_field(self):
        from bilibot.api.config import _find_changed_fields
        original = {"web": {"port": 8080, "host": "0.0.0.0"}}
        updated = {"web": {"port": 9090, "host": "0.0.0.0"}}
        changed = _find_changed_fields(original, updated)
        pairs = set(changed)
        assert ("web", "port") in pairs
        assert ("web", "host") not in pairs

    def test_skips_internal_keys(self):
        from bilibot.api.config import _find_changed_fields
        original = {"reply": {"auto_reply": True}}
        updated = {"reply": {"auto_reply": True}, "_expected_revision": 5, "config_revision": 10}
        changed = _find_changed_fields(original, updated)
        assert len(changed) == 0

    def test_no_changes_returns_empty(self):
        from bilibot.api.config import _find_changed_fields
        original = {"reply": {"auto_reply": True}}
        updated = {"reply": {"auto_reply": True}}
        assert _find_changed_fields(original, updated) == []


# ═══════════════════════════════════════════════════════
#  单元测试：save_config 自动递增
# ═══════════════════════════════════════════════════════

class TestSaveConfigAutoRevision:
    def test_auto_bumps_when_not_set(self, tmp_data_dir):
        from bilibot.app.config_loader import ConfigLoader
        cl = ConfigLoader(config_dict={"data_dir": tmp_data_dir, "reply": {"auto_reply": True}})
        assert cl.get_raw_config().get("config_revision", 0) == 0
        raw = cl.get_raw_config()
        raw["reply"] = {"auto_reply": False}
        cl.save_config(raw, str(tmp_data_dir + "/config.yaml"))
        assert cl.get_raw_config()["config_revision"] == 1

    def test_auto_bumps_on_second_save(self, tmp_data_dir):
        from bilibot.app.config_loader import ConfigLoader
        cl = ConfigLoader(config_dict={"data_dir": tmp_data_dir, "reply": {"auto_reply": True}})
        # First save (no explicit revision)
        raw = cl.get_raw_config()
        raw["reply"] = {"auto_reply": False}
        cl.save_config(raw, str(tmp_data_dir + "/config.yaml"))
        assert cl.get_raw_config()["config_revision"] == 1
        # Second save (no explicit revision)
        raw = cl.get_raw_config()
        raw["reply"] = {"auto_reply": True}
        cl.save_config(raw, str(tmp_data_dir + "/config.yaml"))
        assert cl.get_raw_config()["config_revision"] == 2

    def test_preserves_explicit_revision(self, tmp_data_dir):
        from bilibot.app.config_loader import ConfigLoader
        cl = ConfigLoader(config_dict={"data_dir": tmp_data_dir, "reply": {"auto_reply": True}})
        raw = cl.get_raw_config()
        raw["reply"] = {"auto_reply": False}
        raw["config_revision"] = 42  # 显式设置
        cl.save_config(raw, str(tmp_data_dir + "/config.yaml"))
        assert cl.get_raw_config()["config_revision"] == 42


# ═══════════════════════════════════════════════════════
#  API 集成测试：config_revision
# ═══════════════════════════════════════════════════════

class TestConfigRevision:
    def test_get_config_full_returns_revision(self, authed_client):
        resp = authed_client.get("/api/config/full")
        assert resp.status_code == 200
        body = resp.json()
        assert "config_revision" in body
        assert isinstance(body["config_revision"], int)

    def test_patch_without_expected_revision_succeeds(self, authed_client):
        """不带 _expected_revision → 200（现有行为：跳过冲突检测）"""
        resp = authed_client.patch("/api/config", json={
            "reply": {"min_comment_length": 5}
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert "config_revision" in body

    def test_patch_with_correct_revision_succeeds(self, authed_client):
        # 获取当前 revision
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        # 用正确 revision 提交
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 10}
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["config_revision"] == rev + 1

    def test_patch_with_stale_revision_returns_409(self, authed_client):
        # 获取当前 revision
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        # 第一次 PATCH 推进 revision
        authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 3}
        })
        # 用过期 revision 再 PATCH → 409
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 7}
        })
        assert resp.status_code == 409
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_REVISION_CONFLICT"
        assert body["error"]["details"]["expected"] == rev
        assert body["error"]["details"]["actual"] == rev + 1

    def test_concurrent_patches_one_succeeds_one_409(self, authed_client):
        """模拟并发：两个请求都读取相同 revision，第一个成功，第二个 409"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        # 第一个 PATCH（成功）
        r1 = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 8}
        })
        assert r1.status_code == 200
        # 第二个 PATCH（相同 revision → 冲突）
        r2 = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 9}
        })
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "CONFIG_REVISION_CONFLICT"


# ═══════════════════════════════════════════════════════
#  API 集成测试：专用 API 递增统一 revision
# ═══════════════════════════════════════════════════════

class TestDedicatedApiRevision:
    def test_video_analysis_patch_increments_revision(self, authed_client):
        """PATCH /api/video-analysis 应递增统一 config_revision"""
        resp = authed_client.get("/api/config/full")
        rev_before = resp.json()["config_revision"]
        # 修改视频理解配置
        resp = authed_client.patch("/api/video-analysis", json={
            "enabled": True,
            "frame_extractor": "ffmpeg",
        })
        assert resp.status_code == 200, resp.text
        # 验证 config_revision 递增
        resp = authed_client.get("/api/config/full")
        rev_after = resp.json()["config_revision"]
        assert rev_after == rev_before + 1

    def test_image_generation_patch_increments_revision(self, authed_client):
        """PATCH /api/image-generation 应递增统一 config_revision"""
        resp = authed_client.get("/api/config/full")
        rev_before = resp.json()["config_revision"]
        resp = authed_client.patch("/api/image-generation", json={
            "enabled": True,
            "api_key": "test-key",
            "base_url": "http://x/v1",
            "model": "test-model",
            "default_size": "1024x768",
            "timeout": 120,
            "with_image": False,
        })
        assert resp.status_code == 200, resp.text
        resp = authed_client.get("/api/config/full")
        rev_after = resp.json()["config_revision"]
        assert rev_after == rev_before + 1


# ═══════════════════════════════════════════════════════
#  API 集成测试：热重载契约（applied dict）
# ═══════════════════════════════════════════════════════

class TestHotReloadContract:
    def test_patch_returns_applied_dict(self, authed_client):
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 15}
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "applied" in body
        assert isinstance(body["applied"], dict)
        assert "reply" in body["applied"]
        assert body["applied"]["reply"]["level"] == "immediate"

    def test_immediate_field_calls_reload_config(self, authed_client, mock_safety):
        """safety 字段变更应调用 safety_checker.reload_config()"""
        assert len(mock_safety.reload_calls) == 0
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "safety": {"rate_limit_per_minute": 99}
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"]["safety"]["level"] == "immediate"
        assert body["applied"]["safety"]["status"] == "applied"
        assert len(mock_safety.reload_calls) == 1

    def test_restart_account_field_returns_requires_restart(self, authed_client):
        """accounts.llm_id 变更 → requires_restart"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "accounts": [{"id": "main", "llm_id": "new_llm_id"}]
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "accounts.llm_id" in body["applied"]
        assert body["applied"]["accounts.llm_id"]["level"] == "restart_account"
        assert body["applied"]["accounts.llm_id"]["status"] == "requires_restart"

    def test_restart_app_field_returns_requires_restart(self, authed_client):
        """web.port 变更 → requires_restart"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "web": {"port": 9999}
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "web.port" in body["applied"]
        assert body["applied"]["web.port"]["level"] == "restart_app"
        assert body["applied"]["web.port"]["status"] == "requires_restart"

    def test_next_task_field_returns_pending(self, authed_client):
        """video_analysis 变更 → pending_next_task"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "video_analysis": {"enabled": True}
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "video_analysis" in body["applied"]
        assert body["applied"]["video_analysis"]["level"] == "next_task"
        assert body["applied"]["video_analysis"]["status"] == "pending_next_task"

    def test_accounts_cookie_is_immediate(self, authed_client):
        """accounts.sessdata 变更 → immediate"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "accounts": [{"id": "main", "sessdata": "new_sessdata_value"}]
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "accounts.sessdata" in body["applied"]
        assert body["applied"]["accounts.sessdata"]["level"] == "immediate"

    def test_multiple_fields_in_one_patch(self, authed_client):
        """一次 PATCH 多个字段 → applied 包含所有变更字段"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "reply": {"min_comment_length": 20},
            "safety": {"rate_limit_per_hour": 77},
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "reply" in body["applied"]
        assert "safety" in body["applied"]
        assert body["applied"]["reply"]["level"] == "immediate"
        assert body["applied"]["safety"]["level"] == "immediate"

    def test_data_dir_returns_requires_restart(self, authed_client):
        """data_dir 变更 → restart_app / requires_restart"""
        resp = authed_client.get("/api/config/full")
        rev = resp.json()["config_revision"]
        resp = authed_client.patch("/api/config", json={
            "_expected_revision": rev,
            "data_dir": "/new/data/path"
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "data_dir" in body["applied"]
        assert body["applied"]["data_dir"]["level"] == "restart_app"
        assert body["applied"]["data_dir"]["status"] == "requires_restart"
