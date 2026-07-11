"""
tests/test_mem_501_single_instance.py - MEM-501 每账号单记忆实例测试

PRD-V5 §9.1 要求：
1. AccountInstance 是 KnowledgeBaseMemory 的唯一所有者
2. Scheduler 不再自行创建 KnowledgeBaseMemory，由 AccountInstance 注入
3. 所有异步写入通过 MemoryWriteQueue（幂等键、有界、退避、死信、drain）
4. 业务代码不得直接 import memory_writer

覆盖：
- AccountInstance 创建 KnowledgeBaseMemory 恰好一次
- Scheduler 不创建自己的 KnowledgeBaseMemory（接收注入）
- close() 在关闭时恰好调用一次
- flush() 在关闭时恰好调用一次
- MemoryWriteQueue 幂等键去重
- MemoryWriteQueue 指数退避重试
- MemoryWriteQueue 死信（超过 max_retries）
- MemoryWriteQueue drain 优雅关闭
- 业务代码不直接 import memory_writer（AST 检查）
"""
import ast
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


# ═══════════════════════════════════════════════════════
#  MemoryWriteQueue 单元测试
# ═══════════════════════════════════════════════════════

class TestMemoryWriteQueueDedup:
    """幂等键去重"""

    @pytest.mark.asyncio
    async def test_dedup_skips_duplicate_key(self):
        """相同 idempotency_key 的写操作只执行一次"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        queue = MemoryWriteQueue(max_length=100, max_retries=1)
        await queue.start()

        call_count = 0

        def write_fn():
            nonlocal call_count
            call_count += 1

        # 入队两个相同 key 的写操作
        r1 = await queue.enqueue("dup_key", write_fn)
        r2 = await queue.enqueue("dup_key", write_fn)

        assert r1 is True   # 第一次入队成功
        assert r2 is False  # 第二次被去重跳过

        # 等待 worker 处理
        await asyncio.sleep(0.3)
        await queue.drain(timeout=2.0)

        assert call_count == 1  # 只执行了一次


class TestMemoryWriteQueueBackoff:
    """指数退避重试"""

    @pytest.mark.asyncio
    async def test_retry_on_failure_with_backoff(self):
        """写失败时按指数退避重试"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        queue = MemoryWriteQueue(max_length=100, max_retries=3)
        await queue.start()

        attempt_times = []

        async def failing_write():
            attempt_times.append(time.time())
            raise RuntimeError("simulated failure")

        await queue.enqueue("backoff_key", failing_write)

        # 等待足够时间让重试完成（2+4=6s 最少）
        await asyncio.sleep(0.5)
        # 此时应该已尝试 1 次，等待 2s 重试
        await asyncio.sleep(2.5)
        # 第 2 次重试，等待 4s
        await asyncio.sleep(4.5)
        # 第 3 次重试 → 死信

        await queue.drain(timeout=1.0)

        # 应该有 3 次尝试（初始 + 2 次重试 = 3，因为 max_retries=3）
        assert len(attempt_times) >= 2  # 至少重试了一次

        # 验证退避间隔（第一次失败后至少等 2s）
        if len(attempt_times) >= 2:
            gap = attempt_times[1] - attempt_times[0]
            assert gap >= 1.8  # 允许一点误差

    @pytest.mark.asyncio
    async def test_dead_letter_after_max_retries(self):
        """超过 max_retries 后进入死信"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        queue = MemoryWriteQueue(max_length=100, max_retries=2)
        await queue.start()

        call_count = 0

        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("permanent failure")

        await queue.enqueue("dead_key", always_fail)

        # 等待足够时间让所有重试完成（2s + 4s = 6s）
        await asyncio.sleep(7.5)
        await queue.drain(timeout=1.0)

        # 应该尝试了 max_retries 次
        assert call_count == 2
        # 死信列表中有 1 条
        dead = queue.dead_letters
        assert len(dead) == 1
        assert dead[0].idempotency_key == "dead_key"
        assert "permanent failure" in dead[0].error


class TestMemoryWriteQueueDrain:
    """drain 优雅关闭"""

    @pytest.mark.asyncio
    async def test_drain_waits_for_pending(self):
        """drain 等待队列排空"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        queue = MemoryWriteQueue(max_length=100, max_retries=1)
        await queue.start()

        results = []

        async def slow_write():
            await asyncio.sleep(0.3)
            results.append("done")

        await queue.enqueue("drain_key", slow_write)

        # drain 应等待 slow_write 完成
        ok = await queue.drain(timeout=5.0)
        assert ok is True
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_drain_rejects_new_enqueue(self):
        """drain 后拒绝入队"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        queue = MemoryWriteQueue(max_length=100, max_retries=1)
        await queue.start()
        await queue.drain(timeout=1.0)

        # 队列已停止
        r = await queue.enqueue("after_drain", lambda: None)
        assert r is False

    @pytest.mark.asyncio
    async def test_drain_timeout_returns_false(self):
        """drain 超时返回 False"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        queue = MemoryWriteQueue(max_length=100, max_retries=10)
        await queue.start()

        async def very_slow_write():
            await asyncio.sleep(10.0)

        await queue.enqueue("slow_key", very_slow_write)
        await asyncio.sleep(0.2)  # 让 worker 取到任务

        # drain 超时
        ok = await queue.drain(timeout=0.5)
        assert ok is False


