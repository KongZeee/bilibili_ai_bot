"""
tests/test_cfg_501_structured_arrays.py - PRD V5 CFG-501 结构化数组编辑

覆盖：
- profiles / accounts / llm_providers 作为对象数组 PATCH → 成功
- profiles / accounts / llm_providers 作为字符串数组 PATCH → 400
- profiles / accounts / llm_providers 作为逗号分隔字符串 PATCH → 400
- 对象数组 round-trip 深度相等（结构不被破坏）
- schema 标记 itemType=object（前端区分渲染依据）
- 字符串数组仍可逗号分隔提交（向后兼容）
- validate_config_structure 单元测试
"""
import pytest
from starlette.testclient import TestClient


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def web_client(tmp_data_dir):
    """构造完整 Web 应用，预置 profiles/accounts/llm_providers 对象数组"""
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
        "profiles": [
            {"id": "default", "name": "默认", "default_persona": "default", "personas": ["default"]},
        ],
        "accounts": [
            {"id": "main", "name": "主账号", "sessdata": "", "bili_jct": "",
             "dede_user_id": "", "profile_id": "default", "enabled": True},
        ],
        "llm_providers": [
            {"id": "sf", "name": "硅基流动", "api_key": "", "base_url": "http://x/v1",
             "model": "qwen", "enabled": True},
        ],
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
#  validate_config_structure 单元测试
# ═══════════════════════════════════════════════════════

class TestValidateConfigStructure:
    """validate_config_structure 直接测试"""

    def test_object_array_passes(self):
        from bilibot.api.config import validate_config_structure
        # 不应抛异常
        validate_config_structure({
            "profiles": [{"id": "a"}],
            "accounts": [{"id": "b"}],
            "llm_providers": [{"id": "c"}],
        })

    def test_string_array_profiles_rejected(self):
        from bilibot.api.config import validate_config_structure, ConfigValidationError
        with pytest.raises(ConfigValidationError):
            validate_config_structure({"profiles": ["a", "b"]})

    def test_string_array_accounts_rejected(self):
        from bilibot.api.config import validate_config_structure, ConfigValidationError
        with pytest.raises(ConfigValidationError):
            validate_config_structure({"accounts": ["main", "sub"]})

    def test_string_array_llm_providers_rejected(self):
        from bilibot.api.config import validate_config_structure, ConfigValidationError
        with pytest.raises(ConfigValidationError):
            validate_config_structure({"llm_providers": ["sf", "openai"]})

    def test_comma_separated_string_rejected(self):
        from bilibot.api.config import validate_config_structure, ConfigValidationError
        with pytest.raises(ConfigValidationError):
            validate_config_structure({"profiles": "default, multi"})

    def test_non_list_rejected(self):
        from bilibot.api.config import validate_config_structure, ConfigValidationError
        with pytest.raises(ConfigValidationError):
            validate_config_structure({"accounts": {"id": "main"}})

    def test_missing_fields_pass(self):
        """未提交对象数组字段时不应报错"""
        from bilibot.api.config import validate_config_structure
        validate_config_structure({"llm": {"model": "gpt"}})

    def test_none_value_passes(self):
        """None 值跳过（保留原值）"""
        from bilibot.api.config import validate_config_structure
        validate_config_structure({"profiles": None})


# ═══════════════════════════════════════════════════════
#  API 集成测试 - 对象数组 PATCH 成功
# ═══════════════════════════════════════════════════════

