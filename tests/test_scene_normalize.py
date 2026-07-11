"""
tests/test_scene_normalize.py - SceneType 字符串兼容测试

PRD V4 §4.6 / §7.5 必需测试文件。
覆盖：
- normalize_scene() 函数：
  - SceneType 枚举直接返回
  - 合法字符串转枚举
  - 非法字符串 fallback 到 REPLY_COMMENT
  - 大小写不敏感
- Orchestrator.build_system_prompt(scene="reply_comment") 含 reply_rules
- Orchestrator.build / build_user_prompt 兼容字符串 scene
- /api/personas/test 返回的 system_prompt 含 reply_rules
- /api/personas/preview?scene=dynamic_post 含 dynamic_rules
- /api/personas/preview?scene=reply_comment 含 reply_rules
"""
import pytest

from bilibot.models import SceneType, Persona
from bilibot.prompts.orchestrator import PromptOrchestrator, normalize_scene
from bilibot.services.persona_store import PersonaStore


# ═══════════════════════════════════════════════════════
#  normalize_scene 函数单测
# ═══════════════════════════════════════════════════════

class TestNormalizeScene:
    """PRD V4 §4.6.1：normalize_scene 函数"""

    def test_enum_returned_as_is(self):
        """SceneType 枚举直接返回"""
        for scene in SceneType:
            assert normalize_scene(scene) is scene

    def test_valid_string_converted(self):
        """合法字符串转对应 SceneType"""
        assert normalize_scene("reply_comment") is SceneType.REPLY_COMMENT
        assert normalize_scene("dynamic_post") is SceneType.DYNAMIC_POST
        assert normalize_scene("weekly_summary") is SceneType.WEEKLY_SUMMARY
        assert normalize_scene("proactive_comment") is SceneType.PROACTIVE_COMMENT

    def test_case_insensitive(self):
        """大小写不敏感"""
        assert normalize_scene("Reply_Comment") is SceneType.REPLY_COMMENT
        assert normalize_scene("REPLY_COMMENT") is SceneType.REPLY_COMMENT
        assert normalize_scene("Dynamic_Post") is SceneType.DYNAMIC_POST

    def test_invalid_string_fallback_to_reply_comment(self):
        """非法字符串 fallback 到 REPLY_COMMENT"""
        assert normalize_scene("invalid_scene") is SceneType.REPLY_COMMENT
        assert normalize_scene("not_a_scene") is SceneType.REPLY_COMMENT
        assert normalize_scene("") is SceneType.REPLY_COMMENT

    def test_invalid_type_fallback_to_reply_comment(self):
        """非字符串非枚举类型 fallback"""
        assert normalize_scene(None) is SceneType.REPLY_COMMENT
        assert normalize_scene(123) is SceneType.REPLY_COMMENT
        assert normalize_scene(["reply_comment"]) is SceneType.REPLY_COMMENT


# ═══════════════════════════════════════════════════════
#  Orchestrator 兼容字符串 scene
# ═══════════════════════════════════════════════════════

@pytest.fixture
def persona_with_scene_rules(tmp_data_dir):
    """创建一个带明显场景规则的人格"""
    ps = PersonaStore(data_dir=tmp_data_dir)
    p_dict = ps.create_persona({
        "name": "SceneRule测试人格",
        "base_prompt": "基础人设",
        "reply_rules": "REPLY_RULES_MARKER",
        "proactive_comment_rules": "PROACTIVE_RULES_MARKER",
        "dynamic_rules": "DYNAMIC_RULES_MARKER",
        "weekly_rules": "WEEKLY_RULES_MARKER",
    })
    p = Persona.from_dict(p_dict)
    ps.set_current(p.id)
    return ps, p


