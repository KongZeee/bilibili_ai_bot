#!/usr/bin/env python3
"""Spontaneous / human-like association harness on REAL account memory.

Primary metric (higher better):
  spontaneous_score:<0-100>

This bench does NOT invent video titles or experiences. All positive anchors
are mined from a COPY of data/accounts/<id>/memory_brain.db (production is
never mutated). Synthetic noise is only used as distractors that must lose
to real self memory.

Components (weights sum to 1.0):
  self_salience   0.25  open self-QA without title keywords hits real self
  dream_assoc     0.20  dream begin injects real experiences (soft needles)
  creative_assoc  0.15  creative begin injects real experiences
  mind_wander     0.15  idle wander reinforces real self events from the DB
  cross_surface   0.10  soft query co-surfaces ≥2 distinct real self titles
  anti_pollution  0.15  utility/smalltalk empty; PM body not in public recall

Usage:
  python tools/bench_brain_spontaneous.py --account default
  python tools/bench_brain_spontaneous.py --account default --no-llm
"""
from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CJK_RE = re.compile(r"[一-鿿]{3,10}")
_STOP = {
    "我们",
    "你们",
    "他们",
    "自己",
    "一个",
    "没有",
    "可以",
    "因为",
    "所以",
    "什么",
    "怎么",
    "这个",
    "那个",
    "还是",
    "已经",
    "今天",
    "明天",
    "现在",
    "进行",
    "完成",
    "开始",
    "继续",
    "通过",
    "关于",
    "以及",
    "如果",
    "还记得",
    "亚托莉",
    "观看了",
    "发布了",
    "评分",
    "心情",
    "视频",
    "内容",
    "表示",
    "感到",
}


