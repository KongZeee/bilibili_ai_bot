import asyncio
import hashlib
import sqlite3
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bilibot.context_builder import ContextBuilder
from bilibot.companion.service import CompanionLifeService
from bilibot.memory_brain import (
    ActivityMemoryError,
    IdempotencyConflictError,
    MemoryBrainStore,
    MemoryBrainService,
    MemoryModelGateway,
    ObservationEnvelope,
    bot_action_observation,
    normalize_entity_type,
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


@pytest.mark.asyncio
async def test_begin_activity_reuses_existing_intent_on_idempotency_conflict(tmp_path):
    """Same action_key with evolved metadata must not raise / must not block callers."""
    brain = MemoryBrainService("acc", tmp_path / "acc")
    first = await brain.begin_activity(
        action_key="companion_explore:2026-07-20",
        action_type="explore_topic",
        current_activity="正在主动探索一个感兴趣的话题。",
        query="第一轮查询 旧念头",
        scene="exploration",
        metadata={"generation_recall_query": "旧念头 A"},
    )
    assert first.intent_event_id

    second = await brain.begin_activity(
        action_key="companion_explore:2026-07-20",
        action_type="explore_topic",
        current_activity="正在主动探索一个感兴趣的话题。",
        query="第二轮查询 新念头 约会技巧",
        scene="exploration",
        metadata={"generation_recall_query": "新念头 约会技巧 会改变 content_hash"},
    )
    assert second.intent_event_id == first.intent_event_id

    finished = await brain.finish_activity(
        action_key="companion_explore:2026-07-20",
        action_type="explore_topic",
        result_text="探索完成 A",
        state="completed",
        scene="exploration",
    )
    # Retry finish with different body → soft reuse, no exception.
    finished_again = await brain.finish_activity(
        action_key="companion_explore:2026-07-20",
        action_type="explore_topic",
        result_text="探索完成 B 不同正文",
        state="completed",
        scene="exploration",
    )
    assert finished_again == finished


def test_companion_idempotency_conflict_does_not_pause_account(tmp_path):
    safety = MagicMock()
    service = CompanionLifeService.__new__(CompanionLifeService)
    service.account_id = "default"
    service.safety_checker = safety

    assert service._is_non_fatal_memory_error(
        IdempotencyConflictError("idempotency key 'x' already exists with different content")
    )
    assert service._is_non_fatal_memory_error(
        None,
        detail="activity_memory_failed:IdempotencyConflictError",
    )
    # Wrapped the way production used to surface it.
    wrapped = ActivityMemoryError("activity intent archive failed: IdempotencyConflictError")
    wrapped.__cause__ = IdempotencyConflictError("different content")
    assert service._is_non_fatal_memory_error(wrapped)

    service._pause_for_memory_failure(
        "activity_memory_failed:ActivityMemoryError",
        exc=wrapped,
    )
    safety.pause_account.assert_not_called()

    # Real storage failures still pause.
    service._pause_for_memory_failure("sqlite disk I/O error")
    safety.pause_account.assert_called_once()


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


class _ExploreConfig:
    def get_raw_config(self):
        return {
            "companion": {
                "enabled": True,
                "schedule": {"enabled": False},
                "dream": {"enabled": False},
                "diary": {"enabled": False},
                "creative": {"enabled": False},
                "exploration": {"enabled": True, "min_interval_hours": 1},
            }
        }


class _ExploreWeb:
    def is_available(self):
        return True

    def is_scene_enabled(self, _scene):
        return True

    async def search(self, _query, **_kwargs):
        return {"items": [{"title": "result", "snippet": "public material"}]}


class _SafetySpy:
    def __init__(self):
        self.pauses = []

    def pause_account(self, account_id, reason=""):
        self.pauses.append((account_id, reason))


@pytest.mark.asyncio
async def test_companion_explorations_have_unique_activity_keys(tmp_path):
    class Brain:
        def __init__(self):
            self.begin_keys = []
            self.finish_keys = []

        async def begin_activity(self, **kwargs):
            self.begin_keys.append(kwargs["action_key"])
            return SimpleNamespace(
                prompt_text="recent self memory",
                event_ids=(),
                recent_self_actions=(),
            )

        async def archive_observation_async(self, _envelope):
            return SimpleNamespace(source_committed=True)

        async def finish_activity(self, **kwargs):
            self.finish_keys.append(kwargs["action_key"])
            return "event-id"

    brain = Brain()
    safety = _SafetySpy()
    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        memory_brain=brain,
        safety_checker=safety,
        web_search=_ExploreWeb(),
    )

    first = await service.maybe_explore(force=True)
    second = await service.maybe_explore(force=True)

    assert first.id != second.id
    assert brain.begin_keys == brain.finish_keys
    assert len(set(brain.begin_keys)) == 2
    assert safety.pauses == []