class TestObjectArrayPatchSuccess:
    """对象数组 PATCH 应成功"""

    def test_profiles_object_array_succeeds(self, authed_client, tmp_data_dir):
        resp = authed_client.patch("/api/config", json={
            "profiles": [
                {"id": "default", "name": "默认", "default_persona": "default",
                 "personas": ["default"]},
                {"id": "multi", "name": "多风格", "default_persona": "tech",
                 "personas": ["tech", "casual"]},
            ]
        })
        assert resp.status_code == 200, resp.text
        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert len(saved["profiles"]) == 2
        assert saved["profiles"][0]["id"] == "default"
        assert saved["profiles"][1]["id"] == "multi"
        assert saved["profiles"][1]["personas"] == ["tech", "casual"]

    def test_accounts_object_array_succeeds(self, authed_client, tmp_data_dir):
        resp = authed_client.patch("/api/config", json={
            "accounts": [
                {"id": "main", "name": "主账号", "enabled": True},
                {"id": "sub", "name": "小号", "enabled": False},
            ]
        })
        assert resp.status_code == 200, resp.text
        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert len(saved["accounts"]) == 2
        assert saved["accounts"][0]["id"] == "main"
        assert saved["accounts"][1]["id"] == "sub"

    def test_llm_providers_object_array_succeeds(self, authed_client, tmp_data_dir):
        resp = authed_client.patch("/api/config", json={
            "llm_providers": [
                {"id": "sf", "name": "硅基流动", "api_key": "k1",
                 "base_url": "http://x/v1", "model": "qwen"},
                {"id": "openai", "name": "OpenAI", "api_key": "k2",
                 "base_url": "http://y/v1", "model": "gpt-4o"},
            ]
        })
        assert resp.status_code == 200, resp.text
        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert len(saved["llm_providers"]) == 2
        assert saved["llm_providers"][0]["id"] == "sf"
        assert saved["llm_providers"][1]["id"] == "openai"


# ═══════════════════════════════════════════════════════
#  API 集成测试 - 字符串数组 PATCH 必须 400
# ═══════════════════════════════════════════════════════

