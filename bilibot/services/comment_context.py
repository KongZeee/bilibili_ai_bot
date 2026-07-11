"""
CommentContextService - 评论上下文构建服务

PRD V4 §4.3 / §6.3：
集中处理从 B站通知到完整 ReplyContext 的构建逻辑，
避免 scheduler 中堆砌散落的上下文拼接代码。

责任：
1. 从通知中提取 oid / rpid / user_id / username / content
2. 调用 BilibiliAPI 获取视频信息（失败则降级为 video_context_complete=False）
3. 复用 SQLite memory_atoms 中的 content_video 记忆（PRD V4 §4.4.3）
4. 调用 UserStateSystem 获取用户画像和心情
5. 调用 DataStore 获取 Bot 在该评论线/同视频下的历史回复
6. 组装为 ReplyContext（dataclass）
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..models import (
    ReplyContext, VideoContext, CommentThread, CommentItem, UserProfile, SceneType,
)

logger = logging.getLogger("bilibot.comment_context")


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
    ):
        self.bili = bili
        self.user_state = user_state
        self.ds = data_store
        self.persona_store = persona_store
        self.config_loader = config_loader
        # PRD V4 §4.4.3：知识库记忆（SQLite memory_atoms），实际存储记忆的地方
        self.knowledge_memory = knowledge_memory

    async def build_context(
        self,
        notification: Dict[str, Any],
        current_user_id: Optional[str] = None,
        persona_id: str = "",
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
        related_memory = await self._get_related_memory(oid=oid, user_id=user_id, persona_id=persona_id)

        # 7. 当前心情
        mood = self._get_current_mood()

        return ReplyContext(
            video=video,
            thread=thread,
            user_profile=user_profile,
            memory_context=related_memory,
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

        # PRD V4 §4.4.3：优先复用 SQLite memory_atoms 中的 content_video 记忆
        # REP-603/REP-604：传入 persona_id 硬过滤 + 异步执行避免阻塞事件循环
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
        except Exception as e:
            logger.warning(f"获取视频信息失败 oid={oid}: {e}")
            return VideoContext(oid=oid, title=""), False

    async def _get_cached_video_memory(self, oid: str, persona_id: str = "") -> Optional[VideoContext]:
        """从 SQLite memory_atoms 复用 content_video 记忆（PRD V4 §4.4.3）

        REP-603：persona_id 作为硬过滤条件进入 SQL WHERE 子句，防止跨人格记忆泄漏。
        REP-604：sqlite3 同步操作通过 asyncio.to_thread 放入工作线程，避免阻塞事件循环。
        """
        if not self.ds or not oid:
            return None
        return await asyncio.to_thread(self._get_cached_video_memory_sync, oid, persona_id)

    def _get_cached_video_memory_sync(self, oid: str, persona_id: str = "") -> Optional[VideoContext]:
        """_get_cached_video_memory 的同步实现（在工作线程内执行）"""
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path(self.ds.data_dir) / "knowledge_base.db"
            if not db_path.exists():
                return None
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            import json
            # 查找同 oid 的 content_video 记忆
            # REP-603：persona_id 硬过滤（参数化查询，防止 SQL 注入）
            # PRD 5.3：用 try/finally 确保 conn.close()
            try:
                if persona_id:
                    rows = conn.execute(
                        "SELECT content, metadata FROM memory_atoms "
                        "WHERE category = 'content_video' AND is_active = 1 "
                        "AND persona_id = ? "
                        "ORDER BY created_at DESC LIMIT 5",
                        (persona_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT content, metadata FROM memory_atoms "
                        "WHERE category = 'content_video' AND is_active = 1 "
                        "ORDER BY created_at DESC LIMIT 5"
                    ).fetchall()
            finally:
                conn.close()
            for r in rows:
                try:
                    meta = json.loads(r["metadata"] or "{}")
                    if str(meta.get("oid", "")) == str(oid):
                        return VideoContext(
                            oid=str(oid),
                            bvid=meta.get("bvid", "") or "",
                            title=meta.get("title", "") or "",
                            owner_name=meta.get("owner_name", "") or "",
                            owner_mid=str(meta.get("owner_mid", "") or ""),
                            desc=meta.get("desc", "") or "",
                            tags=meta.get("tags", []) or [],
                        )
                except Exception:
                    continue
            return None
        except Exception:
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
        """从记忆系统获取用户画像

        优先使用 knowledge_memory（SQLite），回退到旧 memory 系统。
        MEM-603：persona_id 作为硬过滤条件传入记忆检索，防止跨人格记忆泄漏。
        REP-604：同步 SQLite 调用通过 asyncio.to_thread 放入工作线程。
        """
        profile = UserProfile(user_id=user_id, username=username)
        if not user_id:
            return profile

        # 1. 优先用 knowledge_memory 提取用户画像
        if self.knowledge_memory:
            try:
                # PRD V3 §4.6：用 async 版本避免阻塞事件循环
                if hasattr(self.knowledge_memory, "get_user_memories_async"):
                    memories = await self.knowledge_memory.get_user_memories_async(
                        user_id, limit=20, persona_id=persona_id
                    )
                else:
                    # REP-604：同步 get_user_memories 包装到线程，避免阻塞事件循环
                    memories = await asyncio.to_thread(
                        self.knowledge_memory.get_user_memories,
                        user_id, 20, persona_id
                    )
                if memories:
                    # 从历史记忆中提取事实
                    facts = []
                    for m in memories:
                        content = m.get("content", "")
                        if content:
                            facts.append(content)
                    profile.facts = facts[:5]
                    profile.last_interactions = [
                        m.get("content", "")[:100] for m in memories[:3] if m.get("content")
                    ]
                    # 有记忆说明互动过，设置基础好感度
                    profile.affection = min(len(memories) * 5, 50)
                    return profile
            except Exception as e:
                logger.warning(f"knowledge_memory.get_user_memories 失败: {e}")

        # 2. 回退到 UserStateSystem（画像/好感度）
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
        """获取 Bot 在该评论线/同视频下的历史回复"""
        if not self.ds:
            return []
        try:
            # DataStore 优先按 rpid 查询
            if hasattr(self.ds, "get_bot_replies_for_thread"):
                return list(self.ds.get_bot_replies_for_thread(rpid=rpid) or [])
            # fallback：从 chat_memory 读取
            if hasattr(self.ds, "load_json"):
                mem = self.ds.load_json("chat_memory.json", {}) or {}
                replies: List[str] = []
                # 按 rpid 查
                rpid_key = f"thread:{rpid}"
                if rpid_key in mem:
                    for entry in mem[rpid_key][-3:]:
                        reply = entry.get("reply_text") if isinstance(entry, dict) else None
                        if reply:
                            replies.append(reply)
                # 按 oid 查
                oid_key = f"oid:{oid}"
                if oid_key in mem:
                    for entry in mem[oid_key][-3:]:
                        reply = entry.get("reply_text") if isinstance(entry, dict) else None
                        if reply:
                            replies.append(reply)
                return replies[-5:]
        except Exception:
            pass
        return []

    # ── 私有：相关长期记忆 ──

    async def _get_related_memory(self, oid: str, user_id: str, persona_id: str = "") -> List[str]:
        """获取与该用户/视频相关的长期记忆

        优先使用 knowledge_memory（SQLite），回退到旧 memory 系统。
        MEM-603：persona_id 作为硬过滤条件传入 search_by_user，防止跨人格记忆泄漏。
        REP-604：search_by_user 是同步 SQLite 调用，通过 asyncio.to_thread 放入工作线程。
        """
        # 1. 优先用 knowledge_memory（实际存储记忆的地方）
        if self.knowledge_memory and user_id:
            try:
                memories = await asyncio.to_thread(
                    self.knowledge_memory.get_user_memories,
                    user_id, 5, persona_id
                )
                if memories:
                    results = [m.get("content", "") for m in memories if m.get("content")]
                    if results:
                        return results
            except Exception as e:
                logger.warning(f"knowledge_memory.search_by_user 失败: {e}")

        # 2. 回退到旧 memory 系统
        # PRD 4.8：旧 memory 系统没有 search_related 方法，此分支永远 False，已移除
        return []

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
