"""Proactive video main loop extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Dict, Optional

from bilibot.models.proactive_video_context import ProactiveVideoContext
from bilibot.video_understanding.audio_track import ASRTranscriptionError

logger = logging.getLogger("bilibot.scheduler_proactive_video")


async def do_proactive_video_locked(self, task_id: Optional[str] = None):
    """主动看视频主体（调用方已持有同账号串行锁）。"""
    # PRD V4 COM-001：features 在方法内独立读取（与 _check_proactive_tasks 解耦）
    features = self.config_loader.get_raw_config().get("features", {})

    try:
        # 1. 获取视频（C: 推荐流 / D: 分区热门随机翻页，各 50% 概率）
        # 检查是否有中断时正在处理的 bvid（重启恢复场景）
        saved_bvid = ""
        if task_id:
            try:
                _task = self.task_store.get(task_id)
                if _task and _task.input_json:
                    _input = json.loads(_task.input_json)
                    saved_bvid = str(_input.get("bvid") or "").strip()
            except Exception:
                pass

        # 陪伴层：日程若在「刷 B 站/看视频」时段，略提高推荐流占比（更像「按心情刷」）
        companion = getattr(self, "companion", None)
        prefer_browse = bool(
            companion is not None
            and getattr(companion, "enabled", False)
            and hasattr(companion, "wants_browse_bilibili_now")
            and companion.wants_browse_bilibili_now()
        )
        if prefer_browse:
            source = "recommend" if random.random() < 0.72 else "region"
            logger.info("陪伴日程偏向刷站，视频来源权重偏向推荐流")
        else:
            source = random.choice(["recommend", "region"])
        if source == "recommend":
            try:
                data = await self.bili.get_recommend_videos()
            except Exception as exc:
                # Feed endpoints can fail independently or be absent on an
                # older API adapter. Keep the hot-feed fallback reachable.
                logger.warning(
                    "recommend feed unavailable, falling back to hot feed: %s",
                    type(exc).__name__,
                )
                data = None
            logger.info("视频来源: 推荐流")
        else:
            page = random.randint(1, 5)
            try:
                data = await self.bili.get_region_hot_videos(rid=0, page=page)
            except Exception as exc:
                logger.warning(
                    "region feed unavailable, falling back to hot feed: %s",
                    type(exc).__name__,
                )
                data = None
            logger.info(f"视频来源: 分区热门 (page={page})")
        if not data or not data.get("data"):
            # 推荐流/分区失败时回退到热门视频
            logger.warning("推荐流/分区热门获取失败，回退到热门视频")
            data = await self.bili.get_hot_videos()
        if not data or not data.get("data"):
            await self._archive_proactive_video_failure(
                title="推荐流",
                reason="NO_HOT_VIDEOS",
                task_id=task_id or "",
                extra={"feed_state": "unavailable"},
            )
            if task_id:
                self._fail_task(task_id, "NO_HOT_VIDEOS", "获取视频失败", retryable=False)
            return
        videos = data["data"].get("list", [])
        if not videos:
            await self._archive_proactive_video_failure(
                title="推荐流",
                reason="NO_HOT_VIDEOS",
                task_id=task_id or "",
                extra={"feed_state": "empty"},
            )
            if task_id:
                self._fail_task(task_id, "NO_HOT_VIDEOS", "视频列表为空", retryable=False)
            return

        # 仅「评价/互动闭环完成」才跳过：仅有 video_observation 不够
        # （归档在评价之前；中途失败必须允许重试补互动）。
        available_videos = []
        for candidate in videos:
            bvid_value = str(candidate.get("bvid") or "").strip()
            if not bvid_value:
                continue
            if await self._has_completed_proactive_video(bvid_value):
                continue
            cooldown_check = getattr(companion, "is_video_in_cooldown", None)
            if callable(cooldown_check) and cooldown_check(bvid_value):
                logger.info("跳过失败冷却中的视频: bvid=%s", bvid_value)
                continue
            available_videos.append(candidate)

        # 中断恢复：在「空列表提前 return」之前强制插入 saved_bvid
        # （否则 feed 为空/不全时永远轮不到中断视频）
        if saved_bvid:
            cooldown_check = getattr(companion, "is_video_in_cooldown", None)
            if callable(cooldown_check) and cooldown_check(saved_bvid):
                logger.info("中断视频处于失败冷却，不再强制优先: %s", saved_bvid)
                saved_bvid = ""
                if task_id:
                    try:
                        self.task_store.update_input(
                            task_id, {"bvid": "", "scene": "proactive_video"}
                        )
                    except Exception:
                        pass
        if saved_bvid:
            if await self._has_completed_proactive_video(saved_bvid):
                logger.info(f"中断视频 {saved_bvid} 已完成闭环，不再优先")
                saved_bvid = ""
            else:
                saved_idx = next(
                    (
                        i
                        for i, v in enumerate(available_videos)
                        if str(v.get("bvid") or "") == saved_bvid
                    ),
                    -1,
                )
                if saved_idx >= 0:
                    saved_video = available_videos.pop(saved_idx)
                    available_videos.insert(0, saved_video)
                    logger.info(f"恢复中断的视频: {saved_bvid}")
                else:
                    available_videos.insert(0, {"bvid": saved_bvid})
                    logger.info(
                        f"恢复中断的视频（不在当前 feed，强制优先）: {saved_bvid}"
                    )

        if not available_videos:
            logger.info("所有候选视频均已完成主动观看闭环，跳过主动看视频")
            valid_bvid_count = sum(
                1 for item in videos if str(item.get("bvid") or "").strip()
            )
            exhaustion_reason = (
                "ALL_WATCHED_OR_COOLDOWN" if valid_bvid_count else "NO_BVID"
            )
            try:
                await self._archive_proactive_video_failure(
                    title="这一轮推荐",
                    reason=exhaustion_reason,
                    task_id=task_id or "",
                    extra={
                        "candidate_count": len(videos),
                        "valid_bvid_count": valid_bvid_count,
                    },
                )
            except Exception:
                logger.warning("候选耗尽事件归档失败", exc_info=True)
            if task_id:
                self._fail_task(
                    task_id,
                    "ALL_WATCHED" if valid_bvid_count else "NO_BVID",
                    (
                        "所有候选视频均已完成主动观看闭环"
                        if valid_bvid_count
                        else "候选视频均缺少 BVID"
                    ),
                    retryable=False,
                )
            return

        # 随机打乱候选；再按陪伴兴趣/探索笔记软排序（更像「按兴趣点开」）
        if saved_bvid and available_videos and str(
            available_videos[0].get("bvid") or ""
        ) == saved_bvid:
            rest = available_videos[1:]
            random.shuffle(rest)
            if companion is not None and getattr(companion, "enabled", False) and hasattr(companion, "rank_video_candidates"):
                try:
                    rest = companion.rank_video_candidates(rest)
                except Exception as e:
                    logger.debug("companion rank videos failed: %s", e)
            available_videos[1:] = rest
        else:
            random.shuffle(available_videos)
            if companion is not None and getattr(companion, "enabled", False) and hasattr(companion, "rank_video_candidates"):
                try:
                    available_videos = companion.rank_video_candidates(available_videos)
                    logger.info("已按陪伴兴趣软排序视频候选")
                except Exception as e:
                    logger.debug("companion rank videos failed: %s", e)
        max_video_attempts = min(3, len(available_videos))
        last_skip_reason = ""

        for video_attempt, video in enumerate(available_videos[:max_video_attempts], start=1):
            bvid = video.get("bvid", "")
            if not bvid:
                last_skip_reason = "NO_BVID"
                continue

            # 持久化正在处理的 bvid，重启后可优先重试同一视频
            if task_id:
                try:
                    self.task_store.update_input(task_id, {"bvid": bvid, "scene": "proactive_video"})
                except Exception:
                    pass

            oid = await self.bili.get_video_oid_by_bvid(bvid)
            if not oid:
                last_skip_reason = "NO_OID"
                logger.warning(
                    "获取 oid 失败，换视频 attempt=%s/%s bvid=%s",
                    video_attempt,
                    max_video_attempts,
                    bvid,
                )
                await self._archive_proactive_video_failure(
                    bvid=bvid,
                    title=str(video.get("title") or "未知视频"),
                    reason=last_skip_reason,
                    task_id=task_id or "",
                )
                continue

            # 2. 获取视频详情
            video_info = await self.bili.get_video_info(oid)
            if not video_info:
                last_skip_reason = "NO_VIDEO_INFO"
                logger.warning(
                    "获取视频详情失败，换视频 attempt=%s/%s bvid=%s",
                    video_attempt,
                    max_video_attempts,
                    bvid,
                )
                await self._archive_proactive_video_failure(
                    bvid=bvid,
                    oid=str(oid),
                    title=str(video.get("title") or "未知视频"),
                    reason=last_skip_reason,
                    task_id=task_id or "",
                )
                continue

            title = self._clean_platform_text(
                video_info.get("title", "未知视频"), 160
            )
            owner = self._clean_platform_text(
                video_info.get("owner", {}).get("name", "未知UP"), 80
            )
            owner_mid = str(video_info.get("owner", {}).get("mid", ""))
            desc = self._clean_platform_text(video_info.get("desc", ""), 3000)

            # 获取标签；标签接口异常时复用已拿到的详情/热门分区元数据。
            tag_metadata = dict(video_info)
            for field in ("tags", "tag", "tname", "tname_v2", "tnamev2", "pid_name_v2"):
                if not tag_metadata.get(field) and video.get(field):
                    tag_metadata[field] = video[field]
            tags_list = await self.bili.get_video_tags(bvid, video_info=tag_metadata) or []
            if isinstance(tags_list, str):
                tags_list = [t.strip() for t in tags_list.split(",") if t.strip()]

            # 获取热门评论
            hot_comments = await self.bili.get_hot_comments(oid, limit=5) or []

            logger.info(
                f"正在看视频: 《{title}》 by {owner} "
                f"(candidate {video_attempt}/{max_video_attempts})"
            )
            started_feedback = getattr(companion, "on_proactive_video_started", None)
            if callable(started_feedback):
                try:
                    started_feedback(title=title, bvid=bvid)
                except Exception:
                    logger.debug("companion video start feedback failed", exc_info=True)

            # PRD-V5 VID-502：结构化视频上下文 — 各来源独立赋值，不互相覆盖
            # 修复 scheduler.py 旧实现复用 video_content 字符串导致搜索结果被视听分析覆盖的 bug
            ctx = ProactiveVideoContext(bvid=bvid)
            ctx.metadata = video_info
            ctx.hot_comments = hot_comments if hot_comments else None
            video_file_to_cleanup = None
            work_dir_to_cleanup = None
            artifact_cleanup_ready = False
            # 必须按 bvid/oid 做幂等键，不能用 task_id：
            # 任务重试时可能换片；若 key 绑 task_id，新片会与旧归档冲突，
            # 再被 treat_idempotent_as_ready 误当成「已就绪」继续评价错误视频。
            observation_key = f"{bvid}:{oid}"
            video_content = ""  # 评价/评论输入；有 digest 后优先用 digest
            video_detail = ""
            skip_this_video = False
            web_event_id = ""

            try:
                # 来源 1：联网搜索（UNTRUSTED Reference Block）— 失败不清空其他来源
                if self.web_search and self.web_search.is_available():
                    try:
                        search_query = await self.web_search.should_search_for_video(
                            video_info={
                                "title": title,
                                "desc": desc,
                                "tname": tags_list[0] if tags_list else "",
                                "owner_name": owner,
                            },
                            scene="proactive_video",
                        )
                        if search_query:
                            # PRD-V5 VID-502：存储结构化搜索结果，由 to_prompt_sections() 统一格式化
                            search_result = await self.web_search.search(
                                search_query, scene="proactive_video",
                            )
                            if search_result:
                                ctx.search_reference = search_result
                                try:
                                    web_event_id = await self._archive_video_web_reference(
                                        bvid=bvid,
                                        oid=str(oid),
                                        title=title,
                                        query=search_query,
                                        result=search_result,
                                    )
                                except Exception:
                                    logger.warning(
                                        "视频联网参考归档失败 bvid=%s", bvid,
                                        exc_info=True,
                                    )
                                    raise
                    except Exception as e:
                        ctx.degradation_reasons.append(f"search_failed: {e}")

                # 来源 2：视频内容理解（视听双轨分析）
                # 若已有完整观看归档（上次评价前失败），复用 digest，避免重复下载。
                from bilibot.memory_brain.ingestion import video_observation

                existing_detail = ""
                if not skip_this_video:
                    existing_detail = await self._load_existing_video_detail(bvid)
                if existing_detail:
                    video_detail = existing_detail
                    video_content = self._compose_video_content_for_prompt(
                        ctx, video_detail=video_detail,
                    )
                    logger.info(
                        "复用已归档视频详细内容，跳过下载/理解: bvid=%s detail=%s字",
                        bvid,
                        len(video_detail),
                    )
                    # 幂等就绪：不重复写入也可继续评价。
                    try:
                        await self._archive_required(
                            video_observation(
                                account_id=self.account_id or "default",
                                observation_key=observation_key,
                                bvid=bvid,
                                oid=str(oid),
                                title=title,
                                owner=owner,
                                context=ctx.to_dict(),
                                tags=tags_list,
                                persona_id=self._get_current_persona_id(),
                                video_detail=video_detail,
                                pseudonymize_actor=self._pseudonymize_actor_id,
                            ),
                            treat_idempotent_as_ready=True,
                        )
                        artifact_cleanup_ready = True
                    except Exception as archive_exc:
                        from bilibot.memory_brain.models import (
                            IdempotencyConflictError,
                            ReingestBlockedError,
                        )
                        if isinstance(
                            archive_exc,
                            (IdempotencyConflictError, ReingestBlockedError),
                        ):
                            # 已有完整观看：继续评价。
                            logger.info(
                                "复用路径归档冲突，按已就绪继续 bvid=%s", bvid
                            )
                        else:
                            raise
                elif self.video_understanding and self.video_understanding.is_available():
                    try:
                        precheck_reason = self._video_download_precheck(video_info)
                        if precheck_reason:
                            raise RuntimeError(f"DOWNLOAD_PRE_CHECK:{precheck_reason}")
                        cid = video_info.get("cid", 0)
                        if not cid:
                            pages = video_info.get("pages", [])
                            if pages:
                                cid = pages[0].get("cid", 0)
                        if not cid or not bvid:
                            raise RuntimeError("视频缺少可用于完整提取的 CID/BVID")

                        import os as _os
                        video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
                        save_path = _os.path.join(video_temp_dir, f"{bvid}")
                        max_bytes, timeout = self._video_download_bounds()
                        video_file = await self.bili.download_video(
                            bvid,
                            cid,
                            save_path,
                            quality=32,
                            max_bytes=max_bytes,
                            timeout=timeout,
                        )
                        if not video_file or not _os.path.exists(video_file):
                            raise RuntimeError(f"视频下载失败: {bvid}")

                        logger.info(f"视频已下载，开始视听分析: {video_file}")
                        video_file_to_cleanup = video_file
                        throttle = getattr(
                            getattr(self, "memory_brain", None),
                            "set_enrichment_throttled",
                            None,
                        )
                        if callable(throttle):
                            throttle(True, reason="video_understanding")
                        try:
                            vu_result = await self.video_understanding.understand(
                                video_file,
                                defer_cleanup=True,
                                require_complete_audio=True,
                                require_complete_visual=True,
                            )
                        finally:
                            if callable(throttle):
                                throttle(False, reason="video_understanding")
                        if not isinstance(vu_result, dict):
                            raise RuntimeError("视频理解返回了无效结果")

                        work_dir_to_cleanup = vu_result.get("work_dir") or None
                        degradation = str(vu_result.get("degradation_reason") or "")
                        audio_status = vu_result.get("audio_status") or {}
                        # PRD V6：区分"降级"与"真失败"。
                        # - degradation 非空（如 duration_exceeds_limit）是预期降级，
                        #   应记录原因并继续走元数据归档，而不是抛错。
                        # - 音轨 ASR 真正失败 → 换片（不整任务 abort）。
                        audio_failed = (
                            isinstance(audio_status, dict)
                            and audio_status.get("status") == "failed"
                        )
                        if audio_failed:
                            reason = str(
                                audio_status.get("error_code") or "audio_track_failed"
                            )
                            raise RuntimeError(f"视频提取未完成: {reason}")

                        if degradation:
                            ctx.degradation_reasons.append(
                                f"video_understanding: {degradation}"
                            )
                            logger.info(f"视频理解降级，继续元数据归档: {degradation}")

                        if degradation:
                            # Complete audio/visual evidence was explicitly
                            # requested above. A degraded extraction may be
                            # kept for diagnostics, but cannot become a
                            # completed watch memory or authorize actions.
                            last_skip_reason = (
                                f"DEGRADED_EXTRACTION:{degradation}"
                            )
                            skip_this_video = True

                        ctx.audiovisual = vu_result
                        av_log = ctx.audiovisual_log
                        if av_log:
                            logger.info(f"视频理解完成，行为日志 {len(av_log)} 字")
                        elif not degradation:
                            logger.warning("视频理解未生成行为日志")
                    except ASRTranscriptionError as e:
                        work_dir_to_cleanup = (
                            getattr(e, "work_dir", None) or work_dir_to_cleanup
                        )
                        last_skip_reason = f"ASR_FAILED:{e.code}"
                        skip_this_video = True
                        logger.warning(
                            "视频 ASR 失败，换视频 attempt=%s/%s bvid=%s code=%s",
                            video_attempt,
                            max_video_attempts,
                            bvid,
                            e.code,
                        )
                    except Exception as e:
                        work_dir_to_cleanup = (
                            getattr(e, "work_dir", None) or work_dir_to_cleanup
                        )
                        err_text = str(e)
                        if err_text.startswith("DOWNLOAD_PRE_CHECK:"):
                            reason = err_text.removeprefix("DOWNLOAD_PRE_CHECK:")
                            ctx.degradation_reasons.append(reason)
                            last_skip_reason = f"DEGRADED_EXTRACTION:{reason}"
                            logger.warning(
                                "视频下载前资源检查未通过，换视频 attempt=%s/%s bvid=%s reason=%s",
                                video_attempt,
                                max_video_attempts,
                                bvid,
                                reason,
                            )
                        else:
                            last_skip_reason = f"UNDERSTAND_FAILED:{type(e).__name__}"
                            logger.warning(
                                "视频提取/理解失败，换视频 attempt=%s/%s bvid=%s err=%s",
                                video_attempt,
                                max_video_attempts,
                                bvid,
                                type(e).__name__,
                            )
                        skip_this_video = True

                    # Full extracted source archive is the commit boundary. No evaluation
                    # or interaction happens before this succeeds.

                    # 先把长视听 log 压成 ≤2000 字详细内容：
                    # 1) 写入记忆，供日后召回
                    # 2) 作为评价/主动评论的主输入
                    # LLM 摘要失败会内部重试；仍失败则换下一个视频，不接受低质量截断。
                    if not skip_this_video:
                        if ctx.audiovisual_log:
                            video_detail = await self._build_video_detail_digest(
                                title=title,
                                owner=owner,
                                behavior_log=ctx.audiovisual_log or "",
                                max_attempts=2,
                                require_llm=True,
                            )
                            if not video_detail:
                                last_skip_reason = "VIDEO_DETAIL_FAILED"
                                skip_this_video = True
                                logger.warning(
                                    "视频详细内容摘要失败，换视频 attempt=%s/%s bvid=%s title=%s",
                                    video_attempt,
                                    max_video_attempts,
                                    bvid,
                                    title[:40],
                                )
                            else:
                                video_content = self._compose_video_content_for_prompt(
                                    ctx, video_detail=video_detail,
                                )
                        else:
                            # 无 behavior_log：禁止元数据盲评（与「必须视听细节」一致）。
                            last_skip_reason = "NO_AUDIOVISUAL_LOG"
                            skip_this_video = True
                            logger.warning(
                                "视频理解无行为日志，换视频 attempt=%s/%s bvid=%s",
                                video_attempt,
                                max_video_attempts,
                                bvid,
                            )

                    if not skip_this_video:
                        try:
                            await self._archive_required(
                                video_observation(
                                    account_id=self.account_id or "default",
                                    observation_key=observation_key,
                                    bvid=bvid,
                                    oid=str(oid),
                                    title=title,
                                    owner=owner,
                                    context=ctx.to_dict(),
                                    tags=tags_list,
                                    persona_id=self._get_current_persona_id(),
                                    video_detail=video_detail,
                                    pseudonymize_actor=self._pseudonymize_actor_id,
                                ),
                                # 同 bvid 重试时 digest 可能非确定性微变 → content_hash 冲突。
                                # 仅在「同 key 且已是完整观看」时视为就绪；否则换片。
                                treat_idempotent_as_ready=True,
                            )
                            artifact_cleanup_ready = True
                        except Exception as archive_exc:
                            from bilibot.memory_brain.models import (
                                IdempotencyConflictError,
                                ReingestBlockedError,
                            )
                            if isinstance(archive_exc, IdempotencyConflictError):
                                # 同 key 不同内容，且未能当作完整观看就绪：换片避免串内容。
                                last_skip_reason = "ARCHIVE_IDEMPOTENCY_CONFLICT"
                                skip_this_video = True
                                logger.warning(
                                    "视频归档幂等冲突，换视频 attempt=%s/%s bvid=%s",
                                    video_attempt,
                                    max_video_attempts,
                                    bvid,
                                )
                            elif isinstance(archive_exc, ReingestBlockedError):
                                last_skip_reason = "ARCHIVE_BLOCKED"
                                skip_this_video = True
                                logger.warning(
                                    "视频归档被 tombstone 阻断，换视频 bvid=%s", bvid,
                                )
                            else:
                                raise
                else:
                    # 无视频理解服务且无已归档 digest：禁止元数据盲评。
                    # 否则模型只能复读标题，互动质量差且可能编造细节。
                    if not skip_this_video:
                        last_skip_reason = "NO_VIDEO_UNDERSTANDING"
                        skip_this_video = True
                        logger.warning(
                            "视频理解不可用且无已归档详细内容，跳过候选 "
                            "attempt=%s/%s bvid=%s",
                            video_attempt,
                            max_video_attempts,
                            bvid,
                        )
            finally:
                from bilibot.video_understanding.cleanup import (
                    cleanup_media_artifacts,
                    schedule_cleanup,
                )

                if artifact_cleanup_ready:
                    cleanup_media_artifacts(
                        video_file_to_cleanup, work_dir_to_cleanup
                    )
                else:
                    retained = [
                        p
                        for p in (video_file_to_cleanup, work_dir_to_cleanup)
                        if p
                    ]
                    if retained:
                        # 失败证据保留 30 分钟用于诊断/人工补归档；定时清理
                        # 防止多候选失败把 video_temp 永久撑满。
                        schedule_cleanup(retained, delay_seconds=1800)
                        logger.warning(
                            "主动视频证据尚未归档，保留 1800 秒: bvid=%s paths=%s",
                            bvid,
                            len(retained),
                        )

            if skip_this_video:
                failure_event_id = await self._archive_proactive_video_failure(
                    bvid=bvid,
                    oid=str(oid),
                    title=title,
                    owner=owner,
                    reason=last_skip_reason or "VIDEO_SKIPPED",
                    task_id=task_id or "",
                    tags=list(tags_list or []),
                    extra={
                        "degradation_reasons": list(
                            getattr(ctx, "degradation_reasons", None) or []
                        )[:12],
                        "web_reference_event_id": str(
                            locals().get("web_event_id") or ""
                        ),
                    },
                    partial_evidence=str(
                        getattr(ctx, "audiovisual_log", "") or ""
                    ),
                )
                continue

            # 成功选定并归档本视频；跳出候选循环，继续评价/互动。
            break
        else:
            # 所有候选都失败/跳过
            logger.warning(
                "主动看视频：%s 个候选均失败，最后原因=%s",
                max_video_attempts,
                last_skip_reason or "unknown",
            )
            if task_id:
                # 下载/ASR/摘要失败通常可重试；NO_BVID 等结构性问题不重试。
                retryable_prefixes = (
                    "VIDEO_DETAIL_FAILED",
                    "ASR_FAILED",
                    "UNDERSTAND_FAILED",
                    "DEGRADED_EXTRACTION",
                    "DOWNLOAD_FAILED",
                    "NO_OID",
                    "NO_VIDEO_INFO",
                    "ARCHIVE_IDEMPOTENCY_CONFLICT",
                    "ARCHIVE_BLOCKED",
                    "NO_VIDEO_UNDERSTANDING",
                    "NO_AUDIOVISUAL_LOG",
                    "EVALUATION_FAILED",
                )
                reason = last_skip_reason or "NO_USABLE_VIDEO"
                retryable = any(
                    reason == p or reason.startswith(p + ":")
                    for p in retryable_prefixes
                )
                self._fail_task(
                    task_id,
                    reason.split(":", 1)[0] if ":" in reason else reason,
                    f"候选视频均不可用（{reason}）",
                    retryable=retryable,
                )
            return

        # 3. LLM 评价视频（输入优先为 video_detail 摘要）
        # 评价失败不得写 bot_experience / succeed：否则去重会永久跳过该片。
        evaluation = None
        companion_ctx = ""
        companion = getattr(self, "companion", None)
        if companion is not None and getattr(companion, "enabled", False):
            try:
                if hasattr(companion, "build_proactive_context_block"):
                    companion_ctx = companion.build_proactive_context_block() or ""
            except Exception:
                companion_ctx = ""

        # C4：先 begin_activity 再写 mid-watch impression，避免 intent 覆盖初印象。
        eval_action_key = f"proactive_video:{observation_key}:evaluate"
        brain_wm = getattr(self, "memory_brain", None)

        # 账号级混合召回：近期视频/番剧/日记/评论，注入评价与后续主动评论
        activity_context = await self._begin_activity_context(
            action_key=eval_action_key,
            action_type="evaluate_proactive_video",
            current_activity=(
                "正在观看并评价一条视频，结合最近经历决定真实感受、是否互动以及主动评论该说什么。"
            ),
            query=" ".join(
                item
                for item in (
                    str(title or ""),
                    str(owner or ""),
                    " ".join(str(tag) for tag in (tags_list or [])[:8]),
                    str(desc or "")[:500],
                )
                if item
            ),
            scene="proactive_video",
            title=title,
            bvid=bvid,
            oid=str(oid),
            metadata={"bvid": bvid, "oid": str(oid)},
        )
        # Mid-watch working_memory: impression phase after intent is open.
        if brain_wm is not None and callable(
            getattr(brain_wm, "update_working_memory", None)
        ):
            try:
                first_impression = (
                    f"watch_phase=impression title={str(title or '')[:40]} "
                    f"owner={str(owner or '')[:20]}"
                )
                brain_wm.update_working_memory(
                    eval_action_key,
                    phase="watch_phase_impression",
                    belief=first_impression,
                    notes={
                        "bvid": str(bvid or ""),
                        "watch_phase": "impression",
                        "belief_update": True,
                    },
                )
            except Exception:
                logger.debug("mid_watch working_memory seed failed", exc_info=True)
        if activity_context is not None:
            memory_bundle = {
                "memory_evidence": str(activity_context.prompt_text or ""),
                "memory_event_ids": list(activity_context.event_ids or ()),
            }
        else:
            memory_bundle = await self._recall_for_proactive_video(
                title=title,
                owner=owner,
                tags=tags_list,
                bvid=bvid,
                oid=str(oid),
                desc=desc,
            )
        memory_evidence = str(memory_bundle.get("memory_evidence") or "")
        memory_event_ids = list(memory_bundle.get("memory_event_ids") or [])
        try:
            # 挂到 ctx 供 audit/debug；to_dict 仍不含记忆，避免污染 video_observation
            if "ctx" in locals() and ctx is not None:
                ctx.memory_evidence = memory_evidence
                ctx.memory_event_ids = memory_event_ids
                ctx.companion_context = companion_ctx
                # compose 常在召回之前完成（digest 路径），评价前再拼一次避免「只记不用」
                if video_detail:
                    video_content = self._compose_video_content_for_prompt(
                        ctx, video_detail=video_detail,
                    )
                elif (memory_evidence or companion_ctx) and "【相关记忆" not in (
                    str(video_content or "")
                ):
                    extras: list[str] = []
                    base = str(video_content or "").strip()
                    if base:
                        extras.append(base)
                    if memory_evidence:
                        extras.append(
                            "【相关记忆/近期经历】\n" + memory_evidence[:1800]
                        )
                    if companion_ctx:
                        extras.append(
                            "【你今天的状态与念头】\n" + str(companion_ctx).strip()[:500]
                        )
                    if extras:
                        video_content = "\n\n".join(extras)
        except Exception:
            pass

        if self.comment_generator:
            try:
                evaluation = await self.comment_generator.evaluate_video(
                    title=title,
                    owner=owner,
                    desc=desc,
                    tags=tags_list,
                    hot_comments=hot_comments,
                    video_content=video_content,
                    companion_context=companion_ctx,
                    memory_evidence=memory_evidence,
                )
            except TypeError:
                try:
                    evaluation = await self.comment_generator.evaluate_video(
                        title=title,
                        owner=owner,
                        desc=desc,
                        tags=tags_list,
                        hot_comments=hot_comments,
                        video_content=video_content,
                        companion_context=companion_ctx,
                    )
                except TypeError:
                    evaluation = await self.comment_generator.evaluate_video(
                        title=title,
                        owner=owner,
                        desc=desc,
                        tags=tags_list,
                        hot_comments=hot_comments,
                        video_content=video_content,
                    )
            except Exception as e:
                logger.warning(f"视频评价失败: {e}")

        llm_ok = isinstance(evaluation, dict)
        if not llm_ok:
            logger.warning(
                "LLM 评价失败，不写 experience、不标记任务成功 bvid=%s",
                bvid,
            )
            failed_result = await self._archive_bot_action(
                action_key=f"proactive_video:{observation_key}:evaluate",
                action_type="evaluate_proactive_video",
                text=f"评价视频《{title}》失败，等待后续重试",
                published=False,
                status="failed",
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "reason_code": "EVALUATION_FAILED",
                },
            )
            failed_event_id = str(getattr(failed_result, "event_id", "") or "")
            failure_feedback = getattr(
                getattr(self, "companion", None),
                "on_proactive_video_failed",
                None,
            )
            if callable(failure_feedback):
                failure_feedback(
                    title=title,
                    bvid=bvid,
                    reason="EVALUATION_FAILED",
                    memory_event_id=failed_event_id,
                )
            if task_id:
                self._fail_task(
                    task_id,
                    "EVALUATION_FAILED",
                    f"视频评价失败，保留归档以便重试 bvid={bvid}",
                    retryable=True,
                )
            return

        score = evaluation.get("score", 0)
        # 严格校验 score 类型
        if not isinstance(score, (int, float)):
            try:
                score = float(score)
            except Exception:
                score = 0
        score = max(0, min(10, score))
        mood = evaluation.get("mood", "平静")
        review = evaluation.get("review", "")
        logger.info(f"视频评价: score={score}, mood={mood}, llm_ok={llm_ok}")
        # belief_update after full watch evaluation (may replan vs impression).
        if brain_wm is not None and callable(
            getattr(brain_wm, "update_working_memory", None)
        ):
            try:
                belief = (
                    f"watch_phase=evaluated score={score} mood={mood} "
                    f"review={(review or '')[:80]}"
                )
                replan = score < 4  # low score → reconsider interaction impulse
                brain_wm.update_working_memory(
                    eval_action_key,
                    phase="watch_phase_evaluated",
                    belief=belief,
                    draft=str(evaluation.get("comment") or "")[:200],
                    notes={
                        "score": score,
                        "mood": mood,
                        "belief_update": True,
                    },
                    replan=replan,
                )
                if replan and callable(getattr(brain_wm, "mid_action_replan", None)):
                    brain_wm.mid_action_replan(
                        eval_action_key,
                        reason=f"low_score_{score}",
                        new_belief=belief,
                    )
            except Exception:
                logger.debug("mid_watch belief_update failed", exc_info=True)
        await self._archive_bot_action(
            action_key=eval_action_key,
            action_type="evaluate_proactive_video",
            text=(
                f"已看完并评价视频《{title}》：评分 {score}/10，心情 {mood}。"
                + (f" 感想：{review}" if review else "")
            ),
            published=True,
            title=title,
            scene="proactive_video",
            metadata={"bvid": bvid, "oid": str(oid)},
        )

        # PRD V4 VID-003：区分 inspected / watched 语义
        # inspected：读取元数据或下载分析，但未向 B站上报观看
        # watched：按照平台允许的接口上报观看进度并得到成功响应（默认关闭）
        watch_state = "inspected"
        watched_flag = False

        # 4. PRD V4 VID-006：互动决策由确定性 PolicyEngine 执行
        # 模型只输出建议，最终决策受开关、日预算、评分阈值和去重状态控制
        decisions = await self.interaction_policy.evaluate_async(
            llm_suggestion=evaluation,
            score=score,
            bvid=bvid,
            oid=str(oid),
        )
        action_outcomes: Dict[str, str] = {}

        # 执行点赞 / 投币 / 收藏（C5 三态 + H3 begin fail-closed + H4 fav begin）
        async def _run_interaction_action(
            *,
            action_name: str,
            action_type: str,
            action_key: str,
            current_activity: str,
            api_coro,
            success_text: str,
            fail_text: str,
            risk_tag: str,
            on_success=None,
        ) -> None:
            """begin → API(Optional[bool]) → archive。begin 失败不打平台 API。"""
            try:
                await self._begin_activity_context(
                    action_key=action_key,
                    action_type=action_type,
                    current_activity=current_activity,
                    query=str(title or ""),
                    scene="proactive_video",
                    title=title,
                    bvid=bvid,
                    oid=str(oid),
                    metadata={"bvid": bvid, "oid": str(oid)},
                )
            except Exception:
                logger.warning(
                    "%s begin_activity failed; skip API to preserve memory fail-closed",
                    action_name,
                    exc_info=True,
                )
                action_outcomes[action_name] = "skipped:begin_failed"
                return
            try:
                ok = await api_coro()
            except Exception as e:
                action_outcomes[action_name] = f"error:{type(e).__name__}"
                logger.debug("%s 失败: %s", action_name, e)
                await self.interaction_policy.record_result_async(
                    action_name, bvid, str(oid), "failed", failure_reason=str(e)
                )
                await self._archive_bot_action(
                    action_key=action_key,
                    action_type=action_type,
                    text=fail_text,
                    published=False,
                    status="failed",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": type(e).__name__,
                    },
                )
                return
            # C5：None = transport 不确定，禁止当 failed 自动重试 / 不占「可再试」语义
            if ok is None:
                action_outcomes[action_name] = "result_unknown"
                await self.interaction_policy.record_result_async(
                    action_name,
                    bvid,
                    str(oid),
                    "result_unknown",
                    api_code=getattr(self.bili, "last_api_code", None),
                    failure_reason="transport_uncertain",
                )
                await self._archive_bot_action(
                    action_key=action_key,
                    action_type=action_type,
                    text=f"{fail_text}（结果不确定，不自动重试）",
                    published=False,
                    status="result_unknown",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "TRANSPORT_UNCERTAIN",
                    },
                )
                return
            if ok:
                action_outcomes[action_name] = "success"
                await self.interaction_policy.record_result_async(
                    action_name,
                    bvid,
                    str(oid),
                    "success",
                    api_code=getattr(self.bili, "last_api_code", None),
                )
                logger.info("视频%s成功", action_name)
                await self._archive_bot_action(
                    action_key=action_key,
                    action_type=action_type,
                    text=success_text,
                    published=True,
                    title=title,
                    scene="proactive_video",
                    metadata={"bvid": bvid, "oid": str(oid)},
                )
                if callable(on_success):
                    try:
                        on_success()
                    except Exception:
                        logger.debug(
                            "%s on_success hook failed", action_name, exc_info=True
                        )
            else:
                action_outcomes[action_name] = "failed"
                await self.interaction_policy.record_result_async(
                    action_name,
                    bvid,
                    str(oid),
                    "failed",
                    api_code=getattr(self.bili, "last_api_code", None),
                    failure_reason="bili_api_false",
                )
                self._check_bili_risk_control(risk_tag)
                await self._archive_bot_action(
                    action_key=action_key,
                    action_type=action_type,
                    text=fail_text,
                    published=False,
                    status="failed",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "BILI_API_FALSE",
                    },
                )

        like_result = decisions.get("like", {})
        if like_result.get("planned"):
            like_key = f"video:{observation_key}:like"

            def _like_salient():
                companion = getattr(self, "companion", None)
                if companion is not None and getattr(companion, "enabled", False):
                    push = getattr(companion, "_push_salient_self", None)
                    if callable(push):
                        push(line=f"给《{(title or '')[:40]}》点了赞")

            await _run_interaction_action(
                action_name="like",
                action_type="like_video",
                action_key=like_key,
                current_activity=f"准备给视频《{title}》点赞。",
                api_coro=lambda: self.bili.like_video(oid),
                success_text=f"观看了视频《{title}》并点了赞。",
                fail_text=f"点赞视频《{title}》失败",
                risk_tag="proactive_like",
                on_success=_like_salient,
            )
        else:
            logger.info(
                "点赞未执行: reason=%s used=%s max=%s",
                like_result.get("reason"),
                like_result.get("used", "-"),
                (like_result.get("cfg") or {}).get("max_per_day", "-"),
            )

        coin_result = decisions.get("coin", {})
        if coin_result.get("planned"):
            coin_key = f"video:{observation_key}:coin"
            await _run_interaction_action(
                action_name="coin",
                action_type="coin_video",
                action_key=coin_key,
                current_activity=f"准备给视频《{title}》投币。",
                api_coro=lambda: self.bili.coin_video(oid, num=1),
                success_text=f"给视频《{title}》投了币。",
                fail_text=f"给视频《{title}》投币失败",
                risk_tag="proactive_coin",
            )
        else:
            logger.info(
                "投币未执行: reason=%s used=%s max=%s",
                coin_result.get("reason"),
                coin_result.get("used", "-"),
                (coin_result.get("cfg") or {}).get("max_per_day", "-"),
            )

        fav_result = decisions.get("favorite", {})
        if fav_result.get("planned"):
            fav_key = f"video:{observation_key}:favorite"
            await _run_interaction_action(
                action_name="favorite",
                action_type="favorite_video",
                action_key=fav_key,
                current_activity=f"准备收藏视频《{title}》。",
                api_coro=lambda: self.bili.fav_video(oid),
                success_text=f"收藏了视频《{title}》。",
                fail_text=f"收藏视频《{title}》失败",
                risk_tag="proactive_fav",
            )
        else:
            logger.info(
                "收藏未执行: reason=%s used=%s max=%s",
                fav_result.get("reason"),
                fav_result.get("used", "-"),
                (fav_result.get("cfg") or {}).get("max_per_day", "-"),
            )

        # 5. 生成评论并发表（PRD V4 COM-001/COM-002/COM-003 / PRD-V5 §10.2 COM-501）
        comment_text = ""
        # COM-001：proactive_comment 开关独立控制是否允许发布主动评论
        proactive_comment_enabled = features.get("proactive_comment", True)
        comment_decision = decisions.get("comment", {})
        if (proactive_comment_enabled
                and comment_decision.get("planned")
                and self.comment_generator):
            # PRD-V5 §10.2 COM-501：原子 claim — 同账号同视频只能一个 worker 进入流程
            comment_text = await self._do_proactive_comment_publish(
                bvid=bvid,
                oid=oid,
                title=title,
                owner=owner,
                desc=desc,
                tags_list=tags_list,
                review=review,
                mood=mood,
                video_content=video_content,
                evaluation=evaluation,
                llm_ok=llm_ok,
                task_id=task_id or "",
                memory_evidence=memory_evidence if "memory_evidence" in locals() else "",
                companion_context=companion_ctx if "companion_ctx" in locals() else "",
                memory_event_ids=memory_event_ids if "memory_event_ids" in locals() else None,
            )
        elif not proactive_comment_enabled:
            logger.debug("主动评论开关关闭（proactive_comment=false），跳过评论发布")
        elif not comment_decision.get("planned"):
            logger.info(
                "主动评论策略未批准: reason=%s used=%s max=%s",
                comment_decision.get("reason"),
                comment_decision.get("used", "-"),
                (comment_decision.get("cfg") or {}).get("max_per_day", "-"),
            )

        # 6. Archive the complete evaluation and real outcomes. Source text is
        # never truncated; raw audiovisual observations are already in the
        # preceding video_observation event.
        if comment_decision.get("planned"):
            action_outcomes["comment"] = "success" if comment_text else "failed_or_skipped"
        interaction_summary = await self.interaction_policy.get_today_summary_async()
        # Distinct from video_observation (raw AV archive): this is post-watch evaluation.
        experience_lines = [
            f"观看并评价了视频《{title}》，UP主 {owner}",
            f"评分: {score}",
            f"心情: {mood}",
        ]
        if review:
            experience_lines.append(f"评价: {review}")
        if comment_text:
            experience_lines.append(f"实际发布评论: {comment_text}")
        experience_lines.append(
            "评价结构化结果: " + json.dumps(evaluation, ensure_ascii=False, default=str)
        )
        from bilibot.memory_brain.ingestion import text_observation

        prior_failure_event_ids = await self._recent_failed_video_event_ids(bvid)

        # 同片重试时 score/comment/outcomes 可能不同 → content_hash 冲突。
        # 已有 experience 视为闭环完成，不得因此暂停账号。
        experience_result = await self._archive_required(
            text_observation(
                account_id=self.account_id or "default",
                idempotency_key=observation_key,
                source_type="video_experience",
                event_type="bot_experience",
                text="\n".join(experience_lines),
                title=title,
                persona_id=self._get_current_persona_id(),
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "owner": owner,
                    "watch_state": watch_state,
                    "watched": watched_flag,
                    "action_outcomes": action_outcomes,
                    "interaction_budget": interaction_summary,
                    "memory_event_ids": list(
                        memory_event_ids if "memory_event_ids" in locals() else []
                    )[:20],
                    "memory_grounded": bool(
                        (memory_evidence if "memory_evidence" in locals() else "")
                        or (companion_ctx if "companion_ctx" in locals() else "")
                    ),
                    "supersedes_event_ids": prior_failure_event_ids,
                    "web_reference_event_id": str(web_event_id or ""),
                },
                importance=max(0.1, min(1.0, score / 10.0)),
            ),
            treat_idempotent_as_ready=True,
        )
        experience_event_id = str(
            getattr(experience_result, "event_id", "") or ""
        )
        brain_store = getattr(getattr(self, "memory_brain", None), "store", None)
        if experience_event_id and prior_failure_event_ids and brain_store is not None:
            try:
                await asyncio.to_thread(
                    brain_store.upsert_links,
                    experience_event_id,
                    [
                        {
                            "target_event_id": failed_id,
                            "relation_type": "corrects",
                            "weight": 0.95,
                            "evidence_ids": [experience_event_id, failed_id],
                        }
                        for failed_id in prior_failure_event_ids
                    ],
                )
            except Exception:
                logger.debug("failed→success correlation link skipped", exc_info=True)

        # 更新情绪
        if self.behavior_sim:
            try:
                self.behavior_sim.update_mood("watched_video")
            except Exception:
                pass

        # 陪伴生活层回写：精力/心情/当前活动/念头（让「看过视频」进入当天生活）
        companion = getattr(self, "companion", None)
        if companion is not None and getattr(companion, "enabled", False):
            try:
                if hasattr(companion, "on_proactive_video_finished"):
                    try:
                        companion.on_proactive_video_finished(
                            title=title or "",
                            score=score,
                            mood=str(mood or ""),
                            review=str(review or ""),
                            comment=str(comment_text or ""),
                            bvid=str(bvid or ""),
                            oid=str(oid or ""),
                            memory_event_ids=list(
                                memory_event_ids
                                if "memory_event_ids" in locals()
                                else []
                            ),
                        )
                    except TypeError:
                        companion.on_proactive_video_finished(
                            title=title or "",
                            score=score,
                            mood=str(mood or ""),
                            review=str(review or ""),
                            comment=str(comment_text or ""),
                            bvid=str(bvid or ""),
                        )
            except Exception as e:
                logger.debug("companion video feedback failed: %s", e)

        logger.info(
            "视频处理完成: 《%s》 score=%s comment=%s memory_events=%s",
            title,
            score,
            "是" if comment_text else "否",
            len(memory_event_ids) if "memory_event_ids" in locals() else 0,
        )

        # PRD-V5 §7：只有真正完成才 succeed（创建协程 ≠ 成功）
        if task_id:
            self._succeed_task(task_id, {
                "success": True,
                "summary": f"视频《{title}》处理完成 score={score}",
                "bvid": bvid,
                "title": title,
                "memory_event_ids": list(
                    memory_event_ids if "memory_event_ids" in locals() else []
                )[:12],
                "memory_grounded": bool(
                    memory_evidence if "memory_evidence" in locals() else ""
                ),
            })

    except Exception as e:
        logger.error(f"主动看视频失败: {e}", exc_info=True)
        # PRD-V5 §7：失败 → retry_wait/failed
        if task_id:
            self._fail_task(task_id, "PROACTIVE_VIDEO_ERROR", str(e), retryable=True)
