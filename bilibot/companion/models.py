"""Companion life data models (JSON-serializable dataclasses)."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


@dataclass
class LifeCondition:
    title: str = ""
    kind: str = ""  # health / fatigue / mood / other
    phase: str = ""
    mood: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "LifeCondition":
        d = d or {}
        return cls(
            title=str(d.get("title") or ""),
            kind=str(d.get("kind") or ""),
            phase=str(d.get("phase") or ""),
            mood=str(d.get("mood") or ""),
            note=str(d.get("note") or ""),
        )


@dataclass
class MotiveScore:
    """One ranked desire competing for the next autonomous act."""

    name: str
    score: float
    reason: str = ""
    suggested_action: str = ""  # browse_video / post_dynamic / reply / explore / rest / creative

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "score": float(self.score),
            "reason": self.reason,
            "suggested_action": self.suggested_action,
        }


@dataclass
class MotiveQueue:
    """Desire layer sitting above TaskRun slots.

    Scheduler / companion tick should call ``rank_motives`` then pick the top
    motive before instantiating proactive_video / dynamic_post / explore.
    This is intentionally lightweight — not a full planner.
    """

    motives: List[MotiveScore] = field(default_factory=list)
    updated_at: str = ""

    def top(self) -> Optional[MotiveScore]:
        if not self.motives:
            return None
        return max(self.motives, key=lambda m: float(m.score))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "motives": [m.to_dict() for m in self.motives],
            "updated_at": self.updated_at,
            "top": (self.top().to_dict() if self.top() else None),
        }


@dataclass
class SelfSnapshot:
    """Unified read model of "who I am right now" for generation paths.

    LifeState remains the durable JSON document; SelfSnapshot is the
    generation-facing projection. Prefer this type at begin_activity /
    dynamic / proactive prompt assembly so companion + memory_brain share
    one self surface.

    ``salient_recent`` items may carry ``|eid=<event_id>`` suffixes so
    claims stay evidence-linkable without a second store.
    """

    energy: int = 70
    mood_bias: str = "平稳"
    activity: str = ""
    message_seed: str = ""
    dream_afterglow: str = ""
    salient_recent: List[str] = field(default_factory=list)
    ongoing_threads: List[str] = field(default_factory=list)
    # Optional standing attitudes e.g. "UP:xxx=喜欢" — reserved for later.
    standing_attitudes: List[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "energy": int(self.energy),
            "mood_bias": self.mood_bias,
            "activity": self.activity,
            "message_seed": self.message_seed,
            "dream_afterglow": self.dream_afterglow,
            "salient_recent": list(self.salient_recent or [])[:8],
            "ongoing_threads": list(self.ongoing_threads or [])[:6],
            "standing_attitudes": list(self.standing_attitudes or [])[:8],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_life_state(cls, state: "LifeState") -> "SelfSnapshot":
        if state is None:
            return cls()
        return cls(
            energy=int(getattr(state, "energy", 70) or 70),
            mood_bias=str(getattr(state, "mood_bias", "") or "平稳"),
            activity=str(getattr(state, "activity", "") or ""),
            message_seed=str(getattr(state, "message_seed", "") or ""),
            dream_afterglow=str(getattr(state, "dream_afterglow", "") or ""),
            salient_recent=list(getattr(state, "salient_recent", None) or [])[:8],
            ongoing_threads=list(getattr(state, "ongoing_threads", None) or [])[:6],
            standing_attitudes=[],
            updated_at=str(getattr(state, "updated_at", "") or ""),
        )

    def prompt_block(self, *, max_salient: int = 4, max_threads: int = 4) -> str:
        """Compact Chinese block for injection into activity / dynamic prompts."""
        lines = [f"【自我快照】精力 {int(self.energy)}/100 · 心情 {self.mood_bias or '平稳'}"]
        if self.activity:
            lines.append(f"当前活动：{self.activity[:80]}")
        sal = [str(x).strip() for x in (self.salient_recent or []) if str(x or "").strip()]
        if sal:
            # Strip eid markers from human-facing lines but keep text.
            shown = []
            for item in sal[:max_salient]:
                shown.append(item.split("|eid=")[0].strip())
            lines.append("刚经历：" + "；".join(shown))
        thr = [str(x).strip() for x in (self.ongoing_threads or []) if str(x or "").strip()]
        if thr:
            lines.append("进行中：" + "；".join(thr[:max_threads]))
        if self.dream_afterglow:
            lines.append(f"梦境余韵：{self.dream_afterglow[:80]}")
        if self.message_seed:
            lines.append(f"想说的话：{self.message_seed[:60]}")
        return "\n".join(lines)

    def event_ids(self) -> List[str]:
        ids: List[str] = []
        for item in list(self.salient_recent or []) + list(self.ongoing_threads or []):
            s = str(item or "")
            if "|eid=" in s:
                eid = s.split("|eid=", 1)[-1].strip().split()[0]
                if eid and eid not in ids:
                    ids.append(eid)
        return ids


@dataclass
class LifeState:
    date: str = ""
    energy: int = 70
    sleep: str = ""
    mood_bias: str = "平稳"
    activity: str = ""
    message_seed: str = ""
    location: str = ""
    conditions: List[LifeCondition] = field(default_factory=list)
    dream_afterglow: str = ""
    # Short continuous self: recent closed activities for prompt injection.
    # Each item is a one-line human string, newest first, max ~8.
    # Optional suffix ``|eid=<memory_event_id>`` binds the claim to brain evidence.
    salient_recent: List[str] = field(default_factory=list)
    # Open threads e.g. "小说:《xx》续写中" / "追番:ATRI" — free text, max ~6.
    ongoing_threads: List[str] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "energy": int(self.energy),
            "sleep": self.sleep,
            "mood_bias": self.mood_bias,
            "activity": self.activity,
            "message_seed": self.message_seed,
            "location": self.location,
            "conditions": [c.to_dict() for c in self.conditions],
            "dream_afterglow": self.dream_afterglow,
            "salient_recent": list(self.salient_recent or [])[:8],
            "ongoing_threads": list(self.ongoing_threads or [])[:6],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "LifeState":
        d = d or {}
        conditions = [LifeCondition.from_dict(x) for x in (d.get("conditions") or []) if isinstance(x, dict)]
        energy = d.get("energy", 70)
        try:
            energy = max(0, min(100, int(energy)))
        except (TypeError, ValueError):
            energy = 70
        salient = [
            str(x).strip()
            for x in (d.get("salient_recent") or [])
            if str(x or "").strip()
        ][:8]
        threads = [
            str(x).strip()
            for x in (d.get("ongoing_threads") or [])
            if str(x or "").strip()
        ][:6]
        return cls(
            date=str(d.get("date") or ""),
            energy=energy,
            sleep=str(d.get("sleep") or ""),
            mood_bias=str(d.get("mood_bias") or "平稳"),
            activity=str(d.get("activity") or ""),
            message_seed=str(d.get("message_seed") or ""),
            location=str(d.get("location") or ""),
            conditions=conditions,
            dream_afterglow=str(d.get("dream_afterglow") or ""),
            salient_recent=salient,
            ongoing_threads=threads,
            updated_at=str(d.get("updated_at") or ""),
        )


@dataclass
class PlanItem:
    time: str = ""
    end: str = ""
    activity: str = ""
    mood: str = ""
    message_seed: str = ""
    basis: str = ""
    confidence: float = 0.6

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "PlanItem":
        d = d or {}
        try:
            conf = float(d.get("confidence", 0.6))
        except (TypeError, ValueError):
            conf = 0.6
        return cls(
            time=str(d.get("time") or ""),
            end=str(d.get("end") or ""),
            activity=str(d.get("activity") or ""),
            mood=str(d.get("mood") or ""),
            message_seed=str(d.get("message_seed") or ""),
            basis=str(d.get("basis") or ""),
            confidence=max(0.0, min(1.0, conf)),
        )


@dataclass
class DailyPlan:
    date: str = ""
    generated_at: str = ""
    source: str = "fallback"  # llm | fallback
    items: List[PlanItem] = field(default_factory=list)
    quality_score: int = 0
    raw: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "source": self.source,
            "items": [i.to_dict() for i in self.items],
            "quality_score": self.quality_score,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DailyPlan":
        d = d or {}
        items = [PlanItem.from_dict(x) for x in (d.get("items") or []) if isinstance(x, dict)]
        try:
            score = int(d.get("quality_score", 0))
        except (TypeError, ValueError):
            score = 0
        return cls(
            date=str(d.get("date") or ""),
            generated_at=str(d.get("generated_at") or ""),
            source=str(d.get("source") or "fallback"),
            items=items,
            quality_score=score,
            raw=str(d.get("raw") or ""),
        )


@dataclass
class StoryDetail:
    date: str = ""
    segment_key: str = ""
    window: str = ""
    summary: str = ""
    events: List[str] = field(default_factory=list)
    proactive_hooks: List[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "StoryDetail":
        d = d or {}
        events = [str(x) for x in (d.get("events") or []) if str(x).strip()]
        hooks = [str(x) for x in (d.get("proactive_hooks") or []) if str(x).strip()]
        return cls(
            date=str(d.get("date") or ""),
            segment_key=str(d.get("segment_key") or ""),
            window=str(d.get("window") or ""),
            summary=str(d.get("summary") or ""),
            events=events,
            proactive_hooks=hooks,
            generated_at=str(d.get("generated_at") or ""),
        )


@dataclass
class DreamFragment:
    text: str = ""
    weight: float = 1.0
    created_ts: float = 0.0
    source: str = ""
    date: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DreamFragment":
        d = d or {}
        try:
            w = float(d.get("weight", 1.0))
        except (TypeError, ValueError):
            w = 1.0
        try:
            ts = float(d.get("created_ts") or time.time())
        except (TypeError, ValueError):
            ts = time.time()
        return cls(
            text=str(d.get("text") or "").strip(),
            weight=max(0.0, w),
            created_ts=ts,
            source=str(d.get("source") or ""),
            date=str(d.get("date") or ""),
        )


@dataclass
class DreamRecord:
    date: str = ""
    generated_at: str = ""
    dream_type: str = ""
    content: str = ""
    afterglow: str = ""
    label: str = ""
    mood: str = ""
    energy_delta: int = 0
    factors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DreamRecord":
        d = d or {}
        try:
            delta = int(d.get("energy_delta", 0))
        except (TypeError, ValueError):
            delta = 0
        factors = [str(x) for x in (d.get("factors") or []) if str(x).strip()]
        return cls(
            date=str(d.get("date") or ""),
            generated_at=str(d.get("generated_at") or ""),
            dream_type=str(d.get("dream_type") or ""),
            content=str(d.get("content") or ""),
            afterglow=str(d.get("afterglow") or ""),
            label=str(d.get("label") or ""),
            mood=str(d.get("mood") or ""),
            energy_delta=max(-20, min(20, delta)),
            factors=factors,
        )


@dataclass
class DiaryEntry:
    date: str = ""
    generated_at: str = ""
    summary: str = ""
    body: str = ""
    share_seed: str = ""
    tags: List[str] = field(default_factory=list)
    dream_fragments: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DiaryEntry":
        d = d or {}
        return cls(
            date=str(d.get("date") or ""),
            generated_at=str(d.get("generated_at") or ""),
            summary=str(d.get("summary") or ""),
            body=str(d.get("body") or ""),
            share_seed=str(d.get("share_seed") or ""),
            tags=[str(x) for x in (d.get("tags") or []) if str(x).strip()],
            dream_fragments=[str(x) for x in (d.get("dream_fragments") or []) if str(x).strip()],
        )


@dataclass
class ExploreNote:
    id: str = ""
    created_at: str = ""
    query: str = ""
    motive: str = ""
    impression: str = ""
    self_link: str = ""
    should_share: bool = False
    items: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "web_search"  # web_search | news

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "ExploreNote":
        d = d or {}
        items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            created_at=str(d.get("created_at") or _now_iso()),
            query=str(d.get("query") or ""),
            motive=str(d.get("motive") or ""),
            impression=str(d.get("impression") or ""),
            self_link=str(d.get("self_link") or ""),
            should_share=bool(d.get("should_share", False)),
            items=items,
            source=str(d.get("source") or "web_search"),
        )


@dataclass
class CreativeChunk:
    at: str = ""
    text: str = ""
    chars: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "CreativeChunk":
        d = d or {}
        text = str(d.get("text") or "")
        try:
            chars = int(d.get("chars") or len(text))
        except (TypeError, ValueError):
            chars = len(text)
        return cls(at=str(d.get("at") or ""), text=text, chars=chars)


@dataclass
class CreativeProject:
    id: str = ""
    title: str = ""
    work_type: str = "短篇小说"
    premise: str = ""
    tone: str = ""
    status: str = "drafting"  # drafting | finished
    target_chars: int = 1200
    current_chars: int = 0
    outline: List[str] = field(default_factory=list)
    draft_chunks: List[CreativeChunk] = field(default_factory=list)
    next_hint: str = ""
    inspiration_source: str = ""
    created_at: str = ""
    updated_at: str = ""
    next_advance_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "work_type": self.work_type,
            "premise": self.premise,
            "tone": self.tone,
            "status": self.status,
            "target_chars": self.target_chars,
            "current_chars": self.current_chars,
            "outline": list(self.outline),
            "draft_chunks": [c.to_dict() for c in self.draft_chunks],
            "next_hint": self.next_hint,
            "inspiration_source": self.inspiration_source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_advance_at": self.next_advance_at,
        }

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "CreativeProject":
        d = d or {}
        chunks = [CreativeChunk.from_dict(x) for x in (d.get("draft_chunks") or []) if isinstance(x, dict)]
        try:
            target = int(d.get("target_chars", 1200))
        except (TypeError, ValueError):
            target = 1200
        try:
            current = int(d.get("current_chars", 0))
        except (TypeError, ValueError):
            current = 0
        try:
            next_at = float(d.get("next_advance_at") or 0)
        except (TypeError, ValueError):
            next_at = 0.0
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            title=str(d.get("title") or "未命名"),
            work_type=str(d.get("work_type") or "短篇小说"),
            premise=str(d.get("premise") or ""),
            tone=str(d.get("tone") or ""),
            status=str(d.get("status") or "drafting"),
            target_chars=max(300, min(8000, target)),
            current_chars=max(0, current),
            outline=[str(x) for x in (d.get("outline") or []) if str(x).strip()],
            draft_chunks=chunks,
            next_hint=str(d.get("next_hint") or ""),
            inspiration_source=str(d.get("inspiration_source") or ""),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            next_advance_at=next_at,
        )
