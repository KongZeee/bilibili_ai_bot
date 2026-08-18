"""Shared operational health rules for the bot and its external monitor."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

ENRICHMENT_MAX_PENDING_JOBS = 100
ENRICHMENT_MAX_PENDING_AGE_SECONDS = 15 * 60.0
CONSOLIDATION_ERROR_OUTCOMES = frozenset(
    {"invalid_model_output", "exception", "memory_unavailable"}
)
CONSOLIDATION_DEFAULT_HOUR = 3
CONSOLIDATION_DEFAULT_MINUTE = 0
CONSOLIDATION_MAX_ERROR_AGE_SECONDS = 26 * 3600.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def memory_enrichment_is_clear(
    stats: Mapping[str, Any],
    *,
    max_jobs: int = ENRICHMENT_MAX_PENDING_JOBS,
    max_age_seconds: float = ENRICHMENT_MAX_PENDING_AGE_SECONDS,
) -> bool:
    """Apply one queue/dead-letter rule to store stats and monitor metrics."""

    jobs = stats.get("jobs") if isinstance(stats.get("jobs"), Mapping) else {}
    operations = (
        stats.get("operations")
        if isinstance(stats.get("operations"), Mapping)
        else stats
    )
    active = operations.get("active_jobs", stats.get("pending_jobs", 0))
    oldest_age = operations.get(
        "oldest_active_job_age_seconds",
        stats.get("pending_oldest_age_seconds", 0.0),
    )
    dead = jobs.get("dead", stats.get("dead_jobs", 0))
    return (
        _as_float(active) <= max(0, int(max_jobs))
        and _as_float(oldest_age) <= max(0.0, float(max_age_seconds))
        and _as_float(dead) == 0
    )


def consolidation_schedule_reached(
    now: datetime,
    *,
    hour: int = CONSOLIDATION_DEFAULT_HOUR,
    minute: int = CONSOLIDATION_DEFAULT_MINUTE,
) -> bool:
    """Return whether today's scheduled consolidation time has passed."""

    target_hour = max(0, min(23, int(hour)))
    target_minute = max(0, min(59, int(minute)))
    scheduled = now.replace(
        hour=target_hour,
        minute=target_minute,
        second=0,
        microsecond=0,
    )
    return now >= scheduled


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None


def _attempt_age_seconds(value: Any, now: datetime) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        attempted = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if attempted.tzinfo is None:
        attempted = attempted.astimezone()
    return max(0.0, now.timestamp() - attempted.timestamp())


def evaluate_consolidation_state(
    state: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    state_exists: bool = True,
    hour: int = CONSOLIDATION_DEFAULT_HOUR,
    minute: int = CONSOLIDATION_DEFAULT_MINUTE,
    max_error_age_seconds: float = CONSOLIDATION_MAX_ERROR_AGE_SECONDS,
) -> dict[str, Any]:
    """Evaluate today's consolidation without treating a missing state as healthy."""

    current = now or datetime.now().astimezone()
    raw_state = state if isinstance(state, Mapping) else {}
    attempted_day = _parse_date(raw_state.get("last_attempt_date"))
    outcome = str(raw_state.get("outcome") or "").strip() or "unknown"
    age_seconds = _attempt_age_seconds(raw_state.get("attempted_at"), current)

    if attempted_day == current.date():
        recent_error = (
            outcome in CONSOLIDATION_ERROR_OUTCOMES
            and (
                age_seconds is None
                or age_seconds <= max(0.0, float(max_error_age_seconds))
            )
        )
        return {
            "clear": not recent_error,
            "outcome": outcome,
            "age_seconds": age_seconds,
            "due": False,
        }

    due = consolidation_schedule_reached(current, hour=hour, minute=minute)
    if not due:
        return {
            "clear": True,
            "outcome": "not_due",
            "age_seconds": age_seconds,
            "due": False,
        }
    return {
        "clear": False,
        "outcome": "missing" if not state_exists else "stale",
        "age_seconds": age_seconds,
        "due": True,
    }


__all__ = [
    "CONSOLIDATION_DEFAULT_HOUR",
    "CONSOLIDATION_DEFAULT_MINUTE",
    "CONSOLIDATION_ERROR_OUTCOMES",
    "CONSOLIDATION_MAX_ERROR_AGE_SECONDS",
    "ENRICHMENT_MAX_PENDING_AGE_SECONDS",
    "ENRICHMENT_MAX_PENDING_JOBS",
    "consolidation_schedule_reached",
    "evaluate_consolidation_state",
    "memory_enrichment_is_clear",
]