@pytest.mark.asyncio
async def test_exploration_conflict_is_isolated_and_backed_off(tmp_path, monkeypatch):
    class Brain:
        async def begin_activity(self, **_kwargs):
            raise IdempotencyConflictError("different content")

    safety = _SafetySpy()
    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        memory_brain=Brain(),
        safety_checker=safety,
        web_search=_ExploreWeb(),
    )
    monkeypatch.setattr("bilibot.companion.service.random.random", lambda: 0.0)

    result = await service.tick()

    assert "error" not in result
    assert "explore_error:IdempotencyConflictError" in result["actions"]
    assert safety.pauses == []
    assert service._exploration_due() is False


@pytest.mark.asyncio
async def test_memory_enrichment_salvages_extra_json_and_normalizes_types(tmp_path):
    class Chat:
        enabled = True

        async def generate(self, _prompt, **_kwargs):
            return (
                '[{"name":"ATRI","type":"Person / Character"}]\n'
                '[{"name":"duplicate"}]'
            )

    gateway = MemoryModelGateway(Chat(), None)
    entities = await gateway.extract_entities(
        {"sources": [{"full_text": "ATRI 在海边。"}]}
    )
    assert entities == [{"name": "ATRI", "type": "Person / Character"}]
    assert normalize_entity_type(entities[0]["type"]) == "character"

    db_path = tmp_path / "brain.db"
    store = MemoryBrainStore(db_path, account_id="acc")
    archived = store.archive_observation(
        ObservationEnvelope(
            idempotency_key="entity:1",
            account_id="acc",
            source_type="text",
            source_text="ATRI 在海边。",
            job_types=(),
        )
    )
    store.upsert_entities(archived.event_id, entities)
    assert store.get_event(archived.event_id)["entities"][0]["entity_type"] == "character"

    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE memory_entities SET entity_type='PERSON/CHANNEL'")
    conn.execute("DELETE FROM brain_info WHERE key='entity_type_taxonomy_version'")
    conn.commit()
    conn.close()
    reopened = MemoryBrainStore(db_path, account_id="acc")
    assert reopened.get_event(archived.event_id)["entities"][0]["entity_type"] == "person"


def test_memory_operations_trace_dto_and_bounded_dead_letter_recovery(tmp_path):
    store = MemoryBrainStore(tmp_path / "brain.db", account_id="acc")
    canonical_body = "09:00 到 11:30 处理日常事务，偶尔摸鱼。"
    archived = store.archive_observation(
        ObservationEnvelope(
            idempotency_key="ops:event",
            account_id="acc",
            source_type="life_plan",
            source_text=canonical_body,
            event_type="life_detail",
            event_title="上午安排",
            metadata={
                "action_state": "completed",
                "canonical_sha256": hashlib.sha256(canonical_body.encode("utf-8")).hexdigest(),
                "canonical_chars": len(canonical_body),
            },
            job_types=("summarize_event",),
        )
    )
    store.save_recall_trace(
        query_hash="query-hash",
        used_fallback=True,
        latency_ms=123.0,
        prompt_chars=456,
        candidates=[{"event_id": archived.event_id, "accepted": True, "injected": True}],
    )

    trace = store.get_recall_trace(store.list_recall_traces(limit=1)[0]["id"])
    assert trace["candidates"][0]["title"] == "上午安排"
    assert trace["candidates"][0]["event_type"] == "life_detail"
    assert trace["candidates"][0]["index_status"]

    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE brain_jobs SET status='dead', attempts=max_attempts")
    conn.commit()
    conn.close()
    assert store.retry_dead_letters(limit=1) == 1
    assert store.list_jobs(status="pending", limit=10)

    operations = store.stats()["operations"]
    assert operations["events_last_30m"] == 1
    assert operations["recall_last_30m"]["fallback_rate"] == 1.0
    assert operations["recall_last_30m"]["latency_ms_p50"] == 123.0
    assert operations["recall_last_30m"]["prompt_chars_p50"] == 456
    assert operations["canonical_documents"]["checked"] == 1
    assert operations["canonical_documents"]["verified"] == 1
    assert operations["canonical_documents"]["mismatch"] == 0


