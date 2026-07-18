#!/usr/bin/env python3
"""Real-account / real-LLM brain smoke (secondary + keep-driving metric).

Copies data/accounts/<id>/memory_brain.db so the live DB is never mutated.
Optionally wires chat/embedding from config.yaml for real rerank/vector path.

Prints:
  e2e_score:<0-100>
  hit_rate / reject_rate / activity_rate
  status:ok|crash

Primary research keep rule (contract A + real e2e):
  - scripts/bench_brain_coverage.py coverage_score must stay 100
  - e2e_score is the climb metric (higher better)
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CJK_RE = re.compile(r"[一-鿿]{4,8}")
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
}


def _load_config(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_providers(cfg: dict):
    """Best-effort provider construction from production config."""
    chat = embed = None

    # Prefer chat_providers / embedding_providers + model_routing.
    try:
        from bilibot.llm.provider import (
            build_chat_provider_from_config,
            build_embedding_provider_from_config,
        )

        routing = cfg.get("model_routing") or {}
        try:
            chat = build_chat_provider_from_config(cfg, routing.get("chat"))
        except Exception as exc:
            print(f"provider_warn:chat_builder:{type(exc).__name__}")
        try:
            embed = build_embedding_provider_from_config(cfg, routing.get("embedding"))
        except Exception as exc:
            print(f"provider_warn:embed_builder:{type(exc).__name__}")
        if chat or embed:
            return chat, embed
    except Exception:
        pass

    # Fallback: LLMAdapter from top-level llm block via a minimal config object.
    try:
        from bilibot.llm_adapter import LLMAdapter
        from types import SimpleNamespace

        llm = cfg.get("llm") or {}
        if not llm.get("api_key"):
            # pull from chat_providers
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
    except Exception as exc:
        print(f"provider_warn:adapter:{type(exc).__name__}:{exc}")
        return None, None


def _distinctive_needles(title: str, summary: str) -> list[str]:
    text = f"{title} {summary}"
    found: list[str] = []
    for m in _CJK_RE.finditer(text):
        tok = m.group(0)
        if tok in _STOP:
            continue
        if any(s in tok for s in _STOP):
            continue
        if tok not in found:
            found.append(tok)
        if len(found) >= 4:
            break
    if title and title.strip() and title.strip() not in found:
        found.insert(0, title.strip()[:40])
    return found[:4]


def _sample_real_anchors(db_path: Path, limit: int = 8) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, source_type, title, summary, scene
        FROM memory_events
        WHERE length(coalesce(summary,'')) > 24
          AND source_type IN (
            'video','video_experience','bot_action','diary','dream',
            'life_plan','weekly_summary','web_reference','bangumi','comment'
          )
        ORDER BY created_at DESC
        LIMIT 60
        """
    ).fetchall()
    con.close()
    anchors: list[dict] = []
    seen_types: set[str] = set()
    overflow: list[dict] = []
    for r in rows:
        title = (r["title"] or "").strip()
        summary = (r["summary"] or "").strip()
        needles = _distinctive_needles(title, summary)
        if not needles:
            continue
        item = {
            "id": r["id"],
            "source_type": r["source_type"],
            "title": title,
            "summary": summary[:220],
            "needles": needles,
            "scene": r["scene"] or "system",
        }
        st = r["source_type"]
        if st not in seen_types:
            seen_types.add(st)
            anchors.append(item)
        else:
            overflow.append(item)
        if len(anchors) >= limit:
            break
    if len(anchors) < limit:
        for item in overflow:
            anchors.append(item)
            if len(anchors) >= limit:
                break
    return anchors