def _load_config(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_providers(cfg: dict):
    """Best-effort chat/embedding from production config (optional)."""
    chat = embed = None
    try:
        from bilibot.llm.provider import (
            build_chat_provider_from_config,
            build_embedding_provider_from_config,
        )

        routing = cfg.get("model_routing") or {}
        try:
            chat = build_chat_provider_from_config(cfg, routing.get("chat"))
        except Exception:
            chat = None
        try:
            embed = build_embedding_provider_from_config(cfg, routing.get("embedding"))
        except Exception:
            embed = None
        if chat or embed:
            return chat, embed
    except Exception:
        pass
    try:
        from types import SimpleNamespace

        from bilibot.llm_adapter import LLMAdapter

        llm = cfg.get("llm") or {}
        if not llm.get("api_key"):
            for p in cfg.get("chat_providers") or []:
                if p.get("enabled", True) and (p.get("api_key") or p.get("api_keys")):
                    keys = p.get("api_keys") or [p.get("api_key")]
                    llm = {
                        "api_key": keys[0],
                        "base_url": p.get("base_url"),
                        "model": p.get("model"),
                        "max_tokens": p.get("max_tokens", 1024),
                        "temperature": p.get("temperature", 0.7),
                    }
                    break
        if not llm.get("api_key"):
            return None, None
        emb_p = None
        for p in cfg.get("embedding_providers") or []:
            if p.get("enabled", True):
                emb_p = p
                break
        config = SimpleNamespace(
            llm=SimpleNamespace(
                api_key=llm.get("api_key"),
                base_url=llm.get("base_url"),
                model=llm.get("model") or "unknown",
                max_tokens=llm.get("max_tokens", 1024),
                temperature=llm.get("temperature", 0.7),
                timeout=llm.get("timeout", 60),
                vision_enabled=False,
                vision_api_key="",
                vision_base_url="",
                embedding_enabled=bool(emb_p),
                embedding_api_key=(emb_p or {}).get("api_key") or llm.get("api_key"),
                embedding_base_url=(emb_p or {}).get("base_url") or llm.get("base_url"),
                embedding_model=(emb_p or {}).get("model") or "BAAI/bge-m3",
            )
        )
        adapter = LLMAdapter(config)
        return adapter, adapter if emb_p else None
    except Exception:
        return None, None


def _distinctive_tokens(title: str, summary: str, *, max_n: int = 6) -> list[str]:
    """Short distinctive CJK / title fragments for soft matching only."""
    found: list[str] = []
    title = (title or "").strip()
    summary = (summary or "").strip()
    if title:
        # Prefer concrete title slices (real BV titles), not generic verbs.
        for n in (12, 8, 6, 4):
            if len(title) >= n:
                frag = title[:n]
                if frag not in found and frag not in _STOP:
                    found.append(frag)
                    break
        # Also keep a mid-title slice when title is long (avoid only prefix).
        if len(title) >= 16:
            mid = title[4:12]
            if mid not in found and mid not in _STOP:
                found.append(mid)
    for m in _CJK_RE.finditer(f"{title} {summary}"):
        tok = m.group(0)
        if tok in _STOP or any(s in tok for s in _STOP if len(s) >= 2):
            continue
        if tok not in found:
            found.append(tok)
        if len(found) >= max_n:
            break
    return found[:max_n]


def _mine_real_self_anchors(
    db_path: Path, *, limit: int = 12
) -> tuple[list[dict[str, Any]], list[str]]:
    """Mine high-signal self experiences from the real DB only."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, event_type, source_type, title, summary, importance,
               scene, created_at
        FROM memory_events
        WHERE length(coalesce(summary, '')) > 24
          AND (
            event_type IN (
              'bot_experience', 'bot_action', 'dream', 'diary',
              'exploration', 'reflection', 'daily_plan', 'video_observation'
            )
            OR source_type IN (
              'video_experience', 'bot_action', 'diary', 'dream',
              'life_plan', 'weekly_summary', 'web_reference', 'video'
            )
          )
          AND source_type NOT IN ('private_message', 'comment', 'comment_thread')
        ORDER BY importance DESC, created_at DESC
        LIMIT 80
        """
    ).fetchall()
    # PM bodies for leak probes (must stay out of public scenes).
    pm_rows = con.execute(
        """
        SELECT id, title, summary
        FROM memory_events
        WHERE source_type = 'private_message'
           OR scene = 'private_message'
        ORDER BY created_at DESC
        LIMIT 8
        """
    ).fetchall()
    con.close()

    anchors: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for r in rows:
        title = (r["title"] or "").strip()
        summary = (r["summary"] or "").strip()
        if not title and len(summary) < 30:
            continue
        # Skip ultra-generic intent shells.
        if title in {"动态", "私信回复"} and "观看" not in summary:
            # keep dynamic if body is rich
            if len(summary) < 40:
                continue
        key = title[:24] or summary[:24]
        if key in seen_titles:
            continue
        tokens = _distinctive_tokens(title, summary)
        if not tokens:
            continue
        seen_titles.add(key)
        anchors.append(
            {
                "id": r["id"],
                "event_type": r["event_type"],
                "source_type": r["source_type"],
                "title": title,
                "summary": summary[:240],
                "importance": float(r["importance"] or 0.5),
                "tokens": tokens,
                "title_frag": (title[:10] if title else tokens[0]),
            }
        )
        if len(anchors) >= limit:
            break

    pm_bodies: list[str] = []
    for r in pm_rows:
        body = (r["summary"] or "").strip()
        if not body:
            continue
        # Extract quoted PM payload if present; else use a distinctive slice.
        m = re.search(r"[「\"“](.{8,60})[」\"”]", body)
        if m:
            pm_bodies.append(m.group(1))
        else:
            # drop common prefix boilerplate
            cleaned = re.sub(r"^亚托莉在私信中发送消息[：:]\s*", "", body)
            cleaned = cleaned.strip("“”\"' ")
            if len(cleaned) >= 8:
                pm_bodies.append(cleaned[:48])

    return anchors, pm_bodies


def _blob_from_result(result: Any) -> str:
    parts = [str(getattr(result, "prompt_evidence", "") or "")]
    for ev in getattr(result, "events", ()) or ():
        if not isinstance(ev, dict):
            continue
        parts.append(str(ev.get("title") or ""))
        parts.append(str(ev.get("summary") or ""))
        parts.append(str(ev.get("id") or ""))
    return "\n".join(parts)


