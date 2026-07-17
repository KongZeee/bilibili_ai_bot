"""
番剧追番模块 — PGC 视频下载 + 字幕识别 + 视觉轨分析

流程：选番(排行/时间表) → PGC视频下载(不含音频) → 字幕识别 → VideoUnderstandingService(字幕轨+视觉轨) → LLM评价 → 互动 → 番剧记忆

设计要点（相对参考仓库的差异化）：
- PGC 内容仍走完整视频下载（/pgc/player/web/playurl），但不下载/不转写音频。
- 番剧一般都有字幕，用字幕文本（带时间轴）作为「文本轨」替代声音轨，
  与视觉帧时序对齐，既省音频带宽又避免 ASR 误差。
- 视频理解复用 VideoUnderstandingService（understand(subtitle_segments=...)）。
"""
import asyncio
import inspect
import json
import logging
import os
import random
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from bilibot.memory_brain.ingestion import (
    bangumi_episode_observation,
    bot_action_observation,
    text_observation,
)
from bilibot.memory_brain.models import Observation, SourceDocument

logger = logging.getLogger("bilibot.bangumi")

# 评价 LLM prompt
_EVAL_PROMPT = """你刚看完一集番剧：
- 番名：{title}
- 类型：{styles} | 地区：{areas}
- B站评分：{score}
- 本集：第{ep_index}话 {ep_title}
- 字幕与画面分析日志：
{analysis}

{context}

以JSON格式回复你的观后感：
{{"score": 1到10的整数评分, "comment": "评论区留言（15-30字）", "mood": "看完的心情（开心/平静/无聊/感动/好笑/震撼/困惑/燃/虐 选一个）", "review": "本集感想（50字以内）", "want_continue": true或false}}

评分标准：1-3烂片、4-5凑合、6-7还行、8-9好看、10封神。大部分正常番应该在5-7分。
comment要求：像追番观众会打的弹幕或评论。
want_continue：是否值得继续追，烂番可以果断弃。
直接输出JSON，不要加其他内容。"""

# 番剧评论 LLM prompt（评价未生成 comment 时兜底）
_COMMENT_PROMPT = "你刚看完番剧《{title}》第{ep_index}话。在评论区留一条话，像追番观众随手打的评论（不超过30字）。直接输出内容。"