def test_dead_letter_report_and_event_scoped_replay(tmp_path):
    store = MemoryBrainStore(tmp_path / "brain.db", account_id="acc")
    first = store.archive_observation(
        ObservationEnvelope(
            idempotency_key="dead:first",
            account_id="acc",
            source_type="text",
            source_text="first",
            job_types=("summarize_event",),
        )
    )
    second = store.archive_observation(
        ObservationEnvelope(
            idempotency_key="dead:second",
            account_id="acc",
            source_type="text",
            source_text="second",
            job_types=("link_associations",),
        )
    )
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE brain_jobs SET status='dead',attempts=max_attempts,last_error=? WHERE event_id=?",
        ("TimeoutError: summarize timed out", first.event_id),
    )
    conn.execute(
        "UPDATE brain_jobs SET status='dead',attempts=max_attempts,last_error=? WHERE event_id=?",
        ("ValueError: invalid association", second.event_id),
    )
    conn.commit()
    conn.close()

    report = store.dead_letter_report(limit=10)

    assert report["total"] == 2
    assert report["affected_event_count"] == 2
    assert report["by_status"] == {"dead": 2}
    assert report["by_job_type"] == {
        "link_associations": 1,
        "summarize_event": 1,
    }
    assert report["by_error"] == {"TimeoutError": 1, "ValueError": 1}
    assert store.retry_dead_letters(event_id=first.event_id, limit=10) == 1
    assert len(store.list_jobs(status="pending")) == 1
    remaining = store.dead_letter_report(limit=10)
    assert remaining["total"] == 1
    assert remaining["samples"][0]["event_id"] == second.event_id


def test_operations_report_recall_p95_timeout_error_and_job_causes(tmp_path):
    store = MemoryBrainStore(tmp_path / "brain.db", account_id="acc")
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="ops:failure",
            account_id="acc",
            source_type="text",
            source_text="failure",
            job_types=("extract_entities",),
        )
    )
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE brain_jobs SET status='retry',last_error='ProviderError: unavailable'"
    )
    conn.commit()
    conn.close()
    for index, (latency, status, errors) in enumerate(
        (
            (10.0, "ok", {}),
            (20.0, "timeout", {}),
            (200.0, "error:RuntimeError", {"main_embedding": "TimeoutError"}),
        )
    ):
        store.save_recall_trace(
            query_hash=f"query-{index}",
            rerank_status=status,
            channel_errors=errors,
            latency_ms=latency,
        )

    operations = store.stats()["operations"]
    recall = operations["recall_last_30m"]
    assert recall["latency_ms_p95"] == 200.0
    assert recall["latency_ms_max"] == 200.0
    assert recall["timeout_count"] == 2
    assert recall["timeout_rate"] == pytest.approx(2 / 3)
    assert recall["error_count"] == 1
    assert recall["rerank_statuses"] == {
        "error:RuntimeError": 1,
        "ok": 1,
        "timeout": 1,
    }
    assert operations["active_jobs_by_type"] == {"extract_entities": 1}
    assert operations["job_failures_by_type"] == {
        "extract_entities": {"ProviderError": 1}
    }


def test_memory_list_can_hide_open_intents_without_hiding_outcomes(tmp_path):
    store = MemoryBrainStore(tmp_path / "brain.db", account_id="acc")
    for state in ("intent", "completed"):
        store.archive_observation(
            bot_action_observation(
                account_id="acc",
                action_key=f"state:{state}",
                action_type="test_action",
                text=state,
                published=state == "completed",
                state=state,
            )
        )
    visible = store.list_events(limit=10, exclude_intents=True)
    assert [item["metadata"]["action_state"] for item in visible] == ["completed"]
    assert store.count_events(exclude_intents=True) == 1


