"""TaskRun retry processing extracted from scheduler.py."""

from __future__ import annotations

import logging

logger = logging.getLogger("bilibot.scheduler_task_retry")


async def process_retryable_tasks(self):
    """Task 5：处理 retry_wait 状态的 TaskRun（到期自动重试）

    - 拉取 task_store.list_retryable() 中到期的 retry_wait 任务
    - 对每个任务 retry() → claim() → 分发到对应的 _do_xxx 方法
      （start() 由 _do_xxx 方法内部完成，与手动/定时路径一致）
    - 不支持自动重试的场景标记失败
    """
    try:
        retryable = self.task_store.list_retryable()
        if not retryable:
            return
        logger.info(f"Task 5: 发现 {len(retryable)} 个到期可重试 TaskRun")
        for task in retryable:
            # retry_wait → scheduled（not_before=now, trigger_type=retry）
            if not self.task_store.retry(task.task_id):
                logger.warning(f"Task 5: TaskRun {task.task_id} retry 失败")
                continue
            if not self._dispatch_task_run(
                task.task_id, tag_prefix="retry", claim_if_scheduled=True,
            ):
                logger.warning(
                    f"Task 5: TaskRun {task.task_id} 场景 {task.scene} "
                    f"不支持自动重试，标记失败"
                )
                self._fail_task(
                    task.task_id, "UNSUPPORTED_RETRY_SCENE",
                    f"场景 {task.scene} 不支持自动重试", retryable=False,
                )
    except Exception as e:
        logger.error(f"Task 5: 处理可重试 TaskRun 失败: {e}", exc_info=True)