class TestStringArrayPatchRejected:
    """对象数组收到字符串数组必须 400（不尝试转换）"""

    def test_profiles_string_array_returns_400(self, authed_client):
        resp = authed_client.patch("/api/config", json={
            "profiles": ["default", "multi"]
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_ERROR"
        assert "profiles" in body["error"]["message"]

    def test_accounts_string_array_returns_400(self, authed_client):
        resp = authed_client.patch("/api/config", json={
            "accounts": ["main", "sub"]
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_ERROR"
        assert "accounts" in body["error"]["message"]

    def test_llm_providers_string_array_returns_400(self, authed_client):
        resp = authed_client.patch("/api/config", json={
            "llm_providers": ["sf", "openai"]
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "CONFIG_VALIDATION_ERROR"
        assert "llm_providers" in body["error"]["message"]

    def test_profiles_comma_string_returns_400(self, authed_client):
        """逗号分隔字符串也应 400（不能被解析为对象数组）"""
        resp = authed_client.patch("/api/config", json={
            "profiles": "default, multi"
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "CONFIG_VALIDATION_ERROR"

    def test_accounts_comma_string_returns_400(self, authed_client):
        resp = authed_client.patch("/api/config", json={
            "accounts": "main, sub"
        })
        assert resp.status_code == 400

    def test_llm_providers_comma_string_returns_400(self, authed_client):
        resp = authed_client.patch("/api/config", json={
            "llm_providers": "sf, openai"
        })
        assert resp.status_code == 400

    def test_mixed_object_and_string_returns_400(self, authed_client):
        """混合数组（部分项是字符串）也应 400"""
        resp = authed_client.patch("/api/config", json={
            "profiles": [
                {"id": "ok"},
                "bad-string-item",
            ]
        })
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "CONFIG_VALIDATION_ERROR"
        assert "profiles[1]" in body["error"]["message"]


# ═══════════════════════════════════════════════════════
#  Round-trip 深度相等
# ═══════════════════════════════════════════════════════

class TestObjectArrayRoundTrip:
    """对象数组 round-trip 保留结构（深度相等）"""

    def test_profiles_round_trip_preserves_structure(self, authed_client, tmp_data_dir):
        original_profiles = [
            {"id": "default", "name": "默认人格组", "default_persona": "default",
             "personas": ["default", "tech"]},
            {"id": "multi", "name": "多风格", "default_persona": "casual",
             "personas": ["casual", "gamer", "tech"]},
        ]
        resp = authed_client.patch("/api/config", json={"profiles": original_profiles})
        assert resp.status_code == 200

        # 读取 GET /api/config/full 验证结构保留
        resp = authed_client.get("/api/config/full")
        assert resp.status_code == 200
        cfg = resp.json()["data"]
        assert cfg["profiles"] == original_profiles

    def test_accounts_round_trip_preserves_structure(self, authed_client, tmp_data_dir):
        original_accounts = [
            {"id": "main", "name": "主账号", "sessdata": "s1", "bili_jct": "j1",
             "dede_user_id": "100", "profile_id": "default", "enabled": True},
            {"id": "sub", "name": "小号", "sessdata": "s2", "bili_jct": "j2",
             "dede_user_id": "200", "profile_id": "default", "enabled": False},
        ]
        resp = authed_client.patch("/api/config", json={"accounts": original_accounts})
        assert resp.status_code == 200

        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        # sessdata 是敏感字段，会被脱敏；比较时排除
        for i in range(len(original_accounts)):
            original_accounts[i]["sessdata"] = "***已配置***"
            original_accounts[i]["bili_jct"] = "***已配置***"
        assert cfg["accounts"] == original_accounts

    def test_llm_providers_round_trip_preserves_structure(self, authed_client, tmp_data_dir):
        original_providers = [
            {"id": "sf", "name": "硅基流动", "api_key": "k1",
             "base_url": "http://x/v1", "model": "qwen", "enabled": True},
            {"id": "openai", "name": "OpenAI", "api_key": "k2",
             "base_url": "http://y/v1", "model": "gpt-4o", "enabled": True},
        ]
        resp = authed_client.patch("/api/config", json={"llm_providers": original_providers})
        assert resp.status_code == 200

        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        # api_key 是敏感字段
        for i in range(len(original_providers)):
            original_providers[i]["api_key"] = "***已配置***"
        assert cfg["llm_providers"] == original_providers

    def test_no_object_object_degradation(self, authed_client):
        """round-trip 后对象数组中不应出现 [object Object] 字符串"""
        resp = authed_client.patch("/api/config", json={
            "profiles": [{"id": "x", "name": "X", "personas": ["a", "b"]}]
        })
        assert resp.status_code == 200
        resp = authed_client.get("/api/config/full")
        cfg = resp.json()["data"]
        # 关键断言：没有任何值变成 "[object Object]"
        profiles_json = str(cfg["profiles"])
        assert "[object Object]" not in profiles_json
        assert isinstance(cfg["profiles"], list)
        assert isinstance(cfg["profiles"][0], dict)


# ═══════════════════════════════════════════════════════
#  Schema itemType 标记
# ═══════════════════════════════════════════════════════

class TestSchemaItemType:
    """schema 中对象数组字段标记 itemType=object"""

    def test_schema_marks_profiles_as_object_array(self, authed_client):
        resp = authed_client.get("/api/config/schema")
        assert resp.status_code == 200
        schema = resp.json()["data"]
        assert schema["profiles"]["type"] == "array"
        assert schema["profiles"]["itemType"] == "object"

    def test_schema_marks_string_arrays_as_string(self, authed_client):
        """字符串数组标记为 itemType=string"""
        resp = authed_client.get("/api/config/schema")
        schema = resp.json()["data"]
        assert schema["reply"]["fields"]["block_keywords"]["itemType"] == "string"
        assert schema["dynamic_publish"]["fields"]["topics"]["itemType"] == "string"
        assert schema["proactive"]["fields"]["interest_keywords"]["itemType"] == "string"
        assert schema["web"]["fields"]["cors_origins"]["itemType"] == "string"

    def test_schema_profiles_has_item_fields(self, authed_client):
        """对象数组应提供 item_fields 描述项结构"""
        resp = authed_client.get("/api/config/schema")
        schema = resp.json()["data"]
        assert "item_fields" in schema["profiles"]
        assert "id" in schema["profiles"]["item_fields"]
        assert "name" in schema["profiles"]["item_fields"]


# ═══════════════════════════════════════════════════════
#  字符串数组向后兼容
# ═══════════════════════════════════════════════════════

class TestStringArrayBackwardCompat:
    """字符串数组仍可使用逗号分隔提交（向后兼容）"""

    def test_block_keywords_string_accepted(self, authed_client, tmp_data_dir):
        resp = authed_client.patch("/api/config", json={
            "reply": {"block_keywords": "a,b,c"}
        })
        assert resp.status_code == 200
        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["block_keywords"] == ["a", "b", "c"]

    def test_block_keywords_list_accepted(self, authed_client, tmp_data_dir):
        resp = authed_client.patch("/api/config", json={
            "reply": {"block_keywords": ["x", "y"]}
        })
        assert resp.status_code == 200
        import yaml
        with open(tmp_data_dir + "/config.yaml", "r", encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["reply"]["block_keywords"] == ["x", "y"]
