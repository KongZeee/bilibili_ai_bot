"""
用户状态系统 - UserStateSystem

从 MemorySystem 拆出的纯 JSON 读写模块，负责：
1. 用户画像（user_profiles.json）
2. 好感度（affection.json）
3. 心情（mood.json）

不依赖 LLMAdapter，不涉及记忆存储/检索（由账号级 MemoryBrainService 负责）。
"""
import logging
import random
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any

from .data_store import DataStore

logger = logging.getLogger("bilibot.user_state")

# 文件常量（与旧 MemorySystem 保持一致，便于复用已有数据）
USER_PROFILE_FILE = "user_profiles.json"
AFFECTION_FILE = "affection.json"
MOOD_FILE = "mood.json"


class UserStateSystem:
    """用户状态系统：画像 / 好感度 / 心情"""

    def __init__(self, data_store: DataStore, config):
        self.ds = data_store
        self.config = config
        # 好感度内存缓存
        self._affection: Dict[str, int] = self._load_affection()

    # ══════════════════════════════════════
    #  JSON 读写辅助
    # ══════════════════════════════════════

    def _save_json(self, filename: str, data: Any):
        self.ds.save_json(filename, data)

    def _load_json(self, filename: str, default: Any = None) -> Any:
        return self.ds.load_json(filename, default)

    # ══════════════════════════════════════
    #  用户画像
    # ══════════════════════════════════════

    def get_user_profile_context(self, mid: str) -> str:
        """获取用户画像上下文（用于注入 LLM prompt）"""
        profiles = self._load_json(USER_PROFILE_FILE, {})
        p = profiles.get(str(mid))
        if not p:
            return ""

        entries = []
        if p.get("username"):
            entries.append(f"昵称：{p['username']}")
        if p.get("facts"):
            for f in p["facts"][-10:]:
                entries.append(f"事实：{f}")
        if p.get("tags"):
            entries.append(f"标签：{', '.join(p['tags'])}")
        if p.get("impression"):
            entries.append(f"印象：{p['impression']}")

        return "【对该用户的了解】\n" + "\n".join(entries) if entries else ""

    def update_user_profile(
        self,
        mid: str,
        username: Optional[str] = None,
        impression: Optional[str] = None,
        new_facts: Optional[List[str]] = None,
        new_tags: Optional[List[str]] = None,
    ):
        """更新用户画像"""
        profiles = self._load_json(USER_PROFILE_FILE, {})
        uid = str(mid)

        if uid not in profiles:
            profiles[uid] = {"username": "", "impression": "", "facts": [], "tags": []}

        if username and not profiles[uid].get("username"):
            profiles[uid]["username"] = username
        if impression:
            profiles[uid]["impression"] = impression
        if new_facts:
            ex = profiles[uid].setdefault("facts", [])
            for f in new_facts:
                f = f.strip()
                if f and f not in ex:
                    ex.append(f)
            profiles[uid]["facts"] = ex[-20:]
        if new_tags:
            et = profiles[uid].setdefault("tags", [])
            for t in new_tags:
                t = t.strip()
                if t and t not in et:
                    et.append(t)
            profiles[uid]["tags"] = et[-10:]

        self._save_json(USER_PROFILE_FILE, profiles)

    # ══════════════════════════════════════
    #  好感度
    # ══════════════════════════════════════

    def _load_affection(self) -> Dict[str, int]:
        return self.ds.load_json(AFFECTION_FILE, {})

    def _save_affection(self):
        self.ds.save_json(AFFECTION_FILE, self._affection)

    def get_affection(self, user_id: str) -> int:
        """获取用户好感度"""
        return self._affection.get(str(user_id), 0)

    def update_affection(self, user_id: str, delta: int):
        """增减好感度（范围 -100 ~ 100）"""
        uid = str(user_id)
        old = self._affection.get(uid, 0)
        new = max(-100, min(100, old + delta))
        self._affection[uid] = new
        self._save_affection()
        return old, new

    def get_level(self, score: int, mid: Optional[str] = None) -> str:
        """根据好感度分数获取关系等级"""
        owner = self.config.personality.owner_mid
        if mid and owner and str(mid).strip() == owner.strip():
            return "special"

        if score <= -10:
            return "cold"
        if score >= 51:
            return "close"
        if score >= 31:
            return "friend"
        if score >= 11:
            return "normal"
        return "stranger"

    def get_level_prompt(self, level: str) -> str:
        """获取等级对应的行为提示"""
        owner = self.config.personality.owner_name or "主人"
        defaults = {
            "special": f"这是你的主人{owner}。内心：深深的喜爱和依恋。外在：随意、自然、可以撒娇。",
            "close": "这是你的好友（好感度高）。内心：真诚关心。外在：温柔亲近。",
            "friend": "这是熟悉的粉丝（好感度中）。内心：放松和信任。外在：自然，话变多。",
            "normal": "这是普通粉丝（好感度低）。保持善意，温和有礼但保持距离。",
            "stranger": "这是陌生人。保持礼貌和善意，简洁客气。",
            "cold": "这个人多次恶意攻击你。平静坚定划清界限，回复极简短。",
        }
        return defaults.get(level, defaults["stranger"])

    # ══════════════════════════════════════
    #  心情
    # ══════════════════════════════════════

    def get_today_mood(self) -> Tuple[str, str]:
        """获取今日心情（每天随机一次，之后缓存）

        PRD V4 CFG-003：使用 features.mood 替代 personality.enable_mood
        旧字段 personality.enable_mood 仅用于迁移兼容

        Returns:
            (mood_str, mood_prompt)
        """
        # PRD V4 CFG-003：优先读 features.mood，兼容旧 personality.enable_mood
        features_cfg = getattr(self.config, "features", None)
        mood_enabled = True
        if features_cfg is not None:
            mood_enabled = getattr(features_cfg, "mood", True)
        elif hasattr(self.config, "personality"):
            mood_enabled = getattr(self.config.personality, "enable_mood", True)
        if not mood_enabled:
            return "🌙 平静如常", ""

        md = self._load_json(MOOD_FILE, {})
        today = datetime.now().strftime("%Y-%m-%d")

        if md.get("date") == today:
            return md["mood"], md.get("mood_prompt", "")

        moods = [
            ("☀️ 心情不错", "语气稍微轻快一点。"),
            ("🌙 平静如常", "按正常性格回复。"),
            ("🌧️ 有点安静", "话少一点。"),
            ("😏 有点皮", "偶尔多一点调侃。"),
            ("🧊 懒得废话", "回复更简洁。"),
        ]
        mood, prompt = random.choice(moods)
        self._save_json(MOOD_FILE, {"date": today, "mood": mood, "mood_prompt": prompt})
        return mood, prompt
