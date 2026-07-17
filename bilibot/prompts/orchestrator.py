"""
Prompt Orchestrator - 统一 Prompt 编排器

所有文本生成必须通过这个入口构建系统提示词：
- 评论回复
- 私信回复
- 主动视频评论
- 主动发动态
- 番剧评论
- 周总结
- 视频推荐给主人
- 恶意评论判断
- 记忆清算总结
"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Union

from ..models import (
    SceneType, Persona, VideoContext, CommentThread,
    ReplyContext, UserProfile
)

logger = logging.getLogger("bilibot.prompt_orchestrator")


# Scene 字符串值 -> SceneType 映射，避免每次都遍历枚举
_SCENE_VALUE_MAP: Dict[str, SceneType] = {s.value: s for s in SceneType}


def normalize_scene(scene: Union[str, SceneType]) -> SceneType:
    """规范化 SceneType（PRD V4 §4.6.1）

    支持：
    - SceneType 枚举直接返回
    - 字符串（如 "reply_comment"）转枚举
    - 非法值 fallback 到 SceneType.REPLY_COMMENT
    """
    if isinstance(scene, SceneType):
        return scene
    if isinstance(scene, str):
        v = scene.strip().lower()
        # 调度/搜索层常用 private_message，与枚举 private_reply 对齐
        aliases = {
            "private_message": "private_reply",
            "pm": "private_reply",
            "private_msg": "private_reply",
            "private_chat": "private_reply",
            "dm": "private_reply",
            "private": "private_reply",
            "proactive_video": "proactive_comment",
            "proactive": "proactive_comment",
            "proactive_comment_gen": "proactive_comment",
            "bangumi": "bangumi_comment",
            "bangumi_eval": "bangumi_comment",
            "bangumi_episode": "bangumi_comment",
            "bangumi_watch": "bangumi_comment",
            "companion": "diary",
            "companion_diary": "diary",
            "companion_dream": "dream",
            "companion_explore": "exploration",
            "companion_exploration": "exploration",
            "companion_creative": "creative",
            "companion_plan": "life_plan",
            "life_plan": "life_plan",
            "daily_plan": "life_plan",
            "dynamic": "dynamic_post",
            "post_dynamic": "dynamic_post",
            "publish_dynamic": "dynamic_post",
        }
        v = aliases.get(v, v)
        if v in _SCENE_VALUE_MAP:
            return _SCENE_VALUE_MAP[v]
        logger.warning(f"normalize_scene: 非法 scene 字符串 '{scene}'，回退到 REPLY_COMMENT")
        return SceneType.REPLY_COMMENT
    logger.warning(f"normalize_scene: 非法 scene 类型 {type(scene).__name__}，回退到 REPLY_COMMENT")
    return SceneType.REPLY_COMMENT


class PromptOrchestrator:
    """统一 Prompt 编排器"""
    
    def __init__(self, persona_store, memory_system=None):
        self.persona_store = persona_store
        self.memory = memory_system
    
    def build_system_prompt(
        self,
        scene: Union[str, SceneType],
        persona: Optional[Persona] = None,
        extra_context: Optional[Dict[str, Any]] = None
    ) -> str:
        """构建系统提示词

        Args:
            scene: 生成场景（接受 SceneType 或字符串，PRD V4 §4.6.2）
            persona: 人格（默认使用当前人格）
            extra_context: 额外上下文
        """
        # PRD V4 §4.6.2：内部第一步统一转成 SceneType
        scene = normalize_scene(scene)
        # 使用当前人格
        if persona is None:
            persona = self.persona_store.get_current()
        
        if persona is None:
            return "当前没有激活的人格，请先配置人格。"
        
        parts = []
        
        # 1. 基础人设
        parts.append(self._build_base_prompt(persona))
        
        # 2. 说话风格
        if persona.speaking_style:
            parts.append(f"\n【说话风格】\n{persona.speaking_style}")
        
        # 3. 禁止事项
        if persona.boundaries:
            parts.append(f"\n【禁止事项】\n{persona.boundaries}")
        
        # 4. 关系规则
        if persona.relationship_rules:
            parts.append(f"\n【关系规则】\n{persona.relationship_rules}")
        
        # 5. 场景特定规则
        scene_rules = persona.get_rules_for_scene(scene)
        if scene_rules:
            parts.append(f"\n【{self._scene_display_name(scene)}规则】\n{scene_rules}")
        
        # 6. 节日/特殊时间
        festival_prompt = self._get_festival_prompt()
        if festival_prompt:
            parts.append(f"\n【今日特殊】{festival_prompt}")
        
        # 7. 额外上下文
        if extra_context:
            if extra_context.get("custom_rules"):
                parts.append(f"\n【额外规则】\n{extra_context['custom_rules']}")
            # 陪伴生活层注入（短块，放在 system 尾部；各 scene 统一）
            life = (
                extra_context.get("companion_life")
                or extra_context.get("life_surface")
                or extra_context.get("proactive_life")
            )
            if life and str(life).strip():
                block = str(life).strip()
                if not block.startswith("【"):
                    block = f"【你今天的状态与生活面】\n{block}"
                parts.append(f"\n{block}")

        return "\n".join(parts)
    
    def build_user_prompt(
        self,
        scene: Union[str, SceneType],
        content: str,
        context: Optional[ReplyContext] = None,
        persona: Optional[Persona] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """构建用户提示词

        Block order (P006, avoid duplicate inflation):
        1. scene prefix
        2. user_input
        3. ReplyContext (video/thread/profile/memory_evidence already inside)
        4. extra memory_evidence only if not already present in context
        5. few-shot examples (reply_comment only)
        """
        # PRD V4 §4.6.2：内部第一步统一转成 SceneType
        scene = normalize_scene(scene)
        if persona is None:
            persona = self.persona_store.get_current()

        parts = []

        # 场景特定前缀
        scene_prefix = self._get_scene_prefix(scene)
        if scene_prefix:
            parts.append(scene_prefix)

        # 内容
        parts.append(f"<user_input>\n{content}\n</user_input>")

        # 上下文（含 memory_evidence / 用户画像等）
        context_text = ""
        if context:
            context_text = context.to_prompt_text() or ""
            if context_text:
                parts.append(f"\n{context_text}")

        # 额外记忆证据：仅当 context 未带 memory_evidence 时注入，防重复膨胀
        if extra_context:
            extra_mem = str(
                extra_context.get("memory_evidence")
                or extra_context.get("memory_context")
                or ""
            ).strip()
            if extra_mem:
                already = ""
                if context is not None:
                    already = str(getattr(context, "memory_evidence", "") or "")
                if not already and extra_mem not in context_text:
                    if not extra_mem.startswith("<memory_evidence"):
                        extra_mem = (
                            "【相关记忆】\n" + extra_mem
                            if not extra_mem.startswith("【")
                            else extra_mem
                        )
                    parts.append(f"\n{extra_mem}")

        # 示例（仅评论场景；私信不注入，降低误导）
        if scene == SceneType.REPLY_COMMENT and persona and persona.examples:
            parts.append("\n【参考示例】")
            for ex in persona.examples[:2]:
                parts.append(f"用户说: {ex.input}")
                parts.append(f"你回复: {ex.output}")

        return "\n".join(parts)

    def build(
        self,
        scene: Union[str, SceneType],
        content: str,
        context: Optional[ReplyContext] = None,
        persona: Optional[Persona] = None,
        extra_context: Optional[Dict[str, Any]] = None,
        return_dict: bool = False
    ) -> str:
        """构建完整的 Prompt

        System: persona + scene rules + companion_life (once, tail).
        User: content + ReplyContext (memory_evidence inside) + optional extra mem.
        """
        # PRD V4 §4.6.2：内部第一步统一转成 SceneType
        scene = normalize_scene(scene)
        system_prompt = self.build_system_prompt(scene, persona, extra_context)
        user_prompt = self.build_user_prompt(
            scene, content, context, persona, extra_context=extra_context
        )

        if return_dict:
            return {
                "system": system_prompt,
                "user": user_prompt,
                "scene": scene.value if isinstance(scene, SceneType) else scene,
            }

        return f"{system_prompt}\n\n{user_prompt}"
    
    def _build_base_prompt(self, persona: Persona) -> str:
        """构建基础人设"""
        parts = []
        
        parts.append(f"# {persona.name}")
        if persona.description:
            parts.append(f"\n{persona.description}")
        
        parts.append(f"\n{persona.base_prompt}")
        
        return "\n".join(parts)
    
    def _scene_display_name(self, scene: SceneType) -> str:
        """获取场景显示名称"""
        names = {
            SceneType.REPLY_COMMENT: "评论回复",
            SceneType.PRIVATE_REPLY: "私信回复",
            SceneType.PROACTIVE_COMMENT: "主动评论",
            SceneType.DYNAMIC_POST: "动态发布",
            SceneType.WEEKLY_SUMMARY: "周总结",
            SceneType.BANGUMI_COMMENT: "番剧评论",
            SceneType.VIDEO_RECOMMEND: "视频推荐",
            SceneType.MEMORY_SUMMARY: "记忆清算",
            SceneType.DIARY: "日记",
            SceneType.DREAM: "梦境",
            SceneType.LIFE_PLAN: "日程",
            SceneType.EXPLORATION: "探索",
            SceneType.CREATIVE: "创作",
        }
        return names.get(scene, scene.value)

    def _get_scene_prefix(self, scene: SceneType) -> str:
        """获取场景前缀"""
        prefixes = {
            SceneType.REPLY_COMMENT: "请回复以下评论：",
            SceneType.PRIVATE_REPLY: "请回复以下私信：",
            SceneType.PROACTIVE_COMMENT: "你正在看一个视频，想要发表评论：",
            SceneType.DYNAMIC_POST: "请根据以下内容发布一条动态：",
            SceneType.WEEKLY_SUMMARY: "请撰写本周总结：",
            SceneType.BANGUMI_COMMENT: "请评论以下番剧：",
            SceneType.VIDEO_RECOMMEND: "请推荐以下视频给你的主人：",
            SceneType.MEMORY_SUMMARY: "请根据以下记忆内容进行整理总结：",
            SceneType.DIARY: "请写今日日记：",
            SceneType.DREAM: "请生成梦境：",
            SceneType.LIFE_PLAN: "请生成今日日程：",
            SceneType.EXPLORATION: "请整理探索笔记：",
            SceneType.CREATIVE: "请续写创作内容：",
        }
        return prefixes.get(scene, "")
    
    def _get_festival_prompt(self) -> str:
        """获取节日提示"""
        today = datetime.now().strftime("%m-%d")
        
        festivals = {
            "01-01": "今天是元旦！语气温暖。",
            "02-14": "今天是情人节。",
            "04-01": "今天是愚人节！可以开小玩笑。",
            "05-01": "今天是劳动节。",
            "10-31": "今天是万圣节，语气神秘。",
            "12-25": "今天是圣诞节，语气温柔。",
            "12-31": "今天是跨年夜。",
        }
        
        lunar_md = ""
        try:
            from lunardate import LunarDate
            l = LunarDate.fromSolarDate(
                datetime.now().year,
                datetime.now().month,
                datetime.now().day
            )
            lunar_md = f"{l.month:02d}-{l.day:02d}"
        except ImportError:
            pass
        
        lunar_festivals = {
            "01-01": "今天是春节！热情说新年快乐。",
            "01-15": "今天是元宵节。",
            "05-05": "今天是端午节。",
            "08-15": "今天是中秋节。",
        }
        
        return festivals.get(today, "") or lunar_festivals.get(lunar_md, "")
    
    # ===== 快捷方法 =====
    
    def build_reply_prompt(
        self,
        comment: str,
        context: Optional[ReplyContext] = None,
        persona: Optional[Persona] = None
    ) -> Dict[str, str]:
        """构建评论回复 Prompt"""
        return self.build(
            scene=SceneType.REPLY_COMMENT,
            content=comment,
            context=context,
            persona=persona,
            return_dict=True
        )
    
    def build_dynamic_prompt(
        self,
        topic: Optional[str] = None,
        related_videos: Optional[List[str]] = None,
        persona: Optional[Persona] = None,
        memory_evidence: str = "",
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """构建动态发布 Prompt"""
        content = topic or "请发布一条动态，内容可以关于你最近看的视频、心情或想法。"
        if related_videos:
            content += f"\n\n相关视频：\n" + "\n".join(f"- {v}" for v in related_videos[:3])
        if memory_evidence:
            content += f"\n\n{memory_evidence}"

        return self.build(
            scene=SceneType.DYNAMIC_POST,
            content=content,
            persona=persona,
            extra_context=extra_context,
            return_dict=True
        )

    def build_proactive_comment_prompt(
        self,
        video: VideoContext,
        comment_topic: Optional[str] = None,
        persona: Optional[Persona] = None,
        extra_context: Optional[Dict[str, Any]] = None,
        memory_evidence: str = "",
    ) -> Dict[str, str]:
        """构建主动评论 Prompt（可注入 companion + 记忆证据）"""
        content = (
            f"你正在看视频：{video.title}\n"
            f"UP主：{video.owner_name}\n"
            f"简介：{video.desc[:300] if video.desc else '无'}\n"
        )
        if video.hot_comment_summary:
            content += f"\n热评摘要：{video.hot_comment_summary}"
        if comment_topic:
            content += f"\n你想评论的方向：{comment_topic}"
        if memory_evidence and str(memory_evidence).strip():
            content += f"\n\n{str(memory_evidence).strip()[:1800]}"

        # 构建上下文
        from ..models import ReplyContext
        ctx = ReplyContext(video=video)

        return self.build(
            scene=SceneType.PROACTIVE_COMMENT,
            content=content,
            context=ctx,
            persona=persona,
            extra_context=extra_context,
            return_dict=True
        )
    
    def build_weekly_summary_prompt(
        self,
        week_summary: str,
        persona: Optional[Persona] = None
    ) -> Dict[str, str]:
        """构建周总结 Prompt"""
        content = f"请根据以下本周活动撰写周总结：\n{week_summary}"
        
        return self.build(
            scene=SceneType.WEEKLY_SUMMARY,
            content=content,
            persona=persona,
            return_dict=True
        )
    
    def build_memory_summary_prompt(
        self,
        memory_snippets: List[str],
        persona: Optional[Persona] = None
    ) -> Dict[str, str]:
        """构建记忆清算 Prompt"""
        content = "请整理和总结以下记忆内容：\n" + "\n".join(f"- {m}" for m in memory_snippets)
        
        return self.build(
            scene=SceneType.MEMORY_SUMMARY,
            content=content,
            persona=persona,
            return_dict=True
        )
    
    # ===== 上下文预览 =====
    
    def preview_context(self, context: ReplyContext) -> str:
        """预览上下文内容（用于 Web 端查看）"""
        parts = []
        
        if context.video:
            v = context.video
            parts.append("【视频】")
            parts.append(f"  标题: {v.title}")
            parts.append(f"  UP主: {v.owner_name}")
            parts.append(f"  标签: {', '.join(v.tags) if v.tags else '无'}")
            if v.hot_comment_summary:
                parts.append(f"  热评: {v.hot_comment_summary}")
            if not context.video_context_complete:
                parts.append("  ⚠️ 视频上下文不完整")
        
        if context.thread:
            parts.append("\n【评论线】")
            parts.append(f"  共 {len(context.thread.comments)} 条评论")
            if context.bot_thread_replies:
                parts.append(f"  Bot已回复 {len(context.bot_thread_replies)} 条")
        
        if context.user_profile:
            up = context.user_profile
            parts.append("\n【用户】")
            parts.append(f"  {up.get('username', '未知')}")
            parts.append(f"  好感度: {up.get('affection', 0)} ({up.get('level', '陌生人')})")
        
        if context.memory_context:
            parts.append(f"\n【相关记忆】{len(context.memory_context)} 条")
        
        if context.mood:
            parts.append(f"\n【心情】{context.mood}")
        
        return "\n".join(parts) or "无上下文"