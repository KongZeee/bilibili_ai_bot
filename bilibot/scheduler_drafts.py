"""_get_draft_store, create_draft_publish_task, get_draft_store, spawn_publish_task extracted from scheduler.py."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional


logger = logging.getLogger("bilibot.scheduler_drafts")


def get_draft_store_lazy(self):
    """懒加载 DynamicDraftStore（与 TaskRunStore 同目录）"""
    from bilibot.services.dynamic_draft_store import DynamicDraftStore
    db_path = str(Path(self._get_data_dir()) / "dynamic_drafts.db")
    store = getattr(self, "_dynamic_draft_store", None)
    if store is None or store.db_path != db_path:
        store = DynamicDraftStore(db_path, account_id=self.account_id or "")
        self._dynamic_draft_store = store
    return store


def create_draft_publish_task(self, draft_id: str) -> Optional[str]:
    """PRD-V5 §4.1 DYN-501：为已审核通过的草稿创建发布 TaskRun

    供 API approve / retry 端点调用。返回 task_id。
    scene 使用 dynamic_post（与手动触发/用量 scene 对齐）；恢复与重试同时兼容 legacy dynamic。
    """
    from bilibot.services.task_store import TRIGGER_MANUAL
    try:
        # 草稿必须存在且归属当前账号（禁止跨账号派发 / 空 account_id 漏检）
        try:
            draft = self._get_draft_store().get(draft_id)
            if draft is None:
                logger.error(
                    "创建草稿发布 TaskRun 拒绝：草稿不存在 draft=%s", draft_id,
                )
                return None
            d_acc = str(getattr(draft, "account_id", "") or "")
            self_acc = str(self.account_id or "")
            if self_acc and d_acc != self_acc:
                logger.error(
                    "创建草稿发布 TaskRun 拒绝：草稿账号不匹配 draft=%s draft_acc=%s self=%s",
                    draft_id, d_acc, self_acc,
                )
                return None
        except Exception as e:
            logger.warning("创建草稿发布 TaskRun 时校验草稿归属失败: %s", e)
            return None
        idem_key = (
            f"{self.account_id or '_default'}:dynamic_publish:"
            f"{draft_id}:{int(time.time() * 1000)}"
        )
        task = self.task_store.create(
            account_id=self.account_id or "_default",
            scene="dynamic_post",
            idempotency_key=idem_key,
            trigger_type=TRIGGER_MANUAL,
            scheduled_at=time.time(),
            input_data={"draft_id": draft_id, "kind": "publish_draft"},
        )
        return task.task_id if task else None
    except Exception as e:
        logger.error(f"创建草稿发布 TaskRun 失败 draft={draft_id}: {e}")
        return None

def get_draft_store(self):
    """公开接口：获取动态草稿存储（懒加载，与 TaskRunStore 同目录）

    供 API 层调用，避免直接访问私有 _get_draft_store。
    """
    return self._get_draft_store()

def spawn_publish_task(self, task_id: str, draft_id: str, tag: str = ""):
    """公开接口：异步触发已审核通过草稿的发布任务

    供 API approve / retry 端点调用，避免直接访问私有
    _spawn_memory_task / _do_publish_approved_draft。

    Args:
        task_id: 已创建的 TaskRun id
        draft_id: 关联的草稿 id
        tag: 后台任务标签（用于日志）
    """
    # DYN-501 P0：_do_publish_approved_draft 要求 task 已 claim（start 仅接受 claimed）。
    # create_draft_publish_task 只写入 scheduled；API 路径必须在此 claim，
    # 否则 start 恒失败，审核通过后永远不发布。
    try:
        task = self.task_store.get(task_id) if self.task_store else None
        status = getattr(task, "status", None) if task is not None else None
        if status == "scheduled":
            if not self.task_store.claim(task_id):
                logger.warning(
                    "spawn_publish_task claim 失败 task=%s draft=%s status=%s",
                    task_id, draft_id, status,
                )
                return
        elif status is not None and status != "claimed":
            logger.warning(
                "spawn_publish_task 跳过：TaskRun 状态不可派发 task=%s draft=%s status=%s",
                task_id, draft_id, status,
            )
            return
        elif status is None and self.task_store is not None:
            # 记录不存在时尝试 claim（兼容竞态）；失败则放弃
            if not self.task_store.claim(task_id):
                logger.warning(
                    "spawn_publish_task claim 失败（无记录或不可 claim）task=%s draft=%s",
                    task_id, draft_id,
                )
                return
    except Exception as e:
        logger.error(
            "spawn_publish_task claim 异常 task=%s draft=%s: %s",
            task_id, draft_id, e, exc_info=True,
        )
        return

    self._spawn_memory_task(
        self._do_publish_approved_draft(task_id, draft_id),
        tag=tag or f"publish_draft:{draft_id}",
    )
