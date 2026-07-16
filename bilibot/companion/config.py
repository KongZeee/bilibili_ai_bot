"""Parse companion.* config section."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v)


def _list_str(v: Any) -> List[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


@dataclass
class LifeStateConfig:
    enabled: bool = True
    inject_into_replies: bool = True
    energy_default: int = 70


@dataclass
class ScheduleConfig:
    enabled: bool = True
    generate_time: str = "07:30"
    item_count: int = 8
    detail_lead_minutes: int = 15


@dataclass
class DreamConfig:
    enabled: bool = True
    generate_with_diary: bool = True


@dataclass
class DiaryConfig:
    enabled: bool = True
    time: str = "23:10"
    max_entries: int = 14
    offer_dynamic_draft: bool = False


@dataclass
class ExplorationConfig:
    enabled: bool = False
    min_interval_hours: float = 8.0
    max_results: int = 6
    interests: List[str] = field(default_factory=list)
    offer_dynamic_draft: bool = False


@dataclass
class NewsConfig:
    enabled: bool = False
    min_interval_hours: float = 6.0
    sources: List[str] = field(default_factory=list)


@dataclass
class CreativeConfig:
    enabled: bool = False
    max_active_projects: int = 2
    chars_per_session: int = 220
    inspiration_probability: float = 0.2
    offer_dynamic_draft: bool = False


@dataclass
class DynamicShareConfig:
    require_review: bool = True


@dataclass
class CompanionConfig:
    enabled: bool = False
    life_state: LifeStateConfig = field(default_factory=LifeStateConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    dream: DreamConfig = field(default_factory=DreamConfig)
    diary: DiaryConfig = field(default_factory=DiaryConfig)
    exploration: ExplorationConfig = field(default_factory=ExplorationConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    creative: CreativeConfig = field(default_factory=CreativeConfig)
    dynamic_share: DynamicShareConfig = field(default_factory=DynamicShareConfig)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "life_state": {
                "enabled": self.life_state.enabled,
                "inject_into_replies": self.life_state.inject_into_replies,
                "energy_default": self.life_state.energy_default,
            },
            "schedule": {
                "enabled": self.schedule.enabled,
                "generate_time": self.schedule.generate_time,
                "item_count": self.schedule.item_count,
                "detail_lead_minutes": self.schedule.detail_lead_minutes,
            },
            "dream": {
                "enabled": self.dream.enabled,
                "generate_with_diary": self.dream.generate_with_diary,
            },
            "diary": {
                "enabled": self.diary.enabled,
                "time": self.diary.time,
                "max_entries": self.diary.max_entries,
                "offer_dynamic_draft": self.diary.offer_dynamic_draft,
            },
            "exploration": {
                "enabled": self.exploration.enabled,
                "min_interval_hours": self.exploration.min_interval_hours,
                "max_results": self.exploration.max_results,
                "interests": list(self.exploration.interests),
                "offer_dynamic_draft": self.exploration.offer_dynamic_draft,
            },
            "news": {
                "enabled": self.news.enabled,
                "min_interval_hours": self.news.min_interval_hours,
                "sources": list(self.news.sources),
            },
            "creative": {
                "enabled": self.creative.enabled,
                "max_active_projects": self.creative.max_active_projects,
                "chars_per_session": self.creative.chars_per_session,
                "inspiration_probability": self.creative.inspiration_probability,
                "offer_dynamic_draft": self.creative.offer_dynamic_draft,
            },
            "dynamic_share": {
                "require_review": self.dynamic_share.require_review,
            },
        }


def load_companion_config(raw: Optional[Dict[str, Any]] = None) -> CompanionConfig:
    """Load companion config from full app/account raw config dict."""
    raw = raw or {}
    # Prefer top-level companion; allow features.companion as soft enable only when companion missing
    sec = raw.get("companion")
    if not isinstance(sec, dict):
        sec = {}
    features = raw.get("features") or {}
    enabled_default = False
    if "enabled" not in sec and isinstance(features, dict) and "companion" in features:
        enabled_default = _bool(features.get("companion"), False)

    life = sec.get("life_state") or {}
    schedule = sec.get("schedule") or {}
    dream = sec.get("dream") or {}
    diary = sec.get("diary") or {}
    exploration = sec.get("exploration") or {}
    news = sec.get("news") or {}
    creative = sec.get("creative") or {}
    share = sec.get("dynamic_share") or {}

    return CompanionConfig(
        enabled=_bool(sec.get("enabled"), enabled_default),
        life_state=LifeStateConfig(
            enabled=_bool(life.get("enabled"), True),
            inject_into_replies=_bool(life.get("inject_into_replies"), True),
            energy_default=max(0, min(100, _int(life.get("energy_default"), 70))),
        ),
        schedule=ScheduleConfig(
            enabled=_bool(schedule.get("enabled"), True),
            generate_time=_str(schedule.get("generate_time"), "07:30") or "07:30",
            item_count=max(4, min(16, _int(schedule.get("item_count"), 8))),
            detail_lead_minutes=max(0, min(120, _int(schedule.get("detail_lead_minutes"), 15))),
        ),
        dream=DreamConfig(
            enabled=_bool(dream.get("enabled"), True),
            generate_with_diary=_bool(dream.get("generate_with_diary"), True),
        ),
        diary=DiaryConfig(
            enabled=_bool(diary.get("enabled"), True),
            time=_str(diary.get("time"), "23:10") or "23:10",
            max_entries=max(3, min(60, _int(diary.get("max_entries"), 14))),
            offer_dynamic_draft=_bool(diary.get("offer_dynamic_draft"), False),
        ),
        exploration=ExplorationConfig(
            enabled=_bool(exploration.get("enabled"), False),
            min_interval_hours=max(1.0, _float(exploration.get("min_interval_hours"), 8.0)),
            max_results=max(1, min(20, _int(exploration.get("max_results"), 6))),
            interests=_list_str(exploration.get("interests")),
            offer_dynamic_draft=_bool(exploration.get("offer_dynamic_draft"), False),
        ),
        news=NewsConfig(
            enabled=_bool(news.get("enabled"), False),
            min_interval_hours=max(1.0, _float(news.get("min_interval_hours"), 6.0)),
            sources=_list_str(news.get("sources")),
        ),
        creative=CreativeConfig(
            enabled=_bool(creative.get("enabled"), False),
            max_active_projects=max(1, min(5, _int(creative.get("max_active_projects"), 2))),
            chars_per_session=max(60, min(1200, _int(creative.get("chars_per_session"), 220))),
            inspiration_probability=max(0.0, min(1.0, _float(creative.get("inspiration_probability"), 0.2))),
            offer_dynamic_draft=_bool(creative.get("offer_dynamic_draft"), False),
        ),
        dynamic_share=DynamicShareConfig(
            require_review=_bool(share.get("require_review"), True),
        ),
    )