@pytest.mark.asyncio
async def test_failed_video_archives_metadata_only_evidence_and_failed_outcome(tmp_path):
    from bilibot.scheduler import Scheduler

    brain = MemoryBrainService("acc", tmp_path / "acc")

    class CompanionSpy:
        def __init__(self):
            self.failures = []

        def on_proactive_video_failed(self, **kwargs):
            self.failures.append(kwargs)

    scheduler = Scheduler.__new__(Scheduler)
    scheduler.account_id = "acc"
    scheduler.memory_brain = brain
    scheduler._memory_brain_required = True
    scheduler.persona_store = None
    scheduler.companion = CompanionSpy()

    outcome_id = await scheduler._archive_proactive_video_failure(
        bvid="BV1FAILED001",
        oid="123",
        title="失败样本",
        owner="测试UP",
        reason="ASR_API_REQUEST_FAILED",
        task_id="task-1",
        partial_evidence="音轨里已经识别到一句有效台词",
    )

    events = brain.list_events(limit=10)
    by_type = {event["event_type"]: event for event in events}
    metadata_event = by_type["video_metadata_observation"]
    outcome = by_type["action_outcome"]
    assert outcome["id"] == outcome_id
    assert metadata_event["metadata"]["metadata_only"] is True
    assert metadata_event["metadata"]["watched"] is False
    assert metadata_event["metadata"]["partial_evidence"] is True
    assert metadata_event["metadata"]["watch_state"] == "attempted_not_completed"
    assert metadata_event["index_status"] == "degraded"
    assert outcome["metadata"]["failure_observation_event_id"] == metadata_event["id"]
    assert outcome["metadata"]["action_state"] == "failed"
    assert outcome["index_status"] == "ready"
    assert "音轨里已经识别到一句有效台词" in brain.get_event(metadata_event["id"])["sources"][0]["full_text"]
    assert brain.store.list_jobs(limit=10) == []
    assert any(
        link["relation_type"] == "is_about"
        and link["target_event_id"] == metadata_event["id"]
        for link in brain.get_event(outcome_id)["links"]
    )
    assert scheduler.companion.failures[0]["memory_event_id"] == outcome_id


def test_companion_night_rhythm_drains_awake_state_and_recovers_sleep(tmp_path):
    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        memory_brain=None,
    )
    state = service.ensure_life_state()
    state.energy = 50
    state.activity = "正在看视频"
    service.store.save_life_state(state)

    night = datetime.now().replace(hour=1, minute=0, second=0, microsecond=0)
    service.store.patch_runtime(last_life_rhythm_ts=night.timestamp() - 1800)
    awake = service._refresh_life_rhythm(night)
    assert awake.energy == 49
    assert awake.mood_bias == "困倦"
    assert service.rank_motives(night).top().suggested_action == "rest"

    awake.activity = "已经躺下睡觉"
    service.store.save_life_state(awake)
    later = night + timedelta(minutes=31)
    service.store.patch_runtime(last_life_rhythm_ts=later.timestamp() - 1800)
    asleep = service._refresh_life_rhythm(later)
    assert asleep.energy == 50


def test_companion_successful_watch_still_costs_energy_and_sets_cooldown(tmp_path):
    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        memory_brain=None,
    )
    state = service.ensure_life_state()
    state.energy = 60
    service.store.save_life_state(state)
    service.on_proactive_video_started(title="测试视频", bvid="BV1TEST00001")
    service.on_proactive_video_finished(
        title="测试视频", score=9, bvid="BV1TEST00001"
    )
    assert service.store.get_life_state().energy == 58
    assert float(service.store.get_runtime()["browse_cooldown_until"]) > time.time()


def test_fallback_plan_keeps_sleep_and_uses_self_snapshot():
    from bilibot.companion.service import _fallback_plan_items

    items = _fallback_plan_items(
        8,
        interests=["机器人动画"],
        energy=52,
        weekday="周一",
        self_cues=["刚看了《测试视频》|eid=evt_1"],
        ongoing_threads=["最近在看：《测试视频》|eid=evt_1"],
    )

    assert len(items) == 8
    assert items[-1].activity == "睡眠"
    assert items[-1].time == "23:30"
    assert items[-1].end == "07:00"
    assert all(item.basis == "self_snapshot" for item in items)
    assert any("测试视频" in item.activity or "测试视频" in item.message_seed for item in items)


