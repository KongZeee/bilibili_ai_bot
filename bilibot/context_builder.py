"""
ContextBuilder - 上下文包构建器

按 PRD §5.5 评论回复上下文优先级组装完整上下文：
  1. 视频/动态内容
  2. 评论线历史
  3. Bot 在该线的历史回复
  4. 同一视频相关评论（按用户分组）
  5. 当前用户画像 / 好感度
  6. 相关长期记忆
  7. Bot 最近主动行为
  8. 当前人格 + 心情

PRD-V5 §5.1 ACC-502：每个账号拥有独立 ContextBuilder，
注入该账号的 DataStore / UserState / BiliClient / KnowledgeMemory，
build() 校验 account_id 隔离，防止跨账号读取 recent behavior。
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bilibot.context")


class IsolationError(RuntimeError):
    """ACC-502：账号隔离校验失败

    ContextBuilder.build() 传入的 account_id 与实例绑定的 account_id
    不一致时抛出，阻止跨账号上下文构建。
    """


def _value(obj, key, default=None):
    """统一读取 helper：兼容 dict 与 dataclass"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _first_value(obj, keys, default=None):
    """按多个候选字段名读取第一个非空值"""
    for key in keys:
        value = _value(obj, key, None)
        if value not in (None, "", [], {}):
            return value
    return default


