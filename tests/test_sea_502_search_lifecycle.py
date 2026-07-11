"""
tests/test_sea_502_search_lifecycle.py - SEA-502 搜索服务生命周期测试

PRD-V5 §10.1 / §10.2 SEA-502：
- 账号级缓存隔离，初始化调用 _load_cache()
- 共享长连接 Session，close() 关闭
- 同账号同后端同 query 同 freshness 并发请求合并为 in-flight（一次 API 调用）
- 429/5xx/网络错误带抖动指数退避，受 TaskRun 超时控制
- 日预算持久化，重启不清零，预算键含 account_id + date
- 结构化结果保留 citations；Perplexity 必须保留 citations
- Custom 后端必须能力探测或显式 supports_web_search=true
"""
import asyncio
import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bilibot.services.web_search import WebSearchService, _RetryableSearchError
from bilibot.data_store import DataStore


# ═════════════════════════════════════════════════════════════
#  辅助：构造配置
# ═════════════════════════════════════════════════════════════

def _make_config(backend="tavily", ws_enabled=True, api_key="test-key-12345",
                 daily_budget=100, supports_web_search=None,
                 retry=None, api_base="", model=""):
    """构造 web_search 配置"""
    ws = {
        "enabled": ws_enabled,
        "backend": backend,
        "api_key": api_key,
        "api_base": api_base,
        "model": model,
        "max_results": 3,
        "daily_budget_per_account": daily_budget,
        "scenes": {
            "reply_comment": {"enabled": True},
            "proactive_video": {"enabled": True},
        },
    }
    if supports_web_search is not None:
        ws["supports_web_search"] = supports_web_search
    if retry is not None:
        ws["retry"] = retry
    return {"web_search": ws}


# ═════════════════════════════════════════════════════════════
#  Step 2: 账号级缓存隔离
# ═════════════════════════════════════════════════════════════

class TestAccountScopedCache:
    """PRD-V5 §10.1 SEA-502：账号级缓存隔离"""

    def test_cache_filename_isolated_by_account(self, tmp_data_dir):
        ds = DataStore(tmp_data_dir)
        svc = WebSearchService(
            _make_config(), data_store=ds, account_id="acc1",
        )
        assert svc._cache_filename == "acc1_search_cache.json"
        assert svc._budget_filename == "acc1_search_budget.json"

    def test_cache_filename_default_when_no_account(self, tmp_data_dir):
        ds = DataStore(tmp_data_dir)
        svc = WebSearchService(_make_config(), data_store=ds)
        assert svc._cache_filename == "web_search_cache.json"
        assert svc._budget_filename == "search_budget.json"

    def test_cache_loaded_from_account_specific_file_on_init(self, tmp_data_dir):
        """初始化时从账号专属缓存文件加载"""
        ds = DataStore(tmp_data_dir)
        # 预写 acc1 的缓存文件
        cache_payload = {
            "tavily:测试查询": {
                "ts": 9999999999.0,
                "result": {"answer": "预存答案", "items": [], "backend": "tavily"},
            }
        }
        ds.save_json("acc1_search_cache.json", cache_payload)

        svc = WebSearchService(
            _make_config(), data_store=ds, account_id="acc1",
        )
        assert "tavily:测试查询" in svc._cache
        assert svc._cache["tavily:测试查询"]["result"]["answer"] == "预存答案"

    def test_cache_not_shared_between_accounts(self, tmp_data_dir):
        """不同账号缓存互不共享"""
        ds = DataStore(tmp_data_dir)
        ds.save_json("acc1_search_cache.json", {
            "tavily:q1": {"ts": 9999999999.0, "result": {"answer": "acc1结果"}},
        })
        # acc2 不应加载到 acc1 的缓存
        svc2 = WebSearchService(
            _make_config(), data_store=ds, account_id="acc2",
        )
        assert "tavily:q1" not in svc2._cache

    @pytest.mark.asyncio
    async def test_cache_persisted_to_account_file_after_search(self, tmp_data_dir):
        ds = DataStore(tmp_data_dir)
        svc = WebSearchService(
            _make_config(daily_budget=10), data_store=ds, account_id="accA",
        )
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "结果", "items": [], "citations": []}
            await svc.search("测试查询", scene="reply_comment")

        # 文件应写入 accA_search_cache.json
        # cache_key 格式为 "{backend}:{query}:{freshness}"，含 freshness 后缀
        raw = ds.load_json("accA_search_cache.json", {})
        assert any("tavily:测试查询" in k for k in raw.keys())
        # accB 文件不应存在
        assert not os.path.exists(os.path.join(tmp_data_dir, "accB_search_cache.json"))
        await svc.close()


