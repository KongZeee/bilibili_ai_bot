from types import SimpleNamespace

import pytest

from bilibot.context_builder import ContextBuilder
from bilibot.memory_brain import (
    ActivityMemoryError,
    MemoryBrainService,
    bot_action_observation,
    text_observation,
)
from bilibot.models import ReplyContext


@pytest.mark.asyncio
async def test_begin_activity_commits_intent_and_forces_cross_scene_recent_memory(tmp_path):
    brain = MemoryBrainService("acc", tmp_path / "acc")
    await brain.archive_observation_async(
        bot_action_observation(
            account_id="acc",
            action_key="dynamic:old",
            action_type="dynamic_post",
            text="刚刚发了一条关于雨夜散步的动态",
            published=True,
            state="completed",
            scene="dynamic_post",
        )
    )
    await brain.archive_observation_async(
        text_observation(
            account_id="acc",
            idempotency_key="diary:old",
            source_type="diary",
            event_type="diary",
            text="日记里记下了看完番剧后的平静心情",
            title="昨日日记",
            scene="companion",
        )
    )

    context = await brain.begin_activity(
        action_key="comment:42",
        action_type="reply_comment",
        current_activity="正在回复评论，并回想最近做过的事。",
        query="雨夜 番剧 评论",
        scene="reply_comment",
    )

    assert "正在回复评论" in context.prompt_text
    assert "recent_self_memory" in context.prompt_text
    assert any("雨夜散步" in item for item in context.recent_self_actions)
    assert any("昨日日记" in item for item in context.recent_self_actions)
    assert context.intent_event_id
    intent = brain.get_event(context.intent_event_id, chunks_per_event=0)
    assert intent["metadata"]["action_state"] == "intent"

    await brain.finish_activity(
        action_key="comment:42",
        action_type="reply_comment",
        result_text="评论已经回复并归档。",
        scene="reply_comment",
    )
    next_context = await brain.begin_activity(
        action_key="diary:next",
        action_type="write_diary",
        current_activity="正在写日记。",
        scene="companion",
    )
    assert any("评论已经回复" in item for item in next_context.recent_self_actions)
    assert not any(
        "正在回复评论" in item for item in next_context.recent_self_actions
    )


@pytest.mark.asyncio
async def test_begin_activity_is_fail_closed_when_intent_commit_is_not_confirmed(tmp_path):
    brain = MemoryBrainService("acc", tmp_path / "acc")

    async def reject(_envelope):
        return SimpleNamespace(source_committed=False)

    brain.archive_observation_async = reject
    with pytest.raises(ActivityMemoryError, match="not confirmed"):
        await brain.begin_activity(
            action_key="diary:today",
            action_type="write_diary",
            current_activity="正在写日记。",
            scene="companion",
        )


def test_context_builder_prefers_v6_recent_actions_and_deduplicates_legacy_lane():
    class DataStore:
        def get_recent_actions(self, limit=5):
            return [
                {"summary": "刚看完一集番剧"},
                {"summary": "刚发完动态"},
            ]

    context = ReplyContext(
        recent_bot_actions=["刚看完一集番剧", "正在回复评论"],
    )
    built = ContextBuilder(data_store=DataStore()).build(context)

    assert built["text"].count("刚看完一集番剧") == 1
    assert "正在回复评论" in built["text"]
    assert "刚发完动态" in built["text"]


@pytest.mark.asyncio
async def test_recent_lane_keeps_distinctive_memories_under_noise(tmp_path):
    """Automation bursts must not bury distinctive self experiences."""
    from bilibot.memory_brain import bot_action_observation, video_observation

    brain = MemoryBrainService("acc", tmp_path / "acc")
    await brain.archive_observation_async(
        video_observation(
            account_id="acc",
            observation_key="v1",
            bvid="BV1COVERKEY01",
            oid="1",
            title="旧物收藏室探访",
            owner="UP",
            context={"video_detail": "青铜钥匙放在旧唱片机下面"},
            video_detail="青铜钥匙放在旧唱片机下面",
        )
    )
    await brain.archive_observation_async(
        bot_action_observation(
            account_id="acc",
            action_key="dynamic:rain",
            action_type="dynamic_post",
            text="发了一条关于雨夜散步的动态，提到路灯和潮湿的石板路。",
            published=True,
            state="completed",
            scene="dynamic_post",
            title="雨夜散步动态",
        )
    )
    for i in range(40):
        await brain.begin_activity(
            action_key=f"noise:{i}",
            action_type="tick",
            current_activity=f"noise {i}",
            query="日常",
            scene="companion",
        )
        await brain.finish_activity(
            action_key=f"noise:{i}",
            action_type="tick",
            result_text=f"noise done {i}",
            state="completed",
            scene="companion",
        )

    ctx = await brain.begin_activity(
        action_key="reply:noise",
        action_type="reply_comment",
        current_activity="正在回复评论，结合最近经历。",
        query="你不是发过雨夜散步的动态吗？钥匙呢？",
        scene="reply_comment",
        recent_limit=6,
        recall_limit=4,
    )
    recent = "\n".join(ctx.recent_self_actions)
    blob = "\n".join([ctx.prompt_text or "", recent, ctx.memory_evidence or ""])
    assert "雨夜" in blob
    assert ("钥匙" in blob) or ("青铜" in blob) or ("唱片" in blob)
