"""
BiliBot 数据模型

所有模型必须提供 to_dict() / from_dict() / to_prompt_text()（上下文模型）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


# ═══════════════════════════════════════════════
# 枚举
# ═══════════════════════════════════════════════

class SceneType(str, Enum):
    """生成场景"""
    REPLY_COMMENT = "reply_comment"
    PRIVATE_REPLY = "private_reply"
    PROACTIVE_COMMENT = "proactive_comment"
    DYNAMIC_POST = "dynamic_post"
    WEEKLY_SUMMARY = "weekly_summary"
    BANGUMI_COMMENT = "bangumi_comment"
    VIDEO_RECOMMEND = "video_recommend"
    MEMORY_SUMMARY = "memory_summary"
    SAFETY_CHECK = "safety_check"


class MemoryLevel(str, Enum):
    """记忆层级"""
    SESSION = "session"
    USER = "user"
    CONTENT = "content"
    LONG_TERM = "long_term"


# ═══════════════════════════════════════════════
# Persona 模型
# ═══════════════════════════════════════════════

@dataclass
class PersonaExample:
    """人格示例输入输出"""
    input: str = ""
    output: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PersonaExample":
        return cls(
            input=d.get("input", ""),
            output=d.get("output", ""),
        )


@dataclass
class Persona:
    """人格定义"""
    id: str = ""
    name: str = ""
    description: str = ""
    base_prompt: str = ""
    speaking_style: str = ""
    boundaries: str = ""
    relationship_rules: str = ""
    reply_rules: str = ""
    proactive_comment_rules: str = ""
    dynamic_rules: str = ""
    weekly_rules: str = ""
    examples: list[PersonaExample] = field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    # P3: 市场元数据
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    github_url: str = ""

    # ─── 序列化 ───

    def to_dict(self) -> dict:
        d = asdict(self)
        d["examples"] = [e.to_dict() for e in self.examples]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PersonaExample":
        examples = [PersonaExample.from_dict(e) for e in d.get("examples", [])]
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            base_prompt=d.get("base_prompt", ""),
            speaking_style=d.get("speaking_style", ""),
            boundaries=d.get("boundaries", ""),
            relationship_rules=d.get("relationship_rules", ""),
            reply_rules=d.get("reply_rules", ""),
            proactive_comment_rules=d.get("proactive_comment_rules", ""),
            dynamic_rules=d.get("dynamic_rules", ""),
            weekly_rules=d.get("weekly_rules", ""),
            examples=examples,
            enabled=d.get("enabled", True),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            version=d.get("version", "1.0.0"),
            author=d.get("author", ""),
            tags=d.get("tags", []),
            github_url=d.get("github_url", ""),
        )

    def get_rules_for_scene(self, scene: SceneType) -> str:
        """根据场景获取对应规则"""
        mapping = {
            SceneType.REPLY_COMMENT: self.reply_rules,
            SceneType.PRIVATE_REPLY: self.reply_rules,
            SceneType.PROACTIVE_COMMENT: self.proactive_comment_rules,
            SceneType.DYNAMIC_POST: self.dynamic_rules,
            SceneType.WEEKLY_SUMMARY: self.weekly_rules,
            SceneType.BANGUMI_COMMENT: self.reply_rules,
        }
        return mapping.get(scene, "")


# ═══════════════════════════════════════════════
# 上下文模型
# ═══════════════════════════════════════════════

@dataclass
class VideoContext:
    """视频上下文 / 结构化视频记忆

    合并 PRD §5.5 视频上下文 + §5.6/§8.3 结构化视频记忆字段。
    """
    # ── 基础信息 ──
    bvid: str = ""
    oid: str = ""
    title: str = ""
    owner_name: str = ""
    owner_mid: str = ""
    desc: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = ""
    publish_time: str = ""
    # ── 文本内容 ──
    subtitle_summary: str = ""
    hot_comment_summary: str = ""
    # ── 视觉内容 ──
    visual_summary: str = ""
    # ── Bot 历史分析 ──
    bot_review: str = ""
    # ── Bot 主观评价（PRD §5.6 / §8.3）──
    interest_score: int = 0          # 兴趣分 0-100
    emotion: str = ""                # 观看情绪
    suitable_for_comment: bool = True  # 是否适合评论
    # ── 互动状态（PRD §5.6）──
    liked: bool = False
    coined: bool = False
    favorited: bool = False
    commented: bool = False
    followed: bool = False
    # ── 生成过的评论内容（PRD §5.6）──
    generated_comments: list[str] = field(default_factory=list)
    # ── 观看时间（PRD §8.3）──
    watched_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VideoContext":
        return cls(
            bvid=d.get("bvid", ""),
            oid=d.get("oid", ""),
            title=d.get("title", ""),
            owner_name=d.get("owner_name", ""),
            owner_mid=d.get("owner_mid", ""),
            desc=d.get("desc", ""),
            tags=d.get("tags", []),
            category=d.get("category", ""),
            publish_time=d.get("publish_time", ""),
            subtitle_summary=d.get("subtitle_summary", ""),
            hot_comment_summary=d.get("hot_comment_summary", ""),
            visual_summary=d.get("visual_summary", ""),
            bot_review=d.get("bot_review", ""),
            interest_score=d.get("interest_score", 0),
            emotion=d.get("emotion", ""),
            suitable_for_comment=d.get("suitable_for_comment", True),
            liked=d.get("liked", False),
            coined=d.get("coined", False),
            favorited=d.get("favorited", False),
            commented=d.get("commented", False),
            followed=d.get("followed", False),
            generated_comments=d.get("generated_comments", []),
            watched_at=d.get("watched_at", ""),
        )

    def to_prompt_text(self) -> str:
        """转为 prompt 文本"""
        parts = []
        if self.title:
            parts.append(f"【视频标题】{self.title}")
        if self.owner_name:
            parts.append(f"【UP主】{self.owner_name}")
        if self.desc:
            parts.append(f"【简介】{self.desc[:500]}")
        if self.tags:
            parts.append(f"【标签】{', '.join(self.tags)}")
        if self.category:
            parts.append(f"【分区】{self.category}")
        if self.hot_comment_summary:
            parts.append(f"【热评摘要】{self.hot_comment_summary}")
        if self.subtitle_summary:
            parts.append(f"【字幕摘要】{self.subtitle_summary}")
        if self.bot_review:
            parts.append(f"【Bot 之前的分析】{self.bot_review}")
        # Bot 主观评价
        if self.interest_score:
            parts.append(f"【兴趣分】{self.interest_score}")
        if self.emotion:
            parts.append(f"【观看情绪】{self.emotion}")
        if self.watched_at:
            parts.append(f"【观看时间】{self.watched_at}")
        return "\n".join(parts)


@dataclass
class CommentItem:
    """单条评论"""
    rpid: str = ""
    user_id: str = ""
    username: str = ""
    content: str = ""
    created_at: str = ""
    is_bot: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CommentItem":
        return cls(
            rpid=str(d.get("rpid", "")),
            user_id=str(d.get("user_id", "")),
            username=d.get("username", ""),
            content=d.get("content", ""),
            created_at=d.get("created_at", ""),
            is_bot=d.get("is_bot", False),
        )


@dataclass
class CommentThread:
    """评论线（楼中楼）"""
    root_rpid: str = ""
    comments: list[CommentItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "root_rpid": self.root_rpid,
            "comments": [c.to_dict() for c in self.comments],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CommentThread":
        comments = [CommentItem.from_dict(c) for c in d.get("comments", [])]
        return cls(
            root_rpid=d.get("root_rpid", ""),
            comments=comments,
        )

    def to_prompt_text(self) -> str:
        """转为 prompt 文本"""
        if not self.comments:
            return ""
        lines = ["【当前评论线】"]
        for c in self.comments:
            prefix = "[Bot]" if c.is_bot else f"[{c.username}]"
            lines.append(f"  {prefix}: {c.content}")
        return "\n".join(lines)


@dataclass
class UserProfile:
    """用户画像"""
    user_id: str = ""
    username: str = ""
    affection: int = 0
    tags: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    last_interactions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UserProfile":
        return cls(
            user_id=str(d.get("user_id", "")),
            username=d.get("username", ""),
            affection=d.get("affection", 0),
            tags=d.get("tags", []),
            facts=d.get("facts", []),
            last_interactions=d.get("last_interactions", []),
        )

    def to_prompt_text(self) -> str:
        parts = [f"【用户】{self.username or '未知'}"]
        if self.tags:
            parts.append(f"标签: {', '.join(self.tags)}")
        if self.facts:
            parts.append(f"已知事实: {'; '.join(self.facts[-5:])}")
        if self.affection:
            parts.append(f"好感度: {self.affection}")
        return "\n".join(parts)


@dataclass
class ReplyContext:
    """回复完整上下文包"""
    video: Optional[VideoContext] = None
    thread: Optional[CommentThread] = None
    user_profile: Optional[UserProfile] = None
    memory_context: list[str] = field(default_factory=list)
    bot_thread_replies: list[str] = field(default_factory=list)
    related_area_history: list[str] = field(default_factory=list)
    recent_bot_actions: list[str] = field(default_factory=list)
    mood: str = ""
    video_context_complete: bool = False

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "video": self.video.to_dict() if self.video else None,
            "thread": self.thread.to_dict() if self.thread else None,
            "user_profile": self.user_profile.to_dict() if self.user_profile else None,
            "memory_context": self.memory_context,
            "bot_thread_replies": self.bot_thread_replies,
            "related_area_history": self.related_area_history,
            "recent_bot_actions": self.recent_bot_actions,
            "mood": self.mood,
            "video_context_complete": self.video_context_complete,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ReplyContext":
        video = VideoContext.from_dict(d["video"]) if d.get("video") else None
        thread = CommentThread.from_dict(d["thread"]) if d.get("thread") else None
        user_profile = UserProfile.from_dict(d["user_profile"]) if d.get("user_profile") else None
        return cls(
            video=video,
            thread=thread,
            user_profile=user_profile,
            memory_context=d.get("memory_context", []),
            bot_thread_replies=d.get("bot_thread_replies", []),
            related_area_history=d.get("related_area_history", []),
            recent_bot_actions=d.get("recent_bot_actions", []),
            mood=d.get("mood", ""),
            video_context_complete=d.get("video_context_complete", False),
        )

    def to_prompt_text(self) -> str:
        """将完整上下文转为 prompt 文本块"""
        parts = []

        if self.video:
            parts.append(self.video.to_prompt_text())
            if not self.video_context_complete:
                parts.append("⚠️ 视频上下文不完整，不得编造视频细节。")

        if self.thread:
            parts.append(self.thread.to_prompt_text())

        if self.user_profile:
            parts.append(self.user_profile.to_prompt_text())

        if self.memory_context:
            parts.append("【相关长期记忆】\n" + "\n".join(f"- {m}" for m in self.memory_context))

        if self.bot_thread_replies:
            parts.append("【Bot 本线历史回复】\n" + "\n".join(f"- {r}" for r in self.bot_thread_replies))

        if self.related_area_history:
            parts.append("【评论区相关历史】\n" + "\n".join(f"- {h}" for h in self.related_area_history))

        if self.recent_bot_actions:
            parts.append("【Bot 最近行为】\n" + "\n".join(f"- {a}" for a in self.recent_bot_actions))

        if self.mood:
            parts.append(f"【当前心情】{self.mood}")

        return "\n".join(parts)


# ═══════════════════════════════════════════════
# 审计模型
# ═══════════════════════════════════════════════

@dataclass
class GenerationAudit:
    """生成审计记录"""
    id: str = ""
    scene: str = ""
    persona_id: str = ""
    input_summary: str = ""
    context_summary: str = ""
    prompt_preview: str = ""
    output: str = ""
    published: bool = False
    target: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GenerationAudit":
        return cls(
            id=d.get("id", ""),
            scene=d.get("scene", ""),
            persona_id=d.get("persona_id", ""),
            input_summary=d.get("input_summary", ""),
            context_summary=d.get("context_summary", ""),
            prompt_preview=d.get("prompt_preview", ""),
            output=d.get("output", ""),
            published=d.get("published", False),
            target=d.get("target", {}),
            created_at=d.get("created_at", ""),
        )


# ═══════════════════════════════════════════════
# 互动建议 DTO（VID-501）
# ═══════════════════════════════════════════════

from bilibot.models.interaction import InteractionSuggestion


# ═══════════════════════════════════════════════
# 生成结果（REP-501 / PRD-V5 §6.1）
# ═══════════════════════════════════════════════

from .generation import (
    GenerationOutcome,
    STATUS_GENERATED,
    STATUS_SKIP,
    STATUS_RETRYABLE_ERROR,
    STATUS_PERMANENT_ERROR,
    LLM_NOT_CONFIGURED,
    LLM_CLIENT_UNAVAILABLE,
    LLM_TIMEOUT,
    LLM_CONNECTION_ERROR,
    LLM_RATE_LIMITED,
    LLM_SERVER_ERROR,
    LLM_UNKNOWN_ERROR,
    MODEL_EMPTY_REPLY,
)


# ═══════════════════════════════════════════════
# 公共导出
# ═══════════════════════════════════════════════

__all__ = [
    "SceneType",
    "MemoryLevel",
    "PersonaExample",
    "Persona",
    "VideoContext",
    "CommentItem",
    "CommentThread",
    "UserProfile",
    "ReplyContext",
    "GenerationAudit",
    "InteractionSuggestion",
    "GenerationOutcome",
    "STATUS_GENERATED",
    "STATUS_SKIP",
    "STATUS_RETRYABLE_ERROR",
    "STATUS_PERMANENT_ERROR",
    "LLM_NOT_CONFIGURED",
    "LLM_CLIENT_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_CONNECTION_ERROR",
    "LLM_RATE_LIMITED",
    "LLM_SERVER_ERROR",
    "LLM_UNKNOWN_ERROR",
    "MODEL_EMPTY_REPLY",
]