def test_dynamic_image_anime_style_is_available_and_applied():
    from bilibot.image.styles import apply_image_style, image_style_options

    options = {item["value"]: item for item in image_style_options()}
    assert options["anime"]["label"] == "二次元"
    prompt = apply_image_style("A girl reading beside a window", style="anime")
    assert "Japanese anime illustration" in prompt
    assert "no text, no watermark" in prompt


def test_dynamic_image_custom_style_and_empty_custom_fallback():
    from bilibot.image.styles import (
        DEFAULT_IMAGE_STYLE,
        apply_image_style,
        resolve_image_style,
    )

    custom = apply_image_style(
        "A quiet mountain village",
        style="custom",
        custom_style="国风工笔画，淡雅矿物色，宣纸纹理",
    )
    assert "国风工笔画" in custom

    key, label, instruction = resolve_image_style("custom", "")
    assert key == DEFAULT_IMAGE_STYLE
    assert label == "电影质感"
    assert "cinematic composition" in instruction


@pytest.mark.asyncio
async def test_dynamic_prompt_enforces_selected_image_style_after_llm_generation():
    from bilibot.scheduler import Scheduler

    scheduler = Scheduler.__new__(Scheduler)
    scheduler.llm = MagicMock()
    scheduler.llm.generate = AsyncMock(return_value="A girl reading beside a window")
    scheduler.persona_store = None
    scheduler.account_id = "style-account"
    scheduler.config_loader = MagicMock()
    scheduler.config_loader.get_raw_config.return_value = {
        "dynamic_publish": {
            "image_style": "anime",
            "image_style_custom": "",
        }
    }

    result = await scheduler._generate_image_prompt("午后在窗边看书")

    assert result is not None
    assert "Japanese anime illustration" in result
    llm_prompt = scheduler.llm.generate.await_args.args[0]
    assert "二次元（anime）" in llm_prompt
    assert "Japanese anime illustration" in llm_prompt