class ContextBuilder:
    """上下文包构建器"""

    def __init__(self, data_store=None, user_state=None,
                 persona_store=None, bili=None, config: dict | None = None,
                 knowledge_memory=None, account_id: str = "", companion=None):
        self.ds = data_store
        self.user_state = user_state
        self.persona_store = persona_store
        self.bili = bili
        self.config = config or {}
        # ACC-502：账号隔离字段，build() 会校验传入 account_id 与此一致
        self.knowledge_memory = knowledge_memory
        self.account_id = account_id
        # 账号级陪伴生活层（可选）；enabled=false 时 get_prompt_surface 返回空
        self.companion = companion

    # ── 主入口 ──

    def build(self, context: "ReplyContext", account_id: str = "") -> dict[str, Any]:
        """组装完整上下文包，返回结构化字典

        兼容 dataclass 与 dict 两种输入（PRD V3 §7）。

        Args:
            context: 回复上下文
            account_id: PRD V3 §4.10 多账号场景下，优先用账号绑定的人格，
                        避免共享单例的 get_current() 竞态返回错误人格

        Raises:
            IsolationError: ACC-502 当传入 account_id 与实例绑定的 account_id
                            都非空且不一致时，阻止跨账号上下文构建。
        """
        # ACC-502：账号隔离校验 —— 实例绑定账号与调用方传入账号必须一致
        if self.account_id and account_id and self.account_id != account_id:
            raise IsolationError(
                f"ContextBuilder account_id mismatch: instance={self.account_id!r} "
                f"but build() called with account_id={account_id!r}; "
                f"refusing to build cross-account context"
            )

        parts: list[str] = []
        meta: dict[str, Any] = {"sources": [], "video_ctx_complete": True}

        # 顶层字段统一用 _value 读取，兼容 dict / dataclass
        video = _value(context, "video")
        thread = _value(context, "thread")
        user_profile = _value(context, "user_profile")
        memory_context = _value(context, "memory_context", []) or []
        memory_evidence = _value(context, "memory_evidence", "") or ""
        bot_thread_replies = _value(context, "bot_thread_replies", []) or []
        context_recent_actions = _value(context, "recent_bot_actions", []) or []
        mood = _value(context, "mood", "")
        video_context_complete = _value(context, "video_context_complete", True)

        # 1. 视频 / 动态
        if video:
            v = video
            title = _first_value(v, ["title"], "未知")
            owner_name = _first_value(v, ["owner_name", "up_name", "owner"], "未知")
            category = _first_value(v, ["category"], "未知")
            desc = _first_value(v, ["desc", "description"], "") or ""
            tags = _first_value(v, ["tags"], []) or []
            hot_comment_summary = _first_value(v, ["hot_comment_summary"], "")
            subtitle_summary = _first_value(v, ["subtitle_summary"], "")
            bot_review = _first_value(v, ["bot_review"], "")

            video_block = (
                f"【视频信息】\n"
                f"标题: {title}\n"
                f"UP主: {owner_name}\n"
                f"分区: {category}\n"
                f"简介: {(desc or '无')[:400]}\n"
                f"标签: {', '.join(tags) if tags else '无'}\n"
            )
            if hot_comment_summary:
                video_block += f"热评摘要: {hot_comment_summary}\n"
            if subtitle_summary:
                video_block += f"字幕摘要: {subtitle_summary}\n"
            if bot_review:
                video_block += f"历史分析: {bot_review}\n"
            if not video_context_complete:
                video_block += "⚠️ 视频上下文不足：不得编造视频细节。\n"
                meta["video_ctx_complete"] = False
            parts.append(video_block)
            meta["sources"].append("video")

        # 2. 评论线
        if thread:
            t = thread
            comments = _first_value(t, ["comments"], []) or []
            thread_block = f"【评论线】共 {len(comments)} 条\n"
            for i, c in enumerate(comments[:20], 1):
                username = _first_value(c, ["username", "user", "uname"], "?")
                text = _first_value(c, ["content", "text", "message"], "")
                thread_block += f"  [{i}] {username}: {str(text)[:120]}\n"
            if bot_thread_replies:
                thread_block += (
                    f"Bot 已回复 {len(bot_thread_replies)} 条: " +
                    "; ".join(r[:80] for r in bot_thread_replies[:5])
                ) + "\n"
            parts.append(thread_block)
            meta["sources"].append("thread")

        # 3. 用户画像
        if user_profile:
            up = user_profile
            username = _first_value(up, ["username", "name", "uname"], "未知")
            affection = _first_value(up, ["affection"], 0)
            level = _first_value(up, ["level"], "陌生人")
            notes = _first_value(up, ["notes", "facts"], []) or []
            tags = _first_value(up, ["tags"], []) or []

            user_block = (
                f"【用户】{username}\n"
                f"好感度: {affection} ({level})\n"
            )
            if tags:
                user_block += f"标签: {', '.join(tags)}\n"
            if notes:
                user_block += f"已知事实: {'; '.join(notes[:3])}\n"
            parts.append(user_block)
            meta["sources"].append("user_profile")

        # 4. 相关长期记忆（V6 evidence 优先；legacy memory_context 仅作补充且去重）
        if memory_evidence:
            parts.append(str(memory_evidence))
            meta["sources"].append("memory_brain")
        if memory_context:
            # Skip lines already covered by the evidence block to avoid inflation
            evidence_blob = str(memory_evidence or "")
            extra_lines = []
            for m in memory_context[:5]:
                line = str(m or "").strip()
                if not line:
                    continue
                if evidence_blob and line[:40] in evidence_blob:
                    continue
                extra_lines.append(f"  - {line}")
            if extra_lines:
                parts.append("【相关记忆补充】\n" + "\n".join(extra_lines))
                meta["sources"].append("memory")
        if memory_evidence or memory_context:
            meta["memory_present"] = True
        else:
            meta["memory_present"] = False

        # 5. Bot 最近主动行为
        # The V6 activity context supplies account-brain actions. Keep the
        # legacy DataStore lane as a compatibility supplement, not as the sole
        # source of what the Bot just did.
        recent: list[str] = []
        for item in [*context_recent_actions, *self._get_recent_actions(limit=5)]:
            line = str(item or "").strip()
            if line and line not in recent:
                recent.append(line)
            if len(recent) >= 8:
                break
        if recent:
            parts.append("【Bot 近期行为】\n  " + "\n  ".join(recent))
            meta["sources"].append("recent_actions")

        # 6. 人格 + 心情
        # PRD V3 §4.10：多账号并发时优先用账号绑定的人格，避免 get_current() 竞态
        persona = None
        if self.persona_store:
            if account_id and hasattr(self.persona_store, "get_persona_for_account"):
                persona = self.persona_store.get_persona_for_account(account_id)
            if not persona:
                persona = self.persona_store.get_current()
        if persona:
            parts.append(f"【当前人格】{persona.name}: {persona.description or ''}")
        if mood:
            parts.append(f"【当前心情】{mood}")

        # 7. 陪伴生活层（账号级，可选；enabled=false 不注入、不挡主链路）
        companion_surface = ""
        try:
            companion = getattr(self, "companion", None)
            if companion is not None and getattr(companion, "enabled", False):
                # Prefer prompt_surface; fall back to proactive block for richer seed
                getter = getattr(companion, "get_prompt_surface", None)
                if callable(getter):
                    companion_surface = getter() or ""
                if not companion_surface:
                    pro = getattr(companion, "build_proactive_context_block", None)
                    if callable(pro):
                        companion_surface = pro() or ""
                if companion_surface:
                    parts.append(str(companion_surface).strip())
                    meta["sources"].append("companion_life")
        except Exception:
            companion_surface = ""

        return {
            "text": "\n".join(parts),
            "meta": meta,
            "persona": persona,
            "companion_life": companion_surface,
        }

    # ── 私有辅助 ──

    def _get_recent_actions(self, limit: int = 5) -> list[str]:
        """从 data_store 获取 Bot 最近主动行为"""
        if not self.ds:
            return []
        try:
            recent = self.ds.get_recent_actions(limit=limit)
            return [a.get("summary", "") for a in recent if a.get("summary")]
        except Exception:
            return []
