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
