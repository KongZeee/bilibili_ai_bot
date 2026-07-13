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
- 主动视频 / 动态 / 周总结主写入 SQLite memory_atoms（保留 JSON 备份）
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
                 image_provider=None, knowledge_memory=None, memory_write_queue=None,
                 proactive_comment_store=None, memory_brain=None):
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
            # MISC-608：同时检查 web_search.enabled 和 features.web_search 作为 fallback
            ws_enabled = ws_config.get("web_search", {}).get("enabled", False) or \
                ws_config.get("features", {}).get("web_search", False)
            if ws_enabled:
                self.web_search = WebSearchService(
                    ws_config, llm_provider=self.llm, data_store=self.ds,
                    audit_store=self.audit_store, account_id=self.account_id,
                )
                if self.web_search.is_available():
                    logger.info("联网搜索服务已启用")
        except Exception as e:
            logger.warning(f"联网搜索服务初始化失败: {e}")

        # 番剧追番服务（可选，需 features.bangumi=true + video_understanding）
        self.bangumi_service = None
        self._last_bangumi_check_date = None
        try:
            raw = config_loader.get_raw_config()
            if raw.get("features", {}).get("bangumi", False):
                from bilibot.bangumi import BangumiService
                data_dir = raw.get("data_dir", "./data")
                self.bangumi_service = BangumiService(
                    bili_api=bili, llm_manager=llm,
                    video_service=self.video_understanding,
                    config_loader=config_loader, data_dir=data_dir,
                    memory_brain=resolved_memory_brain,
                    account_id=self.account_id,
                )
                logger.info("番剧追番服务已启用")
        except Exception as e:
            logger.warning(f"番剧追番服务初始化失败: {e}")

        self.running = False

        # PRD V3 §3.2 / §4.2：后台任务引用集合，防止 GC + 记录异常
        self._running_tasks: set = set()

        # 初始化各子系统
        self.personality = PersonalitySystem(config_loader)

        # MEM-501：knowledge_memory 由 AccountInstance 注入（单一实例，不再自行创建）
        self.knowledge_memory = knowledge_memory
        self.memory_brain = resolved_memory_brain
        self._memory_brain_required = resolved_memory_brain is not None
        # MEM-501：记忆写入队列（由 AccountInstance 注入，业务代码通过此队列写入）
        self.memory_write_queue = memory_write_queue

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
                self.dynamic_poster = DynamicPoster(self.behavior_sim, self.llm, config_loader)
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
        self._dynamic_times: List[tuple] = []
        self._dynamic_triggered: set = set()
        # PRD V6：轮询状态去重日志——只在数值变化时打印 INFO，否则 DEBUG，避免日志膨胀
        self._last_notify_count: Optional[int] = None
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
        # PRD V4 COM-002：主动评论发布策略（去重 + 预算 + 审计固化）
        self.comment_policy = CommentPolicy(
            config=config_loader.get_raw_config(),
            data_dir=_policy_data_dir,
            account_id=self.account_id or "",
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

    async def _archive_required(self, envelope):
        """Archive a raw observation before any irreversible business action."""
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
            # 这些异常向上传播让调用方决定如何处理，但不触发风险暂停。
            from bilibot.memory_brain.models import (
                IdempotencyConflictError,
                ReingestBlockedError,
            )
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

    def _pause_for_memory_failure(self) -> None:
        safety = getattr(self, "safety_checker", None)
        account_id = str(getattr(self, "account_id", "") or "")
        if safety is None or not account_id:
            return
        try:
            safety.pause_account(account_id, reason="memory_archive_failed")
        except Exception:
            pass

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

        # Task 5：启动时恢复 interrupted 状态的 TaskRun
        # 按场景重新入队（retry → scheduled）或立即重跑（retry → claim → dispatch）
        try:
            interrupted = self.task_store.list_interrupted()
            if interrupted:
                logger.info(f"Task 5: 发现 {len(interrupted)} 个 interrupted TaskRun，开始恢复")
                for task in interrupted:
                    scene = task.scene
                    if scene in ("proactive_video", "dynamic"):
                        # 立即重跑：retry → claim → dispatch（start 由 _do_xxx 完成）
                        if not self.task_store.retry(task.task_id):
                            logger.warning(f"Task 5: TaskRun {task.task_id} retry 失败")
                            continue
                        if not self.task_store.claim(task.task_id):
                            logger.warning(f"Task 5: TaskRun {task.task_id} claim 失败")
                            continue
                        if scene == "proactive_video":
                            self._spawn_memory_task(
                                self._do_proactive_video(task_id=task.task_id),
                                tag=f"recovery_proactive_video:{task.task_id}",
                            )
                            logger.info(f"Task 5: interrupted TaskRun {task.task_id} 重新执行（proactive_video）")
                        else:
                            self._spawn_memory_task(
                                self._do_post_dynamic(task_id=task.task_id),
                                tag=f"recovery_dynamic:{task.task_id}",
                            )
                            logger.info(f"Task 5: interrupted TaskRun {task.task_id} 重新执行（dynamic）")
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
                nav = await self.bili.get_nav_status()
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

                # 番剧追番更新检测（每天一次）
                if self.bangumi_service and now.date() != self._last_bangumi_check_date:
                    try:
                        result = await self.bangumi_service.check_updates()
                        self._last_bangumi_check_date = now.date()
                        if result.get("updated", 0) > 0:
                            logger.info(f"追番更新检测: {result['updated']} 部有更新，已观看 {result.get('watched', 0)} 集")
                    except Exception as e:
                        logger.error(
                            "追番更新检测失败，保留为可重试状态: %s",
                            type(e).__name__,
                            exc_info=True,
                        )

                # REP-602：恢复卡在中间态的评论（context_building/
                # generation_pending/safety_pending/publish_pending 超过 10 分钟
                # 未推进 → 转为 deferred，由后续 _process_retryable_comments 拾取）
                try:
                    self.reply_state_store.recover_stuck_intermediate(timeout_minutes=10)
                except Exception as e:
                    logger.warning(
                        f"REP-602: 恢复卡在中间态评论失败: {e}", exc_info=True
                    )

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

        # PRD §5.9：全局暂停或账号风险暂停时跳过所有自动行为
        if self.safety_checker is not None and (
            self.safety_checker.is_paused()
            or self.safety_checker.is_account_paused(self.account_id)
        ):
            logger.info(f"跳过评论检查（全局暂停={self.safety_checker.is_paused()}, 账号暂停={self.safety_checker.is_account_paused(self.account_id)}）")
            return

        config = self.config_loader.get_raw_config()
        # PRD V4 REP-002 / CFG-003：统一使用 features.reply_comment
        # 旧 reply.auto_reply 已迁移，运行时不再消费
        features = config.get("features", {})
        if not features.get("reply_comment", True):
            logger.info("features.reply_comment=false，跳过评论检查")
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
                        logger.info(f"@我的 通知返回 {len(at_items)} 条，合并后候选共 {len(items)} 条")
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
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason=f"llm_error: {llm_err}", error_code="LLM_ERROR",
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
                        # retryable / permanent → deferred（非终态，可恢复）
                        logger.warning(
                            f"LLM 生成未成功 (status={outcome.status}, code={outcome.error_code})，deferred"
                        )
                        self.reply_state_store.mark_deferred(
                            comment_type, reply_id,
                            reason=f"generation_{outcome.status}: {outcome.error_code}",
                            error_code=outcome.error_code or "GEN_FAILED",
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

                    # PRD §5.9：发布前内容检查 + 频率限制
                    if self.safety_checker is not None:
                        persona_id_for_check = self._get_current_persona_id()
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
                                            target={"kind": "reply_comment", "rpid": str(reply_id)},
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
                            if not self.safety_checker.check_rate_limit(scene="reply_comment", account_id=self.account_id):
                                # PRD V4 §9.1：限流 → deferred（非终态，可恢复）
                                logger.warning("评论发布频率限制触发，deferred")
                                self.reply_state_store.mark_deferred(
                                    comment_type, reply_id,
                                    reason="rate_limited", error_code="RATE_LIMIT",
                                )
                                continue
                            # PRD 4.4：预占频率配额（失败也计数，防止反复尝试失败永不触发限流）
                            self.safety_checker.record_publish(scene="reply_comment", account_id=self.account_id)
                        except Exception as e:
                            # PRD V4 §9.1：安全检查异常 → deferred（非终态，可恢复）
                            logger.error(f"安全检查异常，deferred: {e}", exc_info=True)
                            self.reply_state_store.mark_deferred(
                                comment_type, reply_id,
                                reason=f"safety_exception: {e}", error_code="SAFETY_ERROR",
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
                        logger.error(f"发表评论异常 reply_id={reply_id}: {post_err}")
                        self.reply_state_store.mark_retry_wait(
                            comment_type, reply_id,
                            reason=f"post_exception: {post_err}",
                            error_code="POST_EXCEPTION",
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
                                    "reason_code": "POST_EXCEPTION",
                                },
                            )
                        except Exception:
                            logger.error(
                                "failed comment result could not be archived: reply_id=%s",
                                reply_id,
                            )
                        continue
                    if not success:
                        self._check_bili_risk_control("reply_comment")

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
                                        "oid": str(oid),
                                        "published_at": datetime.now().isoformat(),
                                    },
                                )
                            else:
                                self.audit_store.mark_published(
                                    audit_id, published=False,
                                    target={"kind": "reply_comment", "rpid": str(reply_id)},
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
                    logger.error(f"处理评论失败: {e}")

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
                            # 无生成结果或 hash 失效，转为 deferred 重新生成
                            self.reply_state_store.mark_deferred(
                                ct, rpid, reason="no_generation_result", error_code="RETRY_NO_GEN",
                            )
                            continue
                        # 幂等检查：重试前先查询楼中楼，确认 Bot 是否已回复过（避免超时导致的重复发帖）
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
                                already_replied = any(
                                    str(r.get("member", {}).get("mid", "")) == self._bot_uid
                                    for r in existing
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
                                    continue
                            except Exception as e:
                                logger.warning(f"幂等检查失败，继续重试: {e}")
                        # Task 9：重试路径也需频率检查和预占配额（与主路径一致）
                        if self.safety_checker is not None:
                            if not self.safety_checker.check_rate_limit(
                                scene="reply_comment", account_id=self.account_id
                            ):
                                logger.warning("重试路径评论发布频率限制触发，deferred")
                                self.reply_state_store.mark_deferred(
                                    ct, rpid,
                                    reason="rate_limited", error_code="RATE_LIMIT",
                                )
                                continue
                            # PRD 4.4：预占频率配额（失败也计数）
                            self.safety_checker.record_publish(
                                scene="reply_comment", account_id=self.account_id
                            )
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
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"post_exception: {post_err}",
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
                                    status="failed",
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
                                    "failed retried comment result could not be archived: rpid=%s",
                                    rpid,
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
                            logger.info(f"重试发布成功: rpid={rpid}")
                        else:
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
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"retry_gen_exception: {gen_err}",
                                error_code="RETRY_GEN_EXCEPTION",
                            )
                            continue

                        if outcome.is_skip:
                            # LLM 明确不回复 → ignored
                            self.reply_state_store.mark_ignored(
                                ct, rpid, rule="llm_no_reply_retry",
                            )
                            continue

                        if not outcome.is_generated:
                            # retryable/permanent → 继续 defer
                            self.reply_state_store.mark_deferred(
                                ct, rpid,
                                reason=f"retry_gen_{outcome.status}: {outcome.error_code}",
                                error_code=outcome.error_code or "RETRY_GEN_FAILED",
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
                            )
                            continue

                        # 持久化生成结果
                        self.reply_state_store.save_generation_result(
                            ct, rpid,
                            text=reply_text,
                            persona_id=context_meta.get("persona_id", "") or self._get_current_persona_id() or "",
                            audit_id=audit_id,
                        )

                        # 安全检查
                        if self.safety_checker is not None:
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
                                                target={"kind": "reply_comment", "rpid": str(rpid)},
                                                failure_reason=f"safety_check: {reason}",
                                            )
                                        except Exception:
                                            pass
                                    self.reply_state_store.mark_rejected(
                                        ct, rpid, reason=f"safety_check: {reason}",
                                    )
                                    continue
                                if not self.safety_checker.check_rate_limit(
                                    scene="reply_comment", account_id=self.account_id
                                ):
                                    self.reply_state_store.mark_deferred(
                                        ct, rpid,
                                        reason="rate_limited_retry", error_code="RATE_LIMIT",
                                    )
                                    continue
                                self.safety_checker.record_publish(
                                    scene="reply_comment", account_id=self.account_id
                                )
                            except Exception as safety_err:
                                self.reply_state_store.mark_deferred(
                                    ct, rpid,
                                    reason=f"safety_exception: {safety_err}",
                                    error_code="SAFETY_ERROR",
                                )
                                continue

                        # 发布回复
                        comment_root = root_id if root_id else source_id
                        try:
                            success = await self.bili.post_comment(
                                oid=oid, content=reply_text, comment_type=ct,
                                rpid=comment_root, parent=source_id,
                            )
                        except Exception as post_err:
                            self.reply_state_store.mark_retry_wait(
                                ct,
                                rpid,
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
                                    status="failed",
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
                                    "failed deferred comment result could not be archived: rpid=%s",
                                    rpid,
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
                                        target={"kind": "reply_comment", "rpid": str(rpid)},
                                    )
                                except Exception:
                                    pass
                            logger.info(f"deferred 重试成功: rpid={rpid}")
                        else:
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

        # 2. 生成评论
        comment_text = evaluation.get("comment", "") if llm_ok else ""
        if not comment_text or len(comment_text) < 5:
            try:
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

        # 保存生成结果（便于 retry 时复用）
        self.proactive_comment_store.save_generation(action_id, comment_text)
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

        # 4. PRD §5.9：发布前内容检查 + 频率限制（fail-closed）
        if self.safety_checker is not None:
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
                    logger.warning(f"主动评论频率限制触发，跳过: {rate_reason}")
                    self.proactive_comment_store.mark_failed(
                        action_id, "RATE_LIMITED", "proactive_comment rate limited",
                    )
                    return ""
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
            return ""

        # PRD 3.5 / COM-004：审计记录
        audit_id = None
        if self.audit_store is not None:
            try:
                audit_id = await self.audit_store.record_async(
                    scene="proactive_comment",
                    persona_id=persona_id_for_policy or "default",
                    prompt_preview=f"视频: {title} | UP: {owner}",
                    output=comment_text,
                    target={"bvid": bvid, "oid": oid},
                )
            except Exception as e:
                logger.warning(f"审计记录失败: {e}")

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
            # 兼容旧逻辑：仍记录 CommentPolicy / InteractionPolicy
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
            if audit_id:
                try:
                    self.audit_store.mark_published(
                        audit_id, published=False, failure_reason=str(e)
                    )
                except Exception:
                    pass
            return ""

        if not success:
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
            if audit_id:
                try:
                    self.audit_store.mark_published(audit_id, published=True)
                except Exception:
                    pass
            try:
                await self._archive_bot_action(
                    action_key=f"proactive_comment:{action_id}",
                    action_type="proactive_comment",
                    text=comment_text,
                    published=True,
                    title=title,
                    scene="proactive_video",
                    metadata={"bvid": bvid, "oid": str(oid)},
                )
            except Exception:
                logger.error(
                    "published proactive comment result could not be archived: action=%s",
                    action_id,
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
            if audit_id:
                try:
                    self.audit_store.mark_published(
                        audit_id, published=False, failure_reason="bili_api_error"
                    )
                except Exception:
                    pass
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
                # retry_wait → publishing
                if not self.proactive_comment_store.mark_publishing(action.action_id):
                    continue
                try:
                    # oid 从 CommentPolicy 历史拿不到，调用方需要保存
                    # 这里从 generation 阶段无法恢复 oid；用 action.bvid 反查
                    oid = await self._resolve_oid_from_bvid(action.bvid)
                    if not oid:
                        self.proactive_comment_store.mark_failed(
                            action.action_id, "NO_OID",
                            f"无法解析 bvid={action.bvid} 的 oid",
                        )
                        continue
                    # COM-601：重试前重新执行策略 + 安全检查（与首次发布一致）
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
                    if self.safety_checker is not None:
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
                            if not self.safety_checker.check_rate_limit(
                                scene="proactive_comment", account_id=self.account_id,
                            ):
                                logger.warning("重试主动评论频率限制触发")
                                self.proactive_comment_store.mark_failed(
                                    action.action_id, "RATE_LIMITED",
                                    "proactive_comment rate limited",
                                )
                                continue
                        except Exception as se:
                            # fail-closed
                            logger.error(f"重试主动评论安全检查异常（拒绝发布）: {se}", exc_info=True)
                            self.proactive_comment_store.mark_failed(
                                action.action_id, "SAFETY_CHECK_ERROR", str(se),
                            )
                            continue
                    # COM-602：创建审计记录（便于成功后 mark_published）
                    audit_id = None
                    if self.audit_store is not None:
                        try:
                            audit_id = await self.audit_store.record_async(
                                scene="proactive_comment",
                                persona_id=action.persona_id or "default",
                                prompt_preview=f"retry bvid={action.bvid}",
                                output=reply_text,
                                target={"bvid": action.bvid, "oid": oid},
                            )
                        except Exception as e:
                            logger.warning(f"重试审计记录失败: {e}")
                    success = await self.bili.post_comment(
                        oid=oid, content=reply_text,
                        comment_type=1, rpid=0, parent=0,
                    )
                except Exception as e:
                    logger.error(f"重试主动评论异常 action={action.action_id}: {e}")
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
                    continue
                if success:
                    # Task 4：检查 mark_published 返回值，失败时告警
                    if not self.proactive_comment_store.mark_published(action.action_id):
                        logger.warning(
                            f"Task 4: 重试主动评论 mark_published 失败 action={action.action_id}（状态可能已变更）"
                        )
                    logger.info(f"主动评论重试成功 action={action.action_id}")
                    # COM-602：记录 policy / interaction / audit（与首次发布一致）
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
                        try:
                            self.audit_store.mark_published(audit_id, published=True)
                        except Exception:
                            pass
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
                    self.proactive_comment_store.mark_retry_wait(
                        action.action_id, "RETRY_PUBLISH_FAILED",
                        "重试发布失败：bili.post_comment 返回 False",
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
                    logger.warning(f"主动评论重试失败 action={action.action_id}")
        except Exception as e:
            logger.error(f"处理重试主动评论失败: {e}", exc_info=True)

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
                # scheduled → claimed
                if not self.task_store.claim(task.task_id):
                    logger.warning(f"Task 5: TaskRun {task.task_id} claim 失败")
                    continue
                scene = task.scene
                if scene == "proactive_video":
                    self._spawn_memory_task(
                        self._do_proactive_video(task_id=task.task_id),
                        tag=f"retry_proactive_video:{task.task_id}",
                    )
                elif scene == "dynamic":
                    self._spawn_memory_task(
                        self._do_post_dynamic(task_id=task.task_id),
                        tag=f"retry_dynamic:{task.task_id}",
                    )
                else:
                    logger.warning(
                        f"Task 5: TaskRun {task.task_id} 场景 {scene} 不支持自动重试，标记失败"
                    )
                    self._fail_task(
                        task.task_id, "UNSUPPORTED_RETRY_SCENE",
                        f"场景 {scene} 不支持自动重试", retryable=False,
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

        # 全局暂停或账号风险暂停
        if self.safety_checker is not None and (
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

            my_uid = int(config.get("bilibili", {}).get("dede_user_id", 0) or 0)
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

                    # 从 last_msg 直接取消息内容（避免额外 API 调用）
                    last_msg = session.get("last_msg") or {}
                    history_messages = (
                        session.get("messages")
                        or session.get("message_list")
                        or session.get("session_messages")
                        or []
                    )
                    if not isinstance(history_messages, list):
                        history_messages = []
                    msg_content_raw = last_msg.get("content", "")
                    if not msg_content_raw:
                        # last_msg 没有 content，尝试调用消息 API
                        msgs_resp = await self.bili.get_session_messages(
                            sender_uid=talker_id,
                            receiver_uid=my_uid,
                            limit=5,
                        )
                        if not msgs_resp or msgs_resp.get("code") != 0:
                            continue
                        messages = msgs_resp.get("data", {}).get("messages", [])
                        if not messages:
                            continue
                        history_messages = messages
                        last_msg = messages[-1]
                        msg_content_raw = last_msg.get("content", "")

                    if not msg_content_raw:
                        continue

                    # B站私信 content 字段是 JSON 字符串 {"content":"消息内容"}
                    msg_content = ""
                    try:
                        content_obj = json.loads(msg_content_raw)
                        if isinstance(content_obj, dict):
                            msg_content = content_obj.get("content", "")
                        else:
                            msg_content = str(content_obj)
                    except (json.JSONDecodeError, TypeError):
                        msg_content = msg_content_raw

                    if not msg_content:
                        continue

                    # 确保是对方发来的消息（不是自己发的）
                    sender_uid_raw = last_msg.get("sender_uid", 0)
                    try:
                        sender_uid = int(sender_uid_raw)
                    except (TypeError, ValueError):
                        sender_uid = 0
                    if sender_uid and sender_uid == my_uid:
                        # 最后一条是自己发的，跳过
                        continue

                    # 获取对方用户名
                    talker_name = "用户"
                    talker_info = session.get("talker_info") or {}
                    if isinstance(talker_info, dict):
                        talker_name = talker_info.get("uname") or talker_info.get("name") or "用户"

                    # PRD-V5 §6.3 / PM-501：私信幂等键使用平台消息 ID
                    # （不得用 talker_id + 内容前 50 字）
                    from bilibot.services.pm_state_store import (
                        extract_platform_message_id,
                        TERMINAL_STATUSES as PM_TERMINAL,
                        RETRYABLE_STATUSES as PM_RETRYABLE,
                    )
                    platform_msg_id = extract_platform_message_id(last_msg)
                    if not platform_msg_id:
                        # 无法确定平台消息 ID，无法做幂等，跳过（避免重复发送）
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

                    logger.info("发现新私信: actor=%s", safe_pm.actor_pseudonym)

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

                    from bilibot.models import ReplyContext
                    reply_result = await self.reply_gen.generate_reply(
                        user_id=safe_pm.actor_pseudonym,
                        username="私信用户",
                        comment=safe_pm.text,
                        thread_id=f"pm_{safe_pm.actor_pseudonym}",
                        oid="",
                        comment_type=0,
                        reply_context=ReplyContext(memory_evidence=memory_evidence),
                        scene="private_message",
                    )

                    if not reply_result or not reply_result.get("reply"):
                        logger.debug("跳过私信回复 actor=%s", safe_pm.actor_pseudonym)
                        self.pm_state_store.mark_ignored(
                            pm_state.id, rule="empty_reply",
                        )
                        continue

                    reply_text = reply_result["reply"]
                    audit_id = reply_result.get("audit_id")  # PRD 4.16：私信审计
                    safe_reply = self._redact_private_message_runtime(
                        reply_text,
                        actor_id=safe_pm.actor_pseudonym,
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

                    # 安全检查
                    if self.safety_checker is not None:
                        passed, reason = await self.safety_checker.check_content(
                            safe_reply_text, scene="private_message",
                            persona_id=self._get_current_persona_id(),
                            account_id=self.account_id,
                        )
                        if not passed:
                            logger.warning(f"私信回复安全检查未通过: {reason}")
                            self.pm_state_store.mark_rejected(
                                pm_state.id, reason=reason or "safety_check_failed",
                            )
                            continue
                        if not self.safety_checker.check_rate_limit(scene="private_message", account_id=self.account_id):
                            logger.warning("私信频率限制触发")
                            self.pm_state_store.mark_deferred(
                                pm_state.id, reason="rate_limited",
                                error_code="PM_RATE_LIMITED",
                            )
                            continue
                        # PRD 4.4：预占频率配额
                        self.safety_checker.record_publish(scene="private_message", account_id=self.account_id)

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
                        logger.info("已回复私信 actor=%s", safe_pm.actor_pseudonym)
                        # PRD-V5 §6.3 / PM-501：标记已发布（终态）
                        self.pm_state_store.mark_published(pm_state.id)
                        try:
                            brain = getattr(self, "memory_brain", None)
                            if brain is not None:
                                _, archive_result = await brain.archive_private_message(
                                    platform_message_id=platform_msg_id,
                                    text=safe_reply_text,
                                    actor_id=safe_pm.actor_pseudonym,
                                    username="",
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
                        if self.safety_checker is not None:
                            self.safety_checker.record_content(
                                safe_reply_text, account_id=self.account_id
                            )
                        # 标记已读
                        await self.bili.ack_session(talker_id, my_uid)
                    else:
                        # PRD-V5 §6.3 / PM-501：发布失败 → retry_wait（独立退避）
                        # 平台明确返回失败（非本地异常），按 retry_wait 处理
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

                except Exception as e:
                    logger.warning("处理私信会话异常: %s", type(e).__name__)

        except Exception as e:
            logger.error("私信检查异常: %s", type(e).__name__)

    async def _process_retryable_pms(self):
        """PRD-V5 §6.3 / PM-501：重试 retry_wait 状态的私信

        - retry_wait：使用已保存的 generation_text 重新发送（不重新生成）
        - 超过 max_attempts → failed（由 mark_retry_wait 自动判定）
        - result_unknown 不自动重发（需人工对账）
        - PM 独立退避，与评论回复列表互不影响
        """
        if not self.bili or not self.reply_gen:
            return
        try:
            retryable = self.pm_state_store.list_retry_wait(account_id=self.account_id)
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
                    # 校验文本完整性
                    reply_text = pm_state.generation_text or ""
                    gen_hash = pm_state.generation_hash or ""
                    if not reply_text:
                        # 无生成文本，转 deferred 让下次发现重新生成
                        self.pm_state_store.mark_deferred(
                            pm_state.id, reason="no_generation_text",
                            error_code="RETRY_NO_GEN",
                        )
                        continue
                    if gen_hash:
                        expected = self.pm_state_store.compute_generation_hash(reply_text)
                        if expected != gen_hash:
                            logger.warning("重试私信文本 hash 不匹配: actor=%s", retry_actor)
                            self.pm_state_store.mark_deferred(
                                pm_state.id, reason="hash_mismatch",
                                error_code="RETRY_HASH_MISMATCH",
                            )
                            continue

                    # 直接使用原始文本重新发送（不调用 generate_reply）
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

                    safe_retry = self._redact_private_message_runtime(
                        reply_text,
                        actor_id=str(talker_id),
                    )
                    safe_retry_text = safe_retry.text

                    # 推进：retry_wait → publish_pending
                    self.pm_state_store.update_status(pm_state.id, "publish_pending")

                    try:
                        success = await self.bili.send_private_message(
                            receiver_id=talker_id, msg=reply_text,
                        )
                    except Exception as send_exc:
                        # 平台结果不确定 → result_unknown（不自动重发）
                        send_error = type(send_exc).__name__
                        logger.error(
                            "重试私信发送抛异常: actor=%s error=%s",
                            retry_actor,
                            send_error,
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

                    if success:
                        self.pm_state_store.mark_published(pm_state.id)
                        logger.info("重试私信发送成功: actor=%s", retry_actor)
                        try:
                            brain = getattr(self, "memory_brain", None)
                            if brain is not None:
                                _, archive_result = await brain.archive_private_message(
                                    platform_message_id=pm_state.platform_message_id,
                                    text=reply_text,
                                    actor_id=retry_actor,
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
                            self._pause_for_memory_failure()
                            logger.error(
                                "重试私信结果归档失败: actor=%s", retry_actor
                            )
                        if self.safety_checker is not None:
                            self.safety_checker.record_content(
                                reply_text, account_id=self.account_id,
                            )
                        try:
                            await self.bili.ack_session(talker_id, int(self._bot_uid or 0))
                        except Exception:
                            pass
                    else:
                        # 再次失败 → retry_wait（attempt 递增，超限转 failed）
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
                    logger.error("重试私信失败: actor=%s error=%s", retry_actor, type(e).__name__)
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

        # PRD V4 COM-001：proactive_video 和 proactive_comment 解耦
        # proactive_video 控制视频获取/分析/评价/记忆；proactive_comment 只控制是否发布主动评论
        # 两个开关不再以 AND 方式决定整个视频任务是否运行
        if features.get("proactive_video", True):
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
        if features.get("dynamic_post", True):
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
                        if event.get("event_type") == "video_observation":
                            for source in event.get("sources") or []:
                                if source.get("source_type") in (
                                    "asr", "subtitle", "visual_description", "behavior_log",
                                ):
                                    has_full_observation = True
                                    break
                        if has_full_observation:
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
            try:
                cid = video_info.get("cid", 0)
                if not cid:
                    pages = video_info.get("pages", [])
                    if pages:
                        cid = pages[0].get("cid", 0)
                if not cid:
                    raise RuntimeError("视频缺少 CID")

                import os as _os
                video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
                save_path = _os.path.join(video_temp_dir, f"{bvid}")
                video_file = await self.bili.download_video(bvid, cid, save_path, quality=32)
                if not video_file or not _os.path.exists(video_file):
                    raise RuntimeError(f"视频下载失败: {bvid}")

                logger.info(f"@回复：视频已下载，开始视听分析: {video_file}")
                video_file_to_cleanup = video_file
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
                logger.warning("@回复：视频 ASR 失败 code=%s retryable=%s，降级为元数据回复",
                               e.code, e.retryable)
                return False
            except Exception as e:
                logger.warning("@回复：视频理解失败，降级为元数据回复: %s: %s",
                               type(e).__name__, e)
                return False

            # 归档到 memory_brain（让后续 build_context 命中缓存）
            try:
                from bilibot.memory_brain.ingestion import video_observation

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
                    )
                )
                logger.info(f"@回复：视频已归档 bvid={bvid} oid={oid_int}")
            except Exception as archive_exc:
                logger.warning(f"@回复：视频归档失败: {archive_exc}")
                return False
            finally:
                import os as _os
                import shutil as _shutil
                if video_file_to_cleanup:
                    try:
                        _os.remove(video_file_to_cleanup)
                    except OSError:
                        pass
                if work_dir_to_cleanup:
                    _shutil.rmtree(work_dir_to_cleanup, ignore_errors=True)

            return True
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
        """
        logger.info("开始主动看视频...")
        if not self.bili:
            if task_id:
                self._fail_task(task_id, "NO_BILI_API", "bili API 未初始化")
            return

        # PRD §5.9：全局暂停或账号风险暂停时跳过
        if self.safety_checker is not None and (
            self.safety_checker.is_paused()
            or self.safety_checker.is_account_paused(self.account_id)
        ):
            logger.info(f"跳过主动看视频（暂停状态）")
            if task_id:
                self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
            return

        # PRD-V5 §7：claim → start
        # Task 2 修复：手动触发任务未经 _claim_task_for_slot，状态仍为 scheduled，
        # 需先 claim 再 start；定时任务已由 _claim_task_for_slot claim 过（状态为 claimed）
        if task_id:
            from bilibot.services.task_store import TRIGGER_MANUAL, STATUS_SCHEDULED
            _task = self.task_store.get(task_id)
            if _task is None:
                logger.warning(f"TaskRun {task_id} 不存在")
                return
            if _task.trigger_type == TRIGGER_MANUAL and _task.status == STATUS_SCHEDULED:
                if not self.task_store.claim(task_id):
                    logger.warning(f"TaskRun {task_id} claim 失败（可能已被处理）")
                    return
            if not self.task_store.start(task_id):
                logger.warning(f"TaskRun {task_id} start 失败（可能已被处理）")
                return

        # PRD V4 COM-001：features 在方法内独立读取（与 _check_proactive_tasks 解耦）
        features = self.config_loader.get_raw_config().get("features", {})

        try:
            # 1. 获取视频（C: 推荐流 / D: 分区热门随机翻页，各 50% 概率）
            source = random.choice(["recommend", "region"])
            if source == "recommend":
                data = await self.bili.get_recommend_videos()
                logger.info("视频来源: 推荐流")
            else:
                page = random.randint(1, 5)
                data = await self.bili.get_region_hot_videos(rid=0, page=page)
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

            # V6 dedupe is derived from permanent account-brain events, never JSON.
            async def _not_observed(candidate):
                bvid_value = str(candidate.get("bvid") or "")
                brain = getattr(self, "memory_brain", None)
                if not bvid_value or brain is None:
                    return bool(bvid_value)
                seen = await asyncio.to_thread(brain.has_identifier, bvid_value)
                return not seen

            available_videos = []
            for candidate in videos:
                if await _not_observed(candidate):
                    available_videos.append(candidate)
            if not available_videos:
                logger.info("所有热门视频均已看过，跳过主动看视频")
                if task_id:
                    self._fail_task(task_id, "ALL_WATCHED", "所有热门视频均已看过", retryable=False)
                return

            # 随机选一个视频
            # Task 24：视频选择目前为简单随机采样（已删除未启用的 StrategyEngine 死代码，
            # 若未来需要基于热度/相关度/疲劳的多因子加权选片，需重新实现并接入此处 + 充分测试）
            video = random.choice(available_videos)
            bvid = video.get("bvid", "")
            if not bvid:
                if task_id:
                    self._fail_task(task_id, "NO_BVID", "视频缺少 bvid", retryable=False)
                return

            oid = await self.bili.get_video_oid_by_bvid(bvid)
            if not oid:
                if task_id:
                    self._fail_task(task_id, "NO_OID", "获取视频 oid 失败", retryable=False)
                return

            # 2. 获取视频详情
            video_info = await self.bili.get_video_info(oid)
            if not video_info:
                if task_id:
                    self._fail_task(task_id, "NO_VIDEO_INFO", "获取视频详情失败", retryable=False)
                return

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

            logger.info(f"正在看视频: 《{title}》 by {owner}")

            # PRD-V5 VID-502：结构化视频上下文 — 各来源独立赋值，不互相覆盖
            # 修复 scheduler.py 旧实现复用 video_content 字符串导致搜索结果被视听分析覆盖的 bug
            ctx = ProactiveVideoContext(bvid=bvid)
            ctx.metadata = video_info
            ctx.hot_comments = hot_comments if hot_comments else None
            video_file_to_cleanup = None
            work_dir_to_cleanup = None

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

            # 来源 2：视频内容理解（视听双轨分析）— 失败不清空搜索结果
            if self.video_understanding and self.video_understanding.is_available():
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
                    # - 仅当音轨 ASR 真正失败时才视为未完成，抛错等待重试。
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
                        ctx.degradation_reasons.append(f"video_understanding: {degradation}")
                        logger.info(f"视频理解降级，继续元数据归档: {degradation}")

                    ctx.audiovisual = vu_result
                    av_log = ctx.audiovisual_log
                    if av_log:
                        logger.info(f"视频理解完成，行为日志 {len(av_log)} 字")
                    elif not degradation:
                        logger.warning("视频理解未生成行为日志")
                except ASRTranscriptionError as e:
                    logger.warning(
                        "视频 ASR 提取未完成，保留媒体并等待任务重试: code=%s retryable=%s",
                        e.code,
                        e.retryable,
                    )
                    raise
                except Exception as e:
                    logger.warning(
                        "视频提取未完成，保留媒体并等待任务重试: %s",
                        type(e).__name__,
                    )
                    raise

            # VID-502：统一截断并标记来源；metadata/hot_comments 已由 evaluate_video 单独渲染，
            # 此处只传搜索参考 + 视听分析 + 降级说明，避免重复段落
            video_content = ctx.to_prompt_sections(
                include_metadata=False, include_hot_comments=False,
            )

            # Full extracted source archive is the commit boundary. No evaluation,
            # interaction or temporary cleanup happens before this succeeds.
            from bilibot.memory_brain.ingestion import video_observation

            observation_key = task_id or f"{bvid}:{oid}"
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
                )
            )

            # The complete extracted text is durable; media/keyframes can now go.
            import os as _os
            import shutil as _shutil
            if video_file_to_cleanup:
                try:
                    _os.remove(video_file_to_cleanup)
                except OSError:
                    pass
            if work_dir_to_cleanup:
                _shutil.rmtree(work_dir_to_cleanup, ignore_errors=True)

            # 3. LLM 评价视频
            evaluation = None
            if self.comment_generator:
                try:
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

            # PRD V4 VID-006：LLM 失败时所有有副作用动作默认为 false
            # 不再用硬编码 want_like=True 的降级评价，只保留无副作用的元数据字段
            llm_ok = isinstance(evaluation, dict)
            if not llm_ok:
                evaluation = {
                    "score": 0,
                    "mood": "平静",
                    "comment": "",
                    "review": "",
                }
                logger.warning("LLM 评价失败，所有互动动作默认 false（VID-006）")

            score = evaluation.get("score", 0) if llm_ok else 0
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

            # PRD V4 VID-003：区分 inspected / watched 语义
            # inspected：读取元数据或下载分析，但未向 B站上报观看
            # watched：按照平台允许的接口上报观看进度并得到成功响应（默认关闭）
            watch_state = "inspected"
            watched_flag = False

            # 4. PRD V4 VID-006：互动决策由确定性 PolicyEngine 执行
            # 模型只输出建议，最终决策受开关、日预算、评分阈值和去重状态控制
            decisions = self.interaction_policy.evaluate(
                llm_suggestion=evaluation if llm_ok else None,
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
                            text=f"已点赞视频《{title}》",
                            published=True,
                            title=title,
                            scene="proactive_video",
                            metadata={"bvid": bvid, "oid": str(oid)},
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
                            text=f"已给视频《{title}》投币",
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
                            text=f"已收藏视频《{title}》",
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
            interaction_summary = self.interaction_policy.get_today_summary()
            experience_lines = [
                f"观察了视频《{title}》，UP主 {owner}",
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
                        "watch_state": watch_state,
                        "watched": watched_flag,
                        "action_outcomes": action_outcomes,
                        "interaction_budget": interaction_summary,
                    },
                    importance=max(0.1, min(1.0, score / 10.0)),
                )
            )

            # 更新情绪
            if self.behavior_sim:
                try:
                    self.behavior_sim.update_mood("watched_video")
                except Exception:
                    pass

            logger.info(f"视频处理完成: 《{title}》 score={score} comment={'是' if comment_text else '否'}")

            # PRD-V5 §7：只有真正完成才 succeed（创建协程 ≠ 成功）
            if task_id:
                self._succeed_task(task_id, {
                    "success": True,
                    "summary": f"视频《{title}》处理完成 score={score}",
                    "bvid": bvid,
                    "title": title,
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
            result = await self.llm.generate(prompt, max_tokens=120, temperature=0.7)
            return result.strip() if result else None
        except Exception as e:
            logger.warning(f"生成图片 prompt 失败: {e}")
            return None

    async def _do_post_dynamic(self, task_id: Optional[str] = None):
        """发布动态

        PRD V3 §8.4 / §9.3：
        - 主流程使用 orchestrator.build_dynamic_prompt
        - LLM 失败时不得硬编码万能动态自动发布
        - 主写入 SQLite memory_atoms（保留 JSON 备份）

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
        # Task 2 修复：手动触发任务未经 _claim_task_for_slot，状态仍为 scheduled，
        # 需先 claim 再 start；定时任务已由 _claim_task_for_slot claim 过（状态为 claimed）
        if task_id:
            from bilibot.services.task_store import TRIGGER_MANUAL, STATUS_SCHEDULED
            _task = self.task_store.get(task_id)
            if _task is None:
                logger.warning(f"TaskRun {task_id} 不存在")
                return
            if _task.trigger_type == TRIGGER_MANUAL and _task.status == STATUS_SCHEDULED:
                if not self.task_store.claim(task_id):
                    logger.warning(f"TaskRun {task_id} claim 失败（可能已被处理）")
                    return
            if not self.task_store.start(task_id):
                logger.warning(f"TaskRun {task_id} start 失败（可能已被处理）")
                return

        try:
            # 1. 收集主题池（来自 V6 brain，不再读取 watch_log.json）
            related_videos: List[str] = []
            brain = getattr(self, "memory_brain", None)
            if brain:
                try:
                    recent_video_events = await asyncio.to_thread(
                        brain.list_events,
                        limit=20,
                        source_type="video_experience",
                    )
                    related_videos = [
                        f"《{event.get('title', '')}》"
                        for event in recent_video_events[:3]
                        if event.get("title")
                    ]
                except Exception:
                    pass

            # PRD V4 DYN-001：主题选择
            # dynamic_publish.topics 非空时按权重或轮换选择主题
            # 最近已使用主题需记录，避免连续重复
            # topics 为空才允许自由发挥，代码不得固定传 topic=None 忽略配置
            selected_topic: Optional[str] = None
            _cfg_loader = getattr(self, "config_loader", None)
            dp_cfg = _cfg_loader.get_raw_config().get("dynamic_publish", {}) if _cfg_loader else {}
            topics_cfg = dp_cfg.get("topics", []) or []
            if topics_cfg:
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
            # selected_topic 为 None 表示 topics 为空，允许自由发挥

            # 召回近期经历（视频观察、评论互动、反思等），让动态内容有据可依
            memory_evidence = ""
            if brain:
                try:
                    from bilibot.memory_brain import RecallQuery
                    recall_query_text = (
                        f"最近看的视频、心情、想法{': ' + selected_topic if selected_topic else ''}"
                    )
                    recall_result = await brain.recall(
                        RecallQuery(
                            current_message=recall_query_text,
                            account_id=self.account_id,
                            scene="dynamic_post",
                        )
                    )
                    if recall_result and recall_result.prompt_evidence:
                        memory_evidence = recall_result.prompt_evidence
                        logger.info(f"动态发布召回记忆: {len(recall_result.events)} 条事件")
                except Exception as recall_exc:
                    logger.debug(f"动态发布记忆召回失败: {recall_exc}")

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
                    prompt_dict = self.orchestrator.build_dynamic_prompt(
                        topic=selected_topic,
                        related_videos=related_videos,
                        persona=persona,
                        memory_evidence=memory_evidence,
                    )
                    system_prompt = prompt_dict.get("system", "")
                    user_prompt = prompt_dict.get("user", "")
                except Exception as e:
                    logger.warning(f"orchestrator.build_dynamic_prompt 失败: {e}")

            if not system_prompt or not user_prompt:
                # 仅在 orchestrator 不可用时回退到 legacy personality
                system_prompt = self.personality.get_system_prompt()
                if selected_topic:
                    user_prompt = (
                        f"现在轮到你发B站动态了，主题是「{selected_topic}」。"
                        "20-80字，口语化，像真人发动态的感觉。"
                    )
                else:
                    user_prompt = (
                        "现在轮到你发B站动态了，想发点什么？20-80字，口语化，像真人发动态的感觉。"
                    )

            # 3. 调用 LLM
            content = await self.llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=200,
            )

            # LLM 失败时不再硬编码万能动态自动发布（PRD V3 §8.4）
            if not content:
                logger.warning("LLM 生成动态失败，跳过本次发布（不自动发万能动态）")
                return

            content = content.strip().replace("\n\n", "\n")
            # PRD V3 §5.4 (P2-4)：截断不加省略号
            if len(content) > 200:
                content = content[:200]

            dynamic_key = task_id or hashlib.sha256(content.encode("utf-8")).hexdigest()
            # PRD V6：动态发布作为内部任务，不单独记录 intent；
            # 仅在最终结果（成功/失败/拒绝/异常）时归档一次，避免同一动态出现两条记忆。

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
                    metadata={"reason_code": "NO_SAFETY_CHECKER"},
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
                    metadata={"reason_code": "SAFETY_CHECK_ERROR"},
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
                    metadata={"reason_code": "SAFETY_REJECTED"},
                )
                return
            if not self.safety_checker.check_rate_limit(
                scene="dynamic_post", account_id=self.account_id
            ):
                logger.warning("动态发布频率限制触发，跳过本次")
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
                    metadata={"reason_code": "RATE_LIMITED"},
                )
                return
            # PRD 4.4：预占频率配额
            self.safety_checker.record_publish(
                scene="dynamic_post", account_id=self.account_id
            )

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
            try:
                success = await self.bili.post_dynamic_text(
                    content, images=image_list if image_list else None
                )
            except Exception as publish_exc:
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
                            "topic": selected_topic or "",
                            "reason_code": type(publish_exc).__name__,
                        },
                    )
                except Exception:
                    logger.error("unknown dynamic publish result could not be archived")
                return
            if not success:
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
                        metadata={"topic": selected_topic or "", "has_image": has_image},
                    )
                except Exception:
                    logger.error("published dynamic result could not be archived")
                    if task_id:
                        self._mark_task_result_unknown(
                            task_id, "dynamic published but V6 result archive failed"
                        )
                    return

                logger.info(f"动态发布成功: {content[:50]}...")
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
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="failed",
                    title=selected_topic or "动态",
                    scene="dynamic_post",
                    metadata={
                        "topic": selected_topic or "",
                        "reason_code": "BILI_API_FALSE",
                    },
                )
                # PRD-V5 §7：发布失败 → retry_wait/failed
                if task_id:
                    self._fail_task(task_id, "DYNAMIC_PUBLISH_FAILED",
                                    "bili.post_dynamic_text 返回 False", retryable=True)

        except Exception as e:
            logger.error(f"发布动态失败: {e}", exc_info=True)
            # PRD-V5 §7：异常 → retry_wait/failed
            if task_id:
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
            # DYN-602：暂停检查（fail-closed，与 _do_post_dynamic 一致）
            if self.safety_checker is not None:
                if self.safety_checker.is_paused():
                    # Task 11.1：暂停时回退草稿状态为 retry_wait，避免卡死在 publishing
                    try:
                        self._get_draft_store().mark_retry_wait(
                            draft_id, "dynamic_post paused"
                        )
                    except Exception as _e:
                        logger.warning(f"Task 11.1: 暂停回退草稿状态失败: {_e}")
                    self._fail_task(task_id, "GLOBAL_PAUSED", "全局暂停状态", retryable=True)
                    return
                if self.safety_checker.is_account_paused(self.account_id):
                    # Task 11.1：账号暂停时回退草稿状态为 retry_wait
                    try:
                        self._get_draft_store().mark_retry_wait(
                            draft_id, "dynamic_post paused"
                        )
                    except Exception as _e:
                        logger.warning(f"Task 11.1: 账号暂停回退草稿状态失败: {_e}")
                    self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
                    return

            store = self._get_draft_store()
            draft = store.get(draft_id)
            if draft is None:
                self._fail_task(task_id, "DRAFT_NOT_FOUND",
                                f"草稿不存在: {draft_id}", retryable=False)
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
                            "reason_code": "IMAGE_UPLOAD_FAILED",
                        },
                    )
                    self._fail_task(task_id, "IMAGE_UPLOAD_FAILED",
                                    "草稿配图上传失败", retryable=True)
                    return

            # DYN-602：发布前频率限制检查
            if self.safety_checker is not None:
                if not self.safety_checker.check_rate_limit(
                    scene="dynamic_post", account_id=self.account_id
                ):
                    # Task 11.2：限流时回退草稿状态为 retry_wait，避免卡死在 publishing
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
                            "reason_code": "RATE_LIMITED",
                        },
                    )
                    return

            # 发布
            try:
                success = await self.bili.post_dynamic_text(
                    content, images=image_list if image_list else None
                )
            except Exception as publish_exc:
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
                            "reason_code": error_name,
                        },
                    )
                except Exception:
                    logger.error(
                        "unknown dynamic draft publish result could not be archived"
                    )
                return
            if not success:
                self._check_bili_risk_control("dynamic_post")

            if success:
                store.mark_published(draft_id)
                logger.info(f"动态草稿发布成功: {draft_id}")
                # DYN-602：记录频率配额 + 内容（与 _do_post_dynamic 一致）
                if self.safety_checker is not None:
                    try:
                        self.safety_checker.record_publish(
                            scene="dynamic_post", account_id=self.account_id
                        )
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
                        metadata={"draft_id": draft_id, "has_image": bool(image_list)},
                    )
                except Exception:
                    logger.error("published dynamic draft result could not be archived")
                    self._mark_task_result_unknown(
                        task_id, "dynamic draft published but V6 result archive failed"
                    )
                    return
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
                        "reason_code": "BILI_API_FALSE",
                    },
                )
                self._fail_task(task_id, "DYNAMIC_PUBLISH_FAILED",
                                "bili.post_dynamic_text 返回 False", retryable=True)
        except Exception as e:
            logger.error(f"发布动态草稿失败: {e}", exc_info=True)
            try:
                self._get_draft_store().mark_retry_wait(draft_id, str(e))
            except Exception:
                pass
            self._fail_task(task_id, "DYNAMIC_ERROR", str(e), retryable=True)

    def create_draft_publish_task(self, draft_id: str) -> Optional[str]:
        """PRD-V5 §4.1 DYN-501：为已审核通过的草稿创建发布 TaskRun

        供 API approve / retry 端点调用。返回 task_id。
        """
        from bilibot.services.task_store import TRIGGER_MANUAL
        try:
            idem_key = (
                f"{self.account_id or '_default'}:dynamic_publish:"
                f"{draft_id}:{int(time.time() * 1000)}"
            )
            task = self.task_store.create(
                account_id=self.account_id or "_default",
                scene="dynamic",
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
        - 写入 audit + SQLite memory_atoms
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

            # 3. 调用 LLM
            summary = await self.llm.generate(
                prompt=user_prompt,
                system_prompt=system_prompt,
                max_tokens=800,
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
                if task and task.scene == "dynamic":
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
