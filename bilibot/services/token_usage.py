"""Token usage store — track LLM/vision/embedding token consumption.

Inspired by companion-style token dashboards; stores per-day aggregates and
recent event samples for the web statistics page.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bilibot.token_usage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage_events (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    account_id TEXT DEFAULT '',
    provider_id TEXT DEFAULT '',
    model TEXT DEFAULT '',
    kind TEXT DEFAULT 'chat',
    scene TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    success INTEGER DEFAULT 1,
    meta_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_token_usage_day ON token_usage_events(day);
CREATE INDEX IF NOT EXISTS idx_token_usage_ts ON token_usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_token_usage_account_day ON token_usage_events(account_id, day);
CREATE INDEX IF NOT EXISTS idx_token_usage_kind_day ON token_usage_events(kind, day);
CREATE INDEX IF NOT EXISTS idx_token_usage_scene_day ON token_usage_events(scene, day);
"""


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _day_offset(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def extract_usage_from_response(response: Any) -> Dict[str, int]:
    """Normalize OpenAI-style usage objects into ints."""
    out = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    if response is None:
        return out
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return out

    def _get(obj, *names, default=0):
        for n in names:
            if isinstance(obj, dict):
                if n in obj and obj[n] is not None:
                    try:
                        return int(obj[n])
                    except (TypeError, ValueError):
                        pass
            else:
                v = getattr(obj, n, None)
                if v is not None:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass
        return default

    prompt = _get(usage, "prompt_tokens", "input_tokens", "promptTokens")
    completion = _get(usage, "completion_tokens", "output_tokens", "completionTokens")
    total = _get(usage, "total_tokens", "totalTokens", default=prompt + completion)
    cached = 0
    # nested prompt_tokens_details.cached_tokens / input_tokens_details
    details = None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    else:
        details = getattr(usage, "prompt_tokens_details", None) or getattr(
            usage, "input_tokens_details", None
        )
    if details is not None:
        cached = _get(details, "cached_tokens", "cache_read_tokens", "cachedTokens")
    if not cached:
        cached = _get(usage, "cached_tokens", "cache_read_tokens", "prompt_cache_hit_tokens")

    out["prompt_tokens"] = max(0, prompt)
    out["completion_tokens"] = max(0, completion)
    out["total_tokens"] = max(0, total if total else prompt + completion)
    out["cached_tokens"] = max(0, cached)
    return out


class TokenUsageStore:
    """SQLite-backed token usage ledger (process-wide, under data_dir)."""

    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "token_usage.db"
        self._lock = threading.RLock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def record(
        self,
        *,
        provider_id: str = "",
        model: str = "",
        kind: str = "chat",
        scene: str = "",
        account_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        success: bool = True,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        now = time.time()
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        eid = uuid.uuid4().hex
        pt = max(0, int(prompt_tokens or 0))
        ct = max(0, int(completion_tokens or 0))
        tt = max(0, int(total_tokens or 0) or (pt + ct))
        cached = max(0, int(cached_tokens or 0))
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO token_usage_events
                    (id, ts, day, account_id, provider_id, model, kind, scene,
                     prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                     success, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        eid,
                        now,
                        day,
                        str(account_id or ""),
                        str(provider_id or ""),
                        str(model or ""),
                        str(kind or "chat"),
                        str(scene or ""),
                        pt,
                        ct,
                        tt,
                        cached,
                        1 if success else 0,
                        json.dumps(meta or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return eid

    def record_from_response(
        self,
        response: Any,
        *,
        provider_id: str = "",
        model: str = "",
        kind: str = "chat",
        scene: str = "",
        account_id: str = "",
        success: bool = True,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        usage = extract_usage_from_response(response)
        return self.record(
            provider_id=provider_id,
            model=model or "",
            kind=kind,
            scene=scene,
            account_id=account_id,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cached_tokens=usage["cached_tokens"],
            success=success,
            meta=meta,
        )

    def _sum_row(self, where: str, params: tuple) -> Dict[str, Any]:
        sql = f"""
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COUNT(*) AS calls,
                COALESCE(SUM(CASE WHEN success=1 THEN 1 ELSE 0 END), 0) AS success_calls
            FROM token_usage_events
            WHERE {where}
        """
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(sql, params).fetchone()
            finally:
                conn.close()
        if not row:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "calls": 0,
                "success_calls": 0,
            }
        return {
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "cached_tokens": int(row["cached_tokens"]),
            "calls": int(row["calls"]),
            "success_calls": int(row["success_calls"]),
        }

    def summary(
        self,
        *,
        days: int = 7,
        account_id: str = "",
    ) -> Dict[str, Any]:
        days = max(1, min(90, int(days or 7)))
        start_day = _day_offset(days - 1)
        where = "day >= ?"
        params: list = [start_day]
        if account_id:
            where += " AND account_id = ?"
            params.append(account_id)

        totals = self._sum_row(where, tuple(params))
        today = self._sum_row(
            "day = ?" + (" AND account_id = ?" if account_id else ""),
            ( _today(), account_id) if account_id else (_today(),),
        )

        # daily series
        daily_sql = f"""
            SELECT day,
                COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                COUNT(*) AS calls
            FROM token_usage_events
            WHERE {where}
            GROUP BY day
            ORDER BY day ASC
        """
        by_kind_sql = f"""
            SELECT kind,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                COALESCE(SUM(cached_tokens),0) AS cached_tokens,
                COUNT(*) AS calls
            FROM token_usage_events
            WHERE {where}
            GROUP BY kind
            ORDER BY total_tokens DESC
        """
        by_scene_sql = f"""
            SELECT COALESCE(NULLIF(scene,''),'(unset)') AS scene,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                COUNT(*) AS calls
            FROM token_usage_events
            WHERE {where}
            GROUP BY scene
            ORDER BY total_tokens DESC
            LIMIT 20
        """
        by_model_sql = f"""
            SELECT COALESCE(NULLIF(model,''),'(unknown)') AS model,
                COALESCE(NULLIF(provider_id,''),'(default)') AS provider_id,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                COUNT(*) AS calls
            FROM token_usage_events
            WHERE {where}
            GROUP BY model, provider_id
            ORDER BY total_tokens DESC
            LIMIT 20
        """
        by_account_sql = f"""
            SELECT COALESCE(NULLIF(account_id,''),'(global)') AS account_id,
                COALESCE(SUM(total_tokens),0) AS total_tokens,
                COUNT(*) AS calls
            FROM token_usage_events
            WHERE {where}
            GROUP BY account_id
            ORDER BY total_tokens DESC
            LIMIT 30
        """
        recent_sql = f"""
            SELECT id, ts, day, account_id, provider_id, model, kind, scene,
                   prompt_tokens, completion_tokens, total_tokens, cached_tokens, success
            FROM token_usage_events
            WHERE {where}
            ORDER BY ts DESC
            LIMIT 50
        """

        with self._lock:
            conn = self._conn()
            try:
                daily_rows = conn.execute(daily_sql, tuple(params)).fetchall()
                kind_rows = conn.execute(by_kind_sql, tuple(params)).fetchall()
                scene_rows = conn.execute(by_scene_sql, tuple(params)).fetchall()
                model_rows = conn.execute(by_model_sql, tuple(params)).fetchall()
                account_rows = conn.execute(by_account_sql, tuple(params)).fetchall()
                recent_rows = conn.execute(recent_sql, tuple(params)).fetchall()
            finally:
                conn.close()

        # fill missing days with zeros for chart continuity
        day_map = {r["day"]: dict(r) for r in daily_rows}
        series = []
        for i in range(days - 1, -1, -1):
            d = _day_offset(i)
            row = day_map.get(d)
            if row:
                series.append({
                    "day": d,
                    "prompt_tokens": int(row["prompt_tokens"]),
                    "completion_tokens": int(row["completion_tokens"]),
                    "total_tokens": int(row["total_tokens"]),
                    "cached_tokens": int(row["cached_tokens"]),
                    "calls": int(row["calls"]),
                })
            else:
                series.append({
                    "day": d,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "calls": 0,
                })

        def _list(rows, fields):
            out = []
            for r in rows:
                item = {}
                for f in fields:
                    v = r[f]
                    item[f] = int(v) if isinstance(v, (int, float)) and f != "scene" and f != "kind" and f != "model" and f != "provider_id" and f != "account_id" else v
                    if f in ("total_tokens", "prompt_tokens", "completion_tokens", "cached_tokens", "calls", "success"):
                        try:
                            item[f] = int(v)
                        except Exception:
                            item[f] = v
                out.append(item)
            return out

        recent = []
        for r in recent_rows:
            recent.append({
                "id": r["id"],
                "ts": float(r["ts"]),
                "day": r["day"],
                "account_id": r["account_id"],
                "provider_id": r["provider_id"],
                "model": r["model"],
                "kind": r["kind"],
                "scene": r["scene"],
                "prompt_tokens": int(r["prompt_tokens"]),
                "completion_tokens": int(r["completion_tokens"]),
                "total_tokens": int(r["total_tokens"]),
                "cached_tokens": int(r["cached_tokens"]),
                "success": bool(r["success"]),
            })

        return {
            "days": days,
            "start_day": start_day,
            "end_day": _today(),
            "today": today,
            "totals": totals,
            "daily": series,
            "by_kind": [
                {
                    "kind": r["kind"],
                    "total_tokens": int(r["total_tokens"]),
                    "prompt_tokens": int(r["prompt_tokens"]),
                    "completion_tokens": int(r["completion_tokens"]),
                    "cached_tokens": int(r["cached_tokens"]),
                    "calls": int(r["calls"]),
                }
                for r in kind_rows
            ],
            "by_scene": [
                {
                    "scene": r["scene"],
                    "total_tokens": int(r["total_tokens"]),
                    "calls": int(r["calls"]),
                }
                for r in scene_rows
            ],
            "by_model": [
                {
                    "model": r["model"],
                    "provider_id": r["provider_id"],
                    "total_tokens": int(r["total_tokens"]),
                    "calls": int(r["calls"]),
                }
                for r in model_rows
            ],
            "by_account": [
                {
                    "account_id": r["account_id"],
                    "total_tokens": int(r["total_tokens"]),
                    "calls": int(r["calls"]),
                }
                for r in account_rows
            ],
            "recent": recent,
        }

    def purge_older_than(self, days: int = 90) -> int:
        days = max(7, int(days or 90))
        cutoff = _day_offset(days)
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM token_usage_events WHERE day < ?", (cutoff,))
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()


# process-wide optional default store (set by app/panel)
_GLOBAL_STORE: Optional[TokenUsageStore] = None
_GLOBAL_LOCK = threading.Lock()


def set_global_token_store(store: Optional[TokenUsageStore]) -> None:
    global _GLOBAL_STORE
    with _GLOBAL_LOCK:
        _GLOBAL_STORE = store


def get_global_token_store() -> Optional[TokenUsageStore]:
    with _GLOBAL_LOCK:
        return _GLOBAL_STORE


def record_usage_safe(**kwargs: Any) -> None:
    store = get_global_token_store()
    if store is None:
        return
    try:
        store.record(**kwargs)
    except Exception as e:
        logger.debug("token usage record failed: %s", e)


def record_response_safe(response: Any, **kwargs: Any) -> None:
    store = get_global_token_store()
    if store is None:
        return
    try:
        store.record_from_response(response, **kwargs)
    except Exception as e:
        logger.debug("token usage record_from_response failed: %s", e)