class TestMemoryWriteQueueBounded:
    """有界队列"""

    @pytest.mark.asyncio
    async def test_full_queue_rejects_enqueue(self):
        """队列满时拒绝入队"""
        from bilibot.services.memory_write_queue import MemoryWriteQueue

        # max_length=2，但 worker 在处理之前先入队
        queue = MemoryWriteQueue(max_length=2, max_retries=1)
        await queue.start()

        async def slow_write():
            await asyncio.sleep(1.0)

        r1 = await queue.enqueue("k1", slow_write)
        r2 = await queue.enqueue("k2", slow_write)
        r3 = await queue.enqueue("k3", slow_write)  # 应该被拒绝

        assert r1 is True
        assert r2 is True
        assert r3 is False  # 队列满

        await queue.drain(timeout=5.0)


# ═══════════════════════════════════════════════════════
#  Scheduler 单实例注入测试
# ═══════════════════════════════════════════════════════

class TestSchedulerNoDuplicateMemory:
    """Scheduler 不创建自己的 KnowledgeBaseMemory"""

    def test_scheduler_receives_injected_knowledge_memory(self, tmp_data_dir):
        """Scheduler 接收注入的 knowledge_memory 而非自行创建"""
        from bilibot.scheduler import Scheduler
        from bilibot.app.config_loader import ConfigLoader

        config = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
            "llm": {"api_key": "test", "base_url": "http://localhost:8000/v1", "model": "test"},
            "data_dir": tmp_data_dir,
        })

        injected_km = MagicMock()

        sched = Scheduler(
            config_loader=config,
            knowledge_memory=injected_km,
            memory_write_queue=None,
        )

        # Scheduler 使用注入的实例
        assert sched.knowledge_memory is injected_km

    def test_scheduler_does_not_create_knowledge_base_memory(self, tmp_data_dir):
        """Scheduler 构造时不创建 KnowledgeBaseMemory"""
        from bilibot.scheduler import Scheduler
        from bilibot.app.config_loader import ConfigLoader

        config = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
            "llm": {"api_key": "test", "base_url": "http://localhost:8000/v1", "model": "test"},
            "data_dir": tmp_data_dir,
        })

        with patch("bilibot.knowledge_memory.KnowledgeBaseMemory") as mock_kbm:
            sched = Scheduler(
                config_loader=config,
                knowledge_memory=None,
                memory_write_queue=None,
            )
            # Scheduler 不应创建 KnowledgeBaseMemory
            mock_kbm.assert_not_called()
            assert sched.knowledge_memory is None

    def test_scheduler_receives_memory_write_queue(self, tmp_data_dir):
        """Scheduler 接收注入的 memory_write_queue"""
        from bilibot.scheduler import Scheduler
        from bilibot.app.config_loader import ConfigLoader

        config = ConfigLoader(config_dict={
            "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
            "llm": {"api_key": "test", "base_url": "http://localhost:8000/v1", "model": "test"},
            "data_dir": tmp_data_dir,
        })

        injected_queue = MagicMock()
        sched = Scheduler(
            config_loader=config,
            knowledge_memory=None,
            memory_write_queue=injected_queue,
        )
        assert sched.memory_write_queue is injected_queue


