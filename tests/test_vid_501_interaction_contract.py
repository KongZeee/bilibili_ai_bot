"""
VID-501 互动意图字段契约测试

PRD V5 §4.4 验证点：
1. want_favorite=True → favorite 动作放行（开关开 + 预算充足）
2. want_favorite=False, score=10 → favorite 不放行（评分不得把 false 升级为 true）
3. 缺失 want_favorite 字段 → False，不放行
4. want_favorite="yes"（字符串）→ False（类型校验）
5. LLM 异常（JSON 解析失败）→ 全 False
6. 旧字段 want_fav=True → 迁移适配为 want_favorite=True

同时覆盖 InteractionSuggestion.from_dict() 的 DTO 级契约。
"""
import json

import pytest

from bilibot.models.interaction import InteractionSuggestion
from bilibot.services.interaction_policy import InteractionPolicyEngine


# ── 测试用配置：favorite 开关开、预算充足、阈值 8 ──

def _fav_enabled_config():
    return {
        "interactions": {
            "like": {"enabled": False, "max_per_day": 10, "score_threshold": 6},
            "coin": {"enabled": False, "max_per_day": 0, "max_per_video": 1, "score_threshold": 8},
            "favorite": {"enabled": True, "max_per_day": 5, "score_threshold": 8},
            "comment": {"enabled": False, "max_per_day": 10, "score_threshold": 7},
        }
    }


@pytest.fixture
def policy(tmp_data_dir):
    return InteractionPolicyEngine(
        config=_fav_enabled_config(),
        data_dir=tmp_data_dir,
        account_id="test_vid_501",
    )


BVID = "BV1xx411c7XX"
OID = "123456"


# ═══════════════════════════════════════════════
# DTO 级契约：InteractionSuggestion.from_dict
# ═══════════════════════════════════════════════

def test_dto_want_favorite_true():
    s = InteractionSuggestion.from_dict({"want_favorite": True, "score": 9})
    assert s.want_favorite is True
    assert s.score == 9


def test_dto_missing_field_defaults_false():
    s = InteractionSuggestion.from_dict({"score": 10})
    assert s.want_favorite is False
    assert s.want_like is False
    assert s.want_coin is False
    assert s.want_comment is False


def test_dto_wrong_type_string_becomes_false():
    s = InteractionSuggestion.from_dict({"want_favorite": "yes"})
    assert s.want_favorite is False


def test_dto_wrong_type_int_becomes_false():
    # 非 bool 类型（含 int 1）一律按类型错误处理为 False
    s = InteractionSuggestion.from_dict({"want_favorite": 1})
    assert s.want_favorite is False


def test_dto_none_input_all_false():
    # LLM 解析失败 → from_dict(None) 全 False
    s = InteractionSuggestion.from_dict(None)
    assert s.want_favorite is False
    assert s.want_like is False
    assert s.score == 0
    assert s.reason == ""


def test_dto_non_dict_input_all_false():
    s = InteractionSuggestion.from_dict("not a dict")  # type: ignore[arg-type]
    assert s.want_favorite is False


def test_dto_legacy_want_fav_converts_to_want_favorite():
    # 旧字段 want_fav → want_favorite（迁移适配器）
    s = InteractionSuggestion.from_dict({"want_fav": True, "score": 9})
    assert s.want_favorite is True


def test_dto_want_favorite_takes_precedence_over_want_fav():
    # 两个字段同时存在时，want_favorite 优先；want_fav 被忽略
    s = InteractionSuggestion.from_dict({"want_favorite": False, "want_fav": True})
    assert s.want_favorite is False


def test_dto_score_non_int_becomes_zero():
    s = InteractionSuggestion.from_dict({"score": "high"})
    assert s.score == 0


def test_dto_score_float_truncated_to_int():
    s = InteractionSuggestion.from_dict({"score": 8.9})
    assert s.score == 8


# ═══════════════════════════════════════════════
# 策略引擎级契约：InteractionPolicyEngine.evaluate
# ═══════════════════════════════════════════════