# ═════════════════════════════════════════════════════════════
#  Step 3: 共享 Session 生命周期
# ═════════════════════════════════════════════════════════════

class TestSharedSessionLifecycle:
    """PRD-V5 §10.1 SEA-502：共享长连接 Session"""

    @pytest.mark.asyncio
    async def test_get_session_returns_shared_instance(self):
        svc = WebSearchService(_make_config())
        s1 = await svc._get_session()
        s2 = await svc._get_session()
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_session_closed_on_close(self):
        svc = WebSearchService(_make_config())
        session = await svc._get_session()
        assert not session.closed
        await svc.close()
        assert session.closed
        assert svc._session is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        svc = WebSearchService(_make_config())
        await svc._get_session()
        await svc.close()
        # 再次 close 不报错
        await svc.close()
        assert svc._session is None


# ═════════════════════════════════════════════════════════════
#  Step 4: in-flight 请求合并
# ═════════════════════════════════════════════════════════════

class TestInflightRequestMerging:
    """PRD-V5 §10.1 SEA-502：并发同查询请求合并"""

    @pytest.mark.asyncio
    async def test_concurrent_same_query_merged_into_one_call(self):
        svc = WebSearchService(_make_config(daily_budget=10))
        call_count = 0

        async def _slow_backend(q):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.05)
            return {"answer": f"结果{q}", "items": [], "citations": []}

        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.side_effect = _slow_backend
            # 并发发起两个相同查询
            r1, r2 = await asyncio.gather(
                svc.search("相同查询", scene="reply_comment"),
                svc.search("相同查询", scene="reply_comment"),
            )

        # 后端只被调用一次（in-flight 合并）
        assert call_count == 1
        assert r1 is not None
        assert r2 is not None
        # 两个调用者拿到相同结果
        assert r1["answer"] == r2["answer"]
        await svc.close()

    @pytest.mark.asyncio
    async def test_different_queries_not_merged(self):
        svc = WebSearchService(_make_config(daily_budget=10))

        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "结果", "items": [], "citations": []}
            await asyncio.gather(
                svc.search("查询A", scene="reply_comment"),
                svc.search("查询B", scene="reply_comment"),
            )

        assert mock_be.call_count == 2
        await svc.close()


# ═════════════════════════════════════════════════════════════
#  Step 5: 指数退避 + 抖动
# ═════════════════════════════════════════════════════════════

class TestExponentialBackoff:
    """PRD-V5 §10.1 SEA-502：429/5xx 指数退避带抖动"""

    @pytest.mark.asyncio
    async def test_429_triggers_retry_then_success(self):
        retry_cfg = {"base_delay": 0.01, "factor": 2.0, "max_delay": 0.05,
                     "max_attempts": 3}
        svc = WebSearchService(_make_config(daily_budget=10, retry=retry_cfg))

        call_count = 0

        async def _flaky_backend(q):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _RetryableSearchError("HTTP 429 Too Many Requests")
            return {"answer": "成功", "items": [], "citations": []}

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def _record_sleep(delay):
            sleep_calls.append(delay)
            await original_sleep(0)

        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.side_effect = _flaky_backend
            with patch("bilibot.services.web_search.asyncio.sleep",
                       new=_record_sleep):
                result = await svc.search("重试查询", scene="reply_comment")

        assert result is not None
        assert result["answer"] == "成功"
        # 首次 + 2 次重试 = 3 次
        assert call_count == 3
        # 至少触发了 2 次退避 sleep
        assert len(sleep_calls) >= 2
        # 第一次退避应在 base_delay ±25% 范围内
        first = sleep_calls[0]
        assert 0.01 * 0.75 <= first <= 0.01 * 1.25
        await svc.close()

    @pytest.mark.asyncio
    async def test_retry_exhausted_returns_none(self):
        retry_cfg = {"base_delay": 0.001, "factor": 2.0, "max_delay": 0.01,
                     "max_attempts": 2}
        svc = WebSearchService(_make_config(daily_budget=10, retry=retry_cfg))

        async def _always_429(q):
            raise _RetryableSearchError("HTTP 429")

        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.side_effect = _always_429
            with patch("bilibot.services.web_search.asyncio.sleep",
                       new=AsyncMock(return_value=None)):
                result = await svc.search("始终失败", scene="reply_comment")

        assert result is None
        # 首次 + 2 次重试 = 3 次
        assert mock_be.call_count == 3
        await svc.close()

    @pytest.mark.asyncio
    async def test_deadline_stops_retry(self):
        retry_cfg = {"base_delay": 10.0, "factor": 2.0, "max_delay": 60.0,
                     "max_attempts": 5}
        svc = WebSearchService(_make_config(daily_budget=10, retry=retry_cfg))

        async def _always_429(q):
            raise _RetryableSearchError("HTTP 429")

        # deadline 设为已过去 → 立即停止（但仍执行首次调用）
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.side_effect = _always_429
            with patch("bilibot.services.web_search.asyncio.sleep",
                       new=AsyncMock(return_value=None)):
                # deadline 已过：首次调用失败后不再重试
                result = await svc.search("deadline查询", scene="reply_comment",
                                          deadline=__import__("time").monotonic() - 1)

        assert result is None
        # deadline 已过，首次失败后即停止
        assert mock_be.call_count == 1
        await svc.close()


