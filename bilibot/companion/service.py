"""CompanionLifeService — account-scoped living persona orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from bilibot.services.clock import now_cn, today_cn

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
    MotiveQueue,
    MotiveScore,
    PlanItem,
    SelfSnapshot,
    StoryDetail,
)
from . import prompts as P
from .store import CompanionStore

logger = logging.getLogger("bilibot.companion")

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_BUSY_MARKERS = ("上班", "工作", "学习", "考试", "通勤", "会议", "赶稿", "赶ddl")
_IDLE_MARKERS = ("摸鱼", "休息", "闲逛", "刷", "放松", "发呆", "创作", "写作", "午睡")
# Category prefixes for ongoing_threads — new thread replaces prior same-prefix entries.
_THREAD_CATEGORY_PREFIXES = ("兴趣：", "最近在看：", "小说：")


def _today() -> str:
    return today_cn().isoformat()


def _now_iso() -> str:
    return now_cn().isoformat(timespec="seconds")


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


def _fallback_plan_items(
    item_count: int = 8,
    *,
    interests: Optional[List[str]] = None,
    energy: int = 70,
    weekday: str = "",
    self_cues: Optional[List[str]] = None,
    ongoing_threads: Optional[List[str]] = None,
) -> List[PlanItem]:
    interest = next((str(x).strip() for x in (interests or []) if str(x).strip()), "感兴趣的内容")
    cues = [
        str(item).split("|eid=", 1)[0].strip()
        for item in (self_cues or [])
        if str(item or "").strip()
    ]
    threads = [
        str(item).split("|eid=", 1)[0].strip()
        for item in (ongoing_threads or [])
        if str(item or "").strip()
    ]
    lived_cue = (threads or cues or [""])[0][:42]
    basis = "self_snapshot" if lived_cue else "routine"
    cue_activity = (
        f"，也给“{lived_cue}”这条近况留一点延续空间"
        if lived_cue
        else ""
    )
    cue_seed = (
        f"如果有新感受，可以接着聊聊{lived_cue[:24]}"
        if lived_cue
        else f"今天想留意一点{interest[:18]}"
    )
    workday = weekday in {"周一", "周二", "周三", "周四", "周五"}
    daytime = (
        f"处理日常事务，间隙看看和{interest[:18]}有关的内容{cue_activity}"
        if workday
        else f"按自己的节奏消磨上午，顺手看看{interest[:18]}{cue_activity}"
    )
    afternoon = (
        f"继续处理手头的事，累了就围绕{interest[:18]}换换脑子"
        if int(energy or 0) >= 45
        else "把下午安排得松一点，优先休息和恢复精力"
    )
    detailed = [
        ("07:30", "08:15", "起床洗漱，慢慢清醒", "困倦", ""),
        ("08:15", "09:00", "随便吃点东西，刷几眼手机", "懒洋洋", ""),
        ("09:00", "11:30", daytime, "平稳", cue_seed),
        ("11:30", "12:30", "午饭与短暂放空", "放松", ""),
        ("12:30", "14:00", "午睡或发呆充电", "困倦", ""),
        ("14:00", "17:30", afternoon, "专注" if int(energy or 0) >= 45 else "疲惫", ""),
        ("17:30", "19:00", "晚饭与晚间过渡", "轻松", ""),
        ("19:00", "22:00", f"晚间娱乐，随心看看{interest[:18]}，也可能写点东西{cue_activity}", "活泼", "有真切感受时再分享一点碎碎念"),
        ("22:00", "23:30", "洗漱放松，准备入睡", "安静", ""),
        ("23:30", "07:00", "睡眠", "沉静", ""),
    ]
    n = max(4, min(int(item_count or 8), len(detailed)))
    if n >= 10:
        templates = detailed
    elif n >= 8:
        templates = [
            ("07:30", "09:00", "起床洗漱，随便吃点东西，慢慢清醒", "懒洋洋", ""),
            detailed[2],
            ("11:30", "14:00", "午饭后短暂放空，困了就午睡一会儿", "放松", ""),
            detailed[5],
            detailed[6],
            detailed[7],
            detailed[8],
            detailed[9],
        ][:n]
    elif n >= 6:
        templates = [
            ("07:30", "09:00", "起床洗漱，随便吃点东西，慢慢清醒", "懒洋洋", ""),
            ("09:00", "12:30", daytime, "平稳", cue_seed),
            ("12:30", "14:00", "午睡或发呆充电", "困倦", ""),
            ("14:00", "19:00", afternoon + "，之后吃晚饭", "专注", ""),
            ("19:00", "23:30", f"晚间放松，看看{interest[:18]}，随后洗漱收尾{cue_activity}", "轻松", "有真切感受时再分享一点碎碎念"),
            detailed[9],
        ][:n]
    else:
        templates = [
            ("07:30", "12:30", daytime, "平稳", cue_seed),
            ("12:30", "17:30", "午饭后休息，再按精力处理下午的事", "放松", ""),
            ("17:30", "23:30", f"吃晚饭、随心看看{interest[:18]}，夜深后洗漱收尾{cue_activity}", "轻松", "有真切感受时再分享一点碎碎念"),
            detailed[9],
        ]
    return [
        PlanItem(time=a, end=b, activity=c, mood=d, message_seed=e, basis=basis, confidence=0.55 if lived_cue else 0.5)
        for a, b, c, d, e in templates
    ]


def _merge_partial_plan_items(
    fallback_items: List[PlanItem], partial_items: List[PlanItem]
) -> List[PlanItem]:
    """Keep safe fallback coverage while salvaging usable role-specific LLM rows."""

    merged = [PlanItem.from_dict(item.to_dict()) for item in fallback_items]
    for candidate in partial_items:
        start = _parse_hhmm(candidate.time)
        if start is None or not str(candidate.activity or "").strip():
            continue
        match_index: Optional[int] = None
        for index, base in enumerate(merged):
            base_start = _parse_hhmm(base.time)
            base_end = _parse_hhmm(base.end)
            if base_start is None:
                continue
            if start == base_start:
                match_index = index
                break
            if base_end is None:
                base_end = base_start + 60
            if base_end > base_start and base_start <= start < base_end:
                match_index = index
                break
            if base_end <= base_start and (start >= base_start or start < base_end):
                match_index = index
                break
        if match_index is None:
            continue
        base = merged[match_index]
        sleep_tokens = ("睡", "睡眠", "就寝", "休息")
        base_is_sleep = any(t in str(base.activity or "") for t in sleep_tokens)
        # The fallback sleep slot is a safety anchor: never let a partial LLM
        # row replace it with an all-night activity, even one that merely
        # mentions "不睡觉/睡不着".
        activity = base.activity if base_is_sleep else (candidate.activity or base.activity)
        merged[match_index] = PlanItem(
            time=base.time,
            end=base.end,
            activity=str(activity).strip()[:180],
            mood=str(candidate.mood or base.mood).strip()[:30],
            message_seed=str(candidate.message_seed or base.message_seed).strip()[:160],
            basis="llm_partial",
            confidence=max(0.55, float(candidate.confidence or 0.0)),
        )
    return merged


def _canonical_companion_document(kind: str, payload: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Build the exact durable document shared by local companion state and V6 memory."""

    body = json.dumps(
        {"kind": str(kind), **dict(payload)},
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=str,
    )
    digest = hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()
    return body, {
        "canonical_kind": str(kind),
        "canonical_sha256": digest,
        "canonical_chars": len(body),
    }
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
        safety_checker=None,
        web_search=None,
        draft_store=None,
    ):
        self.account_id = account_id
        self.account_data_dir = account_data_dir
        self.config_loader = config_loader
        self.llm = llm
        self.persona_store = persona_store
        self.memory_brain = memory_brain
        self.safety_checker = safety_checker
        self.web_search = web_search
        self.draft_store = draft_store
        self.store = CompanionStore(account_data_dir)
        self._cfg = self.reload_config()
        # 异步可重入保护：bool 在 await 间隙会误判；用 asyncio.Lock
        self._tick_lock: Optional[asyncio.Lock] = None
        self._tick_busy = False  # sync fallback if lock not yet bound to loop
        # 本进程内 begin_activity 已打开、尚未 finish 的活动。
        # tick 结束时仍残留的项会被强制关闭，避免 LLM/搜索异常泄漏 intent。
        self._open_companion_activities: dict[str, dict[str, Any]] = {}
        # 最近一次入脑失败（可观测，不阻断主链路）
        self._last_archive_error: str = ""
        self._last_archive_ok_at: str = ""
        self._last_archive_fail_at: str = ""
        self._archive_fail_count: int = 0

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

    def rebind_memory_brain(self, memory_brain) -> None:
        """Hot-reload: keep companion writing the current account brain."""
        self.memory_brain = memory_brain
        # Wire consolidate → apply_reflection so nightly insights rewrite Self/Motive.
        bind = getattr(memory_brain, "bind_reflection_apply_hook", None) if memory_brain else None
        if callable(bind):
            try:
                bind(
                    lambda summary, relation="reflection", evidence_event_ids=None, **_kw: self.apply_reflection(
                        summary=summary,
                        relation=relation,
                        evidence_event_ids=list(evidence_event_ids or []),
                    )
                )
            except Exception:
                logger.debug(
                    "[%s] bind_reflection_apply_hook failed",
                    self.account_id,
                    exc_info=True,
                )

    def rebind_safety_checker(self, safety_checker) -> None:
        """Hot-reload: keep memory failures connected to account fail-closed state."""
        self.safety_checker = safety_checker

    @staticmethod
    def _is_non_fatal_memory_error(exc: BaseException | None = None, detail: str = "") -> bool:
        """Idempotency / reingest collisions are data-shape issues, not storage outages.

        Pausing the whole account for them freezes comments/videos while the brain
        remains writable — the 2026-07-20 companion_explore incident. Never
        escalate these into account risk pause.
        """
        names: list[str] = []
        texts: list[str] = []
        if detail:
            texts.append(str(detail))
        cur: BaseException | None = exc
        seen = 0
        while cur is not None and seen < 6:
            names.append(type(cur).__name__)
            texts.append(str(cur))
            cur = cur.__cause__ or cur.__context__
            seen += 1
        blob = " ".join(texts)
        if any(
            n in {"IdempotencyConflictError", "ReingestBlockedError"}
            for n in names
        ):
            return True
        if "IdempotencyConflictError" in blob or "ReingestBlockedError" in blob:
            return True
        if "already exists with different content" in blob:
            return True
        if "was explicitly deleted and cannot be re-ingested" in blob:
            return True
        return False

    def _pause_for_memory_failure(
        self,
        detail: str = "",
        *,
        exc: BaseException | None = None,
    ) -> None:
        if self._is_non_fatal_memory_error(exc, detail):
            logger.warning(
                "[%s] companion memory conflict not escalated to account pause: %s",
                self.account_id,
                (detail or type(exc).__name__ if exc else "")[:160],
            )
            return
        safety = getattr(self, "safety_checker", None)
        pause = getattr(safety, "pause_account", None)
        if not callable(pause):
            return
        reason = "memory_archive_failed:companion"
        if detail:
            reason = f"{reason}:{detail[:80]}"
        try:
            pause(self.account_id, reason=reason)
        except Exception:
            logger.error(
                "[%s] companion memory failure could not pause account",
                self.account_id,
                exc_info=True,
            )

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
        interests: List[str] = []
        if p:
            interests = list(getattr(p, "interests", None) or [])
            if not interests and getattr(p, "tags", None):
                interests = list(p.tags or [])
        # merge config exploration interests (even without persona store)
        for x in self._cfg.exploration.interests:
            if x and x not in interests:
                interests.append(x)
        # revive proactive.interest_keywords
        try:
            raw = self.config_loader.get_raw_config() if self.config_loader else {}
            kws = (raw.get("proactive") or {}).get("interest_keywords") or []
            if isinstance(kws, str):
                kws = [x.strip() for x in kws.split(",") if x.strip()]
            for x in kws:
                if x and str(x) not in interests:
                    interests.append(str(x))
        except Exception:
            pass
        if not p:
            return {
                "id": "",
                "name": "Bot",
                "base_prompt": "",
                "interests": interests,
                "life_background": "",
                "diary_rules": "",
                "creative_rules": "",
            }
        return {
            "id": getattr(p, "id", "") or "",
            "name": getattr(p, "name", "") or "Bot",
            "base_prompt": getattr(p, "base_prompt", "") or "",
            "interests": interests,
            "life_background": getattr(p, "life_background", "") or "",
            "diary_rules": getattr(p, "diary_rules", "") or "",
            "creative_rules": getattr(p, "creative_rules", "") or "",
        }

    async def _llm_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 900,
        *,
        scene: str = "companion",
    ) -> Optional[str]:
        if not self.llm or not hasattr(self.llm, "generate"):
            return None
        try:
            from bilibot.services.token_usage import usage_context
            with usage_context(scene=scene, account_id=self.account_id):
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
        pause_on_error: bool = True,
    ) -> bool:
        """Archive companion output into the account memory brain.

        Returns True on commit / soft idempotent hit; False on skip or failure.
        Callers publish local companion state only after True.  A real memory
        failure pauses this account's automatic external actions unless the
        caller explicitly isolates a side-effect-free internal activity.
        """
        if not self.enabled:
            return False
        if not text:
            return False
        if not self.memory_brain:
            self._last_archive_error = "memory_brain_missing"
            self._last_archive_fail_at = _now_iso()
            self._archive_fail_count = int(self._archive_fail_count or 0) + 1
            logger.warning(
                "[%s] companion archive skipped: no memory_brain source=%s",
                self.account_id,
                source_type,
            )
            if pause_on_error:
                self._pause_for_memory_failure("memory_brain_missing")
            try:
                self.store.patch_runtime(
                    last_archive_error=self._last_archive_error,
                    last_archive_fail_at=self._last_archive_fail_at,
                    archive_fail_count=self._archive_fail_count,
                )
            except Exception:
                pass
            return False
        try:
            from bilibot.memory_brain.ingestion import text_observation

            # 日记/梦境/日程等同日可重写：键带内容指纹，避免 IdempotencyConflict 刷警告
            base_key = idempotency_key or f"{source_type}:{_today()}:{uuid.uuid4().hex[:8]}"
            if source_type in {"diary", "dream", "life_plan", "creative", "web_reference"}:
                digest = hashlib.sha1(
                    str(text).encode("utf-8", errors="ignore"), usedforsecurity=False
                ).hexdigest()[:10]
                base_key = f"{base_key}:{digest}"

            meta = dict(metadata or {})
            meta.setdefault("companion", True)
            meta.setdefault("source_module", "companion")
            # 可观测：连续归档失败次数挂到 runtime，供面板/运维感知「本地有脑无」
            if int(self._archive_fail_count or 0) > 0:
                meta.setdefault("prior_archive_fail_count", int(self._archive_fail_count or 0))

            env = text_observation(
                account_id=self.account_id,
                idempotency_key=base_key,
                source_type=source_type,
                event_type=event_type,
                text=text,
                title=title,
                persona_id=self._persona_bits().get("id") or "",
                scene="companion",
                metadata=meta,
                importance=importance,
            )
            result = None
            if hasattr(self.memory_brain, "archive_observation_async"):
                result = await self.memory_brain.archive_observation_async(env)
            elif hasattr(self.memory_brain, "archive_observation"):
                result = self.memory_brain.archive_observation(env)
            else:
                raise RuntimeError("memory_brain has no archive_observation")

            committed = True
            if result is not None:
                if isinstance(result, dict):
                    committed = result.get("source_committed", True) is not False
                else:
                    committed = getattr(result, "source_committed", True) is not False
            if not committed:
                raise RuntimeError("companion archive source_committed=false")

            self._last_archive_error = ""
            self._last_archive_ok_at = _now_iso()
            self._archive_fail_count = 0
            try:
                self.store.patch_runtime(
                    last_archive_ok_at=self._last_archive_ok_at,
                    last_archive_error="",
                    last_archive_source=source_type,
                    archive_fail_count=0,
                    companion_memory_cooldown_until=0,
                )
            except Exception:
                pass
            return True
        except Exception as e:
            msg = str(e)
            err_name = type(e).__name__
            # C3：同内容软命中可静默；内容冲突（IdempotencyConflict）不得当成功，
            # 否则本地 Life/日记更新而脑内仍是旧源。
            soft_hit = (
                "already exists" in msg
                and "different content" not in msg
                and err_name != "IdempotencyConflictError"
            )
            if soft_hit:
                logger.debug("[%s] companion archive skip: %s", self.account_id, msg[:160])
                return True
            if err_name == "IdempotencyConflictError" or "different content" in msg:
                logger.warning(
                    "[%s] companion archive conflict (not success): %s",
                    self.account_id,
                    msg[:200],
                )
            self._last_archive_error = f"{err_name}:{msg[:120]}"
            self._last_archive_fail_at = _now_iso()
            self._archive_fail_count = int(self._archive_fail_count or 0) + 1
            # soft：不 pause 平台动作；连续失败升级 warning 便于运维感知「本地有脑无」
            log_fn = (
                logger.error
                if self._archive_fail_count >= 3
                else logger.warning
            )
            log_fn(
                "[%s] companion archive failed source=%s fail_count=%s: %s",
                self.account_id,
                source_type,
                self._archive_fail_count,
                self._last_archive_error,
            )
            if pause_on_error:
                self._pause_for_memory_failure(self._last_archive_error, exc=e)
            try:
                cooldown_seconds = min(
                    30 * 60,
                    60 * (2 ** min(max(0, self._archive_fail_count - 1), 5)),
                )
                self.store.patch_runtime(
                    last_archive_error=self._last_archive_error,
                    last_archive_fail_at=self._last_archive_fail_at,
                    archive_fail_count=self._archive_fail_count,
                    last_archive_source=source_type,
                    companion_memory_cooldown_until=(
                        0 if pause_on_error else time.time() + cooldown_seconds
                    ),
                )
            except Exception:
                pass
            return False

    def _self_state_recall_needles(self) -> str:
        """Short continuous-self needles for generation-time retrieval (not QA)."""
        if not self.enabled:
            return ""
        try:
            state = self.ensure_life_state()
        except Exception:
            return ""
        parts: List[str] = []
        for item in (getattr(state, "ongoing_threads", None) or [])[:4]:
            t = str(item or "").strip()
            if t:
                parts.append(t)
        for item in (getattr(state, "salient_recent", None) or [])[:4]:
            t = str(item or "").strip()
            if t:
                parts.append(t[:48])
        act = str(getattr(state, "activity", "") or "").strip()
        if act:
            parts.append(act[:40])
        seed = str(getattr(state, "message_seed", "") or "").strip()
        if seed:
            parts.append(seed[:40])
        # Dedupe preserve order
        seen = set()
        out: List[str] = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return " ".join(out)[:280]

    def _generation_recall_query(
        self,
        *,
        scene: str,
        base_query: str = "",
        title: str = "",
    ) -> str:
        """Build a task-conditioned retrieval query for generation (≠ user QA).

        Avoid pure bag-of-function-words ("视频 评论 动态 心情") that dilute FTS
        into inbound comment floods. Prefer SelfState needles + scene anchors.
        """
        today = _today()
        scene_l = str(scene or "").strip().casefold()
        base = " ".join(str(base_query or "").replace("\x00", "").split())
        self_needles = self._self_state_recall_needles()
        title_s = " ".join(str(title or "").replace("\x00", "").split())[:60]

        if scene_l in {"dream"}:
            # Dream = high-entropy associative seeds (C14): mood / unfinished
            # threads / afterglow first; avoid comment-thread token bags.
            mood_bits = ""
            afterglow = ""
            try:
                st = self.ensure_life_state()
                mood_bits = str(getattr(st, "mood_bias", "") or "").strip()[:20]
                afterglow = str(getattr(st, "dream_afterglow", "") or "").strip()[:40]
            except Exception:
                pass
            core = " ".join(
                x
                for x in (
                    today,
                    "最近经历",
                    "看了",
                    "日记",
                    "创作",
                    "梦境余韵",
                    mood_bits,
                    afterglow,
                    title_s,
                    self_needles,
                    # Keep base short — do not reintroduce 评论 flood tokens.
                    base[:80],
                )
                if x
            )
            return core[:420]
        if scene_l in {"diary"}:
            # Diary = today's timeline + afterglow; avoid bare 私信 token (PM hard-zero).
            mood_bits = ""
            afterglow = ""
            try:
                st = self.ensure_life_state()
                mood_bits = str(getattr(st, "mood_bias", "") or "").strip()[:20]
                afterglow = str(getattr(st, "dream_afterglow", "") or "").strip()[:40]
            except Exception:
                pass
            core = " ".join(
                x
                for x in (
                    today,
                    "今天做过",
                    "看了",
                    "动态",
                    "梦境余韵",
                    "创作",
                    "日程",
                    mood_bits,
                    afterglow,
                    title_s,
                    self_needles,
                    base[:100],
                )
                if x
            )
            return core[:420]
        if scene_l in {"creative"}:
            # Creative = remote association over lived self (C14), not comment bags.
            mood_bits = ""
            try:
                st = self.ensure_life_state()
                mood_bits = str(getattr(st, "mood_bias", "") or "").strip()[:20]
            except Exception:
                pass
            core = " ".join(
                x
                for x in (
                    "创作",
                    "小说",
                    title_s,
                    "灵感",
                    "续写",
                    "最近经历",
                    mood_bits,
                    self_needles,
                    base[:120],
                )
                if x
            )
            return core[:420]
        if scene_l in {"exploration", "explore"}:
            # Explore from interest gaps + recent self, not pure web bag.
            core = " ".join(
                x
                for x in (
                    "想了解",
                    "兴趣",
                    "探索",
                    "最近经历",
                    title_s,
                    self_needles,
                    base[:120],
                )
                if x
            )
            return core[:420]
        if scene_l in {"life_plan", "companion"}:
            mood_bits = ""
            try:
                st = self.ensure_life_state()
                mood_bits = str(getattr(st, "mood_bias", "") or "").strip()[:20]
            except Exception:
                pass
            core = " ".join(
                x
                for x in (
                    today,
                    "生活安排",
                    "最近做过",
                    "最近经历",
                    mood_bits,
                    title_s,
                    self_needles,
                    base[:100],
                )
                if x
            )
            return core[:420]
        # Fallback: keep caller base but still append continuous self.
        core = " ".join(x for x in (base or today, self_needles, title_s) if x)
        return (core or f"{today} 最近经历")[:420]

    async def _recall_life_evidence(
        self,
        *,
        query: str = "",
        scene: str = "companion",
        limit: int = 6,
        action_key: str = "",
        action_type: str = "",
        current_activity: str = "",
        title: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        pause_on_error: bool = True,
    ) -> Dict[str, Any]:
        """V6 hybrid recall for diary/explore generation (not fragile string search).

        Returns ``{memory_evidence, memory_event_ids, snippets}``. Failures
        degrade to empty with a warning — never raise into generators.
        """
        empty: Dict[str, Any] = {
            "memory_evidence": "",
            "memory_event_ids": [],
            "snippets": [],
        }
        if not self.memory_brain:
            return empty
        today = _today()
        # Generation recipe: scene-conditioned + SelfState needles.
        # Callers may still pass a base query; we rewrite rather than use QA bags.
        q = self._generation_recall_query(
            scene=scene,
            base_query=query or "",
            title=title or action_type or scene,
        ) or (
            f"{today} 最近经历"
        )
        evidence = ""
        event_ids: List[str] = []
        snippets: List[str] = []

        if action_key:
            begin = getattr(self.memory_brain, "begin_activity", None)
            if not callable(begin) or not callable(
                getattr(type(self.memory_brain), "begin_activity", None)
            ):
                # Legacy/test brains predate the activity API. Production uses
                # MemoryBrainService and therefore always takes the durable
                # branch below; compatibility callers retain their old recall-
                # empty then archive-output behavior.
                return empty
            try:
                life_needles: List[str] = []
                mood_cues: List[str] = []
                try:
                    needle_blob = self._self_state_recall_needles() or ""
                    life_needles = [
                        t
                        for t in re.findall(
                            r"[\u4e00-\u9fffA-Za-z0-9]{2,24}", needle_blob
                        )
                    ][:12]
                    state = self.ensure_life_state()
                    if getattr(state, "mood_bias", None):
                        mood_cues.append(str(state.mood_bias)[:20])
                    if getattr(state, "dream_afterglow", None):
                        mood_cues.append(str(state.dream_afterglow)[:40])
                except Exception:
                    pass
                activity = await begin(
                    action_key=action_key,
                    action_type=action_type or scene,
                    current_activity=current_activity or f"正在进行{scene}任务。",
                    query=q,
                    scene=scene,
                    title=title or action_type or scene,
                    persona_id=self._persona_bits().get("id") or "",
                    metadata={
                        **dict(metadata or {}),
                        "companion": True,
                        "source_module": "companion",
                        "generation_recall_query": q[:240],
                    },
                    recent_limit=max(6, limit),
                    recall_limit=limit,
                    mode=str(scene or "").strip().casefold(),
                    life_needles=life_needles,
                    mood_cues=mood_cues,
                )
            except Exception as exc:
                if pause_on_error:
                    self._pause_for_memory_failure(
                        f"activity_memory_failed:{type(exc).__name__}",
                        exc=exc,
                    )
                logger.error(
                    "[%s] companion activity memory failed scene=%s action=%s",
                    self.account_id,
                    scene,
                    action_key,
                    exc_info=True,
                )
                raise
            # Track the opened intent so tick's finally can close it even when
            # a later LLM/search step raises before the generator can finish.
            self._open_companion_activities[str(action_key)] = {
                "action_type": action_type or scene,
                "scene": scene,
                "title": title or action_type or scene,
                "metadata": dict(metadata or {}),
            }
            evidence = str(getattr(activity, "prompt_text", "") or "").strip()
            event_ids = list(getattr(activity, "event_ids", ()) or ())
            snippets = list(getattr(activity, "recent_self_actions", ()) or ())
            # Always attach continuous self surface for generation continuity.
            try:
                life_surface = self.get_prompt_surface() or ""
            except Exception:
                life_surface = ""
            if life_surface:
                life_block = f"【连续自我状态】\n{life_surface}"
                if life_block not in evidence:
                    evidence = "\n\n".join(x for x in (evidence, life_block) if x)
                for line in life_surface.splitlines():
                    line = line.strip()
                    if line and line not in snippets:
                        snippets.append(line)
            if not evidence:
                if pause_on_error:
                    self._pause_for_memory_failure("activity_memory_empty")
                raise RuntimeError("companion activity memory returned empty context")
            return {
                "memory_evidence": evidence[:4000],
                "memory_event_ids": event_ids[:20],
                "snippets": snippets[: max(limit, 8)],
            }

        try:
            if callable(getattr(self.memory_brain, "recall", None)):
                from bilibot.memory_brain import RecallQuery

                life_needles: List[str] = []
                mood_cues: List[str] = []
                try:
                    needle_blob = self._self_state_recall_needles() or ""
                    life_needles = [
                        t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,24}", needle_blob)
                    ][:12]
                    state = self.ensure_life_state()
                    if getattr(state, "mood_bias", None):
                        mood_cues.append(str(state.mood_bias)[:20])
                    if getattr(state, "dream_afterglow", None):
                        mood_cues.append(str(state.dream_afterglow)[:40])
                except Exception:
                    pass
                result = await self.memory_brain.recall(
                    RecallQuery(
                        current_message=q,
                        account_id=self.account_id or "",
                        scene=scene,
                        mode=str(scene or "").strip().casefold(),
                        life_needles=life_needles,
                        mood_cues=mood_cues,
                    )
                )
                if result is not None:
                    evidence = str(getattr(result, "prompt_evidence", "") or "")
                    for ev in getattr(result, "events", ()) or ():
                        if not isinstance(ev, dict):
                            continue
                        eid = str(ev.get("id") or ev.get("event_id") or "").strip()
                        if eid and eid not in event_ids:
                            event_ids.append(eid)
                        title = str(
                            ev.get("title") or ev.get("event_title") or ""
                        ).strip()
                        summary = str(
                            ev.get("summary")
                            or ev.get("event_summary")
                            or ev.get("text")
                            or ""
                        ).strip()
                        line = " — ".join(x for x in (title, summary[:160]) if x)
                        if line and line not in snippets:
                            snippets.append(line)
                        if len(snippets) >= limit:
                            break
            elif hasattr(self.memory_brain, "search_memories"):
                hits = await self.memory_brain.search_memories(query=q, limit=limit)
                if isinstance(hits, list):
                    for h in hits[:limit]:
                        if isinstance(h, dict):
                            line = str(
                                h.get("summary")
                                or h.get("event_summary")
                                or h.get("title")
                                or h.get("text")
                                or ""
                            ).strip()
                            eid = str(h.get("id") or h.get("event_id") or "").strip()
                        else:
                            line = str(h).strip()
                            eid = ""
                        if line:
                            snippets.append(line[:200])
                        if eid and eid not in event_ids:
                            event_ids.append(eid)
        except Exception as exc:
            logger.warning(
                "[%s] companion V6 recall failed scene=%s: %s",
                self.account_id,
                scene,
                type(exc).__name__,
            )
            return empty

        if not evidence and snippets:
            evidence = "\n".join(f"- {s}" for s in snippets[:limit])
        try:
            life_surface = self.get_prompt_surface() or ""
        except Exception:
            life_surface = ""
        if life_surface:
            life_block = f"【连续自我状态】\n{life_surface}"
            if life_block not in (evidence or ""):
                evidence = "\n\n".join(x for x in (evidence, life_block) if x)
            for line in life_surface.splitlines():
                line = line.strip()
                if line and line not in snippets:
                    snippets.append(line)
        if evidence:
            logger.info(
                "[%s] companion recall scene=%s events=%s chars=%s",
                self.account_id,
                scene,
                len(event_ids),
                len(evidence),
            )
        return {
            "memory_evidence": evidence[:2500],
            "memory_event_ids": event_ids[:20],
            "snippets": snippets[: max(limit, 8)],
        }

    @staticmethod
    def _thread_category_prefix(thread: str) -> str:
        """Known continuous-self thread categories replace previous same-prefix rows."""
        t = str(thread or "").strip()
        for pref in ("小说：", "最近在看：", "兴趣：", "追番："):
            if t.startswith(pref):
                return pref
        return ""

    def _push_salient_self(
        self,
        *,
        line: str,
        thread: str = "",
        close_thread_prefix: str = "",
        event_id: str = "",
    ) -> None:
        """Update continuous self surface after a closed companion activity.

        Known category prefixes (兴趣：/最近在看：/小说：) replace prior same-prefix
        threads so one-shot or successive activities do not accumulate forever.
        When ``close_thread_prefix`` is set and no new ``thread`` is provided,
        matching open threads are closed without re-inserting a finished marker.

        When ``event_id`` is provided, the salient line is stored as
        ``{text}|eid={event_id}`` so SelfSnapshot claims stay evidence-bound.
        """
        if not self.enabled:
            return
        text = " ".join(str(line or "").replace("\x00", "").split())
        eid = " ".join(str(event_id or "").replace("\x00", "").split())
        if eid and text and "|eid=" not in text:
            # Keep human text short; bind evidence after the display body.
            text = f"{text[:100]}|eid={eid[:64]}"
        if not text and not thread and not close_thread_prefix:
            return
        # Ensure day-roll happens before atomic mutate (may write once).
        try:
            self.ensure_life_state()
        except Exception:
            return

        def _mutate(state: LifeState) -> None:
            recent = [
                str(x).strip()
                for x in (getattr(state, "salient_recent", None) or [])
                if str(x or "").strip()
            ]
            if text:
                bare = text.split("|eid=")[0]
                recent = [
                    x
                    for x in recent
                    if x != text
                    and not x.startswith(bare[:16])
                    and x.split("|eid=")[0] != bare
                ]
                recent.insert(0, text[:160])
                state.salient_recent = recent[:8]
            threads = [
                str(x).strip()
                for x in (getattr(state, "ongoing_threads", None) or [])
                if str(x or "").strip()
            ]
            if close_thread_prefix:
                pref = close_thread_prefix.strip()
                threads = [t for t in threads if not t.startswith(pref)]
            thr = " ".join(str(thread or "").replace("\x00", "").split())
            if thr:
                if "已完成" in thr:
                    cat = self._thread_category_prefix(thr) or thr[:12]
                    if cat:
                        threads = [t for t in threads if not t.startswith(cat)]
                else:
                    cat = self._thread_category_prefix(thr)
                    if cat:
                        threads = [t for t in threads if not t.startswith(cat)]
                    else:
                        threads = [
                            t
                            for t in threads
                            if t != thr and not t.startswith(thr[:12])
                        ]
                    threads.insert(0, thr[:80])
            state.ongoing_threads = threads[:6]
            state.updated_at = _now_iso()

        try:
            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updated_state = updater(_mutate)
            else:
                state = self.store.get_life_state()
                _mutate(state)
                self.store.save_life_state(state)
                updated_state = state
            try:
                runtime = self.store.get_runtime() or {}
                raw_stamps = runtime.get("ongoing_thread_timestamps") or {}
                stamps = dict(raw_stamps) if isinstance(raw_stamps, dict) else {}
                now_ts = time.time()
                if close_thread_prefix:
                    stamps = {
                        key: value
                        for key, value in stamps.items()
                        if not str(key).startswith(close_thread_prefix.strip())
                    }
                normalized_thread = " ".join(
                    str(thread or "").replace("\x00", "").split()
                )[:80]
                if normalized_thread and "已完成" not in normalized_thread:
                    category = self._thread_category_prefix(normalized_thread)
                    if category:
                        stamps = {
                            key: value
                            for key, value in stamps.items()
                            if not str(key).startswith(category)
                        }
                    stamps[normalized_thread] = now_ts
                current_threads = [
                    str(item).strip()
                    for item in (getattr(updated_state, "ongoing_threads", None) or [])
                    if str(item or "").strip()
                ]
                self.store.patch_runtime(
                    ongoing_thread_timestamps={
                        item: float(stamps.get(item) or now_ts)
                        for item in current_threads[:6]
                    }
                )
            except Exception:
                logger.debug(
                    "[%s] ongoing thread timestamp update failed",
                    self.account_id,
                    exc_info=True,
                )
        except Exception:
            logger.debug(
                "[%s] salient self save failed", self.account_id, exc_info=True
            )

    async def _finish_activity_memory(
        self,
        *,
        action_key: str,
        action_type: str,
        result_text: str,
        scene: str,
        title: str,
        metadata: Optional[Dict[str, Any]] = None,
        state: str = "completed",
        pause_on_error: bool = True,
        salient_line: str = "",
        ongoing_thread: str = "",
        close_thread_prefix: str = "",
    ) -> bool:
        """Close an activity lifecycle after its domain output is archived.

        ``state`` defaults to completed. Pass failed/skipped/rejected when the
        activity opened begin_activity but did not produce a domain archive.
        """
        finish = getattr(self.memory_brain, "finish_activity", None)
        if not callable(finish) or not callable(
            getattr(type(self.memory_brain), "finish_activity", None)
        ):
            return False
        terminal = str(state or "completed").strip().casefold() or "completed"
        try:
            finished_event_id = await finish(
                action_key=action_key,
                action_type=action_type,
                result_text=result_text,
                state=terminal,
                scene=scene,
                title=title,
                persona_id=self._persona_bits().get("id") or "",
                metadata={
                    **dict(metadata or {}),
                    "companion": True,
                    "source_module": "companion",
                },
            )
            if terminal == "completed":
                line = (
                    str(salient_line or "").strip()
                    or str(result_text or "").strip()
                    or str(title or action_type)
                )
                self._push_salient_self(
                    line=line[:120],
                    thread=ongoing_thread,
                    close_thread_prefix=close_thread_prefix,
                    event_id=str(finished_event_id or ""),
                )
            self._open_companion_activities.pop(str(action_key), None)
            return True
        except Exception as exc:
            # Domain output may already be committed; do not regenerate a
            # different diary/dream/chunk. Pause future automation when this
            # was a successful-path close. Soft-close failures (empty chunk)
            # only log so a missing finish does not cascade into a full pause
            # loop on every creative tick.
            if pause_on_error:
                self._pause_for_memory_failure(
                    f"activity_outcome_failed:{type(exc).__name__}",
                    exc=exc,
                )
            else:
                try:
                    self.store.patch_runtime(
                        last_activity_memory_error=(
                            f"{type(exc).__name__}:{str(exc)[:120]}"
                        ),
                        companion_memory_cooldown_until=time.time() + 60,
                    )
                except Exception:
                    pass
            logger.error(
                "[%s] companion activity outcome failed action=%s state=%s",
                self.account_id,
                action_key,
                terminal,
                exc_info=True,
            )
            return False

    async def _fail_open_activity(
        self,
        *,
        action_key: str,
        action_type: str,
        scene: str,
        title: str,
        result_text: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Close an activity intent opened by _recall_life_evidence as failed.

        Called on archive failure so memory's recent-self lane never keeps a
        permanently open "准备中" intent.
        """
        if not action_key:
            return
        try:
            await self._finish_activity_memory(
                action_key=action_key,
                action_type=action_type,
                result_text=result_text or f"{scene} 记忆归档失败",
                scene=scene,
                title=title,
                metadata=metadata,
                state="failed",
                pause_on_error=False,
            )
        except Exception as exc:  # never mask the original archive error
            logger.warning(
                "[%s] failed-activity close failed action=%s: %s",
                self.account_id,
                action_key,
                exc,
            )

    async def _close_stale_open_activities(self) -> int:
        """Fail any begin_activity intent that a generator did not finish.

        Generators close their intents on every normal path; this is the tick
        finally-belt for LLM/search/timeout exceptions that propagate before
        the generator reaches its own close.
        """
        registry = getattr(self, "_open_companion_activities", None) or {}
        if not registry:
            return 0
        stale = list(registry.items())
        remaining: dict[str, dict[str, Any]] = {}
        for key, meta in stale:
            try:
                closed = await self._finish_activity_memory(
                    action_key=key,
                    action_type=str(meta.get("action_type") or "companion"),
                    result_text="companion 生成中断，未产生可归档的终态输出",
                    scene=str(meta.get("scene") or "companion"),
                    title=str(meta.get("title") or "companion"),
                    metadata=dict(meta.get("metadata") or {}),
                    state="failed",
                    pause_on_error=False,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] stale-activity close failed action=%s: %s",
                    self.account_id,
                    key,
                    exc,
                )
                closed = False
            if not closed:
                remaining[key] = meta
        if remaining:
            registry.update(remaining)
        return len(stale) - len(remaining)

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

    def rank_motives(self, now: Optional[datetime] = None) -> MotiveQueue:
        """Score competing desires from SelfSnapshot + schedule soft signals.

        Higher score ⇒ more urgent. Callers (scheduler tick / proactive checks)
        should prefer the top motive's ``suggested_action`` over blind cron slots.
        """
        now = now or now_cn()
        snap = self.get_self_snapshot() if self.enabled else SelfSnapshot()
        energy = int(getattr(snap, "energy", 70) or 70)
        threads = list(getattr(snap, "ongoing_threads", None) or [])
        salient = list(getattr(snap, "salient_recent", None) or [])
        seed = str(getattr(snap, "message_seed", "") or "")
        afterglow = str(getattr(snap, "dream_afterglow", "") or "")
        runtime = self.store.get_runtime() or {}
        now_ts = now.timestamp()
        sleep_hours = now.hour >= 23 or now.hour < 7
        try:
            browse_cooldown_until = float(runtime.get("browse_cooldown_until") or 0)
        except (TypeError, ValueError):
            browse_cooldown_until = 0.0
        try:
            dynamic_cooldown_until = float(runtime.get("dynamic_cooldown_until") or 0)
        except (TypeError, ValueError):
            dynamic_cooldown_until = 0.0
        try:
            browse_failures = max(0, int(runtime.get("browse_failure_streak") or 0))
        except (TypeError, ValueError):
            browse_failures = 0
        try:
            browse_session_count = max(0, int(runtime.get("browse_session_count") or 0))
        except (TypeError, ValueError):
            browse_session_count = 0
        try:
            last_proactive_video_ts = float(
                runtime.get("last_proactive_video_ts") or 0
            )
        except (TypeError, ValueError):
            last_proactive_video_ts = 0.0
        # Session count is a rolling 2h window. Without this decay a crash /
        # restart between the 4th watch and a reset hook keeps rest>=7.0
        # forever and the MotiveQueue gate blocks every future video/dynamic.
        if (
            browse_session_count > 0
            and last_proactive_video_ts > 0
            and now_ts - last_proactive_video_ts >= 2 * 3600
        ):
            browse_session_count = 0
        try:
            last_feed_exhaustion_ts = float(runtime.get("last_feed_exhaustion_ts") or 0)
        except (TypeError, ValueError):
            last_feed_exhaustion_ts = 0.0
        try:
            last_browse_success_ts = float(runtime.get("last_browse_success_ts") or 0)
        except (TypeError, ValueError):
            last_browse_success_ts = 0.0

        motives: List[MotiveScore] = []

        # Rest when drained.
        rest_score = 0.0
        if energy <= 25:
            rest_score = 9.0
        elif energy <= 40:
            rest_score = 6.0
        elif energy <= 55:
            rest_score = 3.0
        if sleep_hours:
            rest_score = max(rest_score, 9.5 if now.hour < 6 else 8.0)
        if browse_session_count >= 4:
            rest_score = max(rest_score, min(9.0, 5.0 + browse_session_count * 0.5))
        if browse_cooldown_until > now_ts and now_ts - last_browse_success_ts < 45 * 60:
            rest_score = max(rest_score, 4.5)
        if browse_cooldown_until > now_ts and now_ts - last_feed_exhaustion_ts < 45 * 60:
            # A dry feed should become a real pause, not a minute-by-minute
            # start→skip loop. Exploration can still run inside companion.tick.
            rest_score = max(rest_score, 6.5)
        motives.append(
            MotiveScore(
                name="疲惫",
                score=rest_score,
                reason=(
                    "feed_exhausted"
                    if browse_cooldown_until > now_ts
                    and now_ts - last_feed_exhaustion_ts < 45 * 60
                    else "watch_afterglow"
                    if browse_cooldown_until > now_ts
                    and now_ts - last_browse_success_ts < 45 * 60
                    else f"energy={energy}"
                ),
                suggested_action="rest",
            )
        )

        # Curiosity / browse
        browse_score = 3.0 + max(0, (energy - 50) / 20.0)
        if self.wants_browse_bilibili_now(now):
            browse_score += 3.5
        if any("最近在看" in str(t) for t in threads):
            browse_score += 1.0
        if sleep_hours:
            browse_score = min(browse_score, 0.5)
        if browse_cooldown_until > now_ts:
            browse_score = min(browse_score, 1.0)
        browse_score -= min(6.0, browse_failures * 1.5)
        browse_score -= min(3.0, max(0, browse_session_count - 2) * 0.75)
        motives.append(
            MotiveScore(
                name="好奇",
                score=max(0.0, min(10.0, browse_score)),
                reason=(
                    "sleep_gate" if sleep_hours
                    else "browse_cooldown" if browse_cooldown_until > now_ts
                    else "recent_failures" if browse_failures
                    else "schedule_browse" if self.wants_browse_bilibili_now(now)
                    else "energy_browse"
                ),
                suggested_action="browse_video",
            )
        )

        # Express / dynamic
        express = 2.0
        if seed:
            express += 2.5
        if afterglow:
            express += 1.0
        if any("想发" in str(s) or "动态" in str(s) for s in salient[:4]):
            express += 2.0
        if energy >= 45:
            express += 1.0
        else:
            express -= 1.5
        if sleep_hours:
            express -= 2.0
        if dynamic_cooldown_until > now_ts:
            express -= 8.0
        motives.append(
            MotiveScore(
                name="表达欲",
                score=max(0.0, min(10.0, express)),
                reason=(
                    "dynamic_cooldown" if dynamic_cooldown_until > now_ts
                    else "seed_or_salient" if seed or afterglow
                    else "baseline"
                ),
                suggested_action="post_dynamic",
            )
        )

        # Social — open reply-ish threads
        social = 1.5
        if any("私信" in str(s) or "评论" in str(s) for s in salient[:5]):
            social += 2.5
        if energy >= 40:
            social += 1.0
        motives.append(
            MotiveScore(
                name="社交欲",
                score=min(10.0, social),
                reason="recent_social" if social > 2.5 else "baseline",
                suggested_action="reply",
            )
        )

        # Explore
        explore = 2.0 + max(0, (energy - 55) / 25.0)
        if any(str(t).startswith("兴趣：") for t in threads):
            explore += 2.0
        if browse_failures:
            explore += min(3.0, browse_failures)
        if sleep_hours:
            explore = min(explore, 1.0)
        motives.append(
            MotiveScore(
                name="探索",
                score=min(10.0, explore),
                reason="interest_thread" if explore > 3 else "baseline",
                suggested_action="explore",
            )
        )

        # Creative
        creative = 1.5
        if any(str(t).startswith("小说：") for t in threads):
            creative += 3.5
        if energy >= 50:
            creative += 1.0
        if browse_failures:
            creative += min(2.0, browse_failures * 0.75)
        if sleep_hours:
            creative = min(creative, 1.0)
        motives.append(
            MotiveScore(
                name="创作",
                score=min(10.0, creative),
                reason="open_novel" if creative > 3 else "baseline",
                suggested_action="creative",
            )
        )

        queue = MotiveQueue(motives=motives, updated_at=_now_iso())
        return queue

    def select_motive(self, now: Optional[datetime] = None) -> Optional[MotiveScore]:
        """Return the highest-scoring motive (or None if companion disabled)."""
        if not self.enabled:
            return None
        return self.rank_motives(now=now).top()

    def apply_reflection(
        self,
        *,
        summary: str,
        relation: str = "reflection",
        evidence_event_ids: Optional[List[str]] = None,
        energy_delta: int = 0,
        mood: str = "",
        thread: str = "",
    ) -> None:
        """Rewrite SelfState from a reflection/consolidate insight.

        Called after nightly consolidate or diary/dream so reflection is not
        archive-only — it updates energy/mood/salient and may open a thread.
        """
        if not self.enabled:
            return
        text = " ".join(str(summary or "").replace("\x00", "").split())
        if not text:
            return
        try:
            self.ensure_life_state()
        except Exception:
            return

        def _mutate(state: LifeState) -> None:
            if energy_delta:
                state.energy = max(0, min(100, int(state.energy) + int(energy_delta)))
            if mood:
                state.mood_bias = str(mood)[:20]
            # Soft seed for later dynamic/express motives.
            if relation in {"reflection", "updates", "corrects"} and not state.message_seed:
                state.message_seed = text[:60]
            state.updated_at = _now_iso()

        try:
            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate)
            else:
                state = self.store.get_life_state()
                _mutate(state)
                self.store.save_life_state(state)
        except Exception:
            logger.debug("[%s] apply_reflection mutate failed", self.account_id, exc_info=True)

        eid = ""
        for raw in evidence_event_ids or []:
            cand = str(raw or "").strip()
            if cand:
                eid = cand
                break
        line = f"反思：{text[:80]}"
        self._push_salient_self(
            line=line,
            thread=str(thread or "").strip()[:80],
            event_id=eid,
        )

    def reflection_to_motive(
        self,
        *,
        summary: str,
        prefer_action: str = "",
    ) -> Optional[MotiveScore]:
        """Map a reflection summary into a soft motive boost suggestion."""
        if not self.enabled:
            return None
        text = str(summary or "")
        action = str(prefer_action or "").strip()
        if not action:
            if any(k in text for k in ("想写", "创作", "小说", "续写")):
                action = "creative"
            elif any(k in text for k in ("想发", "动态", "分享")):
                action = "post_dynamic"
            elif any(k in text for k in ("累", "休息", "困")):
                action = "rest"
            elif any(k in text for k in ("探索", "查", "了解", "兴趣")):
                action = "explore"
            else:
                action = "browse_video"
        # Persist a light afterglow so rank_motives can see it.
        try:
            self.apply_reflection(summary=text[:120], relation="reflection")
        except Exception:
            pass
        return MotiveScore(
            name="反思驱动",
            score=7.0,
            reason=text[:80],
            suggested_action=action,
        )

    def get_self_snapshot(self) -> SelfSnapshot:
        """Unified generation-facing self read model (SelfSnapshot)."""
        try:
            state = self.ensure_life_state() if self.enabled else self.store.get_life_state()
        except Exception:
            try:
                state = self.store.get_life_state()
            except Exception:
                return SelfSnapshot()
        snap = SelfSnapshot.from_life_state(state)
        # Prefer schedule-derived current activity when available.
        try:
            plan = self.store.get_daily_plan()
            if plan and getattr(plan, "items", None):
                now_m = now_cn().hour * 60 + now_cn().minute
                for it in plan.items:
                    s = _parse_hhmm(it.time)
                    e = _parse_hhmm(it.end)
                    if s is None:
                        continue
                    if e is None:
                        e = s + 60
                    if s <= now_m < e or (e <= s and (now_m >= s or now_m < e)):
                        if it.activity:
                            snap.activity = it.activity
                        if it.message_seed:
                            snap.message_seed = it.message_seed
                        break
        except Exception:
            pass
        return snap

    def get_prompt_surface(self) -> str:
        """Short block for system/user prompt injection."""
        if not self.enabled or not self._cfg.life_state.enabled:
            return ""
        if not self._cfg.life_state.inject_into_replies:
            return ""
        state = self.store.get_life_state()
        plan = self.store.get_daily_plan()
        detail = self.store.get_story_detail()
        # Prefer SelfSnapshot core (energy/mood/salient/threads), then schedule.
        snap = SelfSnapshot.from_life_state(state)
        current = snap.activity or state.activity or ""
        seed = snap.message_seed or state.message_seed or ""
        if plan.items:
            now_m = now_cn().hour * 60 + now_cn().minute
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
        threads = [
            str(x).strip()
            for x in (getattr(state, "ongoing_threads", None) or [])
            if str(x or "").strip()
        ]
        if threads:
            lines.append("进行中：" + "；".join(threads[:4]))
        salient = [
            str(x).strip().split("|eid=")[0]
            for x in (getattr(state, "salient_recent", None) or [])
            if str(x or "").strip()
        ]
        if salient:
            lines.append("刚经历：" + "；".join(s[:40] for s in salient[:3]))
        # Expose SelfSnapshot marker so generation paths can detect unified self.
        if snap.salient_recent or snap.ongoing_threads:
            lines.append(snap.prompt_block(max_salient=2, max_threads=2).split("\n")[0])
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
        now = now or now_cn()
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
        memory_event_ids: Optional[List[str]] = None,
        oid: str = "",
    ) -> None:
        """Feedback from operational proactive-video into living state (soft)."""
        if not self.enabled:
            return
        try:
            self.ensure_life_state()
            # Watching always costs some energy. A high score improves mood, but
            # must not erase the physical cost of a long audiovisual session.
            delta = -2
            try:
                sc = float(score)
            except (TypeError, ValueError):
                sc = 0.0
            if sc >= 8:
                delta = -1
            elif sc >= 6:
                delta = -2
            elif sc > 0:
                delta = -3
            short_title = (title or "一个视频")[:40]
            mood_s = str(mood or "")[:20]
            seed = str(review or comment or "")[:60]

            def _mutate_video(state: LifeState) -> None:
                state.energy = max(0, min(100, int(state.energy) + delta))
                if mood_s:
                    state.mood_bias = mood_s
                state.activity = f"刚看了《{short_title}》"
                if seed:
                    state.message_seed = seed
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                state = updater(_mutate_video)
            else:
                state = self.store.get_life_state()
                _mutate_video(state)
                self.store.save_life_state(state)
            # Continuous self: recent watch must appear in reply surface without FTS.
            salient = f"看了《{short_title}》"
            if sc > 0:
                salient += f"，评分{sc:g}"
            if comment:
                salient += "，还发了评论"
            bound_eid = ""
            if memory_event_ids:
                for raw in memory_event_ids:
                    cand = str(raw or "").strip()
                    if cand:
                        bound_eid = cand
                        break
            # Prefer durable brain event ids; fall back to bvid so SelfSnapshot
            # lines stay evidence-linkable even when the caller omits ids.
            if not bound_eid and bvid:
                bound_eid = f"bvid:{str(bvid).strip()}"
            # Replace any prior "最近在看：" thread; only keep when score is high.
            self._push_salient_self(
                line=salient[:120],
                # Category prefix 最近在看： replaces prior watch thread.
                thread=f"最近在看：《{short_title}》" if sc >= 7 else "",
                close_thread_prefix="" if sc >= 7 else "最近在看：",
                event_id=bound_eid,
            )
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
            runtime_patch: Dict[str, Any] = {
                "last_proactive_video_at": _now_iso(),
                "last_proactive_video_bvid": bvid or "",
                "last_proactive_video_score": sc,
                "last_proactive_video_title": short_title,
                "browse_failure_streak": 0,
                "last_browse_success_ts": time.time(),
                # A satisfying watch should create an afterglow, not an
                # immediate urge to open another video every scheduler tick.
                "browse_cooldown_until": time.time() + 45 * 60,
            }
            previous_at = float((self.store.get_runtime() or {}).get("last_proactive_video_ts") or 0)
            previous_count = int((self.store.get_runtime() or {}).get("browse_session_count") or 0)
            runtime_patch["browse_session_count"] = previous_count + 1 if time.time() - previous_at < 2 * 3600 else 1
            runtime_patch["last_proactive_video_ts"] = time.time()
            if oid:
                runtime_patch["last_proactive_video_oid"] = str(oid)[:32]
            if memory_event_ids:
                runtime_patch["last_proactive_video_memory_event_ids"] = [
                    str(x) for x in list(memory_event_ids)[:12] if str(x)
                ]
            if review:
                runtime_patch["last_proactive_video_review"] = str(review)[:120]
            self.store.patch_runtime(**runtime_patch)
            logger.info(
                "[%s] companion feedback from video 《%s》 score=%s energy→%s",
                self.account_id,
                short_title,
                sc,
                state.energy,
            )
        except Exception as e:
            logger.warning("[%s] on_proactive_video_finished failed: %s", self.account_id, e)

    def on_proactive_video_started(self, *, title: str = "", bvid: str = "") -> None:
        """Make the current activity visible as soon as watching really starts."""
        if not self.enabled:
            return
        short_title = " ".join(str(title or "一个视频").split())[:40]
        try:
            self.ensure_life_state()

            def _mutate(state: LifeState) -> None:
                state.energy = max(0, min(100, int(state.energy) - 1))
                state.activity = f"正在看《{short_title}》"
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate)
            self.store.patch_runtime(
                last_proactive_video_started_at=_now_iso(),
                last_proactive_video_started_bvid=str(bvid or "")[:24],
                last_proactive_video_started_title=short_title,
                last_proactive_video_attempt_at=_now_iso(),
                last_proactive_video_attempt_bvid=str(bvid or "")[:24],
            )
        except Exception:
            logger.debug("[%s] proactive video start feedback failed", self.account_id, exc_info=True)

    def on_proactive_video_failed(
        self,
        *,
        title: str = "",
        bvid: str = "",
        reason: str = "",
        memory_event_id: str = "",
    ) -> None:
        """Turn a failed watch into honest lived state and a persistent retry cooldown."""
        if not self.enabled:
            return
        now_ts = time.time()
        runtime = self.store.get_runtime() or {}
        try:
            streak = max(0, int(runtime.get("browse_failure_streak") or 0)) + 1
        except (TypeError, ValueError):
            streak = 1
        short_title = " ".join(str(title or "这个视频").split())[:40]
        reason_s = " ".join(str(reason or "unknown").split())[:100]
        cooldown_seconds = min(6 * 3600, 20 * 60 * (2 ** min(streak - 1, 4)))
        try:
            self.ensure_life_state()

            def _mutate(state: LifeState) -> None:
                state.energy = max(0, min(100, int(state.energy) - 1))
                state.mood_bias = "有点扫兴" if streak < 3 else "疲惫"
                state.activity = f"想看《{short_title}》，但没看成"
                state.message_seed = "先换件事做，晚点再看看"
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate)
            self._push_salient_self(
                line=f"想看《{short_title}》但没看成（{reason_s[:36]}）",
                event_id=str(memory_event_id or (f"bvid:{bvid}" if bvid else "")),
                close_thread_prefix="最近在看：",
            )
            failures = runtime.get("video_failure_cooldowns") or {}
            if not isinstance(failures, dict):
                failures = {}
            if bvid:
                failures[str(bvid)] = {
                    "until": now_ts + cooldown_seconds,
                    "reason": reason_s,
                    "count": streak,
                }
            # Bound runtime growth and discard expired entries.
            compact = {
                str(key): value
                for key, value in list(failures.items())[-80:]
                if isinstance(value, dict) and float(value.get("until") or 0) > now_ts
            }
            self.store.patch_runtime(
                browse_failure_streak=streak,
                browse_cooldown_until=now_ts + min(cooldown_seconds, 90 * 60),
                last_browse_failure_ts=now_ts,
                last_browse_failure_reason=reason_s,
                last_proactive_video_attempt_at=_now_iso(),
                last_proactive_video_attempt_bvid=str(bvid or "")[:24],
                last_proactive_video_failed_at=_now_iso(),
                last_proactive_video_failed_reason=reason_s,
                video_failure_cooldowns=compact,
                browse_session_count=0,
            )
        except Exception:
            logger.debug("[%s] proactive video failure feedback failed", self.account_id, exc_info=True)

    def is_video_in_cooldown(self, bvid: str, now: Optional[datetime] = None) -> bool:
        if not bvid:
            return False
        runtime = self.store.get_runtime() or {}
        failures = runtime.get("video_failure_cooldowns") or {}
        item = failures.get(str(bvid)) if isinstance(failures, dict) else None
        try:
            return bool(item) and float(item.get("until") or 0) > (now or now_cn()).timestamp()
        except (TypeError, ValueError, AttributeError):
            return False

    def on_proactive_video_candidates_exhausted(self, reason: str = "") -> None:
        """Lower browsing drive when the current feed has nothing usable."""
        if not self.enabled:
            return
        now_ts = time.time()
        runtime = self.store.get_runtime() or {}
        count = max(0, int(runtime.get("feed_exhaustion_count") or 0)) + 1
        try:
            existing_cooldown = float(runtime.get("browse_cooldown_until") or 0)
        except (TypeError, ValueError):
            existing_cooldown = 0.0
        self.store.patch_runtime(
            feed_exhaustion_count=count,
            last_feed_exhaustion_reason=str(reason or "candidate_exhausted")[:120],
            last_feed_exhaustion_ts=now_ts,
            browse_cooldown_until=max(existing_cooldown, now_ts + 45 * 60),
            browse_session_count=0,
        )
        try:
            def _mutate(state: LifeState) -> None:
                state.activity = "这一轮没刷到想看的，先换件事做"
                state.message_seed = "换个分区或晚点再看"
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate)
        except Exception:
            logger.debug("[%s] feed exhaustion feedback failed", self.account_id, exc_info=True)

    def on_dynamic_posted(
        self,
        content: str = "",
        topic: str = "",
        *,
        draft_id: str = "",
        dynamic_id: str = "",
        task_id: str = "",
    ) -> None:
        """Soft life-state feedback after a dynamic is published.

        Brain archival of the post itself is owned by scheduler
        (``bot_action`` / ``dynamic_post``); this only updates local life surface
        and records correlation ids so diary/explore can reference the same post.
        """
        if not self.enabled:
            return
        try:
            self.ensure_life_state()
            preview = (content or topic or "").strip().replace("\n", " ")

            def _mutate_dyn(state: LifeState) -> None:
                state.energy = max(0, min(100, int(state.energy) - 1))
                # The urge has been expressed; keeping the same seed would make
                # post_dynamic dominate every later motive ranking.
                state.message_seed = ""
                state.activity = "刚发了条动态"
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate_dyn)
            else:
                state = self.store.get_life_state()
                _mutate_dyn(state)
                self.store.save_life_state(state)
            self._push_salient_self(
                line=(
                    f"发了动态"
                    f"{('：' + preview[:48]) if preview else ''}"
                )[:120]
            )
            runtime_patch: Dict[str, Any] = {
                "last_dynamic_at": _now_iso(),
                "last_dynamic_ts": time.time(),
                "dynamic_cooldown_until": time.time() + 4 * 3600,
            }
            if draft_id:
                runtime_patch["last_dynamic_draft_id"] = str(draft_id)[:64]
            if dynamic_id:
                runtime_patch["last_dynamic_id"] = str(dynamic_id)[:64]
            if task_id:
                runtime_patch["last_dynamic_task_id"] = str(task_id)[:64]
            if content:
                runtime_patch["last_dynamic_preview"] = str(content)[:120]
            if topic:
                runtime_patch["last_dynamic_topic"] = str(topic)[:80]
            self.store.patch_runtime(**runtime_patch)
        except Exception as e:
            logger.warning("[%s] on_dynamic_posted failed: %s", self.account_id, e)

    def on_private_message_replied(
        self,
        *,
        preview: str = "",
        actor_label: str = "",
    ) -> None:
        """Soft life-state feedback after an outgoing private message is sent.

        Privacy: PM body must never enter ``message_seed`` / ``salient_recent``
        (those are injected into public replies). Body may only land on
        non-injected runtime fields for operator debugging.
        """
        if not self.enabled:
            return
        try:
            self.ensure_life_state()
            safe_preview = " ".join(str(preview or "").replace("\x00", "").split())[:48]
            who = " ".join(str(actor_label or "").replace("\x00", "").split())[:20]

            def _mutate_pm(state: LifeState) -> None:
                state.energy = max(0, min(100, int(state.energy) - 1))
                state.activity = "刚回了私信"
                # Generic seed only — never the PM body.
                state.message_seed = "刚回了私信"
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate_pm)
            else:
                state = self.store.get_life_state()
                _mutate_pm(state)
                self.store.save_life_state(state)
            # Actor pseudonym is PM-specific and must not enter the public
            # prompt surface (message_seed / salient_recent). Keep it only in
            # the non-injected runtime debug trail.
            line = "回了私信"
            self._push_salient_self(line=line[:120])
            runtime_patch: Dict[str, Any] = {"last_private_message_at": _now_iso()}
            if safe_preview:
                # Non-injected debug trail only (not in get_prompt_surface).
                runtime_patch["last_private_message_preview"] = safe_preview[:120]
            if who:
                runtime_patch["last_private_message_actor"] = who
            self.store.patch_runtime(**runtime_patch)
        except Exception as e:
            logger.warning("[%s] on_private_message_replied failed: %s", self.account_id, e)

    def on_comment_replied(
        self,
        *,
        title: str = "",
        preview: str = "",
        proactive: bool = False,
    ) -> None:
        """Soft life-state feedback after a public comment reply is published."""
        if not self.enabled:
            return
        try:
            self.ensure_life_state()
            short_title = (title or "").strip()[:40]
            kind = "主动评论" if proactive else "回复评论"
            safe_preview = " ".join(str(preview or "").replace("\x00", "").split())[:48]
            activity = f"刚{kind}" + (f"《{short_title}》" if short_title else "")

            def _mutate_cmt(state: LifeState) -> None:
                state.energy = max(0, min(100, int(state.energy) - 1))
                state.activity = activity
                if safe_preview:
                    state.message_seed = safe_preview[:60]
                state.updated_at = _now_iso()

            updater = getattr(self.store, "update_life_state", None)
            if callable(updater):
                updater(_mutate_cmt)
            else:
                state = self.store.get_life_state()
                _mutate_cmt(state)
                self.store.save_life_state(state)
            line = kind
            if short_title:
                line += f"《{short_title}》"
            if safe_preview:
                line += f"：{safe_preview}"
            self._push_salient_self(line=line[:120])
        except Exception as e:
            logger.warning("[%s] on_comment_replied failed: %s", self.account_id, e)

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
            "memory_brain_bound": bool(self.memory_brain),
            "last_archive_error": self._last_archive_error,
            "archive_fail_count": int(self._archive_fail_count or 0),
        }

    # ── life state ──

    def _roll_life_state_if_needed(
        self,
        state: LifeState,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """Mutate ``state`` in place when its date is stale (day roll).

        Called inside ``store.update_life_state``'s lock so the roll and any
        concurrent energy/salient update cannot clobber each other.
        """
        now = now or now_cn()
        today = now.strftime("%Y-%m-%d")
        if state.date == today:
            return

        # day roll — keep continuous self so overnight threads/recent acts
        # still ground generation; only reset daily energy/mood shell.
        dream = self.store.get_latest_dream()
        energy = self._cfg.life_state.energy_default
        mood = "平稳"
        afterglow = ""
        sleep = "正常"
        previous_energy = int(getattr(state, "energy", energy) or energy)
        late_roll = now.hour < 6
        if late_roll:
            # Crossing midnight while still active is not a full recharge.
            energy = min(previous_energy, max(0, energy - 12))
            mood = "困倦"
            sleep = "晚睡/通宵"
        dream_is_recent = False
        if dream and dream.date:
            try:
                dream_date = datetime.strptime(dream.date, "%Y-%m-%d").date()
                dream_age_days = (now.date() - dream_date).days
                dream_is_recent = 0 <= dream_age_days <= 1
            except (TypeError, ValueError):
                dream_is_recent = False
        if dream_is_recent:
            # A dream may color the following day, but must not become a
            # permanent afterglow when diary/dream generation missed days.
            energy = max(0, min(100, energy + int(dream.energy_delta or 0)))
            mood = dream.mood or mood
            afterglow = dream.afterglow or ""
        prev_salient = [
            str(x).strip()
            for x in (getattr(state, "salient_recent", None) or [])
            if str(x or "").strip()
        ][:5]
        prior_threads = [
            str(x).strip()
            for x in (getattr(state, "ongoing_threads", None) or [])
            if str(x or "").strip() and "已完成" not in str(x)
        ]
        # Day roll is the decay boundary for transient thoughts. Keep a
        # recently watched item into tomorrow, but expire it after 36h so
        # the same old topic cannot pollute every future plan/recall. Older
        # installations lack per-thread timestamps; keep each legacy row
        # once and stamp it now for the next roll.
        runtime = self.store.get_runtime() or {}
        raw_thread_stamps = runtime.get("ongoing_thread_timestamps") or {}
        thread_stamps = (
            dict(raw_thread_stamps) if isinstance(raw_thread_stamps, dict) else {}
        )
        now_ts = now.timestamp()
        prev_threads: List[str] = []
        kept_stamps: Dict[str, float] = {}
        for item in prior_threads:
            persistent = item.startswith(("小说：", "追番："))
            try:
                stamped_at = float(thread_stamps.get(item) or 0)
            except (TypeError, ValueError):
                stamped_at = 0.0
            if not stamped_at:
                stamped_at = now_ts
            if persistent or now_ts - stamped_at <= 36 * 3600:
                prev_threads.append(item)
                kept_stamps[item] = stamped_at
            if len(prev_threads) >= 4:
                break
        try:
            self.store.patch_runtime(ongoing_thread_timestamps=kept_stamps)
        except Exception:
            pass
        state.date = today
        state.energy = energy
        state.sleep = sleep
        state.mood_bias = mood
        state.activity = "夜深了，正在收尾准备休息" if late_roll else ""
        state.message_seed = "先休息，醒来再继续" if late_roll else ""
        state.conditions = []
        state.dream_afterglow = afterglow
        state.salient_recent = prev_salient
        state.ongoing_threads = prev_threads
        state.updated_at = _now_iso()

    def ensure_life_state(self) -> LifeState:
        """Load LifeState and apply day roll atomically under the store lock."""
        def _mutate(state: LifeState) -> None:
            self._roll_life_state_if_needed(state)

        return self.store.update_life_state(_mutate)

    def _refresh_life_rhythm(self, now: Optional[datetime] = None) -> LifeState:
        """Apply bounded time-of-day energy drift at most once per 30 minutes."""
        now = now or now_cn()
        state = self.ensure_life_state()
        runtime = self.store.get_runtime() or {}
        now_ts = now.timestamp()
        try:
            last_ts = float(runtime.get("last_life_rhythm_ts") or 0.0)
        except (TypeError, ValueError):
            last_ts = 0.0
        if last_ts <= 0.0:
            # First run: establish the baseline instead of silently returning
            # forever (the missing key previously made elapsed always 0).
            self.store.patch_runtime(last_life_rhythm_ts=now_ts)
            return state
        elapsed = max(0.0, min(8 * 3600.0, now_ts - last_ts))
        if elapsed < 30 * 60:
            return state
        steps = max(1, int(elapsed // (30 * 60)))
        if now.hour >= 23 or now.hour < 7:
            sleeping = any(
                token in str(state.activity or "")
                for token in ("睡", "休息", "入眠", "躺下")
            )
            if sleeping:
                delta = min(8, steps)
                mood = "沉静" if state.energy >= 35 else "困倦"
            else:
                delta = -min(8, steps)
                mood = "困倦" if state.energy >= 25 else "疲惫"
        else:
            resting = any(
                token in str(state.activity or "")
                for token in ("休息", "午睡", "小憩", "放松")
            )
            if resting:
                # Daytime rest recovers a little energy so a drained bot is
                # not locked at 0 until the next day roll.
                delta = min(3, steps)
                mood = state.mood_bias
            else:
                # Natural awake drain is intentionally slower than activity costs.
                delta = -max(1, steps // 4)
                mood = state.mood_bias

        def _mutate(current: LifeState) -> None:
            current.energy = max(0, min(100, int(current.energy) + delta))
            if mood:
                current.mood_bias = mood
            current.updated_at = _now_iso()

        updater = getattr(self.store, "update_life_state", None)
        if callable(updater):
            state = updater(_mutate)
        self.store.patch_runtime(last_life_rhythm_ts=now_ts, last_life_rhythm_delta=delta)
        return state

    def _sync_state_from_plan(self, plan: DailyPlan, state: Optional[LifeState] = None) -> LifeState:
        del state  # kept for call-site compatibility; always operate atomically
        now_m = now_cn().hour * 60 + now_cn().minute
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

        def _mutate(cur: LifeState) -> None:
            self._roll_life_state_if_needed(cur)
            if current:
                cur.activity = current.activity
                cur.message_seed = current.message_seed
                if current.mood:
                    cur.mood_bias = current.mood
            cur.updated_at = _now_iso()

        return self.store.update_life_state(_mutate)

    def apply_activity_energy_delta(self, delta: int, reason: str = "") -> LifeState:
        energy_delta = int(delta)

        def _mutate(state: LifeState) -> None:
            self._roll_life_state_if_needed(state)
            state.energy = max(0, min(100, int(state.energy) + energy_delta))
            state.updated_at = _now_iso()

        state = self.store.update_life_state(_mutate)
        if reason:
            logger.debug(
                "[%s] energy %+d (%s) -> %s",
                self.account_id,
                energy_delta,
                reason,
                state.energy,
            )
        return state

    # ── schedule ──

    async def ensure_daily_plan(
        self, force: bool = False, *, fallback_only: bool = False
    ) -> DailyPlan:
        self.ensure_life_state()
        plan = self.store.get_daily_plan()
        today = _today()
        if not force and plan.date == today and plan.items:
            self._sync_state_from_plan(plan)
            return plan

        bits = self._persona_bits()
        state = self.store.get_life_state()
        snapshot = self.get_self_snapshot()
        now = now_cn()
        if not self._cfg.schedule.enabled or fallback_only:
            plan = DailyPlan(
                date=today,
                generated_at=_now_iso(),
                source="fallback_bridge" if fallback_only else "fallback",
                items=_fallback_plan_items(
                    self._cfg.schedule.item_count,
                    interests=bits.get("interests") or [],
                    energy=state.energy,
                    weekday=_WEEKDAYS[now.weekday()],
                    self_cues=list(snapshot.salient_recent or []),
                    ongoing_threads=list(snapshot.ongoing_threads or []),
                ),
                quality_score=50,
            )
            if fallback_only:
                bridge_key = f"companion_plan:{today}:bridge"
                bridge_recall = await self._recall_life_evidence(
                    query=f"{today} 凌晨生活衔接 当前精力 {state.energy}",
                    scene="life_plan",
                    limit=3,
                    action_key=bridge_key,
                    action_type="create_daily_plan",
                    current_activity="正在为跨过午夜后的生活补一段临时日程，避免继续沿用昨天的安排。",
                    title=f"凌晨日程衔接 {today}",
                    metadata={"date": today, "provisional": True},
                )
                canonical_text, canonical_meta = _canonical_companion_document(
                    "daily_plan", plan.to_dict()
                )
                archived = await self._archive_text(
                    source_type="life_plan",
                    event_type="daily_plan",
                    text=canonical_text,
                    title=f"日程 {today}（临时衔接）",
                    idempotency_key=f"daily_plan:{today}:bridge",
                    importance=0.35,
                    metadata={
                        "source": plan.source,
                        "quality": plan.quality_score,
                        "provisional": True,
                        "memory_event_ids": list(
                            bridge_recall.get("memory_event_ids") or []
                        )[:10],
                        **canonical_meta,
                    },
                    pause_on_error=False,
                )
                if not archived:
                    await self._fail_open_activity(
                        action_key=bridge_key,
                        action_type="create_daily_plan",
                        scene="life_plan",
                        title=f"凌晨日程衔接 {today}",
                        metadata={"date": today, "provisional": True},
                    )
                    raise RuntimeError("companion bridge plan memory archive failed")
                await self._finish_activity_memory(
                    action_key=bridge_key,
                    action_type="create_daily_plan",
                    result_text="跨午夜后的临时日程已经生成并归档。",
                    scene="life_plan",
                    title=f"凌晨日程衔接 {today}",
                    metadata={"date": today, "provisional": True},
                    salient_line=f"补好了凌晨到早晨的日程衔接（{today}）",
                    pause_on_error=False,
                )
            self.store.save_daily_plan(plan)
            self._sync_state_from_plan(plan)
            return plan

        try:
            self.store.patch_runtime(
                last_plan_attempt_at=_now_iso(),
                plan_attempt_date=today,
            )
        except Exception:
            pass
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
        plan_recall = await self._recall_life_evidence(
            query=(
                f"{today} 安排今天的生活 最近做过的事 日记 梦境 视频 番剧 "
                f"{state.activity or ''} {state.mood_bias or ''}"
            ),
            scene="life_plan",
            limit=5,
            action_key=f"companion_plan:{today}",
            action_type="create_daily_plan",
            current_activity=(
                "正在安排今天的生活计划，会参考最近做过的事、当前精力、心情和还想继续做的事。"
            ),
            title=f"日程 {today}",
            metadata={"date": today},
        )
        plan_memory = str(plan_recall.get("memory_evidence") or "").strip()
        if plan_memory:
            user = f"{user}\n\n【近期活动记忆】\n{plan_memory[:1600]}"
        raw = await self._llm_text(system, user, max_tokens=1200, scene="life_plan")
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
            partial_items = list(items)
            items = _fallback_plan_items(
                self._cfg.schedule.item_count,
                interests=bits.get("interests") or [],
                energy=state.energy,
                weekday=_WEEKDAYS[now.weekday()],
                self_cues=(
                    list(snapshot.salient_recent or [])
                    + [str(item) for item in (plan_recall.get("snippets") or [])[:4]]
                ),
                ongoing_threads=list(snapshot.ongoing_threads or []),
            )
            if partial_items:
                items = _merge_partial_plan_items(items, partial_items)
                source = "fallback_merged"
            else:
                source = "fallback"
        score = _plan_quality(items)
        if score < 55 and source == "llm":
            # one soft retry
            raw2 = await self._llm_text(system, user + "\n上次时段质量偏低，请避免重叠并补全早晚。", max_tokens=1200, scene="life_plan")
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
        canonical_text, canonical_meta = _canonical_companion_document(
            "daily_plan", plan.to_dict()
        )
        archived = await self._archive_text(
            source_type="life_plan",
            event_type="daily_plan",
            text=canonical_text,
            title=f"日程 {today}",
            idempotency_key=f"daily_plan:{today}",
            importance=0.45,
            metadata={
                "source": source,
                "quality": score,
                "memory_grounded": bool(plan_memory),
                "memory_event_ids": list(
                    plan_recall.get("memory_event_ids") or []
                )[:10],
                **canonical_meta,
            },
            pause_on_error=False,
        )
        if not archived:
            await self._fail_open_activity(
                action_key=f"companion_plan:{today}",
                action_type="create_daily_plan",
                scene="life_plan",
                title=f"日程 {today}",
                metadata={"date": today},
            )
            raise RuntimeError("companion daily plan memory archive failed")
        await self._finish_activity_memory(
            action_key=f"companion_plan:{today}",
            action_type="create_daily_plan",
            result_text="今天的生活计划已经生成并归档。",
            scene="life_plan",
            title=f"日程 {today}",
            metadata={"date": today},
            salient_line=f"写好了今天的日程安排（{today}）",
            pause_on_error=False,
        )
        self.store.save_daily_plan(plan)
        self._sync_state_from_plan(plan)
        runtime_patch: Dict[str, Any] = {
            "last_plan_at": _now_iso(),
            "plan_source": source,
            "plan_attempt_date": today,
        }
        if source == "llm":
            runtime_patch.update(
                plan_generation_failures=0,
                last_plan_error="",
                next_plan_retry_at=0,
            )
        else:
            runtime = self.store.get_runtime() or {}
            prior_failures = (
                int(runtime.get("plan_generation_failures") or 0)
                if str(runtime.get("plan_attempt_date") or "") == today
                else 0
            )
            failures = prior_failures + 1
            retry_delay = min(6 * 3600, 30 * 60 * (2 ** min(failures - 1, 3)))
            runtime_patch.update(
                plan_generation_failures=failures,
                last_plan_error="llm_empty_or_invalid_schedule",
                next_plan_retry_at=time.time() + retry_delay,
            )
        self.store.patch_runtime(**runtime_patch)
        return plan

    async def ensure_detail_enhancement(self) -> Optional[StoryDetail]:
        if not self.enabled or not self._cfg.schedule.enabled:
            return None
        plan = self.store.get_daily_plan()
        if plan.date != _today() or not plan.items:
            return None
        lead = self._cfg.schedule.detail_lead_minutes
        now_m = now_cn().hour * 60 + now_cn().minute
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
            evidence="",
        )
        detail_recall = await self._recall_life_evidence(
            query=(
                f"{plan.date} {window} {target.activity} 当前生活时段 最近做过的事"
            ),
            scene="life_plan",
            limit=4,
            action_key=f"companion_life_detail:{seg_key}",
            action_type="expand_life_detail",
            current_activity=(
                "正在细化当前生活时段，先回顾今天已经做过什么、现在处于哪个安排、接下来准备做什么。"
            ),
            title=f"生活时段 {window}",
            metadata={"segment_key": seg_key, "date": plan.date},
        )
        detail_memory = str(detail_recall.get("memory_evidence") or "").strip()
        if detail_memory:
            system, user = P.build_detail_prompt(
                window=window,
                activity=target.activity,
                mood=target.mood,
                persona_name=bits["name"],
                energy=state.energy,
                evidence=detail_memory,
            )
        raw = await self._llm_text(system, user, max_tokens=1500, scene="life_plan")
        summary = target.activity
        events: List[str] = []
        hooks: List[str] = []
        if raw:
            data = _extract_json(raw)
            if isinstance(data, dict):
                summary = str(data.get("summary") or summary)
                events = [str(x) for x in (data.get("events") or []) if str(x).strip()][:5]
                hooks = [str(x) for x in (data.get("proactive_hooks") or []) if str(x).strip()][:3]
        if not events:
            # A detail record should describe at least one concrete thing, even
            # when the LLM is unavailable. Avoid the old summary-only shell.
            events = [f"按当前安排进行：{str(target.activity or window).strip()[:120]}"]
            for snippet in (detail_recall.get("snippets") or [])[:2]:
                clean = " ".join(str(snippet or "").split())[:120]
                if clean and clean not in events:
                    events.append(f"最近经历可作为衔接：{clean}")
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
        detail_text, detail_canonical_meta = _canonical_companion_document(
            "life_detail", detail.to_dict()
        )
        archived = await self._archive_text(
            source_type="life_plan",
            event_type="life_detail",
            text=detail_text,
            title=f"生活时段 {window}",
            idempotency_key=f"life_detail:{seg_key}",
            importance=0.35,
            metadata={
                "segment_key": seg_key,
                "window": window,
                "memory_grounded": bool(detail_memory),
                "memory_event_ids": list(
                    detail_recall.get("memory_event_ids") or []
                )[:10],
                **detail_canonical_meta,
            },
            pause_on_error=False,
        )
        if not archived:
            await self._fail_open_activity(
                action_key=f"companion_life_detail:{seg_key}",
                action_type="expand_life_detail",
                scene="life_plan",
                title=f"生活时段 {window}",
                metadata={"segment_key": seg_key, "date": plan.date},
            )
            raise RuntimeError("companion life detail memory archive failed")
        await self._finish_activity_memory(
            action_key=f"companion_life_detail:{seg_key}",
            action_type="expand_life_detail",
            result_text="当前生活时段已经细化并归档。",
            scene="life_plan",
            title=f"生活时段 {window}",
            metadata={"segment_key": seg_key, "date": plan.date},
            salient_line=f"细化了生活时段：{window}",
            pause_on_error=False,
        )
        self.store.save_story_detail(detail)

        def _mutate_life(cur: LifeState) -> None:
            self._roll_life_state_if_needed(cur)
            cur.activity = target.activity
            cur.message_seed = hooks[0] if hooks else target.message_seed
            cur.updated_at = _now_iso()

        self.store.update_life_state(_mutate_life)
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
        # Same-day idempotency applies to force=True too: force means "run now",
        # not "archive a second dream for the same date into the brain".
        if existing and existing.date == today and existing.content:
            return existing
        bits = self._persona_bits()
        frags = self._normalize_fragments(self.store.get_dream_fragments())
        frag_texts = [f.text for f in frags[:10]]
        plan = self.store.get_daily_plan()
        plan_sum = P.format_plan_summary([i.to_dict() for i in plan.items])
        diaries = self.store.get_diaries()
        diary_hint = diaries[0].summary if diaries else ""
        # V6 混合召回：梦境生成侧读近期经历，避免「只写不读」
        dream_recall = await self._recall_life_evidence(
            # Scene recipe + SelfState needles are applied inside _recall_life_evidence.
            query=(self.ensure_life_state().activity if self.enabled else "") or "",
            scene="dream",
            limit=5,
            action_key=f"companion_dream:{today}",
            action_type="write_dream",
            current_activity=(
                "正在整理今天的梦境，会结合最近看过、做过、写过和感受过的事情形成连续的梦。"
            ),
            title=f"梦境 {today}",
            metadata={"date": today},
        )
        memory_hint = str(dream_recall.get("memory_evidence") or "").strip()
        if not memory_hint and dream_recall.get("snippets"):
            memory_hint = "\n".join(
                f"- {s}" for s in (dream_recall.get("snippets") or [])[:5]
            )
        try:
            system, user = P.build_dream_prompt(
                persona_name=bits["name"],
                persona_prompt=bits["base_prompt"],
                fragments=frag_texts,
                plan_summary=plan_sum,
                diary_hint=diary_hint,
                memory_evidence=memory_hint,
            )
        except TypeError:
            system, user = P.build_dream_prompt(
                persona_name=bits["name"],
                persona_prompt=bits["base_prompt"],
                fragments=frag_texts,
                plan_summary=plan_sum,
                diary_hint=diary_hint,
            )
            if memory_hint:
                user = f"{user}\n\n近期记忆/经历（可选呼应）：\n{memory_hint[:900]}"
        raw = await self._llm_text(system, user, max_tokens=1000, scene="dream")
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
        dream_text, dream_canonical_meta = _canonical_companion_document(
            "dream", dream.to_dict()
        )
        archived = await self._archive_text(
            source_type="dream",
            event_type="dream",
            text=dream_text,
            title=dream.label or f"梦境 {today}",
            idempotency_key=f"dream:{today}",
            importance=0.5,
            metadata={
                "mood": dream.mood,
                "afterglow": dream.afterglow,
                "memory_grounded": bool(memory_hint),
                "memory_event_ids": list(dream_recall.get("memory_event_ids") or [])[:12],
                **dream_canonical_meta,
            },
            pause_on_error=False,
        )
        if not archived:
            await self._fail_open_activity(
                action_key=f"companion_dream:{today}",
                action_type="write_dream",
                scene="dream",
                title=f"梦境 {today}",
                metadata={"date": today},
            )
            raise RuntimeError("companion dream memory archive failed")
        await self._finish_activity_memory(
            action_key=f"companion_dream:{today}",
            action_type="write_dream",
            result_text="今天的梦境已经整理并归档。",
            scene="dream",
            title=f"梦境 {today}",
            metadata={"date": today},
            salient_line=(
                f"做了个梦「{dream.label or '无题'}」"
                f"{('：' + (dream.content or '')[:36]) if dream.content else ''}"
            ),
            pause_on_error=False,
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
        # update life afterglow atomically
        dream_afterglow = dream.afterglow
        dream_mood = dream.mood or ""
        dream_energy_delta = int(dream.energy_delta or 0)

        def _mutate_life(cur: LifeState) -> None:
            self._roll_life_state_if_needed(cur)
            cur.dream_afterglow = dream_afterglow
            if dream_mood:
                cur.mood_bias = dream_mood
            cur.energy = max(0, min(100, cur.energy + dream_energy_delta))
            cur.updated_at = _now_iso()

        self.store.update_life_state(_mutate_life)
        self.store.patch_runtime(last_dream_at=_now_iso())
        return dream

    async def generate_diary(self, force: bool = False) -> Optional[DiaryEntry]:
        if not self.enabled or not self._cfg.diary.enabled:
            return None
        today = _today()
        diaries = self.store.get_diaries()
        # Same-day idempotency applies to force=True too: force means "run
        # now", not "archive a second diary for the same date".
        if any(d.date == today for d in diaries):
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
        # V6 混合召回：近期视频/动态/番剧/评论 + 生活面，替代脆弱 search_memories 字符串
        recall_bundle = await self._recall_life_evidence(
            query=f"{state.activity or ''} {state.message_seed or ''}".strip(),
            scene="diary",
            limit=6,
            action_key=f"companion_diary:{today}",
            action_type="write_diary",
            current_activity=(
                "正在写今天的日记，会回顾今天和最近做过的事、当前生活状态、梦境与真实感受。"
            ),
            title=f"日记 {today}",
            metadata={"date": today},
        )
        memory_evidence = str(recall_bundle.get("memory_evidence") or "").strip()
        memory_event_ids = list(recall_bundle.get("memory_event_ids") or [])
        for snip in recall_bundle.get("snippets") or []:
            if snip and snip not in evidence_parts:
                evidence_parts.append(str(snip)[:220])
        if memory_evidence and memory_evidence not in evidence_parts:
            evidence_parts.append(memory_evidence[:1800])
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
        raw = await self._llm_text(system, user, max_tokens=1100, scene="diary")
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
        diary_text, diary_canonical_meta = _canonical_companion_document(
            "diary", entry.to_dict()
        )
        archived = await self._archive_text(
            source_type="diary",
            event_type="diary",
            text=diary_text,
            title=f"日记 {today}",
            idempotency_key=f"diary:{today}",
            importance=0.6,
            metadata={
                "share_seed": entry.share_seed,
                "tags": entry.tags,
                "memory_event_ids": memory_event_ids[:12],
                "memory_grounded": bool(memory_evidence),
                **diary_canonical_meta,
            },
            pause_on_error=False,
        )
        if not archived:
            await self._fail_open_activity(
                action_key=f"companion_diary:{today}",
                action_type="write_diary",
                scene="diary",
                title=f"日记 {today}",
                metadata={"date": today},
            )
            raise RuntimeError("companion diary memory archive failed")
        await self._finish_activity_memory(
            action_key=f"companion_diary:{today}",
            action_type="write_diary",
            result_text="今天的日记已经写完并归档。",
            scene="diary",
            title=f"日记 {today}",
            metadata={"date": today},
            salient_line=(
                f"写了日记"
                f"{('：' + str(getattr(entry, 'summary', '') or '')[:40]) if getattr(entry, 'summary', '') else ''}"
            ),
            pause_on_error=False,
        )
        # prepend diary list only after the account brain confirms the source.
        diaries = [entry] + [d for d in diaries if d.date != today]
        diaries = diaries[: self._cfg.diary.max_entries]
        self.store.save_diaries(diaries)
        # fragments
        pool = self.store.get_dream_fragments()
        now = time.time()
        for t in entry.dream_fragments:
            pool.append(DreamFragment(text=t, weight=1.0, created_ts=now, source="diary", date=today))
        self.store.save_dream_fragments(self._normalize_fragments(pool))
        if self._cfg.diary.offer_dynamic_draft and entry.share_seed:
            self._offer_draft(entry.share_seed, created_by="companion_diary")
        self.store.patch_runtime(last_diary_at=_now_iso(), diary_date=today)
        # slight evening energy drain
        self.apply_activity_energy_delta(-3, "diary")
        return entry

    # ── exploration ──

    _EXPLORATION_RETRY_BACKOFF_SECONDS = 15 * 60

    def _exploration_due(self) -> bool:
        if not self._cfg.exploration.enabled:
            return False
        rt = self.store.get_runtime()
        last = rt.get("last_explore_ts") or 0
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0.0
        last_attempt = rt.get("last_explore_attempt_ts") or 0
        try:
            last_attempt = float(last_attempt)
        except (TypeError, ValueError):
            last_attempt = 0.0
        min_h = self._cfg.exploration.min_interval_hours
        now = time.time()
        if (now - last) < min_h * 3600:
            return False
        # A failed internal exploration must not retry every scheduler tick.
        # Keep the cooldown short enough to self-heal while isolating provider /
        # memory errors from the rest of the account.
        return (now - last_attempt) >= min(
            self._EXPLORATION_RETRY_BACKOFF_SECONDS,
            min_h * 3600,
        )

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
        # scene gate：优先 companion_exploration，回落到 proactive_video；
        # 实际调用必须使用与 gate 相同的 scene，否则 search() 内部二次
        # gate 会静默返回空结果，白白烧一次探索 LLM。
        search_scene = "companion_exploration"
        if hasattr(self.web_search, "is_scene_enabled"):
            if not self.web_search.is_scene_enabled(search_scene):
                if self.web_search.is_scene_enabled("proactive_video"):
                    search_scene = "proactive_video"
                else:
                    logger.debug(
                        "[%s] exploration skipped: web_search scene companion_exploration disabled",
                        self.account_id,
                    )
                    return None

        # One activity key represents one exploration attempt.  A date-only key
        # collides on the second exploration because the generation recall query
        # is intentionally part of the archived intent metadata and changes as
        # the self state evolves.
        explore_id = uuid.uuid4().hex[:12]
        explore_action_key = f"companion_explore:{_today()}:{explore_id}"
        self.store.patch_runtime(
            last_explore_attempt_ts=time.time(),
            last_explore_error="",
        )

        bits = self._persona_bits()
        state = self.store.get_life_state()
        plan = self.store.get_daily_plan()
        plan_sum = P.format_plan_summary([i.to_dict() for i in plan.items]) if plan.items else ""
        # 探索动机可吸收近期记忆/主动行为，避免只会空转兴趣词
        explore_recall = await self._recall_life_evidence(
            query=" ".join(
                x
                for x in (
                    state.activity or "",
                    " ".join((bits.get("interests") or [])[:6]),
                )
                if x
            ),
            scene="exploration",
            limit=4,
            action_key=explore_action_key,
            action_type="explore_topic",
            current_activity=(
                "正在主动探索一个感兴趣的话题，会结合最近经历和当前生活状态决定要查什么。"
            ),
            title=f"主动探索 {_today()}",
            metadata={"date": _today()},
            pause_on_error=False,
        )
        memory_seed = "；".join(explore_recall.get("snippets") or [])[:300]
        system, user = P.build_explore_query_prompt(
            persona_name=bits["name"],
            interests=bits.get("interests") or [],
            activity=state.activity,
            mood_bias=state.mood_bias,
            recent_topics=", ".join(
                x for x in (self.get_topic_seeds() + ([memory_seed] if memory_seed else [])) if x
            ),
            plan_summary=plan_sum,
        )
        raw = await self._llm_text(system, user, max_tokens=1200, scene="exploration")
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
                scene=search_scene,
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

        fallback_highlights: List[str] = []
        for item in items[:2]:
            if not isinstance(item, dict):
                continue
            title_s = " ".join(str(item.get("title") or "").split())[:80]
            snippet_s = " ".join(
                str(item.get("snippet") or item.get("content") or "").split()
            )[:180]
            line = "：".join(part for part in (title_s, snippet_s) if part)
            if line and line not in fallback_highlights:
                fallback_highlights.append(line)
        if not fallback_highlights and isinstance(result, str):
            compact_result = " ".join(result.split())[:220]
            if compact_result:
                fallback_highlights.append(compact_result)

        system2, user2 = P.build_explore_note_prompt(
            query=query,
            motive=motive,
            results_text=results_text,
            persona_prompt=bits["base_prompt"],
        )
        raw2 = await self._llm_text(system2, user2, max_tokens=500, scene="exploration")
        impression, self_link, should_share = (
            (
                "没搜到什么有用的，下次换个关键词试试。"
                if not search_ok
                else "查到两点：" + "；".join(fallback_highlights[:2])
                if fallback_highlights
                else "有搜索结果，但没有提取出可复述的具体要点。"
            ),
            "",
            False,
        )
        highlights: List[str] = list(fallback_highlights)
        if raw2:
            data = _extract_json(raw2)
            if isinstance(data, dict):
                model_impression = str(data.get("impression") or "").strip()
                if model_impression and model_impression != "看了一些资料。":
                    impression = model_impression
                self_link = str(data.get("self_link") or "")
                should_share = bool(data.get("should_share")) and search_ok
                model_highlights = [
                    str(x) for x in (data.get("highlights") or []) if str(x).strip()
                ][:5]
                if model_highlights:
                    highlights = model_highlights

        note = ExploreNote(
            id=explore_id,
            created_at=_now_iso(),
            query=query,
            motive=motive,
            impression=impression,
            self_link=self_link,
            should_share=should_share,
            items=[x for x in (items or [])[: self._cfg.exploration.max_results] if isinstance(x, dict)],
            source="web_search",
        )
        body = (
            f"探索：{query}\n动机：{motive}\n{impression}\n关联：{self_link}"
            + (f"\n要点：{'；'.join(highlights)}" if highlights else "")
        )
        archived = await self._archive_text(
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
                "memory_event_ids": list(explore_recall.get("memory_event_ids") or [])[:12],
                "memory_grounded": bool(explore_recall.get("memory_evidence")),
            },
            pause_on_error=False,
        )
        if not archived:
            await self._fail_open_activity(
                action_key=explore_action_key,
                action_type="explore_topic",
                scene="exploration",
                title=f"主动探索 {_today()}",
                metadata={"date": _today()},
            )
            raise RuntimeError("companion exploration memory archive failed")
        await self._finish_activity_memory(
            action_key=explore_action_key,
            action_type="explore_topic",
            result_text="本次主动探索已经完成并归档。",
            scene="exploration",
            title=f"主动探索 {_today()}",
            metadata={"date": _today()},
            salient_line=f"探索了「{(query or '')[:40]}」",
            # Keep only the latest interest thread (replace prior 兴趣： entries).
            ongoing_thread=f"兴趣：{(query or '')[:36]}" if query else "",
            close_thread_prefix="兴趣：",
            pause_on_error=False,
        )
        # Local companion surface becomes visible only after the brain commit.
        notes = [note] + self.store.get_explore_notes()
        self.store.save_explore_notes(notes[:40])
        # 无结果不进草稿；有结果且模型认为可分享才进
        if self._cfg.exploration.offer_dynamic_draft and should_share and impression and search_ok:
            seed = f"【随便搜到】{query}\n{impression[:400]}"
            self._offer_draft(seed, created_by="companion_explore")
        self.store.patch_runtime(
            last_explore_ts=time.time(),
            last_explore_query=query,
            last_explore_ok=search_ok,
            last_explore_error="",
        )
        self.apply_activity_energy_delta(-2, "explore")
        return note

    # ── creative ──

    def _creative_idle_ok(self) -> bool:
        if now_cn().hour < 7:
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
                project_recall = await self._recall_life_evidence(
                    query=(
                        f"创作新项目 灵感 最近经历 视频 番剧 日记 梦境 {insp} "
                        f"{state.activity or ''}"
                    ),
                    scene="creative",
                    limit=5,
                    action_key=(
                        f"companion_creative_project:{_today()}:{len(projects)}"
                    ),
                    action_type="create_creative_project",
                    current_activity=(
                        "正在构思一个新的创作项目，会结合最近做过的事、日记、梦境和已有兴趣决定写什么。"
                    ),
                    title=f"新创作项目 {_today()}",
                    metadata={"date": _today(), "project_slot": len(projects)},
                )
                project_memory = str(
                    project_recall.get("memory_evidence") or ""
                ).strip()
                if project_memory:
                    insp = f"{insp}\n\n【近期活动记忆】\n{project_memory[:1400]}"
                system, user = P.build_creative_project_prompt(
                    persona_name=bits["name"],
                    persona_prompt=bits["base_prompt"] + (("\n" + bits.get("creative_rules", "")) if bits.get("creative_rules") else ""),
                    inspiration=insp,
                )
                raw = await self._llm_text(system, user, max_tokens=1500, scene="creative")
                meta = _extract_json(raw) if raw else None
                project_action_key = (
                    f"companion_creative_project:{_today()}:{len(projects)}"
                )
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
                    archived = await self._archive_text(
                        source_type="creative",
                        event_type="creative_project",
                        text=(
                            f"创建《{proj.title}》："
                            f"{proj.premise or proj.inspiration_source or '新的创作计划'}"
                        ),
                        title=proj.title,
                        idempotency_key=f"creative_project:{proj.id}",
                        importance=0.4,
                        metadata={
                            "project_id": proj.id,
                            "status": proj.status,
                            "memory_grounded": bool(diaries or dream or project_memory),
                            "memory_event_ids": list(
                                project_recall.get("memory_event_ids") or []
                            )[:10],
                        },
                        pause_on_error=False,
                    )
                    if not archived:
                        await self._fail_open_activity(
                            action_key=project_action_key,
                            action_type="create_creative_project",
                            scene="creative",
                            title=f"新创作项目 {_today()}",
                            metadata={
                                "date": _today(),
                                "project_slot": len(projects),
                            },
                        )
                        raise RuntimeError(
                            "companion creative project memory archive failed"
                        )
                    await self._finish_activity_memory(
                        action_key=project_action_key,
                        action_type="create_creative_project",
                        result_text="新的创作项目已经建立并归档。",
                        scene="creative",
                        title=f"新创作项目 {_today()}",
                        metadata={"date": _today(), "project_slot": len(projects)},
                        salient_line=f"开了新创作《{proj.title}》",
                        ongoing_thread=f"小说：《{proj.title}》写作中",
                        pause_on_error=False,
                    )
                    projects = [proj] + projects
                    self.store.save_projects(projects[:20])
                    drafting = [p for p in projects if p.status == "drafting"]
                    self.store.patch_runtime(last_creative_project_at=_now_iso())
                else:
                    # begin_activity already opened via _recall_life_evidence.
                    await self._finish_activity_memory(
                        action_key=project_action_key,
                        action_type="create_creative_project",
                        result_text="构思新创作未产出可用标题，稍后再试。",
                        scene="creative",
                        title=f"新创作项目 {_today()}",
                        metadata={
                            "date": _today(),
                            "project_slot": len(projects),
                            "reason_code": "empty_or_invalid_project",
                        },
                        state="failed",
                        pause_on_error=False,
                    )

        # advance one due project
        due = [p for p in drafting if force or (p.next_advance_at or 0) <= now]
        if not due:
            return None
        proj = due[0]
        prev = proj.draft_chunks[-1].text if proj.draft_chunks else ""
        budget = self._cfg.creative.chars_per_session
        creative_chunk_index = len(proj.draft_chunks)
        # 续写前 V6 混合召回：可联想近期经历/兴趣 + 上一章正文针，避免创作路径「只写不读」
        prev_needles = " ".join(str(prev or "").replace("\n", " ").split())[:120]
        creative_recall = await self._recall_life_evidence(
            query=(
                f"{proj.title} {proj.premise or ''} 创作 灵感 最近 视频 番剧 日记 "
                f"{prev_needles} "
                f"{(self.ensure_life_state().activity if self.enabled else '') or ''}"
            ),
            scene="creative",
            limit=4,
            action_key=f"companion_creative_chunk:{proj.id}:{creative_chunk_index}",
            action_type="write_creative_chunk",
            current_activity=(
                "正在续写当前创作项目，会记住此前写到哪里、最近经历了什么以及这一段接下来要写什么。"
            ),
            title=proj.title,
            metadata={
                "project_id": proj.id,
                "chunk_index": creative_chunk_index,
            },
        )
        creative_mem = str(creative_recall.get("memory_evidence") or "").strip()
        persona_for_chunk = bits["base_prompt"]
        if creative_mem:
            persona_for_chunk = (
                f"{persona_for_chunk}\n\n【可轻量呼应的近期经历/兴趣】\n"
                f"{creative_mem[:700]}"
            )
        system, user = P.build_creative_chunk_prompt(
            title=proj.title,
            work_type=proj.work_type,
            premise=proj.premise,
            outline=proj.outline,
            previous=prev,
            next_hint=proj.next_hint,
            budget=budget,
            persona_prompt=persona_for_chunk,
        )
        text = await self._llm_text(system, user, max_tokens=max(300, budget + 100), scene="creative")
        if not text or len(text.strip()) < 40:
            # Close the begin_activity intent opened by _recall_life_evidence;
            # otherwise orphan intents pollute recent-self / open-recent lanes.
            await self._finish_activity_memory(
                action_key=f"companion_creative_chunk:{proj.id}:{creative_chunk_index}",
                action_type="write_creative_chunk",
                result_text="续写未产出可用文本，稍后再试。",
                scene="creative",
                title=proj.title,
                metadata={
                    "project_id": proj.id,
                    "chunk_index": creative_chunk_index,
                    "reason_code": "empty_or_short_chunk",
                },
                state="failed",
                pause_on_error=False,
            )
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
        # Archive full prose (capped) so later creative recall / self-QA can
        # recover needles like 青铜钥匙 — not only a status line.
        prose = str(chunk.text or "").strip()
        archive_body = (
            f"《{proj.title}》续写第{creative_chunk_index + 1}段（{chunk.chars}字）：\n"
            f"{prose[:2400]}"
        )
        archived = await self._archive_text(
            source_type="creative",
            event_type="creative_chunk",
            text=archive_body,
            title=proj.title or f"创作片段 {creative_chunk_index + 1}",
            idempotency_key=f"creative:{proj.id}:{chunk.at}",
            importance=0.55,
            metadata={
                "project_id": proj.id,
                "status": proj.status,
                "chunk_index": creative_chunk_index,
                "chars": chunk.chars,
                "memory_grounded": bool(creative_mem),
                "memory_event_ids": list(creative_recall.get("memory_event_ids") or [])[:8],
            },
            pause_on_error=False,
        )
        if not archived:
            await self._fail_open_activity(
                action_key=f"companion_creative_chunk:{proj.id}:{creative_chunk_index}",
                action_type="write_creative_chunk",
                scene="creative",
                title=proj.title,
                metadata={"project_id": proj.id, "chunk_index": creative_chunk_index},
            )
            raise RuntimeError("companion creative chunk memory archive failed")
        await self._finish_activity_memory(
            action_key=f"companion_creative_chunk:{proj.id}:{creative_chunk_index}",
            action_type="write_creative_chunk",
            result_text=archive_body[:500],
            scene="creative",
            title=proj.title,
            metadata={
                "project_id": proj.id,
                "chunk_index": creative_chunk_index,
            },
            salient_line=(
                f"写完了《{proj.title}》"
                if proj.status == "finished"
                else f"续写了《{proj.title}》约{chunk.chars}字"
            ),
            # Finished novels go to salient only — never stay as open threads.
            ongoing_thread=(
                ""
                if proj.status == "finished"
                else f"小说：《{proj.title}》写作中"
            ),
            # Finished: close this title's open thread only (no re-insert).
            # In-progress: category prefix on ongoing_thread replaces prior 小说：.
            close_thread_prefix=(
                f"小说：《{proj.title}》" if proj.status == "finished" else ""
            ),
            pause_on_error=False,
        )
        # replace in list only after the account brain confirms the chunk.
        projects = [proj if p.id == proj.id else p for p in projects]
        self.store.save_projects(projects[:20])
        self.apply_activity_energy_delta(-2, "creative")
        return proj

    # ── main tick ──

    def _past_time(self, hhmm: str, now: Optional[datetime] = None) -> bool:
        now = now or now_cn()
        m = _parse_hhmm(hhmm)
        if m is None:
            return False
        return now.hour * 60 + now.minute >= m

    async def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Called by scheduler roughly once per minute. Lightweight orchestration."""
        now = now or now_cn()
        result: Dict[str, Any] = {"enabled": self.enabled, "actions": []}
        if not self.enabled:
            return result

        # Prefer asyncio.Lock across await boundaries (create lazily on running loop)
        lock = self._tick_lock
        if lock is None:
            try:
                self._tick_lock = asyncio.Lock()
                lock = self._tick_lock
            except RuntimeError:
                # no running loop — fall back to busy flag
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
            self._refresh_life_rhythm(now)

            # MotiveQueue: rank desires before companion side-effects so
            # downstream scheduler can consult top motive (rest / browse / express).
            try:
                queue = self.rank_motives(now=now)
                top = queue.top()
                result["motive_queue"] = queue.to_dict()
                if top is not None:
                    result["top_motive"] = top.name
                    result["top_action"] = top.suggested_action
                    result["actions"].append(
                        f"motive:{top.suggested_action}:{top.score:.1f}"
                    )
                    if top.suggested_action == "rest" and float(top.score or 0) >= 6.0:
                        # Make the motive observable in SelfState. This also lets
                        # the next rhythm tick recover energy because the bot is
                        # genuinely resting instead of merely skipping cron work.
                        sleep_hours = now.hour >= 23 or now.hour < 7

                        def _mutate_rest(state: LifeState) -> None:
                            desired = (
                                "夜深了，正在睡觉"
                                if sleep_hours
                                else "暂时没什么想看的，正在休息"
                            )
                            if state.activity != desired:
                                state.activity = desired
                                state.message_seed = (
                                    "睡醒再继续"
                                    if sleep_hours
                                    else "歇一会儿，之后换个方向看看"
                                )
                                state.updated_at = _now_iso()

                        updater = getattr(self.store, "update_life_state", None)
                        if callable(updater):
                            updater(_mutate_rest)
                        self.store.patch_runtime(
                            last_rest_motive_at=_now_iso(),
                            last_rest_motive_reason=str(top.reason or "")[:120],
                        )
                        result["actions"].append("rest")
            except Exception as exc:
                logger.debug(
                    "[%s] rank_motives failed: %s", self.account_id, type(exc).__name__
                )

            # Companion-memory failures are isolated from platform comments,
            # private messages and video actions. Back off this subsystem instead
            # of retrying the same archive every minute or pausing the account.
            runtime = self.store.get_runtime() or {}
            try:
                memory_cooldown_until = float(
                    runtime.get("companion_memory_cooldown_until") or 0
                )
            except (TypeError, ValueError):
                memory_cooldown_until = 0.0
            if memory_cooldown_until > now.timestamp():
                result["skipped"] = "memory_cooldown"
                result["memory_cooldown_until"] = memory_cooldown_until
                result["actions"].append("memory_cooldown")
                return result

            # plan generation after generate_time
            if self._cfg.schedule.enabled:
                plan = self.store.get_daily_plan()
                past_generate = self._past_time(self._cfg.schedule.generate_time, now)
                if plan.date != _today() or not plan.items:
                    # Bridge midnight→configured generation time immediately so
                    # prompts never keep yesterday's schedule for seven hours.
                    plan = await self.ensure_daily_plan(
                        force=True, fallback_only=not past_generate
                    )
                    result["actions"].append(f"plan:{plan.source}")
                elif past_generate and plan.source in {
                    "fallback_bridge",
                    "fallback",
                    "fallback_merged",
                }:
                    runtime = self.store.get_runtime() or {}
                    try:
                        retry_at = float(runtime.get("next_plan_retry_at") or 0)
                    except (TypeError, ValueError):
                        retry_at = 0.0
                    retry_due = plan.source == "fallback_bridge" or retry_at <= now.timestamp()
                    if retry_due:
                        plan = await self.ensure_daily_plan(force=True)
                        result["actions"].append(f"plan:{plan.source}")
                else:
                    self._sync_state_from_plan(plan)

                if past_generate or plan.source == "fallback_bridge":
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
                    try:
                        note = await self.maybe_explore()
                        if note:
                            result["actions"].append(f"explore:{note.query[:20]}")
                    except Exception as exc:
                        # Exploration has no irreversible platform side effect.
                        # Isolate it so a search/memory failure cannot abort the
                        # whole companion tick or pause comments/video/dynamics.
                        error = f"{type(exc).__name__}:{str(exc)[:120]}"
                        try:
                            self.store.patch_runtime(last_explore_error=error)
                        except Exception:
                            pass
                        result["actions"].append(
                            f"explore_error:{type(exc).__name__}"
                        )
                        logger.warning(
                            "[%s] companion exploration isolated: %s",
                            self.account_id,
                            error,
                        )

            if self._cfg.creative.enabled and self._creative_idle_ok():
                if random.random() < 0.25:
                    proj = await self.maybe_advance_creative()
                    if proj:
                        result["actions"].append(f"creative:{proj.title[:20]}")

            # Mind-wander (C14): idle associative replay — accessibility only,
            # no LLM speech. Rate-limited via runtime marker.
            if self.memory_brain and self._looks_idle() and random.random() < 0.40:
                try:
                    rt = self.store.get_runtime() or {}
                    last_mw = float(rt.get("mind_wander_at") or 0)
                    if time.time() - last_mw >= 20 * 60:
                        needles = (self._self_state_recall_needles() or "").split()
                        wander = getattr(self.memory_brain, "mind_wander", None)
                        report = None
                        if callable(wander):
                            report = wander(limit=3, seed_needles=needles[:8])
                        if isinstance(report, dict) and int(report.get("reinforced") or 0) > 0:
                            titles = list(report.get("titles") or [])[:2]
                            if titles:
                                # A passing association is not a lived event. Keep
                                # it as a low-priority seed instead of pushing it
                                # into salient_recent / ongoing_threads.
                                try:
                                    def _mutate_mw(state: LifeState) -> None:
                                        state.message_seed = f"走神：{titles[0][:40]}"
                                        state.updated_at = _now_iso()

                                    updater = getattr(self.store, "update_life_state", None)
                                    if callable(updater):
                                        updater(_mutate_mw)
                                    else:
                                        st = self.store.get_life_state()
                                        _mutate_mw(st)
                                        self.store.save_life_state(st)
                                except Exception:
                                    logger.debug(
                                        "[%s] mind_wander life seed skip",
                                        self.account_id,
                                        exc_info=True,
                                    )
                            rt["mind_wander_at"] = time.time()
                            saver = getattr(self.store, "save_runtime", None)
                            if callable(saver):
                                saver(rt)
                            result["actions"].append(
                                f"mind_wander:{int(report.get('reinforced') or 0)}"
                            )
                except Exception:
                    logger.debug(
                        "[%s] mind_wander skipped", self.account_id, exc_info=True
                    )

            return result
        except Exception as e:
            logger.error("[%s] companion tick failed: %s", self.account_id, e, exc_info=True)
            result["error"] = type(e).__name__
            return result
        finally:
            try:
                await self._close_stale_open_activities()
            except Exception as exc:
                logger.debug(
                    "[%s] stale companion activity sweep failed: %s",
                    self.account_id,
                    type(exc).__name__,
                )
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass
            else:
                self._tick_busy = False