def test_case1_want_favorite_true_proceeds(policy):
    """用例 1：want_favorite=True（且开关开、预算足、score≥阈值）→ 放行。"""
    decisions = policy.evaluate(
        llm_suggestion={"want_favorite": True, "score": 9},
        score=9,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is True
    assert fav["reason"] == "approved"


def test_case2_want_favorite_false_score_cannot_upgrade(policy):
    """用例 2：want_favorite=False, score=10 → 不放行（评分不得把 false 升级为 true）。"""
    decisions = policy.evaluate(
        llm_suggestion={"want_favorite": False, "score": 10},
        score=10,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is False
    # 必须是因为模型说 false，而不是 score_below
    assert fav["reason"] == "llm_false"


def test_case3_missing_want_favorite_field_no_action(policy):
    """用例 3：缺失 want_favorite 字段 → False，不放行。"""
    decisions = policy.evaluate(
        llm_suggestion={"score": 10, "mood": "开心"},
        score=10,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is False
    assert fav["reason"] == "llm_false"


def test_case4_want_favorite_string_becomes_false(policy):
    """用例 4：want_favorite="yes"（字符串）→ False（类型校验），不放行。"""
    decisions = policy.evaluate(
        llm_suggestion={"want_favorite": "yes", "score": 10},
        score=10,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is False
    assert fav["reason"] == "llm_false"


def test_case5_llm_parse_failure_all_false(policy):
    """用例 5：LLM 异常（JSON 解析失败，llm_suggestion=None）→ 全 False。"""
    decisions = policy.evaluate(
        llm_suggestion=None,
        score=10,
        bvid=BVID,
        oid=OID,
    )
    for action in ("like", "coin", "favorite", "comment"):
        assert decisions[action]["planned"] is False, f"{action} 应为 False"
        assert decisions[action]["reason"] == "llm_failure_default_false"


def test_case5b_llm_returns_garbage_dict_all_false(policy):
    """用例 5b：LLM 返回非预期内容（无 want_* 字段）→ 全 False。"""
    decisions = policy.evaluate(
        llm_suggestion={"error": "parse failed"},
        score=10,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is False
    assert fav["reason"] == "llm_false"


def test_case6_legacy_want_fav_converts_and_proceeds(policy):
    """用例 6：旧字段 want_fav=True → 迁移为 want_favorite=True → 放行。"""
    decisions = policy.evaluate(
        llm_suggestion={"want_fav": True, "score": 9},
        score=9,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is True
    assert fav["reason"] == "approved"


def test_score_threshold_further_rejects_true_suggestion(policy):
    """补充：want_favorite=True 但 score < 阈值 → 被阈值拒绝（只能拒绝，不能升级）。"""
    decisions = policy.evaluate(
        llm_suggestion={"want_favorite": True, "score": 5},
        score=5,
        bvid=BVID,
        oid=OID,
    )
    fav = decisions["favorite"]
    assert fav["planned"] is False
    assert fav["reason"].startswith("score_below_")


def test_accepts_interaction_suggestion_dto_directly(policy):
    """补充：evaluate 直接接受 InteractionSuggestion DTO。"""
    s = InteractionSuggestion(want_favorite=True, score=9)
    decisions = policy.evaluate(
        llm_suggestion=s,
        score=9,
        bvid=BVID,
        oid=OID,
    )
    assert decisions["favorite"]["planned"] is True


# ═══════════════════════════════════════════════
# humanized_behavior.evaluate_video 端到端契约
# ═══════════════════════════════════════════════

def _make_generator(llm_response):
    """构造一个 HumanizedCommentGenerator，注入固定 LLM 响应。"""
    from bilibot.humanized_behavior import (
        HumanBehaviorSimulator,
        HumanizedCommentGenerator,
    )

    class StubLLM:
        client = True

        async def generate(self, prompt, system_prompt=None, max_tokens=1024, **kwargs):
            return llm_response

    sim = HumanBehaviorSimulator()
    return HumanizedCommentGenerator(
        llm_adapter=StubLLM(),
        personality_system=None,
        knowledge_memory=None,
        behavior_sim=sim,
    )


@pytest.mark.asyncio
async def test_evaluate_video_emits_want_favorite_not_want_fav():
    """evaluate_video 输出 dict 必须含 want_favorite，且不残留 want_fav。"""
    raw = json.dumps({
        "score": 9,
        "mood": "开心",
        "comment": "好视频",
        "review": "不错",
        "want_like": True,
        "want_coin": False,
        "want_favorite": True,
        "want_comment": False,
    })
    gen = _make_generator(raw)
    result = await gen.evaluate_video("标题", "UP", "简介", ["标签"])
    assert result is not None
    assert result["want_favorite"] is True
    # 不得残留旧字段
    assert "want_fav" not in result


@pytest.mark.asyncio
async def test_evaluate_video_legacy_want_fav_migrated():
    """旧模型返回 want_fav → evaluate_video 迁移为 want_favorite。"""
    raw = json.dumps({
        "score": 9,
        "mood": "开心",
        "comment": "好视频",
        "review": "不错",
        "want_like": False,
        "want_coin": False,
        "want_fav": True,
        "want_comment": False,
    })
    gen = _make_generator(raw)
    result = await gen.evaluate_video("标题", "UP", "简介", ["标签"])
    assert result is not None
    assert result["want_favorite"] is True
    assert "want_fav" not in result


@pytest.mark.asyncio
async def test_evaluate_video_invalid_json_returns_none():
    """LLM 返回无法解析的 JSON → evaluate_video 返回 None（上游据此判定 LLM 失败）。"""
    gen = _make_generator("这不是 JSON")
    result = await gen.evaluate_video("标题", "UP", "简介", ["标签"])
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_video_wrong_type_coerced_to_false():
    """want_favorite 为字符串 → 归一化为 False。"""
    raw = json.dumps({
        "score": 9,
        "mood": "开心",
        "comment": "好视频",
        "review": "不错",
        "want_like": False,
        "want_coin": False,
        "want_favorite": "yes",
        "want_comment": False,
    })
    gen = _make_generator(raw)
    result = await gen.evaluate_video("标题", "UP", "简介", ["标签"])
    assert result is not None
    assert result["want_favorite"] is False
