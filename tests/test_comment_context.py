"""
tests/test_comment_context.py - 评论上下文真实接线测试

PRD V4 §4.3 / §7.3 必需测试文件。
覆盖：
- CommentContextService.build_context() 完整上下文链路
- 视频信息成功获取时 video_context_complete=True
- 视频信息失败时 video_context_complete=False，prompt 含"不得编造视频细节"
- ReplyGenerator.generate_reply(reply_context=...) 接入完整上下文
- LLM 收到的 system/user prompt 包含：当前人格、评论内容、视频标题、UP 名称、
  用户名、评论线、禁止编造提示（不完整时）
- SQLite memory_atoms 中 content_video 记忆可复用
"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bilibot.models import (
    SceneType, Persona, ReplyContext, VideoContext,
    CommentThread, CommentItem, UserProfile,
)
from bilibot.prompts.orchestrator import PromptOrchestrator
from bilibot.services.comment_context import CommentContextService
from bilibot.services.persona_store import PersonaStore
from bilibot.context_builder import ContextBuilder
from bilibot.reply import ReplyGenerator


# ═══════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════

@pytest.fixture
def persona_with_rules(tmp_data_dir):
    """创建一个带明显规则的测试人格"""
    ps = PersonaStore(data_dir=tmp_data_dir)
    p_dict = ps.create_persona({
        "name": "上下文测试人格",
        "base_prompt": "你是上下文测试人格，独特标识 CTX_PERSONA_MARK。",
        "reply_rules": "回复评论时遵守规则：必须包含视频标题。",
        "speaking_style": "活泼可爱",
    })
    p = Persona.from_dict(p_dict)
    ps.set_current(p.id)
    return ps, p


@pytest.fixture
def mock_bili_with_video():
    """Mock BilibiliAPI，返回完整视频信息"""
    bili = MagicMock()
    bili.get_video_info = AsyncMock(return_value={
        "bvid": "BV1XX411cXXm",
        "title": "测试视频标题示例",
        "owner": {"name": "测试UP主张三", "mid": 12345},
        "desc": "这是一个用于测试的视频简介。",
        "tag": ["标签1", "标签2", "标签3"],
        "tid": 17,
        "pubdate": 1700000000,
    })
    bili.close = AsyncMock()
    return bili


@pytest.fixture
def mock_memory_with_profile():
    """Mock UserStateSystem，返回用户画像/好感度/心情"""
    us = MagicMock()
    # UserStateSystem.get_user_profile_context 返回纯文本
    us.get_user_profile_context = MagicMock(return_value="【对该用户的了解】\n昵称：评论者小明\n标签：老粉\n事实：之前一起讨论过动漫")
    us.get_affection = MagicMock(return_value=5)
    us.get_level = MagicMock(return_value="normal")
    # get_today_mood 返回 (mood_str, mood_prompt) 元组
    us.get_today_mood = MagicMock(return_value=("开心", "语气轻快"))
    return us


@pytest.fixture
def mock_data_store(tmp_data_dir):
    """Mock DataStore"""
    ds = MagicMock()
    ds.data_dir = tmp_data_dir
    ds.load_json = MagicMock(return_value={})
    ds.save_json = MagicMock()
    return ds


@pytest.fixture
def config_loader(tmp_data_dir):
    from bilibot.app.config_loader import ConfigLoader
    return ConfigLoader(config_dict={
        "bilibili": {"sessdata": "test", "bili_jct": "test", "dede_user_id": "999"},
        "llm": {"api_key": "sk-test", "base_url": "http://localhost:8000/v1", "model": "test"},
        "web": {"enabled": False},
        "data_dir": tmp_data_dir,
    })


@pytest.fixture
def notification():
    """B站通知 item 样例（真实 API 结构：item 嵌套）"""
    return {
        "id": "999999",
        "user": {
            "mid": "88888",
            "nickname": "评论者小明",
        },
        "item": {
            "subject_id": "12345",
            "source_id": "999999",
            "root_id": "0",
            "business_id": 1,
            "source_content": "这个视频真好看！",
        },
    }


# ═══════════════════════════════════════════════════════
#  CommentContextService.build_context 单测
# ═══════════════════════════════════════════════════════

class TestCommentContextServiceBuild:
    """CommentContextService.build_context() 测试"""

    @pytest.mark.asyncio
    async def test_build_context_complete(
        self, mock_bili_with_video, mock_memory_with_profile,
        mock_data_store, persona_with_rules, config_loader, notification,
    ):
        """完整上下文：视频信息成功获取"""
        ps, p = persona_with_rules
        # PRD 4.8：memory_context 现在来自 knowledge_memory（search_related 死分支已移除）
        mock_km = MagicMock()
        mock_km.search_by_user = MagicMock(return_value=[
            {"content": "用户曾评论过同类视频"},
        ])
        # get_user_memories 返回空，让 user_profile 回退到 memory.get_user_profile
        mock_km.get_user_memories = MagicMock(return_value=[])
        svc = CommentContextService(
            bili=mock_bili_with_video,
            user_state=mock_memory_with_profile,
            data_store=mock_data_store,
            persona_store=ps,
            config_loader=config_loader,
            knowledge_memory=mock_km,
        )
        rc = await svc.build_context(notification)

        # 视频上下文完整
        assert rc.video is not None
        assert rc.video.title == "测试视频标题示例"
        assert rc.video.owner_name == "测试UP主张三"
        assert "标签1" in rc.video.tags
        assert rc.video_context_complete is True

        # 评论线含当前评论
        assert rc.thread is not None
        assert len(rc.thread.comments) >= 1
        assert rc.thread.comments[0].content == "这个视频真好看！"
        assert rc.thread.comments[0].username == "评论者小明"

        # 用户画像（UserStateSystem 回退分支：画像文本塞入 facts）
        assert rc.user_profile is not None
        assert rc.user_profile.username == "评论者小明"
        assert rc.user_profile.affection == 5

        # 相关长期记忆
        assert len(rc.memory_context) >= 1

        # 心情
        assert rc.mood == "开心"

    @pytest.mark.asyncio
    async def test_build_context_video_fails(
        self, mock_memory_with_profile, mock_data_store,
        persona_with_rules, config_loader, notification,
    ):
        """视频信息获取失败：video_context_complete=False"""
        bili = MagicMock()
        bili.get_video_info = AsyncMock(side_effect=Exception("API 不可用"))
        bili.close = AsyncMock()

        ps, _ = persona_with_rules
        svc = CommentContextService(
            bili=bili,
            user_state=mock_memory_with_profile,
            data_store=mock_data_store,
            persona_store=ps,
            config_loader=config_loader,
        )
        rc = await svc.build_context(notification)

        # 仍应有 video 占位（含 oid），但标记为不完整
        assert rc.video is not None
        assert rc.video.oid == "12345"
        assert rc.video.title == ""
        assert rc.video_context_complete is False

    @pytest.mark.asyncio
    async def test_build_context_bili_none(
        self, mock_memory_with_profile, mock_data_store,
        persona_with_rules, config_loader, notification,
    ):
        """bili=None：降级为不完整视频上下文"""
        ps, _ = persona_with_rules
        svc = CommentContextService(
            bili=None,
            user_state=mock_memory_with_profile,
            data_store=mock_data_store,
            persona_store=ps,
            config_loader=config_loader,
        )
        rc = await svc.build_context(notification)

        assert rc.video is not None
        assert rc.video.oid == "12345"
        assert rc.video_context_complete is False


# ═══════════════════════════════════════════════════════
#  视频记忆复用（PRD V4 §4.4.3）
# ═══════════════════════════════════════════════════════

class TestVideoMemoryReuse:
    """SQLite memory_atoms 中 content_video 记忆复用"""

    @pytest.mark.asyncio
    async def test_reuse_cached_video_memory(
        self, mock_memory_with_profile, persona_with_rules,
        config_loader, notification, tmp_data_dir,
    ):
        """memory_atoms 中存在同 oid 的 content_video 记忆时优先复用"""
        # 写入一条 content_video 记忆
        db_path = Path(tmp_data_dir) / "knowledge_base.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_atoms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                category TEXT,
                metadata TEXT,
                created_at REAL,
                is_active INTEGER DEFAULT 1
            )
        """)
        meta = json.dumps({
            "oid": "12345",
            "bvid": "BV1XX411cXXm",
            "title": "缓存的视频标题",
            "owner_name": "缓存UP主",
            "tags": ["缓存标签"],
            "desc": "缓存简介",
        }, ensure_ascii=False)
        conn.execute(
            "INSERT INTO memory_atoms (content, category, metadata, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("看过缓存视频", "content_video", meta, 1700000000),
        )
        conn.commit()
        conn.close()

        ds = MagicMock()
        ds.data_dir = tmp_data_dir

        # bili 不会被调用（缓存命中）
        bili = MagicMock()
        bili.get_video_info = AsyncMock(return_value=None)
        bili.close = AsyncMock()

        ps, _ = persona_with_rules
        svc = CommentContextService(
            bili=bili,
            user_state=mock_memory_with_profile,
            data_store=ds,
            persona_store=ps,
            config_loader=config_loader,
        )
        rc = await svc.build_context(notification)

        # 应使用缓存
        assert rc.video.title == "缓存的视频标题"
        assert rc.video.owner_name == "缓存UP主"
        assert rc.video_context_complete is True
        # 不应调用 bili API
        bili.get_video_info.assert_not_called()


