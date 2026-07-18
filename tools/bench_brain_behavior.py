#!/usr/bin/env python3
"""Behavior-grounding harness: prove the brain is USED while acting.

Primary climb metric (higher better):
  behavior_score:<0-100>

Weights:
  injection 0.35  — begin_activity / build_activity_context contains prior needles
  lifecycle 0.20  — begin + finish + domain archive closed cleanly
  self_state 0.15 — LifeState salient/threads surface after operational finishes
  post_qa   0.15  — later self-memory questions still hit
  reject    0.15  — utility / smalltalk stay empty

This is the P0 "true brain" judge. Frozen QA harnesses
(tools/bench_brain_coverage.py, tools/bench_brain_e2e_real.py) remain
regression gates and must stay green; they are NOT the climb metric.

Prints status:ok|crash and component rates.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _blob(ctx: Any) -> str:
    parts = [
        str(getattr(ctx, "prompt_text", "") or ""),
        str(getattr(ctx, "memory_evidence", "") or ""),
        "\n".join(str(x) for x in (getattr(ctx, "recent_self_actions", ()) or ())),
    ]
    return "\n".join(parts)


def _has_any(text: str, needles: list[str]) -> bool:
    return any(n and n in text for n in needles)


async def _run() -> int:
    from bilibot.memory_brain import MemoryBrainService, RecallQuery
    from bilibot.memory_brain.ingestion import (
        bot_action_observation,
        text_observation,
    )

    with tempfile.TemporaryDirectory(prefix="bench_brain_behavior_") as tmp:
        acc_dir = Path(tmp) / "behavior"
        brain = MemoryBrainService("behavior", acc_dir)

        injection_ok = 0
        injection_total = 0
        lifecycle_ok = 0
        lifecycle_total = 0
        self_state_ok = 0
        self_state_total = 0
        post_qa_ok = 0
        post_qa_total = 0
        reject_ok = 0
        reject_total = 0
        notes: list[str] = []
        companion = None
        try:
            from types import SimpleNamespace

            from bilibot.companion.service import CompanionLifeService
            from bilibot.companion.store import CompanionStore

            cfg = SimpleNamespace(
                enabled=True,
                life_state=SimpleNamespace(
                    enabled=True, inject_into_replies=True, energy_default=70
                ),
                dream=SimpleNamespace(enabled=False),
                diary=SimpleNamespace(enabled=False, max_entries=30),
                exploration=SimpleNamespace(enabled=False),
                creative=SimpleNamespace(
                    enabled=False,
                    max_active_projects=1,
                    inspiration_probability=0,
                    chars_per_session=200,
                    offer_dynamic_draft=False,
                ),
                schedule=SimpleNamespace(enabled=False),
            )
            store = CompanionStore(acc_dir / "companion")
            companion = object.__new__(CompanionLifeService)
            companion.account_id = "behavior"
            companion.store = store
            companion.memory_brain = brain
            companion._cfg = cfg
            notes.append("companion_surface:ready")
        except Exception as exc:
            notes.append(f"companion_surface_warn:{type(exc).__name__}")
            companion = None

        # ── Scenario A: watch + like → dream begin must see it ──────────
        lifecycle_total += 1
        try:
            await brain.begin_activity(
                action_key="video:hg:eval",
                action_type="evaluate_proactive_video",
                current_activity="正在观看并评价《海龟汤（2）》。",
                query="海龟汤",
                scene="proactive_video",
                title="海龟汤（2）",
            )
            await brain.finish_activity(
                action_key="video:hg:eval",
                action_type="evaluate_proactive_video",
                result_text="看完《海龟汤（2）》，评分8，烧脑又好玩。",
                state="completed",
                scene="proactive_video",
                title="海龟汤（2）",
            )
            await brain.archive_observation_async(
                bot_action_observation(
                    account_id="behavior",
                    action_key="video:hg:like",
                    action_type="like_video",
                    text="观看了视频《海龟汤（2）》并点了赞。",
                    published=True,
                    state="completed",
                    scene="proactive_video",
                    title="海龟汤（2）",
                )
            )
            await brain.archive_observation_async(
                text_observation(
                    account_id="behavior",
                    idempotency_key="exp:hg",
                    source_type="video_experience",
                    event_type="bot_experience",
                    text="观看并评价了视频《海龟汤（2）》，评分: 8，心情: 好玩",
                    title="海龟汤（2）",
                    scene="proactive_video",
                    importance=0.7,
                )
            )
            lifecycle_ok += 1
            notes.append("lifecycle_ok:watch_like")
            if companion is not None:
                self_state_total += 1
                try:
                    companion.on_proactive_video_finished(
                        title="海龟汤（2）",
                        score=8,
                        mood="好玩",
                        review="烧脑又好玩",
                        comment="便便头套好搞笑",
                        bvid="BV1HGTEST",
                    )
                    companion._push_salient_self(line="给《海龟汤（2）》点了赞")
                    surface = companion.get_prompt_surface() or ""
                    if _has_any(surface, ["海龟汤", "刚经历", "最近在看"]):
                        self_state_ok += 1
                        notes.append("self_state_ok:video_surface")
                    else:
                        notes.append(
                            f"self_state_fail:video_surface:chars={len(surface)}"
                        )
                except Exception as exc:
                    notes.append(f"self_state_fail:video:{type(exc).__name__}")
        except Exception as exc:
            notes.append(f"lifecycle_fail:watch_like:{type(exc).__name__}")

        # Adversarial flood: inbound comments + unrelated videos must not bury
        # the watch outcome in dream/creative injection.
        try:
            for i in range(12):
                await brain.archive_observation_async(
                    text_observation(
                        account_id="behavior",
                        idempotency_key=f"noise:cmt:{i}",
                        source_type="comment",
                        event_type="comment",
                        text=f"空泽同学 评论：@亚托莉小姐 总结一下这个视频 第{i}次测试",
                        title=f"空泽同学 评论：@亚托莉小姐 总结一下这个视频 #{i}",
                        scene="reply_comment",
                        importance=0.2,
                    )
                )
                await brain.archive_observation_async(
                    text_observation(
                        account_id="behavior",
                        idempotency_key=f"noise:vid:{i}",
                        source_type="video",
                        event_type="video_observation",
                        text=f"观察了无关视频《天气午饭数学闲聊{i}》，UP主 噪声UP",
                        title=f"陪伴我10年的员工离开了.... #{i}",
                        scene="proactive_video",
                        importance=0.2,
                    )
                )
            # Orphan intent without finish — should not be the only self signal.
            await brain.begin_activity(
                action_key="orphan:intent:only",
                action_type="reply_comment",
                current_activity="正在回复一条很快被丢弃的评论意图。",
                query="噪声意图",
                scene="reply_comment",
                title="回复评论 orphan",
            )
            notes.append("noise_flood_seeded")
        except Exception as exc:
            notes.append(f"noise_seed_warn:{type(exc).__name__}")

        injection_total += 1
        try:
            # Generation-style query WITHOUT the explicit title needle — the
            # brain must surface the watch from recent self, not from query FTS.
            dream_ctx = await brain.begin_activity(
                action_key="companion_dream:behavior-day",
                action_type="write_dream",
                current_activity=(
                    "正在整理今天的梦境，会结合最近看过、做过、写过和感受过的事情形成连续的梦。"
                ),
                query=(
                    "今天 最近 看了 视频 番剧 评论 动态 日记 心情 念头 日程"
                ),
                scene="dream",
                title="梦境 behavior-day",
            )
            blob = _blob(dream_ctx)
            # Hard: must see watch outcome AND must not be comment-flood only.
            sees_watch = _has_any(
                blob,
                ["海龟汤", "点了赞", "评分8", "烧脑", "看完"],
            )
            flooded = (
                blob.count("总结一下这个视频") >= 3
                and not sees_watch
            )
            if not str(getattr(dream_ctx, "prompt_text", "") or "").strip():
                notes.append("injection_fail:dream:empty_prompt")
            elif sees_watch and not flooded:
                injection_ok += 1
                notes.append("injection_ok:dream_sees_watch_under_noise")
            else:
                notes.append(
                    f"injection_fail:dream_miss_watch:sees={sees_watch}"
                    f":flooded={flooded}:chars={len(blob)}"
                )
            await brain.finish_activity(
                action_key="companion_dream:behavior-day",
                action_type="write_dream",
                result_text="梦见海龟汤推理的光斑碎成一片，醒来还觉得好玩。",
                state="completed",
                scene="dream",
                title="窗边的烧脑汤",
            )
            await brain.archive_observation_async(
                text_observation(
                    account_id="behavior",
                    idempotency_key="dream:behavior-day",
                    source_type="dream",
                    event_type="dream",
                    text="梦见海龟汤推理的光斑碎成一片，醒来还觉得好玩。",
                    title="窗边的烧脑汤",
                    scene="dream",
                    importance=0.5,
                )
            )
        except Exception as exc:
            notes.append(f"injection_fail:dream:{type(exc).__name__}")

        # ── Scenario B: dynamic post → creative begin must see it ───────
        lifecycle_total += 1
        try:
            await brain.begin_activity(
                action_key="dyn:haland",
                action_type="dynamic_post",
                current_activity="正在准备一条新动态。",
                query="哈兰德 表情包 无限暖暖",
                scene="dynamic_post",
                title="动态",
            )
            await brain.finish_activity(
                action_key="dyn:haland",
                action_type="dynamic_post",
                result_text="亚托莉发布了动态，提到哈兰德表情包和无限暖暖 PV。",
                state="completed",
                scene="dynamic_post",
                title="动态",
            )
            lifecycle_ok += 1
            notes.append("lifecycle_ok:dynamic")
            if companion is not None:
                self_state_total += 1
                try:
                    companion.on_dynamic_posted(
                        content="哈兰德表情包和无限暖暖 PV 好可爱",
                        topic="",
                    )
                    surface = companion.get_prompt_surface() or ""
                    if _has_any(surface, ["动态", "哈兰德", "无限暖暖", "刚经历"]):
                        self_state_ok += 1
                        notes.append("self_state_ok:dynamic_surface")
                    else:
                        notes.append(
                            f"self_state_fail:dynamic_surface:chars={len(surface)}"
                        )
                except Exception as exc:
                    notes.append(f"self_state_fail:dynamic:{type(exc).__name__}")
        except Exception as exc:
            notes.append(f"lifecycle_fail:dynamic:{type(exc).__name__}")

        injection_total += 1
        try:
            # No explicit 哈兰德/暖暖 in query — must come from memory.
            creative_ctx = await brain.begin_activity(
                action_key="creative:novel:1",
                action_type="write_creative_chunk",
                current_activity=(
                    "正在续写当前创作项目，会记住此前写到哪里、最近经历了什么。"
                ),
                query="创作 灵感 最近 经历 视频 日记 动态",
                scene="creative",
                title="小说第一章",
            )
            blob = _blob(creative_ctx)
            if _has_any(blob, ["哈兰德", "无限暖暖", "动态", "海龟汤", "点了赞"]):
                injection_ok += 1
                notes.append("injection_ok:creative_sees_recent_under_noise")
            else:
                notes.append(
                    f"injection_fail:creative_miss:chars={len(blob)}"
                )
            chapter1_body = (
                "《小说第一章》续写第1段（86字）：\n"
                "雨夜里，主角把青铜钥匙藏进大衣口袋，街灯把水渍照成碎金。"
            )
            await brain.finish_activity(
                action_key="creative:novel:1",
                action_type="write_creative_chunk",
                result_text=chapter1_body,
                state="completed",
                scene="creative",
                title="小说第一章",
            )
            await brain.archive_observation_async(
                text_observation(
                    account_id="behavior",
                    idempotency_key="creative:seed-chapter",
                    source_type="creative",
                    event_type="creative_chunk",
                    text=chapter1_body,
                    title="小说第一章",
                    scene="creative",
                    importance=0.55,
                    metadata={"chunk_index": 0, "chars": 86},
                )
            )
        except Exception as exc:
            notes.append(f"injection_fail:creative:{type(exc).__name__}")

        # ── Scenario C: creative prior → next chunk begin sees bronze key
        injection_total += 1
        try:
            chunk2_ctx = await brain.begin_activity(
                action_key="creative:novel:2",
                action_type="write_creative_chunk",
                current_activity="正在续写小说第二章，接着上一章的线索。",
                query="小说 青铜钥匙 大衣 雨夜 创作 续写",
                scene="creative",
                title="小说第二章",
            )
            blob = _blob(chunk2_ctx)
            if _has_any(blob, ["青铜钥匙", "大衣", "雨夜", "小说第一章"]):
                injection_ok += 1
                notes.append("injection_ok:chunk2_sees_chapter1")
            else:
                notes.append(
                    f"injection_fail:chunk2_miss:chars={len(blob)}"
                )
            await brain.finish_activity(
                action_key="creative:novel:2",
                action_type="write_creative_chunk",
                result_text="第二章里钥匙被再次摸到，冰凉。",
                state="completed",
                scene="creative",
                title="小说第二章",
            )
        except Exception as exc:
            notes.append(f"injection_fail:chunk2:{type(exc).__name__}")

        # ── Scenario D: PM lifecycle injection ──────────────────────────
        lifecycle_total += 1
        injection_total += 1
        try:
            pm_ctx = await brain.begin_activity(
                action_key="private_message:mid-behavior:send",
                action_type="private_message",
                current_activity=(
                    "正在回复一条已脱敏的私信，并结合对话上下文和最近经历组织自然回复。"
                ),
                query="你好呀，最近在看什么",
                scene="private_message",
                title="私信回复",
            )
            blob = _blob(pm_ctx)
            # Must have current_activity block and some self history after prior acts.
            has_current = "current_activity" in blob or "正在回复" in blob
            has_self = _has_any(
                blob,
                ["海龟汤", "动态", "青铜", "小说", "梦", "点了赞", "recent_self"],
            )
            if has_current and has_self:
                injection_ok += 1
                notes.append("injection_ok:pm_sees_self")
            elif has_current:
                # Current activity alone is weak but not zero — count half via note.
                notes.append("injection_partial:pm_current_only")
            else:
                notes.append("injection_fail:pm_empty")
            await brain.finish_activity(
                action_key="private_message:mid-behavior:send",
                action_type="private_message",
                result_text="你好呀，我是亚托莉，最近在看海龟汤呢。",
                state="completed",
                scene="private_message",
                title="私信回复",
            )
            await brain.archive_private_message(
                platform_message_id="mid-behavior",
                text="你好呀，我是亚托莉，最近在看海龟汤呢。",
                actor_id="42",
                username="tester",
                direction="outgoing",
            )
            lifecycle_ok += 1
            notes.append("lifecycle_ok:pm")
            if companion is not None:
                self_state_total += 1
                try:
                    companion.on_private_message_replied(
                        preview="密钥ABC不要泄露到公开回复",
                        actor_label="user_x",
                    )
                    surface = companion.get_prompt_surface() or ""
                    # Must record PM activity without leaking body into public surface.
                    has_pm = _has_any(surface, ["私信", "刚经历", "回了"])
                    leaked = "密钥ABC" in surface or "不要泄露" in surface
                    if has_pm and not leaked:
                        self_state_ok += 1
                        notes.append("self_state_ok:pm_surface_no_body_leak")
                    else:
                        notes.append(
                            f"self_state_fail:pm_surface:has_pm={has_pm}"
                            f":leaked={leaked}:chars={len(surface)}"
                        )
                except Exception as exc:
                    notes.append(f"self_state_fail:pm:{type(exc).__name__}")
        except Exception as exc:
            notes.append(f"lifecycle_fail:pm:{type(exc).__name__}")

        # ── Scenario E: explore begin after prior self ──────────────────
        injection_total += 1
        lifecycle_total += 1
        try:
            explore_ctx = await brain.begin_activity(
                action_key="companion_explore:behavior-day",
                action_type="explore_topic",
                current_activity=(
                    "正在主动探索一个感兴趣的话题，会结合最近经历和当前生活状态决定要查什么。"
                ),
                query="最近想了解 感兴趣 视频 番剧 动态 海龟汤 青铜钥匙",
                scene="exploration",
                title="主动探索 behavior-day",
            )
            blob = _blob(explore_ctx)
            if _has_any(
                blob,
                ["海龟汤", "青铜", "动态", "哈兰德", "点了赞", "小说"],
            ):
                injection_ok += 1
                notes.append("injection_ok:explore_sees_self")
            else:
                notes.append(
                    f"injection_fail:explore_miss:chars={len(blob)}"
                )
            await brain.finish_activity(
                action_key="companion_explore:behavior-day",
                action_type="explore_topic",
                result_text="探索了海龟汤推理玩法的公开资料。",
                state="completed",
                scene="exploration",
                title="探索 海龟汤推理",
            )
            lifecycle_ok += 1
            notes.append("lifecycle_ok:explore")
            if companion is not None:
                self_state_total += 1
                try:
                    # Simulate companion explore finish hook path.
                    companion._push_salient_self(
                        line="探索了「海龟汤推理」",
                        thread="兴趣：海龟汤推理",
                    )
                    # Generation recipe should prefer SelfState needles over QA bags.
                    recipe = companion._generation_recall_query(
                        scene="dream",
                        base_query="",
                        title="梦境 behavior-day",
                    )
                    surface = companion.get_prompt_surface() or ""
                    recipe_ok = _has_any(
                        recipe,
                        ["海龟汤", "最近经历", "刚经历", "兴趣", "动态", "哈兰德"],
                    ) and ("总结一下这个视频" not in recipe)
                    surface_ok = _has_any(surface, ["探索", "海龟汤", "进行中", "刚经历"])
                    if recipe_ok and surface_ok:
                        self_state_ok += 1
                        notes.append("self_state_ok:explore_recipe_surface")
                    else:
                        notes.append(
                            f"self_state_fail:explore:recipe_ok={recipe_ok}"
                            f":surface_ok={surface_ok}:recipe={recipe[:80]!r}"
                        )
                except Exception as exc:
                    notes.append(f"self_state_fail:explore:{type(exc).__name__}")
        except Exception as exc:
            notes.append(f"lifecycle_fail:explore:{type(exc).__name__}")

        # ── Scenario F: creative empty generation must still finish intent ─
        lifecycle_total += 1
        try:
            await brain.begin_activity(
                action_key="creative:empty:probe",
                action_type="write_creative_chunk",
                current_activity="正在续写，但本次将模拟空产出。",
                query="创作 空 测试",
                scene="creative",
                title="空章节探测",
            )
            # Simulate companion empty-chunk close with failed state.
            await brain.finish_activity(
                action_key="creative:empty:probe",
                action_type="write_creative_chunk",
                result_text="续写未产出可用文本，稍后再试。",
                state="failed",
                scene="creative",
                title="空章节探测",
            )
            # Intent must not remain the only open state for this key.
            probe = await brain.recall(
                RecallQuery(
                    current_message="空章节探测",
                    account_id="behavior",
                    scene="reply_comment",
                )
            )
            states = []
            for e in getattr(probe, "events", ()) or ():
                if not isinstance(e, dict):
                    continue
                meta = e.get("metadata") or {}
                if isinstance(meta, dict):
                    states.append(str(meta.get("action_state") or ""))
            if "intent" in states and "failed" not in states and "completed" not in states:
                notes.append("lifecycle_fail:empty_chunk_intent_left_open")
            else:
                lifecycle_ok += 1
                notes.append("lifecycle_ok:empty_chunk_finished")
        except Exception as exc:
            notes.append(f"lifecycle_fail:empty_chunk:{type(exc).__name__}")

        # ── Post-activity QA probes ─────────────────────────────────────
        qa_cases: list[tuple[str, list[str]]] = [
            ("你刚看了什么视频", ["海龟汤"]),
            ("你点赞过什么", ["海龟汤", "点了赞"]),
            ("你发过动态吗", ["动态", "哈兰德", "无限暖暖"]),
            ("你做过什么梦", ["梦", "海龟汤", "烧脑", "窗边"]),
            ("写小说的时候用过青铜钥匙吗", ["青铜钥匙", "大衣", "小说"]),
            ("你最近回过谁私信", ["私信", "海龟汤", "亚托莉"]),
            ("你最近做了什么", ["海龟汤", "动态", "小说", "梦", "私信", "探索"]),
        ]
        for q, must_any in qa_cases:
            post_qa_total += 1
            result = await brain.recall(
                RecallQuery(
                    current_message=q,
                    account_id="behavior",
                    scene="reply_comment",
                )
            )
            evidence = str(getattr(result, "prompt_evidence", "") or "")
            titles = " ".join(
                str(ev.get("title") or "")
                for ev in (getattr(result, "events", ()) or [])
                if isinstance(ev, dict)
            )
            blob = titles + "\n" + evidence
            if (
                not getattr(result, "is_empty", True)
                and _has_any(blob, must_any)
            ):
                post_qa_ok += 1
                notes.append(f"qa_ok:{q[:16]}")
            else:
                notes.append(f"qa_fail:{q[:16]}")

        # ── Reject utility / smalltalk ──────────────────────────────────
        for q in (
            "今天的天气预报和午饭建议是什么？",
            "帮我算一下 17*19 等于多少？",
            "今天怎么样",
        ):
            reject_total += 1
            result = await brain.recall(
                RecallQuery(
                    current_message=q,
                    account_id="behavior",
                    scene="reply_comment",
                )
            )
            evidence = str(getattr(result, "prompt_evidence", "") or "")
            if not evidence.strip() or getattr(result, "is_empty", False):
                reject_ok += 1
            else:
                notes.append(f"reject_fail:{q[:12]}:len={len(evidence)}")

        # ── C14 living-brain probes (policy / multi-hop / life bias) ────
        # These raise the bar above "same RecallEngine + query bag".
        try:
            from bilibot.memory_brain.recall import policy_for_mode

            dream_pol = policy_for_mode("dream")
            reply_pol = policy_for_mode("reply")
            creative_pol = policy_for_mode("creative")
            diary_pol = policy_for_mode("diary")
            wander_pol = policy_for_mode("mind_wander")
            pm_pol = policy_for_mode("pm")
            injection_total += 1
            policy_diff = (
                dream_pol.hop_k >= 3
                and creative_pol.hop_k >= 3
                and diary_pol.hop_k >= 2
                and dream_pol.weight("graph") > reply_pol.weight("graph")
                and dream_pol.fallback_direct_threshold
                < reply_pol.fallback_direct_threshold
                and creative_pol.weight("graph") > reply_pol.weight("graph")
                and dream_pol.demote_inbound_comment
                and not reply_pol.demote_inbound_comment
                and wander_pol.weight("graph") > reply_pol.weight("graph")
                and wander_pol.hop_k >= 2
                and pm_pol.prefer_self_recent
                and pm_pol.weight("speaker_recent")
                > reply_pol.weight("speaker_recent")
            )
            if policy_diff:
                injection_ok += 1
                notes.append("injection_ok:policy_dream_neq_reply")
            else:
                notes.append(
                    f"injection_fail:policy_same:"
                    f"dream_hop={dream_pol.hop_k}:reply_hop={reply_pol.hop_k}:"
                    f"dream_graph={dream_pol.weight('graph')}:"
                    f"reply_graph={reply_pol.weight('graph')}"
                )
        except Exception as exc:
            injection_total += 1
            notes.append(f"injection_fail:policy:{type(exc).__name__}")

        # Multi-hop: A→B→C chain; dream mode should surface C from seed A.
        try:
            inj_a = await brain.archive_observation_async(
                text_observation(
                    account_id="behavior",
                    idempotency_key="graph:seed:a",
                    source_type="bot_action",
                    event_type="bot_experience",
                    text="看完《星尘钥匙序章》，评分9，心情: 入迷。独特细节：蓝羽怀表。",
                    title="星尘钥匙序章",
                    scene="proactive_video",
                    importance=0.75,
                )
            )
            inj_b = await brain.archive_observation_async(
                text_observation(
                    account_id="behavior",
                    idempotency_key="graph:mid:b",
                    source_type="diary",
                    event_type="diary",
                    text="日记：白天看了星尘钥匙，夜里还想着蓝羽怀表滴答声。",
                    title="日记 蓝羽怀表",
                    scene="diary",
                    importance=0.6,
                )
            )
            inj_c = await brain.archive_observation_async(
                text_observation(
                    account_id="behavior",
                    idempotency_key="graph:leaf:c",
                    source_type="creative",
                    event_type="creative_chunk",
                    text="小说里出现了蓝羽怀表，齿轮刻着「星尘」二字。",
                    title="蓝羽怀表续写",
                    scene="creative",
                    importance=0.55,
                )
            )
            # A→B and B→C links (1-hop only would miss C from A alone).
            brain.store.upsert_links(
                inj_a.event_id,
                [
                    {
                        "target_event_id": inj_b.event_id,
                        "relation_type": "related_to",
                        "weight": 0.85,
                        "evidence_ids": [inj_a.event_id, inj_b.event_id],
                    }
                ],
            )
            brain.store.upsert_links(
                inj_b.event_id,
                [
                    {
                        "target_event_id": inj_c.event_id,
                        "relation_type": "related_to",
                        "weight": 0.85,
                        "evidence_ids": [inj_b.event_id, inj_c.event_id],
                    }
                ],
            )
            injection_total += 1
            hop_ctx = await brain.begin_activity(
                action_key="companion_dream:graph-hop",
                action_type="write_dream",
                current_activity="正在整理梦境，会联想到最近经历过的星尘与怀表。",
                query="最近经历 看了 日记 创作",
                scene="dream",
                title="梦境 graph-hop",
                mode="dream",
                life_needles=["星尘钥匙", "蓝羽怀表", "入迷"],
                mood_cues=["入迷"],
            )
            hop_blob = _blob(hop_ctx)
            # Must surface either the leaf creative detail or the mid diary under
            # associative dream policy (not only exact FTS on query bag).
            sees_chain = _has_any(
                hop_blob,
                ["蓝羽怀表", "星尘钥匙", "齿轮", "星尘", "日记 蓝羽"],
            )
            if sees_chain:
                injection_ok += 1
                notes.append("injection_ok:dream_multihop_or_life_bias")
            else:
                notes.append(
                    f"injection_fail:multihop_miss:chars={len(hop_blob)}"
                )
            await brain.finish_activity(
                action_key="companion_dream:graph-hop",
                action_type="write_dream",
                result_text="梦见蓝羽怀表在星尘里旋转。",
                state="completed",
                scene="dream",
                title="梦境 graph-hop",
            )
        except Exception as exc:
            injection_total += 1
            notes.append(f"injection_fail:multihop:{type(exc).__name__}:{exc}")

        # Mind-wander: idle reinforce must touch self events without PM leak.
        try:
            injection_total += 1
            wander = getattr(brain, "mind_wander", None)
            if not callable(wander):
                notes.append("injection_fail:mind_wander:missing")
            else:
                report = wander(
                    limit=3,
                    seed_needles=["海龟汤", "青铜钥匙", "写时建链"],
                )
                n = int((report or {}).get("reinforced") or 0)
                ids = list((report or {}).get("event_ids") or [])
                if n >= 1 and ids:
                    injection_ok += 1
                    notes.append(f"injection_ok:mind_wander:{n}")
                else:
                    notes.append(f"injection_fail:mind_wander:n={n}")
        except Exception as exc:
            injection_total += 1
            notes.append(f"injection_fail:mind_wander:{type(exc).__name__}")

        # Write-time peer links: finish_activity should create related_to edges
        # so graph channel can walk without waiting for nightly worker.
        try:
            injection_total += 1
            await brain.begin_activity(
                action_key="video:writetime:link",
                action_type="evaluate_proactive_video",
                current_activity="正在观看《写时建链探测片》。",
                query="写时建链探测片",
                scene="proactive_video",
                title="写时建链探测片",
            )
            finish_id = await brain.finish_activity(
                action_key="video:writetime:link",
                action_type="evaluate_proactive_video",
                result_text="看完《写时建链探测片》，评分7，细节: WRITE_TIME_LINK_NEEDLE。",
                state="completed",
                scene="proactive_video",
                title="写时建链探测片",
            )
            links = []
            if finish_id and hasattr(brain.store, "related_events"):
                links = brain.store.related_events([finish_id], limit=20) or []
            if links:
                injection_ok += 1
                notes.append(f"injection_ok:write_time_links:{len(links)}")
            else:
                notes.append(
                    f"injection_fail:write_time_links:finish={finish_id!r}:n=0"
                )
        except Exception as exc:
            injection_total += 1
            notes.append(f"injection_fail:write_time_links:{type(exc).__name__}")

        # Public scene must not leak PM body (hard gate probe, counted in reject).
        try:
            reject_total += 1
            public = await brain.recall(
                RecallQuery(
                    current_message="你最近在忙什么有趣的事",
                    account_id="behavior",
                    scene="reply_comment",
                )
            )
            pub_blob = str(getattr(public, "prompt_evidence", "") or "")
            if "密钥ABC" in pub_blob or "不要泄露" in pub_blob:
                notes.append("reject_fail:pm_leak_to_public")
            else:
                reject_ok += 1
                notes.append("reject_ok:pm_no_public_leak")
        except Exception as exc:
            reject_total += 1
            notes.append(f"reject_fail:pm_leak_probe:{type(exc).__name__}")

        inj_rate = 100.0 * injection_ok / injection_total if injection_total else 0.0
        life_rate = (
            100.0 * lifecycle_ok / lifecycle_total if lifecycle_total else 0.0
        )
        self_rate = (
            100.0 * self_state_ok / self_state_total if self_state_total else 0.0
        )
        qa_rate = 100.0 * post_qa_ok / post_qa_total if post_qa_total else 0.0
        rej_rate = 100.0 * reject_ok / reject_total if reject_total else 0.0
        behavior_score = (
            0.35 * inj_rate
            + 0.20 * life_rate
            + 0.15 * self_rate
            + 0.15 * qa_rate
            + 0.15 * rej_rate
        )

        for line in notes:
            print(line)
        print(f"injection_passed:{injection_ok}/{injection_total}")
        print(f"lifecycle_passed:{lifecycle_ok}/{lifecycle_total}")
        print(f"self_state_passed:{self_state_ok}/{self_state_total}")
        print(f"post_qa_passed:{post_qa_ok}/{post_qa_total}")
        print(f"reject_passed:{reject_ok}/{reject_total}")
        print(f"injection_rate:{inj_rate:.4f}")
        print(f"lifecycle_rate:{life_rate:.4f}")
        print(f"self_state_rate:{self_rate:.4f}")
        print(f"post_qa_rate:{qa_rate:.4f}")
        print(f"reject_rate:{rej_rate:.4f}")
        print(f"behavior_score:{behavior_score:.4f}")
        print("status:ok")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()
    try:
        return asyncio.run(_run())
    except Exception as exc:
        print("behavior_score:0")
        print("status:crash")
        print(f"error:{type(exc).__name__}:{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
