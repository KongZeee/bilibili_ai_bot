"""
MEM-501：每账号记忆写入队列

PRD-V5 §9.1 要求：所有异步记忆写入通过有界队列，提供：
- 幂等键去重（同一 key 已在队列中则跳过）
- 有界队列长度（满时拒绝入队并告警）
- 指数退避重试（写失败后按 2^retries 秒退避）
- 死信队列（超过 max_retries 后移入 dead_letters）
- drain 优雅关闭（等待队列排空，带超时）

由 AccountInstance 拥有，注入到 Scheduler 使用。
业务代码通过 self.memory_write_queue.enqueue(...) 写入，不得直接 import memory_writer。
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

logger = logging.getLogger("bilibot.memory_write_queue")


@dataclass
class MemoryWriteItem:
    """队列中的单个写操作"""
    idempotency_key: str
    write_callable: Callable
    created_at: float
    retries: int = 0


@dataclass
class DeadLetter:
    """死信：超过最大重试次数的失败写操作"""
    idempotency_key: str
    error: str
    created_at: float
    failed_at: float
    retries: int


class MemoryWriteQueue:
    """MEM-501：每账号记忆写入队列

    有界异步队列，负责将记忆写入操作串行化、去重、重试。
    """

    def __init__(self, max_length: int = 1000, max_retries: int = 3):
        self.max_length = max_length
        self.max_retries = max_retries
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_length)
        self._pending_keys: set = set()
        self._dead_letters: List[DeadLetter] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._drain_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """启动后台 worker 任务"""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._running = True
        self._drain_event.clear()
        self._worker_task = asyncio.create_task(self._worker())
        logger.info(f"MemoryWriteQueue 已启动 (max_length={self.max_length})")

    async def enqueue(self, idempotency_key: str, write_callable: Callable) -> bool:
        """入队一个写操作

        幂等去重：如果 idempotency_key 已在待处理集合中，跳过入队。

        Args:
            idempotency_key: 幂等键（相同 key 的写操作只执行一次）
            write_callable: 写入回调，可以是 sync 或 async（返回 coroutine）

        Returns:
            True 如果入队成功，False 如果重复或队列已满或已停止
        """
        if not self._running:
            logger.warning(f"队列已停止，拒绝入队: {idempotency_key}")
            return False

        if idempotency_key in self._pending_keys:
            logger.debug(f"幂等去重，跳过入队: {idempotency_key}")
            return False

        if self._queue.full():
            logger.warning(
                f"记忆写入队列已满 (max={self.max_length})，丢弃: {idempotency_key}"
            )
            return False

        item = MemoryWriteItem(
            idempotency_key=idempotency_key,
            write_callable=write_callable,
            created_at=time.time(),
        )
        self._pending_keys.add(idempotency_key)
        await self._queue.put(item)
        return True

    async def _worker(self) -> None:
        """后台 worker：循环从队列取任务并执行"""
        current_item: Optional[MemoryWriteItem] = None
        try:
            while self._running or not self._queue.empty():
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                current_item = item
                await self._process_item(item)
                current_item = None
        finally:
            # MEM-604：worker 退出时（含 CancelledError）清理当前未处理项的幂等键，
            # 避免未来同 key 写入被 _pending_keys 误判跳过
            if current_item is not None:
                self._pending_keys.discard(current_item.idempotency_key)
            self._drain_event.set()
            logger.info("MemoryWriteQueue worker 已退出")

    async def _process_item(self, item: MemoryWriteItem) -> None:
        """处理单个写操作，失败时指数退避重试"""
        try:
            result = item.write_callable()
            if asyncio.iscoroutine(result):
                await result

            self._pending_keys.discard(item.idempotency_key)
            logger.debug(f"记忆写入成功: {item.idempotency_key}")
        except Exception as e:
            item.retries += 1
            if item.retries >= self.max_retries:
                dl = DeadLetter(
                    idempotency_key=item.idempotency_key,
                    error=str(e),
                    created_at=item.created_at,
                    failed_at=time.time(),
                    retries=item.retries,
                )
                self._dead_letters.append(dl)
                self._pending_keys.discard(item.idempotency_key)
                logger.error(
                    f"记忆写入死信（{item.retries} 次重试后）: "
                    f"{item.idempotency_key} - {e}"
                )
            else:
                backoff = 2 ** item.retries
                logger.warning(
                    f"记忆写入失败，{backoff}s 后重试 "
                    f"({item.retries}/{self.max_retries}): "
                    f"{item.idempotency_key} - {e}"
                )
                await asyncio.sleep(backoff)
                await self._queue.put(item)

    async def drain(self, timeout: float = 10.0) -> bool:
        """优雅关闭：停止接收新任务，等待队列排空

        Args:
            timeout: 最大等待时间（秒）

        Returns:
            True 如果在超时前排空，False 如果超时
        """
        self._running = False

        if self._worker_task is None:
            return True

        try:
            await asyncio.wait_for(self._drain_event.wait(), timeout=timeout)
            logger.info(
                f"MemoryWriteQueue 已排空 (dead_letters={len(self._dead_letters)})"
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"MemoryWriteQueue drain 超时 ({timeout}s)，"
                f"剩余 {self._queue.qsize()} 项"
            )
            return False
        finally:
            if self._worker_task and not self._worker_task.done():
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
            self._worker_task = None
            # MEM-604：超时取消后清理剩余幂等键，防止同 key 写入被永久跳过
            self._pending_keys.clear()

    @property
    def dead_letters(self) -> List[DeadLetter]:
        """死信列表（只读副本）"""
        return list(self._dead_letters)

    @property
    def pending_count(self) -> int:
        """队列中待处理项数"""
        return self._queue.qsize()

    @property
    def size(self) -> int:
        """队列当前大小"""
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        """队列是否在运行"""
        return self._running