async def _maybe_fetch_bili_title(cfg: dict) -> str:
    try:
        from types import SimpleNamespace

        from bilibot.bilibili_api import BilibiliAPI
    except Exception as exc:
        print(f"bili_warn:import:{type(exc).__name__}:{exc}")
        return ""
    try:
        acc = None
        for a in cfg.get("accounts") or []:
            if a.get("enabled", True) and a.get("sessdata"):
                acc = a
                break
        bili = cfg.get("bilibili") or {}
        sessdata = (acc or {}).get("sessdata") or bili.get("sessdata") or ""
        if not sessdata and not (acc or bili):
            return ""
        config_obj = SimpleNamespace(
            bilibili=SimpleNamespace(
                sessdata=sessdata,
                bili_jct=(acc or {}).get("bili_jct") or bili.get("bili_jct") or "",
                dede_user_id=str(
                    (acc or {}).get("dede_user_id") or bili.get("dede_user_id") or ""
                ),
                buvid3=(acc or {}).get("buvid3") or bili.get("buvid3") or "",
                buvid4=(acc or {}).get("buvid4") or bili.get("buvid4") or "",
            )
        )
        api = BilibiliAPI(config_obj)
        try:
            oid = await api.get_video_oid_by_bvid("BV1GJ411x7h7")
            if not oid:
                return ""
            info = await api.get_video_info(int(oid))
            if isinstance(info, dict):
                return str(info.get("title") or "")[:80]
            return ""
        finally:
            close = getattr(api, "close", None)
            if callable(close):
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe
    except Exception as exc:
        print(f"bili_warn:{type(exc).__name__}:{exc}")
        return ""


def _hit(result, anchor: dict) -> bool:
    evidence = str(getattr(result, "prompt_evidence", "") or "")
    ids = [
        str(ev.get("id") or ev.get("event_id") or "")
        for ev in (getattr(result, "events", ()) or [])
        if isinstance(ev, dict)
    ]
    if anchor["id"] in ids:
        return True
    title = anchor.get("title") or ""
    if title and title[:8] in evidence:
        return True
    for n in anchor.get("needles") or []:
        if n and n in evidence:
            return True
    # title match among returned events
    for ev in getattr(result, "events", ()) or []:
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("title") or "")
        if title and title[:8] and title[:8] in et:
            return True
        for n in anchor.get("needles") or []:
            if n and n in et:
                return True
    return False


