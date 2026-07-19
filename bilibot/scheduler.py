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
import hashlib
import logging
import random
import json
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from bilibot.config_loader import ConfigLoader
from bilibot.bilibili_api import BilibiliAPI
from bilibot.user_state import UserStateSystem
from bilibot.personality import PersonalitySystem
from bilibot.llm_adapter import LLMAdapter
from bilibot.data_store import DataStore
from bilibot.reply import ReplyGenerator
from bilibot.humanized_behavior import (
    HumanBehaviorSimulator,
    VideoBrowser,
    HumanizedCommentGenerator,
    DynamicPoster,
)
from bilibot.models import SceneType
from bilibot.models.proactive_video_context import ProactiveVideoContext
from bilibot.video_understanding.audio_track import ASRTranscriptionError

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

    def _maybe_schedule_bangumi_check(self, now: datetime) -> bool:
        """Dispatch the daily bangumi check without blocking the main loop."""
        service = getattr(self, "bangumi_service", None)
        if service is None or now.date() == self._last_bangumi_check_date:
            return False
        task = getattr(self, "_bangumi_check_task", None)
        if task is not None and not task.done():
            return False
        if time.monotonic() < float(getattr(self, "_bangumi_retry_after", 0.0) or 0.0):
            return False
        self._bangumi_check_task = self._spawn_memory_task(
            self._run_bangumi_daily_check(service, now.date()),
            tag=f"bangumi_daily:{self.account_id or '-'}:{now.date().isoformat()}",
        )
        return True

    async def _run_bangumi_daily_check(self, service, check_date) -> None:
        """Run a potentially long PGC update/watch job with bounded retries."""
        current = asyncio.current_task()
        try:
            result = await service.check_updates()
            self._last_bangumi_check_date = check_date
            self._bangumi_failure_count = 0
            self._bangumi_retry_after = 0.0
            result = result if isinstance(result, dict) else {}
            if result.get("updated", 0) > 0:
                logger.info(
                    "追番更新检测: %s 部有更新，已观看 %s 集",
                    result.get("updated", 0),
                    result.get("watched", 0),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures = int(getattr(self, "_bangumi_failure_count", 0) or 0) + 1
            self._bangumi_failure_count = failures
            # 5m, 10m, 20m ... capped at 6h.  Avoid hammering Bilibili every
            # main-loop minute while preserving same-day recovery.
            delay = min(6 * 3600.0, 300.0 * (2 ** min(failures - 1, 8)))
            self._bangumi_retry_after = time.monotonic() + delay
            logger.error(
                "追番更新检测失败，将在 %.0f 秒后重试: %s",
                delay,
                type(exc).__name__,
                exc_info=True,
            )
        finally:
            if getattr(self, "_bangumi_check_task", None) is current:
                self._bangumi_check_task = None

    async def _do_bangumi_task(self, task_id: str) -> None:
        """Execute one observable manual/retry bangumi TaskRun."""
        task = self.task_store.get(task_id) if self.task_store else None
        if task is None:
            logger.warning("番剧 TaskRun 不存在: %s", task_id)
            return
        status = getattr(task, "status", None)
        if status == "scheduled" and not self.task_store.claim(task_id):
            logger.warning("番剧 TaskRun claim 失败: %s", task_id)
            return
        elif status not in ("scheduled", "claimed"):
            logger.warning("番剧 TaskRun 状态不可执行: %s status=%s", task_id, status)
            return
        if not self.task_store.start(task_id):
            logger.warning("番剧 TaskRun start 失败: %s", task_id)
            return
        service = getattr(self, "bangumi_service", None)
        if service is None:
            self._fail_task(
                task_id,
                "BANGUMI_DISABLED",
                "番剧追更未启用或记忆大脑未就绪",
                retryable=False,
            )
            return
        try:
            result = await service.check_updates()
            result = result if isinstance(result, dict) else {}
            self._last_bangumi_check_date = datetime.now().date()
            self._bangumi_failure_count = 0
            self._bangumi_retry_after = 0.0
            self._succeed_task(
                task_id,
                {
                    "success": True,
                    "summary": (
                        f"追番检查完成：更新 {result.get('updated', 0)} 部，"
                        f"观看 {result.get('watched', 0)} 集"
                    ),
                    "updated": int(result.get("updated", 0) or 0),
                    "watched": int(result.get("watched", 0) or 0),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("手动追番任务失败: %s", type(exc).__name__, exc_info=True)
            self._fail_task(
                task_id,
                "BANGUMI_CHECK_FAILED",
                str(exc)[:500],
                retryable=True,
            )

    async def _build_video_detail_digest(
        self,
        *,
        title: str,
        owner: str,
        behavior_log: str,
        extra_context: str = "",
        max_attempts: int = 2,
        require_llm: bool = True,
    ) -> str:
        """Compress audiovisual log into ≤2000 chars for later recall/comment.

        Retries LLM summarization on failure/empty/raw-log dumps. When
        ``require_llm`` is True (default for proactive watch), heuristic
        truncation is NOT accepted as success so the caller can skip the video.
        """
        from bilibot.memory_brain.gateway import MemoryModelGateway

        log = str(behavior_log or "").strip()
        if not log:
            return ""

        gateway = None
        brain = getattr(self, "memory_brain", None)
        if brain is not None and getattr(brain, "gateway", None) is not None:
            gateway = brain.gateway
        elif getattr(self, "llm", None) is not None:
            gateway = MemoryModelGateway(chat_provider=self.llm, embedding_provider=None)
        else:
            gateway = MemoryModelGateway()

        attempts = max(1, int(max_attempts))
        last_err = ""
        for attempt in range(1, attempts + 1):
            try:
                detail = await gateway.summarize_video_detail(
                    title=title,
                    owner=owner,
                    behavior_log=log,
                    extra_context=extra_context,
                    max_chars=2000,
                    allow_heuristic=not require_llm,
                )
            except Exception as exc:
                last_err = type(exc).__name__
                logger.warning(
                    "视频详细内容摘要失败 attempt=%s/%s: %s",
                    attempt,
                    attempts,
                    last_err,
                )
                detail = ""
            detail = str(detail or "").strip()
            if detail and not MemoryModelGateway.looks_like_heuristic_video_detail(detail):
                logger.info(
                    "视频详细内容摘要完成: %s 字 (attempt=%s/%s)",
                    len(detail),
                    attempt,
                    attempts,
                )
                return detail[:2000]
            last_err = last_err or "empty_or_heuristic"
            if attempt < attempts:
                # brief backoff before retrying the same video
                await asyncio.sleep(min(2.0 * attempt, 4.0))

        if require_llm:
            logger.warning(
                "视频详细内容摘要在 %s 次尝试后仍失败（%s），将换视频",
                attempts,
                last_err or "unknown",
            )
            return ""

        # Non-strict path: accept heuristic as last resort.
        detail = MemoryModelGateway.heuristic_video_detail(
            title=title, owner=owner, behavior_log=log, max_chars=2000
        )
        return str(detail or "").strip()[:2000]

    async def _archive_required(self, envelope, *, treat_idempotent_as_ready: bool = False):
        """Archive a raw observation before any irreversible business action.

        Store semantics:
        - same idempotency_key + same content_hash → soft success (no exception)
        - same key + different hash → IdempotencyConflictError

        ``treat_idempotent_as_ready`` only absorbs conflict when the existing
        event is already a full video watch for the same bvid (digest non-
        determinism on retry). It must NOT silently accept a different video
        bound to a reused key.
        """
        brain = getattr(self, "memory_brain", None)
        if brain is None and not getattr(self, "_memory_brain_required", False):
            # Compatibility for direct legacy/minimal Scheduler construction.
            # Production AccountInstance always injects the V6 service.
            return None
        if brain is None:
            raise RuntimeError("V6 memory brain is not initialized")
        try:
            result = await brain.archive_observation_async(envelope)
            if result is None:
                raise RuntimeError("V6 archive returned no commit result")
            if getattr(result, "source_committed", True) is False:
                raise RuntimeError("V6 archive source commit was not confirmed")
            return result
        except Exception as exc:
            # IdempotencyConflictError / ReingestBlockedError 是良性条件
            # （数据已存在或已被删除 tombstone），不是存储故障，不应暂停账号。
            from bilibot.memory_brain.models import (
                IdempotencyConflictError,
                ReingestBlockedError,
            )
            if isinstance(exc, IdempotencyConflictError) and treat_idempotent_as_ready:
                # 仅当库里已是「同 bvid 的完整观看或已完成闭环」时才视为就绪。
                # content_hash 不同通常来自 digest / experience 非确定性；绝不能在
                # key 复用导致「新片撞旧片」时继续评价。
                meta = getattr(envelope, "metadata", None) or {}
                if not isinstance(meta, dict):
                    meta = {}
                bvid = str(meta.get("bvid") or "").strip()
                ready = False
                if bvid:
                    if await self._has_full_video_observation(bvid):
                        ready = True
                    elif await self._has_completed_proactive_video(bvid):
                        ready = True
                if ready:
                    logger.info(
                        "memory archive conflict treated as ready: "
                        "account=%s key=%s bvid=%s",
                        self.account_id,
                        getattr(envelope, "idempotency_key", ""),
                        bvid,
                    )
                    return {
                        "source_committed": True,
                        "idempotent_hit": True,
                        "content_mismatch": True,
                    }
                logger.warning(
                    "memory archive idempotency conflict (not ready): "
                    "account=%s key=%s bvid=%s",
                    self.account_id,
                    getattr(envelope, "idempotency_key", ""),
                    bvid,
                )
                raise
            if isinstance(exc, (IdempotencyConflictError, ReingestBlockedError)):
                logger.warning(
                    "memory archive skipped (idempotency conflict or blocked): "
                    "account=%s key=%s",
                    self.account_id,
                    getattr(envelope, "idempotency_key", ""),
                )
                raise
            self._pause_for_memory_failure()
            logger.error(
                "required memory archive failed; account actions paused: account=%s",
                self.account_id,
                exc_info=True,
            )
            raise

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
        """True only for a real audiovisual watch/digest — not metadata/like/etc."""
        if not isinstance(event, dict):
            return False
        event_type = str(event.get("event_type") or "")
        source_type = str(event.get("source_type") or "")
        if event_type != "video_observation" and source_type != "video":
            return False
        meta = event.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("has_video_detail"):
            return True
        for source in event.get("sources") or []:
            if not isinstance(source, dict):
                continue
            if source.get("source_type") not in self._FULL_VIDEO_SOURCE_TYPES:
                continue
            if str(source.get("full_text") or source.get("text") or "").strip():
                return True
        return False

    async def _has_full_video_observation(self, bvid: str) -> bool:
        """True if account brain already has a full audiovisual watch for bvid."""
        brain = getattr(self, "memory_brain", None)
        bvid_value = str(bvid or "").strip()
        if not brain or not bvid_value:
            return False
        try:
            hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid_value], 20)
            for hit in hits or []:
                if not isinstance(hit, dict):
                    continue
                hit_meta = hit.get("metadata") or {}
                if not isinstance(hit_meta, dict):
                    hit_meta = {}
                # Prefer exact bvid match on metadata; fall through to load full event.
                meta_bvid = str(hit_meta.get("bvid") or "").strip()
                if meta_bvid and meta_bvid != bvid_value:
                    continue
                # Lightweight path: metadata already flags a digest-backed watch.
                if meta_bvid == bvid_value and hit_meta.get("has_video_detail"):
                    if str(hit.get("event_type") or "") == "video_observation" or str(
                        hit.get("source_type") or ""
                    ) == "video":
                        return True
                event_id = str(hit.get("event_id") or hit.get("id") or "")
                if not event_id:
                    continue
                event = await asyncio.to_thread(brain.get_event, event_id, None)
                if not event:
                    continue
                event_meta = event.get("metadata") or {}
                if not isinstance(event_meta, dict):
                    event_meta = {}
                if str(event_meta.get("bvid") or "").strip() not in {"", bvid_value}:
                    continue
                if str(event_meta.get("bvid") or "").strip() != bvid_value:
                    # Accept via source external_id when metadata.bvid missing.
                    if not any(
                        isinstance(s, dict)
                        and str(s.get("external_id") or "").strip() == bvid_value
                        for s in (event.get("sources") or [])
                    ):
                        continue
                if self._event_is_full_video_watch(event):
                    return True
            return False
        except Exception as exc:
            logger.debug(
                "full video observation check failed bvid=%s: %s",
                bvid_value,
                type(exc).__name__,
            )
            return False

    async def _has_completed_proactive_video(self, bvid: str) -> bool:
        """Skip candidate only after the evaluate/interact cycle archived experience.

        Full video_observation alone is NOT enough: archive happens before evaluate.
        If evaluate/interact fails after archive, retry must be allowed to finish
        the cycle (idempotent archive + policy dedupe protect side effects).
        """
        brain = getattr(self, "memory_brain", None)
        bvid_value = str(bvid or "").strip()
        if not brain or not bvid_value:
            return False
        try:
            hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid_value], 30)
            for hit in hits or []:
                if not isinstance(hit, dict):
                    continue
                event_type = str(hit.get("event_type") or "")
                source_type = str(hit.get("source_type") or "")
                hit_meta = hit.get("metadata") or {}
                if not isinstance(hit_meta, dict):
                    hit_meta = {}
                meta_bvid = str(hit_meta.get("bvid") or "").strip()
                if meta_bvid and meta_bvid != bvid_value:
                    continue
                if event_type == "bot_experience" or source_type in {
                    "video_experience",
                    "bot_experience",
                }:
                    if meta_bvid == bvid_value:
                        return True
                    # metadata may be unparsed on list rows; load full event
                    event_id = str(hit.get("event_id") or hit.get("id") or "")
                    if not event_id:
                        continue
                    event = await asyncio.to_thread(brain.get_event, event_id, None)
                    if not event:
                        continue
                    em = event.get("metadata") or {}
                    if isinstance(em, dict) and str(em.get("bvid") or "").strip() == bvid_value:
                        if str(event.get("event_type") or "") == "bot_experience" or str(
                            event.get("source_type") or ""
                        ) in {"video_experience", "bot_experience"}:
                            return True
            return False
        except Exception as exc:
            logger.debug(
                "completed proactive video check failed bvid=%s: %s",
                bvid_value,
                type(exc).__name__,
            )
            return False

    async def _load_existing_video_detail(self, bvid: str) -> str:
        """Load archived video_detail / summary for a bvid if a full watch exists."""
        brain = getattr(self, "memory_brain", None)
        bvid_value = str(bvid or "").strip()
        if not brain or not bvid_value:
            return ""
        try:
            hits = await asyncio.to_thread(brain.find_by_identifiers, [bvid_value], 20)
            for hit in hits or []:
                if not isinstance(hit, dict):
                    continue
                hit_meta = hit.get("metadata") or {}
                if not isinstance(hit_meta, dict):
                    hit_meta = {}
                meta_bvid = str(hit_meta.get("bvid") or "").strip()
                if meta_bvid and meta_bvid != bvid_value:
                    continue
                event_id = str(hit.get("event_id") or hit.get("id") or "")
                if not event_id:
                    continue
                event = await asyncio.to_thread(brain.get_event, event_id, None)
                if not event or not self._event_is_full_video_watch(event):
                    continue
                event_meta = event.get("metadata") or {}
                if not isinstance(event_meta, dict):
                    event_meta = {}
                if str(event_meta.get("bvid") or "").strip() not in {"", bvid_value}:
                    continue
                if str(event_meta.get("bvid") or "").strip() != bvid_value:
                    if not any(
                        isinstance(s, dict)
                        and str(s.get("external_id") or "").strip() == bvid_value
                        for s in (event.get("sources") or [])
                    ):
                        continue
                # Prefer dedicated video_detail source text.
                for source in event.get("sources") or []:
                    if not isinstance(source, dict):
                        continue
                    if source.get("source_type") == "video_detail":
                        text = str(source.get("full_text") or "").strip()
                        if text:
                            return text[:2000]
                summary = str(event.get("summary") or "").strip()
                if summary and len(summary) >= 40:
                    return summary[:2000]
            return ""
        except Exception as exc:
            logger.debug(
                "load existing video_detail failed bvid=%s: %s",
                bvid_value,
                type(exc).__name__,
            )
            return ""

    def _compose_video_content_for_prompt(
        self,
        ctx: "ProactiveVideoContext",
        *,
        video_detail: str = "",
    ) -> str:
        """Build evaluate/comment input: digest first, keep untrusted search ref.

        If ``ctx`` already carries memory/companion (set before compose), append
        them so digest path does not drop cross-scene evidence. Callers may still
        pass memory_evidence kwargs to evaluate/comment for dual coverage.
        """
        parts: list[str] = []
        detail = str(video_detail or "").strip()
        if detail:
            parts.append(detail)
            search_block = ctx.format_search_reference()
            if search_block:
                parts.append(search_block)
        else:
            # to_prompt_sections already includes search + memory/companion if set.
            return ctx.to_prompt_sections(
                include_metadata=False, include_hot_comments=False,
            )
        mem = str(getattr(ctx, "memory_evidence", "") or "").strip()
        if mem:
            parts.append("【相关记忆/近期经历】\n" + mem[:1800])
        life = str(getattr(ctx, "companion_context", "") or "").strip()
        if life:
            parts.append("【你今天的状态与念头】\n" + life[:500])
        return "\n\n".join(parts)

    def _pause_for_memory_failure(self) -> None:
        """Pause account when irreversible observation would be lost.

        Recovery: fix storage/brain health, then
        ``safety_checker.resume_account(account_id)``. Status exposes
        ``memory_archive=true`` and ``resume_hint``.
        """
        safety = getattr(self, "safety_checker", None)
        account_id = str(getattr(self, "account_id", "") or "")
        if safety is None or not account_id:
            logger.error(
                "memory archive failure without safety_checker/account_id; "
                "cannot pause account=%s",
                account_id or "-",
            )
            return
        reason = "memory_archive_failed"
        try:
            pause = getattr(safety, "pause_account", None)
            if callable(pause):
                pause(account_id, reason=reason)
            logger.error(
                "account paused for memory integrity: account=%s reason=%s "
                "(resume after brain is writable via resume_account)",
                account_id,
                reason,
            )
        except Exception as exc:
            logger.error(
                "failed to pause account after memory archive failure: account=%s err=%s",
                account_id,
                type(exc).__name__,
            )

    def _collect_related_titles_for_dynamic(self, brain, *, limit: int = 5) -> List[str]:
        """Titles from recent watch/experience/bangumi events for dynamic grounding.

        ``list_events`` only filters one source_type at a time; merge several
        relevant types and de-dupe by title.
        """
        if brain is None or not hasattr(brain, "list_events"):
            return []
        source_types = (
            "video_experience",
            "video",
            "bangumi",
            "diary",
            "dream",
            "bot_action",
        )
        per = max(5, int(limit) * 2)
        seen: set[str] = set()
        titles: List[str] = []

        def _take_from(events) -> None:
            for event in events or []:
                if not isinstance(event, dict):
                    continue
                title = str(event.get("title") or event.get("event_title") or "").strip()
                if not title or title in seen:
                    continue
                # Skip pure system action labels without content signal
                st = str(event.get("source_type") or "")
                if st == "bot_action" and not title.startswith("《"):
                    # Prefer event summary snippet for dynamic_post bot_actions
                    summary = str(
                        event.get("summary") or event.get("event_summary") or ""
                    ).strip()
                    if summary and summary not in seen:
                        label = summary[:40]
                        seen.add(summary)
                        titles.append(label)
                    continue
                seen.add(title)
                titles.append(f"《{title}》" if not title.startswith("《") else title)
                if len(titles) >= limit:
                    return

        try:
            for st in source_types:
                if len(titles) >= limit:
                    break
                try:
                    events = brain.list_events(limit=per, source_type=st)
                except TypeError:
                    events = brain.list_events(limit=per)
                except Exception:
                    continue
                _take_from(events)
            if len(titles) < limit:
                try:
                    _take_from(brain.list_events(limit=per))
                except Exception:
                    pass
        except Exception:
            return titles[:limit]
        return titles[:limit]

    def _notify_companion_dynamic_posted(
        self,
        *,
        content: str = "",
        topic: str = "",
        draft_id: str = "",
        dynamic_id: str = "",
        task_id: str = "",
    ) -> None:
        companion = getattr(self, "companion", None)
        if companion is None or not getattr(companion, "enabled", False):
            return
        try:
            if not hasattr(companion, "on_dynamic_posted"):
                return
            companion.on_dynamic_posted(
                content=content or "",
                topic=topic or "",
                draft_id=draft_id or "",
                dynamic_id=dynamic_id or "",
                task_id=task_id or "",
            )
        except TypeError:
            # Older signature without correlation kwargs
            try:
                companion.on_dynamic_posted(content=content or "", topic=topic or "")
            except Exception as e:
                logger.debug("companion dynamic feedback failed: %s", e)
        except Exception as e:
            logger.debug("companion dynamic feedback failed: %s", e)

    def _notify_companion_private_message_replied(
        self,
        *,
        actor_label: str = "",
    ) -> None:
        """Push continuous-self feedback after a real PM send (no body text)."""
        companion = getattr(self, "companion", None)
        if companion is None or not getattr(companion, "enabled", False):
            return
        on_pm = getattr(companion, "on_private_message_replied", None)
        if not callable(on_pm):
            return
        try:
            on_pm(preview="", actor_label=str(actor_label or "")[:24])
        except Exception:
            logger.debug("companion PM feedback failed", exc_info=True)

    def _notify_companion_comment_replied(
        self,
        *,
        title: str = "",
        preview: str = "",
        proactive: bool = False,
    ) -> None:
        companion = getattr(self, "companion", None)
        if companion is None or not getattr(companion, "enabled", False):
            return
        on_cmt = getattr(companion, "on_comment_replied", None)
        if not callable(on_cmt):
            return
        try:
            on_cmt(
                title=str(title or "")[:40],
                preview=str(preview or "")[:80],
                proactive=bool(proactive),
            )
        except Exception:
            logger.debug("companion comment feedback failed", exc_info=True)

    async def _recall_for_proactive_video(
        self,
        *,
        title: str = "",
        owner: str = "",
        tags: Optional[List[str]] = None,
        bvid: str = "",
        oid: str = "",
        desc: str = "",
    ) -> Dict[str, Any]:
        """Account-scoped hybrid recall for evaluate / proactive-comment.

        Pulls related video/bangumi/diary/comment experiences so the model can
        ground comments beyond the current clip + companion surface alone.
        Failures degrade to empty evidence (do not block watching/archiving).
        """
        empty = {"memory_evidence": "", "memory_event_ids": [], "event_count": 0}
        brain = getattr(self, "memory_brain", None)
        if brain is None or not callable(getattr(brain, "recall", None)):
            return empty
        tags_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
        query_parts = [
            "最近看的视频、番剧、日记、评论、心情",
            str(title or "").strip(),
            f"UP主 {owner}" if owner else "",
            " ".join(tags_list[:6]),
            str(desc or "")[:120],
        ]
        query_text = " ".join(p for p in query_parts if p).strip()
        if not query_text:
            query_text = "最近观看与生活经历"
        try:
            from bilibot.memory_brain import RecallQuery

            result = await brain.recall(
                RecallQuery(
                    current_message=query_text,
                    account_id=self.account_id or "",
                    title=str(title or "").strip(),
                    bvid=str(bvid or "").strip(),
                    oid=str(oid or "").strip(),
                    scene="proactive_video",
                )
            )
        except Exception as exc:
            logger.debug(
                "proactive video memory recall failed: %s", type(exc).__name__
            )
            return empty
        evidence = ""
        event_ids: List[str] = []
        events = ()
        if result is not None:
            evidence = str(getattr(result, "prompt_evidence", "") or "")
            events = getattr(result, "events", ()) or ()
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                eid = str(ev.get("id") or ev.get("event_id") or "").strip()
                if eid and eid not in event_ids:
                    event_ids.append(eid)
        if evidence:
            logger.info(
                "主动视频混合召回: events=%s evidence_chars=%s bvid=%s",
                len(events),
                len(evidence),
                bvid or "-",
            )
        return {
            "memory_evidence": evidence,
            "memory_event_ids": event_ids[:20],
            "event_count": len(events),
        }

    async def _begin_activity_context(
        self,
        *,
        action_key: str,
        action_type: str,
        current_activity: str,
        query: str = "",
        scene: str = "system",
        title: str = "",
        bvid: str = "",
        oid: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Create durable current intent and return its cross-scene memory."""
        brain = getattr(self, "memory_brain", None)
        if brain is None:
            if getattr(self, "_memory_brain_required", False):
                self._pause_for_memory_failure()
                raise RuntimeError("V6 memory brain is not initialized")
            return None
        begin = getattr(brain, "begin_activity", None)
        if not callable(begin) or not callable(
            getattr(type(brain), "begin_activity", None)
        ):
            # Compatibility for isolated legacy/unit constructions. Production
            # always receives MemoryBrainService, which implements this method.
            if getattr(self, "_memory_brain_required", False):
                self._pause_for_memory_failure()
                raise RuntimeError("V6 activity memory is not available")
            return None
        try:
            context = await begin(
                action_key=action_key,
                action_type=action_type,
                current_activity=current_activity,
                query=query,
                scene=scene,
                title=title,
                bvid=bvid,
                oid=oid,
                persona_id=self._get_current_persona_id(),
                metadata=metadata or {},
            )
        except Exception:
            self._pause_for_memory_failure()
            logger.error(
                "activity memory initialization failed: account=%s action=%s",
                self.account_id,
                action_key,
                exc_info=True,
            )
            raise
        if not str(getattr(context, "prompt_text", "") or "").strip():
            self._pause_for_memory_failure()
            raise RuntimeError("V6 activity memory returned an empty context")
        return context

    async def _archive_bot_action(
        self,
        *,
        action_key: str,
        action_type: str,
        text: str,
        published: bool,
        title: str = "",
        scene: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
        importance: float = 0.6,
        status: str = "",
    ):
        """Archive a terminal bot action; prefer finish_activity when available."""
        brain = getattr(self, "memory_brain", None)
        finish = getattr(brain, "finish_activity", None) if brain is not None else None
        terminal = str(status or ("completed" if published else "failed")).strip().casefold()
        if terminal == "intent":
            terminal = "completed" if published else "failed"
        if callable(finish) and callable(getattr(type(brain), "finish_activity", None)):
            try:
                return await finish(
                    action_key=action_key,
                    action_type=action_type,
                    result_text=text,
                    state=terminal if terminal != "rejected" else "rejected",
                    scene=scene,
                    title=title,
                    persona_id=self._get_current_persona_id(),
                    metadata=metadata or {},
                )
            except Exception:
                logger.warning(
                    "finish_activity failed, falling back to archive: action=%s",
                    action_key,
                    exc_info=True,
                )
        from bilibot.memory_brain.ingestion import bot_action_observation

        return await self._archive_required(
            bot_action_observation(
                account_id=self.account_id or "default",
                action_key=action_key,
                action_type=action_type,
                text=text,
                published=published,
                persona_id=self._get_current_persona_id(),
                title=title,
                scene=scene,
                metadata=metadata or {},
                importance=importance,
                state=status,
            )
        )

    def _bot_aliases(self) -> set[str]:
        aliases = {"bot", "亚托莉", "亚托莉小姐", "atri", "ATRI"}
        bot_name = str(getattr(self, "_bot_name", "") or "").strip()
        if bot_name:
            aliases.add(bot_name)
        try:
            persona_id = str(self._get_current_persona_id() or "").strip()
        except Exception:
            persona_id = ""
        if persona_id:
            aliases.add(persona_id)
        return {alias.casefold() for alias in aliases if alias}

    def _is_bot_speaker(self, actor_id: str | int = "", username: str = "", text: str = "") -> bool:
        bot_uid = str(getattr(self, "_bot_uid", "") or "")
        actor = str(actor_id or "")
        if bot_uid and actor and actor == bot_uid:
            return True
        aliases = self._bot_aliases()
        uname = str(username or "").strip().casefold()
        if uname and uname in aliases:
            return True
        lowered = str(text or "").casefold()
        return any(f"@{alias}" in lowered for alias in aliases if alias not in {"bot", "atri"})

    def _bot_already_replied_to_source(
        self,
        replies: list,
        *,
        source_rpid: str,
        expected_text: str = "",
    ) -> bool:
        """楼中楼幂等：仅当 bot 已回复该 source_rpid（parent 匹配）才算已回。

        旧逻辑 any(mid==bot) 会把同楼其它回复当成已回，导致楼中楼漏回。
        匹配规则（任一命中即 True）：
        1. mid==bot 且 parent/parent_str == source_rpid
        2. mid==bot 且 content.message 与 expected_text 文本一致（生成文本对账）
        不用 rpid==source_rpid：source 是用户评论 id，bot 回复 rpid 不同。
        """
        bot_uid = str(getattr(self, "_bot_uid", "") or "")
        if not bot_uid or not replies:
            return False
        source = str(source_rpid or "")
        expected = (expected_text or "").strip()
        for r in replies or []:
            if not isinstance(r, dict):
                continue
            member = r.get("member") or {}
            mid = str(member.get("mid") or r.get("mid") or "")
            if mid != bot_uid:
                continue
            parent = str(
                r.get("parent")
                or r.get("parent_str")
                or (r.get("reply_control") or {}).get("parent")
                or ""
            )
            if source and parent and parent == source:
                return True
            if expected:
                content = r.get("content") or {}
                msg = str(
                    content.get("message") if isinstance(content, dict) else content or ""
                ).strip()
                if msg and msg == expected:
                    return True
        return False

    def _comment_memory_title(self, username: str, text: str, reply_id: str | int = "") -> str:
        name = str(username or "未知用户").strip() or "未知用户"
        snippet = " ".join(str(text or "").split())
        if len(snippet) > 36:
            snippet = snippet[:36] + "…"
        return f"{name} 评论：{snippet}" if snippet else f"{name} 的评论 {reply_id}"

    def _dynamic_card_context(self, card: Dict[str, Any]) -> Dict[str, Any]:
        """Extract a small target context for comments under Bot's own dynamics."""
        basic = card.get("basic") or {}
        modules = card.get("modules") or {}
        dynamic = modules.get("module_dynamic") or {}
        desc = card.get("desc") or {}
        item = card.get("item") or {}

        text_candidates: List[str] = []
        major = dynamic.get("major") if isinstance(dynamic, dict) else {}
        opus = major.get("opus") if isinstance(major, dict) else {}
        opus_summary = opus.get("summary") if isinstance(opus, dict) else {}
        dynamic_desc = dynamic.get("desc") if isinstance(dynamic, dict) else {}
        for value in (
            dynamic_desc.get("text") if isinstance(dynamic_desc, dict) else "",
            opus_summary.get("text") if isinstance(opus_summary, dict) else "",
            item.get("description"),
            item.get("content"),
            item.get("title"),
            card.get("title"),
        ):
            if isinstance(value, str) and value.strip():
                text_candidates.append(value.strip())

        return {
            "kind": "own_dynamic",
            "dynamic_id": str(
                card.get("id_str")
                or card.get("id")
                or desc.get("dynamic_id_str")
                or desc.get("dynamic_id")
                or ""
            ),
            "comment_oid": str(
                basic.get("comment_id_str")
                or basic.get("comment_id")
                or basic.get("rid_str")
                or basic.get("rid")
                or ""
            ),
            "dynamic_text": (text_candidates[0] if text_candidates else "")[:500],
        }

    def _redact_private_message_runtime(
        self, text: str, *, actor_id: str | int, username: str = ""
    ):
        """Return a PM-safe value even on the legacy direct-construction path."""
        brain = getattr(self, "memory_brain", None)
        if brain is not None:
            return brain.redact_private_message(
                text, actor_id=actor_id, username=username
            )
        from bilibot.memory_brain.redaction import redact_private_message

        salt = hashlib.sha256(
            f"bilibot:legacy-pm:{getattr(self, 'account_id', '')}".encode("utf-8")
        ).digest()
        return redact_private_message(
            text,
            actor_id=actor_id,
            account_salt=salt,
            current_username=username,
        )

    @staticmethod
    def _bounded_recent_turns(
        turns: List[str], *, max_turns: int = 6, max_chars: int = 1200
    ) -> List[str]:
        selected: List[str] = []
        remaining = max(0, int(max_chars))
        for value in reversed(list(turns or [])[-max_turns:]):
            text = str(value or "")
            if not text or remaining <= 0:
                continue
            if len(text) > remaining:
                text = text[-remaining:]
            selected.append(text)
            remaining -= len(text)
        selected.reverse()
        return selected

    async def _archive_comment_thread_context(
        self,
        replies: List[Dict[str, Any]],
        *,
        oid: str | int,
        comment_type: int,
        thread_key: str | int,
    ) -> List[str]:
        rows: List[Dict[str, Any]] = []
        prompt_lines: List[str] = []
        bot_name = str(getattr(self, "_bot_name", "") or "Bot")
        for reply in replies or []:
            member = reply.get("member", {}) or {}
            content = reply.get("content", {}) or {}
            text = str(content.get("message") or "")
            if not text:
                continue
            actor_id = str(reply.get("mid") or member.get("mid") or "")
            username = str(member.get("uname") or "?")
            is_bot = self._is_bot_speaker(actor_id, username, text)
            speaker = bot_name if is_bot else username
            prompt_lines.append(f"{speaker}: {text}")
            rows.append(
                {
                    "external_id": str(reply.get("rpid") or reply.get("id") or len(rows)),
                    "actor_id": actor_id,
                    "username": username,
                    "text": text,
                    "is_bot": is_bot,
                    "occurred_at": reply.get("ctime") or reply.get("timestamp"),
                    "raw": reply,
                }
            )
        if rows:
            from bilibot.memory_brain.ingestion import comment_thread_observation

            await self._archive_required(
                comment_thread_observation(
                    account_id=getattr(self, "account_id", "") or "default",
                    thread_key=str(thread_key),
                    rows=rows,
                    oid=str(oid),
                    comment_type=comment_type,
                    persona_id=self._get_current_persona_id(),
                )
            )
        return prompt_lines

    @staticmethod
    def _private_message_text(message: Dict[str, Any]) -> str:
        raw = message.get("content", "") if isinstance(message, dict) else ""
        if not raw:
            return ""
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            value = raw
        if isinstance(value, dict):
            return str(value.get("content") or "")
        return str(value or "")

    async def _archive_pm_recent_history(
        self,
        messages: List[Dict[str, Any]],
        *,
        current_message_id: str,
        talker_id: int,
        talker_name: str,
        my_uid: int,
    ) -> List[str]:
        from bilibot.services.pm_state_store import extract_platform_message_id

        turns: List[str] = []
        brain = getattr(self, "memory_brain", None)
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            text = self._private_message_text(message)
            if not text:
                continue
            message_id = extract_platform_message_id(message)
            if not message_id:
                digest = hashlib.sha256(
                    json.dumps(
                        message,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                message_id = f"history:{digest}"
            if str(message_id) == str(current_message_id):
                continue
            sender_id = str(message.get("sender_uid") or "")
            is_self = bool(my_uid and sender_id == str(my_uid))
            direction = "outgoing" if is_self else "incoming"
            safe = self._redact_private_message_runtime(
                text,
                actor_id="self" if is_self else str(talker_id),
                username=talker_name,
            )
            if brain is not None:
                _, result = await brain.archive_private_message(
                    platform_message_id=str(message_id),
                    text=text,
                    actor_id="self" if is_self else str(talker_id),
                    username=talker_name,
                    direction=direction,
                    persona_id=self._get_current_persona_id(),
                    redacted=safe,
                )
                if result is None or getattr(result, "source_committed", True) is False:
                    raise RuntimeError("PM history source commit was not confirmed")
            speaker = "我" if is_self else "私信用户"
            turns.append(f"{speaker}: {safe.text}")
        return self._bounded_recent_turns(turns)

    async def start(self):
        """启动调度器"""
        self.running = True
        logger.info("BiliBot 调度器启动")

        # 加载已回复记录
        self._load_replied_state()

        # PRD V3 §4.8：加载评论失败计数器（重启后恢复阈值保护）
        self._load_fail_counts()

        # V6 memories never expire or auto-prune. Durable jobs resume from SQLite.

        # PRD-V5 §7.2 / §6.3：启动时恢复未完成的 TaskRun
        # claimed/running → interrupted（重启前在运行，需按场景恢复）
        try:
            recovered = self.task_store.recover_interrupted()
            if recovered:
                logger.info(f"启动恢复：{recovered} 个 TaskRun 由 claimed/running 转为 interrupted")
            # 标记过期任务（超过 grace_window 的 scheduled）
            expired = self.task_store.expire_overdue()
            if expired:
                logger.info(f"启动过期清理：{expired} 个 TaskRun 标记为 expired")
        except Exception as e:
            logger.warning(f"TaskRun 启动恢复失败: {e}")

        # 先清理 video_temp 孤儿文件，再 spawn 恢复任务，避免误删/与新下载竞态
        try:
            from bilibot.video_understanding.cleanup import cleanup_orphaned_video_temp
            import os as _os
            video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
            cleaned = cleanup_orphaned_video_temp(video_temp_dir)
            if cleaned:
                logger.info(f"启动清理：{cleaned} 个 video_temp 孤儿文件/目录已清理")
        except Exception as e:
            logger.warning(f"启动清理 video_temp 失败: {e}")

        # Task 5：启动时恢复 interrupted 状态的 TaskRun
        # 按场景重新入队（retry → scheduled）或立即重跑（retry → claim → dispatch）
        try:
            interrupted = self.task_store.list_interrupted()
            if interrupted:
                logger.info(f"Task 5: 发现 {len(interrupted)} 个 interrupted TaskRun，开始恢复")
                for task in interrupted:
                    scene = task.scene
                    # dynamic_post is the canonical scene; keep "dynamic" for legacy rows.
                    if scene in ("proactive_video", "dynamic", "dynamic_post", "bangumi"):
                        # 立即重跑：retry → _dispatch_task_run（claim + scene/kind 分流统一）
                        if not self.task_store.retry(task.task_id):
                            logger.warning(f"Task 5: TaskRun {task.task_id} retry 失败")
                            continue
                        if not self._dispatch_task_run(
                            task.task_id,
                            tag_prefix="recovery",
                            claim_if_scheduled=True,
                        ):
                            logger.warning(
                                f"Task 5: TaskRun {task.task_id} dispatch 失败 scene={scene}"
                            )
                            continue
                        _recovery_bvid = ""
                        if scene == "proactive_video":
                            try:
                                _r_input = json.loads(task.input_json) if task.input_json else {}
                                _recovery_bvid = str(_r_input.get("bvid") or "")
                            except Exception:
                                pass
                        logger.info(
                            f"Task 5: interrupted TaskRun {task.task_id} 已派发（scene={scene}）"
                            + (f"，将优先重试 bvid={_recovery_bvid}" if _recovery_bvid else "")
                        )
                    else:
                        # 其他场景：重新入队（转 scheduled，由各自调度机制拾取）
                        if self.task_store.retry(task.task_id):
                            logger.info(f"Task 5: TaskRun {task.task_id} 重新入队（scene={scene}）")
        except Exception as e:
            logger.warning(f"Task 5: interrupted 恢复失败: {e}")

        # Task 4：启动时恢复卡在 publishing 状态的主动评论（崩溃前 mark_publishing 后未完成）
        try:
            stuck = self.proactive_comment_store.recover_stuck_publishing()
            if stuck:
                logger.warning(
                    f"Task 4: 启动恢复 {stuck} 个卡在 publishing 的主动评论 → result_unknown"
                )
        except Exception as e:
            logger.warning(f"Task 4: 启动恢复 publishing 卡死失败: {e}")

        # Task 42：启动时恢复卡住的动态草稿（approved 超期 / publishing 超阈值）
        try:
            stuck_drafts = self._get_draft_store().recover_stuck_drafts()
            if stuck_drafts.get("approved_expired") or stuck_drafts.get("publishing_stuck"):
                logger.warning(
                    f"Task 42: 启动恢复动态草稿: approved_expired={stuck_drafts.get('approved_expired', 0)}, "
                    f"publishing_stuck={stuck_drafts.get('publishing_stuck', 0)}"
                )
        except Exception as e:
            logger.warning(f"Task 42: 启动恢复动态草稿卡死失败: {e}", exc_info=True)

        # 生成今日调度计划
        self._generate_daily_schedule()
        self._schedule_date = datetime.now().date()

        # 跳过已过期的计划
        self._mark_overdue_as_triggered()

        # 检查登录状态
        config = self.config_loader.get_raw_config()
        if not self.bili or not config.get("bilibili", {}).get("sessdata"):
            logger.warning("B站未登录！请配置SESSDATA和bili_jct")

        # 获取 Bot 自身昵称（优先用配置，其次从 B站 API 获取）
        self._bot_name = config.get("personality", {}).get("bot_name", "") or ""
        self._bot_uid = str(config.get("bilibili", {}).get("dede_user_id", "") or "")
        # 任一字段缺失都尝试从 nav API 补全
        if (not self._bot_name or not self._bot_uid) and self.bili:
            try:
                nav = await self.bili.get_nav()
                if nav and nav.get("code") == 0:
                    data = nav.get("data", {})
                    self._bot_name = data.get("uname", "")
                    if not self._bot_uid:
                        self._bot_uid = str(data.get("mid", ""))
                    if self._bot_name:
                        logger.info(f"Bot 昵称: {self._bot_name} (UID: {self._bot_uid})")
                        # 回填到配置，让 personality 系统也能用
                        config.setdefault("personality", {})["bot_name"] = self._bot_name
            except Exception as e:
                logger.warning(f"获取 Bot 昵称失败: {e}")
        if not self._bot_name:
            self._bot_name = "Bot"
            logger.warning("未获取到 Bot 昵称，使用默认值 'Bot'")

        # 主循环
        while self.running:
            try:
                now = datetime.now()
                current_time = f"{now.hour:02d}:{now.minute:02d}"

                # 日期变更检测：跨天时重置调度计划（PRD 3.3）
                if self._schedule_date and now.date() != self._schedule_date:
                    logger.info(f"日期变更：{self._schedule_date} → {now.date()}，重新生成调度计划")
                    self._schedule_date = now.date()
                    self._proactive_triggered.clear()
                    self._dynamic_triggered.clear()
                    self._last_consolidation_date = None
                    self._generate_daily_schedule()
                    self._mark_overdue_as_triggered()

                # Cookie 自动刷新（对齐 AstrBot 插件：默认每 6 小时检查）
                # 间隔优先 features.cookie_check_interval_hours，
                # 其次 bilibili 段（账号级 ConfigLoader 会覆盖 bilibili 凭据字段）
                if self.bili is not None:
                    try:
                        raw_cfg = self.config_loader.get_raw_config() or {}
                        features = raw_cfg.get("features") or {}
                        bili_sec = raw_cfg.get("bilibili") or {}
                        interval_h = features.get("cookie_check_interval_hours")
                        if interval_h is None:
                            interval_h = bili_sec.get("cookie_check_interval_hours", 6)
                        interval_h = float(interval_h)
                    except Exception:
                        interval_h = 6.0
                    try:
                        ok, msg = await self.bili.maybe_refresh_cookie(
                            interval_hours=interval_h,
                        )
                        if not ok and msg not in ("skip", "未登录"):
                            logger.warning("Cookie 检查/刷新: %s", msg)
                    except Exception as e:
                        logger.warning(
                            "Cookie 自动刷新异常: %s", type(e).__name__,
                        )

                # 日终记忆清算（PRD 3.4，默认 03:00）
                consolidation_hour = 3
                try:
                    consolidation_hour = int(
                        self.config_loader.get_raw_config()
                        .get("memory", {})
                        .get("consolidation", {})
                        .get("hour", 3)
                    )
                except Exception:
                    pass
                if (
                    now.hour == consolidation_hour
                    and now.date() != self._last_consolidation_date
                ):
                    self._last_consolidation_date = now.date()
                    try:
                        brain = getattr(self, "memory_brain", None)
                        if brain:
                            await brain.run_jobs_until_idle(max_jobs=1000)
                            reflections = await brain.consolidate_recent(
                                now.date().isoformat()
                            )
                            logger.info("夜间记忆巩固完成：新增 %d 条反思", reflections)
                    except Exception as e:
                        logger.error("夜间记忆巩固失败: %s", type(e).__name__)

                # 番剧追番可能包含下载、视觉分析和多集观看，必须后台执行，
                # 否则会阻塞同账号的评论、私信、主动视频、动态和 companion tick。
                self._maybe_schedule_bangumi_check(now)

                # REP-602：恢复卡在中间态的评论（context_building/
                # generation_pending/safety_pending/publish_pending 超过 10 分钟
                # 未推进 → 转为 deferred，由后续 _process_retryable_comments 拾取）
                try:
                    self.reply_state_store.recover_stuck_intermediate(timeout_minutes=10)
                except Exception as e:
                    logger.warning(
                        f"REP-602: 恢复卡在中间态评论失败: {e}", exc_info=True
                    )

                # PM-501：恢复卡在中间态的私信（publish_pending 有 gen text → retry_wait）
                try:
                    stuck_pm = self.pm_state_store.recover_stuck_intermediate(
                        account_id=self.account_id, timeout_minutes=10,
                    )
                    if stuck_pm:
                        logger.warning(f"PM-501: 恢复 {stuck_pm} 条卡在中间态的私信")
                except Exception as e:
                    logger.warning(f"PM-501: 恢复卡在中间态私信失败: {e}", exc_info=True)

                # Task 4：周期性恢复卡在 publishing 状态的主动评论
                # （mark_publishing 后崩溃 → lease_until 超时 → result_unknown）
                try:
                    stuck = self.proactive_comment_store.recover_stuck_publishing()
                    if stuck:
                        logger.warning(
                            f"Task 4: 恢复 {stuck} 个卡在 publishing 的主动评论 → result_unknown"
                        )
                except Exception as e:
                    logger.warning(f"Task 4: 恢复 publishing 卡死失败: {e}")

                # Task 42：恢复卡住的动态草稿（approved 超期 / publishing 超阈值）
                try:
                    stuck_drafts = self._get_draft_store().recover_stuck_drafts()
                    if stuck_drafts.get("approved_expired") or stuck_drafts.get("publishing_stuck"):
                        logger.warning(
                            f"Task 42: 动态草稿恢复: approved_expired={stuck_drafts.get('approved_expired', 0)}, "
                            f"publishing_stuck={stuck_drafts.get('publishing_stuck', 0)}"
                        )
                except Exception as e:
                    logger.warning(f"Task 42: 动态草稿卡死恢复失败: {e}", exc_info=True)

                # 1. 检查评论回复
                await self._check_new_comments()

                # 1.2 PRD V4 REP-005：重试 deferred/retry_wait 的评论
                await self._process_retryable_comments()

                # 1.25 PRD-V5 §10.2 COM-501：重试 retry_wait 的主动评论
                await self._process_retryable_proactive_comments()

                # 1.3 Task 5：重试 retry_wait 的 TaskRun（主动视频/动态等到期自动重试）
                await self._process_retryable_tasks()

                # 1.5 检查私信
                await self._check_new_messages()

                # 1.6 PRD-V5 §6.3 / PM-501：重试 retry_wait 的私信（独立退避）
                await self._process_retryable_pms()

                # 2. 主动行为
                await self._check_proactive_tasks(current_time)

                # 2.5 陪伴生活层 tick（日程/日记/探索/创作；默认关闭）
                if self.companion is not None and getattr(self.companion, "enabled", False):
                    try:
                        # 保持 web_search / draft 引用新鲜（热重载后可能换实例）
                        if getattr(self.companion, "web_search", None) is None:
                            self.companion.web_search = self.web_search
                        if getattr(self.companion, "draft_store", None) is None:
                            try:
                                self.companion.draft_store = self.get_draft_store()
                            except Exception:
                                pass
                        c_result = await self.companion.tick(now)
                        actions = (c_result or {}).get("actions") or []
                        if actions:
                            logger.info(
                                "[%s] companion tick: %s",
                                self.account_id or "-",
                                ",".join(actions),
                            )
                    except Exception as e:
                        logger.warning(
                            "companion tick 失败: %s", type(e).__name__, exc_info=True
                        )

                # 3. 周总结
                await self._check_weekly_summary()

                # 4. 每分钟检查一次
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                logger.info("调度器被取消")
                break
            except Exception as e:
                logger.error(f"主循环异常: {e}", exc_info=True)
                await asyncio.sleep(60)

        # 清理资源
        await self.cleanup()

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
        """检查 B站通知中心的新评论"""
        if not self.reply_gen:
            logger.info("reply_gen 未初始化，跳过评论检查")
            return
        if not self.bili:
            logger.info("bili API 未初始化，跳过评论检查")
            return
        if not self._authenticated_poll_allowed():
            return

        # PRD §5.9：全局暂停 / 账号风险暂停 / 无 checker → fail-closed 跳过自动回复
        if self.safety_checker is None:
            logger.error("safety_checker 未初始化，跳过评论检查（fail-closed）")
            return
        if (
            self.safety_checker.is_paused()
            or self.safety_checker.is_account_paused(self.account_id)
        ):
            logger.info(
                "跳过评论检查（全局暂停=%s, 账号暂停=%s）",
                self.safety_checker.is_paused(),
                self.safety_checker.is_account_paused(self.account_id),
            )
            return

        config = self.config_loader.get_raw_config()
        # PRD V4 REP-002 / CFG-003：统一使用 features.reply_comment
        # 旧 reply.auto_reply 已迁移，运行时不再消费
        features = config.get("features", {})
        if not features.get("reply_comment", True):
            logger.info("features.reply_comment=false，跳过评论检查")
            return

        # S3：_bot_uid 为空 fail-closed，禁止自动回复（避免无法识别自己导致自回）
        if not str(getattr(self, "_bot_uid", "") or "").strip():
            logger.error(
                "_bot_uid 为空，跳过自动评论回复（fail-closed，防止自回）"
            )
            return

        try:
            items = []
            notifications = await self.bili.get_reply_notifications()
            if not notifications:
                logger.info("通知 API 返回空")
            elif notifications.get("code") != 0:
                logger.info(f"通知 API 返回错误: code={notifications.get('code')} msg={notifications.get('message')}")
            else:
                items = notifications.get("data", {}).get("items", []) or []
                # PRD V6：通知数变化时才打 INFO，否则降级 DEBUG
                _count = len(items)
                if _count != self._last_notify_count:
                    logger.info(f"通知 API 返回 {_count} 条评论")
                    self._last_notify_count = _count
                else:
                    logger.debug(f"通知 API 返回 {_count} 条评论（无变化）")

            # 合并 @我的 通知（结构与 reply 一致，标记 _source="at" 以便后续区分）
            try:
                at_notifications = await self.bili.get_at_notifications()
                if at_notifications and at_notifications.get("code") == 0:
                    at_items = at_notifications.get("data", {}).get("items", []) or []
                    if at_items:
                        # 标记 at 来源，后续处理时优先看视频
                        for at_item in at_items:
                            at_item["_source"] = "at"
                        # 去重：与 reply 通知按 source_id 去重
                        existing_ids = {
                            str((it.get("item") or {}).get("source_id") or it.get("id") or "")
                            for it in items
                        }
                        for at_item in at_items:
                            at_id = str((at_item.get("item") or {}).get("source_id") or at_item.get("id") or "")
                            if at_id and at_id not in existing_ids:
                                items.append(at_item)
                                existing_ids.add(at_id)
                        # 与 reply 通知一致：数量无变化时降级 DEBUG，避免主循环每轮刷屏
                        _at_count = len(at_items)
                        _merged = len(items)
                        if (
                            _at_count != self._last_at_notify_count
                            or _merged != self._last_at_merged_count
                        ):
                            logger.info(
                                f"@我的 通知返回 {_at_count} 条，合并后候选共 {_merged} 条"
                            )
                            self._last_at_notify_count = _at_count
                            self._last_at_merged_count = _merged
                        else:
                            logger.debug(
                                f"@我的 通知返回 {_at_count} 条，合并后候选共 {_merged} 条（无变化）"
                            )
            except Exception as at_exc:
                logger.warning(f"获取@我的通知失败: {at_exc}")

            batch_size = config.get("reply", {}).get("batch_size", 10)
            try:
                own_dynamic_items = await self._collect_own_dynamic_comment_items(
                    config=config,
                    limit=batch_size,
                )
                if own_dynamic_items:
                    existing_ids = {
                        str((it.get("item") or {}).get("source_id") or it.get("id") or "")
                        for it in items
                    }
                    for own_item in own_dynamic_items:
                        own_id = str((own_item.get("item") or {}).get("source_id") or own_item.get("id") or "")
                        if own_id and own_id not in existing_ids:
                            items.append(own_item)
                            existing_ids.add(own_id)
                    _own_count = len(own_dynamic_items)
                    # PRD V6：候选数变化时才打 INFO
                    if _own_count != self._last_own_dynamic_count:
                        logger.info(f"自动态补扫发现 {_own_count} 条候选评论")
                        self._last_own_dynamic_count = _own_count
                    else:
                        logger.debug(f"自动态补扫发现 {_own_count} 条候选评论（无变化）")
            except Exception as own_exc:
                logger.warning(f"自动态评论补扫失败: {own_exc}")

            if not items:
                logger.info("评论区无新通知")
                return

            new_items = []
            for item in items:
                # source_id 是触发通知的评论 rpid（用户的评论），用于去重
                item_detail = item.get("item", {})
                source_id = item_detail.get("source_id", 0)
                comment_type = item_detail.get("business_id", 1)
                # PRD 4.6：统一转 str，避免 int/str 类型不一致导致去重失败
                rpid = str(source_id or item.get("id") or "")
                # PRD V4 REP-001 / REP-601：使用 is_processed 跳过任何已有记录的评论
                # （终态 + 中间态 + deferred/retry_wait），防止进行中的评论被重复拉入队列。
                # deferred 与 retry_wait 由 _process_retryable_comments 单独处理。
                if rpid and not self.reply_state_store.is_processed(comment_type, rpid):
                    new_items.append(item)
            new_items = new_items[:batch_size]

            if not new_items:
                # PRD V6：全部已回复时用指纹（数量+rpid 集合）判断是否变化
                _fp = f"{len(items)}:" + ",".join(sorted(
                    str((it.get("item") or {}).get("source_id") or it.get("id") or "")
                    for it in items
                ))
                if _fp != self._last_all_replied_fingerprint:
                    logger.info(f"评论通知 {len(items)} 条，全部已回复过，跳过")
                    self._last_all_replied_fingerprint = _fp
                else:
                    logger.debug(f"评论通知 {len(items)} 条，全部已回复过，跳过（无变化）")
                return

            # 有新评论待回复时重置指纹，下次全回复时会重新打 INFO
            self._last_all_replied_fingerprint = None
            logger.info(f"发现 {len(new_items)} 条新评论待回复")

            for item in new_items:
                try:
                    item_detail = item.get("item", {})
                    oid = item_detail.get("subject_id", 0)
                    comment_type = item_detail.get("business_id", 1)
                    # source_id 是触发通知的评论 rpid（用户的评论），root_id 是根评论 rpid
                    root_id = item_detail.get("root_id", 0)
                    source_id = item_detail.get("source_id", 0)
                    # reply_id 用于去重，使用 source_id（用户的评论 rpid）
                    # PRD 4.6：统一转 str
                    reply_id = str(source_id or item.get("id") or "")
                    user = item.get("user", {})
                    user_id = str(user.get("mid", ""))
                    username = user.get("nickname", "未知用户")
                    comment_text = item_detail.get("source_content", "")

                    # 调试日志
                    logger.info(f"通知字段: reply_id={reply_id}, oid={oid}, type={comment_type}, "
                                f"root_id={root_id}, source_id={source_id}, username={username}")

                    if not comment_text:
                        # S5：空 source_content 不得 silent skip，记 ignored 终态
                        logger.info(
                            "评论 source_content 为空，标记 ignored: reply_id=%s",
                            reply_id,
                        )
                        if reply_id:
                            self.reply_state_store.mark_ignored(
                                comment_type,
                                reply_id,
                                rule="empty_source_content",
                                notification=item,
                            )
                        continue

                    # V6: every observed comment is archived before reply filters.
                    try:
                        from bilibot.memory_brain.ingestion import comment_observation

                        await self._archive_required(
                            comment_observation(
                                account_id=self.account_id or "default",
                                comment_type=comment_type,
                                reply_id=reply_id,
                                text=comment_text,
                                actor_id=user_id,
                                username=username,
                                oid=str(oid),
                                title=self._comment_memory_title(username, comment_text, reply_id),
                                context=item_detail.get("target_context") or {},
                                persona_id=self._get_current_persona_id(),
                            )
                        )
                    except Exception as archive_exc:
                        self.reply_state_store.mark_deferred(
                            comment_type,
                            reply_id,
                            reason="memory_archive_failed",
                            error_code="MEMORY_ARCHIVE_FAILED",
                            increment_attempt=False,
                        )
                        continue

                    # PRD V4 REP-002：过滤规则真实生效
                    reply_cfg = config.get("reply", {})
                    features = config.get("features", {})

                    # 过短评论 → ignored（终态）
                    min_len = int(reply_cfg.get("min_comment_length", 2))
                    if len(comment_text.strip()) < min_len:
                        logger.info(f"评论过短(<{min_len})，忽略: {comment_text[:20]}")
                        self.reply_state_store.mark_ignored(
                            comment_type, reply_id,
                            rule=f"min_comment_length({min_len})",
                            notification=item,
                        )
                        continue

                    # 自己的评论 → ignored（终态）
                    reply_own = reply_cfg.get("reply_own", False)
                    if not reply_own and self._bot_uid and user_id == str(self._bot_uid):
                        logger.info(f"自己的评论，reply_own=false，忽略")
                        self.reply_state_store.mark_ignored(
                            comment_type, reply_id,
                            rule="reply_own=false",
                            notification=item,
                        )
                        continue

                    # 输入黑名单 → ignored（终态）
                    block_keywords = reply_cfg.get("block_keywords", [])
                    # REP-605：兼容字符串配置，避免按字符迭代
                    if isinstance(block_keywords, str):
                        block_keywords = [block_keywords]
                    if block_keywords and isinstance(block_keywords, list):
                        _blocked = False
                        for kw in block_keywords:
                            if kw and kw in comment_text:
                                logger.info(f"评论命中黑名单关键词 '{kw}'，忽略")
                                self.reply_state_store.mark_ignored(
                                    comment_type, reply_id,
                                    rule=f"block_keyword:{kw}",
                                    notification=item,
                                )
                                _blocked = True
                                break
                        if _blocked:
                            continue

                    # 记录 discovered 状态（含原始通知，供后续审计）
                    self.reply_state_store.upsert(
                        comment_type, reply_id, "context_building",
                        notification=item, persona_id=self._get_current_persona_id(),
                    )

                    # 获取评论上下文（楼中楼对话历史）
                    comment_context = ""
                    reply_replies = []
                    context_root = root_id if root_id else source_id
                    if root_id or source_id:
                        try:
                            replies_data = await self.bili.get_comment_replies(
                                oid=oid, root=context_root, comment_type=comment_type, ps=30,
                            )
                            if replies_data and replies_data.get("code") == 0:
                                reply_replies = replies_data.get("data", {}).get("replies", [])
                        except Exception as e:
                            logger.warning(f"获取评论上下文失败: {e}")
                    # 通知自带的上下文字段：根评论 + 被回复评论（楼中楼接口不返回这些）
                    # 这对 @ 通知尤其重要：bot 需要知道 @ 发生在什么对话语境下
                    _notify_context_lines = []
                    root_reply_content = (item_detail.get("root_reply_content") or "").strip()
                    if root_reply_content:
                        _notify_context_lines.append(f"[根评论] {root_reply_content}")
                    target_reply_content = (item_detail.get("target_reply_content") or "").strip()
                    if target_reply_content:
                        _notify_context_lines.append(f"[被回复的评论] {target_reply_content}")
                    if _notify_context_lines:
                        if comment_context:
                            comment_context = "\n".join(_notify_context_lines) + "\n" + comment_context
                        else:
                            comment_context = "\n".join(_notify_context_lines)
                        logger.info(f"通知自带上下文: 根评论={'有' if root_reply_content else '无'}, 被回复={'有' if target_reply_content else '无'}")
                    if reply_replies:
                        try:
                            context_lines = await self._archive_comment_thread_context(
                                reply_replies,
                                oid=oid,
                                comment_type=comment_type,
                                thread_key=context_root,
                            )
                        except Exception:
                            self.reply_state_store.mark_deferred(
                                comment_type,
                                reply_id,
                                reason="comment_context_archive_failed",
                                error_code="MEMORY_ARCHIVE_FAILED",
                                increment_attempt=False,
                            )
                            continue
                        if context_lines:
                            if comment_context:
                                comment_context = comment_context + "\n" + "\n".join(context_lines)
                            else:
                                comment_context = "\n".join(context_lines)
                            logger.info(f"获取评论上下文: {len(context_lines)} 条对话")

                    # PRD §5.9：黑名单过滤 → ignored（终态）
                    if self.safety_checker is not None and self.safety_checker.is_blacklisted(user_id):
                        logger.info(f"用户 {username}({user_id}) 在黑名单中，跳过回复")
                        self.reply_state_store.mark_ignored(
                            comment_type, reply_id, rule="blacklist",
                        )
                        continue

                    # @来源的视频评论：先看视频再回复，让回复基于真实观看内容
                    if item.get("_source") == "at" and comment_type == 1 and oid:
                        try:
                            watched = await self._watch_video_for_reply(bvid="", oid=oid)
                            if watched:
                                logger.info(f"@回复：已预观看视频 oid={oid}，回复将基于完整视频上下文")
                            else:
                                logger.info(f"@回复：预观看失败/降级，回复将基于元数据")
                        except Exception as watch_exc:
                            logger.warning(f"@回复：预观看异常，降级为元数据回复: {watch_exc}")

                    # PRD V4 §4.3.1：构建完整 ReplyContext
                    reply_context = None
                    if self.comment_context_service is not None:
                        try:
                            reply_context = await self.comment_context_service.build_context(
                                notification=item,
                                current_user_id=user_id,
                                persona_id=self._get_current_persona_id(),
                                recent_turns=self._bounded_recent_turns(
                                    comment_context.splitlines()
                                ),
                            )
                        except Exception as e:
                            from bilibot.services.comment_context import ContextArchiveError

                            if isinstance(e, ContextArchiveError):
                                self._pause_for_memory_failure()
                                self.reply_state_store.mark_deferred(
                                    comment_type,
                                    reply_id,
                                    reason="video_context_archive_failed",
                                    error_code="MEMORY_ARCHIVE_FAILED",
                                    increment_attempt=False,
                                )
                                continue
                            logger.warning(f"构建评论上下文失败，降级处理: {e}")
                            reply_context = None

                    # PRD V4 §9.1：状态 → generation_pending
                    self.reply_state_store.upsert(comment_type, reply_id, "generation_pending")

                    # 生成回复（传入 reply_context + 评论上下文）
                    # BUG A-001：直接调用 _generate_reply_impl 以获取 GenerationOutcome，
                    # 不再通过 generate_reply 包装器（它把 skip/retryable/permanent 全折叠成 None）
                    try:
                        outcome = await self.reply_gen._generate_reply_impl(
                            user_id=user_id,
                            username=username,
                            comment=comment_text,
                            thread_id=str(reply_id),
                            oid=oid,
                            comment_type=comment_type,
                            reply_context=reply_context,
                            comment_context=comment_context,
                        )
                    except Exception as llm_err:
                        # PRD V4 §9.1：LLM 失败 → deferred（非终态，可恢复）
                        logger.error(f"LLM 生成失败，deferred: {llm_err}")
                        _err_name = type(llm_err).__name__
                        _no_burn = (
                            _err_name == "RateLimitExhaustedError"
                            or "rate limit" in str(llm_err).lower()
                            or "rate-limited" in str(llm_err).lower()
                        )
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason=f"llm_error: {llm_err}",
                            error_code="LLM_RATE_LIMITED" if _no_burn else "LLM_ERROR",
                            increment_attempt=not _no_burn,
                        )
                        continue

                    # BUG A-001：按 outcome.status 分派，不再用 None 判断
                    if outcome.is_skip:
                        # LLM 明确决定不回复 → ignored（终态）
                        logger.debug(f"LLM 决定跳过回复 from {username}")
                        self.reply_state_store.mark_ignored(
                            comment_type, reply_id, rule="llm_no_reply",
                        )
                        continue

                    if not outcome.is_generated:
                        # 永久错误（如 LLM 未配置）→ failed 终态，禁止 deferred 空转
                        if getattr(outcome, "is_permanent_error", False):
                            logger.error(
                                f"LLM 永久失败 (code={outcome.error_code})，标记 failed"
                            )
                            self.reply_state_store.mark_failed(
                                comment_type, reply_id,
                                reason=f"generation_permanent: {outcome.error_code}",
                                error_code=outcome.error_code or "GEN_PERMANENT",
                            )
                            continue
                        logger.warning(
                            f"LLM 生成未成功 (status={outcome.status}, code={outcome.error_code})，deferred"
                        )
                        _gen_code = outcome.error_code or "GEN_FAILED"
                        # 429 / 全 key 冷却：条件失败，不烧 attempt（与 RATE_LIMIT 一致）
                        _no_burn = _gen_code in (
                            "LLM_RATE_LIMITED",
                            "RATE_LIMIT",
                            "RATE_LIMITED",
                        )
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason=f"generation_{outcome.status}: {outcome.error_code}",
                            error_code=_gen_code,
                            increment_attempt=not _no_burn,
                        )
                        continue

                    reply_text = outcome.text
                    audit_id = outcome.audit_id
                    context_meta = outcome.context_meta or {}

                    # PRD-V5 §6.2 / REP-502：获取有效文本后、安全检查之前持久化生成结果
                    # 确保即使安全检查或发布失败，重试时也能使用原始文本而非重新生成
                    _gen_persona_id = (
                        context_meta.get("persona_id")
                        or self._get_current_persona_id()
                        or ""
                    )
                    self.reply_state_store.save_generation_result(
                        comment_type, reply_id,
                        text=reply_text,
                        persona_id=_gen_persona_id,
                        audit_id=audit_id,
                    )

                    # PRD V4 §9.1：状态 → safety_pending
                    self.reply_state_store.upsert(comment_type, reply_id, "safety_pending")

                    # PRD §5.9：发布前内容检查 + 频率限制（fail-closed：无 checker 禁止发布）
                    if self.safety_checker is None:
                        logger.error(
                            "safety_checker 未初始化，拒绝发布评论（fail-closed）: reply_id=%s",
                            reply_id,
                        )
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason="safety_checker_missing", error_code="NO_SAFETY_CHECKER",
                            increment_attempt=False,
                        )
                        continue

                    persona_id_for_check = self._get_current_persona_id()
                    rate_reserved = False
                    try:
                        passed, reason = await self.safety_checker.check_content(
                            reply_text, scene="reply_comment",
                            persona_id=persona_id_for_check,
                            account_id=self.account_id,
                        )
                        if not passed:
                            # PRD V4 §9.1：安全检查未通过 → rejected（终态）
                            logger.warning(f"回复内容安全检查未通过: {reason}")
                            if audit_id and self.audit_store:
                                try:
                                    self.audit_store.mark_published(
                                        audit_id, published=False,
                                        target={
                                            "kind": "reply_comment",
                                            "rpid": str(reply_id),
                                            "source_rpid": str(reply_id),
                                            "comment_type": int(comment_type),
                                            "account_id": self.account_id or "",
                                        },
                                        failure_reason=f"safety_check: {reason}",
                                    )
                                except Exception:
                                    pass
                            self.reply_state_store.mark_rejected(
                                comment_type, reply_id, reason=f"safety_check: {reason}",
                            )
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{comment_type}:{reply_id}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="rejected",
                                title=f"回复评论 {reply_id}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": reply_id,
                                    "oid": str(oid),
                                    "reason_code": "SAFETY_REJECTED",
                                },
                            )
                            continue
                        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                            scene="reply_comment", account_id=self.account_id,
                        )
                        if not rate_ok:
                            logger.warning("评论发布频率限制触发，deferred: %s", rate_reason)
                            self.reply_state_store.mark_deferred(
                                comment_type, reply_id,
                                reason="rate_limited", error_code="RATE_LIMIT",
                                increment_attempt=False,
                            )
                            continue
                        rate_reserved = True
                    except Exception as e:
                        # PRD V4 §9.1：安全检查异常 → deferred（非终态，可恢复）
                        logger.error(f"安全检查异常，deferred: {e}", exc_info=True)
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason=f"safety_exception: {e}", error_code="SAFETY_ERROR",
                            increment_attempt=False,
                        )
                        continue

                    # PRD V4 §9.1：状态 → publish_pending
                    self.reply_state_store.upsert(comment_type, reply_id, "publish_pending")

                    # 发表回复
                    # root = 根评论rpid（一级评论时=source_id，二级评论时=root_id）
                    # parent = 要回复的那条评论rpid（始终=source_id，即用户的评论）
                    comment_root = root_id if root_id else source_id
                    try:
                        success = await self.bili.post_comment(
                            oid=oid,
                            content=reply_text,
                            comment_type=comment_type,
                            rpid=comment_root,
                            parent=source_id,
                        )
                    except Exception as post_err:
                        # Exception after optional raise paths: treat as uncertain (no auto-retry, no refund)
                        logger.error(f"发表评论异常 reply_id={reply_id}: {post_err}")
                        self.reply_state_store.mark_result_unknown(
                            comment_type, reply_id,
                            reason=f"post_exception: {type(post_err).__name__}",
                            error_code="POST_EXCEPTION",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{comment_type}:{reply_id}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="result_unknown",
                                title=f"回复评论 {reply_id}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": reply_id,
                                    "oid": str(oid),
                                    "reason_code": "POST_EXCEPTION",
                                },
                            )
                        except Exception:
                            logger.error(
                                "unknown comment result could not be archived: reply_id=%s",
                                reply_id,
                            )
                        continue
                    if success is None:
                        # Transport uncertainty (timeout/5xx/non-json): no refund, no auto-retry
                        logger.error(
                            "评论结果不确定（不自动重发）: reply_id=%s", reply_id,
                        )
                        self.reply_state_store.mark_result_unknown(
                            comment_type, reply_id,
                            reason="post_comment transport uncertainty",
                            error_code="RESULT_UNKNOWN",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{comment_type}:{reply_id}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="result_unknown",
                                title=f"回复评论 {reply_id}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": reply_id,
                                    "oid": str(oid),
                                    "reason_code": "RESULT_UNKNOWN",
                                },
                            )
                        except Exception:
                            logger.error(
                                "unknown comment result could not be archived: reply_id=%s",
                                reply_id,
                            )
                        continue
                    if success is False:
                        self._check_bili_risk_control("reply_comment")
                        if rate_reserved and self.safety_checker is not None:
                            try:
                                self.safety_checker.refund_publish(
                                    scene="reply_comment", account_id=self.account_id,
                                )
                            except Exception:
                                pass

                    # PRD §5.9：发布成功后记录内容（频率已在预占时记录）
                    if success and self.safety_checker is not None:
                        try:
                            self.safety_checker.record_content(reply_text, account_id=self.account_id)
                        except Exception:
                            pass

                    # PRD V4 §4.5.3：发布结果同步到 audit
                    if audit_id and self.audit_store:
                        try:
                            if success:
                                self.audit_store.mark_published(
                                    audit_id, published=True,
                                    target={
                                        "kind": "reply_comment",
                                        "rpid": str(reply_id),
                                        "source_rpid": str(reply_id),
                                        "comment_type": int(comment_type),
                                        "oid": str(oid),
                                        "account_id": self.account_id or "",
                                        "published_at": datetime.now().isoformat(),
                                    },
                                )
                            else:
                                self.audit_store.mark_published(
                                    audit_id, published=False,
                                    target={
                                        "kind": "reply_comment",
                                        "rpid": str(reply_id),
                                        "source_rpid": str(reply_id),
                                        "comment_type": int(comment_type),
                                        "account_id": self.account_id or "",
                                    },
                                    failure_reason="bili.post_comment 返回 False",
                                )
                        except Exception as e:
                            logger.debug(f"audit mark_published 失败: {e}")

                    if success:
                        logger.info(f"回复成功 -> {username}")
                        # PRD V4 REP-001：标记终态 published
                        self.reply_state_store.mark_published(comment_type, reply_id)

                        try:
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{comment_type}:{reply_id}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=True,
                                title=f"已回复评论 {reply_id}",
                                scene="reply_comment",
                                metadata={"reply_id": reply_id, "oid": str(oid)},
                            )
                        except Exception:
                            # The platform action already happened. Pause subsequent
                            # actions and leave the durable intent as evidence.
                            logger.error(
                                "published comment result could not be archived: reply_id=%s",
                                reply_id,
                            )
                        else:
                            self._notify_companion_comment_replied(
                                title=str(oid or "")[:40],
                                preview=str(reply_text or "")[:80],
                                proactive=False,
                            )

                        # PRD V4 REP-006：好感度仅在发布成功后应用
                        features = config.get("features", {})
                        if features.get("affection", False) and self.knowledge_memory:
                            try:
                                if hasattr(self.knowledge_memory, 'update_user_affection'):
                                    self.knowledge_memory.update_user_affection(user_id, delta=1)
                            except Exception as aff_err:
                                logger.warning(f"好感度更新失败（不影响回复）: {aff_err}")
                    else:
                        # PRD V4 REP-005：发布失败 → retry_wait（非终态，指数退避重试）
                        logger.warning(f"回复发表失败，进入 retry_wait: {reply_text[:30]}...")
                        self.reply_state_store.mark_retry_wait(
                            comment_type, reply_id,
                            reason="bili.post_comment returned False",
                            error_code="PUBLISH_FAILED",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{comment_type}:{reply_id}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                status="failed",
                                title=f"回复评论 {reply_id}",
                                scene="reply_comment",
                                metadata={
                                    "reply_id": reply_id,
                                    "oid": str(oid),
                                    "reason_code": "PUBLISH_FAILED",
                                },
                            )
                        except Exception:
                            logger.error(
                                "failed comment result could not be archived: reply_id=%s",
                                reply_id,
                            )

                    await asyncio.sleep(2)

                except Exception as e:
                    # S4：单条处理失败时若已知 comment_type/reply_id，记 deferred 可恢复
                    logger.error(f"处理评论失败: {e}")
                    try:
                        _ct = int(locals().get("comment_type") or 0) or int(
                            (locals().get("item_detail") or {}).get("business_id", 0) or 0
                        )
                        _rid = str(locals().get("reply_id") or "")
                        if not _rid:
                            _sid = (locals().get("item_detail") or {}).get("source_id", 0)
                            _rid = str(_sid or (locals().get("item") or {}).get("id") or "")
                        if _rid:
                            self.reply_state_store.mark_deferred(
                                _ct or 1,
                                _rid,
                                reason=f"unhandled: {type(e).__name__}: {e}",
                                error_code="UNHANDLED",
                                increment_attempt=False,
                            )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"检查评论失败: {e}")

    async def _collect_own_dynamic_comment_items(self, config: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
        """补扫 Bot 自己动态下的评论，并转换成通知中心 item 形状。

        B站通知中心有时不会把“别人评论了我的动态”稳定返回到
        /x/msgfeed/reply，尤其是新动态接口发布的内容。这里只拉取最近几条
        自己动态的评论区，后续仍复用 _check_new_comments 的归档、过滤、
        生成、安全和发布流程。
        """
        if not self.bili or not hasattr(self.bili, "get_user_dynamics"):
            return []
        if not hasattr(self.bili, "get_replies"):
            return []

        reply_cfg = config.get("reply", {}) if isinstance(config, dict) else {}
        if reply_cfg.get("poll_own_dynamics", True) is False:
            return []

        bot_uid = self._bot_uid
        if not bot_uid:
            try:
                bot_uid = getattr(self.bili.config.bilibili, "dede_user_id", 0)
            except Exception:
                bot_uid = 0
        try:
            bot_uid_int = int(bot_uid or 0)
        except Exception:
            bot_uid_int = 0
        if bot_uid_int <= 0:
            return []

        dynamics_limit = int(reply_cfg.get("own_dynamic_poll_limit", 5) or 5)
        replies_ps = int(reply_cfg.get("own_dynamic_reply_ps", 20) or 20)
        dynamics_data = await self.bili.get_user_dynamics(bot_uid_int, limit=dynamics_limit)
        if not dynamics_data or dynamics_data.get("code") != 0:
            return []

        cards = (dynamics_data.get("data") or {}).get("items") or []
        if not cards and (dynamics_data.get("data") or {}).get("cards"):
            cards = (dynamics_data.get("data") or {}).get("cards") or []

        found: List[Dict[str, Any]] = []
        for card in cards[:dynamics_limit]:
            oid, comment_type = self._extract_dynamic_comment_target(card)
            if not oid:
                continue
            replies_data = await self.bili.get_replies(
                oid=int(oid),
                comment_type=int(comment_type or 17),
                pn=1,
                ps=replies_ps,
                sort=0,
            )
            if not replies_data or replies_data.get("code") != 0:
                continue
            replies = (replies_data.get("data") or {}).get("replies") or []
            target_context = self._dynamic_card_context(card)
            for reply in replies:
                found.extend(
                    self._reply_to_notification_items(
                        reply,
                        oid,
                        int(comment_type or 17),
                        root_id=0,
                        target_context=target_context,
                    )
                )
                if len(found) >= limit:
                    return found[:limit]
        return found[:limit]

    def _extract_dynamic_comment_target(self, card: Dict[str, Any]) -> Tuple[int, int]:
        """Return (comment_oid, comment_type) for a dynamic feed card."""
        basic = card.get("basic") or {}
        oid = (
            basic.get("comment_id_str")
            or basic.get("comment_id")
            or basic.get("rid_str")
            or basic.get("rid")
        )
        comment_type = basic.get("comment_type") or 17
        if oid:
            try:
                return int(oid), int(comment_type or 17)
            except Exception:
                return 0, 17

        desc = card.get("desc") or {}
        oid = (
            desc.get("dynamic_id")
            or desc.get("dynamic_id_str")
            or desc.get("rid")
            or desc.get("rid_str")
        )
        comment_type = desc.get("type") or 17
        if oid:
            try:
                return int(oid), int(comment_type or 17)
            except Exception:
                return 0, 17
        return 0, 17

    def _reply_to_notification_items(
        self,
        reply: Dict[str, Any],
        oid: int,
        comment_type: int,
        *,
        root_id: int = 0,
        target_context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Convert a B站 reply object and its floor replies to notification-like items."""
        items: List[Dict[str, Any]] = []

        def _one(reply_obj: Dict[str, Any], root: int) -> Optional[Dict[str, Any]]:
            rpid = reply_obj.get("rpid") or reply_obj.get("rpid_str") or reply_obj.get("id") or 0
            try:
                rpid_int = int(rpid)
            except Exception:
                rpid_int = 0
            if not rpid_int:
                return None
            content = (reply_obj.get("content") or {}).get("message") or ""
            if not content:
                return None
            member = reply_obj.get("member") or {}
            mid = str(member.get("mid") or "")
            uname = member.get("uname") or member.get("name") or "未知用户"
            if self._is_bot_speaker(mid, uname, content):
                return None
            return {
                "id": str(rpid_int),
                "user": {"mid": mid, "nickname": uname},
                "item": {
                    "subject_id": int(oid),
                    "business_id": int(comment_type),
                    "root_id": int(root or 0),
                    "source_id": int(rpid_int),
                    "source_content": content,
                    "target_context": dict(target_context or {}),
                },
                "source": "own_dynamic_poll",
            }

        top = _one(reply, root_id)
        if top:
            items.append(top)
        top_rpid = int((reply.get("rpid") or reply.get("rpid_str") or 0) or 0)
        for child in reply.get("replies") or []:
            child_item = _one(child, top_rpid)
            if child_item:
                items.append(child_item)
        return items

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
        """PRD V4 REP-005：重试 deferred/retry_wait 状态的评论

        - retry_wait：直接重新发布（已有生成结果）
        - deferred：重新走完整流程（LLM 可能已恢复）
        - 重试前查询本地终态，避免重复回复
        """
        if not self.bili or not self.reply_gen:
            return
        # S3：_bot_uid 为空 fail-closed，禁止重试发布（避免无法做幂等/自回识别）
        if not str(getattr(self, "_bot_uid", "") or "").strip():
            logger.error(
                "_bot_uid 为空，跳过评论重试（fail-closed，防止自回/幂等失效）"
            )
            return
        try:
            retryable = self.reply_state_store.get_retryable()
            if not retryable:
                return
            logger.info(f"发现 {len(retryable)} 条待重试评论")
            for item_state in retryable:
                ct = int(item_state.get("comment_type", 1))
                rpid = str(item_state.get("source_rpid", ""))
                state = item_state.get("state", "")
                if not rpid:
                    continue
                # PRD REP-005：重试前查询本地终态，避免重复回复
                if self.reply_state_store.is_terminal(ct, rpid):
                    continue
                # get_retryable 安全网可能返回卡在中间态的行；处理器只处理
                # retry_wait/deferred。有 generation_result 时转 retry_wait 复用原文
                # （与 recover_stuck_intermediate 一致），避免 deferred 重生导致双发。
                if state not in ("retry_wait", "deferred"):
                    gen_existing = (item_state.get("generation_result") or "").strip()
                    if gen_existing and state == "publish_pending":
                        logger.warning(
                            "retryable 中间态 %s 已有生成文本，转为 retry_wait: rpid=%s",
                            state, rpid,
                        )
                        self.reply_state_store.mark_retry_wait(
                            ct, rpid,
                            reason=f"normalize_from_{state}_keep_gen",
                            error_code="STUCK_NORMALIZE_RETRY",
                            increment_attempt=False,
                        )
                        state = "retry_wait"
                        item_state["state"] = "retry_wait"
                    else:
                        logger.warning(
                            "retryable 含未处理中间态 %s，转为 deferred: rpid=%s",
                            state, rpid,
                        )
                        self.reply_state_store.mark_deferred(
                            ct, rpid,
                            reason=f"normalize_from_{state}",
                            error_code="STUCK_NORMALIZE",
                            increment_attempt=False,
                        )
                        state = "deferred"
                        item_state["state"] = "deferred"
                try:
                    # 从状态记录恢复原始通知
                    notif_json = item_state.get("notification_json", "")
                    notif = json.loads(notif_json) if notif_json else None
                    if not notif:
                        # 无原始通知，无法重试，标记失败
                        self.reply_state_store.upsert(ct, rpid, "failed",
                                                      error="no_notification_for_retry")
                        continue

                    item_detail = notif.get("item", {})
                    oid = item_detail.get("subject_id", 0)
                    root_id = item_detail.get("root_id", 0)
                    source_id = item_detail.get("source_id", 0)
                    comment_text = item_detail.get("source_content", "")
                    user = notif.get("user", {})
                    user_id = str(user.get("mid", ""))
                    username = user.get("nickname", "未知用户")

                    if state == "retry_wait":
                        # PRD-V5 §6.2 / REP-502：使用原始生成文本重试，不重新生成
                        gen_result = item_state.get("generation_result", "")
                        gen_hash = item_state.get("generation_hash", "")
                        reply_text = gen_result if gen_result else ""
                        # 校验文本完整性：有文本且（无 hash 记录或 hash 匹配）才重试
                        text_valid = bool(reply_text)
                        if text_valid and gen_hash:
                            expected_hash = self.reply_state_store.compute_generation_hash(reply_text)
                            if expected_hash != gen_hash:
                                logger.warning(
                                    f"重试文本 hash 不匹配，文本可能被篡改，转为 deferred: rpid={rpid}"
                                )
                                text_valid = False
                        if not text_valid:
                            # P1-14：无生成结果或 hash 失效 → deferred 重生，不烧 attempt
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason="no_generation_result",
                                error_code="RETRY_NO_GEN",
                                increment_attempt=False,
                            )
                            continue
                        # 幂等检查：按 parent/source_rpid 判断是否已回该条（避免楼中楼漏回）
                        comment_root = root_id if root_id else source_id
                        if self._bot_uid and self.bili:
                            try:
                                replies_data = await self.bili.get_comment_replies(
                                    oid=oid, root=comment_root, comment_type=ct, ps=30,
                                )
                                existing = (
                                    replies_data.get("data", {}).get("replies", [])
                                    if replies_data and replies_data.get("code") == 0
                                    else []
                                )
                                already_replied = self._bot_already_replied_to_source(
                                    existing,
                                    source_rpid=str(source_id or rpid),
                                    expected_text=reply_text,
                                )
                                if already_replied:
                                    logger.info(f"幂等检查：rpid={rpid} 已有 Bot 回复，跳过重试")
                                    self.reply_state_store.mark_published(ct, rpid)
                                    await self._archive_bot_action(
                                        action_key=f"comment_reply:{ct}:{rpid}",
                                        action_type="reply_comment",
                                        text=reply_text,
                                        published=True,
                                        title=f"已回复评论 {rpid}",
                                        scene="reply_comment",
                                        metadata={
                                            "reply_id": rpid,
                                            "oid": str(oid),
                                            "confirmed_by": "thread_lookup",
                                        },
                                    )
                                    self._notify_companion_comment_replied(
                                        title=str(oid or "")[:40],
                                        preview=str(reply_text or "")[:80],
                                        proactive=False,
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"幂等检查失败，继续重试: {e}")
                        # Task 9：重试路径也需原子预占配额（与主路径一致）；无 checker 时 fail-closed
                        if self.safety_checker is None:
                            logger.error(
                                "safety_checker 未初始化，拒绝重试发布评论（fail-closed）: rpid=%s",
                                rpid,
                            )
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason="safety_checker_missing", error_code="NO_SAFETY_CHECKER",
                                increment_attempt=False,
                            )
                            continue
                        # 策略/规则可能在生成后变严，重试前重新内容安全检查
                        try:
                            passed, reason = await self.safety_checker.check_content(
                                reply_text, scene="reply_comment",
                                persona_id=self._get_current_persona_id(),
                                account_id=self.account_id,
                            )
                            if not passed:
                                logger.warning(f"重试路径评论安全检查未通过: {reason}")
                                self.reply_state_store.mark_rejected(
                                    ct, rpid, reason=f"safety_check: {reason}",
                                )
                                continue
                        except Exception as se:
                            logger.error(f"重试路径安全检查异常: {se}", exc_info=True)
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"safety_exception: {se}", error_code="SAFETY_ERROR",
                                increment_attempt=False,
                            )
                            continue
                        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                            scene="reply_comment", account_id=self.account_id,
                        )
                        if not rate_ok:
                            logger.warning("重试路径评论发布频率限制触发，deferred: %s", rate_reason)
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason="rate_limited", error_code="RATE_LIMIT",
                                increment_attempt=False,
                            )
                            continue
                        rate_reserved = True
                        # 直接使用原始文本重新发布（不调用 generate_reply）
                        await self._archive_bot_action(
                            action_key=(
                                f"comment_reply:{ct}:{rpid}:"
                                f"retry_intent:{int(item_state.get('attempts') or 0) + 1}"
                            ),
                            action_type="reply_comment",
                            text=reply_text,
                            published=False,
                            title=f"回复评论 {rpid}",
                            scene="reply_comment",
                            metadata={"reply_id": rpid, "oid": str(oid)},
                        )
                        try:
                            success = await self.bili.post_comment(
                                oid=oid, content=reply_text, comment_type=ct,
                                rpid=comment_root, parent=source_id,
                            )
                        except Exception as post_err:
                            logger.error(f"重试发表评论异常 rpid={rpid}: {post_err}")
                            self.reply_state_store.mark_result_unknown(
                                ct, rpid,
                                reason=f"post_exception: {type(post_err).__name__}",
                                error_code="POST_EXCEPTION",
                            )
                            try:
                                await self._archive_bot_action(
                                    action_key=(
                                        f"comment_reply:{ct}:{rpid}:"
                                        f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                    ),
                                    action_type="reply_comment",
                                    text=reply_text,
                                    published=False,
                                    status="result_unknown",
                                    title=f"回复评论 {rpid}",
                                    scene="reply_comment",
                                    metadata={
                                        "reply_id": rpid,
                                        "oid": str(oid),
                                        "reason_code": "POST_EXCEPTION",
                                    },
                                )
                            except Exception:
                                logger.error(
                                    "unknown retried comment result could not be archived: rpid=%s",
                                    rpid,
                                )
                            continue
                        if success is None:
                            logger.error("重试评论结果不确定（不自动重发）: rpid=%s", rpid)
                            self.reply_state_store.mark_result_unknown(
                                ct, rpid,
                                reason="post_comment transport uncertainty",
                                error_code="RESULT_UNKNOWN",
                            )
                            continue
                        if success:
                            # PRD-V5 §6.2：发布成功保留同一 generation_hash
                            self.reply_state_store.mark_published(ct, rpid)
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{ct}:{rpid}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=True,
                                title=f"已回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={"reply_id": rpid, "oid": str(oid)},
                            )
                            self._notify_companion_comment_replied(
                                title=str(oid or "")[:40],
                                preview=str(reply_text or "")[:80],
                                proactive=False,
                            )
                            logger.info(f"重试发布成功: rpid={rpid}")
                        else:
                            if rate_reserved and self.safety_checker is not None:
                                try:
                                    self.safety_checker.refund_publish(
                                        scene="reply_comment", account_id=self.account_id,
                                    )
                                except Exception:
                                    pass
                            # 再次失败 → retry_wait（attempts 自动递增，超限转 failed）
                            # generation 字段由 upsert 自动保留
                            self.reply_state_store.mark_retry_wait(
                                ct, rpid, reason="retry_publish_failed", error_code="RETRY_PUBLISH_FAILED",
                            )
                            try:
                                await self._archive_bot_action(
                                    action_key=(
                                        f"comment_reply:{ct}:{rpid}:"
                                        f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                    ),
                                    action_type="reply_comment",
                                    text=reply_text,
                                    published=False,
                                    status="failed",
                                    title=f"回复评论 {rpid}",
                                    scene="reply_comment",
                                    metadata={
                                        "reply_id": rpid,
                                        "oid": str(oid),
                                        "reason_code": "RETRY_PUBLISH_FAILED",
                                    },
                                )
                            except Exception:
                                logger.error(
                                    "failed retried comment result could not be archived: rpid=%s",
                                    rpid,
                                )
                            logger.warning(f"重试发布失败: rpid={rpid}")
                    elif state == "deferred":
                        # BUG A-002：不再转 discovered（is_processed 会去重导致死状态），
                        # 而是在重试流程内直接走完整重生成→发帖/继续defer流程
                        logger.info(f"deferred 评论重试生成: rpid={rpid}")

                        # 获取评论上下文（楼中楼对话历史）
                        comment_context = ""
                        reply_replies = []
                        context_root = root_id if root_id else source_id
                        if root_id or source_id:
                            try:
                                replies_data = await self.bili.get_comment_replies(
                                    oid=oid, root=context_root, comment_type=ct, ps=30,
                                )
                                if replies_data and replies_data.get("code") == 0:
                                    reply_replies = replies_data.get("data", {}).get("replies", [])
                            except Exception as ctx_err:
                                logger.warning(f"deferred 重试获取上下文失败: {ctx_err}")
                        if reply_replies:
                            try:
                                context_lines = await self._archive_comment_thread_context(
                                    reply_replies,
                                    oid=oid,
                                    comment_type=ct,
                                    thread_key=context_root,
                                )
                            except Exception:
                                self.reply_state_store.mark_deferred(
                                    ct,
                                    rpid,
                                    reason="comment_context_archive_failed",
                                    error_code="MEMORY_ARCHIVE_FAILED",
                                    increment_attempt=False,
                                )
                                continue
                            comment_context = "\n".join(context_lines)

                        # 构建 ReplyContext（用于 LLM 生成）
                        reply_context = None
                        if self.comment_context_service is not None:
                            try:
                                reply_context = await self.comment_context_service.build_context(
                                    notification=notif,
                                    current_user_id=user_id,
                                    persona_id=self._get_current_persona_id(),
                                    recent_turns=self._bounded_recent_turns(
                                        comment_context.splitlines()
                                    ),
                                )
                            except Exception as e:
                                from bilibot.services.comment_context import ContextArchiveError

                                if isinstance(e, ContextArchiveError):
                                    self._pause_for_memory_failure()
                                    self.reply_state_store.mark_deferred(
                                        ct,
                                        rpid,
                                        reason="video_context_archive_failed",
                                        error_code="MEMORY_ARCHIVE_FAILED",
                                        increment_attempt=False,
                                    )
                                    continue
                                logger.warning(f"deferred 重试构建上下文失败，降级: {e}")

                        # BUG A-001 配套：直接调用 _generate_reply_impl 获取 GenerationOutcome
                        try:
                            outcome = await self.reply_gen._generate_reply_impl(
                                user_id=user_id,
                                username=username,
                                comment=comment_text,
                                thread_id=str(rpid),
                                oid=oid,
                                comment_type=ct,
                                reply_context=reply_context,
                                comment_context=comment_context,
                            )
                        except Exception as gen_err:
                            logger.error(f"deferred 重试生成异常: {gen_err}")
                            _err_name = type(gen_err).__name__
                            _no_burn = (
                                _err_name == "RateLimitExhaustedError"
                                or "rate limit" in str(gen_err).lower()
                                or "rate-limited" in str(gen_err).lower()
                            )
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"retry_gen_exception: {gen_err}",
                                error_code=(
                                    "LLM_RATE_LIMITED" if _no_burn else "RETRY_GEN_EXCEPTION"
                                ),
                                increment_attempt=not _no_burn,
                            )
                            continue

                        if outcome.is_skip:
                            # LLM 明确不回复 → ignored
                            self.reply_state_store.mark_ignored(
                                ct, rpid, rule="llm_no_reply_retry",
                            )
                            continue

                        if not outcome.is_generated:
                            if getattr(outcome, "is_permanent_error", False):
                                self.reply_state_store.mark_failed(
                                    ct, rpid,
                                    reason=f"retry_gen_permanent: {outcome.error_code}",
                                    error_code=outcome.error_code or "GEN_PERMANENT",
                                )
                                continue
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"retry_gen_{outcome.status}: {outcome.error_code}",
                                error_code=outcome.error_code or "RETRY_GEN_FAILED",
                                increment_attempt=(
                                    (outcome.error_code or "")
                                    not in ("LLM_RATE_LIMITED", "RATE_LIMIT", "RATE_LIMITED")
                                ),
                            )
                            continue

                        reply_text = outcome.text
                        audit_id = outcome.audit_id
                        context_meta = outcome.context_meta or {}

                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"comment_reply:{ct}:{rpid}:"
                                    f"generation:{int(item_state.get('attempts') or 0) + 1}"
                                ),
                                action_type="reply_comment",
                                text=reply_text,
                                published=False,
                                title=f"回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={"reply_id": rpid, "oid": str(oid)},
                            )
                        except Exception:
                            self.reply_state_store.mark_deferred(
                                ct,
                                rpid,
                                reason="memory_intent_archive_failed",
                                error_code="MEMORY_ARCHIVE_FAILED",
                                increment_attempt=False,
                            )
                            continue

                        # 持久化生成结果
                        self.reply_state_store.save_generation_result(
                            ct, rpid,
                            text=reply_text,
                            persona_id=context_meta.get("persona_id", "") or self._get_current_persona_id() or "",
                            audit_id=audit_id,
                        )

                        # 安全检查（fail-closed：无 checker 禁止发布）
                        if self.safety_checker is None:
                            logger.error(
                                "safety_checker 未初始化，拒绝 deferred 发布（fail-closed）: rpid=%s",
                                rpid,
                            )
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason="safety_checker_missing", error_code="NO_SAFETY_CHECKER",
                                increment_attempt=False,
                            )
                            continue
                        rate_reserved = False
                        try:
                            passed, reason = await self.safety_checker.check_content(
                                reply_text, scene="reply_comment",
                                persona_id=self._get_current_persona_id(),
                                account_id=self.account_id,
                            )
                            if not passed:
                                if audit_id and self.audit_store:
                                    try:
                                        self.audit_store.mark_published(
                                            audit_id, published=False,
                                            target={
                                                "kind": "reply_comment",
                                                "rpid": str(rpid),
                                                "source_rpid": str(rpid),
                                                "comment_type": int(ct),
                                                "account_id": self.account_id or "",
                                            },
                                            failure_reason=f"safety_check: {reason}",
                                        )
                                    except Exception:
                                        pass
                                self.reply_state_store.mark_rejected(
                                    ct, rpid, reason=f"safety_check: {reason}",
                                )
                                continue
                            rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                                scene="reply_comment", account_id=self.account_id,
                            )
                            if not rate_ok:
                                self.reply_state_store.mark_deferred(
                                    ct, rpid,
                                    reason="rate_limited_retry", error_code="RATE_LIMIT",
                                    increment_attempt=False,
                                )
                                continue
                            rate_reserved = True
                        except Exception as safety_err:
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"safety_exception: {safety_err}",
                                error_code="SAFETY_ERROR",
                                increment_attempt=False,
                            )
                            continue

                        # 发布回复前幂等检查：按 parent/source_rpid 匹配，避免楼中楼漏回
                        comment_root = root_id if root_id else source_id
                        if self._bot_uid and self.bili:
                            try:
                                replies_data = await self.bili.get_comment_replies(
                                    oid=oid, root=comment_root, comment_type=ct, ps=30,
                                )
                                existing = (
                                    replies_data.get("data", {}).get("replies", [])
                                    if replies_data and replies_data.get("code") == 0
                                    else []
                                )
                                already_replied = self._bot_already_replied_to_source(
                                    existing,
                                    source_rpid=str(source_id or rpid),
                                    expected_text=reply_text,
                                )
                                if already_replied:
                                    logger.info(
                                        f"幂等检查：rpid={rpid} 已有 Bot 回复，跳过 deferred 重发"
                                    )
                                    if rate_reserved and self.safety_checker is not None:
                                        try:
                                            self.safety_checker.refund_publish(
                                                scene="reply_comment",
                                                account_id=self.account_id,
                                            )
                                        except Exception:
                                            pass
                                    self.reply_state_store.mark_published(ct, rpid)
                                    await self._archive_bot_action(
                                        action_key=f"comment_reply:{ct}:{rpid}",
                                        action_type="reply_comment",
                                        text=reply_text,
                                        published=True,
                                        title=f"已回复评论 {rpid}",
                                        scene="reply_comment",
                                        metadata={
                                            "reply_id": rpid,
                                            "oid": str(oid),
                                            "confirmed_by": "thread_lookup",
                                        },
                                    )
                                    self._notify_companion_comment_replied(
                                        title=str(oid or "")[:40],
                                        preview=str(reply_text or "")[:80],
                                        proactive=False,
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"deferred 幂等检查失败，继续发布: {e}")
                        try:
                            success = await self.bili.post_comment(
                                oid=oid, content=reply_text, comment_type=ct,
                                rpid=comment_root, parent=source_id,
                            )
                        except Exception as post_err:
                            self.reply_state_store.mark_result_unknown(
                                ct, rpid,
                                reason=f"deferred_retry_post_exception: {type(post_err).__name__}",
                                error_code="POST_EXCEPTION",
                            )
                            try:
                                await self._archive_bot_action(
                                    action_key=(
                                        f"comment_reply:{ct}:{rpid}:"
                                        f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                    ),
                                    action_type="reply_comment",
                                    text=reply_text,
                                    published=False,
                                    status="result_unknown",
                                    title=f"回复评论 {rpid}",
                                    scene="reply_comment",
                                    metadata={
                                        "reply_id": rpid,
                                        "oid": str(oid),
                                        "reason_code": "POST_EXCEPTION",
                                    },
                                )
                            except Exception:
                                logger.error(
                                    "unknown deferred comment result could not be archived: rpid=%s",
                                    rpid,
                                )
                            continue
                        if success is None:
                            logger.error("deferred 评论结果不确定（不自动重发）: rpid=%s", rpid)
                            self.reply_state_store.mark_result_unknown(
                                ct, rpid,
                                reason="post_comment transport uncertainty",
                                error_code="RESULT_UNKNOWN",
                            )
                            continue
                        if success:
                            self.reply_state_store.mark_published(ct, rpid)
                            await self._archive_bot_action(
                                action_key=f"comment_reply:{ct}:{rpid}",
                                action_type="reply_comment",
                                text=reply_text,
                                published=True,
                                title=f"已回复评论 {rpid}",
                                scene="reply_comment",
                                metadata={"reply_id": rpid, "oid": str(oid)},
                            )
                            self._notify_companion_comment_replied(
                                title=str(oid or "")[:40],
                                preview=str(reply_text or "")[:80],
                                proactive=False,
                            )
                            if self.safety_checker is not None:
                                try:
                                    self.safety_checker.record_content(
                                        reply_text, account_id=self.account_id
                                    )
                                except Exception:
                                    pass
                            if audit_id and self.audit_store:
                                try:
                                    self.audit_store.mark_published(
                                        audit_id, published=True,
                                        target={
                                            "kind": "reply_comment",
                                            "rpid": str(rpid),
                                            "source_rpid": str(rpid),
                                            "comment_type": int(ct),
                                            "account_id": self.account_id or "",
                                        },
                                    )
                                except Exception:
                                    pass
                            logger.info(f"deferred 重试成功: rpid={rpid}")
                        else:
                            if rate_reserved and self.safety_checker is not None:
                                try:
                                    self.safety_checker.refund_publish(
                                        scene="reply_comment", account_id=self.account_id,
                                    )
                                except Exception:
                                    pass
                            self.reply_state_store.mark_retry_wait(
                                ct, rpid,
                                reason="deferred_retry_publish_failed",
                                error_code="RETRY_PUBLISH_FAILED",
                            )
                            try:
                                await self._archive_bot_action(
                                    action_key=(
                                        f"comment_reply:{ct}:{rpid}:"
                                        f"retry:{int(item_state.get('attempts') or 0) + 1}"
                                    ),
                                    action_type="reply_comment",
                                    text=reply_text,
                                    published=False,
                                    status="failed",
                                    title=f"回复评论 {rpid}",
                                    scene="reply_comment",
                                    metadata={
                                        "reply_id": rpid,
                                        "oid": str(oid),
                                        "reason_code": "RETRY_PUBLISH_FAILED",
                                    },
                                )
                            except Exception:
                                logger.error(
                                    "failed deferred comment result could not be archived: rpid=%s",
                                    rpid,
                                )
                            logger.warning(f"deferred 重试发布失败: rpid={rpid}")
                except Exception as e:
                    logger.error(f"重试评论 {rpid} 失败: {e}")
        except Exception as e:
            logger.error(f"处理重试评论失败: {e}")

    # ════════════════════════════════════════
    #  主动评论原子幂等（PRD-V5 §10.2 COM-501）
    # ════════════════════════════════════════

    def _get_proactive_comment_max_attempts(self) -> int:
        """从 config 读取主动评论 max_attempts"""
        from bilibot.services.proactive_comment_store import DEFAULT_MAX_ATTEMPTS
        try:
            prov = self.config_loader.get_raw_config().get("proactive", {})
            scenes_cfg = prov.get("scenes", {}) or {}
            scene_cfg = scenes_cfg.get("proactive_comment", {}) or {}
            return int(scene_cfg.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
        except Exception:
            return DEFAULT_MAX_ATTEMPTS

    async def _record_proactive_comment_audit(
        self,
        *,
        persona_id: str,
        comment_text: str,
        bvid: str,
        oid: Any,
        title: str = "",
        owner: str = "",
        input_summary: str = "",
        context_summary: str = "",
        prompt_preview: str = "",
        published: bool = False,
        status: str = "generated",
        failure_reason: str = "",
        extra_target: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """写入主动评论审计（评论页数据源）。失败仅记日志，不抛出。"""
        if self.audit_store is None:
            return None
        target: Dict[str, Any] = {
            "bvid": bvid,
            "oid": oid,
            "kind": "proactive_comment",
            "account_id": self.account_id or "",
        }
        if title:
            target["video_title"] = title
        if owner:
            target["owner"] = owner
        if failure_reason:
            target["failure_reason"] = failure_reason
        if extra_target:
            try:
                target.update(extra_target)
            except Exception:
                pass
        try:
            return await self.audit_store.record_async(
                scene="proactive_comment",
                persona_id=persona_id or "default",
                input_summary=input_summary or (
                    f"主动评论 · 《{title}》 · UP {owner}" if title else f"主动评论 · bvid={bvid}"
                ),
                context_summary=context_summary or f"主动看视频后发表评论 bvid={bvid}",
                prompt_preview=prompt_preview or (
                    f"视频: {title} | UP: {owner}" if title else f"bvid={bvid}"
                ),
                output=comment_text,
                published=published,
                status=status,
                target=target,
            )
        except Exception as e:
            logger.warning(f"主动评论审计记录失败 bvid={bvid}: {e}")
            return None

    def _finalize_proactive_comment_audit(
        self,
        audit_id: Optional[str],
        *,
        published: bool = False,
        failure_reason: str = "",
        status: Optional[str] = None,
        target: Optional[Dict[str, Any]] = None,
    ) -> None:
        """收口主动评论审计终态，避免评论页长期卡在 generated/pending。"""
        if not audit_id or self.audit_store is None:
            return
        try:
            self.audit_store.mark_published(
                audit_id,
                published=published,
                failure_reason=failure_reason or None,
                status=status,
                target=target,
            )
        except Exception as e:
            logger.warning(f"主动评论审计终态更新失败 audit_id={audit_id}: {e}")

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

    def _get_forbidden_phrases(self) -> Tuple[List[str], List["re.Pattern"]]:
        """读取禁用短语配置（Task 26）

        从 config 的 proactive 段读取：
        - forbidden_phrases：大小写不敏感子串匹配列表
        - forbidden_phrase_patterns：正则模式字符串列表（IGNORECASE 编译）

        未配置时回退到 _DEFAULT_FORBIDDEN_PHRASES / _DEFAULT_FORBIDDEN_PATTERNS。

        Returns:
            (phrases, compiled_patterns)
        """
        try:
            prov = self.config_loader.get_raw_config().get("proactive", {}) or {}
            phrases = prov.get("forbidden_phrases", self._DEFAULT_FORBIDDEN_PHRASES)
            pattern_strs = prov.get(
                "forbidden_phrase_patterns", self._DEFAULT_FORBIDDEN_PATTERNS
            )
        except Exception:
            phrases = self._DEFAULT_FORBIDDEN_PHRASES
            pattern_strs = self._DEFAULT_FORBIDDEN_PATTERNS
        try:
            patterns = [re.compile(p, re.IGNORECASE) for p in pattern_strs]
        except Exception as e:
            logger.warning(f"编译 forbidden_phrase_patterns 失败，回退默认: {e}")
            patterns = [re.compile(p, re.IGNORECASE) for p in self._DEFAULT_FORBIDDEN_PATTERNS]
        return phrases, patterns

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
        """PRD-V5 §10.2 COM-501：主动评论原子幂等发布流程

        流程：
        1. claim — 同账号同视频只能一个 worker 进入；失败则跳过
        2. 生成文本 → save_generation
        3. CommentPolicy 检查 — 拒绝则 mark_failed(policy_rejected)
        4. 安全检查 — 拒绝则 mark_failed(safety_rejected/rate_limited)
        5. mark_publishing
        6. 调用 B站 send_comment API
        7. 成功 → mark_published
        8. 异常（HTTP 超时/解析失败）→ mark_result_unknown（不自动重发）
        9. API 返回 False → mark_retry_wait（达到 max_attempts 自动转 failed）

        Returns:
            成功发表的评论文本；未发表返回空字符串
        """
        from bilibot.services.proactive_comment_store import (
            default_idempotency_key,
        )

        max_attempts = self._get_proactive_comment_max_attempts()
        persona_id_for_policy = self._get_current_persona_id()
        idem_key = default_idempotency_key(self.account_id or "_default", bvid)

        # 1. 原子 claim
        action = self.proactive_comment_store.claim(
            account_id=self.account_id or "_default",
            bvid=bvid,
            task_id=task_id,
            idempotency_key=idem_key,
            persona_id=persona_id_for_policy,
            max_attempts=max_attempts,
        )
        if action is None:
            logger.info(f"主动评论已由其他 worker claim（account={self.account_id}, bvid={bvid}），跳过")
            return ""
        action_id = action.action_id

        activity_context = await self._begin_activity_context(
            action_key=f"proactive_comment:{action_id}",
            action_type="proactive_comment",
            current_activity=(
                "正在为刚看过的视频准备一条主动评论，必须结合视频内容、刚才的评价和最近经历再决定怎么说。"
            ),
            query=" ".join(
                item
                for item in (
                    str(title or ""),
                    str(owner or ""),
                    str(review or "")[:500],
                    str(mood or ""),
                    " ".join(str(tag) for tag in (tags_list or [])[:8]),
                )
                if item
            ),
            scene="proactive_video",
            title=title,
            bvid=bvid,
            oid=str(oid),
            metadata={"bvid": bvid, "oid": str(oid), "task_id": task_id},
        )

        # 2. 生成评论（评价阶段已有 short comment 时可复用；否则再生成并混入记忆）
        comment_text = evaluation.get("comment", "") if llm_ok else ""
        mem_ev = str(memory_evidence or "").strip()
        activity_prompt = str(
            getattr(activity_context, "prompt_text", "") or ""
        ).strip()
        if activity_prompt and activity_prompt not in mem_ev:
            mem_ev = "\n\n".join(item for item in (mem_ev, activity_prompt) if item)
        if activity_context is not None:
            for event_id in list(getattr(activity_context, "event_ids", ()) or ()):
                if event_id and event_id not in (memory_event_ids or []):
                    if memory_event_ids is None:
                        memory_event_ids = []
                    memory_event_ids.append(event_id)
        life_ctx = str(companion_context or "").strip()
        if not life_ctx:
            companion = getattr(self, "companion", None)
            if companion is not None and getattr(companion, "enabled", False):
                try:
                    if hasattr(companion, "build_proactive_context_block"):
                        life_ctx = companion.build_proactive_context_block() or ""
                except Exception:
                    life_ctx = ""
        if not mem_ev:
            try:
                bundle = await self._recall_for_proactive_video(
                    title=title,
                    owner=owner,
                    tags=tags_list,
                    bvid=bvid,
                    oid=str(oid),
                    desc=desc,
                )
                mem_ev = str(bundle.get("memory_evidence") or "")
                if not memory_event_ids:
                    memory_event_ids = list(bundle.get("memory_event_ids") or [])
            except Exception:
                mem_ev = ""

        if not comment_text or len(comment_text) < 5:
            try:
                try:
                    comment_text = await self.comment_generator.generate_proactive_comment(
                        title=title,
                        owner=owner,
                        desc=desc,
                        tags=tags_list,
                        review=review,
                        mood=mood,
                        video_content=video_content,
                        companion_context=life_ctx,
                        memory_evidence=mem_ev,
                    )
                except TypeError:
                    try:
                        comment_text = await self.comment_generator.generate_proactive_comment(
                            title=title,
                            owner=owner,
                            desc=desc,
                            tags=tags_list,
                            review=review,
                            mood=mood,
                            video_content=video_content,
                            companion_context=life_ctx,
                        )
                    except TypeError:
                        comment_text = await self.comment_generator.generate_proactive_comment(
                            title=title,
                            owner=owner,
                            desc=desc,
                            tags=tags_list,
                            review=review,
                            mood=mood,
                            video_content=video_content,
                        )
            except Exception as e:
                logger.warning(f"评论生成失败: {e}")
                comment_text = ""

        # PRD V4 COM-003：禁止生成"我完整看完了"等与真实 watch_state 冲突的表达
        # Task 26 增强：可配置短语表 + 大小写不敏感 + 正则变体覆盖（如"完整.*看完"、"看完了?"）
        if comment_text:
            forbidden_phrases, forbidden_patterns = self._get_forbidden_phrases()
            comment_lower = comment_text.lower()
            hit = next(
                (p for p in forbidden_phrases if p.lower() in comment_lower), None
            )
            if hit is None:
                hit_pat = next(
                    (p for p in forbidden_patterns if p.search(comment_text)), None
                )
                hit = hit_pat.pattern if hit_pat is not None else None
            if hit:
                logger.warning(f"评论包含与 watch_state 冲突的表达 '{hit}'，拒绝发布")
                comment_text = ""

        if not comment_text:
            self.proactive_comment_store.mark_failed(
                action_id, "NO_COMMENT_TEXT", "评论生成失败或为空",
            )
            return ""

        # 保存生成结果（便于 retry 时复用）；附带 memory event ids 便于审计
        try:
            self.proactive_comment_store.save_generation(action_id, comment_text)
        except TypeError:
            self.proactive_comment_store.save_generation(action_id, comment_text)
        # 将 memory_event_ids 挂到 action 元数据（若 store 支持 patch）
        try:
            patch = getattr(self.proactive_comment_store, "patch_metadata", None)
            if callable(patch) and memory_event_ids:
                patch(action_id, {"memory_event_ids": list(memory_event_ids)[:20]})
        except Exception:
            pass
        # PRD V6：不单独记录 intent，仅在最终结果时归档，避免同一行为两条记忆。

        # 3. PRD V4 COM-002：CommentPolicy 检查（去重 + 预算 + content_hash）
        allowed, policy_reason, policy_meta = await self.comment_policy.check_async(
            bvid=bvid, oid=str(oid), content=comment_text,
            persona_id=persona_id_for_policy, max_per_video=1,
        )
        if not allowed:
            logger.warning(f"主动评论策略拒绝: {policy_reason}")
            self.proactive_comment_store.mark_failed(
                action_id, "POLICY_REJECTED", policy_reason,
            )
            await self._record_proactive_comment_audit(
                persona_id=persona_id_for_policy or "default",
                comment_text=comment_text,
                bvid=bvid,
                oid=oid,
                title=title,
                owner=owner,
                published=False,
                status="failed",
                failure_reason=f"policy_rejected: {policy_reason}",
            )
            await self._archive_bot_action(
                action_key=f"proactive_comment:{action_id}",
                action_type="proactive_comment",
                text=comment_text,
                published=False,
                status="rejected",
                title=title,
                scene="proactive_video",
                metadata={
                    "bvid": bvid,
                    "oid": str(oid),
                    "reason_code": "POLICY_REJECTED",
                },
            )
            return ""

        # 4. PRD §5.9：发布前内容检查 + 频率限制（fail-closed：无 checker 禁止发布）
        if self.safety_checker is None:
            logger.error(
                "safety_checker 未初始化，拒绝主动评论（fail-closed）: action=%s",
                action_id,
            )
            self.proactive_comment_store.mark_failed(
                action_id, "NO_SAFETY_CHECKER", "safety_checker not initialized",
            )
            return ""
        rate_reserved = False
        try:
            passed, reason = await self.safety_checker.check_content(
                comment_text, scene="proactive_comment",
                persona_id=persona_id_for_policy,
                account_id=self.account_id,
            )
            if not passed:
                logger.warning(f"主动评论安全检查未通过: {reason}")
                self.proactive_comment_store.mark_failed(
                    action_id, "SAFETY_REJECTED", reason,
                )
                await self._record_proactive_comment_audit(
                    persona_id=persona_id_for_policy or "default",
                    comment_text=comment_text,
                    bvid=bvid,
                    oid=oid,
                    title=title,
                    owner=owner,
                    published=False,
                    status="failed",
                    failure_reason=f"safety_rejected: {reason}",
                )
                await self._archive_bot_action(
                    action_key=f"proactive_comment:{action_id}",
                    action_type="proactive_comment",
                    text=comment_text,
                    published=False,
                    status="rejected",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "SAFETY_REJECTED",
                    },
                )
                return ""
            # Task 21.1：改用 check_and_record_rate_limit 原子方法（避免竞态条件）
            # Task 21.2：原子方法为预扣减设计，post_comment 失败时需退回配额
            rate_passed, rate_reason = self.safety_checker.check_and_record_rate_limit(
                scene="proactive_comment", account_id=self.account_id,
            )
            if not rate_passed:
                logger.warning(f"主动评论频率限制触发，延后重试: {rate_reason}")
                # 限流是临时条件，不得消耗 attempt 预算 / 永久 failed
                self.proactive_comment_store.mark_retry_wait(
                    action_id, "RATE_LIMITED", "proactive_comment rate limited",
                    increment_attempt=False,
                )
                return ""
            rate_reserved = True
        except Exception as e:
            # PRD V4 DYN-003 / §4.2：fail-closed
            logger.error(f"主动评论安全检查异常（拒绝发布）: {e}", exc_info=True)
            self.proactive_comment_store.mark_failed(
                action_id, "SAFETY_CHECK_ERROR", str(e),
            )
            return ""

        # 5. mark_publishing（claimed → publishing）
        if not self.proactive_comment_store.mark_publishing(action_id):
            logger.warning(f"主动评论动作 {action_id} mark_publishing 失败")
            if rate_reserved and self.safety_checker is not None:
                try:
                    self.safety_checker.refund_publish(
                        scene="proactive_comment", account_id=self.account_id,
                    )
                except Exception:
                    pass
            return ""

        # PRD 3.5 / COM-004：审计记录（评论页与 reply_comment 一并展示）
        # 必须在调用平台 API 前落库；若写失败，成功后仍会补写一条 published 审计。
        audit_id = await self._record_proactive_comment_audit(
            persona_id=persona_id_for_policy or "default",
            comment_text=comment_text,
            bvid=bvid,
            oid=oid,
            title=title,
            owner=owner,
            status="publishing",
        )

        # 6. 调用 B站 API 发布
        try:
            success = await self.bili.post_comment(
                oid=oid,
                content=comment_text,
                comment_type=1,
                rpid=0,
                parent=0,
            )
        except Exception as e:
            # PRD-V5 §10.2 COM-501：HTTP 异常 → 平台可能已收到 → result_unknown
            # 配额策略：结果不确定时不退还（可能已发出）
            logger.error(f"评论发表异常（平台可能已收到）: {e}", exc_info=True)
            self.proactive_comment_store.mark_result_unknown(
                action_id, "PUBLISH_EXCEPTION", str(e),
            )
            try:
                await self._archive_bot_action(
                    action_key=f"proactive_comment:{action_id}",
                    action_type="proactive_comment",
                    text=comment_text,
                    published=False,
                    status="result_unknown",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "PUBLISH_EXCEPTION",
                    },
                )
            except Exception:
                logger.error(
                    "unknown proactive comment result could not be archived: action=%s",
                    action_id,
                )
            try:
                await self.comment_policy.record_async(
                    bvid=bvid, oid=str(oid), content=comment_text,
                    persona_id=persona_id_for_policy,
                    published=False, failure_reason=str(e),
                )
                await self.interaction_policy.record_result_async(
                    "comment", bvid, str(oid), "failed", failure_reason=str(e),
                    content_hash=self.comment_policy.content_hash(comment_text),
                )
            except Exception:
                pass
            self._finalize_proactive_comment_audit(
                audit_id,
                published=False,
                failure_reason=str(e),
                status="result_unknown",
            )
            return ""

        if success is None:
            logger.error("主动评论结果不确定（不自动重发）: action=%s", action_id)
            self.proactive_comment_store.mark_result_unknown(
                action_id, "RESULT_UNKNOWN", "post_comment transport uncertainty",
            )
            try:
                await self._archive_bot_action(
                    action_key=f"proactive_comment:{action_id}",
                    action_type="proactive_comment",
                    text=comment_text,
                    published=False,
                    status="result_unknown",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "RESULT_UNKNOWN",
                    },
                )
            except Exception:
                logger.error(
                    "unknown proactive comment result could not be archived: action=%s",
                    action_id,
                )
            self._finalize_proactive_comment_audit(
                audit_id,
                published=False,
                failure_reason="post_comment transport uncertainty",
                status="result_unknown",
            )
            return ""

        if success is False:
            self._check_bili_risk_control("proactive_comment")

        if success:
            # COM-603：先 mark_published，成功后再记录 policy（避免 mark_published 失败时 policy 已记录）
            # Task 4：检查 mark_published 返回值，失败时告警（可能需人工介入）
            if not self.proactive_comment_store.mark_published(action_id):
                logger.warning(
                    f"Task 4: 主动评论 mark_published 失败 action={action_id}（状态可能已变更）"
                )
            logger.info(f"主动评论发表: {comment_text[:30]}")
            # 记录 CommentPolicy / InteractionPolicy
            try:
                await self.comment_policy.record_async(
                    bvid=bvid, oid=str(oid), content=comment_text,
                    persona_id=persona_id_for_policy,
                    published=True,
                )
                await self.interaction_policy.record_result_async(
                    "comment", bvid, str(oid), "success",
                    api_code=getattr(self.bili, "last_api_code", None),
                    content_hash=self.comment_policy.content_hash(comment_text),
                )
            except Exception:
                pass
            if self.safety_checker is not None:
                try:
                    self.safety_checker.record_content(comment_text, account_id=self.account_id)
                except Exception:
                    pass
            # 评论页依赖 audit：pre-publish 写入失败时在成功路径补写，避免“已发出但页面没有”
            if audit_id:
                self._finalize_proactive_comment_audit(audit_id, published=True)
            else:
                await self._record_proactive_comment_audit(
                    persona_id=persona_id_for_policy or "default",
                    comment_text=comment_text,
                    bvid=bvid,
                    oid=oid,
                    title=title,
                    owner=owner,
                    published=True,
                    status="published",
                )
            try:
                await self._archive_bot_action(
                    action_key=f"proactive_comment:{action_id}",
                    action_type="proactive_comment",
                    text=comment_text,
                    published=True,
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "memory_event_ids": list(memory_event_ids or [])[:20],
                        "memory_grounded": bool(mem_ev),
                    },
                )
            except Exception:
                logger.error(
                    "published proactive comment result could not be archived: action=%s",
                    action_id,
                )
            else:
                self._notify_companion_comment_replied(
                    title=str(title or "")[:40],
                    preview=str(comment_text or "")[:80],
                    proactive=True,
                )
            return comment_text
        else:
            # 9. API 返回 False → retry_wait（达 max_attempts 自动转 failed）
            logger.warning("主动评论发表失败")
            # 记录 CommentPolicy / InteractionPolicy
            try:
                await self.comment_policy.record_async(
                    bvid=bvid, oid=str(oid), content=comment_text,
                    persona_id=persona_id_for_policy,
                    published=False,
                    failure_reason="bili_api_false",
                )
                await self.interaction_policy.record_result_async(
                    "comment", bvid, str(oid), "failed",
                    api_code=getattr(self.bili, "last_api_code", None),
                    failure_reason="bili_api_false",
                    content_hash=self.comment_policy.content_hash(comment_text),
                )
            except Exception:
                pass
            self.proactive_comment_store.mark_retry_wait(
                action_id, "BILI_API_FALSE",
                f"bili.post_comment 返回 False (code={getattr(self.bili, 'last_api_code', None)})",
            )
            try:
                await self._archive_bot_action(
                    action_key=f"proactive_comment:{action_id}",
                    action_type="proactive_comment",
                    text=comment_text,
                    published=False,
                    status="failed",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "BILI_API_FALSE",
                        "memory_event_ids": list(memory_event_ids or [])[:20],
                    },
                )
            except Exception:
                logger.error(
                    "failed proactive comment result could not be archived: action=%s",
                    action_id,
                )
            # Task 21.2：发布失败退回预占的频率配额（原子方法预扣减，失败时退回）
            if self.safety_checker is not None:
                try:
                    self.safety_checker.refund_publish(
                        scene="proactive_comment", account_id=self.account_id,
                    )
                except Exception:
                    pass
            self._finalize_proactive_comment_audit(
                audit_id,
                published=False,
                failure_reason="bili_api_error",
            )
            return ""

    async def _process_retryable_proactive_comments(self):
        """PRD-V5 §10.2 COM-501：重试 retry_wait 的主动评论

        - 只复用已生成的文本（不重新生成），保证内容一致性
        - mark_publishing（retry_wait → publishing）→ 调 API → published / retry_wait / result_unknown
        - 已 published / failed / result_unknown 的不重试
        """
        if not self.bili:
            return
        try:
            pending = self.proactive_comment_store.list_pending_retry(
                account_id=self.account_id or "_default",
            )
            if not pending:
                return
            logger.info(f"发现 {len(pending)} 条待重试主动评论")
            for action in pending:
                if self.proactive_comment_store.has_published(
                    action.account_id, action.bvid,
                ):
                    # 已发布（可能另一 worker 成功）→ 跳过
                    continue
                reply_text = action.generation_text or ""
                if not reply_text:
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "NO_GEN_TEXT", "重试时无生成文本",
                    )
                    continue
                # 校验 hash 完整性
                from bilibot.services.proactive_comment_store import (
                    compute_generation_hash,
                )
                if action.generation_hash:
                    expected = compute_generation_hash(reply_text)
                    if expected != action.generation_hash:
                        logger.warning(
                            f"主动评论重试文本 hash 不匹配 action={action.action_id}"
                        )
                        self.proactive_comment_store.mark_failed(
                            action.action_id, "GEN_HASH_MISMATCH",
                            "重试文本 hash 不匹配",
                        )
                        continue
                # oid 反查（在策略/安全检查前完成）
                oid = await self._resolve_oid_from_bvid(action.bvid)
                if not oid:
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "NO_OID",
                        f"无法解析 bvid={action.bvid} 的 oid",
                    )
                    continue

                # COM-601：先完成策略/安全，再 mark_publishing，避免检查失败后卡在 publishing
                allowed, policy_reason, _ = await self.comment_policy.check_async(
                    bvid=action.bvid, oid=str(oid), content=reply_text,
                    persona_id=action.persona_id or "", max_per_video=1,
                )
                if not allowed:
                    logger.warning(f"重试主动评论策略拒绝: {policy_reason}")
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "POLICY_REJECTED", policy_reason,
                    )
                    continue
                if self.safety_checker is None:
                    logger.error(
                        "safety_checker 未初始化，拒绝重试主动评论（fail-closed）: action=%s",
                        action.action_id,
                    )
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "NO_SAFETY_CHECKER",
                        "safety_checker not initialized",
                    )
                    continue
                rate_reserved = False
                try:
                    ok, sreason = await self.safety_checker.check_content(
                        reply_text, scene="proactive_comment",
                        persona_id=action.persona_id or "",
                        account_id=self.account_id,
                    )
                    if not ok:
                        logger.warning(f"重试主动评论安全检查未通过: {sreason}")
                        self.proactive_comment_store.mark_failed(
                            action.action_id, "SAFETY_REJECTED", sreason,
                        )
                        continue
                    rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                        scene="proactive_comment", account_id=self.account_id,
                    )
                    if not rate_ok:
                        logger.warning("重试主动评论频率限制触发: %s", rate_reason)
                        # 保持 retry_wait 且不烧 attempt，避免限流导致永久 failed
                        self.proactive_comment_store.mark_retry_wait(
                            action.action_id, "RATE_LIMITED",
                            "proactive_comment rate limited",
                            increment_attempt=False,
                        )
                        continue
                    rate_reserved = True
                except Exception as se:
                    logger.error(f"重试主动评论安全检查异常（拒绝发布）: {se}", exc_info=True)
                    self.proactive_comment_store.mark_failed(
                        action.action_id, "SAFETY_CHECK_ERROR", str(se),
                    )
                    continue

                # retry_wait → publishing（检查通过后再 claim）
                if not self.proactive_comment_store.mark_publishing(action.action_id):
                    if rate_reserved and self.safety_checker is not None:
                        try:
                            self.safety_checker.refund_publish(
                                scene="proactive_comment", account_id=self.account_id,
                            )
                        except Exception:
                            pass
                    continue

                audit_id = await self._record_proactive_comment_audit(
                    persona_id=action.persona_id or "default",
                    comment_text=reply_text,
                    bvid=action.bvid,
                    oid=oid,
                    input_summary=f"主动评论重试 · bvid={action.bvid}",
                    context_summary=f"主动评论重试发布 bvid={action.bvid}",
                    prompt_preview=f"retry bvid={action.bvid}",
                    status="publishing",
                )

                try:
                    success = await self.bili.post_comment(
                        oid=oid, content=reply_text,
                        comment_type=1, rpid=0, parent=0,
                    )
                except Exception as e:
                    logger.error(f"重试主动评论异常 action={action.action_id}: {e}")
                    # 结果不确定时不退配额（可能已发出）
                    self.proactive_comment_store.mark_result_unknown(
                        action.action_id, "RETRY_PUBLISH_EXCEPTION", str(e),
                    )
                    try:
                        await self._archive_bot_action(
                            action_key=(
                                f"proactive_comment:{action.action_id}:"
                                f"retry:{action.attempt + 1}"
                            ),
                            action_type="proactive_comment",
                            text=reply_text,
                            published=False,
                            status="result_unknown",
                            title=action.bvid,
                            scene="proactive_video",
                            metadata={
                                "bvid": action.bvid,
                                "reason_code": "RETRY_PUBLISH_EXCEPTION",
                            },
                        )
                    except Exception:
                        logger.error(
                            "unknown retried proactive comment result could not be archived: action=%s",
                            action.action_id,
                        )
                    self._finalize_proactive_comment_audit(
                        audit_id,
                        published=False,
                        failure_reason=str(e),
                        status="result_unknown",
                    )
                    continue

                if success is None:
                    logger.error(
                        "重试主动评论结果不确定（不自动重发）: action=%s",
                        action.action_id,
                    )
                    self.proactive_comment_store.mark_result_unknown(
                        action.action_id, "RESULT_UNKNOWN",
                        "post_comment transport uncertainty",
                    )
                    try:
                        await self._archive_bot_action(
                            action_key=(
                                f"proactive_comment:{action.action_id}:"
                                f"retry:{action.attempt + 1}"
                            ),
                            action_type="proactive_comment",
                            text=reply_text,
                            published=False,
                            status="result_unknown",
                            title=action.bvid,
                            scene="proactive_video",
                            metadata={
                                "bvid": action.bvid,
                                "oid": str(oid),
                                "reason_code": "RESULT_UNKNOWN",
                            },
                        )
                    except Exception:
                        logger.error(
                            "unknown retried proactive comment result could not be archived: action=%s",
                            action.action_id,
                        )
                    self._finalize_proactive_comment_audit(
                        audit_id,
                        published=False,
                        failure_reason="post_comment transport uncertainty",
                        status="result_unknown",
                    )
                    continue

                if success:
                    if not self.proactive_comment_store.mark_published(action.action_id):
                        logger.warning(
                            f"Task 4: 重试主动评论 mark_published 失败 action={action.action_id}（状态可能已变更）"
                        )
                    logger.info(f"主动评论重试成功 action={action.action_id}")
                    try:
                        await self.comment_policy.record_async(
                            bvid=action.bvid, oid=str(oid), content=reply_text,
                            persona_id=action.persona_id or "",
                            published=True,
                        )
                        await self.interaction_policy.record_result_async(
                            "comment", action.bvid, str(oid), "success",
                            content_hash=self.comment_policy.content_hash(reply_text),
                        )
                    except Exception:
                        pass
                    if audit_id:
                        self._finalize_proactive_comment_audit(audit_id, published=True)
                    else:
                        await self._record_proactive_comment_audit(
                            persona_id=action.persona_id or "default",
                            comment_text=reply_text,
                            bvid=action.bvid,
                            oid=oid,
                            input_summary=f"主动评论重试 · bvid={action.bvid}",
                            context_summary=f"主动评论重试发布 bvid={action.bvid}",
                            prompt_preview=f"retry bvid={action.bvid}",
                            published=True,
                            status="published",
                        )
                    try:
                        await self._archive_bot_action(
                            action_key=f"proactive_comment:{action.action_id}",
                            action_type="proactive_comment",
                            text=reply_text,
                            published=True,
                            title=action.bvid,
                            scene="proactive_video",
                            metadata={"bvid": action.bvid, "oid": str(oid)},
                        )
                    except Exception:
                        logger.error(
                            "retried proactive comment result could not be archived: action=%s",
                            action.action_id,
                        )
                    else:
                        self._notify_companion_comment_replied(
                            title=str(action.bvid or "")[:40],
                            preview=str(reply_text or "")[:80],
                            proactive=True,
                        )
                else:
                    self.proactive_comment_store.mark_retry_wait(
                        action.action_id, "RETRY_PUBLISH_FAILED",
                        "重试发布失败：bili.post_comment 返回 False",
                    )
                    if rate_reserved and self.safety_checker is not None:
                        try:
                            self.safety_checker.refund_publish(
                                scene="proactive_comment", account_id=self.account_id,
                            )
                        except Exception:
                            pass
                    try:
                        await self._archive_bot_action(
                            action_key=(
                                f"proactive_comment:{action.action_id}:"
                                f"retry:{action.attempt + 1}"
                            ),
                            action_type="proactive_comment",
                            text=reply_text,
                            published=False,
                            status="failed",
                            title=action.bvid,
                            scene="proactive_video",
                            metadata={
                                "bvid": action.bvid,
                                "oid": str(oid),
                                "reason_code": "RETRY_PUBLISH_FAILED",
                            },
                        )
                    except Exception:
                        logger.error(
                            "failed retried proactive comment result could not be archived: action=%s",
                            action.action_id,
                        )
                    self._finalize_proactive_comment_audit(
                        audit_id,
                        published=False,
                        failure_reason="retry_bili_api_error",
                    )
                    logger.warning(f"主动评论重试失败 action={action.action_id}")

        except Exception as e:
            logger.error(f"处理重试主动评论失败: {e}", exc_info=True)

    def _dispatch_task_run(
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
            self._spawn_memory_task(
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
                self._spawn_memory_task(
                    self._do_publish_approved_draft(task_id, draft_id),
                    tag=f"{tag_prefix}_publish_draft:{task_id}",
                )
            else:
                self._spawn_memory_task(
                    self._do_post_dynamic(task_id=task_id),
                    tag=f"{tag_prefix}_dynamic:{task_id}",
                )
            return True
        if scene == "bangumi":
            self._spawn_memory_task(
                self._do_bangumi_task(task_id),
                tag=f"{tag_prefix}_bangumi:{task_id}",
            )
            return True
        logger.warning(
            "dispatch 不支持场景 task=%s scene=%s", task_id, scene,
        )
        return False

    async def _process_retryable_tasks(self):
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

    async def _resolve_oid_from_bvid(self, bvid: str) -> Optional[int]:
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
        """检查新私信并自动回复"""
        if not self.reply_gen or not self.bili:
            return
        if not self._authenticated_poll_allowed():
            return

        # 全局暂停 / 账号风险暂停 / 无 checker → fail-closed
        if self.safety_checker is None:
            logger.error("safety_checker 未初始化，跳过私信检查（fail-closed）")
            return
        if (
            self.safety_checker.is_paused()
            or self.safety_checker.is_account_paused(self.account_id)
        ):
            return

        config = self.config_loader.get_raw_config()
        if not config.get("features", {}).get("private_message", False):
            return

        try:
            sessions_resp = await self.bili.get_private_sessions(limit=20)
            if not sessions_resp:
                logger.info("私信会话 API 返回空")
                return
            if sessions_resp.get("code") != 0:
                logger.info(f"私信会话 API 返回错误: code={sessions_resp.get('code')} msg={sessions_resp.get('message')}")
                return

            session_list = sessions_resp.get("data", {}).get("session_list", [])
            if not session_list:
                logger.info("无私信会话")
                return

            # P1-1：与评论路径统一 bot 身份；优先 _bot_uid（nav 可补全）
            try:
                my_uid = int(
                    str(getattr(self, "_bot_uid", "") or "").strip()
                    or config.get("bilibili", {}).get("dede_user_id", 0)
                    or 0
                )
            except (TypeError, ValueError):
                my_uid = 0
            if not my_uid:
                logger.error(
                    "_bot_uid/dede_user_id 为空，跳过私信检查（fail-closed，防止身份错乱）"
                )
                return
            # PRD V6：会话数变化时才打 INFO，否则降级 DEBUG
            _sess_count = len(session_list)
            if _sess_count != self._last_dm_session_count:
                logger.info("检查私信: %d 个会话", _sess_count)
                self._last_dm_session_count = _sess_count
            else:
                logger.debug("检查私信: %d 个会话（无变化）", _sess_count)

            for session in session_list:
                try:
                    # 只处理单聊（session_type=1）
                    if session.get("session_type", 1) != 1:
                        continue

                    # 检查是否有未读消息
                    unread = session.get("unread_count", 0)
                    try:
                        unread = int(unread or 0)
                    except (TypeError, ValueError):
                        unread = 0
                    if not unread:
                        continue

                    # 获取对方信息
                    talker_id_raw = session.get("talker_id", 0)
                    try:
                        talker_id = int(talker_id_raw)
                    except (TypeError, ValueError):
                        talker_id = 0
                    if not talker_id or talker_id == my_uid:
                        continue

                    # 获取对方用户名
                    talker_name = "用户"
                    talker_info = session.get("talker_info") or {}
                    if isinstance(talker_info, dict):
                        talker_name = talker_info.get("uname") or talker_info.get("name") or "用户"

                    from bilibot.services.pm_state_store import (
                        extract_platform_message_id,
                        TERMINAL_STATUSES as PM_TERMINAL,
                        RETRYABLE_STATUSES as PM_RETRYABLE,
                    )

                    # S6：unread>1 时拉最近 N 条按 platform_message_id 幂等处理，避免只回 last_msg 漏回
                    history_messages = (
                        session.get("messages")
                        or session.get("message_list")
                        or session.get("session_messages")
                        or []
                    )
                    if not isinstance(history_messages, list):
                        history_messages = []

                    fetch_limit = max(5, min(int(unread) + 2, 20))
                    need_fetch = (
                        unread > 1
                        or not (session.get("last_msg") or {}).get("content")
                        or not history_messages
                    )
                    if need_fetch and hasattr(self.bili, "get_session_messages"):
                        try:
                            msgs_resp = await self.bili.get_session_messages(
                                talker_id=talker_id,
                                session_type=1,
                                size=fetch_limit,
                                sender_uid=talker_id,
                                receiver_uid=my_uid,
                                limit=fetch_limit,
                            )
                            if msgs_resp and msgs_resp.get("code") == 0:
                                fetched = (
                                    (msgs_resp.get("data") or {}).get("messages")
                                    or []
                                )
                                if isinstance(fetched, list) and fetched:
                                    history_messages = fetched
                        except Exception as fetch_exc:
                            logger.warning(
                                "拉取会话消息列表失败 talker=%s: %s",
                                talker_id, type(fetch_exc).__name__,
                            )

                    # 候选：对方发来的、有内容、可提取 platform_message_id 的消息
                    # 优先处理历史中的未读条；无列表时退回 last_msg 单条
                    candidate_msgs: List[Dict[str, Any]] = []
                    if history_messages:
                        for m in history_messages:
                            if not isinstance(m, dict):
                                continue
                            try:
                                s_uid = int(m.get("sender_uid") or 0)
                            except (TypeError, ValueError):
                                s_uid = 0
                            if s_uid and s_uid == my_uid:
                                continue
                            if not self._private_message_text(m):
                                continue
                            if not extract_platform_message_id(m):
                                continue
                            candidate_msgs.append(m)
                    if not candidate_msgs:
                        last_msg = session.get("last_msg") or {}
                        if isinstance(last_msg, dict) and last_msg:
                            try:
                                s_uid = int(last_msg.get("sender_uid") or 0)
                            except (TypeError, ValueError):
                                s_uid = 0
                            if not (s_uid and s_uid == my_uid):
                                if self._private_message_text(last_msg) and extract_platform_message_id(last_msg):
                                    candidate_msgs = [last_msg]

                    if not candidate_msgs:
                        continue

                    # 每会话最多处理 batch 条，避免一次拉太多触发限流；按时间序（旧→新）
                    def _msg_ts(m: Dict[str, Any]) -> float:
                        for k in ("timestamp", "msg_timestamp", "msg_seqno", "seqno"):
                            v = m.get(k)
                            if v is not None:
                                try:
                                    return float(v)
                                except (TypeError, ValueError):
                                    pass
                        return 0.0

                    candidate_msgs = sorted(candidate_msgs, key=_msg_ts)
                    # P0-A：本轮最多主动处理 5 条（限流），但其余候选必须至少
                    # ensure_discovered 入库，且未处理完禁止整会话 ack。
                    process_batch = candidate_msgs[:5]
                    overflow_msgs = candidate_msgs[5:]
                    for m in overflow_msgs:
                        pid = extract_platform_message_id(m)
                        if not pid:
                            continue
                        try:
                            self.pm_state_store.ensure_discovered(
                                account_id=self.account_id,
                                platform_message_id=pid,
                                talker_id=str(talker_id),
                                incoming_text=self._private_message_text(m)[:2000],
                            )
                        except Exception:
                            pass

                    # 仅当「全部候选（含 overflow）均已离开未完结态」且无 overflow
                    # 需要后续轮次处理时，才允许 ack。overflow 存在 → 永不本轮 ack。
                    session_ack_ok = not overflow_msgs
                    max_ack_seqno = 0

                    def _msg_seq(m: Dict[str, Any]) -> int:
                        for k in ("msg_seqno", "seqno", "msg_seq", "seq_id"):
                            v = m.get(k)
                            if v is not None:
                                try:
                                    return int(v)
                                except (TypeError, ValueError):
                                    pass
                        return 0

                    for last_msg in process_batch:
                        try:
                            msg_content = self._private_message_text(last_msg)
                            if not msg_content:
                                continue

                            # PRD-V5 §6.3 / PM-501：私信幂等键使用平台消息 ID
                            platform_msg_id = extract_platform_message_id(last_msg)
                            if not platform_msg_id:
                                logger.warning("无法提取私信平台消息 ID，跳过未归档消息")
                                continue

                            pm_state = self.pm_state_store.ensure_discovered(
                                account_id=self.account_id,
                                platform_message_id=platform_msg_id,
                                talker_id=str(talker_id),
                            )

                            # V6 privacy boundary: redact and pseudonymize before brain,
                            # logs, recall, audit or model prompts.
                            try:
                                safe_pm = self._redact_private_message_runtime(
                                    msg_content,
                                    actor_id=str(talker_id),
                                    username=talker_name,
                                )
                                # Persist redacted incoming text for deferred regen
                                try:
                                    self.pm_state_store.ensure_discovered(
                                        account_id=self.account_id,
                                        platform_message_id=platform_msg_id,
                                        talker_id=str(talker_id),
                                        incoming_text=safe_pm.text or "",
                                    )
                                    # refresh state after metadata fill
                                    pm_state = self.pm_state_store.get_by_message_id(
                                        self.account_id, platform_msg_id
                                    ) or pm_state
                                except Exception:
                                    pass
                                brain = getattr(self, "memory_brain", None)
                                if brain is not None:
                                    safe_pm, archive_result = await brain.archive_private_message(
                                        platform_message_id=platform_msg_id,
                                        text=msg_content,
                                        actor_id=str(talker_id),
                                        username=talker_name,
                                        direction="incoming",
                                        persona_id=self._get_current_persona_id(),
                                        redacted=safe_pm,
                                    )
                                    if (
                                        archive_result is None
                                        or getattr(archive_result, "source_committed", True) is False
                                    ):
                                        raise RuntimeError(
                                            "PM source commit was not confirmed"
                                        )
                            except Exception:
                                self._pause_for_memory_failure()
                                self.pm_state_store.mark_deferred(
                                    pm_state.id,
                                    reason="memory_archive_failed",
                                    error_code="MEMORY_ARCHIVE_FAILED",
                                )
                                continue

                            try:
                                pm_recent_turns = await self._archive_pm_recent_history(
                                    history_messages,
                                    current_message_id=platform_msg_id,
                                    talker_id=talker_id,
                                    talker_name=talker_name,
                                    my_uid=my_uid,
                                )
                            except Exception:
                                self._pause_for_memory_failure()
                                self.pm_state_store.mark_deferred(
                                    pm_state.id,
                                    reason="pm_history_archive_failed",
                                    error_code="MEMORY_ARCHIVE_FAILED",
                                )
                                continue

                            logger.info("发现新私信: actor=%s msg_id=%s", safe_pm.actor_pseudonym, platform_msg_id)

                            # Ignored/blacklisted messages remain archived observations.
                            if self.safety_checker is not None and self.safety_checker.is_blacklisted(str(talker_id)):
                                logger.info("私信 actor=%s 命中黑名单，跳过", safe_pm.actor_pseudonym)
                                self.pm_state_store.mark_ignored(pm_state.id, rule="blacklist")
                                continue

                            # 幂等：终态或进行中则跳过（由 _process_retryable_pms 处理 retry_wait）
                            if pm_state.status in PM_TERMINAL:
                                logger.debug(
                                    f"私信已处于终态 {pm_state.status}，跳过: "
                                    f"actor={safe_pm.actor_pseudonym}"
                                )
                                continue
                            if pm_state.status not in ("discovered",):
                                # retry_wait / deferred 由独立重试循环处理；中间态跳过避免并发
                                logger.debug(
                                    f"私信状态 {pm_state.status} 非 discovered，跳过: "
                                    f"actor={safe_pm.actor_pseudonym}"
                                )
                                continue

                            # 推进：discovered → generation_pending
                            pm_state = self.pm_state_store.update_status(
                                pm_state.id, "generation_pending",
                            )

                            # 用 reply_gen 生成回复（复用评论回复逻辑）
                            # PRD-V5 §4.3 SEA-501：私信场景须传 scene=private_message
                            pm_action_key = f"private_message:{platform_msg_id}:send"
                            pm_action_type = "private_message"
                            pm_activity_started = False
                            memory_evidence = ""
                            try:
                                from bilibot.memory_brain import RecallQuery

                                brain = getattr(self, "memory_brain", None)
                                if brain is not None:
                                    recall_result = await brain.recall(
                                        RecallQuery(
                                            current_message=safe_pm.text,
                                            recent_turns=tuple(pm_recent_turns),
                                            account_id=self.account_id,
                                            speaker_actor_id=safe_pm.actor_pseudonym,
                                            scene="private_message",
                                        )
                                    )
                                    memory_evidence = recall_result.prompt_evidence
                            except Exception as exc:
                                logger.warning("私信记忆召回降级为空: %s", type(exc).__name__)

                            # Close the activity loop: begin intent before generation so
                            # recent/self memory sees "正在回复私信", then finish on any
                            # terminal outcome (publish / skip / fail / reject).
                            try:
                                pm_activity = await self._begin_activity_context(
                                    action_key=pm_action_key,
                                    action_type=pm_action_type,
                                    current_activity=(
                                        "正在回复一条已脱敏的私信，并结合对话上下文和最近经历组织自然回复。"
                                    ),
                                    query=str(safe_pm.text or "")[:800],
                                    scene="private_message",
                                    title="私信回复",
                                    metadata={
                                        "actor": safe_pm.actor_pseudonym,
                                        "platform_message_id": platform_msg_id,
                                    },
                                )
                                pm_activity_started = True
                                activity_prompt = str(
                                    getattr(pm_activity, "prompt_text", "") or ""
                                ).strip()
                                if activity_prompt and activity_prompt not in str(
                                    memory_evidence or ""
                                ):
                                    memory_evidence = "\n\n".join(
                                        item
                                        for item in (
                                            str(memory_evidence or "").strip(),
                                            activity_prompt,
                                        )
                                        if item
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "私信 activity begin 降级: %s", type(exc).__name__
                                )

                            from bilibot.models import ReplyContext
                            # 直接调用 _generate_reply_impl 以获取 GenerationOutcome，
                            # 不再通过 generate_reply 包装器（它把 skip/retryable/permanent 全折叠成 None）
                            try:
                                outcome = await self.reply_gen._generate_reply_impl(
                                    user_id=safe_pm.actor_pseudonym,
                                    username="私信用户",
                                    comment=safe_pm.text,
                                    thread_id=f"pm_{safe_pm.actor_pseudonym}",
                                    oid="",
                                    comment_type=0,
                                    reply_context=ReplyContext(memory_evidence=memory_evidence),
                                    scene="private_message",
                                )
                            except Exception as llm_err:
                                logger.error(
                                    "私信 LLM 生成失败，deferred actor=%s: %s",
                                    safe_pm.actor_pseudonym, llm_err,
                                )
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=f"私信生成失败: {type(llm_err).__name__}",
                                            published=False,
                                            status="failed",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": "LLM_ERROR",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_deferred(
                                    pm_state.id,
                                    reason=f"llm_error: {llm_err}",
                                    error_code="LLM_ERROR",
                                )
                                continue

                            if outcome.is_skip:
                                logger.debug("LLM 决定跳过私信回复 actor=%s", safe_pm.actor_pseudonym)
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text="LLM 决定跳过私信回复",
                                            published=False,
                                            status="skipped",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": outcome.error_code or "llm_no_reply",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_ignored(
                                    pm_state.id, rule=outcome.error_code or "llm_no_reply",
                                )
                                continue
                            if not outcome.is_generated:
                                if getattr(outcome, "is_permanent_error", False):
                                    logger.error(
                                        "私信 LLM 永久失败 (code=%s)，标记 failed actor=%s",
                                        outcome.error_code, safe_pm.actor_pseudonym,
                                    )
                                    if pm_activity_started:
                                        try:
                                            await self._archive_bot_action(
                                                action_key=pm_action_key,
                                                action_type=pm_action_type,
                                                text=f"私信生成永久失败: {outcome.error_code}",
                                                published=False,
                                                status="failed",
                                                title="私信回复",
                                                scene="private_message",
                                                metadata={
                                                    "actor": safe_pm.actor_pseudonym,
                                                    "reason_code": outcome.error_code or "PM_GEN_PERMANENT",
                                                },
                                            )
                                        except Exception:
                                            pass
                                    self.pm_state_store.mark_failed(
                                        pm_state.id,
                                        error_code=outcome.error_code or "PM_GEN_PERMANENT",
                                        error=f"generation_permanent: {outcome.error_code}",
                                    )
                                    continue
                                logger.warning(
                                    "私信 LLM 生成未成功 (status=%s, code=%s)，deferred actor=%s",
                                    outcome.status, outcome.error_code, safe_pm.actor_pseudonym,
                                )
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=f"私信生成未成功: {outcome.status}",
                                            published=False,
                                            status="failed",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": outcome.error_code or "GEN_FAILED",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_deferred(
                                    pm_state.id,
                                    reason=f"generation_{outcome.status}: {outcome.error_code}",
                                    error_code=outcome.error_code or "GEN_FAILED",
                                )
                                continue

                            reply_text = outcome.text
                            audit_id = outcome.audit_id  # PRD 4.16：私信审计
                            safe_reply = self._redact_private_message_runtime(
                                reply_text,
                                actor_id=str(talker_id),
                                username=talker_name,
                            )
                            safe_reply_text = safe_reply.text

                            # PRD-V5 §6.3 / PM-501：生成文本持久化（安全检查之前）
                            pm_state = self.pm_state_store.save_generation_result(
                                pm_state.id,
                                text=safe_reply_text,
                                persona_id=self._get_current_persona_id(),
                            )

                            # 推进：generation_pending → safety_pending
                            pm_state = self.pm_state_store.update_status(
                                pm_state.id, "safety_pending",
                            )

                            # 安全检查（fail-closed：无 checker 禁止发送私信）
                            if self.safety_checker is None:
                                logger.error(
                                    "safety_checker 未初始化，拒绝发送私信（fail-closed）: actor=%s",
                                    safe_pm.actor_pseudonym,
                                )
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text="私信安全检查器未初始化，拒绝发送",
                                            published=False,
                                            status="deferred",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": "NO_SAFETY_CHECKER",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_deferred(
                                    pm_state.id, reason="safety_checker_missing",
                                    error_code="NO_SAFETY_CHECKER",
                                )
                                continue
                            rate_reserved = False
                            passed, reason = await self.safety_checker.check_content(
                                safe_reply_text, scene="private_message",
                                persona_id=self._get_current_persona_id(),
                                account_id=self.account_id,
                            )
                            if not passed:
                                logger.warning(f"私信回复安全检查未通过: {reason}")
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=f"私信安全检查未通过: {reason}",
                                            published=False,
                                            status="rejected",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": "safety_check_failed",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_rejected(
                                    pm_state.id, reason=reason or "safety_check_failed",
                                )
                                continue
                            rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                                scene="private_message", account_id=self.account_id,
                            )
                            if not rate_ok:
                                logger.warning("私信频率限制触发: %s", rate_reason)
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=f"私信频率限制: {rate_reason}",
                                            published=False,
                                            status="deferred",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": "PM_RATE_LIMITED",
                                            },
                                        )
                                    except Exception:
                                        pass
                                self.pm_state_store.mark_deferred(
                                    pm_state.id, reason="rate_limited",
                                    error_code="PM_RATE_LIMITED",
                                )
                                continue
                            rate_reserved = True

                            # 推进：safety_pending → publish_pending
                            pm_state = self.pm_state_store.update_status(
                                pm_state.id, "publish_pending",
                            )

                            # 发送私信
                            try:
                                success = await self.bili.send_private_message(
                                    receiver_id=talker_id,
                                    msg=safe_reply_text,
                                )
                            except Exception as send_exc:
                                # 本地异常：平台结果不确定 → result_unknown（不自动重发）
                                # 不确定结果不退配额（可能已发出）；仅明确 False 时退
                                send_error = type(send_exc).__name__
                                logger.error(
                                    "私信发送抛异常（平台结果不确定）: actor=%s error=%s",
                                    safe_pm.actor_pseudonym,
                                    send_error,
                                )
                                self.pm_state_store.mark_result_unknown(
                                    pm_state.id,
                                    error_code="PM_SEND_EXCEPTION",
                                    error=send_error,
                                )
                                try:
                                    await self._archive_bot_action(
                                        action_key=f"private_message:{platform_msg_id}:send",
                                        action_type="private_message",
                                        text=safe_reply_text,
                                        published=False,
                                        status="result_unknown",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "PM_SEND_EXCEPTION",
                                        },
                                    )
                                except Exception:
                                    logger.error(
                                        "unknown PM result could not be archived: actor=%s",
                                        safe_pm.actor_pseudonym,
                                    )
                                # 审计记录失败
                                if audit_id and self.audit_store:
                                    try:
                                        self.audit_store.mark_published(
                                            audit_id, published=False,
                                            failure_reason=f"send_exception: {send_error}",
                                        )
                                    except Exception:
                                        pass
                                continue

                            if success is None:
                                # Transport uncertainty: no refund, no auto-resend
                                logger.error(
                                    "私信结果不确定（不自动重发）: actor=%s",
                                    safe_pm.actor_pseudonym,
                                )
                                self.pm_state_store.mark_result_unknown(
                                    pm_state.id,
                                    error_code="RESULT_UNKNOWN",
                                    error="send_private_message transport uncertainty",
                                )
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=str(safe_reply_text or "")[:200]
                                            or "私信发送结果不确定",
                                            published=False,
                                            status="result_unknown",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "reason_code": "RESULT_UNKNOWN",
                                            },
                                        )
                                    except Exception:
                                        logger.debug(
                                            "PM result_unknown finish failed",
                                            exc_info=True,
                                        )
                                continue

                            # PRD 4.16：私信审计记录
                            if audit_id and self.audit_store:
                                try:
                                    if success:
                                        self.audit_store.mark_published(
                                            audit_id, published=True,
                                            target={"kind": "private_message", "actor": safe_pm.actor_pseudonym},
                                        )
                                    else:
                                        self.audit_store.mark_published(
                                            audit_id, published=False, failure_reason="send_private_message failed",
                                        )
                                except Exception:
                                    pass

                            if success:
                                logger.info("已回复私信 actor=%s msg_id=%s", safe_pm.actor_pseudonym, platform_msg_id)
                                # PRD-V5 §6.3 / PM-501：标记已发布（终态）
                                self.pm_state_store.mark_published(pm_state.id)
                                try:
                                    brain = getattr(self, "memory_brain", None)
                                    if brain is not None:
                                        _, archive_result = await brain.archive_private_message(
                                            platform_message_id=platform_msg_id,
                                            text=safe_reply_text,
                                            actor_id=str(talker_id),
                                            username=talker_name,
                                            direction="outgoing",
                                            persona_id=self._get_current_persona_id(),
                                            redacted=safe_reply,
                                        )
                                        if (
                                            archive_result is None
                                            or getattr(archive_result, "source_committed", True) is False
                                        ):
                                            raise RuntimeError(
                                                "PM outgoing source commit was not confirmed"
                                            )
                                except Exception:
                                    self._pause_for_memory_failure()
                                    logger.error(
                                        "published PM result could not be archived: actor=%s",
                                        safe_pm.actor_pseudonym,
                                    )
                                # Always close activity + continuous self after a real send,
                                # even if durable PM observation archive failed.
                                if pm_activity_started:
                                    try:
                                        await self._archive_bot_action(
                                            action_key=pm_action_key,
                                            action_type=pm_action_type,
                                            text=safe_reply_text,
                                            published=True,
                                            status="completed",
                                            title="私信回复",
                                            scene="private_message",
                                            metadata={
                                                "actor": safe_pm.actor_pseudonym,
                                                "platform_message_id": platform_msg_id,
                                            },
                                        )
                                    except Exception:
                                        logger.debug(
                                            "PM success finish_activity failed",
                                            exc_info=True,
                                        )
                                self._notify_companion_private_message_replied(
                                    actor_label=str(safe_pm.actor_pseudonym or "")[:24],
                                )
                                if self.safety_checker is not None:
                                    self.safety_checker.record_content(
                                        safe_reply_text, account_id=self.account_id
                                    )
                            else:
                                # PRD-V5 §6.3 / PM-501：发布失败 → retry_wait（独立退避）
                                # 平台明确返回失败（非本地异常），按 retry_wait 处理
                                if rate_reserved and self.safety_checker is not None:
                                    try:
                                        self.safety_checker.refund_publish(
                                            scene="private_message", account_id=self.account_id,
                                        )
                                    except Exception:
                                        pass
                                logger.warning("私信发送失败 actor=%s", safe_pm.actor_pseudonym)
                                self.pm_state_store.mark_retry_wait(
                                    pm_state.id,
                                    error_code="PM_PUBLISH_FAILED",
                                    error="send_private_message returned False",
                                )
                                try:
                                    await self._archive_bot_action(
                                        action_key=f"private_message:{platform_msg_id}:send",
                                        action_type="private_message",
                                        text=safe_reply_text,
                                        published=False,
                                        status="failed",
                                        title="私信回复",
                                        scene="private_message",
                                        metadata={
                                            "actor": safe_pm.actor_pseudonym,
                                            "reason_code": "PM_PUBLISH_FAILED",
                                        },
                                    )
                                except Exception:
                                    logger.error(
                                        "failed PM result could not be archived: actor=%s",
                                        safe_pm.actor_pseudonym,
                                    )
                        except Exception as msg_exc:
                            session_ack_ok = False
                            logger.warning(
                                "处理单条私信异常 talker=%s: %s",
                                talker_id, type(msg_exc).__name__,
                            )
                        else:
                            try:
                                max_ack_seqno = max(max_ack_seqno, _msg_seq(last_msg))
                            except Exception:
                                pass

                    # P0-A：仅当无 overflow、本轮处理无异常、且全部候选（含 overflow
                    # 入库的）均已离开 discovered/中间态 时才 ack；并带真实 ack_seqno。
                    if session_ack_ok and candidate_msgs and not overflow_msgs:
                        try:
                            all_settled = True
                            for m in candidate_msgs:
                                pid = extract_platform_message_id(m)
                                if not pid:
                                    all_settled = False
                                    break
                                st = self.pm_state_store.get_by_message_id(
                                    self.account_id, pid
                                )
                                if st is None or st.status in (
                                    "discovered",
                                    "generation_pending",
                                    "safety_pending",
                                    "publish_pending",
                                ):
                                    all_settled = False
                                    break
                                try:
                                    max_ack_seqno = max(max_ack_seqno, _msg_seq(m))
                                except Exception:
                                    pass
                            if all_settled:
                                await self.bili.ack_session(
                                    talker_id,
                                    session_type=1,
                                    ack_seqno=int(max_ack_seqno or 0),
                                )
                        except Exception:
                            pass

                except Exception as e:
                    logger.warning("处理私信会话异常: %s", type(e).__name__)

        except Exception as e:
            logger.error("私信检查异常: %s", type(e).__name__)

    async def _process_retryable_pms(self):
        """PRD-V5 §6.3 / PM-501：重试 retry_wait / deferred 状态的私信

        - retry_wait：使用已保存的 generation_text 重新发送（不重新生成）
        - deferred：有 gen text 则安全复检后发送；无 gen text 则重新生成
        - 超过 max_attempts → failed（由 mark_retry_wait 自动判定）
        - result_unknown 不自动重发（需人工对账）
        """
        if not self.bili or not self.reply_gen:
            return
        try:
            retryable = self.pm_state_store.list_retryable(account_id=self.account_id)
            if not retryable:
                return
            logger.info(f"发现 {len(retryable)} 条待重试私信")
            for pm_state in retryable:
                try:
                    retry_actor = "actor_unknown"
                    try:
                        retry_actor = self._redact_private_message_runtime(
                            "", actor_id=pm_state.talker_id or "unknown"
                        ).actor_pseudonym or retry_actor
                    except Exception:
                        pass

                    talker_id = 0
                    try:
                        talker_id = int(pm_state.talker_id or 0)
                    except (TypeError, ValueError):
                        talker_id = 0
                    if not talker_id:
                        self.pm_state_store.mark_failed(
                            pm_state.id, error_code="PM_NO_TALKER",
                            error="no talker_id for retry",
                        )
                        continue

                    reply_text = pm_state.generation_text or ""
                    gen_hash = pm_state.generation_hash or ""
                    status = pm_state.status or ""

                    # deferred 且无文本：重新生成（优先使用持久化的 incoming_text）
                    if status == "deferred" and not reply_text:
                        incoming = ""
                        try:
                            meta = getattr(pm_state, "metadata", None) or {}
                            if isinstance(meta, dict):
                                incoming = str(meta.get("incoming_text") or "")
                        except Exception:
                            incoming = ""
                        if not incoming.strip():
                            self.pm_state_store.mark_failed(
                                pm_state.id,
                                error_code="PM_NO_INCOMING",
                                error="deferred regen missing incoming_text",
                            )
                            continue
                        # 与首次一致：用脱敏伪名作 user_id，避免真实 UID 进入 prompt/记忆边界
                        regen_user_id = retry_actor if retry_actor != "actor_unknown" else str(talker_id)
                        try:
                            outcome = await self.reply_gen._generate_reply_impl(
                                user_id=regen_user_id,
                                username="私信用户",
                                comment=incoming,
                                thread_id=f"pm_{regen_user_id}",
                                oid="",
                                comment_type=0,
                                scene="private_message",
                            )
                        except Exception as gen_err:
                            logger.error(
                                "deferred 私信重生成异常 actor=%s: %s",
                                retry_actor, type(gen_err).__name__,
                            )
                            self.pm_state_store.mark_deferred(
                                pm_state.id,
                                reason=f"regen_exception: {type(gen_err).__name__}",
                                error_code="PM_REGEN_EXCEPTION",
                            )
                            continue
                        if outcome.is_skip:
                            # deferred → ignored（状态机已允许）
                            self.pm_state_store.mark_ignored(
                                pm_state.id, rule=outcome.error_code or "llm_no_reply",
                            )
                            continue
                        if not outcome.is_generated:
                            if getattr(outcome, "is_permanent_error", False):
                                self.pm_state_store.mark_failed(
                                    pm_state.id,
                                    error_code=outcome.error_code or "PM_REGEN_PERMANENT",
                                    error=f"regen_permanent: {outcome.error_code}",
                                )
                                continue
                            self.pm_state_store.mark_deferred(
                                pm_state.id,
                                reason=f"regen_{outcome.status}: {outcome.error_code}",
                                error_code=outcome.error_code or "PM_REGEN_EMPTY",
                            )
                            continue
                        reply_text = outcome.text
                        safe_regen = self._redact_private_message_runtime(
                            reply_text, actor_id=str(talker_id),
                        )
                        reply_text = safe_regen.text
                        pm_state = self.pm_state_store.save_generation_result(
                            pm_state.id,
                            text=reply_text,
                            persona_id=self._get_current_persona_id(),
                        )
                        gen_hash = pm_state.generation_hash or ""

                    if not reply_text:
                        self.pm_state_store.mark_deferred(
                            pm_state.id, reason="no_generation_text",
                            error_code="RETRY_NO_GEN",
                        )
                        continue
                    if gen_hash:
                        expected = self.pm_state_store.compute_generation_hash(reply_text)
                        if expected != gen_hash:
                            # P1-2：hash 不匹配不再死循环 deferred，直接 failed
                            logger.warning("重试私信文本 hash 不匹配: actor=%s", retry_actor)
                            self.pm_state_store.mark_failed(
                                pm_state.id,
                                error_code="RETRY_HASH_MISMATCH",
                                error="hash_mismatch",
                            )
                            continue

                    safe_retry = self._redact_private_message_runtime(
                        reply_text, actor_id=str(talker_id),
                    )
                    safe_retry_text = safe_retry.text

                    # fail-closed 安全 + 限流（先进入 safety_pending，保证 mark_rejected 合法）
                    if self.safety_checker is None:
                        logger.error(
                            "safety_checker 未初始化，拒绝重试私信（fail-closed）: actor=%s",
                            retry_actor,
                        )
                        self.pm_state_store.mark_deferred(
                            pm_state.id, reason="safety_checker_missing",
                            error_code="NO_SAFETY_CHECKER",
                        )
                        continue
                    try:
                        if (pm_state.status or "") != "safety_pending":
                            pm_state = self.pm_state_store.update_status(
                                pm_state.id, "safety_pending",
                            )
                    except ValueError as te:
                        logger.error(
                            "重试私信无法进入 safety_pending (status=%s): %s",
                            status, te,
                        )
                        self.pm_state_store.mark_deferred(
                            pm_state.id,
                            reason=f"enter_safety_pending_failed: {te}",
                            error_code="PM_STATE_ERROR",
                        )
                        continue
                    rate_reserved = False
                    try:
                        passed, reason = await self.safety_checker.check_content(
                            safe_retry_text, scene="private_message",
                            persona_id=self._get_current_persona_id(),
                            account_id=self.account_id,
                        )
                        if not passed:
                            logger.warning("重试私信安全检查未通过: %s", reason)
                            self.pm_state_store.mark_rejected(
                                pm_state.id, reason=reason or "safety_check_failed",
                            )
                            continue
                        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                            scene="private_message", account_id=self.account_id,
                        )
                        if not rate_ok:
                            logger.warning("重试私信频率限制触发: %s", rate_reason)
                            self.pm_state_store.mark_deferred(
                                pm_state.id, reason="rate_limited",
                                error_code="PM_RATE_LIMITED",
                            )
                            continue
                        rate_reserved = True
                    except Exception as se:
                        logger.error("重试私信安全检查异常: %s", se, exc_info=True)
                        self.pm_state_store.mark_deferred(
                            pm_state.id,
                            reason=f"safety_exception: {type(se).__name__}",
                            error_code="SAFETY_ERROR",
                        )
                        continue

                    # 推进：safety_pending → publish_pending
                    self.pm_state_store.update_status(pm_state.id, "publish_pending")

                    try:
                        success = await self.bili.send_private_message(
                            receiver_id=talker_id, msg=safe_retry_text,
                        )
                    except Exception as send_exc:
                        # Uncertain: no refund
                        send_error = type(send_exc).__name__
                        logger.error(
                            "重试私信发送抛异常: actor=%s error=%s",
                            retry_actor, send_error,
                        )
                        self.pm_state_store.mark_result_unknown(
                            pm_state.id,
                            error_code="PM_RETRY_SEND_EXCEPTION",
                            error=send_error,
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"private_message:{pm_state.platform_message_id}:"
                                    f"retry:{pm_state.attempt + 1}"
                                ),
                                action_type="private_message",
                                text=safe_retry_text,
                                published=False,
                                status="result_unknown",
                                title="私信回复重试",
                                scene="private_message",
                                metadata={
                                    "actor": retry_actor,
                                    "reason_code": "PM_RETRY_SEND_EXCEPTION",
                                },
                            )
                        except Exception:
                            logger.error(
                                "unknown retried PM result could not be archived: actor=%s",
                                retry_actor,
                            )
                        continue

                    if success is None:
                        logger.error(
                            "重试私信结果不确定（不自动重发）: actor=%s", retry_actor,
                        )
                        self.pm_state_store.mark_result_unknown(
                            pm_state.id,
                            error_code="RESULT_UNKNOWN",
                            error="send_private_message transport uncertainty",
                        )
                        continue

                    if success:
                        self.pm_state_store.mark_published(pm_state.id)
                        logger.info("重试私信发送成功: actor=%s", retry_actor)
                        pm_retry_archived = True
                        try:
                            brain = getattr(self, "memory_brain", None)
                            if brain is not None:
                                _, archive_result = await brain.archive_private_message(
                                    platform_message_id=pm_state.platform_message_id,
                                    text=safe_retry_text,
                                    actor_id=str(talker_id),
                                    direction="outgoing",
                                    persona_id=self._get_current_persona_id(),
                                    redacted=safe_retry,
                                )
                                if (
                                    archive_result is None
                                    or getattr(archive_result, "source_committed", True) is False
                                ):
                                    raise RuntimeError(
                                        "PM retry outgoing source commit was not confirmed"
                                    )
                        except Exception:
                            pm_retry_archived = False
                            self._pause_for_memory_failure()
                            logger.error(
                                "重试私信结果归档失败: actor=%s", retry_actor
                            )
                        self._notify_companion_private_message_replied(
                            actor_label=str(retry_actor or "")[:24],
                        )
                        if self.safety_checker is not None:
                            try:
                                self.safety_checker.record_content(
                                    safe_retry_text, account_id=self.account_id,
                                )
                            except Exception:
                                pass
                        try:
                            await self.bili.ack_session(talker_id, session_type=1)
                        except Exception:
                            pass
                    else:
                        if rate_reserved and self.safety_checker is not None:
                            try:
                                self.safety_checker.refund_publish(
                                    scene="private_message", account_id=self.account_id,
                                )
                            except Exception:
                                pass
                        self.pm_state_store.mark_retry_wait(
                            pm_state.id,
                            error_code="PM_RETRY_PUBLISH_FAILED",
                            error="retry send_private_message returned False",
                        )
                        try:
                            await self._archive_bot_action(
                                action_key=(
                                    f"private_message:{pm_state.platform_message_id}:"
                                    f"retry:{pm_state.attempt + 1}"
                                ),
                                action_type="private_message",
                                text=safe_retry_text,
                                published=False,
                                status="failed",
                                title="私信回复重试",
                                scene="private_message",
                                metadata={
                                    "actor": retry_actor,
                                    "reason_code": "PM_RETRY_PUBLISH_FAILED",
                                },
                            )
                        except Exception:
                            logger.error(
                                "failed retried PM result could not be archived: actor=%s",
                                retry_actor,
                            )
                        logger.warning("重试私信发送失败: actor=%s", retry_actor)
                except Exception as e:
                    logger.error(
                        "重试私信失败: actor=%s error=%s",
                        retry_actor, type(e).__name__,
                    )
        except Exception as e:
            logger.error("处理重试私信失败: %s", type(e).__name__)

    # ══════════════════════════════════════════
    #  主动行为
    # ══════════════════════════════════════════

    async def _check_proactive_tasks(self, current_time: str):
        """检查并执行主动任务

        PRD-V5 §7 / TASK-501：通过 TaskRunStore 管理生命周期。
        - 时间匹配时通过 claim() 原子获取任务（避免重启重复触发）
        - claim 成功后 spawn 后台协程，协程内 start/succeed/fail
        - 创建协程 ≠ 成功；不写 triggered 直到真正 succeed
        """
        features = self.config_loader.get_raw_config().get("features", {})

        # MotiveQueue soft gate: when companion ranks "rest" highest with a
        # strong score, skip spawning new proactive video/dynamic this tick.
        # Scheduled TaskRuns remain claimable later when energy recovers.
        motive_rest = False
        companion = getattr(self, "companion", None)
        if companion is not None and getattr(companion, "enabled", False):
            try:
                select = getattr(companion, "select_motive", None)
                if callable(select):
                    top = select()
                    if top is not None and str(getattr(top, "suggested_action", "")) == "rest":
                        if float(getattr(top, "score", 0) or 0) >= 6.0:
                            motive_rest = True
                            logger.info(
                                "MotiveQueue rest gate: skip proactive spawn (score=%.1f)",
                                float(top.score),
                            )
            except Exception:
                motive_rest = False

        # PRD V4 COM-001：proactive_video 和 proactive_comment 解耦
        # proactive_video 控制视频获取/分析/评价/记忆；proactive_comment 只控制是否发布主动评论
        # 两个开关不再以 AND 方式决定整个视频任务是否运行
        if features.get("proactive_video", True) and not motive_rest:
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
                        self._spawn_memory_task(
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

        # 发布动态
        if features.get("dynamic_post", True) and not motive_rest:
            for trigger_time in self._dynamic_times:
                time_str = f"{trigger_time[0]:02d}:{trigger_time[1]:02d}"
                if time_str == current_time and current_time not in self._dynamic_triggered:
                    # PRD-V5 §7：原子 claim
                    task_id = self._claim_task_for_slot("dynamic", time_str)
                    if task_id:
                        # PRD V3 §4.1：发布动态改为异步，不阻塞主循环
                        # 配图链路（LLM生成prompt + 图片生成120s + 上传60s）可能卡3分钟
                        self._spawn_memory_task(
                            self._do_post_dynamic(task_id=task_id),
                            tag="_do_post_dynamic",
                        )
                        self._dynamic_triggered.add(current_time)
                        self._save_dynamic_schedule_state()
                    else:
                        self._dynamic_triggered.add(current_time)
                        self._save_dynamic_schedule_state()
                    break

    def _claim_task_for_slot(self, scene: str, slot: str) -> Optional[str]:
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

    def _task_exists_for_slot(self, scene: str, slot: str) -> bool:
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

    async def _watch_video_for_reply(self, bvid: str, oid) -> bool:
        """为 @我 的视频评论预先观看并归档视频

        被用户 @ 后，回复前先完整看一遍视频（下载+视听分析+归档到 memory_brain），
        这样后续 comment_context_service.build_context 能从 V6 缓存命中完整视频上下文，
        让回复基于真实观看内容而非仅元数据。

        bvid 可为空，此时从 video_info.bvid 补齐。

        Returns:
            True 表示已观看并归档成功（或已存在缓存），False 表示失败（调用方降级为元数据回复）
        """
        if not self.bili:
            return False
        # 视频理解服务不可用时直接降级
        if not (self.video_understanding and self.video_understanding.is_available()):
            logger.info("@回复：视频理解服务不可用，跳过预观看，降级为元数据回复")
            return False

        try:
            oid_int = int(oid) if oid else 0
            if not oid_int and bvid:
                oid_int = await self.bili.get_video_oid_by_bvid(bvid)
            if not oid_int:
                logger.warning(f"@回复：获取 oid 失败 bvid={bvid}")
                return False

            # 检查 memory_brain 是否已有完整视频观察缓存（含视听分析，非仅元数据）
            brain = getattr(self, "memory_brain", None)
            if brain is not None and self.comment_context_service is not None:
                try:
                    # find_by_identifiers 返回所有匹配 oid 的 event，
                    # 需要检查是否有完整 video_observation（含 audiovisual 视听分析）
                    hits = await asyncio.to_thread(
                        brain.find_by_identifiers, [str(oid_int)], 20
                    )
                    has_full_observation = False
                    for hit in hits:
                        event_id = str(hit.get("event_id") or hit.get("id") or "")
                        if not event_id:
                            continue
                        event = await asyncio.to_thread(brain.get_event, event_id, None)
                        if not event:
                            continue
                        event_meta = event.get("metadata") or {}
                        if str(event_meta.get("oid", "")) != str(oid_int):
                            continue
                        # 检查是否为完整 video_observation（含视听分析，非仅元数据）
                        # event_type 为 video_observation 且存在 asr/visual_description/behavior_log 来源
                        if self._event_is_full_video_watch(event):
                            has_full_observation = True
                            break
                    if has_full_observation:
                        logger.info(f"@回复：视频已完整观看过 oid={oid_int}，复用缓存")
                        return True
                    logger.info(f"@回复：视频 oid={oid_int} 仅有元数据缓存，需完整观看")
                except Exception:
                    pass

            video_info = await self.bili.get_video_info(oid_int)
            if not video_info:
                logger.warning(f"@回复：获取视频详情失败 oid={oid_int}")
                return False

            # bvid 补齐
            if not bvid:
                bvid = video_info.get("bvid", "") or ""
            if not bvid:
                logger.warning(f"@回复：视频缺少 bvid oid={oid_int}")
                return False

            title = video_info.get("title", "未知视频")
            owner = video_info.get("owner", {}).get("name", "未知UP")
            desc = video_info.get("desc", "")
            tags_list = await self.bili.get_video_tags(bvid, video_info=video_info) or []
            if isinstance(tags_list, str):
                tags_list = [t.strip() for t in tags_list.split(",") if t.strip()]

            logger.info(f"@回复：预观看视频 《{title}》 by {owner}")

            ctx = ProactiveVideoContext(bvid=bvid)
            ctx.metadata = video_info
            hot_comments = await self.bili.get_hot_comments(oid_int, limit=5) or []
            ctx.hot_comments = hot_comments if hot_comments else None

            # 联网搜索参考
            if self.web_search and self.web_search.is_available():
                try:
                    search_query = await self.web_search.should_search_for_video(
                        video_info={"title": title, "desc": desc,
                                    "tname": tags_list[0] if tags_list else "",
                                    "owner_name": owner},
                        scene="proactive_video",
                    )
                    if search_query:
                        search_result = await self.web_search.search(
                            search_query, scene="proactive_video",
                        )
                        if search_result:
                            ctx.search_reference = search_result
                except Exception as e:
                    ctx.degradation_reasons.append(f"search_failed: {e}")

            # 视频内容理解（视听双轨分析）
            video_file_to_cleanup = None
            work_dir_to_cleanup = None
            archive_committed = False
            try:
                cid = video_info.get("cid", 0)
                if not cid:
                    pages = video_info.get("pages", [])
                    if pages:
                        cid = pages[0].get("cid", 0)
                if not cid:
                    raise RuntimeError("视频缺少 CID")

                import os as _os
                from bilibot.video_understanding.cleanup import cleanup_media_artifacts

                video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
                save_path = _os.path.join(video_temp_dir, f"{bvid}")
                video_file = await self.bili.download_video(bvid, cid, save_path, quality=32)
                if not video_file or not _os.path.exists(video_file):
                    raise RuntimeError(f"视频下载失败: {bvid}")

                logger.info(f"@回复：视频已下载，开始视听分析: {video_file}")
                video_file_to_cleanup = video_file
                try:
                    vu_result = await self.video_understanding.understand(
                        video_file, defer_cleanup=True,
                        require_complete_audio=True, require_complete_visual=True,
                    )
                    if not isinstance(vu_result, dict):
                        raise RuntimeError("视频理解返回了无效结果")

                    work_dir_to_cleanup = vu_result.get("work_dir") or None
                    degradation = str(vu_result.get("degradation_reason") or "")
                    audio_status = vu_result.get("audio_status") or {}
                    audio_failed = (
                        isinstance(audio_status, dict)
                        and audio_status.get("status") == "failed"
                    )
                    if audio_failed:
                        reason = str(audio_status.get("error_code") or "audio_track_failed")
                        raise RuntimeError(f"视频提取未完成: {reason}")
                    if degradation:
                        ctx.degradation_reasons.append(f"video_understanding: {degradation}")
                        logger.info(f"@回复：视频理解降级，继续归档: {degradation}")

                    ctx.audiovisual = vu_result
                    av_log = ctx.audiovisual_log
                    if av_log:
                        logger.info(f"@回复：视频理解完成，行为日志 {len(av_log)} 字")
                    elif not degradation:
                        logger.warning("@回复：视频理解未生成行为日志")
                except ASRTranscriptionError as e:
                    work_dir_to_cleanup = getattr(e, "work_dir", None) or work_dir_to_cleanup
                    logger.warning("@回复：视频 ASR 失败 code=%s retryable=%s，降级为元数据回复",
                                   e.code, e.retryable)
                    return False
                except Exception as e:
                    work_dir_to_cleanup = getattr(e, "work_dir", None) or work_dir_to_cleanup
                    logger.warning("@回复：视频理解失败，降级为元数据回复: %s: %s",
                                   type(e).__name__, e)
                    return False

                # 归档到 memory_brain（让后续 build_context 命中缓存）
                try:
                    from bilibot.memory_brain.ingestion import video_observation

                    video_detail = await self._build_video_detail_digest(
                        title=title,
                        owner=owner,
                        behavior_log=ctx.audiovisual_log or "",
                        max_attempts=2,
                        # @预观看针对指定视频，不能换片；摘要失败时允许启发式降级。
                        require_llm=False,
                    )
                    await self._archive_required(
                        video_observation(
                            account_id=self.account_id or "default",
                            observation_key=f"at_reply:{bvid}:{oid_int}",
                            bvid=bvid,
                            oid=str(oid_int),
                            title=title,
                            owner=owner,
                            context=ctx.to_dict(),
                            tags=tags_list,
                            persona_id=self._get_current_persona_id(),
                            video_detail=video_detail,
                        ),
                        treat_idempotent_as_ready=True,
                    )
                    archive_committed = True
                    logger.info(f"@回复：视频已归档 bvid={bvid} oid={oid_int}")
                except Exception as archive_exc:
                    logger.warning(f"@回复：视频归档失败: {archive_exc}")
                    return False

                return True
            finally:
                from bilibot.video_understanding.cleanup import (
                    cleanup_media_artifacts,
                    schedule_cleanup,
                )

                if archive_committed:
                    cleanup_media_artifacts(
                        video_file_to_cleanup, work_dir_to_cleanup
                    )
                else:
                    retained = [
                        p for p in (video_file_to_cleanup, work_dir_to_cleanup) if p
                    ]
                    if retained:
                        schedule_cleanup(retained, delay_seconds=1800)
                        logger.warning(
                            "@回复视频证据尚未归档，保留 1800 秒: bvid=%s paths=%s",
                            bvid,
                            len(retained),
                        )
        except Exception as e:
            logger.warning(f"@回复：预观看视频异常: {e}")
            return False

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
        - 平台结果不确定时 mark_result_unknown（不自动重发）

        同账号串行：若上一条主动看视频仍在执行，本条在锁上排队等待（不丢 slot、不叠跑）。
        """
        logger.info("开始主动看视频...")
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
        """主动看视频主体（调用方已持有同账号串行锁）。"""
        # PRD V4 COM-001：features 在方法内独立读取（与 _check_proactive_tasks 解耦）
        features = self.config_loader.get_raw_config().get("features", {})

        try:
            # 1. 获取视频（C: 推荐流 / D: 分区热门随机翻页，各 50% 概率）
            # 检查是否有中断时正在处理的 bvid（重启恢复场景）
            saved_bvid = ""
            if task_id:
                try:
                    _task = self.task_store.get(task_id)
                    if _task and _task.input_json:
                        _input = json.loads(_task.input_json)
                        saved_bvid = str(_input.get("bvid") or "").strip()
                except Exception:
                    pass

            # 陪伴层：日程若在「刷 B 站/看视频」时段，略提高推荐流占比（更像「按心情刷」）
            companion = getattr(self, "companion", None)
            prefer_browse = bool(
                companion is not None
                and getattr(companion, "enabled", False)
                and hasattr(companion, "wants_browse_bilibili_now")
                and companion.wants_browse_bilibili_now()
            )
            if prefer_browse:
                source = "recommend" if random.random() < 0.72 else "region"
                logger.info("陪伴日程偏向刷站，视频来源权重偏向推荐流")
            else:
                source = random.choice(["recommend", "region"])
            if source == "recommend":
                try:
                    data = await self.bili.get_recommend_videos()
                except Exception as exc:
                    # Feed endpoints can fail independently or be absent on an
                    # older API adapter. Keep the hot-feed fallback reachable.
                    logger.warning(
                        "recommend feed unavailable, falling back to hot feed: %s",
                        type(exc).__name__,
                    )
                    data = None
                logger.info("视频来源: 推荐流")
            else:
                page = random.randint(1, 5)
                try:
                    data = await self.bili.get_region_hot_videos(rid=0, page=page)
                except Exception as exc:
                    logger.warning(
                        "region feed unavailable, falling back to hot feed: %s",
                        type(exc).__name__,
                    )
                    data = None
                logger.info(f"视频来源: 分区热门 (page={page})")
            if not data or not data.get("data"):
                # 推荐流/分区失败时回退到热门视频
                logger.warning("推荐流/分区热门获取失败，回退到热门视频")
                data = await self.bili.get_hot_videos()
            if not data or not data.get("data"):
                if task_id:
                    self._fail_task(task_id, "NO_HOT_VIDEOS", "获取视频失败", retryable=False)
                return
            videos = data["data"].get("list", [])
            if not videos:
                if task_id:
                    self._fail_task(task_id, "NO_HOT_VIDEOS", "视频列表为空", retryable=False)
                return

            # 仅「评价/互动闭环完成」才跳过：仅有 video_observation 不够
            # （归档在评价之前；中途失败必须允许重试补互动）。
            available_videos = []
            for candidate in videos:
                bvid_value = str(candidate.get("bvid") or "").strip()
                if not bvid_value:
                    continue
                if await self._has_completed_proactive_video(bvid_value):
                    continue
                available_videos.append(candidate)

            # 中断恢复：在「空列表提前 return」之前强制插入 saved_bvid
            # （否则 feed 为空/不全时永远轮不到中断视频）
            if saved_bvid:
                if await self._has_completed_proactive_video(saved_bvid):
                    logger.info(f"中断视频 {saved_bvid} 已完成闭环，不再优先")
                    saved_bvid = ""
                else:
                    saved_idx = next(
                        (
                            i
                            for i, v in enumerate(available_videos)
                            if str(v.get("bvid") or "") == saved_bvid
                        ),
                        -1,
                    )
                    if saved_idx >= 0:
                        saved_video = available_videos.pop(saved_idx)
                        available_videos.insert(0, saved_video)
                        logger.info(f"恢复中断的视频: {saved_bvid}")
                    else:
                        available_videos.insert(0, {"bvid": saved_bvid})
                        logger.info(
                            f"恢复中断的视频（不在当前 feed，强制优先）: {saved_bvid}"
                        )

            if not available_videos:
                logger.info("所有候选视频均已完成主动观看闭环，跳过主动看视频")
                if task_id:
                    self._fail_task(
                        task_id,
                        "ALL_WATCHED",
                        "所有候选视频均已完成主动观看闭环",
                        retryable=False,
                    )
                return

            # 随机打乱候选；再按陪伴兴趣/探索笔记软排序（更像「按兴趣点开」）
            if saved_bvid and available_videos and str(
                available_videos[0].get("bvid") or ""
            ) == saved_bvid:
                rest = available_videos[1:]
                random.shuffle(rest)
                if companion is not None and getattr(companion, "enabled", False) and hasattr(companion, "rank_video_candidates"):
                    try:
                        rest = companion.rank_video_candidates(rest)
                    except Exception as e:
                        logger.debug("companion rank videos failed: %s", e)
                available_videos[1:] = rest
            else:
                random.shuffle(available_videos)
                if companion is not None and getattr(companion, "enabled", False) and hasattr(companion, "rank_video_candidates"):
                    try:
                        available_videos = companion.rank_video_candidates(available_videos)
                        logger.info("已按陪伴兴趣软排序视频候选")
                    except Exception as e:
                        logger.debug("companion rank videos failed: %s", e)
            max_video_attempts = min(3, len(available_videos))
            last_skip_reason = ""

            for video_attempt, video in enumerate(available_videos[:max_video_attempts], start=1):
                bvid = video.get("bvid", "")
                if not bvid:
                    last_skip_reason = "NO_BVID"
                    continue

                # 持久化正在处理的 bvid，重启后可优先重试同一视频
                if task_id:
                    try:
                        self.task_store.update_input(task_id, {"bvid": bvid, "scene": "proactive_video"})
                    except Exception:
                        pass

                oid = await self.bili.get_video_oid_by_bvid(bvid)
                if not oid:
                    last_skip_reason = "NO_OID"
                    logger.warning(
                        "获取 oid 失败，换视频 attempt=%s/%s bvid=%s",
                        video_attempt,
                        max_video_attempts,
                        bvid,
                    )
                    continue

                # 2. 获取视频详情
                video_info = await self.bili.get_video_info(oid)
                if not video_info:
                    last_skip_reason = "NO_VIDEO_INFO"
                    logger.warning(
                        "获取视频详情失败，换视频 attempt=%s/%s bvid=%s",
                        video_attempt,
                        max_video_attempts,
                        bvid,
                    )
                    continue

                title = video_info.get("title", "未知视频")
                owner = video_info.get("owner", {}).get("name", "未知UP")
                owner_mid = str(video_info.get("owner", {}).get("mid", ""))
                desc = video_info.get("desc", "")

                # 获取标签；标签接口异常时复用已拿到的详情/热门分区元数据。
                tag_metadata = dict(video_info)
                for field in ("tags", "tag", "tname", "tname_v2", "tnamev2", "pid_name_v2"):
                    if not tag_metadata.get(field) and video.get(field):
                        tag_metadata[field] = video[field]
                tags_list = await self.bili.get_video_tags(bvid, video_info=tag_metadata) or []
                if isinstance(tags_list, str):
                    tags_list = [t.strip() for t in tags_list.split(",") if t.strip()]

                # 获取热门评论
                hot_comments = await self.bili.get_hot_comments(oid, limit=5) or []

                logger.info(
                    f"正在看视频: 《{title}》 by {owner} "
                    f"(candidate {video_attempt}/{max_video_attempts})"
                )

                # PRD-V5 VID-502：结构化视频上下文 — 各来源独立赋值，不互相覆盖
                # 修复 scheduler.py 旧实现复用 video_content 字符串导致搜索结果被视听分析覆盖的 bug
                ctx = ProactiveVideoContext(bvid=bvid)
                ctx.metadata = video_info
                ctx.hot_comments = hot_comments if hot_comments else None
                video_file_to_cleanup = None
                work_dir_to_cleanup = None
                artifact_cleanup_ready = False
                # 必须按 bvid/oid 做幂等键，不能用 task_id：
                # 任务重试时可能换片；若 key 绑 task_id，新片会与旧归档冲突，
                # 再被 treat_idempotent_as_ready 误当成「已就绪」继续评价错误视频。
                observation_key = f"{bvid}:{oid}"
                video_content = ""  # 评价/评论输入；有 digest 后优先用 digest
                video_detail = ""
                skip_this_video = False

                try:
                    # 来源 1：联网搜索（UNTRUSTED Reference Block）— 失败不清空其他来源
                    if self.web_search and self.web_search.is_available():
                        try:
                            search_query = await self.web_search.should_search_for_video(
                                video_info={
                                    "title": title,
                                    "desc": desc,
                                    "tname": tags_list[0] if tags_list else "",
                                    "owner_name": owner,
                                },
                                scene="proactive_video",
                            )
                            if search_query:
                                # PRD-V5 VID-502：存储结构化搜索结果，由 to_prompt_sections() 统一格式化
                                search_result = await self.web_search.search(
                                    search_query, scene="proactive_video",
                                )
                                if search_result:
                                    ctx.search_reference = search_result
                        except Exception as e:
                            ctx.degradation_reasons.append(f"search_failed: {e}")

                    # 来源 2：视频内容理解（视听双轨分析）
                    # 若已有完整观看归档（上次评价前失败），复用 digest，避免重复下载。
                    from bilibot.memory_brain.ingestion import video_observation

                    existing_detail = ""
                    if not skip_this_video:
                        existing_detail = await self._load_existing_video_detail(bvid)
                    if existing_detail:
                        video_detail = existing_detail
                        video_content = self._compose_video_content_for_prompt(
                            ctx, video_detail=video_detail,
                        )
                        logger.info(
                            "复用已归档视频详细内容，跳过下载/理解: bvid=%s detail=%s字",
                            bvid,
                            len(video_detail),
                        )
                        # 幂等就绪：不重复写入也可继续评价。
                        try:
                            await self._archive_required(
                                video_observation(
                                    account_id=self.account_id or "default",
                                    observation_key=observation_key,
                                    bvid=bvid,
                                    oid=str(oid),
                                    title=title,
                                    owner=owner,
                                    context=ctx.to_dict(),
                                    tags=tags_list,
                                    persona_id=self._get_current_persona_id(),
                                    video_detail=video_detail,
                                ),
                                treat_idempotent_as_ready=True,
                            )
                            artifact_cleanup_ready = True
                        except Exception as archive_exc:
                            from bilibot.memory_brain.models import (
                                IdempotencyConflictError,
                                ReingestBlockedError,
                            )
                            if isinstance(
                                archive_exc,
                                (IdempotencyConflictError, ReingestBlockedError),
                            ):
                                # 已有完整观看：继续评价。
                                logger.info(
                                    "复用路径归档冲突，按已就绪继续 bvid=%s", bvid
                                )
                            else:
                                raise
                    elif self.video_understanding and self.video_understanding.is_available():
                        try:
                            cid = video_info.get("cid", 0)
                            if not cid:
                                pages = video_info.get("pages", [])
                                if pages:
                                    cid = pages[0].get("cid", 0)
                            if not cid or not bvid:
                                raise RuntimeError("视频缺少可用于完整提取的 CID/BVID")

                            import os as _os
                            video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
                            save_path = _os.path.join(video_temp_dir, f"{bvid}")
                            video_file = await self.bili.download_video(
                                bvid, cid, save_path, quality=32
                            )
                            if not video_file or not _os.path.exists(video_file):
                                raise RuntimeError(f"视频下载失败: {bvid}")

                            logger.info(f"视频已下载，开始视听分析: {video_file}")
                            video_file_to_cleanup = video_file
                            vu_result = await self.video_understanding.understand(
                                video_file,
                                defer_cleanup=True,
                                require_complete_audio=True,
                                require_complete_visual=True,
                            )
                            if not isinstance(vu_result, dict):
                                raise RuntimeError("视频理解返回了无效结果")

                            work_dir_to_cleanup = vu_result.get("work_dir") or None
                            degradation = str(vu_result.get("degradation_reason") or "")
                            audio_status = vu_result.get("audio_status") or {}
                            # PRD V6：区分"降级"与"真失败"。
                            # - degradation 非空（如 duration_exceeds_limit）是预期降级，
                            #   应记录原因并继续走元数据归档，而不是抛错。
                            # - 音轨 ASR 真正失败 → 换片（不整任务 abort）。
                            audio_failed = (
                                isinstance(audio_status, dict)
                                and audio_status.get("status") == "failed"
                            )
                            if audio_failed:
                                reason = str(
                                    audio_status.get("error_code") or "audio_track_failed"
                                )
                                raise RuntimeError(f"视频提取未完成: {reason}")

                            if degradation:
                                ctx.degradation_reasons.append(
                                    f"video_understanding: {degradation}"
                                )
                                logger.info(f"视频理解降级，继续元数据归档: {degradation}")

                            if degradation:
                                # Complete audio/visual evidence was explicitly
                                # requested above. A degraded extraction may be
                                # kept for diagnostics, but cannot become a
                                # completed watch memory or authorize actions.
                                last_skip_reason = (
                                    f"DEGRADED_EXTRACTION:{degradation}"
                                )
                                skip_this_video = True

                            ctx.audiovisual = vu_result
                            av_log = ctx.audiovisual_log
                            if av_log:
                                logger.info(f"视频理解完成，行为日志 {len(av_log)} 字")
                            elif not degradation:
                                logger.warning("视频理解未生成行为日志")
                        except ASRTranscriptionError as e:
                            work_dir_to_cleanup = (
                                getattr(e, "work_dir", None) or work_dir_to_cleanup
                            )
                            last_skip_reason = f"ASR_FAILED:{e.code}"
                            skip_this_video = True
                            logger.warning(
                                "视频 ASR 失败，换视频 attempt=%s/%s bvid=%s code=%s",
                                video_attempt,
                                max_video_attempts,
                                bvid,
                                e.code,
                            )
                        except Exception as e:
                            work_dir_to_cleanup = (
                                getattr(e, "work_dir", None) or work_dir_to_cleanup
                            )
                            last_skip_reason = f"UNDERSTAND_FAILED:{type(e).__name__}"
                            skip_this_video = True
                            logger.warning(
                                "视频提取/理解失败，换视频 attempt=%s/%s bvid=%s err=%s",
                                video_attempt,
                                max_video_attempts,
                                bvid,
                                type(e).__name__,
                            )

                        # Full extracted source archive is the commit boundary. No evaluation
                        # or interaction happens before this succeeds.

                        # 先把长视听 log 压成 ≤2000 字详细内容：
                        # 1) 写入记忆，供日后召回
                        # 2) 作为评价/主动评论的主输入
                        # LLM 摘要失败会内部重试；仍失败则换下一个视频，不接受低质量截断。
                        if not skip_this_video:
                            if ctx.audiovisual_log:
                                video_detail = await self._build_video_detail_digest(
                                    title=title,
                                    owner=owner,
                                    behavior_log=ctx.audiovisual_log or "",
                                    max_attempts=2,
                                    require_llm=True,
                                )
                                if not video_detail:
                                    last_skip_reason = "VIDEO_DETAIL_FAILED"
                                    skip_this_video = True
                                    logger.warning(
                                        "视频详细内容摘要失败，换视频 attempt=%s/%s bvid=%s title=%s",
                                        video_attempt,
                                        max_video_attempts,
                                        bvid,
                                        title[:40],
                                    )
                                else:
                                    video_content = self._compose_video_content_for_prompt(
                                        ctx, video_detail=video_detail,
                                    )
                            else:
                                # 无 behavior_log：禁止元数据盲评（与「必须视听细节」一致）。
                                last_skip_reason = "NO_AUDIOVISUAL_LOG"
                                skip_this_video = True
                                logger.warning(
                                    "视频理解无行为日志，换视频 attempt=%s/%s bvid=%s",
                                    video_attempt,
                                    max_video_attempts,
                                    bvid,
                                )

                        if not skip_this_video:
                            try:
                                await self._archive_required(
                                    video_observation(
                                        account_id=self.account_id or "default",
                                        observation_key=observation_key,
                                        bvid=bvid,
                                        oid=str(oid),
                                        title=title,
                                        owner=owner,
                                        context=ctx.to_dict(),
                                        tags=tags_list,
                                        persona_id=self._get_current_persona_id(),
                                        video_detail=video_detail,
                                    ),
                                    # 同 bvid 重试时 digest 可能非确定性微变 → content_hash 冲突。
                                    # 仅在「同 key 且已是完整观看」时视为就绪；否则换片。
                                    treat_idempotent_as_ready=True,
                                )
                                artifact_cleanup_ready = True
                            except Exception as archive_exc:
                                from bilibot.memory_brain.models import (
                                    IdempotencyConflictError,
                                    ReingestBlockedError,
                                )
                                if isinstance(archive_exc, IdempotencyConflictError):
                                    # 同 key 不同内容，且未能当作完整观看就绪：换片避免串内容。
                                    last_skip_reason = "ARCHIVE_IDEMPOTENCY_CONFLICT"
                                    skip_this_video = True
                                    logger.warning(
                                        "视频归档幂等冲突，换视频 attempt=%s/%s bvid=%s",
                                        video_attempt,
                                        max_video_attempts,
                                        bvid,
                                    )
                                elif isinstance(archive_exc, ReingestBlockedError):
                                    last_skip_reason = "ARCHIVE_BLOCKED"
                                    skip_this_video = True
                                    logger.warning(
                                        "视频归档被 tombstone 阻断，换视频 bvid=%s", bvid,
                                    )
                                else:
                                    raise
                    else:
                        # 无视频理解服务且无已归档 digest：禁止元数据盲评。
                        # 否则模型只能复读标题，互动质量差且可能编造细节。
                        if not skip_this_video:
                            last_skip_reason = "NO_VIDEO_UNDERSTANDING"
                            skip_this_video = True
                            logger.warning(
                                "视频理解不可用且无已归档详细内容，跳过候选 "
                                "attempt=%s/%s bvid=%s",
                                video_attempt,
                                max_video_attempts,
                                bvid,
                            )
                finally:
                    from bilibot.video_understanding.cleanup import (
                        cleanup_media_artifacts,
                        schedule_cleanup,
                    )

                    if artifact_cleanup_ready:
                        cleanup_media_artifacts(
                            video_file_to_cleanup, work_dir_to_cleanup
                        )
                    else:
                        retained = [
                            p
                            for p in (video_file_to_cleanup, work_dir_to_cleanup)
                            if p
                        ]
                        if retained:
                            # 失败证据保留 30 分钟用于诊断/人工补归档；定时清理
                            # 防止多候选失败把 video_temp 永久撑满。
                            schedule_cleanup(retained, delay_seconds=1800)
                            logger.warning(
                                "主动视频证据尚未归档，保留 1800 秒: bvid=%s paths=%s",
                                bvid,
                                len(retained),
                            )

                if skip_this_video:
                    continue

                # 成功选定并归档本视频；跳出候选循环，继续评价/互动。
                break
            else:
                # 所有候选都失败/跳过
                logger.warning(
                    "主动看视频：%s 个候选均失败，最后原因=%s",
                    max_video_attempts,
                    last_skip_reason or "unknown",
                )
                if task_id:
                    # 下载/ASR/摘要失败通常可重试；NO_BVID 等结构性问题不重试。
                    retryable_prefixes = (
                        "VIDEO_DETAIL_FAILED",
                        "ASR_FAILED",
                        "UNDERSTAND_FAILED",
                        "DEGRADED_EXTRACTION",
                        "DOWNLOAD_FAILED",
                        "NO_OID",
                        "NO_VIDEO_INFO",
                        "ARCHIVE_IDEMPOTENCY_CONFLICT",
                        "ARCHIVE_BLOCKED",
                        "NO_VIDEO_UNDERSTANDING",
                        "NO_AUDIOVISUAL_LOG",
                        "EVALUATION_FAILED",
                    )
                    reason = last_skip_reason or "NO_USABLE_VIDEO"
                    retryable = any(
                        reason == p or reason.startswith(p + ":")
                        for p in retryable_prefixes
                    )
                    self._fail_task(
                        task_id,
                        reason.split(":", 1)[0] if ":" in reason else reason,
                        f"候选视频均不可用（{reason}）",
                        retryable=retryable,
                    )
                return

            # 3. LLM 评价视频（输入优先为 video_detail 摘要）
            # 评价失败不得写 bot_experience / succeed：否则去重会永久跳过该片。
            evaluation = None
            companion_ctx = ""
            companion = getattr(self, "companion", None)
            if companion is not None and getattr(companion, "enabled", False):
                try:
                    if hasattr(companion, "build_proactive_context_block"):
                        companion_ctx = companion.build_proactive_context_block() or ""
                except Exception:
                    companion_ctx = ""

            # Mid-watch working_memory: impression phase before full evaluation.
            # Enables mid_action replan if later evidence contradicts first impression.
            eval_action_key = f"proactive_video:{observation_key}:evaluate"
            brain_wm = getattr(self, "memory_brain", None)
            if brain_wm is not None and callable(
                getattr(brain_wm, "update_working_memory", None)
            ):
                try:
                    first_impression = (
                        f"watch_phase=impression title={str(title or '')[:40]} "
                        f"owner={str(owner or '')[:20]}"
                    )
                    brain_wm.update_working_memory(
                        eval_action_key,
                        phase="watch_phase_impression",
                        belief=first_impression,
                        notes={
                            "bvid": str(bvid or ""),
                            "watch_phase": "impression",
                            "belief_update": True,
                        },
                    )
                except Exception:
                    logger.debug("mid_watch working_memory seed failed", exc_info=True)

            # 账号级混合召回：近期视频/番剧/日记/评论，注入评价与后续主动评论
            activity_context = await self._begin_activity_context(
                action_key=eval_action_key,
                action_type="evaluate_proactive_video",
                current_activity=(
                    "正在观看并评价一条视频，结合最近经历决定真实感受、是否互动以及主动评论该说什么。"
                ),
                query=" ".join(
                    item
                    for item in (
                        str(title or ""),
                        str(owner or ""),
                        " ".join(str(tag) for tag in (tags_list or [])[:8]),
                        str(desc or "")[:500],
                    )
                    if item
                ),
                scene="proactive_video",
                title=title,
                bvid=bvid,
                oid=str(oid),
                metadata={"bvid": bvid, "oid": str(oid)},
            )
            if activity_context is not None:
                memory_bundle = {
                    "memory_evidence": str(activity_context.prompt_text or ""),
                    "memory_event_ids": list(activity_context.event_ids or ()),
                }
            else:
                memory_bundle = await self._recall_for_proactive_video(
                    title=title,
                    owner=owner,
                    tags=tags_list,
                    bvid=bvid,
                    oid=str(oid),
                    desc=desc,
                )
            memory_evidence = str(memory_bundle.get("memory_evidence") or "")
            memory_event_ids = list(memory_bundle.get("memory_event_ids") or [])
            try:
                # 挂到 ctx 供 audit/debug；to_dict 仍不含记忆，避免污染 video_observation
                if "ctx" in locals() and ctx is not None:
                    ctx.memory_evidence = memory_evidence
                    ctx.memory_event_ids = memory_event_ids
                    ctx.companion_context = companion_ctx
                    # compose 常在召回之前完成（digest 路径），评价前再拼一次避免「只记不用」
                    if video_detail:
                        video_content = self._compose_video_content_for_prompt(
                            ctx, video_detail=video_detail,
                        )
                    elif (memory_evidence or companion_ctx) and "【相关记忆" not in (
                        str(video_content or "")
                    ):
                        extras: list[str] = []
                        base = str(video_content or "").strip()
                        if base:
                            extras.append(base)
                        if memory_evidence:
                            extras.append(
                                "【相关记忆/近期经历】\n" + memory_evidence[:1800]
                            )
                        if companion_ctx:
                            extras.append(
                                "【你今天的状态与念头】\n" + str(companion_ctx).strip()[:500]
                            )
                        if extras:
                            video_content = "\n\n".join(extras)
            except Exception:
                pass

            if self.comment_generator:
                try:
                    evaluation = await self.comment_generator.evaluate_video(
                        title=title,
                        owner=owner,
                        desc=desc,
                        tags=tags_list,
                        hot_comments=hot_comments,
                        video_content=video_content,
                        companion_context=companion_ctx,
                        memory_evidence=memory_evidence,
                    )
                except TypeError:
                    try:
                        evaluation = await self.comment_generator.evaluate_video(
                            title=title,
                            owner=owner,
                            desc=desc,
                            tags=tags_list,
                            hot_comments=hot_comments,
                            video_content=video_content,
                            companion_context=companion_ctx,
                        )
                    except TypeError:
                        evaluation = await self.comment_generator.evaluate_video(
                            title=title,
                            owner=owner,
                            desc=desc,
                            tags=tags_list,
                            hot_comments=hot_comments,
                            video_content=video_content,
                        )
                except Exception as e:
                    logger.warning(f"视频评价失败: {e}")

            llm_ok = isinstance(evaluation, dict)
            if not llm_ok:
                logger.warning(
                    "LLM 评价失败，不写 experience、不标记任务成功 bvid=%s",
                    bvid,
                )
                await self._archive_bot_action(
                    action_key=f"proactive_video:{observation_key}:evaluate",
                    action_type="evaluate_proactive_video",
                    text=f"评价视频《{title}》失败，等待后续重试",
                    published=False,
                    status="failed",
                    title=title,
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "reason_code": "EVALUATION_FAILED",
                    },
                )
                if task_id:
                    self._fail_task(
                        task_id,
                        "EVALUATION_FAILED",
                        f"视频评价失败，保留归档以便重试 bvid={bvid}",
                        retryable=True,
                    )
                return

            score = evaluation.get("score", 0)
            # 严格校验 score 类型
            if not isinstance(score, (int, float)):
                try:
                    score = float(score)
                except Exception:
                    score = 0
            score = max(0, min(10, score))
            mood = evaluation.get("mood", "平静")
            review = evaluation.get("review", "")
            logger.info(f"视频评价: score={score}, mood={mood}, llm_ok={llm_ok}")
            # belief_update after full watch evaluation (may replan vs impression).
            if brain_wm is not None and callable(
                getattr(brain_wm, "update_working_memory", None)
            ):
                try:
                    belief = (
                        f"watch_phase=evaluated score={score} mood={mood} "
                        f"review={(review or '')[:80]}"
                    )
                    replan = score < 4  # low score → reconsider interaction impulse
                    brain_wm.update_working_memory(
                        eval_action_key,
                        phase="watch_phase_evaluated",
                        belief=belief,
                        draft=str(evaluation.get("comment") or "")[:200],
                        notes={
                            "score": score,
                            "mood": mood,
                            "belief_update": True,
                        },
                        replan=replan,
                    )
                    if replan and callable(getattr(brain_wm, "mid_action_replan", None)):
                        brain_wm.mid_action_replan(
                            eval_action_key,
                            reason=f"low_score_{score}",
                            new_belief=belief,
                        )
                except Exception:
                    logger.debug("mid_watch belief_update failed", exc_info=True)
            await self._archive_bot_action(
                action_key=eval_action_key,
                action_type="evaluate_proactive_video",
                text=(
                    f"已看完并评价视频《{title}》：评分 {score}/10，心情 {mood}。"
                    + (f" 感想：{review}" if review else "")
                ),
                published=True,
                title=title,
                scene="proactive_video",
                metadata={"bvid": bvid, "oid": str(oid)},
            )

            # PRD V4 VID-003：区分 inspected / watched 语义
            # inspected：读取元数据或下载分析，但未向 B站上报观看
            # watched：按照平台允许的接口上报观看进度并得到成功响应（默认关闭）
            watch_state = "inspected"
            watched_flag = False

            # 4. PRD V4 VID-006：互动决策由确定性 PolicyEngine 执行
            # 模型只输出建议，最终决策受开关、日预算、评分阈值和去重状态控制
            decisions = await self.interaction_policy.evaluate_async(
                llm_suggestion=evaluation,
                score=score,
                bvid=bvid,
                oid=str(oid),
            )
            action_outcomes: Dict[str, str] = {}

            # 执行点赞
            like_result = decisions.get("like", {})
            if like_result.get("planned"):
                # PRD V6：不单独记录 intent，仅在结果时归档
                try:
                    ok = await self.bili.like_video(oid)
                except Exception as e:
                    action_outcomes["like"] = f"error:{type(e).__name__}"
                    logger.debug(f"点赞失败: {e}")
                    await self.interaction_policy.record_result_async(
                        "like", bvid, str(oid), "failed", failure_reason=str(e)
                    )
                    await self._archive_bot_action(
                        action_key=f"video:{observation_key}:like",
                        action_type="like_video",
                        text=f"点赞视频《{title}》失败",
                        published=False,
                        status="failed",
                        title=title,
                        scene="proactive_video",
                        metadata={
                            "bvid": bvid,
                            "oid": str(oid),
                            "reason_code": type(e).__name__,
                        },
                    )
                else:
                    action_outcomes["like"] = "success" if ok else "failed"
                    await self.interaction_policy.record_result_async(
                        "like", bvid, str(oid), "success" if ok else "failed",
                        api_code=getattr(self.bili, "last_api_code", None),
                        failure_reason="" if ok else "bili_api_false",
                    )
                    if ok:
                        logger.info("视频点赞成功")
                        await self._archive_bot_action(
                            action_key=f"video:{observation_key}:like",
                            action_type="like_video",
                            # Phrase "点了赞" is the self-like recall needle.
                            text=f"观看了视频《{title}》并点了赞。",
                            published=True,
                            title=title,
                            scene="proactive_video",
                            metadata={"bvid": bvid, "oid": str(oid)},
                        )
                        companion = getattr(self, "companion", None)
                        if companion is not None and getattr(companion, "enabled", False):
                            push = getattr(companion, "_push_salient_self", None)
                            if callable(push):
                                try:
                                    push(
                                        line=f"给《{(title or '')[:40]}》点了赞",
                                    )
                                except Exception:
                                    logger.debug(
                                        "companion like salient push failed",
                                        exc_info=True,
                                    )
                    else:
                        self._check_bili_risk_control("proactive_like")
                        await self._archive_bot_action(
                            action_key=f"video:{observation_key}:like",
                            action_type="like_video",
                            text=f"点赞视频《{title}》失败",
                            published=False,
                            status="failed",
                            title=title,
                            scene="proactive_video",
                            metadata={
                                "bvid": bvid,
                                "oid": str(oid),
                                "reason_code": "BILI_API_FALSE",
                            },
                        )
            else:
                logger.debug(f"点赞未执行: {like_result.get('reason')}")

            # 执行投币
            coin_result = decisions.get("coin", {})
            if coin_result.get("planned"):
                # PRD V6：不单独记录 intent，仅在结果时归档
                try:
                    ok = await self.bili.coin_video(oid, num=1)
                except Exception as e:
                    action_outcomes["coin"] = f"error:{type(e).__name__}"
                    logger.debug(f"投币失败: {e}")
                    await self.interaction_policy.record_result_async(
                        "coin", bvid, str(oid), "failed", failure_reason=str(e)
                    )
                    await self._archive_bot_action(
                        action_key=f"video:{observation_key}:coin",
                        action_type="coin_video",
                        text=f"给视频《{title}》投币失败",
                        published=False,
                        status="failed",
                        title=title,
                        scene="proactive_video",
                        metadata={
                            "bvid": bvid,
                            "oid": str(oid),
                            "reason_code": type(e).__name__,
                        },
                    )
                else:
                    action_outcomes["coin"] = "success" if ok else "failed"
                    await self.interaction_policy.record_result_async(
                        "coin", bvid, str(oid), "success" if ok else "failed",
                        api_code=getattr(self.bili, "last_api_code", None),
                        failure_reason="" if ok else "bili_api_false",
                    )
                    if ok:
                        logger.info("视频投币成功")
                        await self._archive_bot_action(
                            action_key=f"video:{observation_key}:coin",
                            action_type="coin_video",
                            text=f"给视频《{title}》投了币。",
                            published=True,
                            title=title,
                            scene="proactive_video",
                            metadata={"bvid": bvid, "oid": str(oid)},
                        )
                    else:
                        self._check_bili_risk_control("proactive_coin")
                        await self._archive_bot_action(
                            action_key=f"video:{observation_key}:coin",
                            action_type="coin_video",
                            text=f"给视频《{title}》投币失败",
                            published=False,
                            status="failed",
                            title=title,
                            scene="proactive_video",
                            metadata={
                                "bvid": bvid,
                                "oid": str(oid),
                                "reason_code": "BILI_API_FALSE",
                            },
                        )
            else:
                logger.debug(f"投币未执行: {coin_result.get('reason')}")

            # 执行收藏
            fav_result = decisions.get("favorite", {})
            if fav_result.get("planned"):
                # PRD V6：不单独记录 intent，仅在结果时归档
                try:
                    ok = await self.bili.fav_video(oid)
                except Exception as e:
                    action_outcomes["favorite"] = f"error:{type(e).__name__}"
                    logger.debug(f"收藏失败: {e}")
                    await self.interaction_policy.record_result_async(
                        "favorite", bvid, str(oid), "failed", failure_reason=str(e)
                    )
                    await self._archive_bot_action(
                        action_key=f"video:{observation_key}:favorite",
                        action_type="favorite_video",
                        text=f"收藏视频《{title}》失败",
                        published=False,
                        status="failed",
                        title=title,
                        scene="proactive_video",
                        metadata={
                            "bvid": bvid,
                            "oid": str(oid),
                            "reason_code": type(e).__name__,
                        },
                    )
                else:
                    action_outcomes["favorite"] = "success" if ok else "failed"
                    await self.interaction_policy.record_result_async(
                        "favorite", bvid, str(oid), "success" if ok else "failed",
                        api_code=getattr(self.bili, "last_api_code", None),
                        failure_reason="" if ok else "bili_api_false",
                    )
                    if ok:
                        logger.info("视频收藏成功")
                        await self._archive_bot_action(
                            action_key=f"video:{observation_key}:favorite",
                            action_type="favorite_video",
                            text=f"收藏了视频《{title}》。",
                            published=True,
                            title=title,
                            scene="proactive_video",
                            metadata={"bvid": bvid, "oid": str(oid)},
                        )
                    else:
                        self._check_bili_risk_control("proactive_fav")
                        await self._archive_bot_action(
                            action_key=f"video:{observation_key}:favorite",
                            action_type="favorite_video",
                            text=f"收藏视频《{title}》失败",
                            published=False,
                            status="failed",
                            title=title,
                            scene="proactive_video",
                            metadata={
                                "bvid": bvid,
                                "oid": str(oid),
                                "reason_code": "BILI_API_FALSE",
                            },
                        )
            else:
                logger.debug(f"收藏未执行: {fav_result.get('reason')}")

            # 5. 生成评论并发表（PRD V4 COM-001/COM-002/COM-003 / PRD-V5 §10.2 COM-501）
            comment_text = ""
            # COM-001：proactive_comment 开关独立控制是否允许发布主动评论
            proactive_comment_enabled = features.get("proactive_comment", True)
            comment_decision = decisions.get("comment", {})
            if (proactive_comment_enabled
                    and comment_decision.get("planned")
                    and self.comment_generator):
                # PRD-V5 §10.2 COM-501：原子 claim — 同账号同视频只能一个 worker 进入流程
                comment_text = await self._do_proactive_comment_publish(
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
                    task_id=task_id or "",
                    memory_evidence=memory_evidence if "memory_evidence" in locals() else "",
                    companion_context=companion_ctx if "companion_ctx" in locals() else "",
                    memory_event_ids=memory_event_ids if "memory_event_ids" in locals() else None,
                )
            elif not proactive_comment_enabled:
                logger.debug("主动评论开关关闭（proactive_comment=false），跳过评论发布")
            elif not comment_decision.get("planned"):
                logger.debug(f"主动评论策略未批准: {comment_decision.get('reason')}")

            # 6. Archive the complete evaluation and real outcomes. Source text is
            # never truncated; raw audiovisual observations are already in the
            # preceding video_observation event.
            if comment_decision.get("planned"):
                action_outcomes["comment"] = "success" if comment_text else "failed_or_skipped"
            interaction_summary = await self.interaction_policy.get_today_summary_async()
            # Distinct from video_observation (raw AV archive): this is post-watch evaluation.
            experience_lines = [
                f"观看并评价了视频《{title}》，UP主 {owner}",
                f"评分: {score}",
                f"心情: {mood}",
            ]
            if review:
                experience_lines.append(f"评价: {review}")
            if comment_text:
                experience_lines.append(f"实际发布评论: {comment_text}")
            experience_lines.append(
                "评价结构化结果: " + json.dumps(evaluation, ensure_ascii=False, default=str)
            )
            from bilibot.memory_brain.ingestion import text_observation

            # 同片重试时 score/comment/outcomes 可能不同 → content_hash 冲突。
            # 已有 experience 视为闭环完成，不得因此暂停账号。
            await self._archive_required(
                text_observation(
                    account_id=self.account_id or "default",
                    idempotency_key=observation_key,
                    source_type="video_experience",
                    event_type="bot_experience",
                    text="\n".join(experience_lines),
                    title=title,
                    persona_id=self._get_current_persona_id(),
                    scene="proactive_video",
                    metadata={
                        "bvid": bvid,
                        "oid": str(oid),
                        "owner": owner,
                        "watch_state": watch_state,
                        "watched": watched_flag,
                        "action_outcomes": action_outcomes,
                        "interaction_budget": interaction_summary,
                        "memory_event_ids": list(
                            memory_event_ids if "memory_event_ids" in locals() else []
                        )[:20],
                        "memory_grounded": bool(
                            (memory_evidence if "memory_evidence" in locals() else "")
                            or (companion_ctx if "companion_ctx" in locals() else "")
                        ),
                    },
                    importance=max(0.1, min(1.0, score / 10.0)),
                ),
                treat_idempotent_as_ready=True,
            )

            # 更新情绪
            if self.behavior_sim:
                try:
                    self.behavior_sim.update_mood("watched_video")
                except Exception:
                    pass

            # 陪伴生活层回写：精力/心情/当前活动/念头（让「看过视频」进入当天生活）
            companion = getattr(self, "companion", None)
            if companion is not None and getattr(companion, "enabled", False):
                try:
                    if hasattr(companion, "on_proactive_video_finished"):
                        try:
                            companion.on_proactive_video_finished(
                                title=title or "",
                                score=score,
                                mood=str(mood or ""),
                                review=str(review or ""),
                                comment=str(comment_text or ""),
                                bvid=str(bvid or ""),
                                oid=str(oid or ""),
                                memory_event_ids=list(
                                    memory_event_ids
                                    if "memory_event_ids" in locals()
                                    else []
                                ),
                            )
                        except TypeError:
                            companion.on_proactive_video_finished(
                                title=title or "",
                                score=score,
                                mood=str(mood or ""),
                                review=str(review or ""),
                                comment=str(comment_text or ""),
                                bvid=str(bvid or ""),
                            )
                except Exception as e:
                    logger.debug("companion video feedback failed: %s", e)

            logger.info(
                "视频处理完成: 《%s》 score=%s comment=%s memory_events=%s",
                title,
                score,
                "是" if comment_text else "否",
                len(memory_event_ids) if "memory_event_ids" in locals() else 0,
            )

            # PRD-V5 §7：只有真正完成才 succeed（创建协程 ≠ 成功）
            if task_id:
                self._succeed_task(task_id, {
                    "success": True,
                    "summary": f"视频《{title}》处理完成 score={score}",
                    "bvid": bvid,
                    "title": title,
                    "memory_event_ids": list(
                        memory_event_ids if "memory_event_ids" in locals() else []
                    )[:12],
                    "memory_grounded": bool(
                        memory_evidence if "memory_evidence" in locals() else ""
                    ),
                })

        except Exception as e:
            logger.error(f"主动看视频失败: {e}", exc_info=True)
            # PRD-V5 §7：失败 → retry_wait/failed
            if task_id:
                self._fail_task(task_id, "PROACTIVE_VIDEO_ERROR", str(e), retryable=True)

    async def _generate_image_prompt(self, content: str) -> Optional[str]:
        """让 LLM 根据动态内容生成英文图片描述 prompt

        PRD V6：若当前人格配置了 appearance（外貌描述），将其作为画面主角注入 prompt，
        确保动态配图始终符合 bot 自身形象，而不是生成无关人物。
        """
        if not self.llm:
            return None
        try:
            # 取当前人格外貌（账号绑定优先，回退 current）
            appearance_desc = ""
            if self.persona_store is not None:
                persona = None
                try:
                    if self.account_id:
                        persona = self.persona_store.get_persona_for_account(self.account_id)
                except Exception:
                    persona = None
                if persona is None:
                    try:
                        persona = self.persona_store.get_current()
                    except Exception:
                        persona = None
                if persona is not None:
                    appearance_desc = (getattr(persona, "appearance", "") or "").strip()

            if appearance_desc:
                persona_block = (
                    "画面主角必须固定为以下形象，不可替换为其他人物：\n"
                    f"主角外貌: {appearance_desc}\n\n"
                    "如果动态内容与人物无关（如风景/物品），则主角不一定要入镜，"
                    "但只要出现人物就必须是上述主角形象。\n\n"
                )
            else:
                persona_block = ""

            prompt = (
                "根据以下动态内容，生成一个适合配图的英文图片描述 prompt（1-2 句话）。\n"
                "要求：\n"
                "- 只输出 prompt 本身，不要任何解释或前缀\n"
                "- 风格关键词：cinematic, high quality, detailed, no text, no watermark\n"
                "- 画面应与动态内容相关但不重复文字\n"
                f"{persona_block}"
                f"\n动态内容: {content}\n\nImage prompt:"
            )
            from bilibot.services.token_usage import usage_context
            with usage_context(scene="image_prompt", account_id=self.account_id or ""):
                result = await self.llm.generate(prompt, max_tokens=800, temperature=0.7)
            return result.strip() if result else None
        except Exception as e:
            logger.warning(f"生成图片 prompt 失败: {e}")
            return None

    async def _do_post_dynamic(self, task_id: Optional[str] = None):
        """发布动态

        PRD V3 §8.4 / §9.3：
        - 主流程使用 orchestrator.build_dynamic_prompt
        - LLM 失败时不得硬编码万能动态自动发布
        - 主写入账号级 V6 memory brain

        PRD-V5 §7 / TASK-501：通过 TaskRunStore 跟踪生命周期。
        平台返回 success 但本地写入失败时不会 mark_result_unknown（动态无回查接口）。
        """
        logger.info("准备发布动态...")
        if not self.bili or not self.llm:
            if task_id:
                self._fail_task(task_id, "NO_BILI_OR_LLM", "bili/llm 未初始化")
            return

        # PRD §5.9：全局暂停或账号风险暂停时跳过（DYN-604：safety_checker None → fail-closed）
        if self.safety_checker is None:
            logger.error("safety_checker 未初始化，拒绝发布动态（DYN-604 fail-closed）")
            if task_id:
                self._fail_task(task_id, "NO_SAFETY_CHECKER", "safety_checker 未初始化", retryable=False)
            return
        if (
            self.safety_checker.is_paused()
            or self.safety_checker.is_account_paused(self.account_id)
        ):
            logger.info(f"跳过发布动态（暂停状态）")
            if task_id:
                self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
            return

        # PRD-V5 §7：claim → start
        # scheduled（manual / retry）须先 claim；已 claimed 由调度/dispatch 完成
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

        try:
            # 1. 收集主题池（视频体验/完整观看/番剧等多源，不再只扫 video_experience）
            dynamic_activity_key = task_id or f"manual_{time.time_ns()}"
            brain = getattr(self, "memory_brain", None)
            related_videos: List[str] = []
            if brain:
                try:
                    related_videos = await asyncio.to_thread(
                        self._collect_related_titles_for_dynamic, brain, limit=5
                    )
                except Exception:
                    related_videos = []

            # PRD V4 DYN-001：主题选择
            # dynamic_publish.topics 非空时按权重或轮换选择主题
            # 最近已使用主题需记录，避免连续重复
            # topics 为空才允许自由发挥，代码不得固定传 topic=None 忽略配置
            # 陪伴层开启时优先用生活种子（日记/念头/探索），更像「此刻想说什么」
            selected_topic: Optional[str] = None
            companion = getattr(self, "companion", None)
            companion_life_block = ""
            if companion is not None and getattr(companion, "enabled", False):
                try:
                    if hasattr(companion, "build_proactive_context_block"):
                        companion_life_block = companion.build_proactive_context_block() or ""
                except Exception:
                    companion_life_block = ""
            _cfg_loader = getattr(self, "config_loader", None)
            dp_cfg = _cfg_loader.get_raw_config().get("dynamic_publish", {}) if _cfg_loader else {}
            topics_cfg = dp_cfg.get("topics", []) or []
            if companion is not None and getattr(companion, "enabled", False) and hasattr(companion, "pick_dynamic_topic"):
                try:
                    selected_topic = companion.pick_dynamic_topic(topics_cfg)
                    if selected_topic:
                        logger.info(f"DYN-001 陪伴生活选中动态主题: {selected_topic}")
                except Exception as e:
                    logger.debug("companion pick_dynamic_topic failed: %s", e)
                    selected_topic = None
            if not selected_topic and topics_cfg:
                try:
                    recent_topics: List[str] = []
                    if self.ds:
                        recent_topics = self.ds.load_json("recent_dynamic_topics.json", []) or []
                    # 过滤掉最近用过的主题（避免连续重复）；若全部用过则允许复用最旧的
                    available = [t for t in topics_cfg if t not in recent_topics]
                    if not available:
                        available = list(topics_cfg)
                    selected_topic = random.choice(available)
                    # 记录最近使用主题（保留最近 5 个）
                    recent_topics.append(selected_topic)
                    self.ds.save_json("recent_dynamic_topics.json", recent_topics[-5:])
                    logger.info(f"DYN-001 选中动态主题: {selected_topic}")
                except Exception as e:
                    logger.warning(f"主题选择失败: {e}")
                    selected_topic = None
            elif selected_topic and self.ds:
                try:
                    recent_topics = self.ds.load_json("recent_dynamic_topics.json", []) or []
                    recent_topics.append(selected_topic)
                    self.ds.save_json("recent_dynamic_topics.json", recent_topics[-5:])
                except Exception:
                    pass
            # selected_topic 为 None 表示 topics 为空，允许自由发挥

            # Before generation, persist what the Bot is doing now and attach a
            # guaranteed recent-self lane plus topic-relevant hybrid recall.
            recall_query_text = (
                f"最近看的视频、番剧、心情、日记、想法"
                f"{': ' + selected_topic if selected_topic else ''}"
            )
            if related_videos:
                recall_query_text += " " + " ".join(related_videos[:3])
            activity_context = await self._begin_activity_context(
                action_key=f"dynamic:{dynamic_activity_key}",
                action_type="dynamic_post",
                current_activity=(
                    "正在准备一条新动态，先回顾最近做过的事、正在经历的生活和相关记忆，再决定此刻想说什么。"
                ),
                query=recall_query_text,
                scene="dynamic_post",
                # Keep idempotent retries stable even if topic selection changes.
                title="动态发布",
                metadata={"task_id": task_id or ""},
            )
            memory_evidence = str(
                getattr(activity_context, "prompt_text", "") or ""
            ).strip()
            if not memory_evidence and brain:
                try:
                    from bilibot.memory_brain import RecallQuery
                    recall_result = await brain.recall(
                        RecallQuery(
                            current_message=recall_query_text,
                            account_id=self.account_id,
                            scene="dynamic_post",
                        )
                    )
                    if recall_result and recall_result.prompt_evidence:
                        memory_evidence = recall_result.prompt_evidence
                        logger.info(
                            "动态发布召回记忆: %s 条事件 sources=memory_brain",
                            len(getattr(recall_result, "events", []) or []),
                        )
                except Exception as recall_exc:
                    logger.debug(f"动态发布记忆召回失败: {recall_exc}")

            # companion 生活面单独保留一份，再并入 evidence 供 orchestrator
            memory_evidence_for_prompt = memory_evidence
            if companion_life_block:
                memory_evidence_for_prompt = (
                    f"{companion_life_block}\n\n{memory_evidence}".strip()
                    if memory_evidence
                    else companion_life_block
                )

            # 2. 通过 orchestrator 构建 prompt
            system_prompt = ""
            user_prompt = ""
            persona_id = "unknown"
            persona = None
            if self.persona_store is not None:
                try:
                    # PRD V4 ACC-002：使用账号绑定人格，避免多账号取全局人格
                    persona = self.persona_store.get_persona_for_account(self.account_id)
                    persona_id = persona.id if persona else "unknown"
                except Exception:
                    persona = None

            if self.orchestrator is not None:
                try:
                    extra_ctx = {
                        "companion_life": companion_life_block,
                        "sources": [
                            s
                            for s, ok in (
                                ("memory_brain", bool(memory_evidence)),
                                ("companion_life", bool(companion_life_block)),
                                ("related_videos", bool(related_videos)),
                            )
                            if ok
                        ],
                    }
                    if not extra_ctx["companion_life"] and not extra_ctx["sources"]:
                        extra_ctx = None
                    prompt_dict = self.orchestrator.build_dynamic_prompt(
                        topic=selected_topic,
                        related_videos=related_videos,
                        persona=persona,
                        memory_evidence=memory_evidence_for_prompt,
                        extra_context=extra_ctx,
                    )
                    system_prompt = prompt_dict.get("system", "")
                    user_prompt = prompt_dict.get("user", "")
                except TypeError:
                    # 兼容旧签名
                    try:
                        prompt_dict = self.orchestrator.build_dynamic_prompt(
                            topic=selected_topic,
                            related_videos=related_videos,
                            persona=persona,
                            memory_evidence=memory_evidence_for_prompt,
                        )
                        system_prompt = prompt_dict.get("system", "")
                        user_prompt = prompt_dict.get("user", "")
                    except Exception as e:
                        logger.warning(f"orchestrator.build_dynamic_prompt 失败: {e}")
                except Exception as e:
                    logger.warning(f"orchestrator.build_dynamic_prompt 失败: {e}")

            if not system_prompt or not user_prompt:
                # DynamicPoster 路径：注入记忆/companion，禁止只靠随机默认文案
                poster = getattr(self, "dynamic_poster", None)
                if poster is not None and hasattr(poster, "generate_dynamic"):
                    try:
                        content_fallback = await poster.generate_dynamic(
                            topic=selected_topic or "",
                            memory_evidence=memory_evidence,
                            companion_context=companion_life_block,
                            related_videos=related_videos,
                        )
                    except TypeError:
                        content_fallback = await poster.generate_dynamic(
                            context=memory_evidence_for_prompt or companion_life_block
                        )
                    except Exception as e:
                        logger.warning("DynamicPoster.generate_dynamic 失败: %s", e)
                        content_fallback = ""
                    if content_fallback:
                        # 直接作为生成结果走后续安全/发布；system/user 留空标记
                        system_prompt = "__dynamic_poster__"
                        user_prompt = content_fallback
                if not system_prompt or not user_prompt:
                    # 仅在 orchestrator/poster 都不可用时回退到 legacy personality
                    system_prompt = (
                        self.personality.get_system_prompt()
                        if self.personality
                        else "你是一个真实的B站用户。"
                    )
                    if companion_life_block:
                        system_prompt = f"{system_prompt}\n\n{companion_life_block}"
                    if memory_evidence:
                        system_prompt = f"{system_prompt}\n\n{memory_evidence[:1500]}"
                    if selected_topic:
                        user_prompt = (
                            f"现在轮到你发B站动态了，主题是「{selected_topic}」。"
                            "20-80字，口语化，像真人发动态的感觉。"
                        )
                    else:
                        user_prompt = (
                            "现在轮到你发B站动态了，想发点什么？20-80字，口语化，像真人发动态的感觉。"
                        )

            # 3. 调用 LLM（DynamicPoster 已直接产出正文时跳过二次生成）
            if system_prompt == "__dynamic_poster__":
                content = user_prompt
            else:
                from bilibot.services.token_usage import usage_context
                with usage_context(scene="dynamic_post", account_id=self.account_id or ""):
                    content = await self.llm.generate(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        max_tokens=1500,
                    )

            # LLM 失败时不再硬编码万能动态自动发布（PRD V3 §8.4）
            if not content:
                logger.warning("LLM 生成动态失败，跳过本次发布（不自动发万能动态）")
                if task_id:
                    self._fail_task(
                        task_id, "LLM_EMPTY", "LLM 生成动态返回空内容", retryable=True,
                    )
                return

            content = content.strip().replace("\n\n", "\n")
            # PRD V3 §5.4 (P2-4)：截断不加省略号
            if len(content) > 200:
                content = content[:200]

            dynamic_key = dynamic_activity_key
            # PRD V6：动态发布作为内部任务，不单独记录 intent；
            # 仅在最终结果（成功/失败/拒绝/异常）时归档一次，避免同一动态出现两条记忆。
            dynamic_meta_base = {
                "topic": selected_topic or "",
                "task_id": task_id or "",
                "memory_grounded": bool(memory_evidence or companion_life_block),
            }

            # 4. 写入 audit（含 persona_id，PRD V3 §8.6）
            audit_id = None
            if self.audit_store:
                try:
                    audit_id = await self.audit_store.record_async(
                        scene="dynamic_post",
                        persona_id=persona_id or "unknown",
                        input_summary=user_prompt[:200],
                        context_summary="",
                        prompt_preview=system_prompt[:2000],
                        output=content[:2000],
                        published=False,
                        target={"kind": "dynamic"},
                    )
                except Exception:
                    audit_id = None

            # PRD-V5 §4.1 DYN-501：动态审核强制生效
            # review_before_publish=true 时，生成内容 + 可选图片（不上传）→ 写入草稿，
            # 不调用任何 B站 publish / image upload API。管理员审核通过后独立发布。
            review_before_publish = bool(dp_cfg.get("review_before_publish", False))
            if review_before_publish:
                await self._handle_dynamic_review_mode(
                    task_id=task_id,
                    action_key=dynamic_key,
                    content=content,
                    persona_id=persona_id or "unknown",
                    audit_id=audit_id,
                    dp_cfg=dp_cfg,
                )
                return

            # PRD §5.9：发布前内容检查 + 频率限制（DYN-604：safety_checker None → fail-closed）
            rate_reserved = False
            if self.safety_checker is None:
                logger.error("safety_checker 未初始化，拒绝发布动态（DYN-604 fail-closed）")
                if audit_id and self.audit_store:
                    try:
                        self.audit_store.mark_published(
                            audit_id, published=False,
                            target={"kind": "dynamic"},
                            failure_reason="safety_checker unavailable",
                        )
                    except Exception:
                        pass
                if task_id:
                    self._fail_task(task_id, "NO_SAFETY_CHECKER", "safety_checker 未初始化", retryable=False)
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="rejected",
                    title=selected_topic or "动态",
                    scene="dynamic_post",
                    metadata={**dynamic_meta_base, "reason_code": "NO_SAFETY_CHECKER"},
                )
                return
            try:
                passed, reason = await self.safety_checker.check_content(
                    content, scene="dynamic_post",
                    persona_id=persona_id or "unknown",
                    account_id=self.account_id,
                )
            except Exception as e:
                # PRD V4 DYN-003 / §4.2：安全检查异常必须拒绝发布，不得降级放行
                logger.error(f"动态安全检查异常（拒绝发布，DYN-003）: {e}", exc_info=True)
                if audit_id and self.audit_store:
                    try:
                        self.audit_store.mark_published(
                            audit_id, published=False,
                            target={"kind": "dynamic"},
                            failure_reason=f"safety_check_exception: {e}",
                        )
                    except Exception:
                        pass
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="rejected",
                    title=selected_topic or "动态",
                    scene="dynamic_post",
                    metadata={**dynamic_meta_base, "reason_code": "SAFETY_CHECK_ERROR"},
                )
                if task_id:
                    self._fail_task(
                        task_id, "SAFETY_CHECK_ERROR",
                        f"safety_check_exception: {type(e).__name__}",
                        retryable=True,
                    )
                return

            if not passed:
                logger.warning(f"动态内容安全检查未通过: {reason}")
                if audit_id and self.audit_store:
                    try:
                        self.audit_store.mark_published(
                            audit_id, published=False,
                            target={"kind": "dynamic"},
                            failure_reason=f"safety_check: {reason}",
                        )
                    except Exception:
                        pass
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="rejected",
                    title=selected_topic or "动态",
                    scene="dynamic_post",
                    metadata={**dynamic_meta_base, "reason_code": "SAFETY_REJECTED"},
                )
                if task_id:
                    self._fail_task(
                        task_id, "SAFETY_REJECTED",
                        f"safety_check: {reason}",
                        retryable=False,
                    )
                return
            rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                scene="dynamic_post", account_id=self.account_id,
            )
            if not rate_ok:
                logger.warning("动态发布频率限制触发，跳过本次: %s", rate_reason)
                if audit_id and self.audit_store:
                    try:
                        self.audit_store.mark_published(
                            audit_id, published=False, failure_reason="rate_limited",
                        )
                    except Exception:
                        pass
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="deferred",
                    title=selected_topic or "动态",
                    scene="dynamic_post",
                    metadata={**dynamic_meta_base, "reason_code": "RATE_LIMITED"},
                )
                if task_id:
                    self._fail_task(
                        task_id, "RATE_LIMITED",
                        f"rate_limited: {rate_reason}",
                        retryable=True,
                    )
                return
            rate_reserved = True

            # 4.5 生成配图（如果配置了 with_image 且 image_provider 可用）
            image_list = []
            has_image = False
            _img_provider = getattr(self, 'image_provider', None)
            if _img_provider and _img_provider.is_available():
                try:
                    _raw = self.config_loader.get_raw_config()
                except Exception:
                    _raw = {}
                dp_config = _raw.get("dynamic_publish", {})
                if dp_config.get("with_image", False):
                    try:
                        image_prompt = await self._generate_image_prompt(content)
                        if image_prompt:
                            logger.info(f"正在生成动态配图: {image_prompt[:60]}...")
                            image_bytes = await _img_provider.generate(image_prompt)
                            if image_bytes:
                                img_info = await self.bili.upload_dynamic_image(image_bytes)
                                if img_info:
                                    image_list = [img_info]
                                    has_image = True
                                    logger.info("动态配图生成并上传成功")
                                else:
                                    logger.warning("B站图片上传失败，降级为纯文字动态")
                            else:
                                logger.warning("文生图失败，降级为纯文字动态")
                    except Exception as e:
                        logger.warning(f"配图生成失败（降级为纯文字动态）: {e}")

            # 5. 发布（根据配置）
            publish_attempted = False
            try:
                success = await self.bili.post_dynamic_text(
                    content, images=image_list if image_list else None
                )
                publish_attempted = True
            except Exception as publish_exc:
                publish_attempted = True
                if task_id:
                    self._mark_task_result_unknown(
                        task_id, f"dynamic publish exception: {type(publish_exc).__name__}"
                    )
                try:
                    await self._archive_bot_action(
                        action_key=f"dynamic:{dynamic_key}",
                        action_type="dynamic_post",
                        text=content,
                        published=False,
                        status="result_unknown",
                        title=selected_topic or "动态",
                        scene="dynamic_post",
                        metadata={
                            **dynamic_meta_base,
                            "has_image": has_image,
                            "reason_code": type(publish_exc).__name__,
                        },
                    )
                except Exception:
                    logger.error("unknown dynamic publish result could not be archived")
                return
            if success is None:
                logger.error("动态发布结果不确定（不自动重发）")
                if task_id:
                    self._mark_task_result_unknown(
                        task_id, "post_dynamic_text transport uncertainty"
                    )
                try:
                    await self._archive_bot_action(
                        action_key=f"dynamic:{dynamic_key}",
                        action_type="dynamic_post",
                        text=content,
                        published=False,
                        status="result_unknown",
                        title=selected_topic or "动态",
                        scene="dynamic_post",
                        metadata={**dynamic_meta_base, "reason_code": "RESULT_UNKNOWN"},
                    )
                except Exception:
                    logger.error("unknown dynamic result could not be archived")
                return

            if success is False:
                self._check_bili_risk_control("dynamic_post")

            # PRD §5.9：发布成功后记录内容（频率已在预占时记录）
            if success and self.safety_checker is not None:
                try:
                    self.safety_checker.record_content(content, account_id=self.account_id)
                except Exception:
                    pass

            # PRD V4 §4.5.2：发布结果同步到 audit
            if audit_id and self.audit_store:
                try:
                    if success:
                        self.audit_store.mark_published(
                            audit_id, published=True,
                            target={
                                "kind": "dynamic",
                                "published_at": datetime.now().isoformat(),
                                "platform": "bilibili",
                            },
                        )
                    else:
                        self.audit_store.mark_published(
                            audit_id, published=False,
                            target={"kind": "dynamic"},
                            failure_reason="bili.post_dynamic_text 返回 False",
                        )
                except Exception as e:
                    logger.debug(f"audit mark_published 失败: {e}")

            if success:
                try:
                    await self._archive_bot_action(
                        action_key=f"dynamic:{dynamic_key}",
                        action_type="dynamic_post",
                        text=content,
                        published=True,
                        title=selected_topic or "动态",
                        scene="dynamic_post",
                        metadata={
                            **dynamic_meta_base,
                            "has_image": has_image,
                        },
                    )
                except Exception:
                    logger.error("published dynamic result could not be archived")
                    if task_id:
                        self._mark_task_result_unknown(
                            task_id, "dynamic published but V6 result archive failed"
                        )
                    return

                logger.info(f"动态发布成功: {content[:50]}...")
                self._notify_companion_dynamic_posted(
                    content=content or "",
                    topic=selected_topic or "",
                    task_id=task_id or "",
                )
                # PRD-V5 §7：只有真正成功才 succeed
                if task_id:
                    self._succeed_task(task_id, {
                        "success": True,
                        "summary": f"动态发布成功: {content[:50]}",
                        "published_at": datetime.now().isoformat(),
                        "platform": "bilibili",
                        "kind": "dynamic",
                    })
            else:
                logger.error("动态发布失败")
                if rate_reserved and self.safety_checker is not None:
                    try:
                        self.safety_checker.refund_publish(
                            scene="dynamic_post", account_id=self.account_id,
                        )
                    except Exception:
                        pass
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="failed",
                    title=selected_topic or "动态",
                    scene="dynamic_post",
                    metadata={
                        **dynamic_meta_base,
                        "reason_code": "BILI_API_FALSE",
                    },
                )
                # PRD-V5 §7：发布失败 → retry_wait/failed
                if task_id:
                    self._fail_task(task_id, "DYNAMIC_PUBLISH_FAILED",
                                    "bili.post_dynamic_text 返回 False", retryable=True)

        except Exception as e:
            logger.error(f"发布动态失败: {e}", exc_info=True)
            # After a platform write attempt, prefer result_unknown over retryable fail
            # to avoid double-post. Pre-publish failures remain retryable.
            if task_id:
                if locals().get("publish_attempted"):
                    self._mark_task_result_unknown(
                        task_id, f"DYNAMIC_ERROR_AFTER_PUBLISH: {type(e).__name__}"
                    )
                else:
                    self._fail_task(task_id, "DYNAMIC_ERROR", str(e), retryable=True)

    # ══════════════════════════════════════════
    #  PRD-V5 §4.1 DYN-501：动态草稿审核流程
    # ══════════════════════════════════════════

    def _get_draft_store(self):
        """懒加载 DynamicDraftStore（与 TaskRunStore 同目录）"""
        from bilibot.services.dynamic_draft_store import DynamicDraftStore
        db_path = str(Path(self._get_data_dir()) / "dynamic_drafts.db")
        store = getattr(self, "_dynamic_draft_store", None)
        if store is None or store.db_path != db_path:
            store = DynamicDraftStore(db_path, account_id=self.account_id or "")
            self._dynamic_draft_store = store
        return store

    async def _handle_dynamic_review_mode(
        self,
        task_id: Optional[str],
        action_key: str,
        content: str,
        persona_id: str,
        audit_id: Optional[str],
        dp_cfg: Dict[str, Any],
    ):
        """PRD-V5 §4.1 DYN-501：审核模式处理

        - 执行安全检查（作为审核依据，不阻断草稿创建）
        - 生成配图（不上传 B站，存 base64 到草稿）
        - 安全通过 → awaiting_review；安全拒绝 → rejected
        - 不调用 post_dynamic_text / upload_dynamic_image
        """
        store = self._get_draft_store()

        # 安全检查（审核模式下作为审核依据快照）
        safety_passed = True
        safety_reason = ""
        safety_snapshot: Dict[str, Any] = {
            "scene": "dynamic_post",
            "persona_id": persona_id,
            "account_id": self.account_id,
            "checked_at": datetime.now().isoformat(),
        }
        if self.safety_checker is not None:
            try:
                passed, reason = await self.safety_checker.check_content(
                    content, scene="dynamic_post",
                    persona_id=persona_id,
                    account_id=self.account_id,
                )
                safety_passed = passed
                safety_reason = reason
                safety_snapshot["passed"] = passed
                safety_snapshot["reason"] = reason
            except Exception as e:
                # DYN-003：安全检查异常 → 拒绝（不降级放行）
                safety_passed = False
                safety_reason = f"safety_check_exception: {e}"
                safety_snapshot["passed"] = False
                safety_snapshot["reason"] = safety_reason
                logger.error(f"审核模式安全检查异常（DYN-003）: {e}", exc_info=True)
        else:
            # DYN-604：safety_checker None → fail-closed（不降级放行）
            safety_passed = False
            safety_reason = "safety_checker unavailable"
            safety_snapshot["passed"] = False
            safety_snapshot["reason"] = safety_reason
            logger.error("审核模式 safety_checker 未初始化（DYN-604 fail-closed）")

        # 生成配图（不上传 B站，存 base64）
        image_refs: List[str] = []
        _img_provider = getattr(self, "image_provider", None)
        if (
            _img_provider
            and _img_provider.is_available()
            and dp_cfg.get("with_image", False)
        ):
            try:
                image_prompt = await self._generate_image_prompt(content)
                if image_prompt:
                    image_bytes = await _img_provider.generate(image_prompt)
                    if image_bytes:
                        import base64 as _b64
                        image_refs = [_b64.b64encode(image_bytes).decode("ascii")]
                    else:
                        # Task 13：配图生成返回 None，记录失败信息（不静默降级为纯文字）
                        safety_snapshot["image_generation_failed"] = True
                        safety_snapshot["image_failure_reason"] = (
                            "image_provider.generate returned None"
                        )
                        logger.warning("审核模式配图生成返回 None（with_image=True）")
                else:
                    # Task 13：image_prompt 生成失败，记录失败信息
                    safety_snapshot["image_generation_failed"] = True
                    safety_snapshot["image_failure_reason"] = (
                        "image_prompt generation returned empty"
                    )
                    logger.warning("审核模式 image_prompt 生成返回空（with_image=True）")
            except Exception as e:
                # Task 13：配图生成异常，记录失败信息（不静默降级）
                safety_snapshot["image_generation_failed"] = True
                safety_snapshot["image_failure_reason"] = f"image_generation_exception: {e}"
                logger.warning(f"审核模式配图生成失败: {e}")

        # 计算过期时间
        draft_expiry = dp_cfg.get("draft_expiry_seconds", 86400)
        expires_at = time.time() + draft_expiry if draft_expiry else None

        draft_status = "awaiting_review" if safety_passed else "rejected"
        # task_id 可能为 None（非 TaskRun 调用路径），生成唯一占位 id
        draft_task_id = task_id or f"gen_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"

        draft_id = store.create(
            account_id=self.account_id or "_default",
            persona_id=persona_id,
            task_id=draft_task_id,
            content=content,
            image_refs=image_refs,
            safety_snapshot=safety_snapshot,
            created_by="scheduler",
            status=draft_status,
            expires_at=expires_at,
        )

        await self._archive_bot_action(
            action_key=f"dynamic:{action_key}",
            action_type="dynamic_post",
            text=content,
            published=False,
            status="drafted" if safety_passed else "rejected",
            title="动态草稿",
            scene="dynamic_post",
            metadata={
                "draft_id": draft_id,
                "draft_status": draft_status,
                "task_id": task_id or draft_task_id,
                "reason_code": "" if safety_passed else "SAFETY_REJECTED",
            },
        )

        # 同步 audit 状态（OBS-501 语义化状态）
        if audit_id and self.audit_store:
            try:
                self.audit_store.set_status(audit_id, draft_status)
            except Exception:
                pass

        logger.info(f"动态草稿已创建，等待审核: {draft_id} status={draft_status}")
        if task_id:
            self._succeed_task(task_id, {
                "success": True,
                "summary": f"动态草稿已创建: {draft_id}",
                "draft_id": draft_id,
                "draft_status": draft_status,
                "review_mode": True,
            })

    async def _do_publish_approved_draft(self, task_id: str, draft_id: str):
        """PRD-V5 §4.1 DYN-501：审核通过后的独立发布任务

        - start → mark_publishing（approved/retry_wait → publishing，原子 claim）
        - 上传图片（如有 base64 引用）+ 调用 post_dynamic_text
        - 成功 → mark_published；失败 → mark_retry_wait
        """
        # DYN-604：safety_checker None → fail-closed（与 _do_post_dynamic 一致）
        if self.safety_checker is None:
            self._fail_task(task_id, "SAFETY_CHECKER_MISSING", "safety_checker not initialized, fail-closed", retryable=False)
            return
        if not self.bili:
            self._fail_task(task_id, "NO_BILI", "bili API 未初始化", retryable=False)
            return
        # PRD-V5 §7：claim → start
        if not self.task_store.start(task_id):
            logger.warning(f"publish draft TaskRun {task_id} start 失败（可能已被处理）")
            return

        try:
            # DYN-602：暂停检查（fail-closed）。在 mark_publishing 之前暂停时草稿仍为 approved，
            # mark_retry_wait 只接受 publishing 来源，这里不要调用（会是 no-op）。
            # 仅失败 TaskRun（retryable），下次重试会再次从 approved claim。
            if self.safety_checker is not None:
                if self.safety_checker.is_paused():
                    self._fail_task(task_id, "GLOBAL_PAUSED", "全局暂停状态", retryable=True)
                    return
                if self.safety_checker.is_account_paused(self.account_id):
                    self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
                    return

            store = self._get_draft_store()
            draft = store.get(draft_id)
            if draft is None:
                self._fail_task(task_id, "DRAFT_NOT_FOUND",
                                f"草稿不存在: {draft_id}", retryable=False)
                return

            # 多账号隔离：草稿归属必须与当前 scheduler 账号一致（先于 claim）
            # 空 draft_acc 或空 self.account_id 也不得放行跨账号（对齐 task API 空 account 403）
            draft_acc = str(getattr(draft, "account_id", "") or "")
            self_acc = str(self.account_id or "")
            if self_acc and draft_acc != self_acc:
                logger.error(
                    "草稿账号不匹配，拒绝发布 draft=%s draft_acc=%s self=%s",
                    draft_id, draft_acc, self_acc,
                )
                self._fail_task(
                    task_id, "DRAFT_ACCOUNT_MISMATCH",
                    f"草稿账号 {draft_acc or '(empty)'} 与当前账号 {self_acc} 不匹配",
                    retryable=False,
                )
                return

            # 原子 claim：approved/retry_wait → publishing
            if not store.mark_publishing(draft_id):
                self._fail_task(task_id, "DRAFT_NOT_PUBLISHABLE",
                                f"草稿状态 {draft.status} 不可发布", retryable=False)
                return

            content = draft.content
            image_refs = draft.image_refs
            # PRD V6：不单独记录 intent，仅在最终结果时归档，避免同一动态两条记忆。

            # 上传图片（如有 base64 引用）
            image_list = []
            if image_refs:
                import base64 as _b64
                _img_failed = False
                for ref in image_refs:
                    try:
                        img_bytes = _b64.b64decode(ref)
                        img_info = await self.bili.upload_dynamic_image(img_bytes)
                        if img_info:
                            image_list.append(img_info)
                        else:
                            logger.warning("草稿配图上传失败")
                            _img_failed = True
                    except Exception as e:
                        logger.warning(f"草稿配图上传异常: {e}")
                        _img_failed = True
                # DYN-607：审核模式草稿（有 image_refs）图片上传失败不应降级为纯文字
                if _img_failed or not image_list:
                    store.mark_retry_wait(
                        draft_id, "草稿配图上传失败，等待重试"
                    )
                    await self._archive_bot_action(
                        action_key=f"dynamic_draft:{draft_id}:image_upload",
                        action_type="dynamic_post",
                        text=content,
                        published=False,
                        status="deferred",
                        title="已审核动态草稿",
                        scene="dynamic_post",
                        metadata={
                            "draft_id": draft_id,
                            "task_id": task_id,
                            "reason_code": "IMAGE_UPLOAD_FAILED",
                        },
                    )
                    self._fail_task(task_id, "IMAGE_UPLOAD_FAILED",
                                    "草稿配图上传失败", retryable=True)
                    return

            # DYN-602：发布前原子预占频率配额
            rate_reserved = False
            # 发布前再次内容检查 + 限流（与直发路径一致；审核后内容可能被编辑）
            if self.safety_checker is None:
                try:
                    store.mark_retry_wait(draft_id, "safety_checker missing")
                except Exception:
                    pass
                self._fail_task(
                    task_id, "NO_SAFETY_CHECKER",
                    "safety_checker 未初始化", retryable=False,
                )
                return
            try:
                passed, reason = await self.safety_checker.check_content(
                    content,
                    scene="dynamic_post",
                    persona_id=getattr(draft, "persona_id", "") or "",
                    account_id=self.account_id,
                )
            except Exception as e:
                logger.error(
                    "草稿真发安全检查异常（拒绝发布）: %s", e, exc_info=True,
                )
                try:
                    store.mark_retry_wait(
                        draft_id, f"safety_check_exception: {type(e).__name__}",
                    )
                except Exception:
                    pass
                self._fail_task(
                    task_id, "SAFETY_CHECK_ERROR",
                    f"safety_check_exception: {type(e).__name__}",
                    retryable=True,
                )
                return
            if not passed:
                logger.warning("草稿真发内容安全检查未通过: %s", reason)
                try:
                    store.mark_retry_wait(
                        draft_id, f"safety_check: {reason}",
                    )
                except Exception:
                    pass
                self._fail_task(
                    task_id, "SAFETY_REJECTED",
                    f"safety_check: {reason}", retryable=True,
                )
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}:safety",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="rejected",
                    title="已审核动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "reason_code": "SAFETY_REJECTED",
                        "reason": str(reason)[:200],
                    },
                )
                return

            rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
                scene="dynamic_post", account_id=self.account_id,
            )
            if not rate_ok:
                try:
                    store.mark_retry_wait(
                        draft_id, "dynamic_post rate limited"
                    )
                except Exception as _e:
                    logger.warning(f"Task 11.2: 限流回退草稿状态失败: {_e}")
                self._fail_task(task_id, "RATE_LIMITED",
                                "dynamic_post rate limited", retryable=True)
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}:publish_rate",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="deferred",
                    title="已审核动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "reason_code": "RATE_LIMITED",
                    },
                )
                return
            rate_reserved = True

            # 发布
            try:
                success = await self.bili.post_dynamic_text(
                    content, images=image_list if image_list else None
                )
            except Exception as publish_exc:
                # 结果不确定：不退配额
                error_name = type(publish_exc).__name__
                store.mark_result_unknown(draft_id, error_name)
                self._mark_task_result_unknown(
                    task_id, f"dynamic draft publish exception: {error_name}"
                )
                try:
                    await self._archive_bot_action(
                        action_key=f"dynamic_draft:{draft_id}",
                        action_type="dynamic_post",
                        text=content,
                        published=False,
                        status="result_unknown",
                        title="已审核动态草稿",
                        scene="dynamic_post",
                        metadata={
                            "draft_id": draft_id,
                            "task_id": task_id,
                            "reason_code": error_name,
                        },
                    )
                except Exception:
                    logger.error(
                        "unknown dynamic draft publish result could not be archived"
                    )
                return
            if success is None:
                logger.error("草稿动态发布结果不确定（不自动重发） draft=%s", draft_id)
                try:
                    store.mark_result_unknown(draft_id, "post_dynamic transport uncertainty")
                except Exception:
                    pass
                try:
                    await self._archive_bot_action(
                        action_key=f"dynamic_draft:{draft_id}",
                        action_type="dynamic_post",
                        text=content,
                        published=False,
                        status="result_unknown",
                        title="已审核动态草稿",
                        scene="dynamic_post",
                        metadata={
                            "draft_id": draft_id,
                            "task_id": task_id,
                            "reason_code": "RESULT_UNKNOWN",
                        },
                    )
                except Exception:
                    logger.error("unknown draft dynamic result could not be archived")
                self._mark_task_result_unknown(
                    task_id, "post_dynamic_text transport uncertainty"
                )
                return

            if success is False:
                self._check_bili_risk_control("dynamic_post")
                if rate_reserved and self.safety_checker is not None:
                    try:
                        self.safety_checker.refund_publish(
                            scene="dynamic_post", account_id=self.account_id,
                        )
                    except Exception:
                        pass

            if success:
                store.mark_published(draft_id)
                logger.info(f"动态草稿发布成功: {draft_id}")
                if self.safety_checker is not None:
                    try:
                        self.safety_checker.record_content(content, account_id=self.account_id)
                    except Exception:
                        pass
                try:
                    await self._archive_bot_action(
                        action_key=f"dynamic_draft:{draft_id}",
                        action_type="dynamic_post",
                        text=content,
                        published=True,
                        title="已发布动态草稿",
                        scene="dynamic_post",
                        metadata={
                            "draft_id": draft_id,
                            "task_id": task_id,
                            "has_image": bool(image_list),
                        },
                    )
                except Exception:
                    logger.error("published dynamic draft result could not be archived")
                    self._mark_task_result_unknown(
                        task_id, "dynamic draft published but V6 result archive failed"
                    )
                    return
                # 审核通过真发：生活面回写 + 与 draft_id 关联（脑归档已在上方完成）
                self._notify_companion_dynamic_posted(
                    content=content or "",
                    topic="",
                    draft_id=draft_id,
                    task_id=task_id,
                )
                self._succeed_task(task_id, {
                    "success": True,
                    "summary": f"动态草稿发布成功: {draft_id}",
                    "draft_id": draft_id,
                    "published_at": datetime.now().isoformat(),
                    "platform": "bilibili",
                    "kind": "dynamic",
                })
            else:
                store.mark_retry_wait(
                    draft_id, "bili.post_dynamic_text 返回 False"
                )
                await self._archive_bot_action(
                    action_key=f"dynamic_draft:{draft_id}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="failed",
                    title="已审核动态草稿",
                    scene="dynamic_post",
                    metadata={
                        "draft_id": draft_id,
                        "task_id": task_id,
                        "reason_code": "BILI_API_FALSE",
                    },
                )
                self._fail_task(task_id, "DYNAMIC_PUBLISH_FAILED",
                                "bili.post_dynamic_text 返回 False", retryable=True)
        except Exception as e:
            # After mark_publishing / post_dynamic, unexpected errors are result-unknown
            # to avoid auto re-publish. Pre-publish failures already returned above.
            logger.error(f"发布动态草稿失败: {e}", exc_info=True)
            try:
                self._get_draft_store().mark_result_unknown(draft_id, str(e))
            except Exception:
                try:
                    self._get_draft_store().mark_retry_wait(draft_id, str(e))
                except Exception:
                    pass
            self._mark_task_result_unknown(task_id, f"DYNAMIC_ERROR: {type(e).__name__}")

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

    # ══════════════════════════════════════════
    #  周总结
    # ══════════════════════════════════════════

    async def _check_weekly_summary(self):
        """检查并生成周总结

        PRD V3 §8.5 / §9.3 / PRD V4 SUM-001：
        - features.weekly_summary=false 时不执行检查和生成
        - 从 SQLite 读取本周活动（fallback 到 JSON）
        - 通过 orchestrator.build_weekly_summary_prompt 构建 prompt
        - 写入 audit + 账号级 V6 memory brain
        """
        # PRD V4 SUM-001：周总结开关
        features = self.config_loader.get_raw_config().get("features", {})
        if not features.get("weekly_summary", True):
            return

        if datetime.now().weekday() != 0:  # 0=Monday
            return

        if not self.llm:
            return

        # DYN-605：task_id 在 try 块外初始化，便于 except 中 fail
        task_id = None
        try:
            this_week = datetime.now().strftime("%G-W%V")
            brain = getattr(self, "memory_brain", None)
            if brain and await asyncio.to_thread(
                brain.has_identifier, this_week
            ):
                return

            logger.info("生成周总结...")

            # DYN-605：创建 TaskRun 跟踪周总结生命周期（可重试）
            try:
                from bilibot.services.task_store import TRIGGER_SCHEDULE
                _idem_key = f"{self.account_id or '_default'}:weekly_summary:{this_week}"
                _task = self.task_store.create_if_absent(
                    account_id=self.account_id or "_default",
                    scene="weekly_summary",
                    idempotency_key=_idem_key,
                    trigger_type=TRIGGER_SCHEDULE,
                    scheduled_at=time.time(),
                    input_data={"week": this_week},
                )
                if _task:
                    task_id = _task.task_id
                    if not self.task_store.start(task_id):
                        logger.warning(f"周总结 TaskRun {task_id} start 失败（可能已被处理）")
                        return
            except Exception as e:
                logger.warning(f"创建周总结 TaskRun 失败: {e}")

            # 1. 从 V6 account brain 读取本周活动。
            data_dir = self._get_data_dir()
            week_summary_text = self._build_week_summary_from_sqlite(data_dir)

            if not week_summary_text.strip() or week_summary_text == "无活动记录":
                logger.info("本周无活动记录，跳过周总结")
                if task_id:
                    self._succeed_task(task_id, {
                        "success": True,
                        "summary": "本周无活动记录，跳过",
                        "week": this_week,
                    })
                return

            weekly_activity = await self._begin_activity_context(
                action_key=f"weekly_summary:{this_week}",
                action_type="write_weekly_summary",
                current_activity=(
                    "正在写本周总结，会回顾这一周已经做过、看过、发布过和写过的事情，再整理连续的自我感受。"
                ),
                query=week_summary_text[:3000],
                scene="weekly_summary",
                title=f"周总结 {this_week}",
                # Do not bind the intent hash to TaskRun creation; a retry may
                # acquire a task id after an earlier TaskStore failure.
                metadata={"week": this_week},
            )
            weekly_activity_prompt = str(
                getattr(weekly_activity, "prompt_text", "") or ""
            ).strip()

            # 2. 通过 orchestrator 构建 prompt
            persona_id = "unknown"
            persona = None
            if self.persona_store is not None:
                try:
                    # PRD V4 ACC-002：使用账号绑定人格，避免多账号取全局人格
                    persona = self.persona_store.get_persona_for_account(self.account_id)
                    persona_id = persona.id if persona else "unknown"
                except Exception:
                    persona = None

            system_prompt = ""
            user_prompt = ""
            if self.orchestrator is not None:
                try:
                    prompt_dict = self.orchestrator.build_weekly_summary_prompt(
                        week_summary=week_summary_text,
                        persona=persona,
                    )
                    system_prompt = prompt_dict.get("system", "")
                    user_prompt = prompt_dict.get("user", "")
                except Exception as e:
                    logger.warning(f"orchestrator.build_weekly_summary_prompt 失败: {e}")

            if not system_prompt or not user_prompt:
                system_prompt = self.personality.get_system_prompt()
                user_prompt = (
                    f"上周的活动记录：\n\n{week_summary_text}\n\n"
                    "请用生动的语气写一份周报，200-350字。"
                )
            if weekly_activity_prompt:
                user_prompt = (
                    f"{user_prompt}\n\n【当前活动与跨场景记忆】\n"
                    f"{weekly_activity_prompt[:3500]}"
                )

            # 3. 调用 LLM
            from bilibot.services.token_usage import usage_context
            with usage_context(scene="weekly_summary", account_id=self.account_id or ""):
                summary = await self.llm.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    max_tokens=2000,
                )

            if not summary:
                logger.warning("LLM生成周总结失败")
                if task_id:
                    self._fail_task(task_id, "LLM_GENERATE_FAILED",
                                    "LLM生成周总结失败", retryable=True)
                return

            # 4. 写入 audit
            audit_id = None
            if self.audit_store:
                try:
                    audit_id = await self.audit_store.record_async(
                        scene="weekly_summary",
                        persona_id=persona_id or "unknown",
                        input_summary=user_prompt[:200],
                        context_summary=week_summary_text[:500],
                        prompt_preview=system_prompt[:2000],
                        output=summary[:2000],
                        published=False,
                        target={"kind": "weekly_summary", "week": this_week},
                    )
                except Exception:
                    audit_id = None

            # 5. 新增反思事件，不覆盖或压缩底层经历。
            from bilibot.memory_brain.ingestion import text_observation

            await self._archive_required(
                text_observation(
                    account_id=self.account_id or "default",
                    idempotency_key=this_week,
                    source_type="weekly_summary",
                    event_type="reflection",
                    text=summary,
                    title=f"周总结 {this_week}",
                    persona_id=persona_id,
                    scene="weekly_summary",
                    metadata={"week": this_week, "audit_id": audit_id},
                    importance=0.8,
                )
            )
            await self._archive_bot_action(
                action_key=f"weekly_summary:{this_week}",
                action_type="write_weekly_summary",
                text=f"本周总结 {this_week} 已经写完并归档。",
                published=True,
                title=f"周总结 {this_week}",
                scene="weekly_summary",
                metadata={"week": this_week, "task_id": task_id or ""},
            )
            logger.info("周总结已生成")
            # DYN-605：标记 TaskRun 成功
            if task_id:
                self._succeed_task(task_id, {
                    "success": True,
                    "summary": f"周总结已生成: {this_week}",
                    "week": this_week,
                })

        except Exception as e:
            logger.error(f"周总结失败: {e}", exc_info=True)
            # DYN-605：失败时标记 TaskRun（可重试）
            if task_id:
                self._fail_task(task_id, "WEEKLY_SUMMARY_ERROR", str(e), retryable=True)

    def _build_week_summary_from_sqlite(self, data_dir: str) -> str:
        """Build a bounded weekly prompt input from validated V6 events."""
        try:
            brain = getattr(self, "memory_brain", None)
            if not brain:
                return ""
            cutoff = time.time() - 7 * 86400
            events = brain.list_events(limit=500)
            lines = []
            total_chars = 0
            for event in reversed(events):
                if float(event.get("created_at") or 0) < cutoff:
                    continue
                if event.get("source_type") == "weekly_summary":
                    continue
                label = event.get("source_type") or event.get("event_type") or "经历"
                text = event.get("summary") or event.get("title") or ""
                if not text:
                    continue
                line = f"- [{label}] {text}"
                if total_chars + len(line) > 12000:
                    break
                lines.append(line)
                total_chars += len(line)
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("从 V6 brain 读取周活动失败: %s", type(exc).__name__)
            return ""

    # ══════════════════════════════════════════
    #  调度管理
    # ══════════════════════════════════════════

    def _generate_daily_schedule(self):
        """生成每日调度计划

        PRD-V5 §7 / TASK-501：同时持久化 TaskRun 记录（不只在内存中跟踪）。
        跨天时先把旧的 scheduled 标记 expired（不伪造 triggered），再生成新计划。
        当日数量为 0 时也持久化空计划（不创建 TaskRun，但清空内存）。
        """
        config = self.config_loader.get_raw_config()
        prov = config.get("proactive", {})

        # PRD-V5 §7.2：跨天清理旧 scheduled（不伪造 triggered）
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            self.task_store.clear_account_scene_today(
                self.account_id or "_default", "proactive_video",
            )
            self.task_store.clear_account_scene_today(
                self.account_id or "_default", "dynamic",
            )
        except Exception as e:
            logger.warning(f"跨天清理 scheduled TaskRun 失败: {e}")

        # 场景级配置（grace_window / max_attempts）
        scenes_cfg = prov.get("scenes", {}) or {}
        pv_scene = scenes_cfg.get("proactive_video", {}) or {}
        dyn_scene = scenes_cfg.get("dynamic", {}) or {}
        default_grace = prov.get("grace_window_seconds", 900)
        pv_grace = pv_scene.get("grace_window_seconds", default_grace)
        pv_max_att = pv_scene.get("max_attempts", 3)
        dyn_grace = dyn_scene.get("grace_window_seconds", default_grace)
        dyn_max_att = dyn_scene.get("max_attempts", 3)

        # 主动看视频时间
        n_videos = prov.get("video_count", 0)
        self._proactive_times = []
        if n_videos > 0:
            # 24 小时 × 60 分钟 = 1440 个分钟点，随机抽取，全天任意时间均可触发
            total_minutes = 24 * 60
            n_videos = min(n_videos, total_minutes)
            slots = sorted(random.sample(range(total_minutes), n_videos))
            self._proactive_times = [(s // 60, s % 60) for s in slots]
            # 持久化 TaskRun（幂等键：account + scene + date + slot）
            self._persist_schedule("proactive_video", self._proactive_times,
                                  pv_grace, pv_max_att, today_str)

        # 动态发布时间
        n_dynamics = prov.get("dynamic_count", 0)
        self._dynamic_times = []
        if n_dynamics > 0:
            hours = sorted(random.sample(range(10, 23), min(n_dynamics, 13)))
            self._dynamic_times = [(h, random.randint(0, 59)) for h in hours]
            self._persist_schedule("dynamic", self._dynamic_times,
                                  dyn_grace, dyn_max_att, today_str)

        logger.info(
            f"主动视频计划: {[f'{h}:{m:02d}' for h, m in self._proactive_times]}"
        )
        logger.info(
            f"动态发布计划: {[f'{h}:{m:02d}' for h, m in self._dynamic_times]}"
        )

    def _persist_schedule(self, scene: str, times: List[tuple],
                          grace_window: int, max_attempts: int,
                          date_str: str):
        """将调度计划持久化为 TaskRun 记录（幂等）"""
        from bilibot.services.task_store import TRIGGER_SCHEDULE
        for (h, m) in times:
            slot = f"{h:02d}:{m:02d}"
            idem_key = f"{self.account_id or '_default'}:{scene}:{date_str}:{slot}"
            scheduled_at = datetime.strptime(
                f"{date_str} {slot}", "%Y-%m-%d %H:%M"
            ).timestamp()
            try:
                self.task_store.create_if_absent(
                    account_id=self.account_id or "_default",
                    scene=scene,
                    idempotency_key=idem_key,
                    trigger_type=TRIGGER_SCHEDULE,
                    scheduled_at=scheduled_at,
                    input_data={"slot": slot, "date": date_str},
                    max_attempts=max_attempts,
                    grace_window=grace_window,
                )
            except Exception as e:
                logger.warning(f"持久化 TaskRun 失败 scene={scene} slot={slot}: {e}")

    def _mark_overdue_as_triggered(self):
        """标记已过期的计划为已执行"""
        now = datetime.now()

        self._proactive_triggered = {
            f"{h:02d}:{m:02d}" for h, m in self._proactive_times
            if now.hour > h or (now.hour == h and now.minute > m)
        }

        self._dynamic_triggered = {
            f"{h:02d}:{m:02d}" for h, m in self._dynamic_times
            if now.hour > h or (now.hour == h and now.minute > m)
        }

    def _save_schedule_state(self):
        """保存调度状态"""
        if not self.ds:
            return
        state = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": [f"{h}:{m:02d}" for h, m in self._proactive_times],
            "proactive_triggered": sorted(self._proactive_triggered),
        }
        self.ds.save_json("schedule_today.json", state)

    def _save_dynamic_schedule_state(self):
        """保存动态调度状态"""
        if not self.ds:
            return
        state = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "dynamic_times": [f"{h}:{m:02d}" for h, m in self._dynamic_times],
            "dynamic_triggered": sorted(self._dynamic_triggered),
        }
        self.ds.save_json("dynamic_schedule.json", state)

    def get_schedule_snapshot(self) -> Dict[str, Any]:
        """获取今日调度快照"""
        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "proactive_times": [f"{h}:{m:02d}" for h, m in self._proactive_times],
            "proactive_triggered": sorted(self._proactive_triggered),
            "dynamic_times": [f"{h}:{m:02d}" for h, m in self._dynamic_times],
            "dynamic_triggered": sorted(self._dynamic_triggered),
        }

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
