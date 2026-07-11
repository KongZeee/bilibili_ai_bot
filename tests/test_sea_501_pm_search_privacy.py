"""
tests/test_sea_501_pm_search_privacy.py - SEA-501 私信搜索隐私边界测试

PRD-V5 §4.3 SEA-501：
- 默认配置下私信搜索不触发（PM scene enabled=false）
- PM 搜索启用 + 脱敏开启时，第三方后端只收到脱敏文本
- 评论搜索不受 PM 搜索关闭影响
- 配置校验：PM enabled=true 但 redact_query=false 时报错
- 第三方调用前记录 external_data_disclosure 审计
- 日志不包含原始私信文本

覆盖：
- redact_query_text() 脱敏覆盖度
- validate_web_search_config() 配置校验
- WebSearchService 场景开关 + 脱敏 + 审计
- AuditStore.record_external_disclosure()
"""
import asyncio
import hashlib
import logging
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bilibot.services.web_search import (
    WebSearchService,
    redact_query_text,
    validate_web_search_config,
)
from bilibot.services.audit_store import AuditStore


# ═════════════════════════════════════════════════════════════
#  辅助：构造配置
# ═════════════════════════════════════════════════════════════

def _make_config(pm_enabled=False, pm_redact=True, ws_enabled=True,
                 api_key="test-key-12345"):
    """构造 web_search 配置"""
    return {
        "web_search": {
            "enabled": ws_enabled,
            "backend": "tavily",
            "api_key": api_key,
            "max_results": 3,
            "daily_budget_per_account": 100,
            "scenes": {
                "reply_comment": {"enabled": True},
                "private_message": {
                    "enabled": pm_enabled,
                    "redact_query": pm_redact,
                },
                "proactive_video": {"enabled": True},
                "dynamic_post": {"enabled": False},
                "weekly_summary": {"enabled": False},
            },
        },
    }


class _MockLLM:
    """可控 Mock LLM"""

    def __init__(self, return_text='{"need_search": true, "query": "最新新闻"}'):
        self.client = True
        self._return_text = return_text

    async def generate(self, prompt, max_tokens=80, **kwargs):
        return self._return_text


# ═════════════════════════════════════════════════════════════
#  redact_query_text 单元测试
# ═════════════════════════════════════════════════════════════

class TestRedactQueryText:
    """PRD-V5 §4.3 SEA-501：查询脱敏覆盖度"""

    def test_phone_redacted(self):
        text = "我的手机是13812345678，帮我查下"
        redacted, types = redact_query_text(text)
        assert "13812345678" not in redacted
        assert "[REDACTED:phone]" in redacted
        assert "phone" in types

    def test_email_redacted(self):
        text = "发到我邮箱 test@example.com 谢谢"
        redacted, types = redact_query_text(text)
        assert "test@example.com" not in redacted
        assert "[REDACTED:email]" in redacted
        assert "email" in types

    def test_uid_redacted(self):
        text = "用户UID 12345678 发了什么"
        redacted, types = redact_query_text(text)
        assert "12345678" not in redacted
        assert "[REDACTED:uid]" in redacted
        assert "uid" in types

    def test_cookie_redacted(self):
        text = "SESSDATA=abc123def456 这是cookie"
        redacted, types = redact_query_text(text)
        assert "abc123def456" not in redacted
        assert "[REDACTED:cookie]" in redacted
        assert "cookie" in types

    def test_bili_jct_cookie_redacted(self):
        text = "bili_jct=xyz789token"
        redacted, types = redact_query_text(text)
        assert "xyz789token" not in redacted
        assert "cookie" in types

    def test_url_token_redacted(self):
        text = "url里 access_key=AKID1234567890abcdef"
        redacted, types = redact_query_text(text)
        assert "AKID1234567890abcdef" not in redacted
        assert "token" in types

    def test_id_card_redacted(self):
        text = "身份证 110101199001011234"
        redacted, types = redact_query_text(text)
        assert "110101199001011234" not in redacted
        # 18 位号码可能被银行卡模式（16-19 位）先于身份证模式捕获
        assert ("id_card" in types or "card" in types)

    def test_order_number_redacted(self):
        text = "订单号 ABC1234567890XYZ"
        redacted, types = redact_query_text(text)
        assert "ABC1234567890XYZ" not in redacted
        assert "order" in types

    def test_long_number_redacted(self):
        text = "参考号 1234567890123456789"
        redacted, types = redact_query_text(text)
        assert "1234567890123456789" not in redacted
        # 19 位号码可能被银行卡模式（16-19 位）先于 uid/number 模式捕获
        assert ("uid" in types or "number" in types or "card" in types)

    def test_multiple_fields_redacted(self):
        text = "手机13812345678 邮箱a@b.com UID 12345678"
        redacted, types = redact_query_text(text)
        assert "13812345678" not in redacted
        assert "a@b.com" not in redacted
        assert "12345678" not in redacted
        assert "phone" in types
        assert "email" in types
        assert "uid" in types

    def test_no_sensitive_data_unchanged(self):
        text = "今天的最新新闻是什么"
        redacted, types = redact_query_text(text)
        assert redacted == text
        assert types == ""

    def test_empty_query(self):
        redacted, types = redact_query_text("")
        assert redacted == ""
        assert types == ""

    def test_idempotent(self):
        """脱敏后的文本再次脱敏不应改变"""
        text = "手机13812345678"
        redacted1, _ = redact_query_text(text)
        redacted2, _ = redact_query_text(redacted1)
        assert redacted1 == redacted2


