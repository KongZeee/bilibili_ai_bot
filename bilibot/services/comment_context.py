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
    ReplyContext, VideoContext, CommentThread, CommentItem, UserProfile, SceneType,
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
        # 召回 query 拼入视频标题，让向量检索能命中该视频的观察记录（视听分析/评价等），
        # 而不是只召回与评论文本语义相似的其他评论
        recall_message = comment_text
        if video and video.title:
            recall_message = f"视频《{video.title}》\n{comment_text}".strip()
        memory_evidence = await self._get_related_memory(
            message=recall_message,
            oid=oid,
            user_id=user_id,
            persona_id=persona_id,
            video=video,
            recent_turns=recent_turns or [],
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

        # 优先复用当前账号 V6 brain 中的完整视频观察。
        cached = await self._get_cached_video_memory(oid, persona_id)
        if cached is not None:
            return cached, True

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

                await self._archive_context_required(
                    video_metadata_observation(
                        account_id=str(getattr(self.memory_brain, "account_id", "") or "default"),
                        oid=str(oid),
                        metadata=info,
                        persona_id=persona_id,
                    )
                )

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
            ), True
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
            raise ContextArchiveError("required model context archive failed") from exc

    async def _get_cached_video_memory(self, oid: str, persona_id: str = "") -> Optional[VideoContext]:
        """Reuse a validated V6 video event by OID/BVID within this account."""
        if not self.memory_brain or not oid:
            return None
        try:
            hits = await asyncio.to_thread(
                self.memory_brain.find_by_identifiers, [str(oid)], 20
            )
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
                if str(event_meta.get("oid", "")) != str(oid):
                    continue
                video_meta = {}
                for source in event.get("sources") or []:
                    if source.get("source_type") == "video_metadata":
                        video_meta = source.get("structured_data") or {}
                        break
                owner = video_meta.get("owner") or {}
                if not isinstance(owner, dict):
                    owner = {}
                return VideoContext(
                    oid=str(oid),
                    bvid=str(event_meta.get("bvid") or video_meta.get("bvid") or ""),
                    title=str(event.get("title") or video_meta.get("title") or ""),
                    owner_name=str(event_meta.get("owner") or owner.get("name") or ""),
                    owner_mid=str(owner.get("mid") or ""),
                    desc=str(video_meta.get("desc") or ""),
                    tags=list(event_meta.get("tags") or []),
                )
        except Exception as exc:
            logger.debug("V6 视频记忆复用失败 oid=%s: %s", oid, type(exc).__name__)
            return None
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
    ) -> str:
        """Run the single account-wide V6 recall contract for this input."""
        if not self.memory_brain or not callable(getattr(self.memory_brain, "recall", None)):
            return ""
        try:
            from bilibot.memory_brain import RecallQuery

            result = await self.memory_brain.recall(
                RecallQuery(
                    current_message=message,
                    recent_turns=tuple(recent_turns or ()),
                    account_id=getattr(self.memory_brain, "account_id", ""),
                    speaker_actor_id=str(user_id),
                    title=video.title if video else "",
                    bvid=video.bvid if video else "",
                    oid=str(oid),
                    scene="reply_comment",
                )
            )
            return result.prompt_evidence
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