# ═══════════════════════════════════════════════════════
#  ReplyGenerator + 完整上下文（PRD V4 §4.3.2）
# ═══════════════════════════════════════════════════════

class TestReplyGeneratorWithContext:
    """ReplyGenerator.generate_reply(reply_context=...) 完整链路"""

    def test_generate_reply_uses_context(
        self, persona_with_rules, mock_data_store, config_loader, tmp_data_dir,
    ):
        """LLM 收到的 prompt 含人格、视频标题、UP、用户名、评论内容"""
        ps, p = persona_with_rules
        orch = PromptOrchestrator(ps)
        cb = ContextBuilder(
            data_store=mock_data_store,
            user_state=None,
            persona_store=ps,
            bili=None,
            config={},
        )

        # 捕获 LLM 收到的 prompt
        captured = {}

        class FakeLLM:
            client = True

            async def generate(self, prompt, system_prompt=None, max_tokens=1024, **kw):
                captured["system"] = system_prompt or ""
                captured["user"] = prompt
                return "这是模拟回复"

            async def get_embedding(self, text):
                return [0.1] * 1536

        from bilibot.personality import PersonalitySystem
        personality = PersonalitySystem(config_loader)

        rg = ReplyGenerator(
            user_state=None,
            personality_system=personality,
            llm_adapter=FakeLLM(),
            data_store=mock_data_store,
            config=config_loader,
            orchestrator=orch,
            context_builder=cb,
            persona_store=ps,
            audit_store=None,  # 不写 audit
        )

        # 构造完整 ReplyContext
        rc = ReplyContext(
            video=VideoContext(
                oid="12345",
                title="测试视频标题示例",
                owner_name="测试UP主张三",
                desc="视频简介示例",
                tags=["标签1"],
            ),
            thread=CommentThread(
                root_rpid="999999",
                comments=[CommentItem(
                    rpid="999999",
                    user_id="88888",
                    username="评论者小明",
                    content="这个视频真好看！",
                )],
            ),
            user_profile=UserProfile(
                user_id="88888",
                username="评论者小明",
                affection=5,
                tags=["老粉"],
            ),
            video_context_complete=True,
        )

        import asyncio
        result = asyncio.run(rg.generate_reply(
            user_id="88888",
            username="评论者小明",
            comment="这个视频真好看！",
            thread_id="999999",
            oid="12345",
            reply_context=rc,
        ))

        # LLM 被调用
        assert result is not None
        assert "reply" in result
        assert result["reply"] == "这是模拟回复"

        # system prompt 含人格标识
        assert "CTX_PERSONA_MARK" in captured["system"]

        # user prompt 含视频标题 / UP / 用户名 / 评论内容
        assert "测试视频标题示例" in captured["user"]
        assert "测试UP主张三" in captured["user"]
        assert "评论者小明" in captured["user"]
        assert "这个视频真好看！" in captured["user"]

        # 返回值含 context_meta（PRD V4 §6.1）
        assert "context_meta" in result
        assert result["context_meta"]["video_ctx_complete"] is True

    def test_generate_reply_incomplete_video_warns(
        self, persona_with_rules, mock_data_store, config_loader,
    ):
        """视频上下文不完整时 prompt 含'不得编造视频细节'"""
        ps, p = persona_with_rules
        orch = PromptOrchestrator(ps)
        cb = ContextBuilder(
            data_store=mock_data_store,
            user_state=None,
            persona_store=ps,
            bili=None,
            config={},
        )

        captured = {}

        class FakeLLM:
            client = True

            async def generate(self, prompt, system_prompt=None, max_tokens=1024, **kw):
                captured["system"] = system_prompt or ""
                captured["user"] = prompt
                return "降级回复"

            async def get_embedding(self, text):
                return [0.1] * 1536

        from bilibot.personality import PersonalitySystem
        personality = PersonalitySystem(config_loader)

        rg = ReplyGenerator(
            user_state=None,
            personality_system=personality,
            llm_adapter=FakeLLM(),
            data_store=mock_data_store,
            config=config_loader,
            orchestrator=orch,
            context_builder=cb,
            persona_store=ps,
            audit_store=None,
        )

        rc = ReplyContext(
            video=VideoContext(oid="12345", title=""),
            video_context_complete=False,
        )

        import asyncio
        result = asyncio.run(rg.generate_reply(
            user_id="88888",
            username="评论者小明",
            comment="随便说的",
            thread_id="999999",
            oid="12345",
            reply_context=rc,
        ))

        assert result is not None
        # user prompt 含禁止编造提示
        assert "不得编造视频细节" in captured["user"] or \
               "视频上下文不完整" in captured["user"]
        assert result["context_meta"]["video_ctx_complete"] is False