class TestOrchestratorStringScene:
    """PRD V4 §4.6.2：Orchestrator 接受字符串 scene"""

    def test_build_system_prompt_string_reply_comment(self, persona_with_scene_rules):
        """scene='reply_comment' 字符串注入 reply_rules"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        sp = orch.build_system_prompt(scene="reply_comment", persona=p)
        assert "REPLY_RULES_MARKER" in sp

    def test_build_system_prompt_string_dynamic_post(self, persona_with_scene_rules):
        """scene='dynamic_post' 字符串注入 dynamic_rules"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        sp = orch.build_system_prompt(scene="dynamic_post", persona=p)
        assert "DYNAMIC_RULES_MARKER" in sp

    def test_build_system_prompt_string_weekly_summary(self, persona_with_scene_rules):
        """scene='weekly_summary' 字符串注入 weekly_rules"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        sp = orch.build_system_prompt(scene="weekly_summary", persona=p)
        assert "WEEKLY_RULES_MARKER" in sp

    def test_build_string_scene_same_as_enum(self, persona_with_scene_rules):
        """字符串 scene 与枚举 scene 行为一致"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        sp_str = orch.build_system_prompt(scene="reply_comment", persona=p)
        sp_enum = orch.build_system_prompt(scene=SceneType.REPLY_COMMENT, persona=p)
        assert sp_str == sp_enum

    def test_build_dict_with_string_scene(self, persona_with_scene_rules):
        """build(return_dict=True) 接受字符串 scene"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        result = orch.build(
            scene="reply_comment",
            content="test",
            persona=p,
            return_dict=True,
        )
        assert "REPLY_RULES_MARKER" in result["system"]
        # return_dict 中 scene 字段应是规范化的 value
        assert result["scene"] == SceneType.REPLY_COMMENT.value

    def test_build_user_prompt_string_scene(self, persona_with_scene_rules):
        """build_user_prompt 接受字符串 scene"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        up = orch.build_user_prompt(scene="reply_comment", content="hi", persona=p)
        assert "hi" in up

    def test_invalid_scene_falls_back(self, persona_with_scene_rules):
        """非法 scene 字符串 fallback 到 reply_comment（注入 reply_rules）"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        sp = orch.build_system_prompt(scene="totally_invalid", persona=p)
        # 因为 fallback 到 REPLY_COMMENT，应注入 reply_rules
        assert "REPLY_RULES_MARKER" in sp


# ═══════════════════════════════════════════════════════
#  /api/personas/test 和 /api/personas/preview 一致性
# ═══════════════════════════════════════════════════════

class TestPersonasAPIConsistency:
    """PRD V4 §4.6.3：Web 预览/测试台与真实生成一致"""

    @pytest.fixture
    def client(self, persona_with_scene_rules):
        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from bilibot.api.personas import create_personas_routes

        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        routes = create_personas_routes(ps, orch)
        return TestClient(Starlette(routes=routes))

    def test_personas_test_includes_reply_rules(self, client, persona_with_scene_rules):
        """POST /api/personas/test 返回的 system_prompt 含 reply_rules"""
        ps, p = persona_with_scene_rules
        resp = client.post("/api/personas/test", json={
            "input": "你好",
            "persona_id": p.id,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        sp = data["data"]["system_prompt"]
        assert "REPLY_RULES_MARKER" in sp

    def test_personas_preview_reply_comment_includes_reply_rules(
        self, client, persona_with_scene_rules,
    ):
        """GET /api/personas/preview?scene=reply_comment 含 reply_rules"""
        ps, p = persona_with_scene_rules
        resp = client.get(
            f"/api/personas/preview?persona_id={p.id}&scene=reply_comment"
        )
        assert resp.status_code == 200
        preview = resp.json()["data"]["preview"]
        assert "REPLY_RULES_MARKER" in preview

    def test_personas_preview_dynamic_post_includes_dynamic_rules(
        self, client, persona_with_scene_rules,
    ):
        """GET /api/personas/preview?scene=dynamic_post 含 dynamic_rules"""
        ps, p = persona_with_scene_rules
        resp = client.get(
            f"/api/personas/preview?persona_id={p.id}&scene=dynamic_post"
        )
        assert resp.status_code == 200
        preview = resp.json()["data"]["preview"]
        assert "DYNAMIC_RULES_MARKER" in preview

    def test_personas_preview_invalid_scene_falls_back(
        self, client, persona_with_scene_rules,
    ):
        """非法 scene fallback 到 reply_comment（仍注入 reply_rules）"""
        ps, p = persona_with_scene_rules
        resp = client.get(
            f"/api/personas/preview?persona_id={p.id}&scene=invalid_scene"
        )
        assert resp.status_code == 200
        preview = resp.json()["data"]["preview"]
        # fallback 到 reply_comment，应包含 reply_rules
        assert "REPLY_RULES_MARKER" in preview

    def test_preview_matches_orchestrator(self, persona_with_scene_rules):
        """preview_system_prompt 与 Orchestrator.build_system_prompt 一致性核心字段"""
        ps, p = persona_with_scene_rules
        orch = PromptOrchestrator(ps)
        # Orchestrator 生成的 system prompt
        orch_sp = orch.build_system_prompt(scene="reply_comment", persona=p)
        # Web 预览
        preview = ps.preview_system_prompt(persona_id=p.id, scene="reply_comment")
        # 两者都应包含 base_prompt / speaking_style / boundaries / reply_rules
        assert p.base_prompt in orch_sp
        assert p.base_prompt in preview
        assert "REPLY_RULES_MARKER" in orch_sp
        assert "REPLY_RULES_MARKER" in preview


# ═══════════════════════════════════════════════════════
#  切换人格后预览即时反映
# ═══════════════════════════════════════════════════════

class TestSwitchPersonaPreview:
    """PRD V4 §8.5：切换当前人格后，预览和测试台立即反映新人格"""

    def test_switch_persona_updates_preview(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        ps.create_persona({
            "name": "A 人格",
            "base_prompt": "我是人格A，独特标识 AAA_MARK",
            "reply_rules": "A 的回复规则",
        })
        ps.create_persona({
            "name": "B 人格",
            "base_prompt": "我是人格B，独特标识 BBB_MARK",
            "reply_rules": "B 的回复规则",
        })
        personas = ps.list_personas()
        id_a = [p for p in personas if p["name"] == "A 人格"][0]["id"]
        id_b = [p for p in personas if p["name"] == "B 人格"][0]["id"]

        # 切换到 A
        ps.set_current(id_a)
        preview_a = ps.preview_system_prompt(scene="reply_comment")
        assert "AAA_MARK" in preview_a
        assert "A 的回复规则" in preview_a

        # 切换到 B
        ps.set_current(id_b)
        preview_b = ps.preview_system_prompt(scene="reply_comment")
        assert "BBB_MARK" in preview_b
        assert "B 的回复规则" in preview_b

        # 两次预览不同
        assert preview_a != preview_b
