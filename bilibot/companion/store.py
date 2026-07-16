"""Per-account companion JSON store (atomic writes)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    CreativeProject,
    DailyPlan,
    DiaryEntry,
    DreamFragment,
    DreamRecord,
    ExploreNote,
    LifeState,
    StoryDetail,
)

logger = logging.getLogger("bilibot.companion.store")


class CompanionStore:
    """File-backed store under {account_data_dir}/companion/."""

    def __init__(self, account_data_dir: str):
        self.root = Path(account_data_dir) / "companion"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, name: str) -> Path:
        safe = os.path.basename(name)
        return self.root / safe

    def _load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        with self._lock:
            if not path.exists():
                return default
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("companion load %s failed: %s", name, e)
                return default

    def _save(self, name: str, data: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as e:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                logger.error("companion save %s failed: %s", name, e)
                raise

    # ── life state ──

    def get_life_state(self) -> LifeState:
        return LifeState.from_dict(self._load("life_state.json", {}))

    def save_life_state(self, state: LifeState) -> None:
        self._save("life_state.json", state.to_dict())

    # ── daily plan ──

    def get_daily_plan(self) -> DailyPlan:
        return DailyPlan.from_dict(self._load("daily_plan.json", {}))

    def save_daily_plan(self, plan: DailyPlan) -> None:
        self._save("daily_plan.json", plan.to_dict())

    # ── story detail ──

    def get_story_detail(self) -> StoryDetail:
        return StoryDetail.from_dict(self._load("story_plan.json", {}))

    def save_story_detail(self, detail: StoryDetail) -> None:
        self._save("story_plan.json", detail.to_dict())

    # ── dreams ──

    def get_dream_fragments(self) -> List[DreamFragment]:
        raw = self._load("dream_fragments.json", [])
        if not isinstance(raw, list):
            return []
        return [DreamFragment.from_dict(x) for x in raw if isinstance(x, dict) and x.get("text")]

    def save_dream_fragments(self, items: List[DreamFragment]) -> None:
        self._save("dream_fragments.json", [x.to_dict() for x in items])

    def get_latest_dream(self) -> Optional[DreamRecord]:
        raw = self._load("latest_dream.json", {})
        if not raw:
            return None
        return DreamRecord.from_dict(raw)

    def save_latest_dream(self, dream: DreamRecord) -> None:
        self._save("latest_dream.json", dream.to_dict())

    # ── diaries ──

    def get_diaries(self) -> List[DiaryEntry]:
        raw = self._load("bot_diaries.json", [])
        if not isinstance(raw, list):
            return []
        return [DiaryEntry.from_dict(x) for x in raw if isinstance(x, dict)]

    def save_diaries(self, entries: List[DiaryEntry]) -> None:
        self._save("bot_diaries.json", [e.to_dict() for e in entries])

    # ── explore notes ──

    def get_explore_notes(self) -> List[ExploreNote]:
        raw = self._load("explore_notes.json", [])
        if not isinstance(raw, list):
            return []
        return [ExploreNote.from_dict(x) for x in raw if isinstance(x, dict)]

    def save_explore_notes(self, notes: List[ExploreNote]) -> None:
        self._save("explore_notes.json", [n.to_dict() for n in notes])

    # ── creative ──

    def get_projects(self) -> List[CreativeProject]:
        raw = self._load("creative_projects.json", [])
        if not isinstance(raw, list):
            return []
        return [CreativeProject.from_dict(x) for x in raw if isinstance(x, dict)]

    def save_projects(self, projects: List[CreativeProject]) -> None:
        self._save("creative_projects.json", [p.to_dict() for p in projects])

    # ── runtime ──

    def get_runtime(self) -> Dict[str, Any]:
        raw = self._load("runtime.json", {})
        return raw if isinstance(raw, dict) else {}

    def save_runtime(self, data: Dict[str, Any]) -> None:
        self._save("runtime.json", data)

    def patch_runtime(self, **kwargs: Any) -> Dict[str, Any]:
        rt = self.get_runtime()
        rt.update(kwargs)
        rt["updated_ts"] = time.time()
        self.save_runtime(rt)
        return rt