# ═════════════════════════════════════════════════════════════
#  validate_web_search_config 单元测试
# ═════════════════════════════════════════════════════════════

class TestValidateWebSearchConfig:
    """PRD-V5 §4.3 SEA-501：配置校验"""

    def test_pm_disabled_valid(self):
        """PM 搜索关闭时，redact_query 无论真假都通过"""
        config = _make_config(pm_enabled=False, pm_redact=False)
        validate_web_search_config(config)  # 不抛异常

    def test_pm_enabled_redact_true_valid(self):
        config = _make_config(pm_enabled=True, pm_redact=True)
        validate_web_search_config(config)  # 不抛异常

    def test_pm_enabled_redact_false_invalid(self):
        """PM enabled=true 但 redact_query=false 时校验失败"""
        config = _make_config(pm_enabled=True, pm_redact=False)
        with pytest.raises(ValueError, match="redact_query"):
            validate_web_search_config(config)

    def test_pm_enabled_redact_missing_invalid(self):
        """PM enabled=true 但缺少 redact_query 时校验失败"""
        config = {
            "web_search": {
                "enabled": True,
                "api_key": "test",
                "scenes": {
                    "private_message": {"enabled": True},
                },
            },
        }
        with pytest.raises(ValueError, match="redact_query"):
            validate_web_search_config(config)

    def test_empty_config_valid(self):
        validate_web_search_config({})
        validate_web_search_config(None)

    def test_no_web_search_section_valid(self):
        validate_web_search_config({"llm": {}})


# ═════════════════════════════════════════════════════════════
#  AuditStore.record_external_disclosure 测试
# ═════════════════════════════════════════════════════════════

class TestAuditStoreExternalDisclosure:
    """PRD-V5 §4.3 SEA-501：外部数据披露审计"""

    @pytest.fixture
    def store(self, tmp_data_dir):
        return AuditStore(data_dir=tmp_data_dir)

    def test_record_returns_id(self, store):
        did = store.record_external_disclosure(
            scene="private_message",
            backend="tavily",
            query_hash="abc123",
            redacted_preview="最新新闻",
            field_types="phone,email",
        )
        assert did.startswith("disc_")

    def test_query_finds_record(self, store):
        store.record_external_disclosure(
            scene="private_message",
            backend="tavily",
            query_hash="hash123",
            redacted_preview="预览",
            field_types="uid",
        )
        results = store.query_external_disclosures(scene="private_message")
        assert len(results) == 1
        assert results[0]["backend"] == "tavily"
        assert results[0]["query_hash"] == "hash123"
        assert results[0]["field_types"] == "uid"

    def test_no_raw_query_stored(self, store):
        """审计记录中不存储原始查询文本"""
        raw_query = "我的手机是13812345678"
        query_hash = hashlib.sha256(raw_query.encode()).hexdigest()[:16]
        store.record_external_disclosure(
            scene="private_message",
            backend="tavily",
            query_hash=query_hash,
            redacted_preview="[REDACTED:phone] 帮我查下",
            field_types="phone",
        )
        results = store.query_external_disclosures()
        for r in results:
            # 原始手机号不应出现在任何字段中
            assert "13812345678" not in r.get("redacted_preview", "")
            assert "13812345678" not in r.get("query_hash", "")
            assert "13812345678" not in r.get("field_types", "")


