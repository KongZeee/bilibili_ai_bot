"""_dispatch_task_run, _resolve_oid_from_bvid, _claim_task_for_slot, _task_exists_for_slot extracted from scheduler.py."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional


logger = logging.getLogger("bilibot.scheduler_task_dispatch")


def dispatch_task_run(
    self,
    task_id: str,
    *,
    tag_prefix: str = "dispatch",
    claim_if_scheduled: bool = True,
) -> bool:
    """按 scene 分发 TaskRun 到对应执行协程。

    - claim_if_scheduled：status=scheduled 时先 claim（API 重试/自动重试路径）
    - dynamic / dynamic_post：按 input.kind 分流 publish_draft vs 生成动态
    - 未知 scene 返回 False，调用方可标失败
    """
    if not self.task_store or not task_id:
        return False
    task = self.task_store.get(task_id)
    if task is None:
        logger.warning("dispatch TaskRun 不存在: %s", task_id)
        return False
    status = getattr(task, "status", None)
    if claim_if_scheduled and status == "scheduled":
        if not self.task_store.claim(task_id):
            logger.warning(
                "dispatch claim 失败 task=%s status=%s", task_id, status,
            )
            return False
        task = self.task_store.get(task_id) or task
    elif status not in (None, "claimed", "scheduled"):
        # claimed 可派发；running 等不可重复派发
        if status != "claimed":
            logger.warning(
                "dispatch 跳过：状态不可派发 task=%s status=%s",
                task_id, status,
            )
            return False

    scene = str(getattr(task, "scene", "") or "")
    input_data: dict = {}
    try:
        raw = getattr(task, "input_json", None) or ""
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                input_data = parsed
    except Exception:
        input_data = {}

    if scene == "proactive_video":
        self._spawn_task_run_coroutine(
            task_id,
            self._do_proactive_video(task_id=task_id),
            tag=f"{tag_prefix}_proactive_video:{task_id}",
        )
        return True
    if scene in ("dynamic", "dynamic_post"):
        if (
            input_data.get("kind") == "publish_draft"
            and input_data.get("draft_id")
        ):
            draft_id = str(input_data.get("draft_id"))
            self._spawn_task_run_coroutine(
                task_id,
                self._do_publish_approved_draft(task_id, draft_id),
                tag=f"{tag_prefix}_publish_draft:{task_id}",
            )
        else:
            self._spawn_task_run_coroutine(
                task_id,
                self._do_post_dynamic(task_id=task_id),
                tag=f"{tag_prefix}_dynamic:{task_id}",
            )
        return True
    if scene == "bangumi":
        self._spawn_task_run_coroutine(
            task_id,
            self._do_bangumi_task(task_id),
            tag=f"{tag_prefix}_bangumi:{task_id}",
        )
        return True
    logger.warning(
        "dispatch 不支持场景 task=%s scene=%s", task_id, scene,
    )
    return False

async def resolve_oid_from_bvid(self, bvid: str) -> Optional[int]:
    """通过 bvid 反查视频 oid（用于主动评论重试）"""
    if not self.bili or not bvid:
        return None
    try:
        # VID-602：get_video_info 期望 oid:int，传 bvid 字符串无效
        # 改用 get_video_oid_by_bvid（params={"bvid": bvid}）
        aid = await self.bili.get_video_oid_by_bvid(bvid)
        if aid:
            return int(aid)
    except Exception as e:
        logger.warning(f"反查 oid 失败 bvid={bvid}: {e}")
    return None

def claim_task_for_slot(self, scene: str, slot: str) -> Optional[str]:
    """PRD-V5 §7：根据场景 + 时间槽 claim 对应 TaskRun

    通过 idempotency_key 反查 task_id，再原子 claim。
    失败返回 None。
    """
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        idem_key = f"{self.account_id or '_default'}:{scene}:{today_str}:{slot}"
        task = self.task_store.get_by_idempotency_key(idem_key)
        if task is None:
            return None
        if self.task_store.claim(task.task_id):
            return task.task_id
        return None
    except Exception as e:
        logger.warning(f"claim TaskRun 失败 scene={scene} slot={slot}: {e}")
        return None

def task_exists_for_slot(self, scene: str, slot: str) -> bool:
    """检查 scene+slot 对应的 TaskRun 是否存在（Task 25：区分 claim 失败原因）

    用于 _claim_task_for_slot 返回 None 时区分：
    - task 不存在（持久化缺失）→ 不应标记 triggered，等待调度重建
    - task 存在但 claim 失败（已被其他 worker 处理）→ 可标记 triggered
    """
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        idem_key = f"{self.account_id or '_default'}:{scene}:{today_str}:{slot}"
        return self.task_store.get_by_idempotency_key(idem_key) is not None
    except Exception as e:
        logger.warning(f"查询 TaskRun 存在性失败 scene={scene} slot={slot}: {e}")
        return False
