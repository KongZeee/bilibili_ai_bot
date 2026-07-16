"""CompanionLifeService — account-scoped living persona orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    import asyncio
except Exception:  # pragma: no cover
    asyncio = None  # type: ignore

from .config import CompanionConfig, load_companion_config
from .models import (
    CreativeChunk,
    CreativeProject,
    DailyPlan,
    DiaryEntry,
    DreamFragment,
    DreamRecord,
    ExploreNote,
    LifeState,
    PlanItem,
    StoryDetail,
)
from . import prompts as P
from .store import CompanionStore

logger = logging.getLogger("bilibot.companion")

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_BUSY_MARKERS = ("上班", "工作", "学习", "考试", "通勤", "会议", "赶稿", "赶ddl")
_IDLE_MARKERS = ("摸鱼", "休息", "闲逛", "刷", "放松", "发呆", "创作", "写作", "午睡")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_hhmm(s: str) -> Optional[int]:
    s = (s or "").strip().replace("：", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def _extract_json(text: str) -> Optional[Any]:
    if not text:
        return None
    text = text.strip()
    # strip fences
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # brace slice
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return None


def _fallback_plan_items(item_count: int = 8) -> List[PlanItem]:
    templates = [
        ("07:30", "08:15", "起床洗漱，慢慢清醒", "困倦", ""),
        ("08:15", "09:00", "随便吃点东西，刷几眼手机", "懒洋洋", ""),
        ("09:00", "11:30", "处理日常事务，偶尔摸鱼看 B 站", "平稳", "今天也想找点有意思的视频"),
        ("11:30", "12:30", "午饭与短暂放空", "放松", ""),
        ("12:30", "14:00", "午睡或发呆充电", "困倦", ""),
        ("14:00", "17:30", "继续刷感兴趣的内容，记点碎想法", "专注", ""),
        ("17:30", "19:00", "晚饭与晚间过渡", "轻松", ""),
        ("19:00", "22:00", "晚间娱乐，可能写点什么或发条动态念头", "活泼", "晚上适合分享一点碎碎念"),
        ("22:00", "23:30", "洗漱放松，准备入睡", "安静", ""),
        ("23:30", "07:00", "睡眠", "沉静", ""),
    ]
    n = max(4, min(item_count, len(templates)))
    return [
        PlanItem(time=a, end=b, activity=c, mood=d, message_seed=e, basis="routine", confidence=0.5)
        for a, b, c, d, e in templates[:n]
    ]


def _plan_quality(items: List[PlanItem]) -> int:
    if len(items) < 4:
        return 30
    score = 80
    minutes = []
    for it in items:
        s = _parse_hhmm(it.time)
        e = _parse_hhmm(it.end)
        if s is None or e is None:
            score -= 8
            continue
        if e == s:
            score -= 5
        minutes.append((s, e if e > s else e + 24 * 60, it))
    minutes.sort()
    for i in range(1, len(minutes)):
        if minutes[i][0] < minutes[i - 1][1] - 5:
            score -= 10
    if not any("睡" in (it.activity or "") for it in items):
        score -= 5
    return max(0, min(100, score))


def _effective_fragment_weight(f: DreamFragment, now: Optional[float] = None) -> float:
    now = now or time.time()
    age_h = max(0.0, (now - (f.created_ts or now)) / 3600.0)
    return float(f.weight) * (0.72 ** (age_h / 24.0))


class CompanionLifeService:
    """Per-account companion life facade."""

    def __init__(
        self,
        account_id: str,
        account_data_dir: str,
        *,
        config_loader=None,
        llm=None,
        persona_store=None,
        memory_brain=None,
        web_search=None,
        draft_store=None,
    ):
        self.account_id = account_id
        self.account_data_dir = account_data_dir
        self.config_loader = config_loader
        self.llm = llm
        self.persona_store = persona_store
        self.memory_brain = memory_brain
        self.web_search = web_search
        self.draft_store = draft_store
        self.store = CompanionStore(account_data_dir)
        self._cfg = self.reload_config()
        # 异步可重入保护：bool 在 await 间隙会误判；用 asyncio.Lock
        self._tick_lock = None
        try:
            if asyncio is not None:
                self._tick_lock = asyncio.Lock()
        except Exception:
            self._tick_lock = None
        self._tick_busy = False  # sync fallback when no event loop yet

    # ── config ──

    def reload_config(self) -> CompanionConfig:
        raw = {}
        if self.config_loader is not None:
            try:
                raw = self.config_loader.get_raw_config() or {}
            except Exception:
                raw = {}
        self._cfg = load_companion_config(raw)
        return self._cfg

    @property
    def config(self) -> CompanionConfig:
        return self._cfg

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    # ── persona helpers ──

    def _resolve_persona(self):
        if not self.persona_store:
            return None
        try:
            if hasattr(self.persona_store, "resolve_persona"):
                return self.persona_store.resolve_persona(account_id=self.account_id)
            return self.persona_store.get_current()
        except Exception:
            try:
                return self.persona_store.get_current()
            except Exception:
                return None

    def _persona_bits(self) -> Dict[str, Any]:
        p = self._resolve_persona()
        if not p:
            return {
                "id": "",
                "name": "Bot",
                "base_prompt": "",
                "interests": [],
                "life_background": "",
            }
        interests = list(getattr(p, "interests", None) or [])
        if not interests and getattr(p, "tags", None):
            interests = list(p.tags or [])
        # merge config exploration interests
        for x in self._cfg.exploration.interests:
            if x not in interests:
                interests.append(x)
        # revive proactive.interest_keywords
        try:
            raw = self.config_loader.get_raw_config() if self.config_loader else {}
            kws = (raw.get("proactive") or {}).get("interest_keywords") or []
            if isinstance(kws, str):
                kws = [x.strip() for x in kws.split(",") if x.strip()]
            for x in kws:
                if x and x not in interests:
                    interests.append(str(x))
        except Exception:
            pass
        return {
            "id": getattr(p, "id", "") or "",
            "name": getattr(p, "name", "") or "Bot",
            "base_prompt": getattr(p, "base_prompt", "") or "",
            "interests": interests,
            "life_background": getattr(p, "life_background", "") or "",
            "diary_rules": getattr(p, "diary_rules", "") or "",
            "creative_rules": getattr(p, "creative_rules", "") or "",
        }

    async def _llm_text(self, system: str, user: str, max_tokens: int = 900) -> Optional[str]:
        if not self.llm or not hasattr(self.llm, "generate"):
            return None
        try:
            return await self.llm.generate(
                prompt=user,
                system_prompt=system,
                max_tokens=max_tokens,
                temperature=0.85,
            )
        except Exception as e:
            logger.warning("[%s] companion LLM failed: %s", self.account_id, type(e).__name__)
            return None

    async def _archive_text(
        self,
        *,
        source_type: str,
        event_type: str,
        text: str,
        title: str = "",
        idempotency_key: str = "",
        importance: float = 0.55,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.memory_brain or not text:
            return
        try:
            from bilibot.memory_brain.ingestion import text_observation

            # 日记/梦境/日程等同日可重写：键带内容指纹，避免 IdempotencyConflict 刷警告
            base_key = idempotency_key or f"{source_type}:{_today()}:{uuid.uuid4().hex[:8]}"
            if source_type in {"diary", "dream", "life_plan", "creative", "web_reference"}:
                digest = hashlib.sha1(str(text).encode("utf-8", errors="ignore")).hexdigest()[:10]
                base_key = f"{base_key}:{digest}"

            env = text_observation(
                account_id=self.account_id,
                idempotency_key=base_key,
                source_type=source_type,
                event_type=event_type,
                text=text,
                title=title,
                persona_id=self._persona_bits().get("id") or "",
                scene="companion",
                metadata=metadata or {},
                importance=importance,
            )
            if hasattr(self.memory_brain, "archive_observation_async"):
                await self.memory_brain.archive_observation_async(env)
            elif hasattr(self.memory_brain, "archive_observation"):
                self.memory_brain.archive_observation(env)
        except Exception as e:
            msg = str(e)
            # 同内容重入可静默；不同内容冲突仅 debug
            if "already exists" in msg or "Idempotency" in type(e).__name__:
                logger.debug("[%s] companion archive skip: %s", self.account_id, msg[:160])
            else:
                logger.warning("[%s] companion archive failed: %s", self.account_id, e)

    def _offer_draft(self, content: str, created_by: str) -> Optional[str]:
        if not content or not content.strip():
            return None
        if not self.draft_store:
            return None
        try:
            # DynamicDraftStore.create returns draft_id str; default status=awaiting_review
            draft_id = self.draft_store.create(
                account_id=self.account_id,
                persona_id=self._persona_bits().get("id") or "",
                task_id=f"companion:{created_by}:{uuid.uuid4().hex[:8]}",
                content=content.strip()[:2000],
                created_by=created_by,
                safety_snapshot={"source": "companion", "kind": created_by},
            )
            return str(draft_id) if draft_id else None
        except Exception as e:
            logger.warning("[%s] companion draft offer failed: %s", self.account_id, e)
            return None

    # ── public snapshot / inject ──

    def get_prompt_surface(self) -> str:
        """Short block for system/user prompt injection."""
        if not self.enabled or not self._cfg.life_state.enabled:
            return ""
        if not self._cfg.life_state.inject_into_replies:
            return ""
        state = self.store.get_life_state()
        plan = self.store.get_daily_plan()
        detail = self.store.get_story_detail()
        # current item
        current = state.activity or ""
        seed = state.message_seed or ""
        if plan.items:
            now_m = datetime.now().hour * 60 + datetime.now().minute
            for it in plan.items:
                s = _parse_hhmm(it.time)
                e = _parse_hhmm(it.end)
                if s is None:
                    continue
                if e is None:
                    e = s + 60
                end = e if e > s else e + 24 * 60
                cur = now_m if now_m >= s or e > s else now_m + 24 * 60
                if s <= cur < end or (e <= s and (now_m >= s or now_m < e)):
                    current = it.activity or current
                    seed = it.message_seed or seed
                    break
        near = []
        for it in plan.items[:6]:
            if it.activity:
                near.append(f"{it.time} {it.activity}")
        lines = [
            f"【今日生活】精力 {state.energy}/100",
            f"心情倾向：{state.mood_bias or '平稳'}",
        ]
        if state.sleep:
            lines.append(f"睡眠：{state.sleep}")
        if current:
            lines.append(f"当前：{current}")
        if seed:
            lines.append(f"念头：{seed}")
        if state.dream_afterglow:
            lines.append(f"梦境余韵：{state.dream_afterglow[:80]}")
        if detail.summary and detail.date == _today():
            lines.append(f"时段细节：{detail.summary[:100]}")
        if near:
            lines.append("近时日程：" + "；".join(near[:4]))
        return "\n".join(lines)

    def get_topic_seeds(self) -> List[str]:
        if not self.enabled:
            return []
        seeds: List[str] = []
        state = self.store.get_life_state()
        if state.message_seed:
            seeds.append(state.message_seed)
        detail = self.store.get_story_detail()
        if detail.date == _today():
            seeds.extend(detail.proactive_hooks[:3])
        plan = self.store.get_daily_plan()
        for it in plan.items:
            if it.message_seed:
                seeds.append(it.message_seed)
        # diary / explore soft seeds
        diaries = self.store.get_diaries()
        if diaries and diaries[0].date == _today() and diaries[0].share_seed:
            seeds.append(diaries[0].share_seed)
        notes = self.store.get_explore_notes()
        for n in notes[:2]:
            if n.query:
                seeds.append(n.query)
            if n.impression:
                seeds.append(n.impression[:40])
        # unique preserve order
        seen = set()
        out = []
        for s in seeds:
            s = (s or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out[:8]

    def get_interest_keywords(self, limit: int = 12) -> List[str]:
        """Keywords for soft-biasing video feed / exploration (persona + config + notes)."""
        if not self.enabled:
            return []
        bits = self._persona_bits()
        keys: List[str] = list(bits.get("interests") or [])
        for s in self.get_topic_seeds():
            # keep short tokens
            for part in re.split(r"[\s,，、/|]+", s):
                part = part.strip()
                if 1 < len(part) <= 12:
                    keys.append(part)
        for n in self.store.get_explore_notes()[:5]:
            if n.query:
                keys.append(n.query[:20])
        seen = set()
        out: List[str] = []
        for k in keys:
            k = (k or "").strip()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(k)
            if len(out) >= limit:
                break
        return out

    def wants_browse_bilibili_now(self, now: Optional[datetime] = None) -> bool:
        """True if current companion plan activity looks like browsing Bilibili."""
        if not self.enabled:
            return False
        now = now or datetime.now()
        plan = self.store.get_daily_plan()
        if plan.date != _today() or not plan.items:
            state = self.store.get_life_state()
            text = f"{state.activity or ''} {state.message_seed or ''}"
            return self._text_looks_like_browse(text)
        now_m = now.hour * 60 + now.minute
        lead = max(0, int(self._cfg.schedule.detail_lead_minutes or 0))
        markers = ("B站", "b站", "刷", "看视频", "摸鱼", "推荐", "热门", "娱乐", "追番", "二创")
        for it in plan.items:
            s = _parse_hhmm(it.time)
            e = _parse_hhmm(it.end)
            if s is None:
                continue
            if e is None:
                e = s + 60
            start_lead = s - lead
            if e > s:
                due = start_lead <= now_m < e
            else:
                due = now_m >= start_lead or now_m < e
            if not due:
                continue
            text = f"{it.activity or ''} {it.message_seed or ''} {it.mood or ''}"
            if self._text_looks_like_browse(text) or any(m in text for m in markers):
                return True
        return False

    @staticmethod
    def _text_looks_like_browse(text: str) -> bool:
        t = text or ""
        hard = ("B站", "b站", "看视频", "刷视频", "推荐流", "热门视频")
        soft = ("刷", "摸鱼", "娱乐", "追更", "二创", "直播")
        if any(m in t for m in hard):
            return True
        return any(m in t for m in soft)

    def score_video_candidate(self, title: str = "", tags: Optional[List[str]] = None, desc: str = "") -> float:
        """Soft relevance score in [0, 1+] for ranking feed candidates."""
        if not self.enabled:
            return 0.0
        keys = self.get_interest_keywords()
        if not keys:
            return 0.0
        blob = f"{title or ''} {' '.join(tags or [])} {desc or ''}".lower()
        if not blob.strip():
            return 0.0
        score = 0.0
        for k in keys:
            kl = k.lower()
            if kl and kl in blob:
                score += 1.0
            # partial Chinese: any 2-char window
            if len(k) >= 2 and k[:2] in (title or ""):
                score += 0.35
        # slight boost when schedule wants browse now
        if self.wants_browse_bilibili_now() and score > 0:
            score += 0.5
        return score

    def rank_video_candidates(self, videos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Stable soft-rank: interest match first, preserve relative order among ties."""
        if not self.enabled or not videos:
            return list(videos or [])
        keyed = []
        for i, v in enumerate(videos):
            if not isinstance(v, dict):
                continue
            title = str(v.get("title") or "")
            # popular/recommend payloads vary
            tags = v.get("tag") or v.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            desc = str(v.get("desc") or v.get("description") or "")
            sc = self.score_video_candidate(title=title, tags=list(tags), desc=desc)
            keyed.append((-sc, i, v))
        keyed.sort(key=lambda x: (x[0], x[1]))
        return [v for _, _, v in keyed]

    def pick_dynamic_topic(self, configured_topics: Optional[List[str]] = None) -> Optional[str]:
        """Prefer companion seeds; optionally blend with configured topic pool."""
        if not self.enabled:
            return None
        seeds = self.get_topic_seeds()
        pool = [str(t).strip() for t in (configured_topics or []) if str(t).strip()]
        # 60% seed when available
        if seeds and (not pool or random.random() < 0.65):
            return seeds[0] if len(seeds) == 1 else random.choice(seeds[:3])
        if pool:
            return random.choice(pool)
        return seeds[0] if seeds else None

    def build_proactive_context_block(self) -> str:
        """Short block for dynamic / proactive-comment system or user prompts."""
        if not self.enabled:
            return ""
        surface = self.get_prompt_surface()
        seeds = self.get_topic_seeds()
        parts = []
        if surface:
            parts.append(surface)
        if seeds:
            parts.append("【当前想聊/分享的念头】" + "；".join(seeds[:4]))
        state = self.store.get_life_state()
        if state.dream_afterglow and "梦境余韵" not in (surface or ""):
            parts.append(f"【梦境余韵】{state.dream_afterglow[:80]}")
        return "\n".join(parts)

    def on_proactive_video_finished(
        self,
        *,
        title: str = "",
        score: float = 0,
        mood: str = "",
        review: str = "",
        comment: str = "",
        bvid: str = "",
    ) -> None:
        """Feedback from operational proactive-video into living state (soft)."""
        if not self.enabled:
            return
        try:
            state = self.ensure_life_state()
            # energy: watching costs a bit; high score slightly recovers curiosity
            delta = -2
            try:
                sc = float(score)
            except (TypeError, ValueError):
                sc = 0.0
            if sc >= 8:
                delta = 1
            elif sc >= 6:
                delta = -1
            elif sc > 0:
                delta = -3
            state.energy = max(0, min(100, int(state.energy) + delta))
            if mood:
                state.mood_bias = str(mood)[:20]
            # activity echo
            short_title = (title or "一个视频")[:40]
            state.activity = f"刚看了《{short_title}》"
            if review:
                state.message_seed = str(review)[:60]
            elif comment:
                state.message_seed = str(comment)[:60]
            state.updated_at = _now_iso()
            self.store.save_life_state(state)
            # fragment pool: keep concrete words from title
            if title:
                pool = self.store.get_dream_fragments()
                pool.append(
                    DreamFragment(
                        text=short_title[:28],
                        weight=0.9,
                        created_ts=time.time(),
                        source="proactive_video",
                        date=_today(),
                    )
                )
                self.store.save_dream_fragments(self._normalize_fragments(pool))
            self.store.patch_runtime(
                last_proactive_video_at=_now_iso(),
                last_proactive_video_bvid=bvid or "",
                last_proactive_video_score=sc,
            )
            logger.info(
                "[%s] companion feedback from video 《%s》 score=%s energy→%s",
                self.account_id,
                short_title,
                sc,
                state.energy,
            )
        except Exception as e:
            logger.warning("[%s] on_proactive_video_finished failed: %s", self.account_id, e)

    def on_dynamic_posted(self, content: str = "", topic: str = "") -> None:
        if not self.enabled:
            return
        try:
            state = self.ensure_life_state()
            state.energy = max(0, min(100, int(state.energy) - 1))
            if topic:
                state.message_seed = str(topic)[:60]
            elif content:
                state.message_seed = str(content)[:60]
            state.activity = "刚发了条动态"
            state.updated_at = _now_iso()
            self.store.save_life_state(state)
            self.store.patch_runtime(last_dynamic_at=_now_iso())
        except Exception as e:
            logger.warning("[%s] on_dynamic_posted failed: %s", self.account_id, e)

    def get_status_snapshot(self) -> Dict[str, Any]:
        state = self.store.get_life_state()
        plan = self.store.get_daily_plan()
        detail = self.store.get_story_detail()
        dream = self.store.get_latest_dream()
        diaries = self.store.get_diaries()
        notes = self.store.get_explore_notes()
        projects = self.store.get_projects()
        return {
            "enabled": self.enabled,
            "config": self._cfg.to_dict(),
            "life_state": state.to_dict(),
            "daily_plan": plan.to_dict(),
            "story_detail": detail.to_dict(),
            "latest_dream": dream.to_dict() if dream else None,
            "diary_count": len(diaries),
            "latest_diary": diaries[0].to_dict() if diaries else None,
            "explore_note_count": len(notes),
            "latest_explore_note": notes[0].to_dict() if notes else None,
            "project_count": len(projects),
            "projects": [p.to_dict() for p in projects[:10]],
            "prompt_surface": self.get_prompt_surface(),
            "topic_seeds": self.get_topic_seeds(),
            "interest_keywords": self.get_interest_keywords(),
            "wants_browse_now": self.wants_browse_bilibili_now(),
            "runtime": self.store.get_runtime(),
        }

    # ── life state ──

    def ensure_life_state(self) -> LifeState:
        state = self.store.get_life_state()
        today = _today()
        if state.date != today:
            # day roll
            dream = self.store.get_latest_dream()
            energy = self._cfg.life_state.energy_default
            mood = "平稳"
            afterglow = ""
            sleep = "正常"
            if dream and dream.date:
                # apply residual from last dream if recent
                energy = max(0, min(100, energy + int(dream.energy_delta or 0)))
                mood = dream.mood or mood
                afterglow = dream.afterglow or ""
            state = LifeState(
                date=today,
                energy=energy,
                sleep=sleep,
                mood_bias=mood,
                activity="",
                message_seed="",
                conditions=[],
                dream_afterglow=afterglow,
                updated_at=_now_iso(),
            )
            self.store.save_life_state(state)
        return state

    def _sync_state_from_plan(self, plan: DailyPlan, state: Optional[LifeState] = None) -> LifeState:
        state = state or self.ensure_life_state()
        now_m = datetime.now().hour * 60 + datetime.now().minute
        current = None
        for it in plan.items:
            s = _parse_hhmm(it.time)
            e = _parse_hhmm(it.end)
            if s is None:
                continue
            if e is None:
                e = s + 60
            in_seg = False
            if e > s:
                in_seg = s <= now_m < e
            else:
                in_seg = now_m >= s or now_m < e
            if in_seg:
                current = it
                break
        if current:
            state.activity = current.activity
            state.message_seed = current.message_seed
            if current.mood:
                state.mood_bias = current.mood
        state.updated_at = _now_iso()
        self.store.save_life_state(state)
        return state

    def apply_activity_energy_delta(self, delta: int, reason: str = "") -> LifeState:
        state = self.ensure_life_state()
        state.energy = max(0, min(100, int(state.energy) + int(delta)))
        state.updated_at = _now_iso()
        self.store.save_life_state(state)
        if reason:
            logger.debug("[%s] energy %+d (%s) -> %s", self.account_id, delta, reason, state.energy)
        return state

    # ── schedule ──

    async def ensure_daily_plan(self, force: bool = False) -> DailyPlan:
        self.ensure_life_state()
        plan = self.store.get_daily_plan()
        today = _today()
        if not force and plan.date == today and plan.items:
            self._sync_state_from_plan(plan)
            return plan

        if not self._cfg.schedule.enabled:
            plan = DailyPlan(
                date=today,
                generated_at=_now_iso(),
                source="fallback",
                items=_fallback_plan_items(self._cfg.schedule.item_count),
                quality_score=50,
            )
            self.store.save_daily_plan(plan)
            self._sync_state_from_plan(plan)
            return plan

        bits = self._persona_bits()
        state = self.store.get_life_state()
        now = datetime.now()
        system, user = P.build_daily_plan_prompt(
            date=today,
            weekday=_WEEKDAYS[now.weekday()],
            persona_name=bits["name"],
            persona_prompt=bits["base_prompt"],
            life_background=bits.get("life_background") or "",
            interests=bits.get("interests") or [],
            energy=state.energy,
            mood_bias=state.mood_bias,
            sleep=state.sleep,
            dream_afterglow=state.dream_afterglow,
            item_count=self._cfg.schedule.item_count,
        )
        raw = await self._llm_text(system, user, max_tokens=1200)
        items: List[PlanItem] = []
        source = "fallback"
        if raw:
            data = _extract_json(raw)
            rows = []
            if isinstance(data, dict):
                rows = data.get("schedule") or data.get("items") or []
            elif isinstance(data, list):
                rows = data
            for row in rows:
                if isinstance(row, dict) and (row.get("activity") or row.get("time")):
                    items.append(PlanItem.from_dict(row))
            if items:
                source = "llm"
        if len(items) < 4:
            items = _fallback_plan_items(self._cfg.schedule.item_count)
            source = "fallback"
        score = _plan_quality(items)
        if score < 55 and source == "llm":
            # one soft retry
            raw2 = await self._llm_text(system, user + "\n上次时段质量偏低，请避免重叠并补全早晚。", max_tokens=1200)
            if raw2:
                data = _extract_json(raw2)
                rows = (data.get("schedule") if isinstance(data, dict) else data) or []
                items2 = [PlanItem.from_dict(r) for r in rows if isinstance(r, dict)]
                if _plan_quality(items2) > score and len(items2) >= 4:
                    items, raw, score = items2, raw2, _plan_quality(items2)

        plan = DailyPlan(
            date=today,
            generated_at=_now_iso(),
            source=source,
            items=items,
            quality_score=score,
            raw=(raw or "")[:4000],
        )
        self.store.save_daily_plan(plan)
        self._sync_state_from_plan(plan)
        self.store.patch_runtime(last_plan_at=_now_iso(), plan_source=source)
        await self._archive_text(
            source_type="life_plan",
            event_type="daily_plan",
            text=P.format_plan_summary([i.to_dict() for i in items]),
            title=f"日程 {today}",
            idempotency_key=f"daily_plan:{today}",
            importance=0.45,
            metadata={"source": source, "quality": score},
        )
        return plan

    async def ensure_detail_enhancement(self) -> Optional[StoryDetail]:
        if not self.enabled or not self._cfg.schedule.enabled:
            return None
        plan = self.store.get_daily_plan()
        if plan.date != _today() or not plan.items:
            return None
        lead = self._cfg.schedule.detail_lead_minutes
        now_m = datetime.now().hour * 60 + datetime.now().minute
        target: Optional[PlanItem] = None
        for it in plan.items:
            s = _parse_hhmm(it.time)
            e = _parse_hhmm(it.end)
            if s is None:
                continue
            if e is None:
                e = s + 60
            # due if in lead window or inside segment
            start_lead = s - lead
            if e > s:
                due = start_lead <= now_m < e
            else:
                due = now_m >= start_lead or now_m < e
            if due:
                target = it
                break
        if not target:
            return self.store.get_story_detail()

        seg_key = f"{plan.date}:{target.time}-{target.end}"
        existing = self.store.get_story_detail()
        if existing.segment_key == seg_key and existing.summary:
            return existing

        bits = self._persona_bits()
        state = self.store.get_life_state()
        window = f"{target.time}-{target.end}"
        system, user = P.build_detail_prompt(
            window=window,
            activity=target.activity,
            mood=target.mood,
            persona_name=bits["name"],
            energy=state.energy,
        )
        raw = await self._llm_text(system, user, max_tokens=500)
        summary = target.activity
        events: List[str] = []
        hooks: List[str] = []
        if raw:
            data = _extract_json(raw)
            if isinstance(data, dict):
                summary = str(data.get("summary") or summary)
                events = [str(x) for x in (data.get("events") or []) if str(x).strip()][:5]
                hooks = [str(x) for x in (data.get("proactive_hooks") or []) if str(x).strip()][:3]
        if target.message_seed and target.message_seed not in hooks:
            hooks.append(target.message_seed)
        detail = StoryDetail(
            date=plan.date,
            segment_key=seg_key,
            window=window,
            summary=summary,
            events=events,
            proactive_hooks=hooks,
            generated_at=_now_iso(),
        )
        self.store.save_story_detail(detail)
        state.activity = target.activity
        state.message_seed = hooks[0] if hooks else target.message_seed
        state.updated_at = _now_iso()
        self.store.save_life_state(state)
        return detail

    # ── dream + diary ──

    def _normalize_fragments(self, items: List[DreamFragment]) -> List[DreamFragment]:
        now = time.time()
        cleaned: List[DreamFragment] = []
        seen = set()
        for f in items:
            t = (f.text or "").strip()
            if not t or len(t) > 40:
                continue
            key = t[:16]
            if key in seen:
                continue
            w = _effective_fragment_weight(f, now)
            if w < 0.12:
                continue
            seen.add(key)
            cleaned.append(
                DreamFragment(text=t, weight=w, created_ts=f.created_ts or now, source=f.source, date=f.date)
            )
        cleaned.sort(key=lambda x: _effective_fragment_weight(x, now), reverse=True)
        return cleaned[:48]

    async def generate_dream(self, force: bool = False) -> Optional[DreamRecord]:
        if not self.enabled or not self._cfg.dream.enabled:
            return None
        today = _today()
        existing = self.store.get_latest_dream()
        if not force and existing and existing.date == today and existing.content:
            return existing
        bits = self._persona_bits()
        frags = self._normalize_fragments(self.store.get_dream_fragments())
        frag_texts = [f.text for f in frags[:10]]
        plan = self.store.get_daily_plan()
        plan_sum = P.format_plan_summary([i.to_dict() for i in plan.items])
        diaries = self.store.get_diaries()
        diary_hint = diaries[0].summary if diaries else ""
        system, user = P.build_dream_prompt(
            persona_name=bits["name"],
            persona_prompt=bits["base_prompt"],
            fragments=frag_texts,
            plan_summary=plan_sum,
            diary_hint=diary_hint,
        )
        raw = await self._llm_text(system, user, max_tokens=1000)
        dream = None
        if raw:
            data = _extract_json(raw)
            if isinstance(data, dict) and data.get("content"):
                dream = DreamRecord.from_dict({**data, "date": today, "generated_at": _now_iso()})
        if not dream:
            dream = DreamRecord(
                date=today,
                generated_at=_now_iso(),
                dream_type="温柔日常",
                content="梦里好像还在刷手机，画面碎成几片光斑，醒来只记得一点说不清的安心。",
                afterglow="醒来有点恍惚，很快被现实拉回。",
                label="碎光",
                mood="恍惚",
                energy_delta=-2,
                factors=["光斑", "手机", "安静"],
            )
        self.store.save_latest_dream(dream)
        # merge factors into fragment pool
        pool = self.store.get_dream_fragments()
        now = time.time()
        for fac in dream.factors:
            pool.append(
                DreamFragment(text=fac, weight=1.2, created_ts=now, source="dream", date=today)
            )
        self.store.save_dream_fragments(self._normalize_fragments(pool))
        # update life afterglow
        state = self.ensure_life_state()
        state.dream_afterglow = dream.afterglow
        if dream.mood:
            state.mood_bias = dream.mood
        state.energy = max(0, min(100, state.energy + int(dream.energy_delta or 0)))
        state.updated_at = _now_iso()
        self.store.save_life_state(state)
        await self._archive_text(
            source_type="dream",
            event_type="dream",
            text=dream.content,
            title=dream.label or f"梦境 {today}",
            idempotency_key=f"dream:{today}",
            importance=0.5,
            metadata={"mood": dream.mood, "afterglow": dream.afterglow},
        )
        self.store.patch_runtime(last_dream_at=_now_iso())
        return dream

    async def generate_diary(self, force: bool = False) -> Optional[DiaryEntry]:
        if not self.enabled or not self._cfg.diary.enabled:
            return None
        today = _today()
        diaries = self.store.get_diaries()
        if not force and any(d.date == today for d in diaries):
            return next(d for d in diaries if d.date == today)

        # optional dream first
        dream = None
        if self._cfg.dream.enabled and self._cfg.dream.generate_with_diary:
            dream = await self.generate_dream(force=False)

        bits = self._persona_bits()
        state = self.ensure_life_state()
        plan = self.store.get_daily_plan()
        plan_sum = P.format_plan_summary([i.to_dict() for i in plan.items])
        evidence_parts = [plan_sum]
        detail = self.store.get_story_detail()
        if detail.summary:
            evidence_parts.append("时段：" + detail.summary)
        if detail.events:
            evidence_parts.append("事件：" + "；".join(detail.events[:4]))
        # soft recall from memory if available
        if self.memory_brain and hasattr(self.memory_brain, "search_memories"):
            try:
                hits = await self.memory_brain.search_memories(
                    query=f"{today} 今天 看了 评论 动态",
                    limit=5,
                )
                if isinstance(hits, list):
                    for h in hits[:5]:
                        if isinstance(h, dict):
                            evidence_parts.append(str(h.get("summary") or h.get("text") or "")[:200])
                        else:
                            evidence_parts.append(str(h)[:200])
            except Exception:
                pass
        dream_sum = ""
        if dream and dream.content:
            dream_sum = f"{dream.label}: {dream.afterglow or dream.content[:120]}"
        system, user = P.build_diary_prompt(
            date=today,
            persona_name=bits["name"],
            persona_prompt=bits["base_prompt"] + (("\n" + bits["diary_rules"]) if bits.get("diary_rules") else ""),
            plan_summary=plan_sum,
            evidence="\n".join(x for x in evidence_parts if x),
            dream_summary=dream_sum,
            energy=state.energy,
            mood_bias=state.mood_bias,
        )
        raw = await self._llm_text(system, user, max_tokens=1100)
        entry = None
        if raw:
            data = _extract_json(raw)
            if isinstance(data, dict) and (data.get("body") or data.get("summary")):
                entry = DiaryEntry.from_dict({**data, "date": today, "generated_at": _now_iso()})
        if not entry:
            entry = DiaryEntry(
                date=today,
                generated_at=_now_iso(),
                summary="平淡的一天",
                body=f"今天整体还算平稳。精力大概在 {state.energy} 左右，按自己的节奏过完了一天。",
                share_seed="",
                tags=["日常"],
                dream_fragments=[],
            )
        # prepend diary list
        diaries = [entry] + [d for d in diaries if d.date != today]
        diaries = diaries[: self._cfg.diary.max_entries]
        self.store.save_diaries(diaries)
        # fragments
        pool = self.store.get_dream_fragments()
        now = time.time()
        for t in entry.dream_fragments:
            pool.append(DreamFragment(text=t, weight=1.0, created_ts=now, source="diary", date=today))
        self.store.save_dream_fragments(self._normalize_fragments(pool))
        await self._archive_text(
            source_type="diary",
            event_type="diary",
            text=entry.body or entry.summary,
            title=f"日记 {today}",
            idempotency_key=f"diary:{today}",
            importance=0.6,
            metadata={"share_seed": entry.share_seed, "tags": entry.tags},
        )
        if self._cfg.diary.offer_dynamic_draft and entry.share_seed:
            self._offer_draft(entry.share_seed, created_by="companion_diary")
        self.store.patch_runtime(last_diary_at=_now_iso(), diary_date=today)
        # slight evening energy drain
        self.apply_activity_energy_delta(-3, "diary")
        return entry

    # ── exploration ──

    def _exploration_due(self) -> bool:
        if not self._cfg.exploration.enabled:
            return False
        rt = self.store.get_runtime()
        last = rt.get("last_explore_ts") or 0
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0.0
        min_h = self._cfg.exploration.min_interval_hours
        return (time.time() - last) >= min_h * 3600

    def _looks_idle(self) -> bool:
        state = self.store.get_life_state()
        text = f"{state.activity} {state.mood_bias}"
        if any(m in text for m in _BUSY_MARKERS):
            return False
        if any(m in text for m in _IDLE_MARKERS):
            return True
        return 35 <= int(state.energy) <= 85

    # 明显不可检索 / 虚构日常类 query
    _BAD_QUERY_PATTERNS = (
        re.compile(r"今天.*(做了|干了|在干|过得|发生)"),
        re.compile(r"(做了什么|在干嘛|在干什么|值得分享)"),
        re.compile(r"^(我|你|他|她)的(一天|日程|生活)"),
        re.compile(r"现在.*(在哪|在干|怎么样)"),
    )

    def _query_is_searchable(self, query: str) -> bool:
        q = (query or "").strip()
        if len(q) < 4 or len(q) > 80:
            return False
        for pat in self._BAD_QUERY_PATTERNS:
            if pat.search(q):
                return False
        # 至少像「实体/领域 + 信息意图」：含兴趣词或常见检索后缀
        info_markers = (
            "是什么", "百科", "设定", "剧情", "教程", "入门", "推荐", "新闻",
            "热门", "历史", "背景", "玩法", "攻略", "评价", "百科", "wiki",
            "2024", "2025", "2026", "最新", "B站", "bilibili",
        )
        interests = self.get_interest_keywords(limit=20)
        if any(m in q for m in info_markers):
            return True
        if any(k and k in q for k in interests):
            return True
        # 含中英文专有名词形态（2+ 连续汉字或英文词）且非纯口语
        if re.search(r"[A-Za-z]{3,}|\d{4}|[一-鿿]{2,}", q) and "？" not in q[-1:]:
            # 拒绝纯人称+动词
            if re.fullmatch(r"[一-鿿A-Za-z]{1,8}(今天|现在).+", q):
                return False
            return True
        return False

    def _fallback_explore_query(self, bits: Dict[str, Any]) -> Tuple[str, str]:
        interests = list(bits.get("interests") or []) or ["B站", "动漫", "科技"]
        name = bits.get("name") or "角色"
        templates = [
            ("{kw} 是什么 百科", "想搞清楚这个概念"),
            ("{kw} 入门 推荐", "想找点靠谱的入门资料"),
            ("{kw} 2025 最新 动态", "看看最近有没有新消息"),
            ("{kw} B站 相关 讨论", "想知道大家怎么聊这个"),
            ("{kw} 设定 背景", "补一点世界观/背景"),
        ]
        kw = random.choice(interests[:8])
        # 避免把人设名单独当成「今天干了啥」
        if kw in {name, "夏生"} and len(interests) > 1:
            kw = random.choice([x for x in interests if x not in {name, "夏生"}] or interests)
        tpl, motive = random.choice(templates)
        return tpl.format(kw=kw), motive

    async def maybe_explore(self, force: bool = False) -> Optional[ExploreNote]:
        if not self.enabled or not self._cfg.exploration.enabled:
            return None
        if not force and (not self._exploration_due() or not self._looks_idle()):
            return None
        if not self.web_search or not getattr(self.web_search, "is_available", lambda: False)():
            logger.debug("[%s] exploration skipped: web_search unavailable", self.account_id)
            return None
        # scene gate
        if hasattr(self.web_search, "is_scene_enabled"):
            if not (
                self.web_search.is_scene_enabled("companion_exploration")
                or self.web_search.is_scene_enabled("proactive_video")
            ):
                scenes = getattr(self.web_search, "scenes", None)
                if isinstance(scenes, dict) and "companion_exploration" in scenes:
                    if not scenes["companion_exploration"].get("enabled", False):
                        return None

        bits = self._persona_bits()
        state = self.store.get_life_state()
        plan = self.store.get_daily_plan()
        plan_sum = P.format_plan_summary([i.to_dict() for i in plan.items]) if plan.items else ""
        system, user = P.build_explore_query_prompt(
            persona_name=bits["name"],
            interests=bits.get("interests") or [],
            activity=state.activity,
            mood_bias=state.mood_bias,
            recent_topics=", ".join(self.get_topic_seeds()),
            plan_summary=plan_sum,
        )
        raw = await self._llm_text(system, user, max_tokens=220)
        query, motive = "", "随便看看公开资料"
        if raw:
            data = _extract_json(raw)
            if isinstance(data, dict):
                query = str(data.get("query") or "").strip()
                motive = str(data.get("motive") or motive).strip()

        if not self._query_is_searchable(query):
            logger.info(
                "[%s] explore query rejected, fallback: %r",
                self.account_id,
                (query or "")[:80],
            )
            query, motive = self._fallback_explore_query(bits)

        # 二次校验 fallback
        if not self._query_is_searchable(query):
            query, motive = "B站 科技区 热门 话题", "看看最近有什么热闹"

        logger.info("[%s] explore query=%r motive=%r", self.account_id, query[:60], motive[:40])

        try:
            result = await self.web_search.search(
                query,
                scene="companion_exploration",
            )
        except TypeError:
            try:
                result = await self.web_search.search(query)
            except Exception as e:
                logger.warning("[%s] explore search failed: %s", self.account_id, e)
                result = None
        except Exception as e:
            logger.warning("[%s] explore search failed: %s", self.account_id, e)
            result = None

        items: List[Any] = []
        results_text = ""
        search_ok = False
        if isinstance(result, dict):
            items = result.get("items") or result.get("results") or []
            search_ok = bool(items) or bool(result.get("answer") or result.get("content"))
            if hasattr(self.web_search, "format_reference_block"):
                try:
                    results_text = self.web_search.format_reference_block(result) or ""
                except Exception:
                    results_text = ""
            if not results_text:
                lines = []
                for it in items[: self._cfg.exploration.max_results]:
                    if isinstance(it, dict):
                        lines.append(
                            f"- {it.get('title') or ''}: "
                            f"{it.get('snippet') or it.get('content') or it.get('url') or ''}"
                        )
                results_text = "\n".join(lines)
        elif isinstance(result, str):
            results_text = result
            search_ok = bool(result.strip())
        if not results_text:
            results_text = "（无结果/搜索失败）"

        system2, user2 = P.build_explore_note_prompt(
            query=query,
            motive=motive,
            results_text=results_text,
            persona_prompt=bits["base_prompt"],
        )
        raw2 = await self._llm_text(system2, user2, max_tokens=500)
        impression, self_link, should_share = (
            ("没搜到什么有用的，下次换个关键词试试。" if not search_ok else "看了一些资料。"),
            "",
            False,
        )
        highlights: List[str] = []
        if raw2:
            data = _extract_json(raw2)
            if isinstance(data, dict):
                impression = str(data.get("impression") or impression)
                self_link = str(data.get("self_link") or "")
                should_share = bool(data.get("should_share")) and search_ok
                highlights = [str(x) for x in (data.get("highlights") or []) if str(x).strip()][:5]

        note = ExploreNote(
            id=uuid.uuid4().hex[:12],
            created_at=_now_iso(),
            query=query,
            motive=motive,
            impression=impression,
            self_link=self_link,
            should_share=should_share,
            items=[x for x in (items or [])[: self._cfg.exploration.max_results] if isinstance(x, dict)],
            source="web_search",
        )
        # stash highlights into first item-like meta via impression already; also runtime
        notes = [note] + self.store.get_explore_notes()
        self.store.save_explore_notes(notes[:40])
        body = (
            f"探索：{query}\n动机：{motive}\n{impression}\n关联：{self_link}"
            + (f"\n要点：{'；'.join(highlights)}" if highlights else "")
        )
        await self._archive_text(
            source_type="web_reference",
            event_type="exploration",
            text=body,
            title=f"探索 {query[:40]}",
            idempotency_key=f"explore:{note.id}",
            importance=0.5,
            metadata={
                "query": query,
                "should_share": should_share,
                "search_ok": search_ok,
                "highlights": highlights,
            },
        )
        # 无结果不进草稿；有结果且模型认为可分享才进
        if self._cfg.exploration.offer_dynamic_draft and should_share and impression and search_ok:
            seed = f"【随便搜到】{query}\n{impression[:400]}"
            self._offer_draft(seed, created_by="companion_explore")
        self.store.patch_runtime(
            last_explore_ts=time.time(),
            last_explore_query=query,
            last_explore_ok=search_ok,
        )
        self.apply_activity_energy_delta(-2, "explore")
        return note

    # ── creative ──

    def _creative_idle_ok(self) -> bool:
        if datetime.now().hour < 7:
            return False
        if not self._looks_idle():
            return False
        state = self.store.get_life_state()
        return 38 <= int(state.energy) <= 82

    async def maybe_advance_creative(self, force: bool = False) -> Optional[CreativeProject]:
        if not self.enabled or not self._cfg.creative.enabled:
            return None
        if not force and not self._creative_idle_ok():
            return None
        projects = self.store.get_projects()
        drafting = [p for p in projects if p.status == "drafting"]
        bits = self._persona_bits()
        now = time.time()

        # maybe start new
        if len(drafting) < self._cfg.creative.max_active_projects:
            # 开新项目冷却：10 小时内不重复开书，避免刷 LLM
            last_create_ts = 0.0
            for p in projects:
                try:
                    # created_at is isoformat
                    if p.created_at:
                        last_create_ts = max(
                            last_create_ts,
                            datetime.fromisoformat(p.created_at).timestamp(),
                        )
                except Exception:
                    pass
            can_create = (now - last_create_ts) >= 10 * 3600 if last_create_ts else True
            if can_create and (force or random.random() < self._cfg.creative.inspiration_probability):
                diaries = self.store.get_diaries()
                dream = self.store.get_latest_dream()
                state = self.store.get_life_state()
                insp = (
                    (diaries[0].share_seed if diaries else "")
                    or (dream.content[:120] if dream else "")
                    or state.activity
                    or "日常碎片"
                )
                system, user = P.build_creative_project_prompt(
                    persona_name=bits["name"],
                    persona_prompt=bits["base_prompt"] + (("\n" + bits.get("creative_rules", "")) if bits.get("creative_rules") else ""),
                    inspiration=insp,
                )
                raw = await self._llm_text(system, user, max_tokens=500)
                meta = _extract_json(raw) if raw else None
                if isinstance(meta, dict) and meta.get("title"):
                    proj = CreativeProject.from_dict(
                        {
                            **meta,
                            "id": uuid.uuid4().hex[:12],
                            "status": "drafting",
                            "current_chars": 0,
                            "draft_chunks": [],
                            "inspiration_source": insp[:200],
                            "created_at": _now_iso(),
                            "updated_at": _now_iso(),
                            "next_advance_at": now + random.randint(45, 140) * 60,
                        }
                    )
                    projects = [proj] + projects
                    self.store.save_projects(projects[:20])
                    drafting = [p for p in projects if p.status == "drafting"]
                    self.store.patch_runtime(last_creative_project_at=_now_iso())

        # advance one due project
        due = [p for p in drafting if force or (p.next_advance_at or 0) <= now]
        if not due:
            return None
        proj = due[0]
        prev = proj.draft_chunks[-1].text if proj.draft_chunks else ""
        budget = self._cfg.creative.chars_per_session
        system, user = P.build_creative_chunk_prompt(
            title=proj.title,
            work_type=proj.work_type,
            premise=proj.premise,
            outline=proj.outline,
            previous=prev,
            next_hint=proj.next_hint,
            budget=budget,
            persona_prompt=bits["base_prompt"],
        )
        text = await self._llm_text(system, user, max_tokens=max(300, budget + 100))
        if not text or len(text.strip()) < 40:
            proj.next_advance_at = now + 30 * 60
            proj.updated_at = _now_iso()
            self.store.save_projects(projects[:20])
            return proj
        text = text.strip()
        # strip accidental json
        if text.startswith("{"):
            data = _extract_json(text)
            if isinstance(data, dict) and data.get("text"):
                text = str(data["text"])
        chunk = CreativeChunk(at=_now_iso(), text=text, chars=len(text))
        proj.draft_chunks.append(chunk)
        proj.draft_chunks = proj.draft_chunks[-40:]
        proj.current_chars = sum(c.chars for c in proj.draft_chunks)
        proj.updated_at = _now_iso()
        proj.next_advance_at = now + random.randint(95, 320) * 60
        if proj.current_chars >= proj.target_chars:
            proj.status = "finished"
            if self._cfg.creative.offer_dynamic_draft:
                seed = f"写完了《{proj.title}》的一小节，自己还挺满意。"
                self._offer_draft(seed, created_by="companion_creative")
        # replace in list
        projects = [proj if p.id == proj.id else p for p in projects]
        self.store.save_projects(projects[:20])
        await self._archive_text(
            source_type="creative",
            event_type="creative_chunk",
            text=f"《{proj.title}》续写 {chunk.chars} 字",
            title=proj.title,
            idempotency_key=f"creative:{proj.id}:{chunk.at}",
            importance=0.4,
            metadata={"project_id": proj.id, "status": proj.status},
        )
        self.apply_activity_energy_delta(-2, "creative")
        return proj

    # ── main tick ──

    def _past_time(self, hhmm: str, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now()
        m = _parse_hhmm(hhmm)
        if m is None:
            return False
        return now.hour * 60 + now.minute >= m

    async def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Called by scheduler roughly once per minute. Lightweight orchestration."""
        now = now or datetime.now()
        result: Dict[str, Any] = {"enabled": self.enabled, "actions": []}
        if not self.enabled:
            return result

        # Prefer asyncio.Lock across await boundaries
        lock = self._tick_lock
        if lock is None and asyncio is not None:
            try:
                self._tick_lock = asyncio.Lock()
                lock = self._tick_lock
            except Exception:
                lock = None

        if lock is not None:
            if lock.locked():
                result["skipped"] = "busy"
                return result
            await lock.acquire()
        else:
            if self._tick_busy:
                result["skipped"] = "busy"
                return result
            self._tick_busy = True

        try:
            self.reload_config()
            if not self._cfg.enabled:
                return result

            self.ensure_life_state()

            # plan generation after generate_time
            if self._cfg.schedule.enabled and self._past_time(self._cfg.schedule.generate_time, now):
                plan = self.store.get_daily_plan()
                if plan.date != _today() or not plan.items:
                    plan = await self.ensure_daily_plan()
                    result["actions"].append(f"plan:{plan.source}")
                else:
                    self._sync_state_from_plan(plan)

                detail = await self.ensure_detail_enhancement()
                if detail and detail.segment_key:
                    result["actions"].append(f"detail:{detail.segment_key}")

            # diary after configured time once/day
            if self._cfg.diary.enabled and self._past_time(self._cfg.diary.time, now):
                rt = self.store.get_runtime()
                if rt.get("diary_date") != _today():
                    entry = await self.generate_diary()
                    if entry:
                        result["actions"].append(f"diary:{entry.date}")

            # exploration / creative — low frequency, probabilistic
            if self._cfg.exploration.enabled and self._exploration_due() and self._looks_idle():
                if random.random() < 0.35:
                    note = await self.maybe_explore()
                    if note:
                        result["actions"].append(f"explore:{note.query[:20]}")

            if self._cfg.creative.enabled and self._creative_idle_ok():
                if random.random() < 0.25:
                    proj = await self.maybe_advance_creative()
                    if proj:
                        result["actions"].append(f"creative:{proj.title[:20]}")

            return result
        except Exception as e:
            logger.error("[%s] companion tick failed: %s", self.account_id, e, exc_info=True)
            result["error"] = type(e).__name__
            return result
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass
            else:
                self._tick_busy = False