# ═════════════════════════════════════════════════════════════
#  WebSearchService 集成测试
# ═════════════════════════════════════════════════════════════

class TestWebSearchServicePMPrivacy:
    """PRD-V5 §4.3 SEA-501：WebSearchService 私信搜索隐私边界"""

    @pytest.fixture
    def audit_store(self, tmp_data_dir):
        return AuditStore(data_dir=tmp_data_dir)

    @pytest.mark.asyncio
    async def test_pm_search_disabled_by_default(self, audit_store):
        """默认配置下 PM 搜索不触发（0 第三方调用）"""
        config = _make_config(pm_enabled=False)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(), audit_store=audit_store,
        )
        # PM 场景未启用 → should_search_for_reply 返回空串
        query = await svc.should_search_for_reply(
            user_comment="帮我查一下最新新闻",
            scene="private_message",
        )
        assert query == ""
        # 直接调用 search 也应返回 None
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            result = await svc.search("最新新闻", scene="private_message")
            assert result is None
            mock_be.assert_not_called()

    @pytest.mark.asyncio
    async def test_pm_search_enabled_redacts_sensitive_data(self, audit_store):
        """PM 搜索启用 + 脱敏开启 → 第三方后端只收到脱敏文本"""
        config = _make_config(pm_enabled=True, pm_redact=True)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(), audit_store=audit_store,
        )
        raw_query = "手机13812345678 邮箱test@example.com 最新新闻"
        captured_queries = []

        async def _capture_query(q):
            captured_queries.append(q)
            return {"answer": "结果", "items": []}

        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.side_effect = _capture_query
            result = await svc.search(raw_query, scene="private_message")

        assert result is not None
        assert len(captured_queries) == 1
        sent = captured_queries[0]
        # 第三方收到的查询中不应包含手机号和邮箱
        assert "13812345678" not in sent
        assert "test@example.com" not in sent
        assert "[REDACTED:phone]" in sent
        assert "[REDACTED:email]" in sent

    @pytest.mark.asyncio
    async def test_comment_search_works_when_pm_off(self, audit_store):
        """评论搜索在 PM 搜索关闭时仍正常工作"""
        config = _make_config(pm_enabled=False)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(), audit_store=audit_store,
        )
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "新闻结果", "items": []}
            result = await svc.search("最新新闻", scene="reply_comment")
            assert result is not None
            mock_be.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_record_created_before_backend_call(self, audit_store):
        """第三方调用前记录 external_data_disclosure 审计"""
        config = _make_config(pm_enabled=True, pm_redact=True)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(), audit_store=audit_store,
        )

        audit_calls_before = len(audit_store.query_external_disclosures())

        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "结果", "items": []}
            await svc.search("最新新闻", scene="private_message")

        audit_calls_after = len(audit_store.query_external_disclosures())
        assert audit_calls_after == audit_calls_before + 1

        disc = audit_store.query_external_disclosures(scene="private_message")[0]
        assert disc["backend"] == "tavily"
        assert disc["scene"] == "private_message"
        assert disc["query_hash"]  # 非空
        # 不含原始查询文本（只有 hash 和预览）
        assert "最新新闻" not in disc["query_hash"]

    @pytest.mark.asyncio
    async def test_logs_dont_contain_raw_pm_text(self, audit_store, caplog):
        """日志不包含原始私信文本"""
        config = _make_config(pm_enabled=True, pm_redact=True)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(), audit_store=audit_store,
        )
        raw_query = "手机13812345678 邮箱test@example.com 最新新闻"

        with caplog.at_level(logging.DEBUG, logger="bilibot.services.web_search"):
            with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
                mock_be.return_value = {"answer": "结果", "items": []}
                await svc.search(raw_query, scene="private_message")

        full_log = caplog.text
        # 原始手机号和邮箱不应出现在任何日志行中
        assert "13812345678" not in full_log
        assert "test@example.com" not in full_log

    @pytest.mark.asyncio
    async def test_pm_config_validation_fails_on_init(self):
        """PM enabled=true 但 redact_query=false 时 WebSearchService 初始化失败"""
        config = _make_config(pm_enabled=True, pm_redact=False)
        with pytest.raises(ValueError, match="redact_query"):
            WebSearchService(config, llm_provider=_MockLLM())

    @pytest.mark.asyncio
    async def test_should_search_for_reply_pm_disabled_returns_empty(self, audit_store):
        """PM 场景未启用时 should_search_for_reply 返回空串"""
        config = _make_config(pm_enabled=False)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(), audit_store=audit_store,
        )
        query = await svc.should_search_for_reply(
            user_comment="帮我查一下最新的科技新闻",
            scene="private_message",
        )
        assert query == ""

    @pytest.mark.asyncio
    async def test_should_search_for_reply_pm_enabled_redacts(self, audit_store):
        """PM 场景启用时 should_search_for_reply 返回脱敏后的查询"""
        config = _make_config(pm_enabled=True, pm_redact=True)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(
                return_text='{"need_search": true, "query": "手机13812345678最新新闻"}'
            ),
            audit_store=audit_store,
        )
        query = await svc.should_search_for_reply(
            user_comment="手机13812345678最新新闻",
            scene="private_message",
        )
        assert query != ""
        assert "13812345678" not in query
        assert "[REDACTED:phone]" in query

    @pytest.mark.asyncio
    async def test_comment_scene_not_redacted(self, audit_store):
        """评论场景不做脱敏（redact_query 默认 false）"""
        config = _make_config(pm_enabled=True, pm_redact=True)
        svc = WebSearchService(
            config, llm_provider=_MockLLM(
                return_text='{"need_search": true, "query": "手机13812345678最新新闻"}'
            ),
            audit_store=audit_store,
        )
        query = await svc.should_search_for_reply(
            user_comment="手机13812345678最新新闻",
            scene="reply_comment",
        )
        # 评论场景不脱敏
        assert "13812345678" in query


