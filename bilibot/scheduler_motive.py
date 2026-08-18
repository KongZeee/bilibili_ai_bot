"""Per-scheduler-tick companion motive snapshots and observability."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from bilibot.services.clock import now_cn


logger = logging.getLogger("bilibot.scheduler_motive")

_REST_ACTION = "rest"
_REST_GATE_SCORE = 6.0
_RUNTIME_HEARTBEAT_SECONDS = 15 * 60.0
_MOTIVE_ERROR_LOG_INTERVAL_SECONDS = 15 * 60.0
_ACTION_ALIASES = {
    "rest": "rest",
    "休息": "rest",
    "post_dynamic": "post_dynamic",
    "动态": "post_dynamic",
    "browse_video": "browse_video",
    "浏览视频": "browse_video",
    "reply": "reply",
    "explore": "explore",
    "creative": "creative",
}


@dataclass(frozen=True)
class MotiveSnapshot:
    action: str
    score: float

    @property
    def rest_gated(self) -> bool:
        return self.action == _REST_ACTION and self.score >= _REST_GATE_SCORE


def _normalize_action(value: object) -> str:
    raw = str(value or "").strip().casefold()
    return _ACTION_ALIASES.get(raw, raw)


def _log_motive_failure(self, kind: str, error_kind: str) -> None:
    now = time.monotonic()
    last = float(getattr(self, "_last_motive_error_logged_at", 0.0) or 0.0)
    if now - last >= _MOTIVE_ERROR_LOG_INTERVAL_SECONDS:
        logger.warning(
            "motive %s unavailable; proactive gate remains fail-open error_kind=%s",
            kind,
            error_kind,
        )
        self._last_motive_error_logged_at = now
    else:
        logger.debug(
            "motive %s still unavailable error_kind=%s",
            kind,
            error_kind,
        )


def _select_motive(self) -> Optional[MotiveSnapshot]:
    companion = getattr(self, "companion", None)
    if companion is None or not getattr(companion, "enabled", False):
        return None
    try:
        select = getattr(companion, "select_motive", None)
        if not callable(select):
            return None
        top = select()
        if top is None:
            return None
        return MotiveSnapshot(
            action=_normalize_action(getattr(top, "suggested_action", "")),
            score=float(getattr(top, "score", 0) or 0),
        )
    except Exception as exc:
        _log_motive_failure(self, "selection", type(exc).__name__)
        return None


def _log_rest_gate_state(self, snapshot: Optional[MotiveSnapshot]) -> None:
    current = (
        (snapshot.action, round(snapshot.score, 1))
        if snapshot is not None and snapshot.rest_gated
        else None
    )
    previous = getattr(self, "_last_rest_gate_state", None)
    if current == previous:
        if current is not None:
            logger.debug("MotiveQueue rest gate remains active score=%.1f", current[1])
        return
    if current is not None:
        logger.info("MotiveQueue rest gate active score=%.1f", current[1])
    elif previous is not None:
        action = snapshot.action if snapshot is not None else "unavailable"
        score = snapshot.score if snapshot is not None else 0.0
        logger.info(
            "MotiveQueue rest gate released action=%s score=%.1f",
            action,
            score,
        )
    self._last_rest_gate_state = current


def prepare_tick_motive(self) -> Optional[MotiveSnapshot]:
    """Evaluate companion motivation once for the current scheduler tick."""
    snapshot = _select_motive(self)
    self._tick_motive_snapshot = snapshot
    self._tick_motive_snapshot_prepared = True
    _log_rest_gate_state(self, snapshot)
    return snapshot


def get_tick_motive(self) -> Optional[MotiveSnapshot]:
    """Return the prepared tick value, or safely evaluate for direct callers."""
    if getattr(self, "_tick_motive_snapshot_prepared", False):
        return getattr(self, "_tick_motive_snapshot", None)
    return _select_motive(self)


def persist_motive_runtime(
    self,
    *,
    snapshot: Optional[MotiveSnapshot],
    actionable: bool,
    gate: str,
) -> bool:
    """Persist stable motive state only on change or a bounded heartbeat."""
    if snapshot is None:
        return False
    companion = getattr(self, "companion", None)
    store = getattr(companion, "store", None)
    patch_runtime = getattr(store, "patch_runtime", None)
    if not callable(patch_runtime):
        _log_motive_failure(self, "runtime persistence", "store_unavailable")
        return False

    signature = (
        snapshot.action,
        round(snapshot.score, 3),
        bool(actionable),
        str(gate),
    )
    now = time.monotonic()
    last_signature = getattr(self, "_last_motive_runtime_signature", None)
    last_persisted_at = float(
        getattr(self, "_last_motive_runtime_persisted_at", 0.0) or 0.0
    )
    if (
        signature == last_signature
        and now - last_persisted_at < _RUNTIME_HEARTBEAT_SECONDS
    ):
        return False

    try:
        patch_runtime(
            last_selected_motive=snapshot.action,
            last_selected_motive_score=snapshot.score,
            last_selected_motive_at=now_cn().isoformat(),
            last_selected_motive_actionable=bool(actionable),
            last_selected_motive_gate=str(gate),
        )
    except Exception as exc:
        _log_motive_failure(self, "runtime persistence", type(exc).__name__)
        return False
    self._last_motive_runtime_signature = signature
    self._last_motive_runtime_persisted_at = now
    return True
