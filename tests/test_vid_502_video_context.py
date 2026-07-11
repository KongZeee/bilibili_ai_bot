"""
VID-502 搜索与视听上下文合并测试

PRD-V5 VID-502 验证点：
1. 结构化 VideoContext 各来源独立赋值不互相覆盖
2. 某一路失败不清空其他成功结果
3. Prompt Builder 统一截断并标记来源；搜索结果始终是不可信 Reference Block
4. 核心 bug 修复：设置 audiovisual 不再清空 search_reference
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bilibot.models.proactive_video_context import ProactiveVideoContext


# ═══════════════════════════════════════════════
# 测试数据工厂
# ═══════════════════════════════════════════════

def _make_metadata():
    return {
        "title": "测试视频标题",
        "desc": "这是一个测试视频的简介",
        "owner": {"name": "测试UP主", "mid": 12345},
        "stat": {"view": 10000, "like": 500, "coin": 200},
        "duration": 300,
        "tname": "科技",
    }


def _make_hot_comments():
    return [
        {"name": "用户A", "content": "好视频！"},
        {"name": "用户B", "content": "学到了很多"},
        {"name": "用户C", "content": "UP主太强了"},
    ]


def _make_search_reference():
    return {
        "answer": "量子计算是一种利用量子力学原理进行计算的技术",
        "items": [
            {"title": "量子计算简介", "snippet": "量子计算利用量子比特进行计算...", "url": "https://example.com/1"},
            {"title": "量子计算最新进展", "snippet": "2024年量子计算取得突破...", "url": "https://example.com/2"},
        ],
        "citations": [],
        "freshness": "daily",
        "fetched_at": "2024-01-01T12:00:00",
        "backend": "tavily",
    }


def _make_audiovisual():
    return {
        "behavior_log": "## 视听行为日志\n\n[00:00-00:15] 画面：UP主在白板前讲解\n[00:15-00:30] 台词：今天我们来聊一聊量子计算",
        "answer": None,
        "work_dir": "/tmp/video_temp/abc123",
        "degradation_reason": "",
    }


BVID = "BV1xx411c7XX"


# ═══════════════════════════════════════════════
# 1. DTO 单元测试：to_prompt_sections
# ═══════════════════════════════════════════════

class TestProactiveVideoContextPromptSections:
    """to_prompt_sections() 各段格式化与截断"""

    def test_all_sources_populated_all_sections_appear(self):
        """4 个来源都有值 → prompt 包含全部 4 段 + 降级说明"""
        ctx = ProactiveVideoContext(
            bvid=BVID,
            metadata=_make_metadata(),
            hot_comments=_make_hot_comments(),
            search_reference=_make_search_reference(),
            audiovisual=_make_audiovisual(),
            degradation_reasons=["test_reason"],
        )
        prompt = ctx.to_prompt_sections()

        assert "【视频信息】" in prompt
        assert "【热门评论】" in prompt
        assert "【网络搜索参考】" in prompt
        assert "【视听分析】" in prompt
        assert "【降级说明】" in prompt
        # 内容正确
        assert "测试视频标题" in prompt
        assert "好视频！" in prompt
        assert "量子计算" in prompt
        assert "视听行为日志" in prompt

    def test_only_metadata_no_empty_sections(self):
        """只有 metadata → 只出现 metadata 段，不出现空段"""
        ctx = ProactiveVideoContext(
            bvid=BVID,
            metadata=_make_metadata(),
        )
        prompt = ctx.to_prompt_sections()

        assert "【视频信息】" in prompt
        assert "【热门评论】" not in prompt
        assert "【网络搜索参考】" not in prompt
        assert "【视听分析】" not in prompt
        assert "【降级说明】" not in prompt

    def test_search_reference_has_untrusted_footer(self):
        """search_reference → prompt 包含 Reference Block 不可信页脚"""
        ctx = ProactiveVideoContext(
            bvid=BVID,
            search_reference=_make_search_reference(),
        )
        prompt = ctx.to_prompt_sections()

        assert "【网络搜索参考】" in prompt
        assert "仅供提取事实参考" in prompt
        assert "可能包含不准确信息" in prompt

    def test_empty_context_only_bvid(self):
        """空 VideoContext（只有 bvid）→ 最小 prompt（空串）"""
        ctx = ProactiveVideoContext(bvid=BVID)
        prompt = ctx.to_prompt_sections()
        # 没有任何来源，prompt 应为空串
        assert prompt == ""
        assert not ctx.has_any_content

    def test_multiple_degradation_reasons_all_appear(self):
        """多个降级原因都出现在降级说明段"""
        reasons = ["search_failed: timeout", "audiovisual_failed: download error", "metadata_failed: 404"]
        ctx = ProactiveVideoContext(
            bvid=BVID,
            degradation_reasons=reasons,
        )
        prompt = ctx.to_prompt_sections()

        assert "【降级说明】" in prompt
        for reason in reasons:
            assert reason in prompt

    def test_each_section_independently_truncated(self):
        """各段独立截断到上限（2000 字符）"""
        # 构造超长 behavior_log 和 answer
        long_log = "A" * 5000
        ctx = ProactiveVideoContext(
            bvid=BVID,
            audiovisual={"behavior_log": long_log},
            search_reference={
                "answer": "B" * 5000,
                "items": [],
            },
        )
        prompt = ctx.to_prompt_sections()

        # to_prompt_sections 段顺序：search_reference → audiovisual（用 \n\n 连接）
        # 搜索参考段：从【网络搜索参考】到【视听分析】之前
        sr_start = prompt.index("【网络搜索参考】")
        av_start = prompt.index("【视听分析】")
        sr_section = prompt[sr_start:av_start].strip()
        assert len(sr_section) <= 2000, f"搜索段 {len(sr_section)} 超限"

        # 视听分析段：从【视听分析】到末尾（最后一段）
        av_section = prompt[av_start:].strip()
        assert len(av_section) <= 2000, f"视听段 {len(av_section)} 超限"


# ═══════════════════════════════════════════════
# 2. 核心 bug 修复：各来源不互相覆盖
# ═══════════════════════════════════════════════

class TestNoOverwriteBetweenSources:
    """核心 bug：设置 audiovisual 不清空 search_reference"""

    def test_setting_audiovisual_does_not_clear_search_reference(self):
        """设置 audiovisual 后 search_reference 仍然存在"""
        ctx = ProactiveVideoContext(bvid=BVID)
        ctx.search_reference = _make_search_reference()

        # 模拟旧 bug：video_content = vu_result（覆盖）
        # 新实现：ctx.audiovisual = vu_result（独立字段）
        ctx.audiovisual = _make_audiovisual()

        # search_reference 未被清空
        assert ctx.search_reference is not None
        assert ctx.search_reference["answer"] == _make_search_reference()["answer"]
        # audiovisual 也存在
        assert ctx.audiovisual is not None
        assert ctx.audiovisual["behavior_log"]

        # prompt 同时包含两段
        prompt = ctx.to_prompt_sections(include_metadata=False, include_hot_comments=False)
        assert "【网络搜索参考】" in prompt
        assert "【视听分析】" in prompt

    def test_search_failure_does_not_clear_audiovisual(self):
        """搜索失败（异常）不清空已成功的视听分析"""
        ctx = ProactiveVideoContext(bvid=BVID)
        ctx.audiovisual = _make_audiovisual()

        # 模拟搜索失败：只追加 degradation_reason，不操作 audiovisual
        try:
            raise ConnectionError("search timeout")
        except Exception as e:
            ctx.degradation_reasons.append(f"search_failed: {e}")

        assert ctx.audiovisual is not None
        assert ctx.audiovisual["behavior_log"]
        assert len(ctx.degradation_reasons) == 1
        assert "search_failed" in ctx.degradation_reasons[0]

    def test_audiovisual_failure_does_not_clear_search(self):
        """视听分析失败不清空已成功的搜索结果"""
        ctx = ProactiveVideoContext(bvid=BVID)
        ctx.search_reference = _make_search_reference()

        # 模拟视听分析失败
        try:
            raise RuntimeError("video download failed")
        except Exception as e:
            ctx.degradation_reasons.append(f"audiovisual_failed: {e}")
            # 不设置 ctx.audiovisual（保持 None）

        assert ctx.search_reference is not None
        assert ctx.audiovisual is None
        assert len(ctx.degradation_reasons) == 1
        assert "audiovisual_failed" in ctx.degradation_reasons[0]

    def test_all_sources_survive_individual_failures(self):
        """3 个来源成功，1 个失败 → 3 个来源仍在，降级说明记录失败"""
        ctx = ProactiveVideoContext(bvid=BVID)
        ctx.metadata = _make_metadata()
        ctx.hot_comments = _make_hot_comments()
        ctx.search_reference = _make_search_reference()
        # audiovisual 失败
        ctx.degradation_reasons.append("audiovisual_failed: download error")

        assert ctx.metadata is not None
        assert ctx.hot_comments is not None
        assert ctx.search_reference is not None
        assert ctx.audiovisual is None
        assert len(ctx.degradation_reasons) == 1

        prompt = ctx.to_prompt_sections()
        assert "【视频信息】" in prompt
        assert "【热门评论】" in prompt
        assert "【网络搜索参考】" in prompt
        assert "【视听分析】" not in prompt  # 失败的段不出现
        assert "【降级说明】" in prompt


# ═══════════════════════════════════════════════
# 3. 集成测试：模拟 scheduler 视频上下文构建流程
# ═══════════════════════════════════════════════

class TestSchedulerVideoContextFlow:
    """模拟 scheduler._do_proactive_video 中的上下文构建流程"""

    @pytest.mark.asyncio
    async def test_metadata_fails_search_succeeds_context_preserved(self):
        """metadata 获取失败但搜索成功 → context 有 search_reference，降级说明记录 metadata 失败"""
        ctx = ProactiveVideoContext(bvid=BVID)

        # 来源：搜索（成功）
        mock_web_search = MagicMock()
        mock_web_search.is_available = MagicMock(return_value=True)
        mock_web_search.should_search_for_video = AsyncMock(return_value="量子计算")
        mock_web_search.search = AsyncMock(return_value=_make_search_reference())

        # 模拟 scheduler 中的搜索流程
        if mock_web_search and mock_web_search.is_available():
            try:
                search_query = await mock_web_search.should_search_for_video(
                    video_info={"title": "", "desc": "", "tname": "", "owner_name": ""},
                    scene="proactive_video",
                )
                if search_query:
                    search_result = await mock_web_search.search(search_query, scene="proactive_video")
                    if search_result:
                        ctx.search_reference = search_result
            except Exception as e:
                ctx.degradation_reasons.append(f"search_failed: {e}")

        # 模拟 metadata 失败（在 scheduler 中 metadata 来自 bili.get_video_info）
        # 这里 metadata 未设置（模拟失败场景）
        ctx.degradation_reasons.append("metadata_failed: bili API 500")

        # 验证
        assert ctx.search_reference is not None
        assert ctx.metadata is None
        assert any("metadata_failed" in r for r in ctx.degradation_reasons)

        prompt = ctx.to_prompt_sections(include_metadata=True)
        assert "【网络搜索参考】" in prompt
        assert "仅供提取事实参考" in prompt
        assert "【视频信息】" not in prompt  # metadata 失败，段不出现
        assert "【降级说明】" in prompt

    @pytest.mark.asyncio
    async def test_search_fails_audiovisual_succeeds_context_preserved(self):
        """搜索失败但视听分析成功 → context 有 audiovisual，降级说明记录 search 失败"""
        ctx = ProactiveVideoContext(bvid=BVID)
        ctx.metadata = _make_metadata()

        # 搜索失败
        mock_web_search = MagicMock()
        mock_web_search.is_available = MagicMock(return_value=True)
        mock_web_search.should_search_for_video = AsyncMock(return_value="量子计算")
        mock_web_search.search = AsyncMock(side_effect=ConnectionError("timeout"))

        if mock_web_search and mock_web_search.is_available():
            try:
                search_query = await mock_web_search.should_search_for_video(
                    video_info={"title": "test", "desc": "", "tname": "", "owner_name": ""},
                    scene="proactive_video",
                )
                if search_query:
                    search_result = await mock_web_search.search(search_query, scene="proactive_video")
                    if search_result:
                        ctx.search_reference = search_result
            except Exception as e:
                ctx.degradation_reasons.append(f"search_failed: {e}")

        # 视听分析成功
        mock_vu = MagicMock()
        mock_vu.is_available = MagicMock(return_value=True)
        mock_vu.understand = AsyncMock(return_value=_make_audiovisual())

        vu_result = await mock_vu.understand("/fake/path.mp4")
        ctx.audiovisual = vu_result

        # 验证
        assert ctx.search_reference is None
        assert ctx.audiovisual is not None
        assert ctx.audiovisual["behavior_log"]
        assert any("search_failed" in r for r in ctx.degradation_reasons)

        prompt = ctx.to_prompt_sections(include_metadata=False, include_hot_comments=False)
        assert "【视听分析】" in prompt
        assert "【网络搜索参考】" not in prompt  # 搜索失败
        assert "【降级说明】" in prompt
        assert "search_failed" in prompt

    @pytest.mark.asyncio
    async def test_both_search_and_audiovisual_succeed_both_in_prompt(self):
        """搜索和视听分析都成功 → prompt 同时包含两段（核心 bug 修复验证）"""
        ctx = ProactiveVideoContext(bvid=BVID)

        # 搜索成功
        ctx.search_reference = _make_search_reference()
        # 视听分析成功（旧 bug 会覆盖搜索，新实现不会）
        ctx.audiovisual = _make_audiovisual()

        prompt = ctx.to_prompt_sections(include_metadata=False, include_hot_comments=False)

        # 两段都在（旧 bug 只有视听分析段）
        assert "【网络搜索参考】" in prompt
        assert "【视听分析】" in prompt
        # 搜索内容仍在
        assert "量子计算" in prompt
        # 视听内容也在
        assert "视听行为日志" in prompt

    @pytest.mark.asyncio
    async def test_audiovisual_degradation_reason_recorded(self):
        """视听分析返回降级原因 → 记录在 degradation_reasons 中"""
        ctx = ProactiveVideoContext(bvid=BVID)
        vu_result = {
            "behavior_log": "",
            "answer": None,
            "work_dir": "",
            "degradation_reason": "preprocess_timeout",
        }
        ctx.audiovisual = vu_result

        # 模拟 scheduler 中的降级处理逻辑
        av_log = ctx.audiovisual_log
        if not av_log:
            deg = vu_result.get("degradation_reason", "")
            if deg:
                ctx.degradation_reasons.append(f"audiovisual_degraded: {deg}")

        assert not av_log  # 行为日志为空
        assert any("audiovisual_degraded" in r and "preprocess_timeout" in r
                   for r in ctx.degradation_reasons)


# ═══════════════════════════════════════════════
# 4. to_dict 序列化
# ═══════════════════════════════════════════════

class TestProactiveVideoContextSerialization:

    def test_to_dict_roundtrip(self):
        """to_dict() 包含所有字段"""
        ctx = ProactiveVideoContext(
            bvid=BVID,
            metadata=_make_metadata(),
            hot_comments=_make_hot_comments(),
            search_reference=_make_search_reference(),
            audiovisual=_make_audiovisual(),
            degradation_reasons=["reason1"],
        )
        d = ctx.to_dict()

        assert d["bvid"] == BVID
        assert d["metadata"] == _make_metadata()
        assert d["hot_comments"] == _make_hot_comments()
        assert d["search_reference"] == _make_search_reference()
        assert d["audiovisual"] == _make_audiovisual()
        assert d["degradation_reasons"] == ["reason1"]

    def test_to_dict_empty_context(self):
        """空 context 的 to_dict() 字段安全"""
        ctx = ProactiveVideoContext(bvid=BVID)
        d = ctx.to_dict()

        assert d["bvid"] == BVID
        assert d["metadata"] is None
        assert d["hot_comments"] is None
        assert d["search_reference"] is None
        assert d["audiovisual"] is None
        assert d["degradation_reasons"] == []


# ═══════════════════════════════════════════════
# 5. include_metadata / include_hot_comments 开关
# ═══════════════════════════════════════════════

class TestPromptSectionFlags:

    def test_exclude_metadata_and_hot_comments(self):
        """scheduler 调用时排除 metadata 和 hot_comments（避免与 evaluate_video 重复）"""
        ctx = ProactiveVideoContext(
            bvid=BVID,
            metadata=_make_metadata(),
            hot_comments=_make_hot_comments(),
            search_reference=_make_search_reference(),
            audiovisual=_make_audiovisual(),
        )
        prompt = ctx.to_prompt_sections(include_metadata=False, include_hot_comments=False)

        assert "【视频信息】" not in prompt
        assert "【热门评论】" not in prompt
        assert "【网络搜索参考】" in prompt
        assert "【视听分析】" in prompt

    def test_default_includes_all(self):
        """默认参数包含所有段"""
        ctx = ProactiveVideoContext(
            bvid=BVID,
            metadata=_make_metadata(),
            hot_comments=_make_hot_comments(),
        )
        prompt = ctx.to_prompt_sections()

        assert "【视频信息】" in prompt
        assert "【热门评论】" in prompt