# ═════════════════════════════════════════════════════════════
#  ReplyGenerator scene 参数测试
# ═════════════════════════════════════════════════════════════

class TestReplyGeneratorSceneParameter:
    """PRD-V5 §4.3 SEA-501：ReplyGenerator scene 参数透传"""

    def test_invalid_scene_raises(self):
        from bilibot.reply import ReplyGenerator, _validate_search_scene
        with pytest.raises(ValueError, match="不支持的搜索场景"):
            _validate_search_scene("invalid_scene")

    def test_valid_scenes_accepted(self):
        from bilibot.reply import _validate_search_scene
        for s in ("reply_comment", "private_message", "proactive_video",
                  "dynamic_post", "weekly_summary"):
            assert _validate_search_scene(s) == s

    @pytest.mark.asyncio
    async def test_generate_reply_accepts_scene_param(self):
        """generate_reply 接受 scene 参数且默认为 reply_comment"""
        from bilibot.reply import ReplyGenerator

        class _MockLLM:
            client = True
            async def generate(self, prompt, system_prompt="", max_tokens=200, **kwargs):
                return "回复"

        gen = ReplyGenerator(
            user_state=None, personality_system=None,
            llm_adapter=_MockLLM(), data_store=None, config=None,
        )
        # 私信场景
        result = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1", scene="private_message",
        )
        assert result is not None
        assert result["reply"] == "回复"

        # 默认场景
        result2 = await gen.generate_reply(
            user_id="u1", username="张三", comment="你好",
            thread_id="t1", oid="o1",
        )
        assert result2 is not None

    @pytest.mark.asyncio
    async def test_generate_reply_invalid_scene_raises(self):
        from bilibot.reply import ReplyGenerator

        class _MockLLM:
            client = True
            async def generate(self, prompt, system_prompt="", max_tokens=200, **kwargs):
                return "回复"

        gen = ReplyGenerator(
            user_state=None, personality_system=None,
            llm_adapter=_MockLLM(), data_store=None, config=None,
        )
        with pytest.raises(ValueError, match="不支持的搜索场景"):
            await gen.generate_reply(
                user_id="u1", username="张三", comment="你好",
                thread_id="t1", oid="o1", scene="bad_scene",
            )
