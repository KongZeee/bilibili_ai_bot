"""_generate_daily_schedule, _persist_schedule, _mark_overdue_as_triggered, _save_schedule_state, _save_dynamic_schedule_state, get_schedule_snapshot extracted from scheduler.py."""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from typing import Any, Dict, List

from bilibot.services.clock import now_cn, today_cn


logger = logging.getLogger("bilibot.scheduler_daily")

_ACTIVE_SCHEDULE_STATUSES = frozenset({
    "scheduled", "claimed", "running", "retry_wait", "interrupted",
})


def _parse_schedule_slots(values: Any) -> List[tuple] | None:
    if not isinstance(values, list):
        return None
    slots: List[tuple] = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or value.count(":") != 1:
            return None
        hour_s, minute_s = value.split(":", 1)
        try:
            hour = int(hour_s)
            minute = int(minute_s)
        except ValueError:
            return None
        slot = (hour, minute)
        if not (0 <= hour < 24 and 0 <= minute < 60) or slot in seen:
            return None
        seen.add(slot)
        slots.append(slot)
    return sorted(slots)


def _load_persisted_schedule_slots(
    self,
    *,
    filename: str,
    slot_field: str,
    today_str: str,
    expected_count: int,
) -> List[tuple] | None:
    if not self.ds:
        return None
    state = self.ds.load_json(filename, {}) or {}
    if not isinstance(state, dict) or str(state.get("date") or "") != today_str:
        return None
    slots = _parse_schedule_slots(state.get(slot_field))
    if slots is None or len(slots) != expected_count:
        return None
    return slots


def _restore_schedule_slots_from_runs(
    self,
    *,
    scene: str,
    today_str: str,
    timezone,
) -> List[tuple] | None:
    """Use durable TaskRuns as a recovery fallback when JSON state is absent."""
    runs = self.task_store.list_by_account_scene(
        self.account_id or "_default", scene, date_str=today_str,
    )
    schedule_runs = []
    for run in runs:
        try:
            payload = json.loads(run.input_json or "{}")
        except (TypeError, ValueError):
            payload = {}
        if (
            isinstance(payload, dict)
            and str(payload.get("date") or "") == today_str
            and str(payload.get("slot") or "")
        ) or run.trigger_type == "schedule":
            schedule_runs.append(run)
    active_runs = [run for run in schedule_runs if run.status in _ACTIVE_SCHEDULE_STATUSES]
    if not active_runs:
        # Existing terminal schedule rows prove that today already had a plan.
        return [] if schedule_runs else None

    slots = set()
    for run in active_runs:
        scheduled = datetime.fromtimestamp(float(run.scheduled_at), tz=timezone)
        slots.add((scheduled.hour, scheduled.minute))
    return sorted(slots)