def test_image_style_api_persists_selection_and_rejects_invalid_values(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from bilibot.api.image_generation import create_image_generation_routes
    from bilibot.app.config_loader import ConfigLoader

    loader = ConfigLoader(
        config_dict={
            "config_revision": 0,
            "dynamic_publish": {"with_image": False},
            "data_dir": str(tmp_path),
        }
    )
    app = Starlette(
        routes=create_image_generation_routes(loader, str(tmp_path / "config.yaml"))
    )

    with TestClient(app) as client:
        saved = client.patch(
            "/api/image-generation",
            json={
                "image_style": "anime",
                "image_style_custom": "",
            },
        )
        assert saved.status_code == 200
        payload = saved.json()["data"]
        assert payload["image_style"] == "anime"
        assert any(
            item["value"] == "anime" and item["label"] == "二次元"
            for item in payload["image_style_options"]
        )

        unknown = client.patch(
            "/api/image-generation", json={"image_style": "unknown-style"}
        )
        assert unknown.status_code == 400

        empty_custom = client.patch(
            "/api/image-generation",
            json={"image_style": "custom", "image_style_custom": ""},
        )
        assert empty_custom.status_code == 400


def test_memory_runtime_budget_defaults_and_custom_values():
    from bilibot.app.config_loader import ConfigLoader

    defaults = ConfigLoader(config_dict={}).memory
    assert defaults.recall_candidate_limit == 12
    assert defaults.rerank_timeout_seconds == 8.0
    assert defaults.recall_total_timeout_seconds == 10.0
    assert defaults.enrichment_chat_timeout_seconds == 12.0
    assert defaults.link_candidate_limit == 12
    assert defaults.link_job_max_attempts == 3

    custom = ConfigLoader(
        config_dict={
            "memory": {
                "rerank_timeout_seconds": 4,
                "recall_total_timeout_seconds": 6,
                "enrichment_chat_timeout_seconds": 5,
                "link_candidate_limit": 8,
                "link_job_max_attempts": 2,
                "job_max_attempts": 4,
            }
        }
    ).memory
    assert custom.rerank_timeout_seconds == 4
    assert custom.recall_total_timeout_seconds == 6
    assert custom.enrichment_chat_timeout_seconds == 5
    assert custom.link_candidate_limit == 8
    assert custom.link_job_max_attempts == 2


@pytest.mark.asyncio
async def test_recall_total_budget_caps_slow_embedding(tmp_path):
    from bilibot.memory_brain.recall import RecallEngine, RecallQuery

    store = MemoryBrainStore(tmp_path / "budget.db", account_id="acc")
    archived = store.archive_observation(
        ObservationEnvelope(
            idempotency_key="budget:event",
            account_id="acc",
            source_type="text",
            source_text="预算测试记忆",
            event_title="预算测试",
            job_types=(),
        )
    )

    class SlowModels:
        async def get_embedding(self, _text):
            await asyncio.sleep(0.2)
            return [1.0, 0.0]

        async def generate(self, **_kwargs):
            return "{}"

    started = time.perf_counter()
    result = await RecallEngine(
        store,
        SlowModels(),
        rerank_timeout=0.03,
        total_timeout=0.03,
    ).recall(
        RecallQuery(
            current_message=archived.event_id,
            account_id="acc",
            explicit_ids=(archived.event_id,),
        )
    )

    assert time.perf_counter() - started < 0.12
    assert result.trace.channel_errors["main_embedding"] == "TimeoutError"
    assert result.trace.rerank_status == "total_timeout"
    assert result.trace.mode == "fallback"
    assert result.events[0]["id"] == archived.event_id


def test_worker_priority_and_independent_throttle_reasons(tmp_path):
    from bilibot.memory_brain import PersistentMemoryWorker

    store = MemoryBrainStore(tmp_path / "worker.db", account_id="acc")
    store.archive_observation(
        ObservationEnvelope(
            idempotency_key="priority:event",
            account_id="acc",
            source_type="text",
            source_text="优先级测试",
            job_types=(
                "link_associations",
                "extract_entities",
                "summarize_event",
                "embed_chunks",
                "embed_event",
            ),
        )
    )
    jobs = store.claim_jobs("priority-worker", limit=5, lease_seconds=60)
    assert [job.job_type for job in jobs] == [
        "embed_event",
        "embed_chunks",
        "summarize_event",
        "extract_entities",
        "link_associations",
    ]

    worker = PersistentMemoryWorker(store, MemoryModelGateway(None, None))
    worker.set_throttled(True, reason="video_understanding")
    worker.set_throttled(True, reason="interactive_recall")
    worker.set_throttled(False, reason="interactive_recall")
    status = worker.runtime_status()
    assert status["throttle_reasons"] == ["video_understanding"]
    assert "summarize_event" not in worker._claimable_job_types()
    assert "embed_event" in worker._claimable_job_types()


def test_day_roll_expires_old_afterglow_and_transient_watch_thread(tmp_path):
    from bilibot.companion.models import DreamRecord

    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        memory_brain=None,
    )
    state = service.store.get_life_state()
    state.date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    state.energy = 55
    state.ongoing_threads = [
        "最近在看：《旧视频》",
        "兴趣：仿生人",
        "小说：海边故事",
        "临时念头一",
        "临时念头二",
        "临时念头三",
    ]
    service.store.save_life_state(state)
    service.store.patch_runtime(
        ongoing_thread_timestamps={
            "最近在看：《旧视频》": time.time() - 3 * 86400,
            "兴趣：仿生人": time.time() - 2 * 3600,
            "小说：海边故事": time.time() - 10 * 86400,
        }
    )
    service.store.save_latest_dream(
        DreamRecord(
            date=(datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
            afterglow="不应跨多日残留",
            mood="恍惚",
            energy_delta=-5,
        )
    )

    rolled = service.ensure_life_state()

    assert rolled.dream_afterglow == ""
    assert not any(item.startswith("最近在看：") for item in rolled.ongoing_threads)
    assert "小说：海边故事" in rolled.ongoing_threads
    assert "兴趣：仿生人" in rolled.ongoing_threads
    assert len(rolled.ongoing_threads) <= 4


def test_partial_llm_plan_is_merged_into_full_fallback_schedule():
    from bilibot.companion.models import PlanItem
    from bilibot.companion.service import _fallback_plan_items, _merge_partial_plan_items

    fallback = _fallback_plan_items(8, interests=["动画"], weekday="周一")
    merged = _merge_partial_plan_items(
        fallback,
        [
            PlanItem(
                time="19:20",
                end="20:00",
                activity="把刚看的海边故事画成小草图",
                mood="期待",
            )
        ],
    )

    evening = next(item for item in merged if item.time == "19:00")
    assert evening.activity == "把刚看的海边故事画成小草图"
    assert evening.basis == "llm_partial"
    assert merged[-1].activity == "睡眠"


@pytest.mark.asyncio
async def test_explore_fallback_internalizes_concrete_search_result(tmp_path):
    class Brain:
        async def begin_activity(self, **_kwargs):
            return SimpleNamespace(prompt_text="", event_ids=(), recent_self_actions=())

        async def archive_observation_async(self, _envelope):
            return SimpleNamespace(source_committed=True)

        async def finish_activity(self, **_kwargs):
            return "event-explore"

    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        llm=None,
        memory_brain=Brain(),
        web_search=_ExploreWeb(),
    )

    note = await service.maybe_explore(force=True)

    assert note is not None
    assert note.impression != "看了一些资料。"
    assert "public material" in note.impression