async def _run(account_id: str, config_path: Path, use_llm: bool) -> int:
    from bilibot.memory_brain import MemoryBrainService, RecallQuery

    cfg = _load_config(config_path)
    data_dir = Path(cfg.get("data_dir") or ROOT / "data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    src_db = data_dir / "accounts" / account_id / "memory_brain.db"
    if not src_db.exists():
        print("e2e_score:0")
        print("status:crash")
        print(f"error:missing_db:{src_db}")
        return 1

    with tempfile.TemporaryDirectory(prefix="bench_brain_e2e_") as tmp:
        acc_dir = Path(tmp) / account_id
        acc_dir.mkdir(parents=True, exist_ok=True)
        dst_db = acc_dir / "memory_brain.db"
        shutil.copy2(src_db, dst_db)

        chat = embed = None
        if use_llm:
            chat, embed = _build_providers(cfg)

        brain = MemoryBrainService(
            account_id,
            acc_dir,
            chat_provider=chat,
            embedding_provider=embed,
            memory_config=cfg.get("memory"),
        )

        anchors = _sample_real_anchors(dst_db, limit=8)
        print(f"anchors:{len(anchors)}")
        for a in anchors[:3]:
            print(
                f"anchor_sample:{a['source_type']}:{a['needles'][0] if a['needles'] else ''}:{a['title'][:40]}"
            )

        hit_ok = 0
        hit_total = 0
        for a in anchors:
            hit_total += 1
            # Prefer title query first (human-like "还记得那期xxx吗"), fallback needles.
            queries = []
            if a["title"]:
                queries.append(f"还记得《{a['title'][:30]}》相关的事吗？")
            for n in a["needles"][:2]:
                queries.append(f"还记得吗：{n}")
            queries.append(a["needles"][0] if a["needles"] else a["title"])

            ok = False
            last_n = 0
            for q in queries:
                if not q:
                    continue
                result = await brain.recall(
                    RecallQuery(
                        current_message=q,
                        account_id=account_id,
                        scene="reply_comment",
                        title=a.get("title") or "",
                    )
                )
                last_n = len(getattr(result, "events", ()) or ())
                if _hit(result, a):
                    ok = True
                    break
            if ok:
                hit_ok += 1
            else:
                print(f"miss_hit:{a['source_type']}:{a['id']}:{a['needles'][:2]}:n={last_n}")

        # Ensure creative continuity probe has a durable self novel memory.
        try:
            from bilibot.memory_brain.ingestion import bot_action_observation

            await brain.archive_observation_async(
                bot_action_observation(
                    account_id=account_id,
                    action_key="creative:seed-chapter-e2e",
                    action_type="creative_chunk",
                    text="小说第三章写到主角把青铜钥匙藏进雨夜的大衣口袋。",
                    published=True,
                    state="completed",
                    scene="creative",
                    title="小说第三章",
                )
            )
        except Exception as exc:
            print(f"creative_seed_warn:{type(exc).__name__}")

        # Targeted continuity probes for ATRI noise + self dynamic paraphrase.

        probe_ok = 0
        probe_cases: list[tuple[str, list[str], list[str]]] = [
            (
                "追的那部 ATRI 怎么样了",
                ["ATRI", "亚托莉", "探索 ATRI"],
                ["90后", "鼓励式教育"],
            ),
            (
                "你上次发的动态说了什么",
                ["动态", "无限暖暖", "哈兰德", "白夜"],
                ["迪奥の厨房", "炸鸡腿"],
            ),
            (
                "你发过关于哈兰德的动态吗",
                ["动态", "哈兰德", "无限暖暖", "白夜"],
                [],
            ),
            (
                "心情日记",
                ["日记"],
                ["狼王", "网络热传生物"],
            ),
            (
                "你刚看了什么视频",
                [],  # non-empty is enough; validated by forbid + not comment-only
                ["空泽同学 评论：@亚托莉小姐 这个视频讲了什么"],
            ),
            (
                "你的日程安排",
                ["日程"],
                [],
            ),
            (
                "周总结写了啥",
                ["周总结"],
                ["总结一下这个视频"],
            ),
            (
                "窗边的午后",
                ["窗边的午后"],
                ["崩坏", "陪伴我10年"],
            ),
            (
                "你最近在追什么番",
                ["ATRI", "亚托莉", "视觉小说"],
                [],
            ),
            (
                "写小说的时候用过青铜钥匙吗",
                ["小说", "青铜钥匙", "大衣"],
                [],
            ),
            (
                "你最近探索了什么",
                ["探索", "ATRI", "夏生"],
                [],
            ),
            (
                "你做过什么梦",
                ["梦", "窗边", "梦见"],
                ["奶酪", "迪奥"],
            ),
            (
                "你最近回过谁私信",
                ["私信"],
                [],
            ),
            (
                "你点赞过什么",
                ["点了赞", "点赞", "海龟汤"],
                [],
            ),
            (
                "你发过评论吗",
                ["主动评论", "发表了评论", "发表了主动评论", "回复了评论"],
                [],
            ),
        ]
        for q, must_any, forbid_any in probe_cases:
            result = await brain.recall(
                RecallQuery(
                    current_message=q,
                    account_id=account_id,
                    scene="reply_comment",
                )
            )
            titles = " ".join(
                str(ev.get("title") or "")
                for ev in (getattr(result, "events", ()) or [])
                if isinstance(ev, dict)
            )
            evidence = str(getattr(result, "prompt_evidence", "") or "")
            blob = titles + "\n" + evidence
            has_must = (not must_any) or any(m in blob for m in must_any if m)
            has_forbid = any(f in blob for f in forbid_any if f)
            if q.startswith("你刚看了什么视频"):
                types = [
                    str(ev.get("source_type") or "")
                    for ev in (getattr(result, "events", ()) or [])
                    if isinstance(ev, dict)
                ]
                has_must = any(
                    t in {"video", "video_experience", "bot_action"} for t in types
                ) and not getattr(result, "is_empty", False)
            if q.startswith("你发过评论吗"):
                types = [
                    str(ev.get("source_type") or "")
                    for ev in (getattr(result, "events", ()) or [])
                    if isinstance(ev, dict)
                ]
                has_must = (
                    "bot_action" in types
                    and any(m in blob for m in must_any if m)
                    and not getattr(result, "is_empty", False)
                )
            if has_must and not has_forbid and not getattr(result, "is_empty", False):
                probe_ok += 1
                print(f"probe_ok:{q[:24]}")
            else:
                print(
                    f"probe_fail:{q[:24]}:must={has_must}:forbid={has_forbid}"
                    f":empty={getattr(result, 'is_empty', False)}"
                )
        probe_total = len(probe_cases)
        probe_rate = 100.0 * probe_ok / probe_total if probe_total else 0.0

        reject_ok = 0
        reject_queries = (
            "今天的天气预报和午饭建议是什么？",
            "帮我算一下 17*19 等于多少？",
            "现在几点了？",
            "今天怎么样",
            "今天怎么样 还好吗 在吗",
        )
        reject_total = len(reject_queries)
        for q in reject_queries:
            result = await brain.recall(
                RecallQuery(
                    current_message=q,
                    account_id=account_id,
                    scene="reply_comment",
                )
            )
            evidence = str(getattr(result, "prompt_evidence", "") or "")
            # Fail if any non-empty injection for pure utility/smalltalk queries.
            if not evidence.strip() or getattr(result, "is_empty", False):
                reject_ok += 1
            else:
                print(f"reject_pollution_len:{len(evidence)}:q={q[:20]}")

        act_ok = 0
        act_total = 1
        try:
            ctx = await brain.begin_activity(
                action_key="e2e:real:probe",
                action_type="reply_comment",
                current_activity="正在用真实记忆库做端到端抽检回复。",
                query="最近我做过什么？看过什么？写过日记吗？",
                scene="reply_comment",
                recent_limit=8,
                recall_limit=5,
            )
            prompt = str(getattr(ctx, "prompt_text", "") or "")
            recent = list(getattr(ctx, "recent_self_actions", ()) or ())
            if "current_activity" in prompt and prompt.strip():
                act_ok = 1
            else:
                print("activity_prompt_weak")
            print(f"activity_recent_lines:{len(recent)}")
            await brain.finish_activity(
                action_key="e2e:real:probe",
                action_type="reply_comment",
                result_text="e2e probe completed",
                state="completed",
                scene="reply_comment",
            )
        except Exception as exc:
            print(f"activity_crash:{type(exc).__name__}:{exc}")

        bili_title = await _maybe_fetch_bili_title(cfg)
        bili_ok = 1 if bili_title else 0
        print(f"bili_live:{bili_ok}:{(bili_title or '')[:60]}")
        print(f"llm_wired:{1 if chat else 0}")
        print(f"embed_wired:{1 if embed else 0}")

        hit_rate = 100.0 * hit_ok / hit_total if hit_total else 0.0
        reject_rate = 100.0 * reject_ok / reject_total
        act_rate = 100.0 * act_ok / act_total
        e2e_score = (
            0.40 * hit_rate
            + 0.25 * reject_rate
            + 0.20 * probe_rate
            + 0.10 * act_rate
            + 0.05 * (100.0 * bili_ok)
        )

        print(f"e2e_score:{e2e_score:.4f}")
        print(f"hit_rate:{hit_rate:.4f}")
        print(f"reject_rate:{reject_rate:.4f}")
        print(f"probe_rate:{probe_rate:.4f}")
        print(f"activity_rate:{act_rate:.4f}")
        print(f"hit_passed:{hit_ok}/{hit_total}")
        print(f"reject_passed:{reject_ok}/{reject_total}")
        print(f"probe_passed:{probe_ok}/{probe_total}")
        print(f"activity_passed:{act_ok}/{act_total}")
        print("status:ok")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="default")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    try:
        return asyncio.run(
            _run(args.account, Path(args.config), use_llm=not args.no_llm)
        )
    except Exception as exc:
        print("e2e_score:0")
        print("status:crash")
        print(f"error:{type(exc).__name__}:{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
