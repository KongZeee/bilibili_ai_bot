#!/usr/bin/env python3
"""Loopback readiness monitor for the fwq BiliBot deployment."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from bilibot.runtime_health import (
        CONSOLIDATION_DEFAULT_HOUR,
        CONSOLIDATION_DEFAULT_MINUTE,
        CONSOLIDATION_MAX_ERROR_AGE_SECONDS,
        ENRICHMENT_MAX_PENDING_AGE_SECONDS,
        ENRICHMENT_MAX_PENDING_JOBS,
        evaluate_consolidation_state,
        memory_enrichment_is_clear,
    )
    from bilibot.services.clock import now_cn
except ModuleNotFoundError:
    # Support both <repo>/scripts/fwq_monitor.py and /root/monitor.py.
    script_path = Path(__file__).resolve()
    candidates = (
        script_path.parent / "bilibot",
        script_path.parents[1],
    )
    for candidate in candidates:
        if (candidate / "bilibot").is_dir():
            sys.path.insert(0, str(candidate))
            break
    from bilibot.runtime_health import (
        CONSOLIDATION_DEFAULT_HOUR,
        CONSOLIDATION_DEFAULT_MINUTE,
        CONSOLIDATION_MAX_ERROR_AGE_SECONDS,
        ENRICHMENT_MAX_PENDING_AGE_SECONDS,
        ENRICHMENT_MAX_PENDING_JOBS,
        evaluate_consolidation_state,
        memory_enrichment_is_clear,
    )
    from bilibot.services.clock import now_cn


STATUS_URL = os.environ.get(
    "BILIBOT_MONITOR_STATUS_URL", "http://127.0.0.1:18081/api/status/public"
)
READY_URL = os.environ.get(
    "BILIBOT_MONITOR_READY_URL", "http://127.0.0.1:18081/api/status/ready"
)
MEMORY_DB = Path(
    os.environ.get("BILIBOT_MONITOR_MEMORY_DB", "/root/bilibot/data/bot/memory_brain.db")
)
BOT_LOG = Path(os.environ.get("BILIBOT_MONITOR_BOT_LOG", "/root/bilibot_console.log"))
MONITOR_LOG = Path(os.environ.get("BILIBOT_MONITOR_LOG", "/root/monitor.log"))
DISK_PATH = Path(os.environ.get("BILIBOT_MONITOR_DISK_PATH", "/root/bilibot"))
CONSOLIDATION_STATE = Path(
    os.environ.get(
        "BILIBOT_MONITOR_CONSOLIDATION_STATE",
        "/root/bilibot/data/bot/consolidation_state.json",
    )
)
MIN_FREE_BYTES = int(os.environ.get("BILIBOT_MONITOR_MIN_FREE_BYTES", str(1 << 30)))
MAX_PENDING_MEMORY_JOBS = int(
    os.environ.get(
        "BILIBOT_MONITOR_MAX_PENDING_MEMORY_JOBS",
        str(ENRICHMENT_MAX_PENDING_JOBS),
    )
)
MAX_PENDING_MEMORY_AGE_SECONDS = float(
    os.environ.get(
        "BILIBOT_MONITOR_MAX_PENDING_MEMORY_AGE_SECONDS",
        str(ENRICHMENT_MAX_PENDING_AGE_SECONDS),
    )
)
MAX_CONSOLIDATION_ERROR_AGE_SECONDS = float(
    os.environ.get(
        "BILIBOT_MONITOR_MAX_CONSOLIDATION_ERROR_AGE_SECONDS",
        str(CONSOLIDATION_MAX_ERROR_AGE_SECONDS),
    )
)
CONSOLIDATION_HOUR = int(
    os.environ.get("BILIBOT_MONITOR_CONSOLIDATION_HOUR", str(CONSOLIDATION_DEFAULT_HOUR))
)
CONSOLIDATION_MINUTE = int(
    os.environ.get(
        "BILIBOT_MONITOR_CONSOLIDATION_MINUTE",
        str(CONSOLIDATION_DEFAULT_MINUTE),
    )
)
APP_LOG_TAIL_BYTES = int(os.environ.get("BILIBOT_MONITOR_APP_LOG_TAIL_BYTES", str(256 * 1024)))

EXPECTED_READINESS_CHECKS = frozenset(
    {
        "service",
        "account_running",
        "account_authenticated",
        "llm_available",
        "global_pause_clear",
        "account_pause_clear",
        "task_runs_clear",
        "pm_states_clear",
        "memory_available",
        "memory_enrichment_clear",
        "consolidation_clear",
    }
)


def _fetch_json(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("monitor endpoint did not return an object")
    return value


def _readiness_payload_is_healthy(value: dict[str, Any]) -> bool:
    """Require the aggregate flag and every known boolean check."""
    if value.get("ready") is not True:
        return False
    checks = value.get("checks")
    if not isinstance(checks, dict):
        return False
    return all(checks.get(name) is True for name in EXPECTED_READINESS_CHECKS)


def _memory_stats(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        now = time.time()
        quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        pending_row = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM brain_jobs "
            "WHERE status IN ('pending','processing','retry','blocked')"
        ).fetchone()
        pending = int(pending_row[0] if pending_row else 0)
        oldest_created = pending_row[1] if pending_row else None
        pending_age = (
            max(0.0, now - float(oldest_created))
            if pending and oldest_created is not None
            else 0.0
        )
        dead = int(
            conn.execute(
                "SELECT COUNT(*) FROM brain_jobs WHERE status = 'dead'"
            ).fetchone()[0]
        )
        events = int(conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0])
        return {
            "quick_check": quick_check,
            "pending_jobs": pending,
            "pending_oldest_age_seconds": pending_age,
            "dead_jobs": dead,
            "events": events,
        }
    finally:
        conn.close()


def _read_recent_log(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max(1, APP_LOG_TAIL_BYTES)))
        text = handle.read().decode("utf-8", errors="replace")
    return text, text.count("[ERROR]")


def _consolidation_status(path: Path) -> dict[str, Any]:
    try:
        state_exists = path.exists()
        state = json.loads(path.read_text(encoding="utf-8")) if state_exists else {}
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        report = evaluate_consolidation_state(
            state,
            now=now_cn(),
            state_exists=state_exists,
            hour=CONSOLIDATION_HOUR,
            minute=CONSOLIDATION_MINUTE,
            max_error_age_seconds=MAX_CONSOLIDATION_ERROR_AGE_SECONDS,
        )
        return {
            "clear": bool(report.get("clear")),
            "outcome": str(report.get("outcome") or "unknown")[:40],
            "age_seconds": report.get("age_seconds"),
        }
    except Exception as exc:
        return {
            "clear": False,
            "outcome": "state_error",
            "age_seconds": None,
            "error_kind": type(exc).__name__,
        }


def sample(
    *,
    status_url: str = STATUS_URL,
    ready_url: str = READY_URL,
    memory_db: Path = MEMORY_DB,
    bot_log: Path = BOT_LOG,
    disk_path: Path = DISK_PATH,
    consolidation_state: Path = CONSOLIDATION_STATE,
) -> dict[str, Any]:
    """Collect a non-sensitive liveness and readiness report."""
    checks = {
        "service_running": False,
        "business_ready": False,
        "memory_integrity": False,
        "memory_dead_jobs_clear": False,
        "memory_pending_jobs_clear": False,
        "disk_free": False,
        "bot_log_readable": False,
        "consolidation_clear": False,
    }
    metrics: dict[str, Any] = {}
    error_kinds: list[str] = []

    try:
        public = _fetch_json(status_url)
        checks["service_running"] = public.get("running") is True
    except Exception as exc:
        error_kinds.append(f"public:{type(exc).__name__}")

    try:
        readiness = _fetch_json(ready_url)
        checks["business_ready"] = _readiness_payload_is_healthy(readiness)
    except Exception as exc:
        error_kinds.append(f"ready:{type(exc).__name__}")

    try:
        memory = _memory_stats(memory_db)
        metrics.update(memory)
        checks["memory_integrity"] = memory["quick_check"].lower() == "ok"
        checks["memory_dead_jobs_clear"] = memory["dead_jobs"] == 0
        checks["memory_pending_jobs_clear"] = memory_enrichment_is_clear(
            memory,
            max_jobs=MAX_PENDING_MEMORY_JOBS,
            max_age_seconds=MAX_PENDING_MEMORY_AGE_SECONDS,
        )
    except Exception as exc:
        error_kinds.append(f"memory:{type(exc).__name__}")

    try:
        free = shutil.disk_usage(disk_path).free
        metrics["disk_free_bytes"] = free
        checks["disk_free"] = free >= MIN_FREE_BYTES
    except Exception as exc:
        error_kinds.append(f"disk:{type(exc).__name__}")

    try:
        _, recent_errors = _read_recent_log(bot_log)
        metrics["recent_error_lines"] = recent_errors
        checks["bot_log_readable"] = True
    except Exception as exc:
        error_kinds.append(f"log:{type(exc).__name__}")

    consolidation = _consolidation_status(consolidation_state)
    metrics["consolidation_outcome"] = consolidation.get("outcome", "unknown")
    metrics["consolidation_age_seconds"] = consolidation.get("age_seconds")
    checks["consolidation_clear"] = bool(consolidation.get("clear"))
    if consolidation.get("error_kind"):
        error_kinds.append(f"consolidation:{consolidation['error_kind']}")

    return {
        "ready": all(checks.values()),
        "checks": checks,
        "metrics": metrics,
        "error_kinds": error_kinds,
    }


def format_report(report: dict[str, Any], now: float | None = None) -> str:
    stamp = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(now if now is not None else time.time()),
    )
    status = "healthy" if report.get("ready") else "ALERT"
    checks = report.get("checks") or {}
    metrics = report.get("metrics") or {}
    errors = ",".join(report.get("error_kinds") or []) or "none"
    free_bytes = metrics.get("disk_free_bytes")
    free_mb = int(float(free_bytes) / (1024 * 1024)) if free_bytes is not None else "-"
    pending_age = int(float(metrics.get("pending_oldest_age_seconds") or 0))
    consolidation = str(metrics.get("consolidation_outcome") or "unknown")[:40]
    return (
        f"{stamp} {status} service={int(bool(checks.get('service_running')))} "
        f"ready={int(bool(checks.get('business_ready')))} "
        f"memory_ok={int(bool(checks.get('memory_integrity')))} "
        f"pending_ok={int(bool(checks.get('memory_pending_jobs_clear')))} "
        f"disk_ok={int(bool(checks.get('disk_free')))} disk_free_mb={free_mb} "
        f"dead_jobs={metrics.get('dead_jobs', '-')} "
        f"pending_jobs={metrics.get('pending_jobs', '-')} "
        f"pending_age_s={pending_age} recent_errors={metrics.get('recent_error_lines', '-')} "
        f"consolidation={consolidation} errors={errors}"
    )


def run(*, cycles: int, interval_seconds: float) -> None:
    for _ in range(max(1, int(cycles))):
        report = sample()
        line = format_report(report)
        print(line, flush=True)
        with MONITOR_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        time.sleep(max(1.0, float(interval_seconds)))


if __name__ == "__main__":
    run(
        cycles=int(os.environ.get("BILIBOT_MONITOR_CYCLES", "4320")),
        interval_seconds=float(os.environ.get("BILIBOT_MONITOR_INTERVAL_SECONDS", "600")),
    )
