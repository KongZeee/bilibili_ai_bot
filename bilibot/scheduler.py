"""
调度器 - Scheduler（最小可用版本）

主循环负责：
1. 定时检查新评论
2. 主动看视频
3. 发布动态
4. 周总结

PRD V3 §8.4 / §8.5 / §9.3：
- 动态 / 周总结 主流程使用 PromptOrchestrator（不再用 PersonalitySystem.get_system_prompt）
- LLM 失败时不得硬编码万能动态自动发布
- 主动视频 / 动态 / 周总结统一写入账号级 V6 memory brain
- 接收应用级 audit_store / context_builder 单例
"""
import asyncio
import logging
import json
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from bilibot.config_loader import ConfigLoader
from bilibot.personality import PersonalitySystem
from bilibot.reply import ReplyGenerator
from bilibot.humanized_behavior import (
    HumanBehaviorSimulator,
    VideoBrowser,
    HumanizedCommentGenerator,
    DynamicPoster,
)
from bilibot.models.proactive_video_context import ProactiveVideoContext

logger = logging.getLogger("bilibot.scheduler")


class Scheduler:
    """主调度器（最小可用版本）"""

    def __init__(self, config_loader: ConfigLoader, user_state=None, llm=None,
                 bili=None, data_store=None, persona_store=None, orchestrator=None,
                 audit_store=None, context_builder=None, comment_context_service=None,
                 safety_checker=None, account_id: str = "", video_understanding_service=None,
                 image_provider=None, knowledge_memory=None,
                 proactive_comment_store=None, memory_brain=None, companion=None):
        self.config_loader = config_loader
        self.user_state = user_state
        self.llm = llm
        self.bili = bili
        self.ds = data_store
        self.persona_store = persona_store
        self.orchestrator = orchestrator
        # 应用级单例（PRD V3 §10.2）
        self.audit_store = audit_store
        self.context_builder = context_builder
        # PRD V4 §4.3：评论上下文构建服务
        self.comment_context_service = comment_context_service
        # PRD §5.9：安全检查器（全局暂停 / 内容检查 / 频率限制 / 黑名单）
        # 可由 panel.py 在运行时注入（panel 创建后回填），构造期可为 None
        self.safety_checker = safety_checker
        # 陪伴生活层（账号级，可选）
        self.companion = companion
        # PRD V2：账号 ID（用于多账号人格解析 / 日志隔离）
        self.account_id = account_id
        # AccountInstance explicitly supplies memory_brain in V6.  A direct
        # Scheduler construction without it is retained only as a legacy/test
        # compatibility surface and must not mistake an arbitrary MagicMock
        # knowledge_memory for the V6 service.
        legacy_brain = None
        if knowledge_memory is not None and callable(
            getattr(type(knowledge_memory), "archive_observation_async", None)
        ):
            legacy_brain = knowledge_memory
        resolved_memory_brain = memory_brain or legacy_brain
        # 视频理解服务（视听双轨分析，可选）
        self.video_understanding = video_understanding_service
        # 文生图 Provider（动态配图，可选）
        self.image_provider = image_provider
        # PRD 3.15：联网搜索服务（可选）
        self.web_search = None
        try:
            from bilibot.services.web_search import WebSearchService
            ws_config = config_loader.get_raw_config()
            # 仅认 web_search.enabled（features.web_search 已废弃，避免双开关误开）
            ws_enabled = bool(
                (ws_config.get("web_search") or {}).get("enabled", False)
            )
            if ws_enabled:
                self.web_search = WebSearchService(
                    ws_config, llm_provider=self.llm, data_store=self.ds,
                    audit_store=self.audit_store, account_id=self.account_id,
                )
                if self.web_search.is_available():
                    logger.info("联网搜索服务已启用")
        except Exception as e:
            logger.warning(f"联网搜索服务初始化失败: {e}")

        # 番剧追番服务（可选，需 features.bangumi=true；无 brain 时 fail-closed 不建服务）
        self.bangumi_service = None
        self._last_bangumi_check_date = None
        self._bangumi_check_task: Optional[asyncio.Task] = None
        self._bangumi_failure_count = 0
        self._bangumi_retry_after = 0.0
        try:
            raw = config_loader.get_raw_config()
            if raw.get("features", {}).get("bangumi", False):
                if resolved_memory_brain is None:
                    logger.error(
                        "features.bangumi=true 但 memory_brain 未注入，跳过追番服务（fail-closed）"
                    )
                else:
                    from bilibot.bangumi import BangumiService
                    # Prefer account data dir when provided via brain; fallback config data_dir
                    data_dir = str(
                        getattr(resolved_memory_brain, "data_dir", "")
                        or raw.get("data_dir", "./data")
                    )
                    self.bangumi_service = BangumiService(
                        bili_api=bili, llm_manager=llm,
                        video_service=self.video_understanding,
                        config_loader=config_loader, data_dir=data_dir,
                        memory_brain=resolved_memory_brain,
                        account_id=self.account_id,
                        companion=getattr(self, "companion", None),
                        safety_checker=self.safety_checker,
                    )
                    logger.info("番剧追番服务已启用")
        except Exception as e:
            logger.warning(f"番剧追番服务初始化失败: {e}")
            self.bangumi_service = None

        self.running = False

        # PRD V3 §3.2 / §4.2：后台任务引用集合，防止 GC + 记录异常
        self._running_tasks: set = set()

        # 初始化各子系统
        self.personality = PersonalitySystem(config_loader)

        # MEM-501：knowledge_memory 由 AccountInstance 注入（单一实例，不再自行创建）
        self.knowledge_memory = knowledge_memory
        self.memory_brain = resolved_memory_brain
        self._memory_brain_required = resolved_memory_brain is not None
        # 人格化行为系统
        self.behavior_sim = None
        self.video_browser = None
        self.comment_generator = None
        self.dynamic_poster = None
        if self.llm and self.personality and self.knowledge_memory:
            try:
                self.behavior_sim = HumanBehaviorSimulator(config_loader)
                self.video_browser = VideoBrowser(self.behavior_sim, config_loader)
                self.comment_generator = HumanizedCommentGenerator(
                    self.llm, self.personality, self.knowledge_memory, self.behavior_sim,
                    persona_store=self.persona_store,
                    account_id=self.account_id,
                )
                self.dynamic_poster = DynamicPoster(
                    self.behavior_sim,
                    self.llm,
                    config_loader,
                    account_id=self.account_id or "",
                )
            except Exception as e:
                logger.warning(f"人格化行为系统初始化失败: {e}")

        # ReplyGenerator（注入应用级 audit_store / context_builder / orchestrator / persona_store）
        self.reply_gen = None
        if self.user_state and self.personality and self.llm and self.ds:
            try:
                self.reply_gen = ReplyGenerator(
                    self.user_state, self.personality, self.llm, self.ds, config_loader,
                    knowledge_memory=self.knowledge_memory,
                    humanized_behavior=self.comment_generator,
                    audit_store=self.audit_store,
                    orchestrator=self.orchestrator,
                    context_builder=self.context_builder,
                    persona_store=self.persona_store,
                    web_search=self.web_search,
                    account_id=self.account_id,
                )
            except Exception as e:
                logger.warning(f"ReplyGenerator 初始化失败: {e}")

        # 调度状态
        self._proactive_times: List[tuple] = []
        self._proactive_triggered: set = set()
        # 同账号主动看视频串行：上一条未完成时下一条排队等待，避免叠跑 + 抢资源
        self._proactive_video_lock: Optional[asyncio.Lock] = None
        self._proactive_video_inflight: int = 0
        self._dynamic_times: List[tuple] = []
        self._dynamic_triggered: set = set()
        # PRD V6：轮询状态去重日志——只在数值变化时打印 INFO，否则 DEBUG，避免日志膨胀
        self._last_notify_count: Optional[int] = None
        self._last_at_notify_count: Optional[int] = None
        self._last_at_merged_count: Optional[int] = None
        self._last_own_dynamic_count: Optional[int] = None
        self._last_all_replied_fingerprint: Optional[str] = None
        self._last_dm_session_count: Optional[int] = None
        self._last_consolidation_date = None  # 日终记忆清算上次执行日期
        self._schedule_date = None  # 当前调度日期，用于检测跨天重置
        # PRD V4 REP-001：结构化回复状态机，替换 _replied:bool 字典
        from bilibot.services.reply_state import ReplyStateStore
        from bilibot.services.interaction_policy import InteractionPolicyEngine, CommentPolicy
        _reply_db = str(Path(self.ds.data_dir) / "reply_states.db") if self.ds else "./data/reply_states.db"
        self.reply_state_store = ReplyStateStore(_reply_db, account_id=self.account_id or "")
        # PRD-V5 §7 / TASK-501：TaskRun 持久化生命周期（主动视频 / 动态 / 周总结）
        from bilibot.services.task_store import TaskRunStore
        _task_db = str(Path(self.ds.data_dir) / "task_runs.db") if self.ds else "./data/task_runs.db"
        self.task_store = TaskRunStore(_task_db, account_id=self.account_id or "")
        # PRD-V5 §6.3 / PM-501：私信独立幂等状态机（独立退避 / 平台消息 ID 幂等键）
        from bilibot.services.pm_state_store import (
            PrivateMessageStateStore,
            DEFAULT_PM_MAX_ATTEMPTS,
            DEFAULT_PM_BACKOFF_BASE_SECONDS,
        )
        _pm_data_dir = str(Path(self.ds.data_dir)) if self.ds else "./data"
        _pm_cfg = config_loader.get_raw_config().get("private_message", {}) or {}
        _pm_max = int(_pm_cfg.get("max_attempts", DEFAULT_PM_MAX_ATTEMPTS) or DEFAULT_PM_MAX_ATTEMPTS)
        _pm_backoff = float(
            _pm_cfg.get("backoff_base_seconds", DEFAULT_PM_BACKOFF_BASE_SECONDS)
            or DEFAULT_PM_BACKOFF_BASE_SECONDS
        )
        self.pm_state_store = PrivateMessageStateStore(
            _pm_data_dir,
            account_id=self.account_id or "",
            max_attempts=_pm_max,
            backoff_base_seconds=_pm_backoff,
        )
        # PRD V4 VID-006：确定性互动决策引擎（替代 LLM 直接决策）
        _policy_data_dir = str(Path(self.ds.data_dir) if self.ds else Path("./data"))
        self.interaction_policy = InteractionPolicyEngine(
            config=config_loader.get_raw_config(),
            data_dir=_policy_data_dir,
            account_id=self.account_id or "",
        )
        # PRD V4 COM-002：主动评论发布策略（去重 + 预算 + 审计固化 + 暂停闸门）
        self.comment_policy = CommentPolicy(
            config=config_loader.get_raw_config(),
            data_dir=_policy_data_dir,
            account_id=self.account_id or "",
            safety_checker=self.safety_checker,
        )
        # PRD-V5 §10.2 COM-501：主动评论原子幂等状态机
        # 由 AccountInstance 注入（per-account SQLite），构造期为 None 时自动创建
        if proactive_comment_store is not None:
            self.proactive_comment_store = proactive_comment_store
        else:
            from bilibot.services.proactive_comment_store import ProactiveCommentStore
            _pc_db = str(Path(self.ds.data_dir) / "proactive_comment_actions.db") if self.ds else "./data/proactive_comment_actions.db"
            self.proactive_comment_store = ProactiveCommentStore(
                _pc_db, account_id=self.account_id or "",
            )
        # 保留 _replied/_pm_replied 作为兼容别名（空 dict，实际由 state_store 管理）
        self._replied: dict = {}
        self._pm_replied: dict = {}
        # _comment_fail_counts 由 state_store.attempts 替代，保留为空 dict 兼容旧代码
        self._comment_fail_counts: Dict[str, int] = {}

        # Bot 自身信息（启动时从 B站 API 获取）
        self._bot_name: str = ""
        self._bot_uid: str = ""

    def _spawn_memory_task(self, coro, tag: str = ""):
        """PRD V3 §3.2 / §4.2：创建后台任务，保存引用防止 GC，加回调记录异常"""
        task = asyncio.create_task(coro)
        self._running_tasks.add(task)
        task_tag = tag or getattr(coro, "__name__", "memory_task")

        def _on_done(t: asyncio.Task):
            self._running_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(f"后台任务异常 [{task_tag}]: {exc}", exc_info=exc)

        task.add_done_callback(_on_done)
        return task

    def _spawn_task_run_coroutine(self, task_id: str, coro, tag: str = ""):
        """Track a TaskRun-backed coroutine so the running-lease watchdog can
        tell a live long-running task apart from an orphaned database row."""
        if not hasattr(self, "_inflight_task_runs"):
            self._inflight_task_runs = {}
        task = self._spawn_memory_task(coro, tag=tag)
        if task is None:
            # Test/fake schedulers may replace _spawn_memory_task with a stub.
            return None
        self._inflight_task_runs[str(task_id)] = task

        def _on_done(t: asyncio.Task):
            self._inflight_task_runs.pop(str(task_id), None)

        task.add_done_callback(_on_done)
        return task

    async def _recover_orphaned_task_runs(self) -> int:
        """Fail running TaskRuns whose lease expired and whose coroutine is gone.

        Healthy long-running tasks (downloads, LLM retries) keep their live
        coroutine in ``_inflight_task_runs`` and are never touched here.
        """
        store = getattr(self, "task_store", None)
        lister = getattr(store, "list_running_expired", None)
        if not callable(lister):
            return 0
        try:
            expired = lister(lease_overdue_seconds=300)
        except Exception as exc:
            logger.warning("list_running_expired failed: %s", type(exc).__name__)
            return 0
        recovered = 0
        inflight = getattr(self, "_inflight_task_runs", {}) or {}
        for run in expired:
            task_id = str(getattr(run, "task_id", "") or "")
            if not task_id:
                continue
            live = inflight.get(task_id)
            if live is not None and not live.done():
                continue
            try:
                store.fail(
                    task_id,
                    "TASK_LEASE_TIMEOUT",
                    "task lease expired without a live coroutine",
                    retryable=True,
                )
                recovered += 1
                logger.warning(
                    "TaskRun watchdog recovered orphaned running task: %s", task_id
                )
            except Exception as exc:
                logger.warning(
                    "TaskRun watchdog fail failed task=%s: %s",
                    task_id,
                    type(exc).__name__,
                )
        return recovered

    def _get_proactive_video_lock(self) -> asyncio.Lock:
        """Lazy loop-bound lock so proactive video runs serially per account."""
        if getattr(self, "_proactive_video_lock", None) is None:
            self._proactive_video_lock = asyncio.Lock()
        # Recovery/migration code and a few production diagnostics may rebuild a
        # Scheduler without running the newest __init__.  Keep the companion
        # counter as lazy as the loop-bound lock so an interrupted video can be
        # resumed after an in-place upgrade instead of crashing before cleanup.
        if not hasattr(self, "_proactive_video_inflight"):
            self._proactive_video_inflight = 0
        return self._proactive_video_lock

    def _pseudonymize_actor_id(self, actor_id: Any, namespace: str = "uid") -> str:
        """PRD V6 7.1：平台原始 UID 不得进入脑库。

        评论/评论线程/视频热评等公开 actor 也统一走账户盐 HMAC 伪名化；
        失败时返回伪名占位符而不是原始 ID。
        """
        raw = str(actor_id or "")
        brain = getattr(self, "memory_brain", None)
        redactor = getattr(brain, "redactor", None)
        method = getattr(redactor, "pseudonymize_identifier", None)
        if callable(method):
            try:
                return str(method(raw, namespace=namespace or "uid"))
            except Exception as exc:
                logger.warning(
                    "actor pseudonymization failed account=%s: %s",
                    getattr(self, "account_id", "") or "-",
                    type(exc).__name__,
                )
        if not raw:
            return "anon"
        import hashlib as _hashlib

        # Fail-closed deterministic placeholder; never leak the raw UID.
        digest = _hashlib.sha256(
            ("bilibot-actor-fallback:v1\0" + raw).encode("utf-8")
        ).hexdigest()
        return f"actor_{digest[:24]}"

    # ── 评论风控暂停/自动恢复 ──

    def _comment_risk_pause_seconds(self) -> float:
        """风控暂停时长：reply.risk_pause_seconds，默认 7200 秒。"""
        try:
            loader = getattr(self, "config_loader", None)
            if loader is None:
                return 7200.0
            raw = (
                loader.get_raw_config()
                .get("reply", {})
                .get("risk_pause_seconds", 7200)
            )
            return max(60.0, float(raw))
        except Exception:
            return 7200.0

    def _comment_risk_pause_remaining(self) -> float:
        """读取持久化的评论风控暂停，返回剩余秒数（0 表示未暂停）。"""
        pause_until = 0.0
        try:
            if getattr(self, "ds", None) is not None:
                state = self.ds.load_json("comment_risk_pause.json", {}) or {}
                if isinstance(state, dict):
                    pause_until = float(state.get("pause_until") or 0.0)
        except Exception:
            pass
        # 内存中的 cooldown 也参与，保证热重载/刚触发时立即生效
        pause_until = max(
            pause_until,
            float(getattr(self, "_comment_reply_cooldown_until", 0.0) or 0.0),
        )
        return max(0.0, pause_until - time.time())

    def _set_comment_risk_pause(self, seconds: float, reason: str) -> float:
        """进入评论风控暂停，持久化并在到期后允许自动恢复。"""
        duration = max(60.0, float(seconds))
        pause_until = time.time() + duration
        self._comment_reply_cooldown_until = pause_until
        try:
            if getattr(self, "ds", None) is not None:
                self.ds.save_json(
                    "comment_risk_pause.json",
                    {
                        "pause_until": pause_until,
                        "reason": str(reason or "bilibili_risk_control"),
                        "duration_seconds": duration,
                        "set_at": time.time(),
                    },
                )
        except Exception as exc:
            logger.warning("评论风控暂停持久化失败: %s", exc)
        logger.warning(
            "评论风控暂停 %.0f 秒 reason=%s，到期后自动恢复回复",
            duration,
            reason,
        )
        return pause_until

    def _clear_comment_risk_pause(self) -> None:
        self._comment_reply_cooldown_until = 0.0
        try:
            if getattr(self, "ds", None) is not None:
                self.ds.save_json("comment_risk_pause.json", {})
        except Exception:
            pass

    async def _verify_and_cleanup_reply_visibility(
        self,
        *,
        oid: int,
        comment_root: int,
        reply_text: str,
        comment_type: int = 1,
    ) -> bool:
        """发布后检查 B站阿瓦隆 state=17（仅自己可见）。

        返回 True 表示回复可见（或无法确认）；False 表示已删除隐藏回复并
        设置一小时的评论发布冷却，避免继续向风控池里灌隐藏评论。
        """
        bili = getattr(self, "bili", None)
        if bili is None or not comment_root:
            return True
        try:
            await asyncio.sleep(2.0)
            resp = await bili.get_comment_replies(
                oid=oid, root=comment_root, comment_type=comment_type, ps=30,
            )
            replies = (resp or {}).get("data", {}).get("replies") or []
            bot_uid = str(getattr(self, "_bot_uid", "") or "")
            target_text = " ".join(str(reply_text or "").split())
            for row in replies:
                if not isinstance(row, dict) or str(row.get("mid") or "") != bot_uid:
                    continue
                msg = " ".join(
                    str((row.get("content") or {}).get("message") or "").split()
                )
                if target_text and msg != target_text:
                    continue
                if int(row.get("state") or 0) == 17:
                    rpid = int(row.get("rpid") or 0)
                    logger.warning(
                        "B站评论被阿瓦隆隐藏（state=17）: oid=%s rpid=%s，"
                        "删除并进入评论发布冷却 1 小时",
                        oid,
                        rpid,
                    )
                    deleter = getattr(bili, "delete_reply", None)
                    if callable(deleter) and rpid:
                        try:
                            await deleter(
                                oid=oid,
                                rpid=rpid,
                                comment_type=comment_type,
                            )
                        except Exception as del_exc:
                            logger.warning(
                                "删除隐藏评论失败 oid=%s rpid=%s: %s",
                                oid,
                                rpid,
                                type(del_exc).__name__,
                            )
                    Scheduler._set_comment_risk_pause(
                        self,
                        Scheduler._comment_risk_pause_seconds(self),
                        "bili_avalon_state_17",
                    )
                    self._last_comment_publish_ts = time.time()
                    return False
                return True
        except Exception as exc:
            logger.warning(
                "评论可见性检查失败（不影响已发布状态）: %s: %s",
                type(exc).__name__,
                exc,
            )
            logger.debug("visibility check traceback", exc_info=True)
        return True

    def _maybe_schedule_bangumi_check(self, now: datetime) -> bool:
        """实现已抽离到 scheduler_bangumi"""
        from bilibot.scheduler_bangumi import maybe_schedule_bangumi_check

        return maybe_schedule_bangumi_check(self=self, now=now)

    async def _run_bangumi_daily_check(self, service, check_date) -> None:
        """实现已抽离到 scheduler_bangumi"""
        from bilibot.scheduler_bangumi import run_bangumi_daily_check

        return await run_bangumi_daily_check(self=self, service=service, check_date=check_date)

    async def _do_bangumi_task(self, task_id: str) -> None:
        """实现已抽离到 scheduler_bangumi"""
        from bilibot.scheduler_bangumi import do_bangumi_task

        return await do_bangumi_task(self=self, task_id=task_id)


    async def _build_video_detail_digest(self, *, title: str, owner: str, behavior_log: str, extra_context: str='', max_attempts: int=2, require_llm: bool=True) -> str:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import build_video_detail_digest

        return await build_video_detail_digest(self=self, title=title, owner=owner, behavior_log=behavior_log, extra_context=extra_context, max_attempts=max_attempts, require_llm=require_llm)


    async def _archive_required(self, envelope, *, treat_idempotent_as_ready: bool=False):
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import archive_required

        return await archive_required(self=self, envelope=envelope, treat_idempotent_as_ready=treat_idempotent_as_ready)


    _FULL_VIDEO_SOURCE_TYPES = frozenset(
        {
            "video_detail",
            "behavior_log",
            "asr",
            "subtitle",
            "visual_description",
        }
    )

    def _event_is_full_video_watch(self, event) -> bool:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import event_is_full_video_watch

        return event_is_full_video_watch(self=self, event=event)


    async def _has_full_video_observation(self, bvid: str) -> bool:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import has_full_video_observation

        return await has_full_video_observation(self=self, bvid=bvid)


    async def _has_completed_proactive_video(self, bvid: str) -> bool:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import has_completed_proactive_video

        return await has_completed_proactive_video(self=self, bvid=bvid)


    async def _load_existing_video_detail(self, bvid: str) -> str:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import load_existing_video_detail

        return await load_existing_video_detail(self=self, bvid=bvid)


    def _compose_video_content_for_prompt(self, ctx: 'ProactiveVideoContext', *, video_detail: str='') -> str:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import compose_video_content_for_prompt

        return compose_video_content_for_prompt(self=self, ctx=ctx, video_detail=video_detail)


    def _pause_for_memory_failure(self) -> None:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import pause_for_memory_failure

        return pause_for_memory_failure(self=self)


    def _collect_related_titles_for_dynamic(self, brain, *, limit: int=5) -> List[str]:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import collect_related_titles_for_dynamic

        return collect_related_titles_for_dynamic(self=self, brain=brain, limit=limit)


    def _notify_companion_dynamic_posted(self, *, content: str='', topic: str='', draft_id: str='', dynamic_id: str='', task_id: str='') -> None:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import notify_companion_dynamic_posted

        return notify_companion_dynamic_posted(self=self, content=content, topic=topic, draft_id=draft_id, dynamic_id=dynamic_id, task_id=task_id)


    def _notify_companion_private_message_replied(self, *, actor_label: str='') -> None:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import notify_companion_private_message_replied

        return notify_companion_private_message_replied(self=self, actor_label=actor_label)


    def _notify_companion_comment_replied(self, *, title: str='', preview: str='', proactive: bool=False) -> None:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import notify_companion_comment_replied

        return notify_companion_comment_replied(self=self, title=title, preview=preview, proactive=proactive)


    async def _recall_for_proactive_video(self, *, title: str='', owner: str='', tags: Optional[List[str]]=None, bvid: str='', oid: str='', desc: str='') -> Dict[str, Any]:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import recall_for_proactive_video

        return await recall_for_proactive_video(self=self, title=title, owner=owner, tags=tags, bvid=bvid, oid=oid, desc=desc)


    async def _begin_activity_context(self, *, action_key: str, action_type: str, current_activity: str, query: str='', scene: str='system', title: str='', bvid: str='', oid: str='', metadata: Optional[Dict[str, Any]]=None):
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import begin_activity_context

        return await begin_activity_context(self=self, action_key=action_key, action_type=action_type, current_activity=current_activity, query=query, scene=scene, title=title, bvid=bvid, oid=oid, metadata=metadata)


    async def _archive_bot_action(self, *, action_key: str, action_type: str, text: str, published: bool, title: str='', scene: str='system', metadata: Optional[Dict[str, Any]]=None, importance: float=0.6, status: str=''):
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import archive_bot_action

        return await archive_bot_action(self=self, action_key=action_key, action_type=action_type, text=text, published=published, title=title, scene=scene, metadata=metadata, importance=importance, status=status)


    @staticmethod
    def _clean_platform_text(value: Any, limit: int=160) -> str:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import clean_platform_text

        return clean_platform_text(value=value, limit=limit)


    async def _archive_proactive_video_failure(self, *, bvid: str='', oid: str='', title: str='', owner: str='', reason: str, task_id: str='', tags: Optional[List[str]]=None, extra: Optional[Dict[str, Any]]=None, partial_evidence: str='') -> str:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import archive_proactive_video_failure

        return await archive_proactive_video_failure(self=self, bvid=bvid, oid=oid, title=title, owner=owner, reason=reason, task_id=task_id, tags=tags, extra=extra, partial_evidence=partial_evidence)


    async def _recent_failed_video_event_ids(self, bvid: str, limit: int=12) -> List[str]:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import recent_failed_video_event_ids

        return await recent_failed_video_event_ids(self=self, bvid=bvid, limit=limit)


    async def _archive_video_web_reference(self, *, bvid: str, oid: str, title: str, query: str, result: Any) -> str:
        """实现已抽离到 scheduler_video_memory"""
        from bilibot.scheduler_video_memory import archive_video_web_reference

        return await archive_video_web_reference(self=self, bvid=bvid, oid=oid, title=title, query=query, result=result)


    def _bot_aliases(self) -> set[str]:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import bot_aliases

        return bot_aliases(self=self)


    def _is_bot_speaker(self, actor_id: str | int='', username: str='', text: str='') -> bool:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import is_bot_speaker

        return is_bot_speaker(self=self, actor_id=actor_id, username=username, text=text)


    def _bot_already_replied_to_source(self, replies: list, *, source_rpid: str, expected_text: str='') -> bool:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import bot_already_replied_to_source

        return bot_already_replied_to_source(self=self, replies=replies, source_rpid=source_rpid, expected_text=expected_text)


    def _comment_memory_title(self, username: str, text: str, reply_id: str | int='') -> str:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import comment_memory_title

        return comment_memory_title(self=self, username=username, text=text, reply_id=reply_id)


    def _dynamic_card_context(self, card: Dict[str, Any]) -> Dict[str, Any]:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import dynamic_card_context

        return dynamic_card_context(self=self, card=card)


    def _redact_private_message_runtime(self, text: str, *, actor_id: str | int, username: str=''):
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import redact_private_message_runtime

        return redact_private_message_runtime(self=self, text=text, actor_id=actor_id, username=username)


    @staticmethod
    def _bounded_recent_turns(turns: List[str], *, max_turns: int=6, max_chars: int=1200) -> List[str]:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import bounded_recent_turns

        return bounded_recent_turns(turns=turns, max_turns=max_turns, max_chars=max_chars)


    async def _archive_comment_thread_context(self, replies: List[Dict[str, Any]], *, oid: str | int, comment_type: int, thread_key: str | int) -> List[str]:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import archive_comment_thread_context

        return await archive_comment_thread_context(self=self, replies=replies, oid=oid, comment_type=comment_type, thread_key=thread_key)


    @staticmethod
    def _private_message_text(message: Dict[str, Any]) -> str:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import private_message_text

        return private_message_text(message=message)


    async def _archive_pm_recent_history(self, messages: List[Dict[str, Any]], *, current_message_id: str, talker_id: int, talker_name: str, my_uid: int) -> List[str]:
        """实现已抽离到 scheduler_archiving"""
        from bilibot.scheduler_archiving import archive_pm_recent_history

        return await archive_pm_recent_history(self=self, messages=messages, current_message_id=current_message_id, talker_id=talker_id, talker_name=talker_name, my_uid=my_uid)


    async def start(self):
        """实现已抽离到 scheduler_start"""
        from bilibot.scheduler_start import scheduler_start

        return await scheduler_start(self=self)


    async def cleanup(self):
        """清理资源"""
        try:
            if self.bili:
                await self.bili.close()
        except Exception:
            pass
        # PRD 3.8：关闭 ImageProvider session
        try:
            if self.image_provider and hasattr(self.image_provider, "close"):
                await self.image_provider.close()
        except Exception:
            pass
        # PRD 3.15：关闭 WebSearchService 客户端
        try:
            if self.web_search and hasattr(self.web_search, "close"):
                await self.web_search.close()
        except Exception:
            pass
        # BUG A-004：取消并等待所有后台记忆任务完成，防止资源泄漏和协程悬挂
        tasks = list(self._running_tasks)
        if tasks:
            logger.info(f"正在取消 {len(tasks)} 个后台任务...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("后台任务已全部取消")
        logger.info("调度器已关闭")

    def stop(self):
        """停止调度器"""
        self.running = False

    def _authenticated_poll_allowed(self) -> bool:
        """Apply BilibiliAPI's -101 backoff to auth-only polling endpoints."""
        if self.bili is None:
            return False
        checker = getattr(self.bili, "auth_poll_allowed", None)
        if not callable(checker):
            return True
        try:
            return bool(checker())
        except Exception:
            return True

    # ══════════════════════════════════════════
    #  评论检查
    # ══════════════════════════════════════════

    async def _check_new_comments(self):
        """实现已抽离到 scheduler_comments"""
        from bilibot.scheduler_comments import check_new_comments

        return await check_new_comments(self)
    async def _collect_own_dynamic_comment_items(self, config: Dict[str, Any], limit: int=10) -> List[Dict[str, Any]]:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import collect_own_dynamic_comment_items

        return await collect_own_dynamic_comment_items(self=self, config=config, limit=limit)


    def _extract_dynamic_comment_target(self, card: Dict[str, Any]) -> Tuple[int, int]:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import extract_dynamic_comment_target

        return extract_dynamic_comment_target(self=self, card=card)


    def _reply_to_notification_items(self, reply: Dict[str, Any], oid: int, comment_type: int, *, root_id: int=0, target_context: Optional[Dict[str, Any]]=None) -> List[Dict[str, Any]]:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import reply_to_notification_items

        return reply_to_notification_items(self=self, reply=reply, oid=oid, comment_type=comment_type, root_id=root_id, target_context=target_context)


    def _save_replied_state(self):
        """PRD V4 REP-001：状态机自动持久化（SQLite），此方法保留为空操作兼容旧调用"""
        pass

    def _load_replied_state(self):
        """PRD V4 REP-001：从旧 replied.json 迁移到状态机（只读旧格式）"""
        try:
            if self.ds:
                data = self.ds.load_json("replied.json", [])
                if isinstance(data, list) and data:
                    self.reply_state_store.migrate_from_replied_json(data)
                    logger.info(f"已从 replied.json 迁移 {len(data)} 条旧回复记录")
                    # 迁移成功后清空文件，避免每次启动重复迁移
                    self.ds.save_json("replied.json", [])
        except Exception as e:
            logger.warning(f"加载回复状态失败: {e}")

    async def _process_retryable_comments(self):
        """实现已抽离到 scheduler_comment_retry"""
        from bilibot.scheduler_comment_retry import process_retryable_comments

        return await process_retryable_comments(self)
    def _get_proactive_comment_max_attempts(self) -> int:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import get_proactive_comment_max_attempts

        return get_proactive_comment_max_attempts(self=self)


    async def _record_proactive_comment_audit(self, *, persona_id: str, comment_text: str, bvid: str, oid: Any, title: str='', owner: str='', input_summary: str='', context_summary: str='', prompt_preview: str='', published: bool=False, status: str='generated', failure_reason: str='', extra_target: Optional[Dict[str, Any]]=None) -> Optional[str]:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import record_proactive_comment_audit

        return await record_proactive_comment_audit(self=self, persona_id=persona_id, comment_text=comment_text, bvid=bvid, oid=oid, title=title, owner=owner, input_summary=input_summary, context_summary=context_summary, prompt_preview=prompt_preview, published=published, status=status, failure_reason=failure_reason, extra_target=extra_target)


    def _finalize_proactive_comment_audit(self, audit_id: Optional[str], *, published: bool=False, failure_reason: str='', status: Optional[str]=None, target: Optional[Dict[str, Any]]=None) -> None:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import finalize_proactive_comment_audit

        return finalize_proactive_comment_audit(self=self, audit_id=audit_id, published=published, failure_reason=failure_reason, status=status, target=target)


    # Task 26：禁用短语默认表（覆盖"看完"类与真实 watch_state 冲突的表达）
    _DEFAULT_FORBIDDEN_PHRASES: List[str] = [
        "我完整看完了", "我看完了", "完整看完", "我看过了",
        "我看过整个视频", "从头看到尾", "全部看完", "整部看完", "一秒不差地看完",
    ]
    # Task 26：默认正则变体（覆盖"完整.*看完"、"看完了?"等中间插字/可选字变体）
    _DEFAULT_FORBIDDEN_PATTERNS: List[str] = [
        r"完整.{0,4}看完",
        r"全部.{0,4}看完",
        r"从头.{0,6}看到尾",
        r"看完了?",
        r"一秒不差.{0,4}看完",
    ]

    def _get_forbidden_phrases(self) -> Tuple[List[str], List['re.Pattern']]:
        """实现已抽离到 scheduler_dynamic_comments"""
        from bilibot.scheduler_dynamic_comments import get_forbidden_phrases

        return get_forbidden_phrases(self=self)


    async def _do_proactive_comment_publish(
        self,
        bvid: str,
        oid: int,
        title: str,
        owner: str,
        desc: str,
        tags_list: list,
        review: str,
        mood: str,
        video_content: str,
        evaluation: dict,
        llm_ok: bool,
        task_id: str = "",
        memory_evidence: str = "",
        companion_context: str = "",
        memory_event_ids: Optional[List[str]] = None,
    ) -> str:
        """实现已抽离到 scheduler_proactive_publish"""
        from bilibot.scheduler_proactive_publish import do_proactive_comment_publish

        return await do_proactive_comment_publish(
            self,
            bvid=bvid,
            oid=oid,
            title=title,
            owner=owner,
            desc=desc,
            tags_list=tags_list,
            review=review,
            mood=mood,
            video_content=video_content,
            evaluation=evaluation,
            llm_ok=llm_ok,
            task_id=task_id,
            memory_evidence=memory_evidence,
            companion_context=companion_context,
            memory_event_ids=memory_event_ids,
        )
    async def _process_retryable_proactive_comments(self):
        """实现已抽离到 scheduler_proactive_retry"""
        from bilibot.scheduler_proactive_retry import process_retryable_proactive_comments

        await process_retryable_proactive_comments(self)
    def _dispatch_task_run(self, task_id: str, *, tag_prefix: str='dispatch', claim_if_scheduled: bool=True) -> bool:
        """实现已抽离到 scheduler_task_dispatch"""
        from bilibot.scheduler_task_dispatch import dispatch_task_run

        return dispatch_task_run(self=self, task_id=task_id, tag_prefix=tag_prefix, claim_if_scheduled=claim_if_scheduled)


    async def _process_retryable_tasks(self):
        """实现已抽离到 scheduler_task_retry"""
        from bilibot.scheduler_task_retry import process_retryable_tasks

        return await process_retryable_tasks(self)
    async def _resolve_oid_from_bvid(self, bvid: str) -> Optional[int]:
        """实现已抽离到 scheduler_task_dispatch"""
        from bilibot.scheduler_task_dispatch import resolve_oid_from_bvid

        return await resolve_oid_from_bvid(self=self, bvid=bvid)


    def _save_fail_counts(self):
        """PRD V3 §4.8：持久化评论失败计数器（重启后保留）"""
        try:
            if self.ds:
                self.ds.save_json("comment_fail_counts.json", self._comment_fail_counts)
        except Exception as e:
            logger.warning(f"保存失败计数器失败: {e}")

    def _load_fail_counts(self):
        """PRD V3 §4.8：加载评论失败计数器"""
        try:
            if self.ds:
                data = self.ds.load_json("comment_fail_counts.json", {})
                if isinstance(data, dict):
                    self._comment_fail_counts = data
        except Exception as e:
            logger.warning(f"加载失败计数器失败: {e}")

    # ══════════════════════════════════════════
    #  私信检查
    # ══════════════════════════════════════════

    async def _check_new_messages(self):
        """实现已抽离到 scheduler_pm"""
        from bilibot.scheduler_pm import check_new_messages

        return await check_new_messages(self)
    async def _process_retryable_pms(self):
        """PRD-V5 §6.3 / PM-501：重试 retry_wait / deferred 状态的私信（实现已抽离）"""
        from bilibot.scheduler_pm_retry import process_retryable_pms

        await process_retryable_pms(self)
    async def _check_proactive_tasks(self, current_time: str):
        """实现已抽离到 scheduler_proactive_tasks"""
        from bilibot.scheduler_proactive_tasks import check_proactive_tasks

        return await check_proactive_tasks(self, current_time=current_time)
    def _claim_task_for_slot(self, scene: str, slot: str) -> Optional[str]:
        """实现已抽离到 scheduler_task_dispatch"""
        from bilibot.scheduler_task_dispatch import claim_task_for_slot

        return claim_task_for_slot(self=self, scene=scene, slot=slot)


    def _task_exists_for_slot(self, scene: str, slot: str) -> bool:
        """实现已抽离到 scheduler_task_dispatch"""
        from bilibot.scheduler_task_dispatch import task_exists_for_slot

        return task_exists_for_slot(self=self, scene=scene, slot=slot)


    def _video_download_precheck(self, video_info: Dict[str, Any]) -> str:
        """PRD-V5 §8.2: refuse oversized / non-video targets BEFORE download."""
        from bilibot.video_understanding.scheduler_bridge import (
            video_download_precheck,
        )

        return video_download_precheck(self.video_understanding, video_info)

    def _video_download_bounds(self) -> Tuple[int, int]:
        """(max_bytes, timeout_seconds) for production downloads."""
        from bilibot.video_understanding.scheduler_bridge import (
            video_download_bounds,
        )

        return video_download_bounds(self.video_understanding)

    async def _watch_video_for_reply(self, bvid: str, oid) -> bool:
        """实现已抽离到 scheduler_watch_video"""
        from bilibot.scheduler_watch_video import watch_video_for_reply

        return await watch_video_for_reply(self=self, bvid=bvid, oid=oid)

    async def _do_proactive_video(self, task_id: Optional[str] = None):
        """主动看视频（参考 chenluQwQ 方案）

        流程：
        1. 获取热门视频 → 随机选一个
        2. 获取视频详情、标签、热门评论
        3. LLM 评价视频（评分、心情、评论、互动意愿）
        4. 根据评分决定互动（点赞/投币/收藏/评论）
        5. 生成评论并发表
        6. 保存记忆

        PRD-V5 §7 / TASK-501：
        - task_id 不为空时通过 TaskRunStore 跟踪生命周期
        - claim → start → succeed/fail（创建协程 ≠ 成功）
        - TaskRun 只表示「观看+评价闭环」；点赞/投币/收藏/评论等互动结果
          由 InteractionPolicy / record_result_async 独立记账，不会自动重发

        同账号串行：若上一条主动看视频仍在执行，本条在锁上排队等待（不丢 slot、不叠跑）。
        """
        if not self.bili:
            if task_id:
                self._fail_task(task_id, "NO_BILI_API", "bili API 未初始化")
            return

        # PRD §5.9：无 checker / 全局暂停 / 账号风险暂停 → fail-closed 跳过
        if self.safety_checker is None:
            logger.error("safety_checker 未初始化，拒绝主动看视频（fail-closed）")
            if task_id:
                self._fail_task(
                    task_id, "NO_SAFETY_CHECKER", "safety_checker 未初始化", retryable=False,
                )
            return
        if (
            self.safety_checker.is_paused()
            or self.safety_checker.is_account_paused(self.account_id)
        ):
            logger.info("跳过主动看视频（暂停状态）")
            if task_id:
                self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
            return

        logger.info("开始主动看视频...")

        # PRD-V5 §7：claim → start
        # scheduled（manual / retry / 其它）须先 claim；已 claimed 的由调度/dispatch 完成
        if task_id:
            from bilibot.services.task_store import STATUS_SCHEDULED, STATUS_CLAIMED
            _task = self.task_store.get(task_id)
            if _task is None:
                logger.warning(f"TaskRun {task_id} 不存在")
                return
            if _task.status == STATUS_SCHEDULED:
                if not self.task_store.claim(task_id):
                    logger.warning(f"TaskRun {task_id} claim 失败（可能已被处理）")
                    return
            elif _task.status != STATUS_CLAIMED:
                logger.warning(
                    f"TaskRun {task_id} 状态不可 start: {_task.status}"
                )
                return
            if not self.task_store.start(task_id):
                logger.warning(f"TaskRun {task_id} start 失败（可能已被处理）")
                return

        lock = self._get_proactive_video_lock()
        if lock.locked() or self._proactive_video_inflight > 0:
            logger.info(
                "主动看视频排队等待：同账号已有进行中的任务 "
                f"(inflight={self._proactive_video_inflight}, task_id={task_id or '-'})"
            )
        async with lock:
            self._proactive_video_inflight += 1
            try:
                await self._do_proactive_video_locked(task_id=task_id)
            finally:
                self._proactive_video_inflight = max(0, self._proactive_video_inflight - 1)

    async def _do_proactive_video_locked(self, task_id: Optional[str] = None):
        """实现已抽离到 scheduler_proactive_video"""
        from bilibot.scheduler_proactive_video import do_proactive_video_locked

        return await do_proactive_video_locked(self, task_id=task_id)

    async def _generate_image_prompt(
        self,
        content: str,
        persona_id: str = "",
    ) -> Optional[str]:
        """实现已抽离到 scheduler_image_prompt"""
        from bilibot.scheduler_image_prompt import generate_image_prompt

        return await generate_image_prompt(
            self=self,
            content=content,
            persona_id=persona_id,
        )


    async def _do_post_dynamic(self, task_id: Optional[str] = None):
        """实现已抽离到 scheduler_dynamic_post"""
        from bilibot.scheduler_dynamic_post import do_post_dynamic

        return await do_post_dynamic(self, task_id=task_id)
    def _get_draft_store(self):
        """实现已抽离到 scheduler_drafts"""
        from bilibot.scheduler_drafts import get_draft_store_lazy

        return get_draft_store_lazy(self=self)


    async def _handle_dynamic_review_mode(
        self,
        task_id: Optional[str],
        action_key: str,
        content: str,
        persona_id: str,
        audit_id: Optional[str],
        dp_cfg: Dict[str, Any],
    ):
        """实现已抽离到 scheduler_dynamic_review"""
        from bilibot.scheduler_dynamic_review import handle_dynamic_review_mode

        await handle_dynamic_review_mode(self, task_id=task_id, action_key=action_key, content=content, persona_id=persona_id, audit_id=audit_id, dp_cfg=dp_cfg)
    async def _do_publish_approved_draft(self, task_id: str, draft_id: str):
        """实现已抽离到 scheduler_draft_publish"""
        from bilibot.scheduler_draft_publish import do_publish_approved_draft

        return await do_publish_approved_draft(self, task_id, draft_id)
    def create_draft_publish_task(self, draft_id: str) -> Optional[str]:
        """实现已抽离到 scheduler_drafts"""
        from bilibot.scheduler_drafts import create_draft_publish_task

        return create_draft_publish_task(self=self, draft_id=draft_id)


    def get_draft_store(self):
        """实现已抽离到 scheduler_drafts"""
        from bilibot.scheduler_drafts import get_draft_store

        return get_draft_store(self=self)


    def spawn_publish_task(self, task_id: str, draft_id: str, tag: str=''):
        """实现已抽离到 scheduler_drafts"""
        from bilibot.scheduler_drafts import spawn_publish_task

        return spawn_publish_task(self=self, task_id=task_id, draft_id=draft_id, tag=tag)


    # ══════════════════════════════════════════
    #  周总结
    # ══════════════════════════════════════════

    async def _check_weekly_summary(self):
        """检查并生成周总结（实现已抽离到 scheduler_weekly）"""
        from bilibot.scheduler_weekly import check_weekly_summary

        await check_weekly_summary(self)

    def _build_week_summary_from_sqlite(self, data_dir: str) -> str:
        """Build a bounded weekly prompt input from validated V6 events."""
        from bilibot.scheduler_weekly import build_week_summary_from_sqlite

        return build_week_summary_from_sqlite(self, data_dir)

    # ══════════════════════════════════════════
    #  调度管理
    # ══════════════════════════════════════════

    def _generate_daily_schedule(self):
        """实现已抽离到 scheduler_daily"""
        from bilibot.scheduler_daily import generate_daily_schedule

        return generate_daily_schedule(self=self)


    def _persist_schedule(self, scene: str, times: List[tuple], grace_window: int, max_attempts: int, date_str: str):
        """实现已抽离到 scheduler_daily"""
        from bilibot.scheduler_daily import persist_schedule

        return persist_schedule(self=self, scene=scene, times=times, grace_window=grace_window, max_attempts=max_attempts, date_str=date_str)


    def _mark_overdue_as_triggered(self):
        """实现已抽离到 scheduler_daily"""
        from bilibot.scheduler_daily import mark_overdue_as_triggered

        return mark_overdue_as_triggered(self=self)


    def _save_schedule_state(self):
        """实现已抽离到 scheduler_daily"""
        from bilibot.scheduler_daily import save_schedule_state

        return save_schedule_state(self=self)


    def _save_dynamic_schedule_state(self):
        """实现已抽离到 scheduler_daily"""
        from bilibot.scheduler_daily import save_dynamic_schedule_state

        return save_dynamic_schedule_state(self=self)


    def get_schedule_snapshot(self) -> Dict[str, Any]:
        """实现已抽离到 scheduler_daily"""
        from bilibot.scheduler_daily import get_schedule_snapshot

        return get_schedule_snapshot(self=self)


    # ── 私有辅助 ──

    def _get_data_dir(self) -> str:
        """获取数据目录"""
        if self.ds is not None and hasattr(self.ds, "data_dir"):
            return str(self.ds.data_dir)
        return self.config_loader.get("data_dir", "./data")

    def _get_current_persona_id(self) -> str:
        """获取当前 persona id（多账号：优先用账号绑定的人格）"""
        if self.persona_store is None:
            return "unknown"
        try:
            # V2：优先解析账号绑定的人格
            if self.account_id:
                pid = self.persona_store.get_account_persona_id(self.account_id)
                if pid:
                    return pid
            p = self.persona_store.get_current()
            return p.id if p else "unknown"
        except Exception:
            return "unknown"

    def _check_bili_risk_control(self, scene: str = "") -> None:
        """PRD 4.9：检查 B站 API 风控码（-352），触发时自动暂停账号

        SAFE-501：风控只暂停触发的账号，不影响其他账号的自动发布。
        """
        code = getattr(self.bili, "last_api_code", 0) if self.bili else 0
        if code == -352 and self.safety_checker is not None:
            try:
                acc_id = self.account_id or "_global_"
                self.safety_checker.pause_account(acc_id, reason="bilibili风控触发(code=-352)")
                logger.error(f"B站风控触发 scene={scene} account={acc_id}，账号已自动暂停")
            except Exception as e:
                logger.error(f"自动暂停失败: {e}")

    # ── TaskRun 生命周期辅助（PRD-V5 §7 / TASK-501）──

    def _succeed_task(self, task_id: str, result: Dict[str, Any]) -> None:
        """标记 TaskRun 为 succeeded（只有真正成功才调用）"""
        try:
            self.task_store.succeed(task_id, result)
        except Exception as e:
            logger.warning(f"TaskRun {task_id} succeed 失败: {e}")

    def _fail_task(self, task_id: str, error_code: str, error: str,
                   retryable: bool = True) -> None:
        """标记 TaskRun 失败（retry_wait/failed 由 store 根据 attempt 决定）"""
        try:
            self.task_store.fail(task_id, error_code, error, retryable=retryable)
        except Exception as e:
            logger.warning(f"TaskRun {task_id} fail 失败: {e}")
        # Task 11.3：永久失败时，若是动态草稿发布任务，同步草稿状态为 failed
        if not retryable:
            try:
                task = self.task_store.get(task_id)
                scene = getattr(task, "scene", None) if task else None
                if task and scene in ("dynamic", "dynamic_post"):
                    input_data = json.loads(task.input_json) if task.input_json else {}
                    if input_data.get("kind") == "publish_draft":
                        draft_id = input_data.get("draft_id", "")
                        if draft_id:
                            self._get_draft_store().mark_failed(draft_id, error)
            except Exception as e:
                logger.warning(f"Task 11.3: 同步草稿 failed 状态失败 task={task_id}: {e}")

    def _mark_task_result_unknown(self, task_id: str, error: str = "") -> None:
        """PRD-V5 §7.2：平台成功 + 本地失败 → result_unknown（不自动重发）"""
        try:
            self.task_store.mark_result_unknown(task_id, error)
        except Exception as e:
            logger.warning(f"TaskRun {task_id} mark_result_unknown 失败: {e}")

    def create_manual_task(
        self,
        scene: str,
        input_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """PRD-V5 §7.4：手动创建 TaskRun（trigger_type=manual）

        返回 task_id（创建失败返回 None）。
        """
        import time as _time
        from bilibot.services.task_store import (
            TRIGGER_MANUAL, DEFAULT_GRACE_WINDOW, DEFAULT_MAX_ATTEMPTS,
        )
        try:
            config = self.config_loader.get_raw_config()
            prov = config.get("proactive", {})
            scenes_cfg = prov.get("scenes", {}) or {}
            scene_cfg = scenes_cfg.get(scene, {}) or {}
            default_grace = prov.get("grace_window_seconds", DEFAULT_GRACE_WINDOW)
            grace = scene_cfg.get("grace_window_seconds", default_grace)
            max_att = scene_cfg.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
            # 幂等键：account + scene + manual + 时间戳（手动任务允许重复创建）
            idem_key = (
                f"{self.account_id or '_default'}:{scene}:manual:"
                f"{int(_time.time() * 1000)}"
            )
            task = self.task_store.create(
                account_id=self.account_id or "_default",
                scene=scene,
                idempotency_key=idem_key,
                trigger_type=TRIGGER_MANUAL,
                scheduled_at=_time.time(),
                input_data=input_data or {"trigger_source": "manual"},
                max_attempts=max_att,
                grace_window=grace,
            )
            return task.task_id if task else None
        except Exception as e:
            logger.error(f"手动创建 TaskRun 失败 scene={scene}: {e}")
            return None
