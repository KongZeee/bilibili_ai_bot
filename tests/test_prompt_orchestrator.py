"""
tests/test_prompt_orchestrator.py - PromptOrchestrator 测试

PRD V3 §5.7 必需测试文件。
覆盖：
- 每个 SceneType 都能 build
- reply_comment 包含当前人格和 reply_rules
- proactive_comment 包含 proactive_comment_rules
- dynamic_post 包含 dynamic_rules
- weekly_summary 包含 weekly_rules
- 切换人格后 prompt 变化
- 视频上下文不足时出现"不得编造视频细节"
"""
import pytest

from bilibot.models import SceneType, Persona, ReplyContext, VideoContext
from bilibot.prompts.orchestrator import PromptOrchestrator
from bilibot.services.persona_store import PersonaStore


@pytest.fixture
def orchestrator(tmp_data_dir):
    ps = PersonaStore(data_dir=tmp_data_dir)
    return PromptOrchestrator(ps)


@pytest.fixture
def custom_persona(tmp_data_dir):
    """创建一个带场景规则的测试人格"""
    ps = PersonaStore(data_dir=tmp_data_dir)
    p_dict = ps.create_persona({
        "name": "测试人格A",
        "base_prompt": "你是测试人格A。",
        "reply_rules": "回复评论时遵守规则A",
        "proactive_comment_rules": "主动评论时遵守规则A",
        "dynamic_rules": "发动态时遵守规则A",
        "weekly_rules": "周总结时遵守规则A",
    })
    # create_persona 返回 dict，但 orchestrator.build 期望 Persona dataclass
    p = Persona.from_dict(p_dict)
    # 同时把它设为当前人格，便于其他测试在不传 persona 时也能取到
    ps.set_current(p.id)
    return ps, p


class TestSceneBuild:
    """每个场景都能 build"""

    def test_build_reply_comment(self, orchestrator):
        result = orchestrator.build(
            scene=SceneType.REPLY_COMMENT,
            content="你好",
            return_dict=True,
        )
        assert "system" in result and "user" in result
        assert "你好" in result["user"]

    def test_build_dynamic_post(self, orchestrator):
        result = orchestrator.build_dynamic_prompt(topic="今天心情不错")
        assert "system" in result
        assert "今天心情不错" in result["user"]

    def test_build_weekly_summary(self, orchestrator):
        result = orchestrator.build_weekly_summary_prompt(week_summary="看了3个视频")
        assert "system" in result
        assert "看了3个视频" in result["user"]

    def test_build_proactive_comment(self, orchestrator):
        v = VideoContext(title="测试视频", owner_name="UP主", desc="简介")
        result = orchestrator.build_proactive_comment_prompt(video=v)
        assert "system" in result
        assert "测试视频" in result["user"]

    def test_all_scenes_buildable(self, orchestrator):
        """所有 SceneType 都能 build，不抛异常"""
        for scene in SceneType:
            result = orchestrator.build(scene=scene, content="测试", return_dict=True)
            assert "system" in result
            assert "user" in result


class TestSceneRules:
    """场景规则注入"""

    def test_reply_comment_includes_reply_rules(self, custom_persona):
        ps, p = custom_persona
        orch = PromptOrchestrator(ps)
        result = orch.build(
            scene=SceneType.REPLY_COMMENT,
            content="你好",
            persona=p,
            return_dict=True,
        )
        assert "回复评论时遵守规则A" in result["system"]

    def test_proactive_comment_includes_rules(self, custom_persona):
        ps, p = custom_persona
        orch = PromptOrchestrator(ps)
        v = VideoContext(title="视频", owner_name="UP", desc="")
        result = orch.build_proactive_comment_prompt(video=v, persona=p)
        assert "主动评论时遵守规则A" in result["system"]

    def test_dynamic_post_includes_rules(self, custom_persona):
        ps, p = custom_persona
        orch = PromptOrchestrator(ps)
        result = orch.build_dynamic_prompt(persona=p)
        assert "发动态时遵守规则A" in result["system"]

    def test_weekly_summary_includes_rules(self, custom_persona):
        ps, p = custom_persona
        orch = PromptOrchestrator(ps)
        result = orch.build_weekly_summary_prompt(week_summary="活动", persona=p)
        assert "周总结时遵守规则A" in result["system"]

    def test_reply_comment_uses_current_persona(self, orchestrator):
        """不传 persona 时使用当前人格"""
        result = orchestrator.build(
            scene=SceneType.REPLY_COMMENT,
            content="测试",
            return_dict=True,
        )
        # 默认人格应被注入
        assert result["system"]


class TestPersonaSwitch:
    """切换人格后 prompt 变化"""

    def test_switch_persona_changes_prompt(self, tmp_data_dir):
        ps = PersonaStore(data_dir=tmp_data_dir)
        ps.create_persona({
            "name": "人格X",
            "base_prompt": "我是人格X，独特标识XMARK",
            "reply_rules": "X规则",
        })
        ps.create_persona({
            "name": "人格Y",
            "base_prompt": "我是人格Y，独特标识YMARK",
            "reply_rules": "Y规则",
        })
        personas = ps.list_personas()
        id_x = [p for p in personas if p["name"] == "人格X"][0]["id"]
        id_y = [p for p in personas if p["name"] == "人格Y"][0]["id"]

        orch = PromptOrchestrator(ps)

        ps.set_current(id_x)
        r1 = orch.build(scene=SceneType.REPLY_COMMENT, content="hi", return_dict=True)
        assert "XMARK" in r1["system"]
        assert "X规则" in r1["system"]

        ps.set_current(id_y)
        r2 = orch.build(scene=SceneType.REPLY_COMMENT, content="hi", return_dict=True)
        assert "YMARK" in r2["system"]
        assert "Y规则" in r2["system"]

        # 两次 prompt 应不同
        assert r1["system"] != r2["system"]


class TestVideoContextWarning:
    """视频上下文不足时出现"不得编造视频细节" """

    def test_incomplete_video_context_warns(self, orchestrator):
        v = VideoContext(title="测试", owner_name="UP", desc="简介")
        ctx = ReplyContext(video=v, video_context_complete=False)
        result = orchestrator.build(
            scene=SceneType.REPLY_COMMENT,
            content="评论",
            context=ctx,
            return_dict=True,
        )
        # context 注入到 user prompt
        assert "不得编造视频细节" in result["user"]

    def test_complete_video_context_no_warn(self, orchestrator):
        v = VideoContext(title="测试", owner_name="UP", desc="简介")
        ctx = ReplyContext(video=v, video_context_complete=True)
        result = orchestrator.build(
            scene=SceneType.REPLY_COMMENT,
            content="评论",
            context=ctx,
            return_dict=True,
        )
        assert "不得编造视频细节" not in result["user"]