# ═══════════════════════════════════════════════════════
#  AccountInstance 单实例测试
# ═══════════════════════════════════════════════════════

class TestAccountInstanceSingleMemory:
    """AccountInstance 创建 KnowledgeBaseMemory 恰好一次"""

    @pytest.mark.asyncio
    async def test_knowledge_memory_created_once(self, tmp_data_dir):
        """AccountInstance.initialize 只创建一个 KnowledgeBaseMemory"""
        from bilibot.account.instance import AccountInstance
        from bilibot.services.persona_store import PersonaStore

        ps = PersonaStore(data_dir=tmp_data_dir)
        mock_llm_mgr = MagicMock()
        mock_llm = MagicMock()
        mock_llm.get_embedding = AsyncMock(return_value=[0.1] * 8)
        mock_llm_mgr.resolve_provider.return_value = (mock_llm, "test", "")

        config_loader = MagicMock()
        config_loader.get_raw_config.return_value = {}

        acc = AccountInstance(
            account_id="test_acc",
            account_config={
                "name": "Test",
                "sessdata": "s",
                "bili_jct": "j",
                "dede_user_id": "123",
                "buvid3": "b",
                "refresh_token": "r",
                "persona_id": "",
                "llm_id": "",
                "enabled": True,
            },
            persona_store=ps,
            llm_manager=mock_llm_mgr,
            audit_store=MagicMock(),
            orchestrator=MagicMock(),
            context_builder=MagicMock(),
            app_config_loader=config_loader,
            data_root=tmp_data_dir,
        )

        with patch(
            "bilibot.knowledge_memory.KnowledgeBaseMemory"
        ) as mock_kbm_cls:
            mock_kbm_instance = MagicMock()
            mock_kbm_instance.flush = MagicMock()
            mock_kbm_instance.close = MagicMock()
            mock_kbm_cls.return_value = mock_kbm_instance

            await acc.initialize()

            # KnowledgeBaseMemory 构造函数恰好调用一次
            assert mock_kbm_cls.call_count == 1
            # AccountInstance 持有该实例
            assert acc.knowledge_memory is mock_kbm_instance
            # Scheduler 也使用同一个实例
            assert acc.scheduler.knowledge_memory is mock_kbm_instance

        await acc.close()

    @pytest.mark.asyncio
    async def test_close_calls_flush_and_close_once(self, tmp_data_dir):
        """close() 恰好调用一次 flush 和 close"""
        from bilibot.account.instance import AccountInstance
        from bilibot.services.persona_store import PersonaStore

        ps = PersonaStore(data_dir=tmp_data_dir)
        mock_llm_mgr = MagicMock()
        mock_llm = MagicMock()
        mock_llm.get_embedding = AsyncMock(return_value=[0.1] * 8)
        mock_llm_mgr.resolve_provider.return_value = (mock_llm, "test", "")

        config_loader = MagicMock()
        config_loader.get_raw_config.return_value = {}

        acc = AccountInstance(
            account_id="test_acc",
            account_config={
                "name": "Test",
                "sessdata": "s",
                "bili_jct": "j",
                "dede_user_id": "123",
                "buvid3": "b",
                "refresh_token": "r",
                "persona_id": "",
                "llm_id": "",
                "enabled": True,
            },
            persona_store=ps,
            llm_manager=mock_llm_mgr,
            audit_store=MagicMock(),
            orchestrator=MagicMock(),
            context_builder=MagicMock(),
            app_config_loader=config_loader,
            data_root=tmp_data_dir,
        )

        await acc.initialize()

        # 替换为 mock 以精确计数
        flush_mock = MagicMock()
        close_mock = MagicMock()
        acc.knowledge_memory.flush = flush_mock
        acc.knowledge_memory.close = close_mock

        await acc.close()

        # flush 恰好调用一次
        assert flush_mock.call_count == 1
        # close 恰好调用一次
        assert close_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_close_drains_queue_before_memory_close(self, tmp_data_dir):
        """close() 先 drain 队列，再 flush/close 记忆"""
        from bilibot.account.instance import AccountInstance
        from bilibot.services.persona_store import PersonaStore

        ps = PersonaStore(data_dir=tmp_data_dir)
        mock_llm_mgr = MagicMock()
        mock_llm = MagicMock()
        mock_llm.get_embedding = AsyncMock(return_value=[0.1] * 8)
        mock_llm_mgr.resolve_provider.return_value = (mock_llm, "test", "")

        config_loader = MagicMock()
        config_loader.get_raw_config.return_value = {}

        acc = AccountInstance(
            account_id="test_acc",
            account_config={
                "name": "Test",
                "sessdata": "s",
                "bili_jct": "j",
                "dede_user_id": "123",
                "buvid3": "b",
                "refresh_token": "r",
                "persona_id": "",
                "llm_id": "",
                "enabled": True,
            },
            persona_store=ps,
            llm_manager=mock_llm_mgr,
            audit_store=MagicMock(),
            orchestrator=MagicMock(),
            context_builder=MagicMock(),
            app_config_loader=config_loader,
            data_root=tmp_data_dir,
        )

        await acc.initialize()

        # 记录调用顺序
        call_order = []

        async def fake_drain(timeout=10.0):
            call_order.append("drain")
            return True

        def fake_flush():
            call_order.append("flush")

        def fake_close():
            call_order.append("close")

        acc.memory_write_queue.drain = fake_drain
        acc.knowledge_memory.flush = fake_flush
        acc.knowledge_memory.close = fake_close

        await acc.close()

        # 顺序必须是 drain → flush → close
        assert call_order == ["drain", "flush", "close"]


