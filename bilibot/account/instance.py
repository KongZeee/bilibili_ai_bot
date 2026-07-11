"""
账号实例 — 封装单个 B站账号的完整运行时

每个 AccountInstance 拥有独立的：
- BilibiliAPI（账号凭据）
- DataStore（data/accounts/{account_id}/）
- UserStateSystem（画像/好感度/心情，纯 JSON）
- KnowledgeBaseMemory（SQLite 记忆存储）
- PersonalitySystem
- Scheduler
- ContextBuilder（PRD-V5 §5.1 ACC-502：账号级，注入本账号 DataStore/UserState/Bili/KnowledgeMemory）

共享应用级单例：
- PersonaStore（人格库，账号通过绑定选择人格）
- LLMManager（多 LLM 提供商，账号通过 llm_id 选择）
- AuditStore / PromptOrchestrator

参考 AstrBot PlatformManager 的 PlatformInstance 设计。
"""
import asyncio
import logging
import os
from typing import Optional, Dict, Any, TYPE_CHECKING

logger = logging.getLogger("bilibot.account")

if TYPE_CHECKING:
    from bilibot.app.config_loader import ConfigLoader
    from bilibot.services.persona_store import PersonaStore
    from bilibot.llm.manager import LLMManager
    from bilibot.services.audit_store import AuditStore
    from bilibot.prompts.orchestrator import PromptOrchestrator
    from bilibot.context_builder import ContextBuilder