def generate_daily_schedule(self):
    """Restore today's durable plan or generate one exactly once per day."""
    config = self.config_loader.get_raw_config()
    prov = config.get("proactive", {})
    current = now_cn()
    today_str = current.date().isoformat()
    today_start = current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    account_id = self.account_id or "_default"

    # Only prior-day slots are stale. Current-day slots must survive a restart.
    try:
        self.task_store.expire_scheduled_before(
            account_id, "proactive_video", today_start,
        )
        self.task_store.expire_scheduled_before(
            account_id, "dynamic", today_start,
        )
    except Exception as e:
        logger.warning(f"跨天清理 scheduled TaskRun 失败: {e}")

    # 场景级配置（grace_window / max_attempts）
    scenes_cfg = prov.get("scenes", {}) or {}
    pv_scene = scenes_cfg.get("proactive_video", {}) or {}
    dyn_scene = scenes_cfg.get("dynamic", {}) or {}
    default_grace = prov.get("grace_window_seconds", 900)
    pv_grace = pv_scene.get("grace_window_seconds", default_grace)
    pv_max_att = pv_scene.get("max_attempts", 3)
    dyn_grace = dyn_scene.get("grace_window_seconds", default_grace)
    dyn_max_att = dyn_scene.get("max_attempts", 3)

    # Prefer the saved plan; recover active rows for legacy/missing JSON state.
    n_videos = min(prov.get("video_count", 0), 24 * 60)
    video_slots = _load_persisted_schedule_slots(
        self,
        filename="schedule_today.json",
        slot_field="proactive_times",
        today_str=today_str,
        expected_count=n_videos,
    )
    if video_slots is None:
        video_slots = _restore_schedule_slots_from_runs(
            self,
            scene="proactive_video",
            today_str=today_str,
            timezone=current.tzinfo,
        )
    if video_slots is None:
        slots = sorted(random.sample(range(24 * 60), n_videos)) if n_videos else []
        video_slots = [(slot // 60, slot % 60) for slot in slots]
    else:
        logger.info("恢复今日主动视频计划: %d 个槽位", len(video_slots))
    self._proactive_times = video_slots
    self._persist_schedule(
        "proactive_video", self._proactive_times, pv_grace, pv_max_att, today_str,
    )

    n_dynamics = min(prov.get("dynamic_count", 0), 13)
    dynamic_slots = _load_persisted_schedule_slots(
        self,
        filename="dynamic_schedule.json",
        slot_field="dynamic_times",
        today_str=today_str,
        expected_count=n_dynamics,
    )
    if dynamic_slots is None:
        dynamic_slots = _restore_schedule_slots_from_runs(
            self,
            scene="dynamic",
            today_str=today_str,
            timezone=current.tzinfo,
        )
    if dynamic_slots is None:
        hours = sorted(random.sample(range(10, 23), n_dynamics)) if n_dynamics else []
        dynamic_slots = [(hour, random.randint(0, 59)) for hour in hours]
    else:
        logger.info("恢复今日动态计划: %d 个槽位", len(dynamic_slots))
    self._dynamic_times = dynamic_slots
    self._persist_schedule(
        "dynamic", self._dynamic_times, dyn_grace, dyn_max_att, today_str,
    )

    # Persist immediately so a restart before the first trigger keeps the plan.
    self._save_schedule_state()
    self._save_dynamic_schedule_state()
    logger.info(
        f"主动视频计划: {[f'{h}:{m:02d}' for h, m in self._proactive_times]}"
    )
    logger.info(
        f"动态发布计划: {[f'{h}:{m:02d}' for h, m in self._dynamic_times]}"
    )

def persist_schedule(self, scene: str, times: List[tuple],
                      grace_window: int, max_attempts: int,
                      date_str: str):
    """将调度计划持久化为 TaskRun 记录（幂等）"""
    from bilibot.services.task_store import TRIGGER_SCHEDULE
    for (h, m) in times:
        slot = f"{h:02d}:{m:02d}"
        idem_key = f"{self.account_id or '_default'}:{scene}:{date_str}:{slot}"
        scheduled_at = datetime.strptime(
            f"{date_str} {slot}", "%Y-%m-%d %H:%M"
        ).timestamp()
        try:
            self.task_store.create_if_absent(
                account_id=self.account_id or "_default",
                scene=scene,
                idempotency_key=idem_key,
                trigger_type=TRIGGER_SCHEDULE,
                scheduled_at=scheduled_at,
                input_data={"slot": slot, "date": date_str},
                max_attempts=max_attempts,
                grace_window=grace_window,
            )
        except Exception as e:
            logger.warning(f"持久化 TaskRun 失败 scene={scene} slot={slot}: {e}")

def mark_overdue_as_triggered(self):
    """标记已过期的计划为已执行"""
    now = now_cn()

    self._proactive_triggered = {
        f"{h:02d}:{m:02d}" for h, m in self._proactive_times
        if now.hour > h or (now.hour == h and now.minute > m)
    }

    self._dynamic_triggered = {
        f"{h:02d}:{m:02d}" for h, m in self._dynamic_times
        if now.hour > h or (now.hour == h and now.minute > m)
    }

def save_schedule_state(self):
    """保存调度状态"""
    if not self.ds:
        return
    state = {
        "date": today_cn().isoformat(),
        "proactive_times": [f"{h}:{m:02d}" for h, m in self._proactive_times],
        "proactive_triggered": sorted(self._proactive_triggered),
    }
    self.ds.save_json("schedule_today.json", state)

def save_dynamic_schedule_state(self):
    """保存动态调度状态"""
    if not self.ds:
        return
    state = {
        "date": today_cn().isoformat(),
        "dynamic_times": [f"{h}:{m:02d}" for h, m in self._dynamic_times],
        "dynamic_triggered": sorted(self._dynamic_triggered),
    }
    self.ds.save_json("dynamic_schedule.json", state)

def get_schedule_snapshot(self) -> Dict[str, Any]:
    """获取今日调度快照"""
    return {
        "date": today_cn().isoformat(),
        "proactive_times": [f"{h}:{m:02d}" for h, m in self._proactive_times],
        "proactive_triggered": sorted(self._proactive_triggered),
        "dynamic_times": [f"{h}:{m:02d}" for h, m in self._dynamic_times],
        "dynamic_triggered": sorted(self._dynamic_triggered),
    }
