"""
tests/test_rep_501_generation_outcome.py - REP-501 GenerationOutcome 测试

PRD-V5 §6.1：generate_reply 不再返回 None 区分"不想回复"与"临时失败"。

覆盖：
- GenerationOutcome 数据类与工厂方法
- ReplyGenerator._generate_reply_impl() 的可判别结果：
  - LLM 未配置 → permanent_error(LLM_NOT_CONFIGURED)
  - LLM client 未初始化 → retryable_error(LLM_CLIENT_UNAVAILABLE)
  - LLM 超时 → retryable_error(LLM_TIMEOUT)
  - LLM 429 → retryable_error(LLM_RATE_LIMITED)
  - LLM 连接错误 → retryable_error(LLM_CONNECTION_ERROR)
  - LLM 5xx → retryable_error(LLM_SERVER_ERROR)
  - 模型空回复 → skip(MODEL_EMPTY_REPLY)
  - 未知异常 → retryable_error(LLM_UNKNOWN_ERROR)
  - 正常文本 → generated
  - 永不返回 None
- 向后兼容包装器 generate_reply() 的 dict / None 行为
"""
import asyncio

import pytest

from bilibot.models.generation import (
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
from bilibot.reply import ReplyGenerator


# ═══════════════════════════════════════════════════════
#  Mock 工具
# ═══════════════════════════════════════════════════════

class _MockLLM:
    """可控的 Mock LLM：可设置 client / raise_exc / return_text"""

    def __init__(self, client=True, return_text="模拟回复", raise_exc=None):
        self.client = client
        self._return_text = return_text
        self._raise_exc = raise_exc

    async def generate(self, prompt, system_prompt="", max_tokens=200, **kwargs):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._return_text


def _make_generator(llm=None, **kwargs):
    """构造 ReplyGenerator（最小依赖）"""
    return ReplyGenerator(
        user_state=None,
        personality_system=None,
        llm_adapter=llm,
        data_store=None,
        config=None,
        **kwargs,
    )


# ═══════════════════════════════════════════════════════
#  GenerationOutcome 数据类测试
# ═══════════════════════════════════════════════════════

class TestGenerationOutcomeDataclass:
    """GenerationOutcome 工厂方法与状态属性"""

    def test_generated(self):
        o = GenerationOutcome.generated("hello", audit_id="a1")
        assert o.status == STATUS_GENERATED
        assert o.text == "hello"
        assert o.audit_id == "a1"
        assert o.is_generated
        assert o.has_text
        assert not o.is_skip
        assert not o.is_retryable
        assert not o.is_permanent_error

    def test_generated_with_meta(self):
        o = GenerationOutcome.generated("hi", context_meta={"k": 1})
        assert o.context_meta == {"k": 1}

    def test_generated_default_meta_empty(self):
        o = GenerationOutcome.generated("hi")
        assert o.context_meta == {}

    def test_skip(self):
        o = GenerationOutcome.skip(MODEL_EMPTY_REPLY)
        assert o.status == STATUS_SKIP
        assert o.error_code == MODEL_EMPTY_REPLY
        assert o.is_skip
        assert not o.has_text

    def test_skip_default_reason(self):
        o = GenerationOutcome.skip()
        assert o.error_code == "SKIP"
        assert o.is_skip

    def test_retryable(self):
        o = GenerationOutcome.retryable(LLM_TIMEOUT, retry_after=5.0)
        assert o.status == STATUS_RETRYABLE_ERROR
        assert o.error_code == LLM_TIMEOUT
        assert o.retry_after == 5.0
        assert o.is_retryable

    def test_retryable_no_retry_after(self):
        o = GenerationOutcome.retryable(LLM_CONNECTION_ERROR)
        assert o.retry_after is None

    def test_retryable_default_code(self):
        o = GenerationOutcome.retryable("")
        assert o.error_code == LLM_UNKNOWN_ERROR

    def test_permanent(self):
        o = GenerationOutcome.permanent(LLM_NOT_CONFIGURED)
        assert o.status == STATUS_PERMANENT_ERROR
        assert o.error_code == LLM_NOT_CONFIGURED
        assert o.is_permanent_error

    def test_permanent_default_code(self):
        o = GenerationOutcome.permanent("")
        assert o.error_code == "PERMANENT_ERROR"

    def test_to_dict(self):
        o = GenerationOutcome.generated("x", audit_id="a", context_meta={"k": 1})
        d = o.to_dict()
        assert d["status"] == STATUS_GENERATED
        assert d["text"] == "x"
        assert d["audit_id"] == "a"
        assert d["context_meta"] == {"k": 1}

    def test_to_dict_is_copy(self):
        meta = {"k": 1}
        o = GenerationOutcome.generated("x", context_meta=meta)
        d = o.to_dict()
        d["context_meta"]["k"] = 999
        assert o.context_meta["k"] == 1

    def test_repr_generated(self):
        o = GenerationOutcome.generated("hi")
        assert "generated" in repr(o)

    def test_repr_skip(self):
        o = GenerationOutcome.skip("R")
        assert "skip" in repr(o)

    def test_repr_retryable(self):
        o = GenerationOutcome.retryable(LLM_TIMEOUT, retry_after=3.0)
        assert "retryable" in repr(o)

    def test_repr_permanent(self):
        o = GenerationOutcome.permanent("P")
        assert "permanent" in repr(o)


# ═══════════════════════════════════════════════════════
#  _generate_reply_impl 可判别结果测试
# ═══════════════════════════════════════════════════════

class TestGenerateReplyImpl:
    """ReplyGenerator._generate_reply_impl 可判别结果（REP-501）"""

    @pytest.mark.asyncio
    async def test_valid_text_generated(self):
        llm = _MockLLM(return_text="你好呀")
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_generated
        assert outcome.text == "你好呀"
        assert outcome.status == STATUS_GENERATED

    @pytest.mark.asyncio
    async def test_model_empty_reply_skip(self):
        llm = _MockLLM(return_text="")
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_skip
        assert outcome.error_code == MODEL_EMPTY_REPLY

    @pytest.mark.asyncio
    async def test_model_none_reply_skip(self):
        llm = _MockLLM(return_text=None)
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_skip
        assert outcome.error_code == MODEL_EMPTY_REPLY

    @pytest.mark.asyncio
    async def test_whitespace_only_reply_skip(self):
        llm = _MockLLM(return_text="   \n  ")
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_skip
        assert outcome.error_code == MODEL_EMPTY_REPLY

    @pytest.mark.asyncio
    async def test_llm_not_configured_permanent(self):
        gen = _make_generator(llm=None)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_permanent_error
        assert outcome.error_code == LLM_NOT_CONFIGURED

    @pytest.mark.asyncio
    async def test_llm_client_unavailable_retryable(self):
        llm = _MockLLM(client=None)
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_CLIENT_UNAVAILABLE

    @pytest.mark.asyncio
    async def test_llm_timeout_retryable(self):
        llm = _MockLLM(raise_exc=asyncio.TimeoutError())
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_TIMEOUT

    @pytest.mark.asyncio
    async def test_llm_connection_error_retryable(self):
        llm = _MockLLM(raise_exc=ConnectionError("connection refused"))
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_CONNECTION_ERROR

    @pytest.mark.asyncio
    async def test_llm_rate_limited_retryable(self):
        class _RateLimitError(Exception):
            pass
        llm = _MockLLM(raise_exc=_RateLimitError("429 Too Many Requests"))
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_RATE_LIMITED

    @pytest.mark.asyncio
    async def test_llm_server_error_retryable(self):
        class _InternalServerError(Exception):
            pass
        llm = _MockLLM(raise_exc=_InternalServerError("503 Service Unavailable"))
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_SERVER_ERROR

    @pytest.mark.asyncio
    async def test_unknown_exception_retryable(self):
        llm = _MockLLM(raise_exc=RuntimeError("something broke"))
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_UNKNOWN_ERROR

    @pytest.mark.asyncio
    async def test_rate_limit_retry_after_extracted(self):
        """429 异常携带 retry_after 时应透传到 outcome"""
        class _RateLimitError(Exception):
            def __init__(self, msg, retry_after=None, response=None):
                super().__init__(msg)
                self.retry_after = retry_after
                self.response = response
        llm = _MockLLM(raise_exc=_RateLimitError(
            "429 Too Many Requests", retry_after=12.0,
        ))
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_retryable
        assert outcome.error_code == LLM_RATE_LIMITED
        assert outcome.retry_after == 12.0

    @pytest.mark.asyncio
    async def test_impl_never_returns_none(self):
        """所有路径都返回 GenerationOutcome，不返回 None"""
        cases = [
            _MockLLM(return_text="ok"),
            _MockLLM(return_text=""),
            _MockLLM(return_text=None),
            _MockLLM(return_text="   "),
            _MockLLM(client=None),
            _MockLLM(raise_exc=asyncio.TimeoutError()),
            _MockLLM(raise_exc=ConnectionError()),
            _MockLLM(raise_exc=RuntimeError("boom")),
        ]
        for llm in cases:
            gen = _make_generator(llm=llm)
            outcome = await gen._generate_reply_impl(
                user_id="u1", username="张三", comment="你好",
                thread_id="t1", oid="o1",
            )
            assert outcome is not None, f"llm={llm!r} 返回了 None"
            assert isinstance(outcome, GenerationOutcome)

    @pytest.mark.asyncio
    async def test_impl_never_returns_none_no_llm(self):
        gen = _make_generator(llm=None)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome is not None
        assert isinstance(outcome, GenerationOutcome)

    @pytest.mark.asyncio
    async def test_generated_carries_context_meta(self):
        llm = _MockLLM(return_text="你好")
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_generated
        assert "affection_delta" in outcome.context_meta
        assert outcome.context_meta["affection_delta"] == 0
        assert "persona_id" in outcome.context_meta

    @pytest.mark.asyncio
    async def test_truncates_overlong_reply(self):
        long_text = "字" * 300
        llm = _MockLLM(return_text=long_text)
        gen = _make_generator(llm=llm)
        outcome = await gen._generate_reply_impl(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert outcome.is_generated
        assert len(outcome.text) <= 233


# ═══════════════════════════════════════════════════════
#  向后兼容包装器测试
# ═══════════════════════════════════════════════════════

class TestGenerateReplyBackwardCompat:
    """generate_reply() 向后兼容包装器（Task 10 将替换）"""

    @pytest.mark.asyncio
    async def test_wrapper_returns_dict_on_success(self):
        llm = _MockLLM(return_text="你好")
        gen = _make_generator(llm=llm)
        result = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert result is not None
        assert result["reply"] == "你好"
        assert "audit_id" in result
        assert "context_meta" in result
        assert result["affection_delta"] == 0

    @pytest.mark.asyncio
    async def test_wrapper_returns_none_on_skip(self):
        llm = _MockLLM(return_text="")
        gen = _make_generator(llm=llm)
        result = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_wrapper_returns_none_on_retryable(self):
        llm = _MockLLM(raise_exc=asyncio.TimeoutError())
        gen = _make_generator(llm=llm)
        result = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_wrapper_returns_none_on_permanent(self):
        gen = _make_generator(llm=None)
        result = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_wrapper_returns_none_on_client_unavailable(self):
        llm = _MockLLM(client=None)
        gen = _make_generator(llm=llm)
        result = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert result is None
