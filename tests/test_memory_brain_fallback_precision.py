from __future__ import annotations

import pytest

from bilibot.memory_brain.models import ObservationEnvelope
from bilibot.memory_brain.recall import RecallEngine, RecallQuery
from bilibot.memory_brain.store import MemoryBrainStore


@pytest.fixture
def seeded_store(tmp_path):
    store = MemoryBrainStore(tmp_path / "acc" / "memory_brain.db", account_id="acc")
    rows = (
        (
            "video:bronze",
            "subtitle",
            "BV1BRONZE0001",
            "旧物收藏室探访",
            "视频中段明确说，青铜钥匙放在旧唱片机下面。片尾提到豆豆。",
        ),
        (
            "video:noise",
            "subtitle",
            "BV1NOISE00001",
            "陪伴我10年的员工离开了....",
            "字幕里有人喊：TM现在几点啊？！ 还有今天怎么样的闲聊。",
        ),
        (
            "bot:rain",
            "bot_action",
            "dyn-rain",
            "雨夜散步动态",
            "发了一条关于雨夜散步的动态，提到路灯和潮湿的石板路。",
        ),
    )
    ids = {}
    for key, source_type, external_id, title, text in rows:
        archived = store.archive_observation(
            ObservationEnvelope(
                idempotency_key=key,
                account_id="acc",
                source_type=source_type,
                source_external_id=external_id,
                source_text=text,
                event_title=title,
                job_types=(),
            )
        )
        ids[key] = archived.event_id
    return store, ids


@pytest.mark.asyncio
async def test_utility_queries_fail_closed(seeded_store):
    store, _ids = seeded_store
    engine = RecallEngine(store)
    for q in (
        "今天的天气预报和午饭建议是什么？",
        "帮我算一下 17*19 等于多少？",
        "现在几点了？",
        "今天怎么样",
        "今天怎么样 还好吗 在吗",
    ):
        result = await engine.recall(
            RecallQuery(current_message=q, account_id="acc", scene="reply_comment")
        )
        assert result.is_empty, q
        assert not (result.prompt_evidence or "").strip(), q


@pytest.mark.asyncio
async def test_distinctive_content_still_recalls(seeded_store):
    store, ids = seeded_store
    engine = RecallEngine(store)
    hit = await engine.recall(
        RecallQuery(
            current_message="青铜钥匙放在旧唱片机下面吗？",
            account_id="acc",
            scene="reply_comment",
        )
    )
    assert not hit.is_empty
    assert ids["video:bronze"] in [e["id"] for e in hit.events] or "青铜钥匙" in (
        hit.prompt_evidence or ""
    )

    dyn = await engine.recall(
        RecallQuery(
            current_message="你不是发过雨夜散步的动态吗？",
            account_id="acc",
            scene="reply_comment",
        )
    )
    assert not dyn.is_empty
    assert "雨夜" in (dyn.prompt_evidence or "") or ids["bot:rain"] in [
        e["id"] for e in dyn.events
    ]


@pytest.mark.asyncio
async def test_multi_term_diary_title_rescue(seeded_store):
    store, ids = seeded_store
    archived = store.archive_observation(
        ObservationEnvelope(
            idempotency_key="diary:calm",
            account_id="acc",
            source_type="diary",
            source_external_id="diary-1",
            source_text="心情日记里写下了平静的一天。",
            event_title="日记 2026-07-16",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="心情日记", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    event_ids = [e.get("id") for e in result.events if isinstance(e, dict)]
    titles = [e.get("title") for e in result.events if isinstance(e, dict)]
    assert archived.event_id in event_ids or any("日记" in (t or "") for t in titles)


@pytest.mark.asyncio
async def test_episode_ordinal_does_not_outrank_title(seeded_store):
    store, _ids = seeded_store
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="noise-ep",
            account_id="acc",
            source_type="subtitle",
            source_external_id="n1",
            source_text="这是第二集的预告片，完全没提汤类内容。",
            event_title="陪伴我10年的员工离开了....",
            job_types=(),
        )
    )
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="hg-ep",
            account_id="acc",
            source_type="video_experience",
            source_external_id="hg1",
            source_text="观看了海龟汤（2）。",
            event_title="海龟汤（2）",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="海龟汤第二集讲了啥", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    titles = [e.get("title") for e in result.events if isinstance(e, dict)]
    assert any("海龟汤" in (t or "") for t in titles)
    # Ordinal-only noise title should not be the sole winner.
    assert not (len(titles) == 1 and "陪伴" in (titles[0] or ""))



