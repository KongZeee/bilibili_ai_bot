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
import logging
import random
import json
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

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

logger = logging.getLogger("bilibot.scheduler")


class Scheduler:
    """主调度器（最小可用版本）"""

    def __init__(self, config_loader: ConfigLoader, user_state=None, llm=None,
                 bili=None, data_store=None, persona_store=None, orchestrator=None,
                 audit_store=None, context_builder=None, comment_context_service=None,
                 safety_checker=None, account_id: str = "", video_understanding_service=None,
                 image_provider=None, knowledge_memory=None, memory_write_queue=None,
                 proactive_comment_store=None):
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
        self.running = False

        # PRD V3 §3.2 / §4.2：后台任务引用集合，防止 GC + 记录异常
        self._running_tasks: set = set()

        # 初始化各子系统
        self.personality = PersonalitySystem(config_loader)

        # MEM-501：knowledge_memory 由 AccountInstance 注入（单一实例，不再自行创建）
        self.knowledge_memory = knowledge_memory
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
        self._consolidation_triggered: set = set()  # 日终记忆清算触发标记
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

    async def start(self):
        """启动调度器"""
        self.running = True
        logger.info("BiliBot 调度器启动")

        # 加载已回复记录
        self._load_replied_state()

        # PRD V3 §4.8：加载评论失败计数器（重启后恢复阈值保护）
        self._load_fail_counts()

        # PRD 4.15：启动时清理过期记忆原子（PRD V3 §4.6：用 async 版本避免阻塞）
        if self.knowledge_memory and hasattr(self.knowledge_memory, "cleanup_expired_async"):
            try:
                purged = await self.knowledge_memory.cleanup_expired_async()
                if purged:
                    logger.info(f"启动清理过期记忆原子: {purged} 条")
            except Exception as e:
                logger.warning(f"启动清理过期记忆失败: {e}")
        elif self.knowledge_memory and hasattr(self.knowledge_memory, "cleanup_expired"):
            try:
                purged = self.knowledge_memory.cleanup_expired()
                if purged:
                    logger.info(f"启动清理过期记忆原子: {purged} 条")
            except Exception as e:
                logger.warning(f"启动清理过期记忆失败: {e}")

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
                    self._consolidation_triggered.clear()
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
                    and now.minute == 0
                    and current_time not in self._consolidation_triggered
                ):
                    self._consolidation_triggered.add(current_time)
                    try:
                        # PRD V3 §2.2.2：日终清算改为 KnowledgeBaseMemory.cleanup_expired()
                        # PRD V3 §4.6：用 async 版本避免阻塞事件循环
                        if self.knowledge_memory and hasattr(self.knowledge_memory, "cleanup_expired_async"):
                            purged = await self.knowledge_memory.cleanup_expired_async()
                            if purged:
                                logger.info(f"日终清理过期记忆原子: {purged} 条")
                            else:
                                logger.info("日终清算完成：无过期记忆")
                        elif self.knowledge_memory and hasattr(self.knowledge_memory, "cleanup_expired"):
                            purged = self.knowledge_memory.cleanup_expired()
                            if purged:
                                logger.info(f"日终清理过期记忆原子: {purged} 条")
                            else:
                                logger.info("日终清算完成：无过期记忆")
                        # PRD V5 Task 16 / MEM-601：日终遗忘低重要性记忆
                        # forget_low_importance 内部会检查 enable_forgetting
                        if self.knowledge_memory and hasattr(self.knowledge_memory, "forget_low_importance_async"):
                            try:
                                forgotten = await self.knowledge_memory.forget_low_importance_async()
                                if forgotten:
                                    logger.info(f"日终遗忘低重要性记忆: {forgotten} 条")
                            except Exception as fe:
                                logger.error(f"日终遗忘低重要性记忆失败: {fe}", exc_info=True)
                    except Exception as e:
                        logger.error(f"日终记忆清算失败: {e}", exc_info=True)

                # REP-602：恢复卡在中间态的评论（context_building/
                # generation_pending/safety_pending/publish_pending 超过 10 分钟
                # 未推进 → 转为 deferred，由后续 _process_retryable_comments 拾取）
                try:
                    self.reply_state_store.recover_stuck_intermediate(timeout_minutes=10)
                except Exception as e:
                    logger.warning(
                        f"REP-602: 恢复卡在中间态评论失败: {e}", exc_info=True
                    )

                # 1. 检查评论回复
                await self._check_new_comments()

                # 1.2 PRD V4 REP-005：重试 deferred/retry_wait 的评论
                await self._process_retryable_comments()

                # 1.25 PRD-V5 §10.2 COM-501：重试 retry_wait 的主动评论
                await self._process_retryable_proactive_comments()

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
        logger.info("调度器已关闭")

    def stop(self):
        """停止调度器"""
        self.running = False

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
            notifications = await self.bili.get_reply_notifications()
            if not notifications:
                logger.info("通知 API 返回空")
                return
            if notifications.get("code") != 0:
                logger.info(f"通知 API 返回错误: code={notifications.get('code')} msg={notifications.get('message')}")
                return

            items = notifications.get("data", {}).get("items", [])
            if not items:
                logger.info("评论区无新通知")
                return

            logger.info(f"通知 API 返回 {len(items)} 条评论")

            batch_size = config.get("reply", {}).get("batch_size", 10)
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
                logger.info(f"评论通知 {len(items)} 条，全部已回复过，跳过")
                return

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
                    if root_id or source_id:
                        try:
                            context_root = root_id if root_id else source_id
                            replies_data = await self.bili.get_comment_replies(
                                oid=oid, root=context_root, comment_type=comment_type, ps=30,
                            )
                            if replies_data and replies_data.get("code") == 0:
                                reply_replies = replies_data.get("data", {}).get("replies", [])
                                if reply_replies:
                                    context_lines = []
                                    for r in reply_replies:
                                        r_user = r.get("member", {}).get("uname", "?")
                                        r_text = r.get("content", {}).get("message", "")
                                        if r_text:
                                            # 标记 Bot 自己的回复（PRD 3.10：修复运算符优先级 bug）
                                            bot_uid_int = int(self._bot_uid) if self._bot_uid else None
                                            is_bot = bot_uid_int is not None and r.get("mid", 0) == bot_uid_int
                                            speaker = self._bot_name if is_bot else r_user
                                            context_lines.append(f"{speaker}: {r_text}")
                                    if context_lines:
                                        comment_context = "\n".join(context_lines)
                                        logger.info(f"获取评论上下文: {len(context_lines)} 条对话")
                        except Exception as e:
                            logger.warning(f"获取评论上下文失败: {e}")

                    # PRD §5.9：黑名单过滤 → ignored（终态）
                    if self.safety_checker is not None and self.safety_checker.is_blacklisted(user_id):
                        logger.info(f"用户 {username}({user_id}) 在黑名单中，跳过回复")
                        self.reply_state_store.mark_ignored(
                            comment_type, reply_id, rule="blacklist",
                        )
                        continue

                    # PRD V4 §4.3.1：构建完整 ReplyContext
                    reply_context = None
                    if self.comment_context_service is not None:
                        try:
                            reply_context = await self.comment_context_service.build_context(
                                notification=item, current_user_id=user_id,
                            )
                        except Exception as e:
                            logger.warning(f"构建评论上下文失败，降级处理: {e}")
                            reply_context = None

                    # PRD V4 §9.1：状态 → generation_pending
                    self.reply_state_store.upsert(comment_type, reply_id, "generation_pending")

                    # 生成回复（传入 reply_context + 评论上下文）
                    try:
                        reply_result = await self.reply_gen.generate_reply(
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

                    if not reply_result or not reply_result.get("reply"):
                        # LLM 明确决定不回复 → ignored（终态）
                        logger.debug(f"LLM 决定跳过回复 from {username}")
                        self.reply_state_store.mark_ignored(
                            comment_type, reply_id, rule="llm_no_reply",
                        )
                        continue

                    reply_text = reply_result["reply"]
                    audit_id = reply_result.get("audit_id")
                    context_meta = reply_result.get("context_meta", {}) or {}

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
                    success = await self.bili.post_comment(
                        oid=oid,
                        content=reply_text,
                        comment_type=comment_type,
                        rpid=comment_root,
                        parent=source_id,
                    )
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

                        # PRD V3 §3.2：记忆写入改为异步，不阻塞回复链路
                        if self.knowledge_memory:
                            # PRD V4 REP-006：记忆写入传入 persona_id（账号隔离）
                            _persona_id = self._get_current_persona_id()
                            self._spawn_memory_task(
                                self.knowledge_memory.save_conversation_as_memory(
                                    conversation_history=[
                                        {"role": "user", "content": comment_text,
                                         "user_id": user_id, "username": username},
                                        {"role": "assistant", "content": reply_text,
                                         "user_id": self._bot_uid, "username": self._bot_name},
                                    ],
                                    user_id=user_id,
                                    username=username,
                                    session_id=str(reply_id),
                                    bot_name=self._bot_name,
                                    persona_id=_persona_id,
                                ),
                                tag=f"save_conversation_as_memory(reply_id={reply_id})",
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

                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"处理评论失败: {e}")

        except Exception as e:
            logger.error(f"检查评论失败: {e}")

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
                                existing = replies_data and replies_data.get("replies") or []
                                already_replied = any(
                                    str(r.get("member", {}).get("mid", "")) == self._bot_uid
                                    for r in existing
                                )
                                if already_replied:
                                    logger.info(f"幂等检查：rpid={rpid} 已有 Bot 回复，跳过重试")
                                    self.reply_state_store.mark_published(ct, rpid)
                                    continue
                            except Exception as e:
                                logger.warning(f"幂等检查失败，继续重试: {e}")
                        # 直接使用原始文本重新发布（不调用 generate_reply）
                        success = await self.bili.post_comment(
                            oid=oid, content=reply_text, comment_type=ct,
                            rpid=comment_root, parent=source_id,
                        )
                        if success:
                            # PRD-V5 §6.2：发布成功保留同一 generation_hash
                            self.reply_state_store.mark_published(ct, rpid)
                            logger.info(f"重试发布成功: rpid={rpid}")
                        else:
                            # 再次失败 → retry_wait（attempts 自动递增，超限转 failed）
                            # generation 字段由 upsert 自动保留
                            self.reply_state_store.mark_retry_wait(
                                ct, rpid, reason="retry_publish_failed", error_code="RETRY_PUBLISH_FAILED",
                            )
                            logger.warning(f"重试发布失败: rpid={rpid}")
                    elif state == "deferred":
                        # 重新走完整流程：标记为 discovered 让下次 _check_new_comments 拾取
                        self.reply_state_store.upsert(ct, rpid, "discovered")
                        logger.info(f"deferred 评论恢复为 discovered: rpid={rpid}")
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
        if comment_text:
            forbidden_phrases = ["我完整看完了", "我看完了", "完整看完"]
            for phrase in forbidden_phrases:
                if phrase in comment_text:
                    logger.warning(f"评论包含与 watch_state 冲突的表达 '{phrase}'，拒绝发布")
                    comment_text = ""
                    break

        if not comment_text:
            self.proactive_comment_store.mark_failed(
                action_id, "NO_COMMENT_TEXT", "评论生成失败或为空",
            )
            return ""

        # 保存生成结果（便于 retry 时复用）
        self.proactive_comment_store.save_generation(action_id, comment_text)

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
                    return ""
                if not self.safety_checker.check_rate_limit(
                    scene="proactive_comment", account_id=self.account_id,
                ):
                    logger.warning("主动评论频率限制触发，跳过")
                    self.proactive_comment_store.mark_failed(
                        action_id, "RATE_LIMITED", "proactive_comment rate limited",
                    )
                    return ""
                # PRD 4.4：预占频率配额
                self.safety_checker.record_publish(
                    scene="proactive_comment", account_id=self.account_id,
                )
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
                audit_id = self.audit_store.record(
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
            self.proactive_comment_store.mark_published(action_id)
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
                            audit_id = self.audit_store.record(
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
                    continue
                if success:
                    self.proactive_comment_store.mark_published(action.action_id)
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
                else:
                    self.proactive_comment_store.mark_retry_wait(
                        action.action_id, "RETRY_PUBLISH_FAILED",
                        "重试发布失败：bili.post_comment 返回 False",
                    )
                    logger.warning(f"主动评论重试失败 action={action.action_id}")
        except Exception as e:
            logger.error(f"处理重试主动评论失败: {e}", exc_info=True)

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
            logger.info(f"检查私信: {len(session_list)} 个会话, my_uid={my_uid}")

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

                    logger.info(f"发现新私信 from {talker_id}: {msg_content[:30]}...")

                    # 黑名单过滤
                    if self.safety_checker is not None and self.safety_checker.is_blacklisted(str(talker_id)):
                        logger.info(f"私信用户 {talker_id} 在黑名单中，跳过")
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
                        logger.warning(f"无法提取私信平台消息 ID，跳过: talker_id={talker_id}")
                        continue

                    pm_state = self.pm_state_store.ensure_discovered(
                        account_id=self.account_id,
                        platform_message_id=platform_msg_id,
                        talker_id=str(talker_id),
                    )

                    # 幂等：终态或进行中则跳过（由 _process_retryable_pms 处理 retry_wait）
                    if pm_state.status in PM_TERMINAL:
                        logger.debug(
                            f"私信已处于终态 {pm_state.status}，跳过: "
                            f"talker_id={talker_id} msg_id={platform_msg_id}"
                        )
                        continue
                    if pm_state.status not in ("discovered",):
                        # retry_wait / deferred 由独立重试循环处理；中间态跳过避免并发
                        logger.debug(
                            f"私信状态 {pm_state.status} 非 discovered，跳过: "
                            f"msg_id={platform_msg_id}"
                        )
                        continue

                    # 推进：discovered → generation_pending
                    pm_state = self.pm_state_store.update_status(
                        pm_state.id, "generation_pending",
                    )

                    # 用 reply_gen 生成回复（复用评论回复逻辑）
                    # PRD-V5 §4.3 SEA-501：私信场景须传 scene=private_message
                    reply_result = await self.reply_gen.generate_reply(
                        user_id=str(talker_id),
                        username=talker_name,
                        comment=msg_content,
                        thread_id=f"pm_{talker_id}",
                        oid=str(talker_id),
                        comment_type=0,
                        scene="private_message",
                    )

                    if not reply_result or not reply_result.get("reply"):
                        logger.debug(f"跳过私信回复 to {talker_id}")
                        self.pm_state_store.mark_ignored(
                            pm_state.id, rule="empty_reply",
                        )
                        continue

                    reply_text = reply_result["reply"]
                    audit_id = reply_result.get("audit_id")  # PRD 4.16：私信审计

                    # PRD-V5 §6.3 / PM-501：生成文本持久化（安全检查之前）
                    pm_state = self.pm_state_store.save_generation_result(
                        pm_state.id,
                        text=reply_text,
                        persona_id=self._get_current_persona_id(),
                    )

                    # 推进：generation_pending → safety_pending
                    pm_state = self.pm_state_store.update_status(
                        pm_state.id, "safety_pending",
                    )

                    # 安全检查
                    if self.safety_checker is not None:
                        passed, reason = await self.safety_checker.check_content(
                            reply_text, scene="private_message",
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
                            msg=reply_text,
                        )
                    except Exception as send_exc:
                        # 本地异常：平台结果不确定 → result_unknown（不自动重发）
                        logger.error(
                            f"私信发送抛异常（平台结果不确定）: {send_exc}",
                            exc_info=True,
                        )
                        self.pm_state_store.mark_result_unknown(
                            pm_state.id,
                            error_code="PM_SEND_EXCEPTION",
                            error=str(send_exc),
                        )
                        # 审计记录失败
                        if audit_id and self.audit_store:
                            try:
                                self.audit_store.mark_published(
                                    audit_id, published=False,
                                    failure_reason=f"send_exception: {send_exc}",
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
                                    target={"kind": "private_message", "talker_id": str(talker_id)},
                                )
                            else:
                                self.audit_store.mark_published(
                                    audit_id, published=False, failure_reason="send_private_message failed",
                                )
                        except Exception:
                            pass

                    if success:
                        logger.info(f"已回复私信 from {talker_id}: {reply_text[:30]}...")
                        # PRD-V5 §6.3 / PM-501：标记已发布（终态）
                        self.pm_state_store.mark_published(pm_state.id)
                        # PRD-V5 §6.3 / PM-501：私信原文不进入 KnowledgeBaseMemory
                        # （仅在 pm_state_store 中保留 generation_text 用于审计/重试）
                        if self.safety_checker is not None:
                            self.safety_checker.record_content(reply_text, account_id=self.account_id)
                        # 标记已读
                        await self.bili.ack_session(talker_id, my_uid)
                    else:
                        # PRD-V5 §6.3 / PM-501：发布失败 → retry_wait（独立退避）
                        # 平台明确返回失败（非本地异常），按 retry_wait 处理
                        logger.warning(f"私信发送失败 to {talker_id}")
                        self.pm_state_store.mark_retry_wait(
                            pm_state.id,
                            error_code="PM_PUBLISH_FAILED",
                            error="send_private_message returned False",
                        )

                except Exception as e:
                    logger.warning(f"处理私信会话异常: {e}")

        except Exception as e:
            logger.error(f"私信检查异常: {e}", exc_info=True)

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
                            logger.warning(
                                f"重试私信文本 hash 不匹配，转 deferred: "
                                f"msg_id={pm_state.platform_message_id}"
                            )
                            self.pm_state_store.mark_deferred(
                                pm_state.id, reason="hash_mismatch",
                                error_code="RETRY_HASH_MISMATCH",
                            )
                            continue

                    # 推进：retry_wait → publish_pending
                    self.pm_state_store.update_status(pm_state.id, "publish_pending")

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

                    try:
                        success = await self.bili.send_private_message(
                            receiver_id=talker_id, msg=reply_text,
                        )
                    except Exception as send_exc:
                        # 平台结果不确定 → result_unknown（不自动重发）
                        logger.error(
                            f"重试私信发送抛异常: {send_exc}", exc_info=True,
                        )
                        self.pm_state_store.mark_result_unknown(
                            pm_state.id,
                            error_code="PM_RETRY_SEND_EXCEPTION",
                            error=str(send_exc),
                        )
                        continue

                    if success:
                        self.pm_state_store.mark_published(pm_state.id)
                        logger.info(
                            f"重试私信发送成功: msg_id={pm_state.platform_message_id}"
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
                        logger.warning(
                            f"重试私信发送失败: msg_id={pm_state.platform_message_id}"
                        )
                except Exception as e:
                    logger.error(
                        f"重试私信 {pm_state.platform_message_id} 失败: {e}"
                    )
        except Exception as e:
            logger.error(f"处理重试私信失败: {e}")

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
                if time_str == current_time and current_time not in self._proactive_triggered:
                    # PRD-V5 §7：原子 claim（事务性条件更新，避免重复触发）
                    task_id = self._claim_task_for_slot("proactive_video", time_str)
                    if task_id:
                        # PRD V3 §4.1/§4.2：用 _spawn_memory_task 避免阻塞主循环 + 异常回调
                        self._spawn_memory_task(
                            self._do_proactive_video(task_id=task_id),
                            tag="_do_proactive_video",
                        )
                        self._proactive_triggered.add(current_time)
                        self._save_schedule_state()
                    else:
                        # claim 失败（已被处理或不存在）→ 仅记录内存标记避免反复尝试
                        self._proactive_triggered.add(current_time)
                        self._save_schedule_state()
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
        if task_id and not self.task_store.start(task_id):
            logger.warning(f"TaskRun {task_id} start 失败（可能已被处理）")
            return

        # PRD V4 COM-001：features 在方法内独立读取（与 _check_proactive_tasks 解耦）
        features = self.config_loader.get_raw_config().get("features", {})

        try:
            # 1. 获取热门视频
            data = await self.bili.get_hot_videos()
            if not data or not data.get("data"):
                if task_id:
                    self._fail_task(task_id, "NO_HOT_VIDEOS", "获取热门视频失败", retryable=False)
                return
            videos = data["data"].get("list", [])
            if not videos:
                if task_id:
                    self._fail_task(task_id, "NO_HOT_VIDEOS", "热门视频列表为空", retryable=False)
                return

            # PRD 4.11：过滤已看过的视频，避免重复观看
            watched_bvids: set = set()
            if self.ds:
                try:
                    watch_log = self.ds.load_json("watch_log.json", [])
                    watched_bvids = {item.get("bvid") for item in watch_log if item.get("bvid")}
                except Exception:
                    pass
            available_videos = [v for v in videos if v.get("bvid") not in watched_bvids]
            if not available_videos:
                logger.info("所有热门视频均已看过，跳过主动看视频")
                if task_id:
                    self._fail_task(task_id, "ALL_WATCHED", "所有热门视频均已看过", retryable=False)
                return

            # 随机选一个视频
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

            # 获取标签
            tags_list = await self.bili.get_video_tags(bvid) or []
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
                    if cid and bvid:
                        import os as _os
                        video_temp_dir = _os.path.join(self._get_data_dir(), "video_temp")
                        save_path = _os.path.join(video_temp_dir, f"{bvid}")
                        video_file = await self.bili.download_video(bvid, cid, save_path, quality=32)
                        if video_file and _os.path.exists(video_file):
                            logger.info(f"视频已下载，开始视听分析: {video_file}")
                            # PRD 3.9：用 try/finally 确保视频文件被清理
                            try:
                                vu_result = await self.video_understanding.understand(video_file)
                                ctx.audiovisual = vu_result
                                av_log = ctx.audiovisual_log
                                if av_log:
                                    logger.info(f"视频理解完成，行为日志 {len(av_log)} 字")
                                else:
                                    logger.warning("视频理解未生成行为日志")
                                    deg = vu_result.get("degradation_reason", "") if isinstance(vu_result, dict) else ""
                                    if deg:
                                        ctx.degradation_reasons.append(f"audiovisual_degraded: {deg}")
                            finally:
                                # 无论成功或异常都清理下载的视频文件
                                try:
                                    _os.remove(video_file)
                                except Exception:
                                    pass
                        else:
                            logger.warning(f"视频下载失败: {bvid}")
                            ctx.degradation_reasons.append("audiovisual_failed: 视频下载失败")
                except Exception as e:
                    logger.warning(f"视频理解失败（降级为元数据评价）: {e}")
                    ctx.degradation_reasons.append(f"audiovisual_failed: {e}")

            # VID-502：统一截断并标记来源；metadata/hot_comments 已由 evaluate_video 单独渲染，
            # 此处只传搜索参考 + 视听分析 + 降级说明，避免重复段落
            video_content = ctx.to_prompt_sections(
                include_metadata=False, include_hot_comments=False,
            )

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

            # 执行点赞
            like_result = decisions.get("like", {})
            if like_result.get("planned"):
                try:
                    ok = await self.bili.like_video(oid)
                    await self.interaction_policy.record_result_async(
                        "like", bvid, str(oid), "success" if ok else "failed",
                        api_code=getattr(self.bili, "last_api_code", None),
                        failure_reason="" if ok else "bili_api_false",
                    )
                    if ok:
                        logger.info("视频点赞成功")
                    else:
                        self._check_bili_risk_control("proactive_like")
                except Exception as e:
                    logger.debug(f"点赞失败: {e}")
                    await self.interaction_policy.record_result_async(
                        "like", bvid, str(oid), "failed", failure_reason=str(e)
                    )
            else:
                logger.debug(f"点赞未执行: {like_result.get('reason')}")

            # 执行投币
            coin_result = decisions.get("coin", {})
            if coin_result.get("planned"):
                try:
                    ok = await self.bili.coin_video(oid, num=1)
                    await self.interaction_policy.record_result_async(
                        "coin", bvid, str(oid), "success" if ok else "failed",
                        api_code=getattr(self.bili, "last_api_code", None),
                        failure_reason="" if ok else "bili_api_false",
                    )
                    if ok:
                        logger.info("视频投币成功")
                    else:
                        self._check_bili_risk_control("proactive_coin")
                except Exception as e:
                    logger.debug(f"投币失败: {e}")
                    await self.interaction_policy.record_result_async(
                        "coin", bvid, str(oid), "failed", failure_reason=str(e)
                    )
            else:
                logger.debug(f"投币未执行: {coin_result.get('reason')}")

            # 执行收藏
            fav_result = decisions.get("favorite", {})
            if fav_result.get("planned"):
                try:
                    ok = await self.bili.fav_video(oid)
                    await self.interaction_policy.record_result_async(
                        "favorite", bvid, str(oid), "success" if ok else "failed",
                        api_code=getattr(self.bili, "last_api_code", None),
                        failure_reason="" if ok else "bili_api_false",
                    )
                    if ok:
                        logger.info("视频收藏成功")
                    else:
                        self._check_bili_risk_control("proactive_fav")
                except Exception as e:
                    logger.debug(f"收藏失败: {e}")
                    await self.interaction_policy.record_result_async(
                        "favorite", bvid, str(oid), "failed", failure_reason=str(e)
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

            # 6. 保存记忆
            data_dir = self._get_data_dir()
            persona_id = self._get_current_persona_id()

            # PRD V4 VID-006：记录每个动作的真实结果（只有 API 成功才记 true）
            interaction_summary = self.interaction_policy.get_today_summary()

            # MEM-501：通过 MemoryWriteQueue 写入（不再直接 import memory_writer）
            try:
                if self.memory_write_queue and self.knowledge_memory:
                    # PRD V4 VID-003：watch_state 区分 inspected/watched，记忆不得把 inspected 写成"完整看过"
                    content_parts = [f"浏览了视频《{title}》，UP主 {owner}"]
                    if review:
                        content_parts.append(f"评价: {review}")
                    if mood:
                        content_parts.append(f"心情: {mood}")
                    if comment_text:
                        content_parts.append(f"评论: {comment_text}")
                    if video_content:
                        # VID-502：记忆摘要取视听行为日志（最相关），无则取 prompt 段落前 500 字
                        av_log = ctx.audiovisual_log
                        mem_summary = av_log if av_log else video_content
                        content_parts.append(f"视频内容: {mem_summary[:500]}")
                    content_text = "，".join(content_parts)

                    _km = self.knowledge_memory
                    _meta = {
                        "kind": "video_memory",
                        "bvid": bvid,
                        "oid": str(oid),
                        "title": title,
                        "owner_name": owner,
                        "owner_mid": owner_mid,
                        "score": score,
                        "mood": mood,
                        "review": review,
                        "comment": comment_text,
                        "tags": tags_list,
                        "video_content": video_content[:2000] if video_content else "",
                        # VID-502：结构化上下文快照（各来源独立保留，便于审计）
                        "video_context": ctx.to_dict(),
                        "degradation_reasons": list(ctx.degradation_reasons),
                        # PRD V4 VID-003：真实观看语义
                        "watch_state": watch_state,
                        "watched": watched_flag,
                        # PRD V4 VID-006：互动决策审计
                        "interactions": {
                            "like": {"planned": like_result.get("planned", False),
                                     "reason": like_result.get("reason", "")},
                            "coin": {"planned": coin_result.get("planned", False),
                                     "reason": coin_result.get("reason", "")},
                            "favorite": {"planned": fav_result.get("planned", False),
                                         "reason": fav_result.get("reason", "")},
                            "comment": {"planned": comment_decision.get("planned", False),
                                        "reason": comment_decision.get("reason", "")},
                        },
                        "interaction_budget": interaction_summary,
                    }
                    _imp = "medium" if score >= 7 else "low"
                    await self.memory_write_queue.enqueue(
                        idempotency_key=f"video:{self.account_id}:{bvid}:{oid}",
                        write_callable=lambda: _km.write_atom(
                            content=content_text,
                            category="content_video",
                            metadata=_meta,
                            persona_id=persona_id,
                            importance=_imp,
                            importance_score=score / 10.0,
                        ),
                    )
            except Exception as e:
                logger.debug(f"记忆入队失败: {e}")

            # PRD V3 §2.2.3：旧 MemorySystem.save_self_memory 已删除，记忆只写 KnowledgeBaseMemory

            # JSON 备份
            if self.ds:
                try:
                    log = self.ds.load_json("watch_log.json", [])
                    log.append({
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "title": title,
                        "bvid": bvid,
                        "up_name": owner,
                        "score": score,
                        "mood": mood,
                        "comment": comment_text,
                        # PRD V4 VID-003：记录真实观看状态
                        "watch_state": watch_state,
                        "watched": watched_flag,
                        # PRD V4 VID-006：记录决策结果而非 LLM 建议
                        "liked_planned": like_result.get("planned", False),
                        "coined_planned": coin_result.get("planned", False),
                        "faved_planned": fav_result.get("planned", False),
                        "comment_planned": comment_decision.get("planned", False),
                    })
                    self.ds.save_json("watch_log.json", log[-100:])
                except Exception:
                    pass

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
        """让 LLM 根据动态内容生成英文图片描述 prompt"""
        if not self.llm:
            return None
        try:
            prompt = (
                "根据以下动态内容，生成一个适合配图的英文图片描述 prompt（1-2 句话）。\n"
                "要求：\n"
                "- 只输出 prompt 本身，不要任何解释或前缀\n"
                "- 风格关键词：cinematic, high quality, detailed, no text, no watermark\n"
                "- 画面应与动态内容相关但不重复文字\n\n"
                f"动态内容: {content}\n\nImage prompt:"
            )
            result = await self.llm.generate(prompt, max_tokens=100, temperature=0.7)
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
        if task_id and not self.task_store.start(task_id):
            logger.warning(f"TaskRun {task_id} start 失败（可能已被处理）")
            return

        try:
            # 1. 收集主题池（最近看过的视频标题作为参考）
            related_videos: List[str] = []
            if self.ds:
                try:
                    watch_log = self.ds.load_json("watch_log.json", [])
                    related_videos = [
                        f"《{v.get('title', '')}》by {v.get('up_name', '')}"
                        for v in watch_log[-3:]
                        if v.get("title")
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

            # 4. 写入 audit（含 persona_id，PRD V3 §8.6）
            audit_id = None
            if self.audit_store:
                try:
                    audit_id = self.audit_store.record(
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
                return
            try:
                passed, reason = await self.safety_checker.check_content(
                    content, scene="dynamic_post",
                    persona_id=persona_id or "unknown",
                    account_id=self.account_id,
                )
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
                    return
                if not self.safety_checker.check_rate_limit(scene="dynamic_post", account_id=self.account_id):
                    logger.warning("动态发布频率限制触发，跳过本次")
                    # PRD 4.18：审计记录失败原因
                    if audit_id and self.audit_store:
                        try:
                            self.audit_store.mark_published(
                                audit_id, published=False, failure_reason="rate_limited",
                            )
                        except Exception:
                            pass
                    return
                # PRD 4.4：预占频率配额
                self.safety_checker.record_publish(scene="dynamic_post", account_id=self.account_id)
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
                return

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
            success = await self.bili.post_dynamic_text(
                content, images=image_list if image_list else None
            )
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
                data_dir = self._get_data_dir()

                # MEM-501：通过 MemoryWriteQueue 写入（不再直接 import memory_writer）
                try:
                    if self.memory_write_queue and self.knowledge_memory:
                        _km = self.knowledge_memory
                        _dyn_content = f"Bot 发了一条动态：{content}"
                        _dyn_meta = {
                            "kind": "dynamic_post",
                            "content": content,
                            "audit_id": audit_id,
                        }
                        await self.memory_write_queue.enqueue(
                            idempotency_key=f"dynamic:{audit_id}",
                            write_callable=lambda: _km.write_atom(
                                content=_dyn_content,
                                category="bot_action",
                                metadata=_dyn_meta,
                                persona_id=persona_id,
                                importance="medium",
                                importance_score=0.6,
                            ),
                        )
                except Exception as e:
                    logger.debug(f"记忆入队失败: {e}")

                # JSON 备份（保留）
                if self.ds:
                    try:
                        log = self.ds.load_json("dynamic_log.json", [])
                        log.append({
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "text": content,
                            "has_image": has_image,
                            "generated_by": "llm",
                        })
                        self.ds.save_json("dynamic_log.json", log[-100:])
                    except Exception:
                        pass

                # PRD V3 §2.2.3：旧 MemorySystem.save_self_memory 已删除，记忆只写 KnowledgeBaseMemory

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
            except Exception as e:
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

            # 原子 claim：approved/retry_wait → publishing
            if not store.mark_publishing(draft_id):
                self._fail_task(task_id, "DRAFT_NOT_PUBLISHABLE",
                                f"草稿状态 {draft.status} 不可发布", retryable=False)
                return

            content = draft.content
            image_refs = draft.image_refs

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
                    self._fail_task(task_id, "IMAGE_UPLOAD_FAILED",
                                    "草稿配图上传失败", retryable=True)
                    return

            # DYN-602：发布前频率限制检查
            if self.safety_checker is not None:
                if not self.safety_checker.check_rate_limit(
                    scene="dynamic_post", account_id=self.account_id
                ):
                    self._fail_task(task_id, "RATE_LIMITED",
                                    "dynamic_post rate limited", retryable=True)
                    return

            # 发布
            success = await self.bili.post_dynamic_text(
                content, images=image_list if image_list else None
            )
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
                # DYN-603：记忆写入（copy from _do_post_dynamic）
                try:
                    if self.memory_write_queue and self.knowledge_memory:
                        _km = self.knowledge_memory
                        _dyn_content = f"Bot 发了一条动态：{content}"
                        _dyn_meta = {
                            "kind": "dynamic_post",
                            "content": content,
                            "draft_id": draft_id,
                        }
                        await self.memory_write_queue.enqueue(
                            idempotency_key=f"dynamic_draft:{draft_id}",
                            write_callable=lambda: _km.write_atom(
                                content=_dyn_content,
                                category="bot_action",
                                metadata=_dyn_meta,
                                persona_id=getattr(draft, "persona_id", "") or "",
                                importance="medium",
                                importance_score=0.6,
                            ),
                        )
                except Exception as e:
                    logger.debug(f"记忆入队失败: {e}")
                # DYN-603：dynamic_log.json 备份（copy from _do_post_dynamic）
                if self.ds:
                    try:
                        log = self.ds.load_json("dynamic_log.json", [])
                        log.append({
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "text": content,
                            "has_image": bool(image_list),
                            "generated_by": "approved_draft",
                            "draft_id": draft_id,
                        })
                        self.ds.save_json("dynamic_log.json", log[-100:])
                    except Exception:
                        pass
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
            if self.ds:
                records = self.ds.load_json("weekly_summary.json", [])
            else:
                records = []

            this_week = datetime.now().strftime("%G-W%V")

            if any(r.get("week") == this_week for r in records):
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

            # 1. 从 SQLite 读取本周活动（fallback 到 JSON）
            data_dir = self._get_data_dir()
            week_summary_text = self._build_week_summary_from_sqlite(data_dir)

            if not week_summary_text.strip():
                # fallback：JSON
                videos = self.ds.load_json("watch_log.json", []) if self.ds else []
                dynamics = self.ds.load_json("dynamic_log.json", []) if self.ds else []
                lines = []
                if videos:
                    lines.append(f"【看过的视频】共 {len(videos)} 个：")
                    for v in videos[-10:]:
                        lines.append(f"- 《{v.get('title', '')[:30]}》by {v.get('up_name', '')}")
                if dynamics:
                    lines.append(f"【发过的动态】共 {len(dynamics)} 条：")
                    for d in dynamics[-5:]:
                        lines.append(f"- {d.get('text', '')[:40]}")
                week_summary_text = "\n".join(lines) if lines else "无活动记录"

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
                    audit_id = self.audit_store.record(
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

            # 5. MEM-501：通过 MemoryWriteQueue 写入（不再直接 import memory_writer）
            try:
                if self.memory_write_queue and self.knowledge_memory:
                    _km = self.knowledge_memory
                    _week_content = f"周总结（{this_week}）：{summary}"
                    _week_meta = {
                        "kind": "weekly_summary",
                        "week": this_week,
                        "audit_id": audit_id,
                    }
                    await self.memory_write_queue.enqueue(
                        idempotency_key=f"weekly:{this_week}",
                        write_callable=lambda: _km.write_atom(
                            content=_week_content,
                            category="summary",
                            metadata=_week_meta,
                            persona_id=persona_id,
                            importance="high",
                            importance_score=0.8,
                        ),
                    )
            except Exception as e:
                logger.debug(f"记忆入队失败: {e}")

            # JSON 备份（保留）
            records.append({
                "week": this_week,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "summary": summary,
            })
            if self.ds:
                self.ds.save_json("weekly_summary.json", records[-20:])
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
        """从 SQLite memory_atoms 读取本周活动"""
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path(data_dir) / "knowledge_base.db"
            if not db_path.exists():
                return ""
            # MISC-604：使用 try/finally 确保连接关闭（避免异常时泄漏）
            conn = sqlite3.connect(str(db_path))
            try:
                conn.row_factory = sqlite3.Row
                # 取近 7 天的活动
                cutoff = time.time() - 7 * 86400
                rows = conn.execute(
                    "SELECT category, content, created_at FROM memory_atoms "
                    "WHERE is_active = 1 AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 50",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()

            if not rows:
                return ""

            videos = []
            dynamics = []
            others = []
            for r in rows:
                cat = r["category"]
                text = r["content"] or ""
                if cat == "content_video":
                    videos.append(text)
                elif cat == "bot_action":
                    dynamics.append(text)
                else:
                    others.append(text)

            lines = []
            if videos:
                lines.append(f"【看过的视频】共 {len(videos)} 条记忆：")
                for v in videos[:10]:
                    lines.append(f"- {v[:80]}")
            if dynamics:
                lines.append(f"【发过的动态】共 {len(dynamics)} 条记忆：")
                for d in dynamics[:5]:
                    lines.append(f"- {d[:80]}")
            if others:
                lines.append(f"【其他活动】共 {len(others)} 条：")
                for o in others[:5]:
                    lines.append(f"- {o[:80]}")
            return "\n".join(lines)
        except Exception:
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
            hours = sorted(random.sample(range(10, 23), min(n_videos, 13)))
            self._proactive_times = [(h, random.randint(0, 59)) for h in hours]
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
            f"{h}:{m:02d}" for h, m in self._proactive_times
            if now.hour > h or (now.hour == h and now.minute > m)
        }

        self._dynamic_triggered = {
            f"{h}:{m:02d}" for h, m in self._dynamic_times
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