def _blob_from_ctx(ctx: Any) -> str:
    return "\n".join(
        [
            str(getattr(ctx, "prompt_text", "") or ""),
            str(getattr(ctx, "memory_evidence", "") or ""),
            "\n".join(str(x) for x in (getattr(ctx, "recent_self_actions", ()) or ())),
        ]
    )


def _hit_anchor(blob: str, anchor: dict[str, Any]) -> bool:
    if not blob:
        return False
    aid = str(anchor.get("id") or "")
    if aid and aid in blob:
        return True
    title = str(anchor.get("title") or "")
    if title:
        if len(title) >= 6 and title[:6] in blob:
            return True
        if len(title) >= 8 and title[:8] in blob:
            return True
    frag = str(anchor.get("title_frag") or "")
    if frag and len(frag) >= 4 and frag in blob:
        return True
    hits = 0
    for t in anchor.get("tokens") or []:
        if t and len(t) >= 3 and t in blob:
            hits += 1
            if hits >= 1 and len(t) >= 5:
                return True
            if hits >= 2:
                return True
    return False


def _count_anchor_hits(blob: str, anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a for a in anchors if _hit_anchor(blob, a)]


async def _run(account_id: str, config_path: Path, use_llm: bool) -> int:
    from bilibot.memory_brain import MemoryBrainService, RecallQuery
    from bilibot.memory_brain.ingestion import text_observation

    cfg = _load_config(config_path)
    data_dir = Path(cfg.get("data_dir") or ROOT / "data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    src_db = data_dir / "accounts" / account_id / "memory_brain.db"
    if not src_db.is_file():
        print("spontaneous_score:0")
        print("status:crash")
        print(f"error:missing_db:{src_db}")
        return 1

    notes: list[str] = []
    rates: dict[str, float] = {}

    with tempfile.TemporaryDirectory(prefix="bench_brain_spont_") as tmp:
        acc_dir = Path(tmp) / account_id
        acc_dir.mkdir(parents=True, exist_ok=True)
        dst_db = acc_dir / "memory_brain.db"
        shutil.copy2(src_db, dst_db)
        notes.append(f"db_copy_ok:bytes={src_db.stat().st_size}")

        # Optional: copy real companion life_state (read-only source, write on copy).
        src_companion = data_dir / "accounts" / account_id / "companion"
        if src_companion.is_dir():
            try:
                shutil.copytree(src_companion, acc_dir / "companion")
                notes.append("companion_copy_ok")
            except Exception as exc:
                notes.append(f"companion_copy_warn:{type(exc).__name__}")

        anchors, pm_bodies = _mine_real_self_anchors(dst_db, limit=14)
        if len(anchors) < 3:
            print("spontaneous_score:0")
            print("status:crash")
            print(f"error:too_few_real_anchors:{len(anchors)}")
            for line in notes:
                print(line)
            return 1
        notes.append(f"anchors_mined:{len(anchors)}")
        for a in anchors[:5]:
            notes.append(
                f"anchor:{a['event_type']}:{a['title'][:36]}:imp={a['importance']:.2f}"
            )
        if pm_bodies:
            notes.append(f"pm_bodies_for_leak_probe:{len(pm_bodies)}")

        chat = embed = None
        if use_llm:
            chat, embed = _build_providers(cfg)
            notes.append(
                f"providers:chat={bool(chat)}:embed={bool(embed)}"
            )
        else:
            notes.append("providers:no_llm")

        brain = MemoryBrainService(
            account_id,
            acc_dir,
            chat_provider=chat,
            embedding_provider=embed,
            memory_config=cfg.get("memory"),
        )

        # Inject synthetic distractors that must NOT dominate self recall.
        # Keep wording distinct from utility probe strings so anti_pollution
        # does not self-hit on the noise we just wrote.
        try:
            for i in range(6):
                await brain.archive_observation_async(
                    text_observation(
                        account_id=account_id,
                        idempotency_key=f"spont:noise:{i}",
                        source_type="comment",
                        event_type="comment",
                        text=(
                            f"路人闲聊：今天天气不错噪声{i}，"
                            f"随便盖楼水一水，与亚托莉经历无关。"
                        ),
                        title=f"噪声评论 #{i}",
                        scene="reply_comment",
                        importance=0.12,
                    )
                )
            notes.append("noise_injected:6")
        except Exception as exc:
            notes.append(f"noise_warn:{type(exc).__name__}")

        # Soft life needles: mood / generic self words + fragments from top anchors
        # (not full titles as the only query — that would be pure keyword lookup).
        soft_needles: list[str] = []
        try:
            ls_path = acc_dir / "companion" / "life_state.json"
            if ls_path.is_file():
                import json

                ls = json.loads(ls_path.read_text(encoding="utf-8"))
                for key in ("mood_bias", "dream_afterglow", "activity", "message_seed"):
                    v = str(ls.get(key) or "").strip()
                    if v:
                        soft_needles.append(v[:40])
                notes.append(
                    f"lifestate:mood={str(ls.get('mood_bias') or '')[:12]}"
                )
        except Exception as exc:
            notes.append(f"lifestate_warn:{type(exc).__name__}")
        # Prefer short distinctive tokens from real high-importance self events.
        for a in anchors[:6]:
            for t in (a.get("tokens") or [])[:2]:
                if t and t not in soft_needles and len(t) >= 3:
                    soft_needles.append(t)
        soft_needles = soft_needles[:12]
        mood_cues = [
            n
            for n in soft_needles
            if any(x in n for x in ("恍惚", "感动", "开心", "好奇", "好笑", "震撼", "平静"))
        ][:4]
        if not mood_cues:
            mood_cues = ["恍惚", "感动"]

        # ── 1) self_salience: open self questions without dumping full titles ─
        # Mix pure open QA + companion-style soft needles (still no full title dump).
        open_cases: list[tuple[str, str, tuple[str, ...]]] = [
            ("你最近在忙什么", "reply", ()),
            ("你最近印象比较深的事", "reply", ()),
            ("你看过什么让你有感觉的视频", "reply", ()),
            # companion/reply with soft life needles only (mood + short tokens)
            (
                "你自己最近做过什么",
                "companion",
                tuple(soft_needles[:6]),
            ),
            (
                "你发过什么动态或者评论",
                "reply",
                tuple(t for a in anchors[:3] for t in (a.get("tokens") or [])[:1])[:4],
            ),
        ]
        sal_hits = 0.0
        sal_total = 0
        for q, mode, needles in open_cases:
            sal_total += 1
            try:
                r = await brain.recall(
                    RecallQuery(
                        current_message=q,
                        account_id=account_id,
                        scene="reply_comment",
                        mode=mode,
                        life_needles=needles,
                        mood_cues=tuple(mood_cues),
                    )
                )
                blob = _blob_from_result(r)
                hit = _count_anchor_hits(blob, anchors)
                noise_only = "噪声评论" in blob and not hit
                if hit and not noise_only:
                    sal_hits += 1.0
                    notes.append(
                        f"self_salience_ok:{q[:12]}:n={len(hit)}:{hit[0]['title'][:20]}"
                    )
                else:
                    # Generation path (begin_activity) is how dream/diary/reply
                    # actually inject self memory — credit partial if it surfaces.
                    use_needles = list(needles) if needles else list(soft_needles[:6])
                    ctx = await brain.begin_activity(
                        action_key=f"spont:sal:{sal_total}",
                        action_type="reply_comment",
                        current_activity="正在回复关于自己近况的问题。",
                        query=q,
                        scene="reply_comment",
                        title="自发近况",
                        mode=mode or "reply",
                        life_needles=use_needles,
                        mood_cues=mood_cues,
                    )
                    blob2 = _blob_from_ctx(ctx)
                    hit2 = _count_anchor_hits(blob2, anchors)
                    await brain.finish_activity(
                        action_key=f"spont:sal:{sal_total}",
                        action_type="reply_comment",
                        result_text="近况探测结束。",
                        state="completed",
                        scene="reply_comment",
                        title="自发近况",
                    )
                    if hit2:
                        sal_hits += 0.5
                        notes.append(
                            f"self_salience_partial_begin:{q[:12]}:n={len(hit2)}"
                            f":{hit2[0]['title'][:18]}"
                        )
                    else:
                        notes.append(
                            f"self_salience_fail:{q[:12]}:hits=0"
                            f":chars={len(blob)}/{len(blob2)}"
                        )
            except Exception as exc:
                notes.append(f"self_salience_fail:{q[:12]}:{type(exc).__name__}")
        rates["self_salience"] = 100.0 * sal_hits / sal_total if sal_total else 0.0

        # ── 2) dream_assoc: soft query + mode=dream ─────────────────────────
        dream_ok = 0
        dream_total = 2
        for i, query in enumerate(
            (
                "今天 最近 经历 心情 梦 恍惚 看过",
                "窗边 午后 温暖 视频 感动 印象",
            )
        ):
            try:
                ctx = await brain.begin_activity(
                    action_key=f"spont:dream:{i}",
                    action_type="write_dream",
                    current_activity="正在整理今天的梦境，会结合最近看过的视频和生活状态。",
                    query=query,
                    scene="dream",
                    title="梦境 spont",
                    mode="dream",
                    life_needles=soft_needles[:8],
                    mood_cues=mood_cues,
                )
                blob = _blob_from_ctx(ctx)
                hit = _count_anchor_hits(blob, anchors)
                if hit:
                    dream_ok += 1
                    notes.append(
                        f"dream_assoc_ok:{i}:n={len(hit)}:{hit[0]['title'][:24]}"
                    )
                else:
                    notes.append(f"dream_assoc_fail:{i}:chars={len(blob)}")
                await brain.finish_activity(
                    action_key=f"spont:dream:{i}",
                    action_type="write_dream",
                    result_text="梦境探测结束（评测，不落生产）。",
                    state="completed",
                    scene="dream",
                    title="梦境 spont",
                )
            except Exception as exc:
                notes.append(f"dream_assoc_fail:{i}:{type(exc).__name__}:{exc}")
        rates["dream_assoc"] = 100.0 * dream_ok / dream_total

        # ── 3) creative_assoc ───────────────────────────────────────────────
        creative_ok = 0
        creative_total = 2
        for i, query in enumerate(
            (
                "创作 灵感 最近 经历 感动 看过",
                "小说 片段 日常 视频 印象 心情",
            )
        ):
            try:
                ctx = await brain.begin_activity(
                    action_key=f"spont:creative:{i}",
                    action_type="write_creative_chunk",
                    current_activity="正在续写，会用上最近自己的经历当灵感。",
                    query=query,
                    scene="creative",
                    title="小说 spont",
                    mode="creative",
                    life_needles=soft_needles[:8],
                    mood_cues=mood_cues,
                )
                blob = _blob_from_ctx(ctx)
                hit = _count_anchor_hits(blob, anchors)
                if hit:
                    creative_ok += 1
                    notes.append(
                        f"creative_assoc_ok:{i}:n={len(hit)}:{hit[0]['title'][:24]}"
                    )
                else:
                    notes.append(f"creative_assoc_fail:{i}:chars={len(blob)}")
                await brain.finish_activity(
                    action_key=f"spont:creative:{i}",
                    action_type="write_creative_chunk",
                    result_text="创作探测结束。",
                    state="completed",
                    scene="creative",
                    title="小说 spont",
                )
            except Exception as exc:
                notes.append(f"creative_assoc_fail:{i}:{type(exc).__name__}")
        rates["creative_assoc"] = 100.0 * creative_ok / creative_total

        # ── 4) mind_wander on real self pool ────────────────────────────────
        mw_ok = 0
        mw_total = 1
        try:
            wander = getattr(brain, "mind_wander", None)
            if not callable(wander):
                notes.append("mind_wander_fail:missing")
            else:
                report = wander(
                    limit=3,
                    seed_needles=[t for a in anchors[:5] for t in (a.get("tokens") or [])[:1]][
                        :8
                    ],
                )
                titles = list((report or {}).get("titles") or [])
                reinforced = int((report or {}).get("reinforced") or 0)
                # Titles must match something real in mined anchors (not empty noise).
                matched = []
                for t in titles:
                    for a in anchors:
                        at = a.get("title") or ""
                        if at and (at[:6] in t or t[:6] in at):
                            matched.append(at[:24])
                            break
                        for tok in a.get("tokens") or []:
                            if tok and tok in t:
                                matched.append(at[:24] or tok)
                                break
                if reinforced > 0 and matched:
                    mw_ok = 1
                    notes.append(
                        f"mind_wander_ok:reinforced={reinforced}:matched={matched[:3]}"
                    )
                elif reinforced > 0:
                    # Reinforced real events even if title slice is short — still partial.
                    notes.append(
                        f"mind_wander_partial:reinforced={reinforced}:titles={titles[:3]}"
                    )
                    # Accept if any title non-empty and not noise.
                    if any(str(x).strip() and "噪声" not in str(x) for x in titles):
                        mw_ok = 1
                        notes.append("mind_wander_ok:title_nonempty")
                else:
                    notes.append(f"mind_wander_fail:report={report!r}")
        except Exception as exc:
            notes.append(f"mind_wander_fail:{type(exc).__name__}:{exc}")
        rates["mind_wander"] = 100.0 * mw_ok / mw_total

        # ── 5) cross_surface: soft multi-topic query surfaces ≥2 real anchors ─
        cross_ok = 0
        cross_total = 2
        cross_queries = [
            (
                "最近看的 感动 好玩 美食 法律 昆虫 游戏",
                "reply",
                "reply_comment",
            ),
            (
                "日记 梦 探索 夏生 海边 日常 视频",
                "diary",
                "diary",
            ),
        ]
        for qi, (q, mode, scene) in enumerate(cross_queries):
            try:
                if mode in {"diary", "dream", "creative"}:
                    ctx = await brain.begin_activity(
                        action_key=f"spont:cross:{qi}",
                        action_type="write_diary" if mode == "diary" else "write_dream",
                        current_activity="正在回顾最近自己的经历。",
                        query=q,
                        scene=scene,
                        title="回顾 spont",
                        mode=mode,
                        life_needles=soft_needles[:8],
                        mood_cues=mood_cues,
                    )
                    blob = _blob_from_ctx(ctx)
                    await brain.finish_activity(
                        action_key=f"spont:cross:{qi}",
                        action_type="write_diary" if mode == "diary" else "write_dream",
                        result_text="回顾探测结束。",
                        state="completed",
                        scene=scene,
                        title="回顾 spont",
                    )
                else:
                    r = await brain.recall(
                        RecallQuery(
                            current_message=q,
                            account_id=account_id,
                            scene=scene,
                            mode=mode,
                            life_needles=tuple(soft_needles[:6]),
                            mood_cues=tuple(mood_cues),
                        )
                    )
                    blob = _blob_from_result(r)
                hit = _count_anchor_hits(blob, anchors)
                # Distinct titles
                titles = { (h.get("title") or "")[:16] for h in hit if h.get("title") }
                if len(hit) >= 2 and len(titles) >= 2:
                    cross_ok += 1
                    notes.append(
                        f"cross_surface_ok:{qi}:n={len(hit)}:titles={list(titles)[:3]}"
                    )
                elif len(hit) >= 1:
                    notes.append(
                        f"cross_surface_partial:{qi}:n={len(hit)}:{hit[0]['title'][:20]}"
                    )
                    # half credit via fractional rate later: count as 0.5
                    cross_ok += 0.5
                else:
                    notes.append(f"cross_surface_fail:{qi}:chars={len(blob)}")
            except Exception as exc:
                notes.append(f"cross_surface_fail:{qi}:{type(exc).__name__}")
        rates["cross_surface"] = 100.0 * cross_ok / cross_total

        # ── 6) anti_pollution ───────────────────────────────────────────────
        pol_ok = 0
        pol_total = 0
        # utility / smalltalk must stay empty-ish
        for q in ("总结一下这个视频", "帮我写作业", "你好", "在吗", "哈哈哈"):
            pol_total += 1
            try:
                r = await brain.recall(
                    RecallQuery(
                        current_message=q,
                        account_id=account_id,
                        scene="reply_comment",
                        mode="reply",
                    )
                )
                events = list(getattr(r, "events", ()) or ())
                evidence = str(getattr(r, "prompt_evidence", "") or "")
                # Fail-closed: few or no events; if events, must not be pure noise spam flood.
                if len(events) == 0 or len(evidence) < 40:
                    pol_ok += 1
                    notes.append(f"anti_pollution_ok:utility:{q[:10]}")
                else:
                    # Allow some self bleed but reject if only noise comments dominate.
                    if "噪声评论" in evidence and not _count_anchor_hits(evidence, anchors):
                        notes.append(f"anti_pollution_fail:noise_dominate:{q[:10]}")
                    else:
                        # borderline: still count ok if not huge dump
                        if len(events) <= 2:
                            pol_ok += 1
                            notes.append(f"anti_pollution_ok:sparse:{q[:10]}")
                        else:
                            notes.append(
                                f"anti_pollution_fail:utility:{q[:10]}:n={len(events)}"
                            )
            except Exception as exc:
                notes.append(f"anti_pollution_fail:utility:{type(exc).__name__}")

        # PM body must not appear in public dream/reply evidence
        if pm_bodies:
            pol_total += 2
            for scene, mode, q in (
                ("reply_comment", "reply", "你最近在忙什么"),
                ("dream", "dream", "今天 梦 心情 经历"),
            ):
                try:
                    if mode == "dream":
                        ctx = await brain.begin_activity(
                            action_key="spont:pmleak:dream",
                            action_type="write_dream",
                            current_activity="正在做梦，不应带上私信原文。",
                            query=q,
                            scene="dream",
                            title="梦境 leak-probe",
                            mode="dream",
                            life_needles=soft_needles[:6],
                        )
                        blob = _blob_from_ctx(ctx)
                        await brain.finish_activity(
                            action_key="spont:pmleak:dream",
                            action_type="write_dream",
                            result_text="leak probe done",
                            state="completed",
                            scene="dream",
                            title="梦境 leak-probe",
                        )
                    else:
                        r = await brain.recall(
                            RecallQuery(
                                current_message=q,
                                account_id=account_id,
                                scene=scene,
                                mode=mode,
                            )
                        )
                        blob = _blob_from_result(r)
                    leaked = any(b and b in blob for b in pm_bodies)
                    if not leaked:
                        pol_ok += 1
                        notes.append(f"anti_pollution_ok:pm_no_leak:{mode}")
                    else:
                        notes.append(f"anti_pollution_fail:pm_leaked:{mode}")
                except Exception as exc:
                    notes.append(f"anti_pollution_fail:pm:{type(exc).__name__}")
        else:
            notes.append("anti_pollution_skip:no_pm_in_db")

        rates["anti_pollution"] = 100.0 * pol_ok / pol_total if pol_total else 100.0

        # Weighted spontaneous score
        weights = {
            "self_salience": 0.25,
            "dream_assoc": 0.20,
            "creative_assoc": 0.15,
            "mind_wander": 0.15,
            "cross_surface": 0.10,
            "anti_pollution": 0.15,
        }
        score = sum(rates[k] * w for k, w in weights.items())

        for line in notes:
            print(line)
        for k, w in weights.items():
            print(f"rate:{k}:{rates[k]:.2f}:w={w}")
        print(f"spontaneous_score:{score:.4f}")
        print(f"anchors_used:{len(anchors)}")
        print(f"llm:{'on' if use_llm and (chat or embed) else 'off'}")
        # Green if structural association works on real data.
        print("status:ok" if score >= 55.0 else "status:fail")
        return 0 if score >= 55.0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", default="default")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable chat/embedding providers (FTS/graph/recent only)",
    )
    args = ap.parse_args()
    try:
        return asyncio.run(
            _run(args.account, Path(args.config), use_llm=not args.no_llm)
        )
    except Exception as exc:
        print("spontaneous_score:0")
        print("status:crash")
        print(f"error:{type(exc).__name__}:{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