@pytest.mark.asyncio
async def test_atri_paraphrase_prefers_entity_title(seeded_store):
    store, _ids = seeded_store
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="atri-explore",
            account_id="acc",
            source_type="web_reference",
            source_external_id="atri-1",
            source_text="探索 ATRI -My Dear Moments- 亚托莉 夏生 海边场景 视觉小说资料。",
            event_title="探索 ATRI -My Dear Moments- 亚托莉 夏生 海边场景 视觉小说",
            job_types=(),
        )
    )
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="noise-90",
            account_id="acc",
            source_type="video",
            source_external_id="v90",
            source_text="90后这辈子第一次接受到的鼓励式教育，和这部番无关。",
            event_title="90后这辈子第一次接受到的鼓励式教育",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="追的那部 ATRI 怎么样了", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    titles = [e.get("title") or "" for e in result.events if isinstance(e, dict)]
    assert any("ATRI" in t for t in titles)
    assert not any("90后" in t for t in titles)


@pytest.mark.asyncio
async def test_self_dynamic_query_prefers_bot_action(seeded_store):
    store, _ids = seeded_store
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="dyn-haland",
            account_id="acc",
            source_type="bot_action",
            source_external_id="dyn-1",
            source_text="亚托莉发布了动态，提到哈兰德表情包和无限暖暖 PV。",
            event_title="动态",
            job_types=(),
        )
    )
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="vid-kitchen",
            account_id="acc",
            source_type="video",
            source_external_id="vk",
            source_text="【迪奥の厨房】复刻临榆炸鸡腿成功，皮脆肉嫩。",
            event_title="【迪奥の厨房】复刻临榆炸鸡腿成功，皮脆肉嫩，香到停止思考！",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="你上次发的动态说了什么", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    types = [e.get("source_type") for e in result.events if isinstance(e, dict)]
    titles = [e.get("title") or "" for e in result.events if isinstance(e, dict)]
    assert "bot_action" in types or any(t == "动态" for t in titles)
    assert not any("迪奥" in t for t in titles)



@pytest.mark.asyncio
async def test_open_watch_query_prefers_recent_watch(seeded_store):
    store, _ids = seeded_store
    # Older topical videos that match 视频/看 tokens.
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="old-cheese",
            account_id="acc",
            source_type="video_experience",
            source_external_id="old1",
            source_text="看完这期视频你就懂了全世界将近2000种奶酪。",
            event_title="全世界将近2000种奶酪，到底都有什么区别？看完这期视频你就懂了！",
            job_types=(),
        )
    )
    # Fresh watch outcome.
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="new-rick",
            account_id="acc",
            source_type="bot_action",
            source_external_id="rick1",
            source_text="看完 Never Gonna Give You Up，评分9。",
            event_title="【官方 MV】Never Gonna Give You Up - Rick Astley",
            job_types=(),
        )
    )
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="new-rick-video",
            account_id="acc",
            source_type="video",
            source_external_id="rickv",
            source_text="Never Gonna Give You Up official MV by Rick Astley.",
            event_title="【官方 MV】Never Gonna Give You Up - Rick Astley",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="你刚看了什么视频", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    titles = [e.get("title") or "" for e in result.events if isinstance(e, dict)]
    assert any("Never Gonna" in t or "Rick" in t for t in titles)



@pytest.mark.asyncio
async def test_schedule_and_weekly_self_queries(seeded_store):
    store, _ids = seeded_store
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="life-1",
            account_id="acc",
            source_type="life_plan",
            source_external_id="lp1",
            source_text="今天上午阅读，下午制作日程安排。",
            event_title="日程 2026-07-16",
            job_types=(),
        )
    )
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="week-1",
            account_id="acc",
            source_type="weekly_summary",
            source_external_id="w1",
            source_text="本周总结：看了视频，发了动态，心情不错。",
            event_title="周总结 2026-W29",
            job_types=(),
        )
    )
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="noise-sum",
            account_id="acc",
            source_type="comment",
            source_external_id="csum",
            source_text="空泽同学 评论：@亚托莉小姐 总结一下这个视频",
            event_title="空泽同学 评论：@亚托莉小姐 总结一下这个视频",
            job_types=(),
        )
    )
    eng = RecallEngine(store)
    sched = await eng.recall(
        RecallQuery(current_message="你的日程安排", account_id="acc", scene="reply_comment")
    )
    assert not sched.is_empty
    assert any("日程" in (e.get("title") or "") for e in sched.events if isinstance(e, dict))
    week = await eng.recall(
        RecallQuery(current_message="周总结写了啥", account_id="acc", scene="reply_comment")
    )
    assert not week.is_empty
    titles = [e.get("title") or "" for e in week.events if isinstance(e, dict)]
    assert any("周总结" in t for t in titles)
    assert not any("总结一下这个视频" in t for t in titles)


