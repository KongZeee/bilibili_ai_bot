"""
CommentContextService - 评论上下文构建服务

PRD V6：
集中处理从 B站通知到完整 ReplyContext 的构建逻辑，
避免 scheduler 中堆砌散落的上下文拼接代码。

责任：
1. 从通知中提取 oid / rpid / user_id / username / content
2. 调用 BilibiliAPI 获取视频信息（失败则降级为 video_context_complete=False）
3. 复用当前账号 V6 memory brain 中的视频观察
4. 调用 UserStateSystem 获取用户画像和心情
5. 调用 DataStore 获取 Bot 在该评论线/同视频下的历史回复
6. 组装为 ReplyContext（dataclass）

本服务不会读取 knowledge_base.db、chat_memory.json 或其他旧记忆载体。
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..models import (
    ReplyContext, VideoContext, CommentThread, CommentItem, UserProfile,
)

logger = logging.getLogger("bilibot.comment_context")


class ContextArchiveError(RuntimeError):
    """A model-context source could not be durably committed to V6."""


class CommentContextService:
    """评论上下文构建服务"""

    def __init__(
        self,
        bili=None,
        user_state=None,
        data_store=None,
        persona_store=None,
        config_loader=None,
        knowledge_memory=None,
        memory_brain=None,
    ):
        self.bili = bili
        self.user_state = user_state
        self.ds = data_store
        self.persona_store = persona_store
        self.config_loader = config_loader
        # ``knowledge_memory`` 只保留为调用签名兼容参数。V6 必须由调用方
        # 显式注入账号级 brain，避免把旧 KnowledgeBaseMemory 误当成新脑。
        self.memory_brain = memory_brain
        self._memory_archive_required = bool(
            memory_brain is not None
            and callable(getattr(type(memory_brain), "archive_observation_async", None))
        )
        if memory_brain is None and knowledge_memory is not None:
            logger.debug("忽略已废弃的 knowledge_memory 参数；V6 记忆降级为空")

    async def build_context(
        self,
        notification: Dict[str, Any],
        current_user_id: Optional[str] = None,
        persona_id: str = "",
        recent_turns: Optional[List[Any]] = None,
    ) -> ReplyContext:
        """从 B站通知构建完整 ReplyContext

        Args:
            notification: B站通知 item，包含 reply_id / subject_id / user / source_content 等
            current_user_id: 当前评论用户 id（可从 notification.user.mid 推导）
            persona_id: 人格ID（MEM-603/REP-603：记忆检索硬过滤，防止跨人格记忆泄漏）

        Returns:
            ReplyContext（必含 video / thread / user_profile；缺失字段标记 video_context_complete=False）
        """
        # 1. 解析通知基础字段
        # B站通知结构: {id, user:{mid,nickname}, item:{subject_id,source_id,root_id,business_id,source_content}}
        item_detail = notification.get("item", {}) or {}
        rpid = str(item_detail.get("source_id", "") or notification.get("id", "") or "")
        oid = str(item_detail.get("subject_id", "") or "")
        comment_type = int(item_detail.get("business_id", 1) or 1)

        user = notification.get("user", {}) or {}
        user_id = str(user.get("mid", "") or "") or (current_user_id or "")
        username = user.get("nickname", "") or user.get("uname", "") or "未知用户"

        # source_content 在 item.source_content 中
        comment_text = (
            item_detail.get("source_content", "")
            or item_detail.get("content", "")
            or notification.get("source_content", "")
            or ""
        )

        # 2. 构建视频上下文（仅对视频评论 type=1 获取，其他类型跳过）
        video, video_complete = await self._build_video_context(oid, comment_type, persona_id)

        # 3. 构建评论线（至少包含当前评论）
        thread = self._build_comment_thread(
            rpid=rpid,
            user_id=user_id,
            username=username,
            content=comment_text,
        )

        # 4. 用户画像
        user_profile = await self._build_user_profile(user_id, username, persona_id)

        # 5. Bot 在该评论线的历史回复
        bot_thread_replies = self._get_bot_thread_replies(rpid=rpid, oid=oid)

        # 6. 同视频下的相关历史互动
        # 召回 query 拼入真实视频标题，让向量/FTS 能命中该视频观察记录。
        # 禁止把「评论对话上下文」等占位标题拼进 query（会污染 title_entity 通道）。
        recall_message = comment_text
        video_title = (video.title or "").strip() if video else ""
        if video_title and video_title not in self._PLACEHOLDER_TITLES:
            recall_message = f"视频《{video_title}》\n{comment_text}".strip()
        memory_evidence = await self._get_related_memory(
            message=recall_message,
            oid=oid,
            user_id=user_id,
            persona_id=persona_id,
            video=video,
            recent_turns=recent_turns or [],
            scene="reply_comment",
        )

        # 7. 当前心情
        mood = self._get_current_mood()

        return ReplyContext(
            video=video,
            thread=thread,
            user_profile=user_profile,
            memory_context=[],
            memory_evidence=memory_evidence,
            bot_thread_replies=bot_thread_replies,
            mood=mood,
            video_context_complete=video_complete,
        )

    # ── 私有：视频上下文 ──

    async def _build_video_context(
        self, oid: str, comment_type: int = 1, persona_id: str = ""
    ) -> tuple[Optional[VideoContext], bool]:
        """构建视频上下文

        Returns:
            (VideoContext or None, video_context_complete)
        """
        if not oid:
            return None, False

        # 仅视频评论(type=1)才获取视频信息，文章(type=11)/动态(type=17)等跳过
        if comment_type != 1:
            return None, False

        # 优先复用当前账号 V6 brain 中的视频观察。
        # complete=True 仅当缓存含视听/摘要；纯元数据不得冒充「看过细节」。
        cached = await self._get_cached_video_memory(oid, persona_id)
        if cached is not None:
            return cached

        # 调用 BilibiliAPI
        if self.bili is None:
            # API 不可用，仅用 oid 占位，标记为不完整
            return VideoContext(oid=oid, title=""), False

        try:
            info = await self.bili.get_video_info(int(oid))
            if not info:
                return VideoContext(oid=oid, title=""), False

            owner = info.get("owner", {}) or {}
            tags = info.get("tag", []) or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]

            if self._memory_archive_required:
                from bilibot.memory_brain.ingestion import video_metadata_observation

                pseudonymize_actor = None
                redactor = getattr(self.memory_brain, "redactor", None)
                pseudo_method = getattr(redactor, "pseudonymize_identifier", None)
                if callable(pseudo_method):
                    pseudonymize_actor = lambda value: pseudo_method(
                        str(value), namespace="uid"
                    )
                try:
                    await self._archive_context_required(
                        video_metadata_observation(
                            account_id=str(getattr(self.memory_brain, "account_id", "") or "default"),
                            oid=str(oid),
                            metadata=info,
                            persona_id=persona_id,
                            pseudonymize_actor=pseudonymize_actor,
                        )
                    )
                except ContextArchiveError as archive_exc:
                    # The fetched metadata is already being used as model
                    # context. If it cannot enter the account brain, generating
                    # a reply would create an unremembered experience.
                    logger.error(
                        "视频元数据归档失败，阻断本次评论上下文 oid=%s: %s",
                        oid,
                        type(archive_exc).__name__,
                    )
                    raise

            # 仅元数据：可回填标题/UP，但禁止模型编造视听细节。
            return VideoContext(
                oid=str(oid),
                bvid=info.get("bvid", "") or "",
                title=info.get("title", "") or "",
                owner_name=owner.get("name", "") or "",
                owner_mid=str(owner.get("mid", "") or ""),
                desc=info.get("desc", "") or "",
                tags=tags,
                category=str(info.get("tid", "") or ""),
                publish_time=str(info.get("pubdate", "") or ""),
            ), False
        except ContextArchiveError:
            raise
        except Exception as e:
            logger.warning(f"获取视频信息失败 oid={oid}: {e}")
            return VideoContext(oid=oid, title=""), False

    async def _archive_context_required(self, envelope):
        try:
            result = await self.memory_brain.archive_observation_async(envelope)
            if result is None:
                raise RuntimeError("V6 context archive returned no commit result")
            if getattr(result, "source_committed", True) is False:
                raise RuntimeError("V6 context source commit was not confirmed")
            return result
        except Exception as exc:
            # 同 key 同内容会 soft success；不同内容冲突 / tombstone 对「仅元数据」
            # 不应抬成致命错误——调用方已 complete=False 降级。
            try:
                from bilibot.memory_brain.models import (
                    IdempotencyConflictError,
                    ReingestBlockedError,
                )
                if isinstance(exc, IdempotencyConflictError):
                    logger.info(
                        "context archive idempotent conflict treated as ready: %s",
                        getattr(envelope, "idempotency_key", ""),
                    )
                    return {"source_committed": True, "idempotent_hit": True}
                if isinstance(exc, ReingestBlockedError):
                    logger.warning(
                        "context archive blocked by tombstone: %s",
                        getattr(envelope, "idempotency_key", ""),
                    )
                    return {"source_committed": False, "blocked": True}
            except Exception:
                pass
            raise ContextArchiveError("required model context archive failed") from exc

    # find_by_identifiers 会按 metadata.oid 命中 comment_thread 等非视频事件；
    # 它们的 event_title 固定为「评论对话上下文」，绝不能当成视频标题喂给召回。
    _VIDEO_EVENT_TYPES = frozenset(
        {
            "video_observation",
            "video_metadata_observation",
            "bot_experience",
        }
    )
    _VIDEO_SOURCE_TYPES = frozenset(
        {
            "video",
            "video_metadata",
            "video_experience",
        }
    )
    _PLACEHOLDER_TITLES = frozenset(
        {
            "评论对话上下文",
            "未知视频",
            "",
        }
    )

    _FULL_AV_SOURCE_TYPES = frozenset(
        {
            "video_detail",
            "behavior_log",
            "asr",
            "subtitle",
            "visual_description",
        }
    )

    @classmethod
    def _event_has_full_audiovisual(cls, event: Optional[dict]) -> bool:
        """True only when the event carries real watch/digest evidence."""
        if not isinstance(event, dict):
            return False
        event_type = str(event.get("event_type") or "")
        source_type = str(event.get("source_type") or "")
        if event_type not in {"video_observation"} and source_type not in {"video"}:
            return False
        event_meta = event.get("metadata") or {}
        if isinstance(event_meta, dict) and event_meta.get("has_video_detail"):
            return True
        for source in event.get("sources") or []:
            if not isinstance(source, dict):
                continue
            if source.get("source_type") in cls._FULL_AV_SOURCE_TYPES:
                text = str(
                    source.get("full_text") or source.get("text") or ""
                ).strip()
                if text:
                    return True
        return False

    async def _get_cached_video_memory(
        self, oid: str, persona_id: str = ""
    ) -> Optional[tuple[VideoContext, bool]]:
        """Reuse a validated V6 *video* event by OID/BVID within this account.

        Returns ``(VideoContext, video_context_complete)`` or None.
        ``complete`` is True only when the hit includes audiovisual digest/log
        evidence — bare metadata is reusable for title/bvid but incomplete.

        Identifier lookup is intentionally broad (any event carrying this oid).
        Only video-related events may populate VideoContext.title; conversation
        threads sharing the same oid must be ignored.
        """
        if not self.memory_brain or not oid:
            return None
        try:
            hits = await asyncio.to_thread(
                self.memory_brain.find_by_identifiers, [str(oid)], 20
            )
            candidates: list[tuple[int, VideoContext, bool]] = []
            for hit in hits:
                event_id = str(hit.get("event_id") or hit.get("id") or "")
                if not event_id:
                    continue
                event = await asyncio.to_thread(
                    self.memory_brain.get_event, event_id, None
                )
                if not event:
                    continue
                event_meta = event.get("metadata") or {}
                if not isinstance(event_meta, dict):
                    event_meta = {}
                if str(event_meta.get("oid", "")) != str(oid):
                    continue

                event_type = str(event.get("event_type") or "")
                source_type = str(event.get("source_type") or "")
                if (
                    event_type not in self._VIDEO_EVENT_TYPES
                    and source_type not in self._VIDEO_SOURCE_TYPES
                ):
                    continue

                video_meta: dict = {}
                for source in event.get("sources") or []:
                    if not isinstance(source, dict):
                        continue
                    if source.get("source_type") == "video_metadata":
                        raw = source.get("structured_data") or {}
                        if isinstance(raw, dict) and raw:
                            video_meta = raw
                            break
                owner = video_meta.get("owner") or {}
                if not isinstance(owner, dict):
                    owner = {}

                # Prefer real B站 metadata title over event_title (the latter is
                # sometimes a generic label or bot-experience summary title).
                title = str(
                    video_meta.get("title")
                    or event_meta.get("title")
                    or ""
                ).strip()
                event_title = str(event.get("title") or "").strip()
                if not title and event_title not in self._PLACEHOLDER_TITLES:
                    # video_observation uses the real title as event_title.
                    if event_type in {"video_observation", "video_metadata_observation"}:
                        title = event_title
                if not title or title in self._PLACEHOLDER_TITLES:
                    # Incomplete cache hit — better fall through to API than
                    # poison recall with a fake title.
                    continue

                tags = event_meta.get("tags") or video_meta.get("tag") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                elif not isinstance(tags, list):
                    tags = []

                ctx = VideoContext(
                    oid=str(oid),
                    bvid=str(
                        event_meta.get("bvid")
                        or video_meta.get("bvid")
                        or ""
                    ),
                    title=title,
                    owner_name=str(
                        event_meta.get("owner")
                        or owner.get("name")
                        or ""
                    ),
                    owner_mid=str(owner.get("mid") or ""),
                    desc=str(video_meta.get("desc") or ""),
                    tags=list(tags),
                )
                has_full_av = self._event_has_full_audiovisual(event)
                # Prefer full audiovisual observation over bare metadata.
                rank = 0
                if event_type == "video_observation":
                    rank += 100
                if has_full_av:
                    rank += 80
                if any(
                    isinstance(s, dict) and s.get("source_type") == "video_detail"
                    for s in (event.get("sources") or [])
                ):
                    rank += 40
                if any(
                    isinstance(s, dict) and s.get("source_type") == "behavior_log"
                    for s in (event.get("sources") or [])
                ):
                    rank += 30
                if ctx.bvid:
                    rank += 10
                if ctx.desc:
                    rank += 5
                candidates.append((rank, ctx, has_full_av))

            if not candidates:
                return None
            candidates.sort(key=lambda item: item[0], reverse=True)
            _rank, best_ctx, complete = candidates[0]
            return best_ctx, complete
        except Exception as exc:
            logger.debug("V6 视频记忆复用失败 oid=%s: %s", oid, type(exc).__name__)
            return None

    # ── 私有：评论线 ──

    def _build_comment_thread(
        self, rpid: str, user_id: str, username: str, content: str
    ) -> CommentThread:
        """构建评论线（至少包含当前评论）"""
        item = CommentItem(
            rpid=rpid,
            user_id=user_id,
            username=username,
            content=content,
        )
        return CommentThread(root_rpid=rpid, comments=[item])

    # ── 私有：用户画像 ──

    async def _build_user_profile(self, user_id: str, username: str, persona_id: str = "") -> UserProfile:
        """从独立 UserStateSystem 获取已验证的用户画像。"""
        profile = UserProfile(user_id=user_id, username=username)
        if not user_id:
            return profile

        # V6 recalled user claims are evidence, not profile facts. The explicit
        # UserStateSystem remains the sole owner of verified profile/affection state.
        if self.user_state is None:
            return profile
        try:
            # UserStateSystem.get_user_profile_context 返回纯文本，直接作为 facts
            ctx_text = self.user_state.get_user_profile_context(user_id)
            if ctx_text:
                # 把画像文本塞入 facts，至少保留互动痕迹
                profile.facts = [ctx_text[:200]]
            # 好感度
            if hasattr(self.user_state, "get_affection"):
                profile.affection = self.user_state.get_affection(user_id)
                # 根据好感度推导等级
                if hasattr(self.user_state, "get_level"):
                    level = self.user_state.get_level(profile.affection, mid=user_id)
                    profile.level = level
        except Exception as e:
            logger.debug(f"获取用户画像失败 uid={user_id}: {e}")

        return profile

    # ── 私有：Bot 历史回复 ──

    def _get_bot_thread_replies(self, rpid: str, oid: str) -> List[str]:
        """获取结构化互动状态中的 Bot 历史回复。

        长期经历统一由一次 V6 recall 注入。DataStore 没有结构化查询能力时
        明确降级为空，绝不回读 chat_memory.json。
        """
        if not self.ds:
            return []
        getter = getattr(self.ds, "get_bot_replies_for_thread", None)
        if not callable(getter):
            return []
        try:
            return list(getter(rpid=rpid) or [])[-5:]
        except Exception as exc:
            logger.debug(
                "读取结构化评论线回复失败 rpid=%s oid=%s: %s",
                rpid,
                oid,
                type(exc).__name__,
            )
        return []

    # ── 私有：相关长期记忆 ──

    async def _get_related_memory(
        self,
        *,
        message: str,
        oid: str,
        user_id: str,
        persona_id: str = "",
        video: Optional[VideoContext] = None,
        recent_turns: Optional[List[Any]] = None,
        scene: str = "reply_comment",
    ) -> str:
        """Account-wide V6 hybrid recall (Direct + Association).

        Never speaker-only: speaker_actor_id is a boost channel; global_recent +
        FTS/vector always surface Bot self experiences (video / bangumi /
        dynamic / companion) when relevant.
        """
        if not self.memory_brain or not callable(getattr(self.memory_brain, "recall", None)):
            return ""
        try:
            from bilibot.memory_brain import RecallQuery

            video_title = ""
            bvid = ""
            entity_hints: list[str] = []
            if video:
                raw_title = str(video.title or "").strip()
                if raw_title and raw_title not in self._PLACEHOLDER_TITLES:
                    video_title = raw_title
                    entity_hints.append(raw_title)
                bvid = str(video.bvid or "")
                owner = str(getattr(video, "owner_name", "") or "").strip()
                if owner:
                    entity_hints.append(owner)
            # Nudge query so bot self experiences are lexical-eligible
            msg = str(message or "").strip()
            if video_title and video_title not in msg:
                msg = f"视频《{video_title}》\n{msg}".strip()
            account_id = str(
                getattr(self.memory_brain, "account_id", "")
                or getattr(self, "account_id", "")
                or ""
            )
            result = await self.memory_brain.recall(
                RecallQuery(
                    current_message=msg,
                    recent_turns=tuple(recent_turns or ()),
                    account_id=account_id,
                    # speaker boost only — hybrid channels still pull Bot self
                    speaker_actor_id=str(user_id or ""),
                    title=video_title,
                    bvid=bvid,
                    oid=str(oid or ""),
                    scene=RecallQuery.normalize_scene(scene),
                    entity_hints=tuple(entity_hints),
                )
            )
            evidence = result.prompt_evidence if result is not None else ""
            return str(evidence or "")
        except Exception as exc:
            logger.warning("V6 记忆召回失败，降级为空: %s", type(exc).__name__)
            return ""

    # ── 私有：当前心情 ──

    def _get_current_mood(self) -> str:
        """获取当前心情（可选）"""
        # UserStateSystem.get_today_mood 返回 (mood_str, mood_prompt) 元组
        if self.user_state and hasattr(self.user_state, "get_today_mood"):
            try:
                result = self.user_state.get_today_mood()
                # get_today_mood 返回 (mood_str, mood_prompt) 元组
                if isinstance(result, tuple):
                    return result[0] or ""
                return str(result) or ""
            except Exception:
                pass
        return ""
