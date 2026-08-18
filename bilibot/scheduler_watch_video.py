"""_watch_video_for_reply extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import logging

from bilibot.models.proactive_video_context import ProactiveVideoContext
from bilibot.video_understanding.audio_track import ASRTranscriptionError

logger = logging.getLogger("bilibot.scheduler_watch_video")


async def watch_video_for_reply(self, bvid: str, oid) -> bool:
    """为 @我 的视频评论预先观看并归档视频

    被用户 @ 后，回复前先完整看一遍视频（下载+视听分析+归档到 memory_brain），
    这样后续 comment_context_service.build_context 能从 V6 缓存命中完整视频上下文，
    让回复基于真实观看内容而非仅元数据。

    bvid 可为空，此时从 video_info.bvid 补齐。

    Returns:
        True 表示已观看并归档成功（或已存在缓存），False 表示失败（调用方降级为元数据回复）
    """
    if not self.bili:
        return False
    # 视频理解服务不可用时直接降级
    if not (self.video_understanding and self.video_understanding.is_available()):
        logger.info("@回复：视频理解服务不可用，跳过预观看，降级为元数据回复")
        return False

    try:
        oid_int = int(oid) if oid else 0
        if not oid_int and bvid:
            oid_int = await self.bili.get_video_oid_by_bvid(bvid)
        if not oid_int:
            logger.warning(f"@回复：获取 oid 失败 bvid={bvid}")
            return False

        # 检查 memory_brain 是否已有完整视频观察缓存（含视听分析，非仅元数据）
        brain = getattr(self, "memory_brain", None)
        if brain is not None and self.comment_context_service is not None:
            try:
                # find_by_identifiers 返回所有匹配 oid 的 event，
                # 需要检查是否有完整 video_observation（含 audiovisual 视听分析）
                hits = await asyncio.to_thread(
                    brain.find_by_identifiers, [str(oid_int)], 20
                )
                has_full_observation = False
                for hit in hits:
                    event_id = str(hit.get("event_id") or hit.get("id") or "")
                    if not event_id:
                        continue
                    event = await asyncio.to_thread(brain.get_event, event_id, None)
                    if not event:
                        continue
                    event_meta = event.get("metadata") or {}
                    if str(event_meta.get("oid", "")) != str(oid_int):
                        continue
                    # 检查是否为完整 video_observation（含视听分析，非仅元数据）
                    # event_type 为 video_observation 且存在 asr/visual_description/behavior_log 来源
                    if self._event_is_full_video_watch(event):
                        has_full_observation = True
                        break
                if has_full_observation:
                    logger.info(f"@回复：视频已完整观看过 oid={oid_int}，复用缓存")
                    return True
                logger.info(f"@回复：视频 oid={oid_int} 仅有元数据缓存，需完整观看")
            except Exception:
                pass

        video_info = await self.bili.get_video_info(oid_int)
        if not video_info:
            logger.warning(f"@回复：获取视频详情失败 oid={oid_int}")
            return False

        # bvid 补齐
        if not bvid:
            bvid = video_info.get("bvid", "") or ""
        if not bvid:
            logger.warning(f"@回复：视频缺少 bvid oid={oid_int}")
            return False

        title = video_info.get("title", "未知视频")
        owner = video_info.get("owner", {}).get("name", "未知UP")
        desc = video_info.get("desc", "")

        precheck = self._video_download_precheck(video_info)
        if precheck:
            logger.warning(
                "@回复：视频 %s 超过资源上限（%s），跳过下载，降级为元数据回复",
                bvid,
                precheck,
            )
            return False

        tags_list = await self.bili.get_video_tags(bvid, video_info=video_info) or []
        if isinstance(tags_list, str):
            tags_list = [t.strip() for t in tags_list.split(",") if t.strip()]

        logger.info(f"@回复：预观看视频 《{title}》 by {owner}")

        ctx = ProactiveVideoContext(bvid=bvid)
        ctx.metadata = video_info
        hot_comments = await self.bili.get_hot_comments(oid_int, limit=5) or []
        ctx.hot_comments = hot_comments if hot_comments else None

        # 联网搜索参考
        if self.web_search and self.web_search.is_available():
            try:
                search_query = await self.web_search.should_search_for_video(
                    video_info={"title": title, "desc": desc,
                                "tname": tags_list[0] if tags_list else "",
                                "owner_name": owner},
                    scene="proactive_video",
                )
                if search_query:
                    search_result = await self.web_search.search(
                        search_query, scene="proactive_video",
                    )
                    if search_result:
                        ctx.search_reference = search_result
            except Exception as e:
                ctx.degradation_reasons.append(f"search_failed: {e}")

        # 视频内容理解（视听双轨分析）
        video_file_to_cleanup = None
        work_dir_to_cleanup = None
        archive_committed = False
        # @-reply pre-watching competes with proactive video for the same
        # model pools and video_temp paths; serialize both through ONE
        # scheduler-level lock so they can never download to the same bvid
        # path and delete each other's artifacts.
        lock_getter = getattr(self, "_get_proactive_video_lock", None)
        if callable(lock_getter):
            watch_lock = lock_getter()
        else:
            watch_lock = getattr(self, "_watch_video_lock", None)
            if watch_lock is None:
                watch_lock = asyncio.Lock()
                self._watch_video_lock = watch_lock
        try:
            # A proactive video may legitimately hold the shared lock for
            # minutes; the scheduler loop must not stall behind it forever.
            lock_timeout = float(
                getattr(self, "_watch_video_lock_timeout_seconds", 90.0)
            )
            await asyncio.wait_for(watch_lock.acquire(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "@回复：视频工作锁等待超时（主动视频进行中），"
                "降级为元数据回复"
            )
            return False
        try:
            cid = video_info.get("cid", 0)
            if not cid:
                pages = video_info.get("pages", [])
                if pages:
                    cid = pages[0].get("cid", 0)
            if not cid:
                raise RuntimeError("视频缺少 CID")

            import os as _os
            from bilibot.video_understanding.cleanup import cleanup_media_artifacts

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

            logger.info(f"@回复：视频已下载，开始视听分析: {video_file}")
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
                    video_file, defer_cleanup=True,
                    require_complete_audio=True, require_complete_visual=True,
                )
            finally:
                if callable(throttle):
                    throttle(False, reason="video_understanding")
            try:
                if not isinstance(vu_result, dict):
                    raise RuntimeError("视频理解返回了无效结果")

                work_dir_to_cleanup = vu_result.get("work_dir") or None
                degradation = str(vu_result.get("degradation_reason") or "")
                audio_status = vu_result.get("audio_status") or {}
                audio_failed = (
                    isinstance(audio_status, dict)
                    and audio_status.get("status") == "failed"
                )
                if audio_failed:
                    reason = str(audio_status.get("error_code") or "audio_track_failed")
                    raise RuntimeError(f"视频提取未完成: {reason}")
                if degradation:
                    ctx.degradation_reasons.append(f"video_understanding: {degradation}")
                    logger.info(f"@回复：视频理解降级，继续归档: {degradation}")

                ctx.audiovisual = vu_result
                av_log = ctx.audiovisual_log
                if av_log:
                    logger.info(f"@回复：视频理解完成，行为日志 {len(av_log)} 字")
                elif not degradation:
                    logger.warning("@回复：视频理解未生成行为日志")
                # Degraded or empty extraction is not a complete watch: archive
                # metadata only and tell the caller to answer from metadata.
                if degradation or not av_log:
                    logger.warning(
                        "@回复：视频理解降级/无行为日志 degradation=%r，"
                        "按元数据回复处理",
                        degradation,
                    )
                    try:
                        from bilibot.memory_brain.ingestion import (
                            video_metadata_observation,
                        )

                        await self._archive_required(
                            video_metadata_observation(
                                account_id=self.account_id or "default",
                                oid=str(oid_int),
                                metadata=video_info,
                                persona_id=self._get_current_persona_id(),
                                scene="at_reply_watch",
                                pseudonymize_actor=self._pseudonymize_actor_id,
                            ),
                            treat_idempotent_as_ready=True,
                        )
                    except Exception as archive_exc:
                        logger.warning(
                            "@回复：降级元数据归档失败: %s", archive_exc
                        )
                    return False
            except ASRTranscriptionError as e:
                work_dir_to_cleanup = getattr(e, "work_dir", None) or work_dir_to_cleanup
                logger.warning("@回复：视频 ASR 失败 code=%s retryable=%s，降级为元数据回复",
                               e.code, e.retryable)
                return False
            except Exception as e:
                work_dir_to_cleanup = getattr(e, "work_dir", None) or work_dir_to_cleanup
                logger.warning("@回复：视频理解失败，降级为元数据回复: %s: %s",
                               type(e).__name__, e)
                return False

            # 归档到 memory_brain（让后续 build_context 命中缓存）
            try:
                from bilibot.memory_brain.ingestion import video_observation

                video_detail = await self._build_video_detail_digest(
                    title=title,
                    owner=owner,
                    behavior_log=ctx.audiovisual_log or "",
                    max_attempts=2,
                    # @预观看针对指定视频，不能换片；摘要失败时允许启发式降级。
                    require_llm=False,
                )
                await self._archive_required(
                    video_observation(
                        account_id=self.account_id or "default",
                        observation_key=f"at_reply:{bvid}:{oid_int}",
                        bvid=bvid,
                        oid=str(oid_int),
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
                archive_committed = True
                logger.info(f"@回复：视频已归档 bvid={bvid} oid={oid_int}")
            except Exception as archive_exc:
                logger.warning(f"@回复：视频归档失败: {archive_exc}")
                return False

            return True
        finally:
            from bilibot.video_understanding.cleanup import (
                cleanup_media_artifacts,
                schedule_cleanup,
            )

            if archive_committed:
                cleanup_media_artifacts(
                    video_file_to_cleanup, work_dir_to_cleanup
                )
            else:
                retained = [
                    p for p in (video_file_to_cleanup, work_dir_to_cleanup) if p
                ]
                if retained:
                    schedule_cleanup(retained, delay_seconds=1800)
                    logger.warning(
                        "@回复视频证据尚未归档，保留 1800 秒: bvid=%s paths=%s",
                        bvid,
                        len(retained),
                    )
            watch_lock.release()
    except Exception as e:
        logger.warning(f"@回复：预观看视频异常: {e}")
        return False