# ═════════════════════════════════════════════════════════════
#  Step 6: 日预算持久化
# ═════════════════════════════════════════════════════════════

class TestDailyBudgetPersistence:
    """PRD-V5 §10.1 SEA-502：日预算持久化，重启不清零"""

    def test_budget_key_includes_account_and_date(self):
        svc = WebSearchService(_make_config(), account_id="acc1")
        today = datetime.now().strftime("%Y-%m-%d")
        key = svc._budget_key(today)
        assert "acc1" in key
        assert today in key
        assert key == f"acc1:{today}"

    @pytest.mark.asyncio
    async def test_daily_budget_persisted_across_restart(self, tmp_data_dir):
        ds = DataStore(tmp_data_dir)
        svc1 = WebSearchService(
            _make_config(daily_budget=10), data_store=ds, account_id="accR",
        )
        with patch.object(svc1, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "结果", "items": [], "citations": []}
            await svc1.search("持久化测试", scene="reply_comment")

        # 预算文件已写入
        raw = ds.load_json("accR_search_budget.json", {})
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"accR:{today}"
        assert key in raw
        assert raw[key] == 1

        # 模拟重启：新建 service，加载持久化预算
        svc2 = WebSearchService(
            _make_config(daily_budget=10), data_store=ds, account_id="accR",
        )
        assert svc2._daily_count == 1  # 重启不清零
        await svc1.close()

    @pytest.mark.asyncio
    async def test_budget_exhausted_search_skipped(self, tmp_data_dir):
        ds = DataStore(tmp_data_dir)
        svc = WebSearchService(
            _make_config(daily_budget=1), data_store=ds, account_id="accE",
        )
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "第一次", "items": [], "citations": []}
            # 第一次：消耗预算
            r1 = await svc.search("查询一", scene="reply_comment")
            assert r1 is not None
            assert mock_be.call_count == 1
            # 第二次：预算耗尽 → 跳过
            r2 = await svc.search("查询二", scene="reply_comment")
            assert r2 is None
            # 后端未被再次调用
            assert mock_be.call_count == 1
        await svc.close()


# ═════════════════════════════════════════════════════════════
#  Step 7: citations 保留
# ═════════════════════════════════════════════════════════════

class TestCitationsPreservation:
    """PRD-V5 §10.2 SEA-502：结构化结果保留 citations"""

    @pytest.mark.asyncio
    async def test_perplexity_preserves_citations(self):
        svc = WebSearchService(_make_config(backend="perplexity", model="sonar"))

        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "搜索结果摘要"
        mock_resp.citations = [
            "https://example.com/source1",
            "https://example.com/source2",
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch.object(svc, "_get_openai_client", return_value=mock_client):
            result = await svc.search("perplexity查询", scene="reply_comment")

        assert result is not None
        assert result["backend"] == "perplexity"
        assert "citations" in result
        assert len(result["citations"]) == 2
        assert result["citations"][0]["url"] == "https://example.com/source1"
        assert result["citations"][1]["url"] == "https://example.com/source2"
        await svc.close()

    @pytest.mark.asyncio
    async def test_tavily_result_has_empty_citations(self):
        svc = WebSearchService(_make_config(backend="tavily"))
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "结果", "items": [],
                                    "citations": []}
            result = await svc.search("tavily查询", scene="reply_comment")
        assert result is not None
        assert result["citations"] == []
        await svc.close()

    @pytest.mark.asyncio
    async def test_structured_result_has_query_hash(self):
        svc = WebSearchService(_make_config(daily_budget=10))
        with patch.object(svc, "_search_tavily", new_callable=AsyncMock) as mock_be:
            mock_be.return_value = {"answer": "结果", "items": [], "citations": []}
            result = await svc.search("结构化查询", scene="reply_comment")
        assert result is not None
        assert "query_hash" in result
        assert len(result["query_hash"]) == 16
        assert result["cached"] is False
        await svc.close()