# ═══════════════════════════════════════════════════════
#  业务代码不直接 import memory_writer 测试
# ═══════════════════════════════════════════════════════

class TestNoDirectMemoryWriterImport:
    """业务代码不得直接 import memory_writer"""

    def test_scheduler_does_not_import_memory_writer(self):
        """scheduler.py 的 AST 中没有 memory_writer 的 import"""
        scheduler_path = Path(__file__).parent.parent / "bilibot" / "scheduler.py"
        source = scheduler_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        memory_writer_imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "memory_writer" in node.module:
                    memory_writer_imports.append(
                        f"line {node.lineno}: from {node.module} import ..."
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "memory_writer" in alias.name:
                        memory_writer_imports.append(
                            f"line {node.lineno}: import {alias.name}"
                        )

        assert memory_writer_imports == [], (
            f"scheduler.py 不应直接 import memory_writer，发现: {memory_writer_imports}"
        )

    def test_scheduler_does_not_construct_knowledge_base_memory(self):
        """scheduler.py 的 AST 中没有 KnowledgeBaseMemory 构造调用"""
        scheduler_path = Path(__file__).parent.parent / "bilibot" / "scheduler.py"
        source = scheduler_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        kbm_calls = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # 检测 KnowledgeBaseMemory(...) 构造
                if isinstance(func, ast.Name) and func.id == "KnowledgeBaseMemory":
                    kbm_calls.append(f"line {node.lineno}")
                elif isinstance(func, ast.Attribute) and func.attr == "KnowledgeBaseMemory":
                    kbm_calls.append(f"line {node.lineno}")

        assert kbm_calls == [], (
            f"scheduler.py 不应构造 KnowledgeBaseMemory，发现: {kbm_calls}"
        )
