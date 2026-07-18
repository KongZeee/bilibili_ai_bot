"""Unit smoke for SelfState HIGH fixes (PM privacy, threads, day-roll)."""

from __future__ import annotations

from datetime import datetime, timedelta

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
    assert any("网友甲" in (x or "") for x in (state.salient_recent or []))

    surface = svc.get_prompt_surface()
    assert secret not in surface
    assert "phone" not in surface
    # Body may live only on non-injected runtime.
    runtime = svc.store.get_runtime()
    assert secret[:20] in (runtime.get("last_private_message_preview") or "")


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


def test_day_roll_preserves_salient_and_threads(tmp_path):
    svc = _svc(tmp_path)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    old = LifeState(
        date=yesterday,
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
    today = datetime.now().strftime("%Y-%m-%d")
    assert state.date == today
    assert state.activity == ""
    assert state.message_seed == ""
    assert "小说：《续》写作中" in state.ongoing_threads
    assert "最近在看：《番》" in state.ongoing_threads
    # salient kept (capped to 5 on roll)
    assert any("看了《昨天》" in s for s in state.salient_recent)
    assert len(state.salient_recent) <= 5
