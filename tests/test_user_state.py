"""
tests/test_user_state.py - UserStateSystem 单元测试

覆盖从旧 MemorySystem 拆出的功能：
- 用户画像（get_user_profile_context / update_user_profile）
- 好感度（get_affection / update_affection / get_level / get_level_prompt）
- 心情（get_today_mood）
"""
import pytest

from bilibot.user_state import UserStateSystem


def _cfg(enable_mood=True, owner_mid="999", owner_name="主人"):
    """构造最小配置对象"""
    return type("C", (), {
        "personality": type("P", (), {
            "owner_mid": owner_mid,
            "owner_name": owner_name,
            "enable_mood": enable_mood,
        })()
    })()


class TestUserProfile:
    def test_get_user_profile_context_empty(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg())
        assert us.get_user_profile_context("u1") == ""

    def test_get_user_profile_context_populated(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        ds.save_json("user_profiles.json", {
            "u1": {
                "username": "test",
                "facts": ["喜欢编程"],
                "tags": ["技术宅"],
                "impression": "友善",
            }
        })
        us = UserStateSystem(ds, _cfg())
        ctx = us.get_user_profile_context("u1")
        assert "test" in ctx
        assert "喜欢编程" in ctx
        assert "技术宅" in ctx
        assert "友善" in ctx

    def test_update_user_profile(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg())
        us.update_user_profile("u1", username="小明", new_facts=["喜欢动漫"], new_tags=["老粉"])
        ctx = us.get_user_profile_context("u1")
        assert "小明" in ctx
        assert "喜欢动漫" in ctx
        assert "老粉" in ctx

    def test_update_user_profile_dedup_facts(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg())
        us.update_user_profile("u1", new_facts=["事实A"])
        us.update_user_profile("u1", new_facts=["事实A"])  # 重复
        ctx = us.get_user_profile_context("u1")
        assert ctx.count("事实A") == 1


class TestAffection:
    def test_affection_update(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg())
        old, new = us.update_affection("u1", 5)
        assert old == 0
        assert new == 5
        assert us.get_affection("u1") == 5

    def test_affection_clamp(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg())
        us.update_affection("u1", 200)
        assert us.get_affection("u1") == 100
        us.update_affection("u1", -200)
        assert us.get_affection("u1") == -100

    def test_get_level(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg(owner_mid="999"))
        assert us.get_level(0) == "stranger"
        assert us.get_level(10) == "stranger"
        assert us.get_level(11) == "normal"
        assert us.get_level(30) == "normal"
        assert us.get_level(31) == "friend"
        assert us.get_level(50) == "friend"
        assert us.get_level(51) == "close"
        assert us.get_level(100) == "close"
        assert us.get_level(-11) == "cold"
        # 主人特殊等级
        assert us.get_level(999, "999") == "special"

    def test_get_level_prompt(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg(owner_name="测试主人"))
        assert "测试主人" in us.get_level_prompt("special")
        assert "好友" in us.get_level_prompt("close")
        assert "陌生人" in us.get_level_prompt("stranger")
        assert "恶意" in us.get_level_prompt("cold")


class TestMood:
    def test_get_today_mood_disabled(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg(enable_mood=False))
        mood, prompt = us.get_today_mood()
        assert mood == "🌙 平静如常"
        assert prompt == ""

    def test_get_today_mood_enabled(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg(enable_mood=True))
        mood, prompt = us.get_today_mood()
        # 首次随机生成
        assert mood != ""
        assert isinstance(prompt, str)

    def test_get_today_mood_cached(self, tmp_data_dir):
        from bilibot.data_store import DataStore
        ds = DataStore(tmp_data_dir)
        us = UserStateSystem(ds, _cfg(enable_mood=True))
        mood1, _ = us.get_today_mood()
        mood2, _ = us.get_today_mood()
        # 同一天内心情不变
        assert mood1 == mood2