@pytest.mark.asyncio
async def test_story_detail_fallback_contains_concrete_event(tmp_path):
    from bilibot.companion.models import DailyPlan, PlanItem

    class ScheduleConfig:
        def get_raw_config(self):
            return {
                "companion": {
                    "enabled": True,
                    "schedule": {"enabled": True, "detail_lead_minutes": 0},
                    "dream": {"enabled": False},
                    "diary": {"enabled": False},
                    "exploration": {"enabled": False},
                    "creative": {"enabled": False},
                }
            }

    class Brain:
        async def begin_activity(self, **_kwargs):
            return SimpleNamespace(prompt_text="", event_ids=(), recent_self_actions=())

        async def archive_observation_async(self, _envelope):
            return SimpleNamespace(source_committed=True)

        async def finish_activity(self, **_kwargs):
            return "event-detail"

    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=ScheduleConfig(),
        llm=None,
        memory_brain=Brain(),
    )
    service.store.save_daily_plan(
        DailyPlan(
            date=datetime.now().strftime("%Y-%m-%d"),
            source="fallback",
            items=[PlanItem(time="00:00", end="23:59", activity="整理今天的见闻")],
        )
    )

    detail = await service.ensure_detail_enhancement()

    assert detail is not None
    assert detail.events
    assert "整理今天的见闻" in detail.events[0]


@pytest.mark.asyncio
async def test_nav_retries_then_uses_last_successful_cache():
    from bilibot.bilibili_api import BilibiliAPI

    api = object.__new__(BilibiliAPI)
    success = {"code": 0, "data": {"mid": 42, "uname": "测试账号"}}
    api.get_nav_status = AsyncMock(side_effect=[RuntimeError("temporary"), success])

    first = await api.get_nav(max_attempts=2, retry_delay_seconds=0)
    assert first == success
    assert api.get_nav_status.await_count == 2

    api.get_nav_status = AsyncMock(side_effect=RuntimeError("still down"))
    cached = await api.get_nav(max_attempts=1, retry_delay_seconds=0)
    assert cached == success


def test_completed_asr_is_rendered_as_partial_evidence_when_visual_fails():
    from bilibot.video_understanding.audio_track import ASRTranscriptionResult, AudioEvent
    from bilibot.video_understanding.service import _partial_audio_behavior_log

    result = ASRTranscriptionResult(
        [AudioEvent(start=1.0, end=2.0, text="已经识别到的有效台词", source="asr")],
        "ok",
    )

    log = _partial_audio_behavior_log(result, 10.0)

    assert "已经识别到的有效台词" in log
    assert "【听到声音】" in log


@pytest.mark.asyncio
async def test_companion_archive_failure_uses_subsystem_cooldown_not_account_pause(tmp_path):
    class RejectingBrain:
        async def archive_observation_async(self, _envelope):
            return SimpleNamespace(source_committed=False)

    safety = _SafetySpy()
    service = CompanionLifeService(
        "acc",
        str(tmp_path),
        config_loader=_ExploreConfig(),
        memory_brain=RejectingBrain(),
        safety_checker=safety,
    )

    committed = await service._archive_text(
        source_type="diary",
        event_type="diary",
        text="这次归档会失败",
        title="失败样本",
        idempotency_key="companion:soft-failure",
        pause_on_error=False,
    )

    runtime = service.store.get_runtime()
    assert committed is False
    assert safety.pauses == []
    assert runtime["archive_fail_count"] == 1
    assert runtime["companion_memory_cooldown_until"] > time.time()