# ═════════════════════════════════════════════════════════════
#  Step 8: Custom 后端能力探测
# ═════════════════════════════════════════════════════════════

class TestCustomBackendCapabilityProbe:
    """PRD-V5 §10.2 SEA-502：Custom 后端能力探测"""

    @pytest.mark.asyncio
    async def test_custom_without_supports_web_search_not_used(self):
        """未声明 supports_web_search 且探测失败 → 不作为搜索后端"""
        svc = WebSearchService(_make_config(
            backend="custom", api_base="https://custom.example.com/v1",
            model="custom-model", supports_web_search=None,
        ))
        # 探测返回 False（普通 Chat Completions 无联网能力）
        with patch.object(svc, "_probe_custom_capability",
                          new_callable=AsyncMock) as mock_probe:
            mock_probe.return_value = False
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(choices=[]))
            with patch.object(svc, "_get_openai_client", return_value=mock_client):
                result = await svc.search("custom查询", scene="reply_comment")

        assert result is None
        # 探测被执行
        assert mock_probe.call_count == 1
        # 实际搜索 chat.completions.create 未被调用
        mock_client.chat.completions.create.assert_not_called()
        await svc.close()

    @pytest.mark.asyncio
    async def test_custom_with_supports_web_search_true_used(self):
        """显式 supports_web_search=true → 直接作为搜索后端"""
        svc = WebSearchService(_make_config(
            backend="custom", api_base="https://custom.example.com/v1",
            model="custom-model", supports_web_search=True,
        ))
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "custom 搜索结果"
        mock_resp.citations = []
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch.object(svc, "_get_openai_client", return_value=mock_client):
            result = await svc.search("custom显式查询", scene="reply_comment")

        assert result is not None
        assert result["backend"] == "custom"
        assert result["answer"] == "custom 搜索结果"
        # chat.completions.create 被调用
        mock_client.chat.completions.create.assert_called_once()
        await svc.close()

    @pytest.mark.asyncio
    async def test_custom_with_supports_web_search_false_not_used(self):
        """显式 supports_web_search=false → 不作为搜索后端"""
        svc = WebSearchService(_make_config(
            backend="custom", api_base="https://custom.example.com/v1",
            model="custom-model", supports_web_search=False,
        ))
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=MagicMock())

        with patch.object(svc, "_get_openai_client", return_value=mock_client):
            result = await svc.search("custom禁用查询", scene="reply_comment")

        assert result is None
        mock_client.chat.completions.create.assert_not_called()
        await svc.close()

    @pytest.mark.asyncio
    async def test_custom_probe_passes_then_used(self):
        """未声明但探测通过 → 作为搜索后端"""
        svc = WebSearchService(_make_config(
            backend="custom", api_base="https://custom.example.com/v1",
            model="custom-model", supports_web_search=None,
        ))
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "探测后结果"
        mock_resp.citations = []
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        with patch.object(svc, "_probe_custom_capability",
                          new_callable=AsyncMock) as mock_probe:
            mock_probe.return_value = True
            with patch.object(svc, "_get_openai_client", return_value=mock_client):
                result = await svc.search("探测通过查询", scene="reply_comment")

        assert result is not None
        assert result["answer"] == "探测后结果"
        await svc.close()

    def test_extract_perplexity_citations_from_list_of_strings(self):
        citations = WebSearchService._extract_perplexity_citations(
            MagicMock(citations=["https://a.com", "https://b.com"])
        )
        assert len(citations) == 2
        assert citations[0] == {"url": "https://a.com", "title": ""}

    def test_extract_perplexity_citations_from_list_of_dicts(self):
        citations = WebSearchService._extract_perplexity_citations(
            MagicMock(citations=[{"url": "https://a.com", "title": "A"}])
        )
        assert citations == [{"url": "https://a.com", "title": "A"}]

    def test_extract_perplexity_citations_empty(self):
        assert WebSearchService._extract_perplexity_citations(
            MagicMock(citations=None)) == []
        assert WebSearchService._extract_perplexity_citations(
            MagicMock(spec=["citations"])) == []
