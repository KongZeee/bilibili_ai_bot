"""
tests/test_context_builder.py - ContextBuilder 测试

PRD V3 §5.8 必需测试文件。
覆盖：
- dataclass ReplyContext 不报错
- dict 旧数据不报错
- CommentItem 的 username/content 能进入上下文
- UserProfile 的 username/affection/tags/facts 能进入上下文
- VideoContext 的标题、UP、简介能进入上下文
- video_context_complete=False 时输出禁止编造提示
"""
import pytest

from bilibot.context_builder import ContextBuilder
from bilibot.models import (
    ReplyContext, VideoContext, CommentThread, CommentItem, UserProfile,
)
from bilibot.services.persona_store import PersonaStore


@pytest.fixture
def cb(tmp_data_dir):
    ps = PersonaStore(data_dir=tmp_data_dir)
    return ContextBuilder(persona_store=ps)


# ── dataclass 输入 ──

class TestDataclassInput:
    def test_empty_dataclass_no_error(self, cb):
        ctx = ReplyContext()
        result = cb.build(ctx)
        assert "text" in result
        assert result["text"] == "" or "人格" in result["text"]

    def test_dataclass_with_video(self, cb):
        v = VideoContext(title="标题A", owner_name="UP主A", desc="简介A")
        ctx = ReplyContext(video=v, video_context_complete=True)
        result = cb.build(ctx)
        assert "标题A" in result["text"]
        assert "UP主A" in result["text"]
        assert "简介A" in result["text"]

    def test_dataclass_with_thread(self, cb):
        c = CommentItem(username="用户X", content="评论内容Y")
        t = CommentThread(root_rpid="r1", comments=[c])
        ctx = ReplyContext(thread=t)
        result = cb.build(ctx)
        assert "用户X" in result["text"]
        assert "评论内容Y" in result["text"]

    def test_dataclass_with_user_profile(self, cb):
        up = UserProfile(
            username="小明", affection=42, tags=["老粉"], facts=["喜欢猫"]
        )
        ctx = ReplyContext(user_profile=up)
        result = cb.build(ctx)
        assert "小明" in result["text"]
        assert "42" in result["text"]
        assert "老粉" in result["text"]
        assert "喜欢猫" in result["text"]


# ── dict 输入（旧数据兼容） ──

class TestDictInput:
    def test_empty_dict_no_error(self, cb):
        ctx = {}
        result = cb.build(ctx)
        assert "text" in result

    def test_dict_with_video(self, cb):
        ctx = {
            "video": {
                "title": "标题D",
                "owner_name": "UP主D",
                "desc": "简介D",
            },
            "video_context_complete": True,
        }
        result = cb.build(ctx)
        assert "标题D" in result["text"]
        assert "UP主D" in result["text"]
        assert "简介D" in result["text"]

    def test_dict_with_thread(self, cb):
        ctx = {
            "thread": {
                "comments": [
                    {"username": "用户M", "content": "内容N"},
                ]
            }
        }
        result = cb.build(ctx)
        assert "用户M" in result["text"]
        assert "内容N" in result["text"]

    def test_dict_with_user_profile(self, cb):
        ctx = {
            "user_profile": {
                "username": "小红",
                "affection": 7,
                "tags": ["新人"],
                "facts": ["第一次来"],
            }
        }
        result = cb.build(ctx)
        assert "小红" in result["text"]
        assert "7" in result["text"]
        assert "新人" in result["text"]
        assert "第一次来" in result["text"]


# ── 视频上下文不足警告 ──

class TestVideoContextWarning:
    def test_dataclass_incomplete_warns(self, cb):
        v = VideoContext(title="T", owner_name="U", desc="D")
        ctx = ReplyContext(video=v, video_context_complete=False)
        result = cb.build(ctx)
        assert "不得编造视频细节" in result["text"]
        assert result["meta"]["video_ctx_complete"] is False

    def test_dataclass_complete_no_warn(self, cb):
        v = VideoContext(title="T", owner_name="U", desc="D")
        ctx = ReplyContext(video=v, video_context_complete=True)
        result = cb.build(ctx)
        assert "不得编造视频细节" not in result["text"]
        assert result["meta"]["video_ctx_complete"] is True

    def test_dict_incomplete_warns(self, cb):
        ctx = {
            "video": {"title": "T", "owner_name": "U", "desc": "D"},
            "video_context_complete": False,
        }
        result = cb.build(ctx)
        assert "不得编造视频细节" in result["text"]

    def test_dict_default_complete(self, cb):
        """dict 不传 video_context_complete 时默认视为完整"""
        ctx = {"video": {"title": "T", "owner_name": "U", "desc": "D"}}
        result = cb.build(ctx)
        assert "不得编造视频细节" not in result["text"]