class BangumiService:
    """番剧追番服务"""

    def __init__(
        self,
        bili_api,
        llm_manager,
        video_service,
        config_loader,
        data_dir: str = "./data",
        memory_brain=None,
        account_id: str = "",
        persona_id: str = "",
        persona: str = "",
        companion=None,
        safety_checker=None,
    ):
        """
        Args:
            bili_api: BilibiliAPI 实例
            llm_manager: LLMManager / ModelRouter 实例
            video_service: VideoUnderstandingService 实例
            config_loader: ConfigLoader 实例
            data_dir: 数据目录
            memory_brain: 当前账号的 MemoryBrainService；必须由账号运行时注入
            account_id: 账号 ID，用于严格隔离脑库
            persona_id/persona: 本次观察所用人格 ID，仅作为来源信息
            companion: 可选 CompanionLifeService，评价时注入生活面
            safety_checker: 应用级 SafetyChecker；缺失时所有站外互动 fail-closed
        """
        self.bili = bili_api
        self.llm = llm_manager
        self.video_service = video_service
        self.config = config_loader
        self.data_dir = Path(data_dir)
        self._ensure_dirs()

        brain_account_id = str(getattr(memory_brain, "account_id", "") or "")
        derived_account_id = (
            self.data_dir.name if self.data_dir.parent.name == "accounts" else "default"
        )
        self.account_id = str(account_id or brain_account_id or derived_account_id)
        if brain_account_id and brain_account_id != self.account_id:
            raise ValueError("bangumi memory brain belongs to a different account")
        self.persona_id = str(persona_id or persona or "")
        self.companion = companion
        self.safety_checker = safety_checker
        self._action_locks: dict[str, asyncio.Lock] = {}
        # Production AccountInstance always injects the account brain. Creating a
        # second brain under data_dir would break isolation / reload semantics.
        if memory_brain is None:
            raise RuntimeError(
                "BangumiService requires an account-scoped memory_brain "
                "(fail-closed: no silent orphan brain)"
            )
        self.memory_brain = memory_brain

    def _ensure_dirs(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "bangumi_videos").mkdir(parents=True, exist_ok=True)

    def _provider_by_type(self, provider_type: str):
        if self.llm and hasattr(self.llm, "get_provider_by_type"):
            try:
                return self.llm.get_provider_by_type(provider_type)
            except Exception:
                return None
        return self.llm if provider_type == "chat" else None

    def _require_memory_brain(self):
        brain = getattr(self, "memory_brain", None)
        if brain is None:
            raise RuntimeError("bangumi memory brain is not initialized")
        brain_account = str(getattr(brain, "account_id", "") or "")
        if brain_account and brain_account != str(self.account_id):
            raise ValueError("bangumi memory brain belongs to a different account")
        return brain

    def rebind_memory_brain(self, memory_brain) -> None:
        """Hot-reload: re-attach the account brain after config reload."""
        if memory_brain is None:
            raise RuntimeError("cannot rebind bangumi to empty memory_brain")
        brain_account = str(getattr(memory_brain, "account_id", "") or "")
        if brain_account and brain_account != str(self.account_id):
            raise ValueError("bangumi memory brain belongs to a different account")
        self.memory_brain = memory_brain

    def rebind_safety_checker(self, safety_checker) -> None:
        """Hot-reload: keep bangumi interactions on the application safety gate."""
        self.safety_checker = safety_checker

    def _bangumi_options(self) -> Dict[str, Any]:
        """Return one normalized options mapping for old and new configs.

        ``proactive.bangumi`` is historically a boolean feature hint, while
        early implementations also accepted an object at the same path.  A
        production config with ``true`` must therefore never be treated like a
        mapping.  The optional top-level ``bangumi`` mapping is preferred for
        new deployments; legacy proactive fields remain compatible.
        """
        raw = self.config.get_raw_config() if hasattr(self.config, "get_raw_config") else {}
        raw = raw if isinstance(raw, Mapping) else {}
        proactive = raw.get("proactive") or {}
        proactive = proactive if isinstance(proactive, Mapping) else {}

        options: Dict[str, Any] = {}
        nested = proactive.get("bangumi")
        if isinstance(nested, Mapping):
            options.update(nested)
        top_level = raw.get("bangumi")
        if isinstance(top_level, Mapping):
            options.update(top_level)

        # Preserve the legacy flat controls used by existing config.yaml files.
        if "comment" not in options and "comment" in proactive:
            options["comment"] = bool(proactive.get("comment"))
        if "auto_follow" not in options:
            if "follow" in proactive:
                options["auto_follow"] = bool(proactive.get("follow"))
            else:
                options["auto_follow"] = True
        options.setdefault("continue_score", 7)
        options.setdefault("max_episodes", 3)
        options.setdefault("pools", ["trending"])
        return options

    def _create_memory_brain(self):
        """Test/legacy helper only; production must inject brain via ctor."""
        from bilibot.memory_brain.service import MemoryBrainService

        return MemoryBrainService(
            account_id=self.account_id,
            data_dir=self.data_dir,
            chat_provider=self._provider_by_type("chat"),
            embedding_provider=self._provider_by_type("embedding"),
            memory_config=getattr(self.config, "memory", None),
        )

    # ══════════════════════════════════════
    #  账号级观看状态与脑内上下文
    # ══════════════════════════════════════

    def _get_watched_ep_ids(self, season_id) -> set:
        states = self._require_memory_brain().list_bangumi_watch_state(
            season_id=str(season_id), limit=5000
        )
        return {
            str(state.get("episode_id"))
            for state in states
            if state.get("episode_id") and state.get("completed")
        }

    @staticmethod
    def _season_is_completed(states: Sequence[Mapping[str, Any]], season_id: Any) -> bool:
        sid = str(season_id)
        return any(
            str(state.get("season_id")) == sid
            and bool((state.get("metadata") or {}).get("season_completed"))
            for state in states
        )

    def _mark_season_completed(self, season_id: Any) -> None:
        brain = self._require_memory_brain()
        states = brain.list_bangumi_watch_state(
            season_id=str(season_id), limit=5000
        )
        if not states:
            return
        latest = states[0]
        metadata = dict(latest.get("metadata") or {})
        metadata["season_completed"] = True
        brain.upsert_bangumi_watch_state(
            str(season_id),
            str(latest["episode_id"]),
            episode_title=str(latest.get("episode_title") or ""),
            progress_ms=int(latest.get("progress_ms") or 0),
            completed=True,
            watched_at=float(latest.get("watched_at") or time.time()),
            metadata=metadata,
        )

    @staticmethod
    def _sort_eps(eps: list) -> list:
        def _key(e):
            idx = e.get("ep_index", "")
            try:
                return (0, int(idx))
            except (ValueError, TypeError):
                return (1, str(idx))
        return sorted(eps, key=_key)

    async def _get_context_summary(self, season_id, season_title) -> str:
        """Build evaluation context from archived brain events, not legacy JSON."""
        brain = self._require_memory_brain()
        states = brain.list_bangumi_watch_state(
            season_id=str(season_id), limit=5000
        )
        if not states:
            return ""
        recent = list(reversed(states[:5]))
        lines = []
        for state in recent:
            metadata = dict(state.get("metadata") or {})
            evaluation = dict(metadata.get("evaluation") or {})
            event_id = str(metadata.get("memory_event_id") or "")
            if event_id and hasattr(brain, "get_event"):
                event = brain.get_event(event_id, chunks_per_event=0)
                if inspect.isawaitable(event):
                    event = await event
                for source in (event or {}).get("sources", []):
                    if source.get("source_type") != "bangumi_evaluation":
                        continue
                    try:
                        evaluation = json.loads(source.get("full_text") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        pass
                    break
            lines.append(
                f"[第{metadata.get('episode_index', '?')}话] "
                f"评分:{evaluation.get('score', '?')}/10 "
                f"心情:{evaluation.get('mood', '?')} "
                f"感想:{evaluation.get('review', '')}"
            )
        return f"【《{season_title}》已看 {len(states)} 集，最近记录】\n" + "\n".join(lines)

    async def _recall_for_bangumi(
        self,
        *,
        season_title: str = "",
        episode_title: str = "",
        episode_index: str = "",
        season_id: str = "",
        styles: str = "",
    ) -> Dict[str, Any]:
        """Hybrid recall so evaluation/comment can link other scenes' memories.

        Failures degrade to empty evidence; archive path remains fail-closed.
        """
        empty = {"memory_evidence": "", "memory_event_ids": []}
        brain = getattr(self, "memory_brain", None)
        if brain is None or not callable(getattr(brain, "recall", None)):
            return empty
        query_parts = [
            "番剧 看番 评价 日记 视频",
            str(season_title or "").strip(),
            f"第{episode_index}话" if episode_index else "",
            str(episode_title or "").strip(),
            str(styles or "").strip(),
        ]
        query_text = " ".join(p for p in query_parts if p).strip() or "最近追番经历"
        try:
            begin = getattr(brain, "begin_activity", None)
            if callable(begin) and callable(getattr(type(brain), "begin_activity", None)):
                result = await begin(
                    action_key=(
                        f"bangumi_watch:{season_id or season_title}:{episode_index}:evaluate"
                    ),
                    action_type="evaluate_bangumi_episode",
                    current_activity=(
                        "正在观看并评价一集番剧，会结合此前追番进度、最近经历和当前感受决定评价、评论及是否继续追。"
                    ),
                    query=query_text,
                    scene="bangumi",
                    title=(
                        f"{str(season_title or '').strip()} 第{episode_index}话"
                        if episode_index not in (None, "")
                        else str(season_title or "").strip()
                    ),
                    oid=str(season_id or "").strip(),
                    persona_id=self.persona_id,
                    metadata={
                        "season_id": str(season_id or ""),
                        "episode_index": str(episode_index or ""),
                    },
                )
            else:
                from bilibot.memory_brain import RecallQuery

                result = await brain.recall(
                    RecallQuery(
                        current_message=query_text,
                        account_id=self.account_id or "",
                        title=str(season_title or "").strip(),
                        oid=str(season_id or "").strip(),
                        scene="bangumi",
                        entity_hints=tuple(
                            h
                            for h in (
                                str(season_title or "").strip(),
                                str(episode_title or "").strip(),
                            )
                            if h
                        ),
                    )
                )
        except Exception as exc:
            if callable(getattr(type(brain), "begin_activity", None)):
                raise RuntimeError("bangumi activity memory failed") from exc
            logger.debug("bangumi hybrid recall failed: %s", type(exc).__name__)
            return empty
        evidence = str(
            getattr(result, "prompt_text", "")
            or getattr(result, "prompt_evidence", "")
            or ""
        ) if result else ""
        event_ids: List[str] = []
        activity_ids = list(getattr(result, "event_ids", ()) or ()) if result else []
        for eid in activity_ids:
            eid = str(eid or "").strip()
            if eid and eid not in event_ids:
                event_ids.append(eid)
        for ev in (getattr(result, "events", ()) or ()) if result else ():
            if not isinstance(ev, dict):
                continue
            eid = str(ev.get("id") or ev.get("event_id") or "").strip()
            if eid and eid not in event_ids:
                event_ids.append(eid)
        if evidence:
            logger.info(
                "番剧混合召回: events=%s evidence_chars=%s title=%s",
                len(event_ids),
                len(evidence),
                (season_title or "")[:40],
            )
        return {
            "memory_evidence": evidence,
            "memory_event_ids": event_ids[:20],
        }

    # ══════════════════════════════════════
    #  选番
    # ══════════════════════════════════════

    async def pick_bangumi(self) -> Optional[int]:
        """选一部番：排行/时间表 → 去重 → 排除已看完 → 追更优先 → 随机选"""
        pools = self._bangumi_options().get("pools") or ["trending"]
        if isinstance(pools, str):
            pools = [pools]
        elif not isinstance(pools, Sequence):
            pools = ["trending"]

        candidates = []
        for pool in pools:
            pool = str(pool).lower().strip()
            try:
                if pool == "trending":
                    for st in [1, 4]:
                        data = await self.bili.get_bangumi_trending(season_type=st)
                        items = self._extract_list(data)
                        for it in items[:10]:
                            sid = it.get("season_id", 0)
                            if sid:
                                candidates.append({"season_id": sid, "title": it.get("title", ""),
                                                   "score": self._parse_score(it), "source": "trending"})
                elif pool == "timeline":
                    data = await self.bili.get_bangumi_timeline(day_before=2, day_after=0)
                    items = self._extract_timeline(data)
                    for it in items:
                        if it.get("published") and it.get("season_id"):
                            candidates.append({"season_id": it["season_id"], "title": it.get("title", ""),
                                               "score": 0, "source": "timeline"})
            except Exception as e:
                logger.warning(f"番剧池 {pool} 拉取失败: {e}")

        if not candidates:
            # 保底热门
            try:
                data = await self.bili.get_bangumi_trending(season_type=1)
                items = self._extract_list(data)
                candidates = [{"season_id": it.get("season_id", 0), "title": it.get("title", ""), "score": 0, "source": "fallback"}
                              for it in items]
            except Exception:
                pass
        if not candidates:
            logger.warning("无法获取任何番剧候选")
            return None

        # 去重
        seen, unique = set(), []
        for c in candidates:
            sid = c["season_id"]
            if sid not in seen:
                seen.add(sid)
                unique.append(c)

        states_by_season = {
            str(candidate["season_id"]): list(
                self._require_memory_brain().list_bangumi_watch_state(
                    season_id=str(candidate["season_id"]), limit=5000
                )
            )
            for candidate in unique
        }
        tracked_seasons = {
            sid
            for sid, states in states_by_season.items()
            if any(state.get("completed") for state in states)
        }

        # 排除已看完
        unique = [
            candidate
            for candidate in unique
            if not self._season_is_completed(
                states_by_season[str(candidate["season_id"])], candidate["season_id"]
            )
        ]

        # 追更中 vs 新番
        prioritized = [c for c in unique if str(c["season_id"]) in tracked_seasons]
        fresh = [c for c in unique if str(c["season_id"]) not in tracked_seasons]

        # 30% 追更旧番，70% 新番
        pool_to_pick = fresh
        if prioritized and (not fresh or random.random() < 0.3):
            pool_to_pick = prioritized
        top = pool_to_pick[:8]
        if not top:
            top = (prioritized + fresh)[:8]
        if not top:
            return None
        chosen = random.choice(top)
        logger.info(f"选番：《{chosen['title']}》(sid={chosen['season_id']}) 来源:{chosen['source']}")
        return chosen["season_id"]

    @staticmethod
    def _extract_list(data) -> list:
        if not data or not isinstance(data, dict):
            return []
        return (data.get("data") or data.get("result") or {}).get("list", []) or []

    @staticmethod
    def _extract_timeline(data) -> list:
        if not data or not isinstance(data, dict):
            return []
        result = []
        for day in (data.get("result") or data.get("data", {}).get("timeline", []) or []):
            for ep in day.get("episodes", []):
                result.append({
                    "season_id": ep.get("season_id", 0),
                    "title": ep.get("title", ""),
                    "published": ep.get("published", 0) == 1,
                })
        return result

    @staticmethod
    def _parse_score(item) -> float:
        rating = str(item.get("rating", "") or item.get("score", 0))
        cleaned = re.sub(r'[^\d.]', '', rating)
        try:
            return float(cleaned) if cleaned else 0
        except ValueError:
            return 0

    # ══════════════════════════════════════
    #  看番主流程
    # ══════════════════════════════════════

    async def watch_bangumi(self, season_id: int = None, max_episodes: int = 3,
                             start_ep_id: int = None) -> dict:
        """看番主流程：选番 → 逐集下载+分析+评价 → 评分低则停止

        Args:
            season_id: 指定番剧 season_id，None 则自动选番
            max_episodes: 本次最多看几集
            start_ep_id: 从指定 ep_id 开始看

        Returns:
            {"watched": n, "season_title": "...", "last_score": n}
        """
        # Fail-closed: no brain → never mark progress / never interact.
        self._require_memory_brain()
        if not season_id:
            season_id = await self.pick_bangumi()
        if not season_id:
            logger.warning("选番失败，跳过本次看番")
            return {"watched": 0}

        detail = await self.bili.get_bangumi_detail(season_id=season_id)
        if not detail or not detail.get("episodes"):
            logger.warning(f"番剧详情获取失败或无剧集 (sid={season_id})")
            return {"watched": 0}

        season_title = detail.get("title", "")
        all_eps = detail["episodes"]
        watched_ids = self._get_watched_ep_ids(season_id)
        unwatched = [
            ep
            for ep in all_eps
            if ep.get("ep_id") and str(ep["ep_id"]) not in watched_ids
        ]

        if start_ep_id:
            for i, ep in enumerate(all_eps):
                if str(ep.get("ep_id")) == str(start_ep_id):
                    unwatched = [
                        candidate
                        for candidate in all_eps[i:]
                        if candidate.get("ep_id")
                        and str(candidate["ep_id"]) not in watched_ids
                    ]
                    break

        if not unwatched:
            logger.info(f"《{season_title}》已全部看完")
            self._mark_season_completed(season_id)
            return {"watched": 0, "season_title": season_title, "completed": True}

        raw = self.config.get_raw_config() if hasattr(self.config, "get_raw_config") else {}
        raw = raw if isinstance(raw, Mapping) else {}
        bangumi_cfg = self._bangumi_options()
        try:
            continue_score = max(1, min(10, int(bangumi_cfg.get("continue_score", 7))))
        except (TypeError, ValueError):
            continue_score = 7
        comment_enabled = bangumi_cfg.get("comment", True)
        auto_follow = bangumi_cfg.get("auto_follow", True)
        like_enabled = raw.get("interactions", {}).get("like", {}).get("enabled", False)

        logger.info(f"开始看番：《{season_title}》共{len(all_eps)}集，已看{len(watched_ids)}集，本次最多{max_episodes}集")

        watched_count = 0
        last_score = 0

        for ep in unwatched:
            if watched_count >= max_episodes:
                break

            context = await self._get_context_summary(season_id, season_title)
            score, evaluation = await self._watch_episode(
                detail, ep, context, comment_enabled, auto_follow, like_enabled,
            )
            watched_count += 1
            last_score = score

            # 评分低于阈值且不想继续 → 停止
            if score < continue_score:
                want = (evaluation or {}).get("want_continue", False)
                if not want:
                    logger.info(f"评分{score}<{continue_score}，停止看番")
                    break
                logger.info(f"评分{score}偏低但想继续追")

            # 集间等待
            if watched_count < max_episodes and watched_count < len(unwatched):
                wait = random.randint(20, 60)
                logger.info(f"集间等待 {wait}秒...")
                await asyncio.sleep(wait)

        logger.info(f"看番结束：《{season_title}》本次看了 {watched_count} 集")
        return {"watched": watched_count, "season_title": season_title, "last_score": last_score}

    async def _watch_episode(self, season_info, ep_info, context, comment_enabled, auto_follow, like_enabled):
        """看一集番剧，先完整归档，再互动、记进度并清理媒体。"""
        ep_id = ep_info.get("ep_id", 0)
        cid = ep_info.get("cid", 0)
        ep_index = ep_info.get("ep_index", "?")
        ep_title = ep_info.get("long_title", "") or ep_info.get("title", "")
        season_title = season_info.get("title", "")

        logger.info(f"看番：《{season_title}》第{ep_index}话 {ep_title}")

        # 1. 下载 PGC 视频（番剧走字幕识别，不下载音频轨）
        analysis_result: dict[str, Any] = {}
        subtitle_segments: list[Mapping[str, Any]] = []
        video_path = None
        try:
            save_path = str(self.data_dir / "bangumi_videos" / f"ep{ep_id}")
            video_path = await self.bili.download_bangumi_video(
                ep_id, cid, save_path, quality=32, with_audio=False
            )

            # 2. 获取番剧字幕（替代声音轨）
            try:
                subtitle_segments = list(
                    await self.bili.get_bangumi_subtitles(ep_id, cid) or []
                )
            except Exception as e:
                logger.warning(f"番剧字幕获取失败（降级为视觉轨）: {e}")

            video_service_available = bool(
                self.video_service and self.video_service.is_available()
            )
            if video_service_available:
                if not video_path or not Path(str(video_path)).is_file():
                    raise RuntimeError("番剧视频下载未生成可分析的媒体文件")

                # 3. 视频理解分析：番剧字幕识别模式（read_subtitles=True）
                #    - 不下载/不转写音频（with_audio=False + 跳过 ASR）
                #    - B站正版番剧的中文字幕多压在画面内（硬字幕，无独立字幕轨文件），
                #      由 Vision LLM 从关键帧转写画面上的字幕文字，替代声音轨
                #    - 若某集恰好有独立字幕文件(subtitle_segments)，则优先用文件
                logger.info("开始视频理解分析（番剧字幕识别：帧内硬字幕 + 视觉轨，不走音频 ASR）...")
                result = await self.video_service.understand(
                    video_path,
                    subtitle_segments=subtitle_segments,
                    read_subtitles=True,
                    defer_cleanup=True,
                    require_complete_visual=True,
                )
                if not isinstance(result, Mapping):
                    raise RuntimeError("番剧视频理解返回了无效结果")
                analysis_result = dict(result)
                degradation = str(analysis_result.get("degradation_reason") or "")
                if degradation:
                    raise RuntimeError(f"番剧视频提取未完成: {degradation}")
                analysis = str(analysis_result.get("behavior_log") or "")
                if analysis:
                    logger.info(f"视频分析完成: {len(analysis)} 字符")
                else:
                    analysis_result["behavior_log"] = self._fallback_analysis(
                        season_info, ep_info
                    )
            else:
                # 降级：用番剧详情做简单文本
                analysis_result = {
                    "behavior_log": self._fallback_analysis(season_info, ep_info),
                    "degradation_reason": "video_understanding_unavailable",
                }
        except Exception as e:
            retained_work_dir = getattr(e, "work_dir", "")
            if retained_work_dir and not analysis_result.get("work_dir"):
                analysis_result["work_dir"] = retained_work_dir
            # 失败不再保留媒体：重试会重新下载，残留只会占满磁盘
            self._cleanup_episode_artifacts(ep_id, video_path, analysis_result)
            logger.error(
                "番剧视频提取未完成，已清理临时媒体并等待后续重试: sid=%s ep=%s",
                season_info.get("season_id", 0),
                ep_id,
                exc_info=True,
            )
            raise

        # 3. LLM 评价（本剧进度 + 跨场景混合召回）
        analysis = str(analysis_result.get("behavior_log") or "")
        memory_bundle = await self._recall_for_bangumi(
            season_title=season_title,
            episode_title=ep_title,
            episode_index=str(ep_index),
            season_id=str(season_info.get("season_id", "") or ""),
            styles=str(season_info.get("styles") or ""),
        )
        memory_evidence = str(memory_bundle.get("memory_evidence") or "")
        memory_event_ids = list(memory_bundle.get("memory_event_ids") or [])
        evaluation = await self._evaluate_episode(
            season_info,
            ep_info,
            analysis,
            context,
            memory_evidence=memory_evidence,
            companion_context="",  # resolved inside from self.companion if present
        )
        if not evaluation:
            evaluation = {"score": 5, "comment": "", "mood": "平静", "review": "没什么特别的感觉", "want_continue": False}

        evaluation = dict(evaluation)
        try:
            score = max(1, min(10, int(evaluation.get("score", 5))))
        except (TypeError, ValueError):
            score = 5
        evaluation["score"] = score
        comment = evaluation.get("comment", "")
        mood = evaluation.get("mood", "平静")
        review = evaluation.get("review", "")
        logger.info(f"评分：{score}/10 | 心情：{mood} | 短评：{(comment or '')[:30]}")

        # 4. 原始提取内容与评价必须先持久提交；失败时不允许主动互动 / 不推进 completed。
        try:
            self._require_memory_brain()
            envelope = self._build_episode_envelope(
                season_info,
                ep_info,
                analysis_result,
                subtitle_segments,
                evaluation,
            )
            existing_event = await self._find_existing_episode_event(envelope.idempotency_key)
            if existing_event:
                memory_event_id = str(
                    existing_event.get("id") or existing_event.get("event_id") or ""
                )
            else:
                archived = await self._archive_envelope(envelope)
                memory_event_id = str(
                    getattr(archived, "event_id", "")
                    or (
                        archived.get("event_id", "")
                        if isinstance(archived, Mapping)
                        else ""
                    )
                )
            if not memory_event_id:
                # Existing hit without id is still a committed observation; keep going
                # but surface empty id for metadata.
                logger.debug(
                    "bangumi archive missing event_id sid=%s ep=%s",
                    season_info.get("season_id", 0),
                    ep_id,
                )

            # 5. 只有完整档案成功后才执行互动；每个实际结果单独归档。
            action_results = await self._perform_interactions(
                season_info,
                ep_info,
                score=score,
                comment=comment,
                comment_enabled=comment_enabled,
                auto_follow=auto_follow,
                like_enabled=like_enabled,
                memory_evidence=memory_evidence,
            )

            completed_ids = self._get_watched_ep_ids(season_info.get("season_id", 0))
            completed_ids.add(str(ep_id))
            catalog_ids = {
                str(item.get("ep_id"))
                for item in season_info.get("episodes", [])
                if item.get("ep_id")
            }
            season_completed = bool(catalog_ids) and catalog_ids.issubset(completed_ids)
            self._require_memory_brain().upsert_bangumi_watch_state(
                str(season_info.get("season_id", 0)),
                str(ep_id),
                episode_title=ep_title,
                progress_ms=max(0, int(ep_info.get("duration") or 0)),
                completed=True,
                metadata={
                    "season_title": season_title,
                    "episode_index": str(ep_index),
                    "evaluation": dict(evaluation),
                    "memory_event_id": memory_event_id,
                    "recall_memory_event_ids": memory_event_ids[:12],
                    "action_results": action_results,
                    "season_completed": season_completed,
                },
            )
            # Close the begin_activity intent so cross-scene recent/self memory
            # sees a terminal evaluate_bangumi_episode outcome, not a dangling intent.
            try:
                brain = self._require_memory_brain()
                finish = getattr(brain, "finish_activity", None)
                if callable(finish):
                    ep_label = f"第{ep_index}话" if ep_index not in (None, "") else ""
                    result_text = (
                        f"看完《{season_title}》{ep_label}"
                        f"{('「' + ep_title + '」') if ep_title else ''}，"
                        f"评分{score}，心情{mood}。"
                        f"{('想继续追。' if evaluation.get('want_continue') else '')}"
                        f"{(' 短评：' + str(comment)[:120]) if comment else ''}"
                    ).strip()
                    await finish(
                        action_key=(
                            f"bangumi_watch:{season_info.get('season_id', 0) or season_title}"
                            f":{ep_index}:evaluate"
                        ),
                        action_type="evaluate_bangumi_episode",
                        result_text=result_text,
                        state="completed",
                        scene="bangumi",
                        title=f"{season_title} {ep_label}".strip(),
                        persona_id=self.persona_id,
                        metadata={
                            "season_id": str(season_info.get("season_id", 0) or ""),
                            "episode_id": str(ep_id),
                            "episode_index": str(ep_index),
                            "score": score,
                            "memory_event_id": memory_event_id,
                        },
                    )
            except Exception:
                logger.warning(
                    "bangumi finish_activity failed sid=%s ep=%s",
                    season_info.get("season_id", 0),
                    ep_id,
                    exc_info=True,
                )
        except Exception:
            self._cleanup_episode_artifacts(ep_id, video_path, analysis_result)
            logger.error(
                "番剧完整档案提交失败，已清理临时媒体并延后处理: sid=%s ep=%s",
                season_info.get("season_id", 0),
                ep_id,
                exc_info=True,
            )
            raise
        else:
            self._cleanup_episode_artifacts(ep_id, video_path, analysis_result)

        successful_actions = [
            item["label"] for item in action_results if item.get("success")
        ]
        action_str = " ".join(successful_actions) if successful_actions else "（默默看完）"
        logger.info(f"互动：{action_str}")

        return score, evaluation

    async def _archive_envelope(self, envelope):
        brain = self._require_memory_brain()
        method = getattr(brain, "archive_observation_async", None)
        if callable(method):
            result = method(envelope)
        else:
            result = brain.archive_observation(envelope)
        archived = await result if inspect.isawaitable(result) else result
        committed = (
            archived.get("source_committed", True)
            if isinstance(archived, Mapping)
            else getattr(archived, "source_committed", True)
        )
        if committed is not True:
            raise RuntimeError("memory source archive did not commit")
        return archived

    async def _find_existing_episode_event(self, idempotency_key: str):
        brain = self._require_memory_brain()
        method = getattr(brain, "find_by_identifiers", None)
        if not callable(method):
            return None
        events = method([idempotency_key], limit=1)
        if inspect.isawaitable(events):
            events = await events
        if not events:
            return None
        return events[0] if isinstance(events, (list, tuple)) else events

    @classmethod
    def _safe_extracted_data(cls, value: Any) -> Any:
        media_keys = {
            "audio_path",
            "file_path",
            "frame_path",
            "image_path",
            "keyframe",
            "keyframes",
            "media_path",
            "video_path",
            "work_dir",
        }
        if isinstance(value, Mapping):
            return {
                str(key): cls._safe_extracted_data(item)
                for key, item in value.items()
                if str(key).casefold() not in media_keys
                and not str(key).casefold().endswith("_base64")
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [cls._safe_extracted_data(item) for item in value]
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[binary omitted]"
        return value

    def _build_episode_envelope(
        self,
        season_info: Mapping[str, Any],
        ep_info: Mapping[str, Any],
        analysis_result: Mapping[str, Any],
        subtitle_segments: Sequence[Mapping[str, Any]],
        evaluation: Mapping[str, Any],
    ):
        season_id = season_info.get("season_id", 0)
        ep_id = ep_info.get("ep_id", 0)
        ep_index = str(ep_info.get("ep_index", "?"))
        season_title = str(season_info.get("title") or "")
        ep_title = str(ep_info.get("long_title") or ep_info.get("title") or "")
        envelope = bangumi_episode_observation(
            account_id=self.account_id,
            observation_key=f"{season_id}:{ep_id}",
            season_id=season_id,
            episode_id=ep_id,
            season_title=season_title,
            episode_title=ep_title,
            episode_index=ep_index,
            analysis_result=analysis_result,
            subtitle_segments=subtitle_segments,
            persona_id=self.persona_id,
            evaluation=evaluation,
        )
        sources = list(envelope.normalized_sources())

        subtitle_text = "\n".join(
            str(item.get("content") or "")
            for item in subtitle_segments
            if item.get("content")
        )
        if subtitle_text and not any(
            source.source_type == "subtitle" and source.full_text == subtitle_text
            for source in sources
        ):
            safe_segments = self._safe_extracted_data(list(subtitle_segments))
            sources.append(
                SourceDocument(
                    source_type="subtitle",
                    external_id=f"{ep_id}:subtitles",
                    full_text=subtitle_text,
                    data={"segments": safe_segments},
                    observations=tuple(
                        Observation(
                            text=str(item.get("content") or ""),
                            modality="subtitle",
                            start_ms=int(float(item.get("from") or 0) * 1000),
                            end_ms=int(float(item.get("to") or 0) * 1000),
                            external_id=str(index),
                            data=self._safe_extracted_data(dict(item)),
                        )
                        for index, item in enumerate(subtitle_segments)
                        if item.get("content")
                    ),
                )
            )

        ocr_rows = self._extract_ocr_rows(analysis_result)
        ocr_text = "\n".join(row["text"] for row in ocr_rows)
        if ocr_rows and not any(
            source.source_type == "ocr" and source.full_text == ocr_text
            for source in sources
        ):
            sources.append(
                SourceDocument(
                    source_type="ocr",
                    external_id=f"{ep_id}:ocr",
                    full_text=ocr_text,
                    data={"observations": ocr_rows},
                    observations=tuple(
                        Observation(
                            text=row["text"],
                            modality="ocr",
                            start_ms=row.get("start_ms"),
                            end_ms=row.get("end_ms"),
                            data=row,
                        )
                        for row in ocr_rows
                    ),
                )
            )

        evaluation_text = json.dumps(
            dict(evaluation), ensure_ascii=False, sort_keys=True, default=str
        )
        sources.append(
            SourceDocument(
                source_type="bangumi_evaluation",
                external_id=f"{ep_id}:evaluation",
                full_text=evaluation_text,
                data=dict(evaluation),
                observations=(
                    Observation(
                        text=str(
                            evaluation.get("review")
                            or evaluation.get("comment")
                            or evaluation_text
                        ),
                        modality="bangumi_evaluation",
                        actor_id="self",
                        data=dict(evaluation),
                    ),
                ),
            )
        )
        return replace(
            envelope,
            sources=tuple(sources),
            metadata={
                **dict(envelope.metadata),
                "episode_index": ep_index,
                "evaluation": dict(evaluation),
            },
        )

    @classmethod
    def _extract_ocr_rows(
        cls, analysis_result: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in analysis_result.get("ocr_observations", []) or []:
            if not isinstance(item, Mapping):
                continue
            text = str(
                item.get("text") or item.get("content") or item.get("description") or ""
            )
            if text:
                row = cls._safe_extracted_data(dict(item))
                row["text"] = text
                if "start_ms" not in row and item.get("timestamp") is not None:
                    row["start_ms"] = int(float(item.get("timestamp") or 0) * 1000)
                    row["end_ms"] = row["start_ms"]
                rows.append(row)
        for item in analysis_result.get("visual_observations", []) or []:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("ocr_text") or item.get("ocr") or "")
            if not text:
                continue
            timestamp = int(float(item.get("timestamp") or 0) * 1000)
            rows.append(
                {
                    "text": text,
                    "start_ms": timestamp,
                    "end_ms": timestamp,
                    "frame_number": item.get("frame_number"),
                }
            )
        return rows

    async def _perform_interactions(
        self,
        season_info: Mapping[str, Any],
        ep_info: Mapping[str, Any],
        *,
        score: int,
        comment: str,
        comment_enabled: bool,
        auto_follow: bool,
        like_enabled: bool,
        memory_evidence: str = "",
    ) -> list[dict[str, Any]]:
        aid = ep_info.get("aid", 0)
        if not aid:
            return []
        results: list[dict[str, Any]] = []
        if score >= 6 and like_enabled:
            results.append(
                await self._execute_and_archive_action(
                    season_info,
                    ep_info,
                    action_type="like",
                    label="点赞",
                    # aid 是稿件 id，必须走视频点赞接口，不是评论 like_reply
                    operation=lambda: self.bili.like_video(aid, like=1),
                )
            )
        if score >= 6 and comment_enabled:
            if not comment:
                comment = await self._generate_comment(
                    str(season_info.get("title") or ""),
                    ep_info.get("ep_index", "?"),
                    memory_evidence=memory_evidence,
                    episode_title=str(
                        ep_info.get("long_title") or ep_info.get("title") or ""
                    ),
                ) or "这集还行"
            result = await self._execute_and_archive_action(
                season_info,
                ep_info,
                action_type="comment",
                label="评论",
                operation=lambda: self.bili.post_comment(
                    oid=aid, content=comment, comment_type=1
                ),
                content=comment,
            )
            results.append(result)
            if result["success"]:
                logger.info(f"番剧评论：{comment}")
        if score >= 7 and auto_follow:
            results.append(
                await self._execute_and_archive_action(
                    season_info,
                    ep_info,
                    action_type="follow",
                    label="追番",
                    operation=lambda: self.bili.follow_bangumi(
                        season_info.get("season_id", 0)
                    ),
                )
            )
        return results

    async def _execute_and_archive_action(
        self,
        season_info: Mapping[str, Any],
        ep_info: Mapping[str, Any],
        *,
        action_type: str,
        label: str,
        operation,
        content: str = "",
    ) -> dict[str, Any]:
        season_id = season_info.get("season_id", 0)
        ep_id = ep_info.get("ep_id", 0)
        action_key = f"bangumi:{season_id}:{ep_id}:action:{action_type}"
        lock = self._action_locks.setdefault(action_key, asyncio.Lock())

        async with lock:
            base_metadata = {
                "label": label,
                "content": content,
                "season_id": str(season_id),
                "episode_id": str(ep_id),
                "episode_index": str(ep_info.get("ep_index", "?")),
            }

            async def archive_state(
                state: str, *, success: bool, error: str = ""
            ) -> dict[str, Any]:
                result = {
                    "action_type": action_type,
                    "label": label,
                    "success": success,
                    "error": error,
                    "content": content,
                }
                outcome = "成功" if success else (error or "失败")
                envelope = bot_action_observation(
                    account_id=self.account_id,
                    action_key=action_key,
                    action_type=action_type,
                    text=(
                        f"番剧《{season_info.get('title', '')}》第"
                        f"{ep_info.get('ep_index', '?')}话：{label}{outcome}。"
                        + (f"实际内容：{content}" if content else "")
                    ),
                    published=success,
                    persona_id=self.persona_id,
                    title=f"番剧{label}{state}",
                    scene="bangumi",
                    metadata={**base_metadata, **result},
                    importance=0.55,
                    state=state,
                )
                await self._archive_envelope(envelope)
                return result

            intent = bot_action_observation(
                account_id=self.account_id,
                action_key=action_key,
                action_type=action_type,
                text=(
                    f"准备对番剧《{season_info.get('title', '')}》第"
                    f"{ep_info.get('ep_index', '?')}话执行{label}。"
                    + (f"拟发布内容：{content}" if content else "")
                ),
                published=False,
                persona_id=self.persona_id,
                title=f"番剧{label}意图",
                scene="bangumi",
                metadata=base_metadata,
                importance=0.5,
                state="intent",
            )

            # A previous intent without a committed outcome may mean the remote
            # action succeeded just before a crash.  Prefer a deferred/manual
            # decision over repeating a like/comment/follow.
            if await self._find_existing_episode_event(intent.idempotency_key):
                return await archive_state(
                    "deferred", success=False, error="ACTION_ALREADY_ATTEMPTED"
                )
            await self._archive_envelope(intent)

            safety = self.safety_checker
            if safety is None:
                return await archive_state(
                    "rejected", success=False, error="SAFETY_CHECKER_UNAVAILABLE"
                )

            try:
                if content:
                    passed, reason = await safety.check_content(
                        content,
                        scene="bangumi_comment",
                        persona_id=self.persona_id,
                        account_id=self.account_id,
                    )
                elif safety.is_paused():
                    passed, reason = False, "global_paused"
                elif safety.is_account_paused(self.account_id):
                    passed, reason = False, f"account_paused:{self.account_id}"
                else:
                    passed, reason = True, "ok"
            except Exception as exc:
                passed, reason = False, f"SAFETY_CHECK_FAILED:{type(exc).__name__}"
            if not passed:
                return await archive_state("rejected", success=False, error=str(reason))

            rate_scene = f"bangumi_{action_type}"
            try:
                rate_ok, rate_reason = safety.check_and_record_rate_limit(
                    rate_scene, account_id=self.account_id
                )
            except Exception as exc:
                rate_ok, rate_reason = False, f"RATE_CHECK_FAILED:{type(exc).__name__}"
            if not rate_ok:
                return await archive_state(
                    "rejected", success=False, error=str(rate_reason)
                )

            error = ""
            state = "failed"
            try:
                raw = await operation()
                if raw is None:
                    success = False
                    error = "RESULT_UNKNOWN"
                    state = "result_unknown"
                else:
                    success = bool(raw)
                    if success:
                        state = "completed"
                    else:
                        error = "REMOTE_REJECTED"
            except Exception as exc:
                success = False
                error = f"RESULT_UNKNOWN:{type(exc).__name__}"
                state = "result_unknown"
                logger.warning("番剧%s失败: %s", label, error)

            if not success:
                try:
                    safety.refund_publish(rate_scene, account_id=self.account_id)
                except Exception:
                    logger.warning("番剧%s限流配额回滚失败", label, exc_info=True)

            result = await archive_state(state, success=success, error=error)
            if success and content:
                try:
                    safety.record_content(content, account_id=self.account_id)
                except Exception:
                    logger.warning("番剧评论最近内容记录失败", exc_info=True)
            return result

    @staticmethod
    def _artifact_paths(video_path: Any, analysis_result: Mapping[str, Any]) -> list[Path]:
        paths: list[Path] = []
        if video_path:
            paths.append(Path(str(video_path)))
        artifact_keys = {"work_dir", "keyframe", "keyframes", "frame_path", "image_path"}

        def collect(value: Any, key: str = "") -> None:
            if isinstance(value, Mapping):
                for item_key, item in value.items():
                    collect(item, str(item_key).casefold())
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    collect(item, key)
            elif key in artifact_keys and isinstance(value, (str, os.PathLike)) and value:
                paths.append(Path(value))

        collect(analysis_result)
        return list(dict.fromkeys(paths))

    def _preserve_episode_artifacts(
        self, ep_id: Any, video_path: Any, analysis_result: Mapping[str, Any]
    ) -> None:
        work_dir = analysis_result.get("work_dir")
        if not work_dir:
            return
        source = Path(str(work_dir))
        if not source.is_dir():
            return
        destination = self.data_dir / "bangumi_videos" / f"deferred_ep{ep_id}_work"
        try:
            if source.resolve() != destination.resolve():
                shutil.copytree(source, destination, dirs_exist_ok=True)
        except Exception as exc:
            logger.warning("保留番剧关键帧失败: %s", type(exc).__name__)

    def _cleanup_episode_artifacts(
        self, ep_id: Any, video_path: Any, analysis_result: Mapping[str, Any]
    ) -> None:
        paths = self._artifact_paths(video_path, analysis_result)
        paths.append(self.data_dir / "bangumi_videos" / f"deferred_ep{ep_id}_work")
        for path in sorted(dict.fromkeys(paths), key=lambda item: len(item.parts), reverse=True):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except Exception as exc:
                logger.warning("清理番剧临时文件失败 %s: %s", path, type(exc).__name__)

    @staticmethod
    def _fallback_analysis(season_info, ep_info) -> str:
        """降级分析（视频下载/分析失败时用文本）"""
        parts = [
            f"番剧《{season_info.get('title', '?')}》第{ep_info.get('ep_index', '?')}话",
        ]
        evaluate = season_info.get("evaluate", "")
        if evaluate:
            parts.append(f"简介：{evaluate}")
        areas = season_info.get("areas", "")
        if areas:
            parts.append(f"地区：{areas}")
        score = season_info.get("score", 0)
        if score:
            parts.append(f"B站评分：{score}")
        return "\n".join(parts)

    async def _evaluate_episode(
        self,
        season_info,
        ep_info,
        analysis,
        context,
        memory_evidence: str = "",
        companion_context: str = "",
    ) -> Optional[dict]:
        """LLM 评价番剧单集（本剧进度 + 可选跨场景记忆证据 + 生活面）。"""
        if not self.llm:
            return None
        try:
            provider = self._provider_by_type("chat")
            if not provider:
                return None

            extra_blocks = []
            if context:
                extra_blocks.append(f"【你之前看过的进度】\n{context}")
            life = str(companion_context or "").strip()
            if not life:
                companion = getattr(self, "companion", None)
                if companion is not None and getattr(companion, "enabled", False):
                    try:
                        getter = getattr(companion, "build_proactive_context_block", None)
                        if callable(getter):
                            life = str(getter() or "").strip()
                        if not life:
                            surface = getattr(companion, "get_prompt_surface", None)
                            if callable(surface):
                                life = str(surface() or "").strip()
                    except Exception:
                        life = ""
            if life:
                block = life if life.startswith("【") else f"【你今天的状态与念头】\n{life}"
                extra_blocks.append(block[:800])
            mem = str(memory_evidence or "").strip()
            if mem:
                extra_blocks.append(
                    "【相关记忆/近期经历】（可自然联想，勿编造未出现的细节）\n"
                    + mem[:1600]
                )
            context_block = ("\n" + "\n\n".join(extra_blocks)) if extra_blocks else ""

            prompt = _EVAL_PROMPT.format(
                title=season_info.get("title", ""),
                styles=season_info.get("styles", ""),
                areas=season_info.get("areas", ""),
                score=season_info.get("score", "暂无"),
                ep_index=ep_info.get("ep_index", "?"),
                ep_title=ep_info.get("long_title", "") or ep_info.get("title", ""),
                analysis=analysis if analysis else "（无分析数据）",
                context=context_block,
            )
            from bilibot.services.token_usage import usage_context
            with usage_context(scene="bangumi_eval", account_id=getattr(self, "account_id", "") or ""):
                # Reasoning models need headroom before JSON content appears.
                text = await provider.generate(prompt, max_tokens=1500)
            if not text:
                return None

            # 解析 JSON
            text = text.strip()
            m = re.search(r'\{.*\}', text, re.DOTALL)
            candidate = m.group() if m else text
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                fixed = re.sub(r',\s*([}\]])', r'\1', candidate)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    logger.warning(f"番剧评价JSON解析失败: {text[:300]}")
                    return None
        except Exception as e:
            logger.error(f"番剧评价失败: {e}")
            return None

    async def _generate_comment(
        self,
        title,
        ep_index,
        memory_evidence: str = "",
        episode_title: str = "",
    ) -> str:
        """LLM 生成番剧评论（可带混合记忆，避免空洞夸赞）。"""
        if not self.llm:
            return ""
        try:
            provider = self._provider_by_type("chat")
            if not provider:
                return ""
            mem = str(memory_evidence or "").strip()
            mem_section = (
                f"\n【相关记忆/近期经历】\n{mem[:800]}\n" if mem else ""
            )
            ep_bit = f"「{episode_title}」" if episode_title else ""
            prompt = (
                _COMMENT_PROMPT.format(title=title, ep_index=ep_index)
                + mem_section
                + (
                    f"\n本集标题：{ep_bit}\n"
                    "评论尽量点到本集具体感受，若记忆相关可自然带一句，不要复述记忆。"
                    if (mem or episode_title)
                    else ""
                )
            )
            from bilibot.services.token_usage import usage_context
            with usage_context(scene="bangumi_comment", account_id=getattr(self, "account_id", "") or ""):
                return await provider.generate(prompt, max_tokens=1200) or ""
        except Exception:
            return ""

    # ══════════════════════════════════════
    #  追番更新检测
    # ══════════════════════════════════════

    async def check_updates(self) -> dict:
        """检查已追番剧是否有更新，有则触发看番

        Returns:
            {"updated": n, "watched": n}
        """
        followed = await self.bili.get_followed_bangumi(follow_status=2)  # 在看
        if not followed:
            logger.info("没有在追的番")
            return {"updated": 0}

        to_watch = []

        for f in followed:
            sid = f.get("season_id", 0)
            watched_ids = self._get_watched_ep_ids(sid)
            if not watched_ids:
                # 追了但没看过
                to_watch.append(f)
                continue
            # 检查有没有新集
            detail = await self.bili.get_bangumi_detail(season_id=sid)
            if not detail or not detail.get("episodes"):
                continue
            new_eps = [
                ep
                for ep in detail["episodes"]
                if ep.get("ep_id") and str(ep["ep_id"]) not in watched_ids
            ]
            if new_eps:
                to_watch.append(f)
                logger.info(f"追番更新：《{f.get('title', '')}》有 {len(new_eps)} 集新内容")
            await asyncio.sleep(1)

        if not to_watch:
            logger.info("追番都是最新的，没有更新")
            return {"updated": 0}

        # 看一部更新的番
        target = random.choice(to_watch)
        logger.info(f"触发追番更新：《{target.get('title', '')}》")
        options = self._bangumi_options()
        try:
            max_episodes = max(1, min(10, int(options.get("max_episodes", 3))))
        except (TypeError, ValueError):
            max_episodes = 3
        result = await self.watch_bangumi(
            season_id=target["season_id"], max_episodes=max_episodes
        )
        return {"updated": len(to_watch), "watched": result.get("watched", 0)}