@pytest.mark.asyncio
async def test_exact_dream_title_not_polluted(seeded_store):
    store, _ids = seeded_store
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="dream-1",
            account_id="acc",
            source_type="dream",
            source_external_id="d1",
            source_text="梦见窗边的午后，夏生在看书。",
            event_title="窗边的午后",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="窗边的午后", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    titles = [e.get("title") or "" for e in result.events if isinstance(e, dict)]
    assert titles == ["窗边的午后"] or titles[0] == "窗边的午后"



@pytest.mark.asyncio
async def test_open_bangumi_query_seeds_atri(seeded_store):
    store, _ids = seeded_store
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="atri-vn",
            account_id="acc",
            source_type="web_reference",
            source_external_id="atri-vn",
            source_text="探索 ATRI -My Dear Moments- 视觉小说 亚托莉 夏生 海边。",
            event_title="探索 ATRI -My Dear Moments- 亚托莉 夏生 海边场景 视觉小说",
            job_types=(),
        )
    )
    result = await RecallEngine(store).recall(
        RecallQuery(current_message="你最近在追什么番", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    titles = [e.get("title") or "" for e in result.events if isinstance(e, dict)]
    assert any("ATRI" in t for t in titles)



@pytest.mark.asyncio
async def test_open_watch_excludes_bangumi_episode(tmp_path):
    from bilibot.memory_brain import MemoryBrainService

    brain = MemoryBrainService("acc", tmp_path / "acc")
    await brain.begin_activity(
        action_key="bg:1",
        action_type="evaluate_bangumi_episode",
        current_activity="观看番剧",
        query="星际驿站",
        scene="bangumi",
        title="星际驿站 第3话",
    )
    await brain.finish_activity(
        action_key="bg:1",
        action_type="evaluate_bangumi_episode",
        result_text="看完《星际驿站》第3话，评分8，想继续追。",
        state="completed",
        scene="bangumi",
        title="星际驿站 第3话",
    )
    await brain.begin_activity(
        action_key="v:1",
        action_type="evaluate_proactive_video",
        current_activity="观看视频",
        query="Never Gonna",
        scene="proactive_video",
        title="【官方 MV】Never Gonna Give You Up - Rick Astley",
    )
    await brain.finish_activity(
        action_key="v:1",
        action_type="evaluate_proactive_video",
        result_text="看完 Never Gonna Give You Up，评分9。",
        state="completed",
        scene="proactive_video",
        title="【官方 MV】Never Gonna Give You Up - Rick Astley",
    )
    result = await brain.recall(
        RecallQuery(current_message="你刚看了什么视频", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    titles = [e.get("title") or "" for e in result.events if isinstance(e, dict)]
    assert any("Never Gonna" in t or "Rick" in t for t in titles)
    assert not any("第3话" in t or "星际驿站" in t for t in titles)



@pytest.mark.asyncio
async def test_completed_activity_hides_intent_duplicate(tmp_path):
    from bilibot.memory_brain import MemoryBrainService

    brain = MemoryBrainService("acc", tmp_path / "acc")
    await brain.begin_activity(
        action_key="v:dup",
        action_type="evaluate_proactive_video",
        current_activity="观看 Never Gonna",
        query="Never Gonna",
        scene="proactive_video",
        title="【官方 MV】Never Gonna Give You Up - Rick Astley",
    )
    await brain.finish_activity(
        action_key="v:dup",
        action_type="evaluate_proactive_video",
        result_text="看完 Never Gonna Give You Up，评分9。",
        state="completed",
        scene="proactive_video",
        title="【官方 MV】Never Gonna Give You Up - Rick Astley",
    )
    result = await brain.recall(
        RecallQuery(current_message="Never Gonna", account_id="acc", scene="reply_comment")
    )
    assert not result.is_empty
    states = []
    for e in result.events:
        if not isinstance(e, dict):
            continue
        meta = e.get("metadata") or {}
        if isinstance(meta, dict):
            states.append(str(meta.get("action_state") or ""))
    assert "intent" not in states
    assert any(s == "completed" for s in states) or any(
        "评分9" in str((e.get("summary") if isinstance(e, dict) else "") or "")
        for e in result.events
    )