class AccountInstance:
    """单个 B站账号实例"""

    def __init__(
        self,
        account_id: str,
        account_config: Dict[str, Any],
        *,
        persona_store: "PersonaStore",
        llm_manager: "LLMManager",
        audit_store: "AuditStore",
        orchestrator: "PromptOrchestrator",
        context_builder: "ContextBuilder",
        app_config_loader: "ConfigLoader",
        data_root: str = "./data",
        safety_checker=None,
    ):
        """
        Args:
            account_id: 账号唯一 ID
            account_config: 账号配置（含 sessdata/bili_jct/dede_user_id/buvid3/refresh_token/persona_id/llm_id/enabled/name）
            persona_store: 应用级人格库单例
            llm_manager: 应用级 LLM 管理器单例
            audit_store: 应用级审计存储单例
            orchestrator: 应用级 Prompt 编排器单例
            context_builder: ACC-502 保留参数（向后兼容），initialize() 会用本账号依赖重建实例
            app_config_loader: 应用级配置加载器（共享 proactive/reply/features 等配置）
            data_root: 数据根目录（账号目录将创建在 {data_root}/accounts/{account_id}/）
        """
        self.account_id = account_id
        self.account_config = account_config
        self.persona_store = persona_store
        self.llm_manager = llm_manager
        self.audit_store = audit_store
        self.orchestrator = orchestrator
        self.context_builder = context_builder
        self.app_config_loader = app_config_loader
        self.data_root = data_root
        # PRD V4 BOOT-003：应用级 SafetyChecker，传入 Scheduler
        self.safety_checker = safety_checker

        # 账号配置字段
        self.name: str = account_config.get("name", account_id)
        self.enabled: bool = account_config.get("enabled", True)
        # PRD V3 §7：profile_id 优先于 persona_id（多人格组）
        self.profile_id: str = account_config.get("profile_id", "")
        self.persona_id: str = account_config.get("persona_id", "")
        self.llm_id: str = account_config.get("llm_id", "")

        # 账号数据目录
        self.account_data_dir = os.path.join(data_root, "accounts", account_id)

        # 运行时组件（initialize 后填充）
        self.data_store = None
        self.bili = None
        self.llm = None
        self.user_state = None
        self.knowledge_memory = None
        # MEM-501：每账号记忆写入队列（与 knowledge_memory 同生命周期）
        self.memory_write_queue = None
        self.personality = None
        self.comment_context_service = None
        self.scheduler = None
        # 账号级配置加载器（共享配置 + 账号 bilibili 凭据 + 账号 data_dir）
        self.account_config_loader = None
        self._started = False
        # PRD V4 BOOT-002：后台调度任务引用 + 最近错误
        self._scheduler_task: Optional["asyncio.Task"] = None
        self._last_error: str = ""
        # PRD-V5 §5.3 LLM-501：LLM 解析元数据（configured / effective / fallback_reason）
        self._configured_llm_id: str = self.llm_id
        self._effective_llm_id: str = ""
        self._fallback_reason: str = ""
        self._llm_config_error: bool = False

    # ══════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════

    def _build_account_config_loader(self):
        """构建账号级 ConfigLoader（合并共享配置 + 账号凭据）"""
        from bilibot.app.config_loader import ConfigLoader

        # 取应用级原始配置
        app_raw = self.app_config_loader.get_raw_config()
        # 账号凭据覆盖 bilibili 段
        account_bili = {
            "sessdata": self.account_config.get("sessdata", ""),
            "bili_jct": self.account_config.get("bili_jct", ""),
            "dede_user_id": self.account_config.get("dede_user_id", ""),
            "buvid3": self.account_config.get("buvid3", ""),
            "refresh_token": self.account_config.get("refresh_token", ""),
        }
        # 合并：共享配置 + 账号 bilibili + 账号 data_dir
        import copy
        merged = copy.deepcopy(app_raw)
        merged["bilibili"] = account_bili
        merged["data_dir"] = self.account_data_dir
        # 移除 V2 多账号字段避免递归
        merged.pop("accounts", None)
        return ConfigLoader(merged)

    async def initialize(self):
        """初始化账号所有子系统"""
        if not self.enabled:
            logger.info(f"[{self.account_id}] 账号已禁用，跳过初始化")
            return

        # 1. 账号数据目录
        os.makedirs(self.account_data_dir, exist_ok=True)

        # 2. 账号级配置加载器
        self.account_config_loader = self._build_account_config_loader()

        # 3. 数据存储（账号隔离）
        from bilibot.data_store import DataStore
        self.data_store = DataStore(self.account_data_dir)

        # 4. LLM（PRD-V5 §5.3 LLM-501：按 llm_id 解析 provider，含回退控制）
        self._configured_llm_id = self.llm_id
        self.llm, self._effective_llm_id, self._fallback_reason = (
            self.llm_manager.resolve_provider(self.llm_id)
        )
        if self.llm is None:
            self._llm_config_error = True
            logger.warning(
                f"[{self.account_id}] LLM 不可用: {self._fallback_reason}，"
                f"账号功能将受限"
            )
        else:
            self._llm_config_error = False
            if self._fallback_reason:
                logger.warning(
                    f"[{self.account_id}] LLM 回退: {self._fallback_reason}，"
                    f"使用 '{self._effective_llm_id}'"
                )

        # 5. 人格系统（PRD V3 §7：profile_id 优先于 persona_id）
        if self.profile_id:
            # 绑定到 profile（支持运行时切换 profile.personas 中的任意一个）
            if not self.persona_store.set_account_profile(self.account_id, self.profile_id):
                logger.warning(f"[{self.account_id}] 绑定 profile={self.profile_id} 失败，回退到默认人格")
        elif self.persona_id:
            # 向后兼容：单人格绑定
            self.persona_store.set_account_persona(self.account_id, self.persona_id)

        # 6. B站 API
        from bilibot.bilibili_api import BilibiliAPI
        if self.account_config_loader.bilibili.is_authenticated:
            self.bili = BilibiliAPI(self.account_config_loader)
            logger.info(f"[{self.account_id}] B站 API 已初始化 (uid={self.account_config_loader.bilibili.dede_user_id})")
        else:
            logger.warning(f"[{self.account_id}] B站凭据未配置，跳过 API 初始化")

        # 7. 用户状态系统（画像/好感度/心情，纯 JSON，不依赖 LLM）
        from bilibot.user_state import UserStateSystem
        self.user_state = UserStateSystem(self.data_store, self.account_config_loader)

        # 8. 人格系统（PersonalitySystem）
        from bilibot.personality import PersonalitySystem
        self.personality = PersonalitySystem(self.account_config_loader)

        # 9. 知识库记忆
        if self.llm and self.data_store:
            try:
                from bilibot.knowledge_memory import KnowledgeBaseMemory
                # PRD V5 Task 16：注入记忆容量与遗忘配置（ConfigLoader.memory）
                self.knowledge_memory = KnowledgeBaseMemory(
                    self.account_data_dir, self.llm, self.personality,
                    memory_config=self.account_config_loader.memory,
                )
            except Exception as e:
                logger.warning(f"[{self.account_id}] 知识库初始化失败: {e}")

        # 9.1 MEM-501：每账号记忆写入队列（与 knowledge_memory 绑定）
        if self.knowledge_memory is not None:
            from bilibot.services.memory_write_queue import MemoryWriteQueue
            self.memory_write_queue = MemoryWriteQueue(
                max_length=1000, max_retries=3
            )
            await self.memory_write_queue.start()
            logger.info(f"[{self.account_id}] 记忆写入队列已启动")

        # 9.5 PRD-V5 §5.1 ACC-502：账号级 ContextBuilder
        # 注入本账号的 DataStore / UserState / BiliClient / KnowledgeMemory / persona_store，
        # 替换构造期传入的（可能是共享或 None）实例，确保 recent behavior 只读本账号数据。
        from bilibot.context_builder import ContextBuilder
        self.context_builder = ContextBuilder(
            data_store=self.data_store,
            user_state=self.user_state,
            persona_store=self.persona_store,
            bili=self.bili,
            config=self.account_config_loader.get_raw_config(),
            knowledge_memory=self.knowledge_memory,
            account_id=self.account_id,
        )

        # 10. 评论上下文服务（账号级）
        from bilibot.services.comment_context import CommentContextService
        self.comment_context_service = CommentContextService(
            bili=self.bili,
            user_state=self.user_state,
            data_store=self.data_store,
            persona_store=self.persona_store,
            config_loader=self.account_config_loader,
            knowledge_memory=self.knowledge_memory,
        )

        # 10.5 视频理解服务（视听双轨分析，可选）
        self.video_understanding = None
        if self.llm:
            try:
                from bilibot.video_understanding import VideoUnderstandingService
                self.video_understanding = VideoUnderstandingService(self.llm, self.account_config_loader)
                if self.video_understanding.is_available():
                    logger.info(f"[{self.account_id}] 视频理解服务已启用")
            except Exception as e:
                logger.warning(f"[{self.account_id}] 视频理解服务初始化失败: {e}")

        # 10.6 文生图 Provider（动态配图，可选）
        self.image_provider = None
        try:
            from bilibot.image import ImageProvider
            raw_cfg = self.account_config_loader.get_raw_config()
            ig_config = raw_cfg.get("image_generation", {})
            if ig_config.get("enabled") and ig_config.get("api_key"):
                self.image_provider = ImageProvider(ig_config)
                logger.info(f"[{self.account_id}] 文生图 Provider 已启用: {ig_config.get('model', 'agnes-image-2.1-flash')}")
        except Exception as e:
            logger.warning(f"[{self.account_id}] 文生图 Provider 初始化失败: {e}")

        # 11. 调度器
        from bilibot.scheduler import Scheduler
        # PRD-V5 §10.2 COM-501：每账号独立的主动评论原子幂等存储
        from bilibot.services.proactive_comment_store import ProactiveCommentStore
        _pc_db = os.path.join(self.account_data_dir, "proactive_comment_actions.db")
        self.proactive_comment_store = ProactiveCommentStore(
            _pc_db, account_id=self.account_id,
        )
        self.scheduler = Scheduler(
            config_loader=self.account_config_loader,
            user_state=self.user_state,
            llm=self.llm,
            bili=self.bili,
            data_store=self.data_store,
            persona_store=self.persona_store,
            orchestrator=self.orchestrator,
            audit_store=self.audit_store,
            context_builder=self.context_builder,
            comment_context_service=self.comment_context_service,
            safety_checker=self.safety_checker,
            account_id=self.account_id,
            video_understanding_service=self.video_understanding,
            image_provider=self.image_provider,
            knowledge_memory=self.knowledge_memory,
            memory_write_queue=self.memory_write_queue,
            proactive_comment_store=self.proactive_comment_store,
        )

        logger.info(f"[{self.account_id}] 账号实例初始化完成")

    async def start(self):
        """启动账号调度器（非阻塞）

        PRD V4 BOOT-002：只创建受管理的后台任务，不直接等待永久调度循环。
        重复启动返回幂等成功，不创建第二个 Scheduler。
        调度任务异常退出后，账号状态自动变为 failed。
        """
        if not self.enabled:
            logger.info(f"[{self.account_id}] 账号已禁用，不启动")
            return
        if self.scheduler is None:
            logger.warning(f"[{self.account_id}] 调度器未初始化，跳过启动")
            return
        # 幂等：已运行则不重复创建
        if self._scheduler_task is not None and not self._scheduler_task.done():
            logger.info(f"[{self.account_id}] 调度器已在运行中，跳过重复启动")
            return
        self._started = True
        self._last_error = ""
        self._scheduler_task = asyncio.create_task(self._run_scheduler())

    async def _run_scheduler(self):
        """后台运行调度器，捕获异常避免静默退出"""
        try:
            await self.scheduler.start()
        except asyncio.CancelledError:
            logger.info(f"[{self.account_id}] 调度器被取消")
            raise
        except Exception as e:
            logger.error(f"[{self.account_id}] 调度器异常退出: {e}", exc_info=True)
            self._last_error = str(e)
        finally:
            self._started = False
            logger.info(f"[{self.account_id}] 调度器已停止")

    def stop(self):
        """停止账号调度器"""
        if self.scheduler:
            try:
                self.scheduler.stop()
            except Exception:
                pass
        # PRD V4 BOOT-002：取消后台任务
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
        self._started = False
        logger.info(f"[{self.account_id}] 账号已停止")

    async def close(self):
        """关闭账号

        PRD V4 BOOT-004 / MEM-501 优雅关闭顺序：
        1. 停止调度器（不再接受新任务）
        2. 等待后台调度任务结束
        3. drain 记忆写入队列（等待 pending 写入完成）
        4. flush 记忆向量索引
        5. close 记忆系统
        6. 关闭 B站 session
        """
        self.stop()
        # 等待后台任务结束
        if self._scheduler_task is not None:
            try:
                await asyncio.wait_for(self._scheduler_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            self._scheduler_task = None
        # MEM-501：drain 记忆写入队列 → flush 向量索引 → close 记忆系统
        if self.memory_write_queue is not None:
            try:
                await self.memory_write_queue.drain(timeout=10.0)
            except Exception as e:
                logger.warning(f"[{self.account_id}] 记忆队列 drain 失败: {e}")
        if self.knowledge_memory is not None:
            try:
                self.knowledge_memory.flush()
            except Exception as e:
                logger.warning(f"[{self.account_id}] flush 记忆系统失败: {e}")
            try:
                self.knowledge_memory.close()
            except Exception as e:
                logger.warning(f"[{self.account_id}] 关闭记忆系统失败: {e}")
        # 关闭 B站 session
        if self.bili is not None:
            try:
                await self.bili.close()
            except Exception:
                pass
        logger.info(f"[{self.account_id}] 账号已关闭")

    # ══════════════════════════════════════
    #  状态
    # ══════════════════════════════════════

    def is_running(self) -> bool:
        """PRD V4 BOOT-002：running 不得仅依靠一个布尔值判断，
        必须确认调度任务仍存活。"""
        if not self._started or self.scheduler is None:
            return False
        if self._scheduler_task is None or self._scheduler_task.done():
            return False
        return True

    def get_status(self) -> dict:
        """获取账号运行状态"""
        # PRD V3 §7：返回 profile_id 和当前激活人格（而非静态 persona_id）
        persona_status = {}
        if self.persona_store:
            persona_status = self.persona_store.get_account_persona_status(self.account_id)
        # PRD V4 BOOT-002：调度任务异常退出时返回 failed 状态
        if self._scheduler_task is not None and self._scheduler_task.done():
            exc = self._scheduler_task.exception() if not self._scheduler_task.cancelled() else None
            if exc is not None:
                state = "failed"
            elif self._last_error:
                state = "failed"
            else:
                state = "stopped"
        elif self.is_running():
            # PRD-V5 §5.3 LLM-501：LLM 配置错误时标记为 degraded
            state = "degraded" if self._llm_config_error else "running"
        else:
            state = "stopped"
        return {
            "account_id": self.account_id,
            "name": self.name,
            "enabled": self.enabled,
            "running": self.is_running(),
            "state": state,
            "last_error": self._last_error,
            "profile_id": self.profile_id or persona_status.get("profile_id"),
            "persona_id": persona_status.get("active_persona_id") or self.persona_id,
            "available_personas": persona_status.get("available_personas", []),
            "llm_id": self.llm_id,
            # PRD-V5 §5.3 LLM-501：LLM 绑定校验状态
            "configured_llm_id": self._configured_llm_id,
            "effective_llm_id": self._effective_llm_id,
            "fallback_reason": self._fallback_reason,
            "uid": self.account_config.get("dede_user_id", ""),
            "authenticated": bool(self.account_config.get("sessdata") and self.account_config.get("bili_jct")),
            "has_llm": self.llm is not None,
            "has_bili": self.bili is not None,
        }

    def update_config(self, account_config: Dict[str, Any]):
        """
        更新账号配置（需要重新 initialize 才生效）

        Args:
            account_config: 新的账号配置
        """
        self.account_config = account_config
        self.name = account_config.get("name", self.account_id)
        self.enabled = account_config.get("enabled", True)
        # PRD V3 §7：profile_id 优先于 persona_id
        self.profile_id = account_config.get("profile_id", "")
        self.persona_id = account_config.get("persona_id", "")
        self.llm_id = account_config.get("llm_id", "")
        # PRD-V5 §5.3 LLM-501：同步 configured_llm_id（effective 需 re-initialize 才更新）
        self._configured_llm_id = self.llm_id

    async def reload(self):
        """PRD V3 §3.3：热重载账号配置（凭据级别，不重启调度器）

        重新读取 config.yaml 中的账号凭据并更新 BilibiliAPI。
        注意：persona_id / llm_id 变更需要重新 initialize，本方法不处理。

        Returns:
            dict: {"reloaded": bool, "message": str}
        """
        try:
            # 重新构建账号级 ConfigLoader（从最新 config.yaml 读取）
            self.account_config_loader = self._build_account_config_loader()

            # 更新 BilibiliAPI 凭据（不重建 session）
            if self.bili is not None:
                self.bili.reload_credentials(self.account_config_loader)
                logger.info(f"[{self.account_id}] BilibiliAPI 凭据已热重载")
            elif self.account_config_loader.bilibili.is_authenticated:
                # 之前未初始化（凭据缺失），现在有了 → 创建 BilibiliAPI
                from bilibot.bilibili_api import BilibiliAPI
                self.bili = BilibiliAPI(self.account_config_loader)
                logger.info(f"[{self.account_id}] BilibiliAPI 已创建（凭据补齐）")

            return {"reloaded": True, "message": "凭据已热重载"}
        except Exception as e:
            logger.error(f"[{self.account_id}] 热重载失败: {e}", exc_info=True)
            return {"reloaded": False, "message": str(e)}

    def __repr__(self) -> str:
        return f"<AccountInstance id={self.account_id!r} name={self.name!r} running={self._started}>"
