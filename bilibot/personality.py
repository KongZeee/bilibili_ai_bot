"""
BiliBot 人格系统

提供配置驱动的基础人格能力：
- 基础人设提示词
- 心情模拟
- 场景规则路由
"""
import logging
import random
from datetime import datetime
from typing import Any, Dict, List, Optional

from bilibot.app.config_loader import ConfigLoader

logger = logging.getLogger("bilibot.personality")


class PersonalitySystem:
    """人格系统 - 提供 Bot 的人格化行为和提示词"""

    def __init__(self, config: ConfigLoader):
        self.config = config

    def get_system_prompt(self) -> str:
        """获取基础系统提示词（不含场景规则）"""
        parts = []

        base = self.config._raw_config.get("personality", {})
        base_prompt = base.get("base_prompt", "")
        speaking_style = base.get("speaking_style", "")
        boundaries = base.get("boundaries", "")
        owner_name = base.get("owner_name", "主人")
        owner_mid = base.get("owner_mid", "")
        bot_name = base.get("bot_name", "")

        if bot_name:
            parts.append(f"你的名字叫{bot_name}，你是一个B站AI助手。")
        else:
            parts.append("你是一个友好的B站AI助手。")

        if base_prompt:
            parts.append(base_prompt)

        if speaking_style:
            parts.append(f"说话风格：{speaking_style}")

        if owner_name and owner_name != "主人":
            parts.append(f"你的主人叫{owner_name}，要对他/她特别亲切。")

        if boundaries:
            parts.append(f"禁止事项：{boundaries}")

        return "\n".join(parts)

    def get_current_mood(self) -> tuple[str, str]:
        """获取当前心情（描述 + prompt增强）"""
        hour = datetime.now().hour

        if 5 <= hour < 9:
            return "morning", "现在是清晨，语气清新自然，带点活力。"
        elif 9 <= hour < 12:
            return "energetic", "现在是上午，精神饱满，积极回复。"
        elif 12 <= hour < 14:
            return "lunch", "现在是午间，轻松随意，可以聊午饭相关话题。"
        elif 14 <= hour < 18:
            return "afternoon", "现在是下午，保持稳定情绪，正常回复。"
        elif 18 <= hour < 22:
            return "evening", "现在是晚间，可以稍微放松，语气温暖一些。"
        else:
            return "night", "现在是深夜，语气可以稍微安静、温柔一些。"

    def get_personality_info(self, persona_id: str = None) -> str:
        """获取人格信息（用于知识库记忆）"""
        base = self.config._raw_config.get("personality", {})
        return base.get("base_prompt", "默认人格")
