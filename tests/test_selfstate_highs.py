"""Unit smoke for SelfState HIGH fixes (PM privacy, threads, day-roll)."""

from __future__ import annotations

from datetime import datetime

from bilibot.companion import service as service_mod
from bilibot.companion.models import LifeState
from bilibot.companion.service import CompanionLifeService


class _Cfg:
    def get_raw_config(self):
        return {"companion": {"enabled": True}}


def _svc(tmp_path) -> CompanionLifeService:
    return CompanionLifeService(
        account_id="test_acc",
        account_data_dir=str(tmp_path),
        config_loader=_Cfg(),
    )


def test_pm_replied_does_not_leak_body_into_prompt_surface(tmp_path):
    svc = _svc(tmp_path)
    secret = "私密内容请勿外泄 phone 13800138000"
    svc.on_private_message_replied(preview=secret, actor_label="网友甲")

    state = svc.store.get_life_state()
    assert secret not in (state.message_seed or "")
    assert state.message_seed == "刚回了私信"
    for item in state.salient_recent or []:
        assert secret not in item
        assert "回了私信" in item
        assert "网友甲" not in (item or "")

    surface = svc.get_prompt_surface()
    assert secret not in surface
    assert "phone" not in surface
    assert "网友甲" not in surface
    # Body and actor may live only on non-injected runtime.
    runtime = svc.store.get_runtime()
    assert secret[:20] in (runtime.get("last_private_message_preview") or "")
    assert runtime.get("last_private_message_actor") == "网友甲"


def test_stale_browse_session_count_decays_and_does_not_block_motives(tmp_path):
    svc = _svc(tmp_path)
    now = datetime(2026, 8, 16, 15, 0, 0)
    svc.store.patch_runtime(
        browse_session_count=4,
        last_proactive_video_ts=now.timestamp() - 3 * 3600,
    )
    queue = svc.rank_motives(now)
    top = queue.top()
    # 3 小时前的会话不应继续把 rest 顶到 7 分以上
    assert top is not None
    assert not (top.suggested_action == "rest" and top.score >= 7.0)


def test_fresh_browse_session_count_still_raises_rest(tmp_path):
    svc = _svc(tmp_path)
    now = datetime(2026, 8, 16, 15, 0, 0)
    svc.store.patch_runtime(
        browse_session_count=4,
        last_proactive_video_ts=now.timestamp() - 60,
    )
    queue = svc.rank_motives(now)
    assert queue.top().suggested_action == "rest"
    assert queue.top().score >= 7.0


def test_thread_category_replace_and_creative_finished_not_ongoing(tmp_path):
    svc = _svc(tmp_path)

    svc._push_salient_self(line="探索了「旧钥匙」", thread="兴趣：旧钥匙", close_thread_prefix="兴趣：")
    svc._push_salient_self(line="探索了「雨夜」", thread="兴趣：雨夜", close_thread_prefix="兴趣：")
    state = svc.store.get_life_state()
    interest = [t for t in state.ongoing_threads if t.startswith("兴趣：")]
    assert interest == ["兴趣：雨夜"]

    svc._push_salient_self(
        line="看了《片A》",
        thread="最近在看：《片A》",
        close_thread_prefix="最近在看：",
    )
    svc._push_salient_self(
        line="看了《片B》",
        thread="最近在看：《片B》",
        close_thread_prefix="最近在看：",
    )
    state = svc.store.get_life_state()
    watching = [t for t in state.ongoing_threads if t.startswith("最近在看：")]
    assert watching == ["最近在看：《片B》"]

    # Low-score style close without new thread clears 最近在看.
    svc._push_salient_self(
        line="看了《片C》",
        thread="",
        close_thread_prefix="最近在看：",
    )
    state = svc.store.get_life_state()
    assert not any(t.startswith("最近在看：") for t in state.ongoing_threads)

    svc._push_salient_self(
        line="续写了《夜航》约100字",
        thread="小说：《夜航》写作中",
    )
    svc._push_salient_self(
        line="写完了《夜航》",
        thread="",
        close_thread_prefix="小说：《夜航》",
    )
    state = svc.store.get_life_state()
    assert not any("夜航" in t and "已完成" in t for t in state.ongoing_threads)
    assert not any(t.startswith("小说：《夜航》") for t in state.ongoing_threads)
    assert any("写完了《夜航》" in s for s in state.salient_recent)


def test_day_roll_preserves_salient_and_threads(tmp_path, monkeypatch):
    # Freeze "now" to 10:00 so this test is deterministic regardless of the
    # wall-clock hour.  The real day-roll deliberately keeps a late-night
    # activity between 00:00-05:00 (late_roll), which made this test fail
    # whenever the suite ran in the small hours.
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 20, 10, 0, 0)

    monkeypatch.setattr(service_mod, "datetime", FixedDateTime)
    monkeypatch.setattr(
        service_mod, "now_cn", lambda: FixedDateTime(2026, 7, 20, 10, 0, 0)
    )
    monkeypatch.setattr(
        service_mod, "today_cn", lambda: FixedDateTime(2026, 7, 20, 10, 0, 0).date()
    )

    svc = _svc(tmp_path)
    old = LifeState(
        date="2026-07-19",
        energy=55,
        sleep="正常",
        mood_bias="平静",
        activity="旧活动",
        message_seed="旧念头",
        salient_recent=["看了《昨天》", "回了私信（甲）", "探索了「光」"],
        ongoing_threads=["小说：《续》写作中", "最近在看：《番》"],
        updated_at="2020-01-01T00:00:00",
    )
    svc.store.save_life_state(old)

    state = svc.ensure_life_state()
    assert state.date == "2026-07-20"
    assert state.activity == ""
    assert state.message_seed == ""
    assert "小说：《续》写作中" in state.ongoing_threads
    assert "最近在看：《番》" in state.ongoing_threads
    # salient kept (capped to 5 on roll)
    assert any("看了《昨天》" in s for s in state.salient_recent)
    assert len(state.salient_recent) <= 5
