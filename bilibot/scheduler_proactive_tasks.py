"""Proactive task dispatch extracted from scheduler.py."""

from __future__ import annotations

import logging

logger = logging.getLogger("bilibot.scheduler_proactive_tasks")


async def check_proactive_tasks(self, current_time: str):
    """检查并执行主动任务

    PRD-V5 §7 / TASK-501：通过 TaskRunStore 管理生命周期。
    - 时间匹配时通过 claim() 原子获取任务（避免重启重复触发）
    - claim 成功后 spawn 后台协程，协程内 start/succeed/fail
    - 创建协程 ≠ 成功；不写 triggered 直到真正 succeed
    """
    features = self.config_loader.get_raw_config().get("features", {})

    # MotiveQueue soft gate: use the single snapshot prepared for this tick.
    # Scheduled TaskRuns remain claimable later when energy recovers.
    from bilibot.scheduler_motive import get_tick_motive, persist_motive_runtime

    motive = get_tick_motive(self)
    motive_top_action = motive.action if motive is not None else ""
    motive_top_score = motive.score if motive is not None else 0.0
    motive_rest = bool(motive is not None and motive.rest_gated)

    # PRD V4 COM-001：proactive_video 和 proactive_comment 解耦
    # proactive_video 控制视频获取/分析/评价/记忆；proactive_comment 只控制是否发布主动评论
    # 两个开关不再以 AND 方式决定整个视频任务是否运行
    dynamic_due_now = any(
        f"{hour:02d}:{minute:02d}" <= current_time
        and f"{hour:02d}:{minute:02d}" not in self._dynamic_triggered
        for hour, minute in self._dynamic_times
    )
    video_due_now = any(
        f"{hour:02d}:{minute:02d}" <= current_time
        and f"{hour:02d}:{minute:02d}" not in self._proactive_triggered
        for hour, minute in self._proactive_times
    )
    prefer_dynamic_now = (
        motive_top_action == "post_dynamic"
        and motive_top_score >= 6.0
        and dynamic_due_now
    )
    prefer_video_now = (
        motive_top_action == "browse_video"
        and motive_top_score >= 6.0
        and video_due_now
    )
    if motive is not None:
        try:
            actionable = (
                motive_top_action == "rest"
                or (motive_top_action == "post_dynamic" and dynamic_due_now)
                or (motive_top_action == "browse_video" and video_due_now)
                or motive_top_action in {"reply", "explore", "creative"}
            )
            gate = (
                "ready"
                if actionable
                else "waiting_for_dynamic_slot"
                if motive_top_action == "post_dynamic"
                else "waiting_for_video_slot"
                if motive_top_action == "browse_video"
                else "unsupported_action"
            )
            persist_motive_runtime(
                self,
                snapshot=motive,
                actionable=bool(actionable),
                gate=gate,
            )
        except Exception:
            logger.debug("motive runtime persistence skipped", exc_info=True)

    if (
        features.get("proactive_video", True)
        and not motive_rest
        and not prefer_dynamic_now
    ):
        for trigger_time in self._proactive_times:
            time_str = f"{trigger_time[0]:02d}:{trigger_time[1]:02d}"
            # Task 23 修复：范围匹配（slot ≤ 当前时间且当日未触发）
            # 主循环每 60s 一轮，若某轮耗时 >60s 跳过某一分钟，原精确匹配（== current_time）
            # 会导致该 slot 永远不再匹配。改为范围匹配 + _proactive_triggered 幂等保护，
            # 既不漏槽也不重复触发（_proactive_times 按 hour 去重排序，break 保证每轮至多触发一个）。
            if time_str <= current_time and time_str not in self._proactive_triggered:
                # PRD-V5 §7：原子 claim（事务性条件更新，避免重复触发）
                task_id = self._claim_task_for_slot("proactive_video", time_str)
                if task_id:
                    # PRD V3 §4.1/§4.2：用 _spawn_memory_task 避免阻塞主循环 + 异常回调
                    self._spawn_task_run_coroutine(
                        task_id,
                        self._do_proactive_video(task_id=task_id),
                        tag="_do_proactive_video",
                    )
                    self._proactive_triggered.add(time_str)
                    self._save_schedule_state()
                else:
                    # Task 25 修复：区分"claim 失败（已被处理）"与"任务不存在（创建缺失）"
                    # - task 存在但 claim 失败 → 已被其他 worker claim/完成，标记 triggered 避免反复尝试
                    # - task 不存在 → TaskRun 持久化缺失，告警但不标记 triggered，
                    #   等待下次 _generate_daily_schedule 重建（避免吞掉本该执行的槽位）
                    if self._task_exists_for_slot("proactive_video", time_str):
                        self._proactive_triggered.add(time_str)
                        self._save_schedule_state()
                    else:
                        logger.error(
                            f"proactive_video slot={time_str} 的 TaskRun 不存在，"
                            f"调度持久化可能缺失，等待下次调度重建（不标记 triggered）"
                        )
                break

    # 发布动态（H1：范围匹配，对齐 proactive_video，避免主循环跳分钟漏槽）
    if (
        features.get("dynamic_post", True)
        and not motive_rest
        and not prefer_video_now
    ):
        for trigger_time in self._dynamic_times:
            time_str = f"{trigger_time[0]:02d}:{trigger_time[1]:02d}"
            if time_str <= current_time and time_str not in self._dynamic_triggered:
                # PRD-V5 §7：原子 claim
                task_id = self._claim_task_for_slot("dynamic", time_str)
                if task_id:
                    # PRD V3 §4.1：发布动态改为异步，不阻塞主循环
                    # 配图链路（LLM生成prompt + 图片生成120s + 上传60s）可能卡3分钟
                    self._spawn_memory_task(
                        self._do_post_dynamic(task_id=task_id),
                        tag="_do_post_dynamic",
                    )
                    self._dynamic_triggered.add(time_str)
                    self._save_dynamic_schedule_state()
                else:
                    # H2：区分 claim 失败（已处理）与 TaskRun 缺失（不 mark，等重建）
                    if self._task_exists_for_slot("dynamic", time_str):
                        self._dynamic_triggered.add(time_str)
                        self._save_dynamic_schedule_state()
                    else:
                        logger.error(
                            f"dynamic slot={time_str} 的 TaskRun 不存在，"
                            f"调度持久化可能缺失，等待下次调度重建（不标记 triggered）"
                        )
                break
