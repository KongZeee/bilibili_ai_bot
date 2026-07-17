"""
账号实例 — 封装单个 B站账号的完整运行时

每个 AccountInstance 拥有独立的：
- BilibiliAPI（账号凭据）
- DataStore（data/accounts/{account_id}/）
- UserStateSystem（画像/好感度/心情，纯 JSON）
- MemoryBrainService（账号级 SQLite 统一记忆）
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
from collections.abc import Mapping
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
        # AccountManager 在 _create_instance 后注入，用于凭据回调同步 registry
        self.account_manager = None

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
        self.memory_brain = None
        self.personality = None
        self.comment_context_service = None
        self.companion = None
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
        if not isinstance(app_raw, Mapping):
            app_raw = {}
        # 账号凭据覆盖 bilibili 段
        account_bili = {
            "sessdata": self.account_config.get("sessdata", ""),
            "bili_jct": self.account_config.get("bili_jct", ""),
            "dede_user_id": self.account_config.get("dede_user_id", ""),
            "buvid3": self.account_config.get("buvid3", ""),
            "buvid4": self.account_config.get("buvid4", ""),
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
            self.bili.set_credential_update_callback(self._on_bili_credentials_updated)
            logger.info(f"[{self.account_id}] B站 API 已初始化 (uid={self.account_config_loader.bilibili.dede_user_id})")
            # 设备指纹：无 buvid3 时自动领取（失败不阻断）
            try:
                await self.bili.ensure_buvid()
            except Exception as e:
                logger.warning(f"[{self.account_id}] ensure_buvid 失败: {e}")
        else:
            logger.warning(f"[{self.account_id}] B站凭据未配置，跳过 API 初始化")

        # 7. 用户状态系统（画像/好感度/心情，纯 JSON，不依赖 LLM）
        from bilibot.user_state import UserStateSystem
        self.user_state = UserStateSystem(self.data_store, self.account_config_loader)

        # 8. 人格系统（PersonalitySystem）
        from bilibot.personality import PersonalitySystem
        self.personality = PersonalitySystem(self.account_config_loader)

        # 9. V6 account-scoped memory brain. Archival/FTS remain available even
        # when model providers are not configured; enrichment jobs become blocked.
        try:
            from bilibot.memory_brain import MemoryBrainService

            embedding_provider = (
                self.llm_manager.resolve_embedding() if self.llm_manager else None
            )
            self.memory_brain = MemoryBrainService(
                self.account_id,
                self.account_data_dir,
                chat_provider=self.llm,
                embedding_provider=embedding_provider,
                memory_config=self.account_config_loader.memory,
            )
            await self.memory_brain.start()
            # Narrow compatibility alias. It points to V6 and never opens legacy files.
            self.knowledge_memory = self.memory_brain
            logger.info(f"[{self.account_id}] V6 记忆大脑已启动")
        except Exception as e:
            logger.error(f"[{self.account_id}] V6 记忆大脑初始化失败: {e}", exc_info=True)
            raise

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
            companion=None,  # filled after CompanionLifeService init
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
            memory_brain=self.memory_brain,
        )

        # 10.5 视频理解服务（视听双轨分析，可选）— 通过 ModelRouter 解析 vision/asr
        self.video_understanding = None
        if self.llm_manager:
            try:
                from bilibot.video_understanding import VideoUnderstandingService
                self.video_understanding = VideoUnderstandingService(self.llm_manager, self.account_config_loader)
                if self.video_understanding.is_available():
                    logger.info(f"[{self.account_id}] 视频理解服务已启用")
            except Exception as e:
                logger.warning(f"[{self.account_id}] 视频理解服务初始化失败: {e}")

        # 10.6 文生图 Provider（动态配图，可选）— 通过 ModelRouter 解析 image provider
        self.image_provider = None
        try:
            from bilibot.image import ImageProvider
            img_p = self.llm_manager.resolve_image() if self.llm_manager else None
            if img_p and img_p.enabled and (img_p.api_key or getattr(img_p, "api_keys", None)):
                self.image_provider = ImageProvider({
                    "enabled": True,
                    "api_key": img_p.api_key,
                    "api_keys": list(getattr(img_p, "api_keys", []) or []),
                    "base_url": img_p.base_url,
                    "model": img_p.model,
                    "default_size": getattr(img_p, "default_size", "1024x768"),
                    "timeout": getattr(img_p, "timeout", 120),
                })
                logger.info(f"[{self.account_id}] 文生图 Provider 已启用: {img_p.model}")
        except Exception as e:
            logger.warning(f"[{self.account_id}] 文生图 Provider 初始化失败: {e}")

        # 11. 陪伴生活层（账号级；默认 companion.enabled=false）
        self.companion = None
        try:
            from bilibot.companion import CompanionLifeService

            self.companion = CompanionLifeService(
                self.account_id,
                self.account_data_dir,
                config_loader=self.account_config_loader,
                llm=self.llm,
                persona_store=self.persona_store,
                memory_brain=self.memory_brain,
                safety_checker=self.safety_checker,
                web_search=None,  # filled after Scheduler creates WebSearchService
                draft_store=None,  # filled after Scheduler draft store is available
            )
            # ContextBuilder 注入 companion 供回复上下文使用
            if self.context_builder is not None:
                self.context_builder.companion = self.companion
            if self.companion.enabled:
                logger.info(f"[{self.account_id}] 陪伴生活层已启用")
            else:
                logger.debug(f"[{self.account_id}] 陪伴生活层已加载（未启用）")
        except Exception as e:
            logger.warning(f"[{self.account_id}] 陪伴生活层初始化失败: {e}")
            self.companion = None

        # 12. 调度器
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
            memory_brain=self.memory_brain,
            proactive_comment_store=self.proactive_comment_store,
            companion=self.companion,
        )
        # 回填 web_search / draft_store 给 companion
        if self.companion is not None:
            try:
                self.companion.web_search = getattr(self.scheduler, "web_search", None)
                self.companion.draft_store = self.scheduler.get_draft_store()
            except Exception as e:
                logger.debug(f"[{self.account_id}] companion 回填 web_search/draft 失败: {e}")

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

    def _on_bili_credentials_updated(self, updates: dict) -> None:
        """BilibiliAPI 刷新/领取凭据后的持久化回调（同步，由 API 层调用）

        - 更新内存 account_config + 账号级 BiliConfig dataclass
        - 原子写盘（ConfigLoader.patch_account_credentials，多账号锁）
        - 同步 AccountConfigRegistry（面板/列表一致）
        """
        if not isinstance(updates, dict) or not updates:
            return
        allowed = (
            "sessdata", "bili_jct", "dede_user_id",
            "buvid3", "buvid4", "refresh_token",
        )
        patch = {k: str(v) for k, v in updates.items() if k in allowed and v}
        if not patch:
            return
        # 1) 更新内存账号配置（运行时权威）
        self.account_config.update(patch)
        # 2) 只更新账号级 ConfigLoader 的 dataclass（请求头读这里）；
        #    勿 mutate get_raw_config() 的深拷贝（无效）
        if self.account_config_loader is not None:
            try:
                for k, v in patch.items():
                    if hasattr(self.account_config_loader.bilibili, k):
                        setattr(self.account_config_loader.bilibili, k, v)
            except Exception as e:
                logger.warning(f"[{self.account_id}] 同步账号 BiliConfig 失败: {e}")
        # 3) 原子写盘（应用级锁，防多账号互盖）
        try:
            # 只使用调用方显式绑定的配置路径。纯内存 ConfigLoader 不得猜测
            # cwd/config.yaml，否则测试或嵌入式实例会覆盖真实生产配置。
            path = getattr(self.app_config_loader, "filepath", None)
            ok = self.app_config_loader.patch_account_credentials(
                self.account_id, patch, filepath=path,
            )
            if ok:
                logger.info(
                    f"[{self.account_id}] 凭据已持久化: {', '.join(sorted(patch.keys()))}"
                )
        except Exception as e:
            logger.error(
                f"[{self.account_id}] 凭据写盘失败: {e}",
                exc_info=True,
            )
        # 4) 同步配置注册表（Web 列表/状态）
        try:
            mgr = self.account_manager
            if mgr is not None and hasattr(mgr, "config_registry"):
                reg = mgr.config_registry
                if reg is not None and reg.has(self.account_id):
                    reg.update(self.account_id, patch)
        except Exception as e:
            logger.warning(f"[{self.account_id}] 同步配置注册表失败: {e}")

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
                # M10：超时从 5 秒增加到 10 秒，给调度任务更多优雅退出时间
                await asyncio.wait_for(self._scheduler_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                # M10：不再静默吞掉异常，记录 warning 便于排查
                logger.warning(f"[{self.account_id}] 等待调度任务结束超时/取消: {e}")
            except Exception as e:
                logger.warning(f"[{self.account_id}] 等待调度任务结束异常: {e}")
            self._scheduler_task = None
        # V6 jobs are durable. 关闭前尽量 run_jobs_until_idle 冲刷向量/衍生 job，
        # 再 stop worker；未完成 lease 仍可在重启后恢复。
        if self.memory_brain is not None:
            try:
                idle = getattr(self.memory_brain, "run_jobs_until_idle", None)
                if callable(idle):
                    await asyncio.wait_for(idle(max_jobs=64), timeout=12.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[{self.account_id}] 关闭前记忆 job 冲刷超时（将继续 close worker）"
                )
            except Exception as e:
                logger.warning(
                    f"[{self.account_id}] 关闭前记忆 job 冲刷失败: {e}"
                )
            try:
                await asyncio.wait_for(self.memory_brain.close(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.account_id}] 关闭 V6 记忆大脑超时")
            except Exception as e:
                logger.warning(f"[{self.account_id}] 关闭 V6 记忆大脑失败: {e}")
            self.memory_brain = None
        # B3：关闭视频理解（线程池 / Whisper / 临时目录 / 定时清理）
        vu = getattr(self, "video_understanding", None)
        if vu is not None:
            try:
                try:
                    from bilibot.video_understanding.cleanup import cancel_all_scheduled
                    cancel_all_scheduled()
                except Exception as ce:
                    logger.debug(f"[{self.account_id}] cancel video cleanup timers: {ce}")
                shutdown = getattr(vu, "shutdown", None)
                if callable(shutdown):
                    await asyncio.to_thread(shutdown)
            except Exception as e:
                logger.warning(f"[{self.account_id}] 关闭视频理解服务失败: {e}")
            self.video_understanding = None
        # 关闭 B站 session
        if self.bili is not None:
            try:
                await self.bili.close()
            except Exception as e:
                # M10：记录关闭 session 异常，不再静默
                logger.warning(f"[{self.account_id}] 关闭 B站 session 失败: {e}")
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
        from bilibot.app.config_loader import bili_credentials_are_configured

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
            "id": self.account_id,  # 前端兼容别名
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
            "dede_user_id": self.account_config.get("dede_user_id", ""),
            "authenticated": bili_credentials_are_configured(
                self.account_config.get("sessdata"),
                self.account_config.get("bili_jct"),
            ),
            "has_llm": self.llm is not None,
            "has_bili": self.bili is not None,
            "memory_brain": str(self.memory_brain.db_path) if self.memory_brain else "",
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

    def _rebuild_bili_dependents(self):
        """凭据补齐后重建依赖 bili 的下游组件（不重置记忆/状态）

        当 bili 从 None 变为非 None 时，initialize() 期间绑定了 None 的
        ContextBuilder / CommentContextService / Scheduler 需要重建/刷新以注入
        新的 bili 引用。保留 data_store / user_state / personality /
        knowledge_memory 等已有状态。
        """
        # 重建 ContextBuilder（注入新 bili；保留 companion 引用）
        from bilibot.context_builder import ContextBuilder
        self.context_builder = ContextBuilder(
            data_store=self.data_store,
            user_state=self.user_state,
            persona_store=self.persona_store,
            bili=self.bili,
            config=self.account_config_loader.get_raw_config(),
            knowledge_memory=self.knowledge_memory,
            account_id=self.account_id,
            companion=getattr(self, "companion", None),
        )

        # 重建 CommentContextService（注入新 bili）
        from bilibot.services.comment_context import CommentContextService
        self.comment_context_service = CommentContextService(
            bili=self.bili,
            user_state=self.user_state,
            data_store=self.data_store,
            persona_store=self.persona_store,
            config_loader=self.account_config_loader,
            knowledge_memory=self.knowledge_memory,
            memory_brain=self.memory_brain,
        )

        # 更新 Scheduler 的 bili / context_builder / comment_context_service 引用
        # （不重建 Scheduler，避免丢失正在运行的调度任务）
        if self.scheduler is not None:
            self.scheduler.bili = self.bili
            self.scheduler.context_builder = self.context_builder
            self.scheduler.comment_context_service = self.comment_context_service

        logger.info(f"[{self.account_id}] 已重建依赖 bili 的下游组件")

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
            bili_was_none = self.bili is None
            if self.bili is not None:
                self.bili.reload_credentials(self.account_config_loader)
                self.bili.set_credential_update_callback(self._on_bili_credentials_updated)
                logger.info(f"[{self.account_id}] BilibiliAPI 凭据已热重载")
            elif self.account_config_loader.bilibili.is_authenticated:
                # 之前未初始化（凭据缺失），现在有了 → 创建 BilibiliAPI
                from bilibot.bilibili_api import BilibiliAPI
                self.bili = BilibiliAPI(self.account_config_loader)
                self.bili.set_credential_update_callback(self._on_bili_credentials_updated)
                try:
                    await self.bili.ensure_buvid()
                except Exception as e:
                    logger.warning(f"[{self.account_id}] ensure_buvid 失败: {e}")
                logger.info(f"[{self.account_id}] BilibiliAPI 已创建（凭据补齐）")

            if self.scheduler is not None:
                self.scheduler.config_loader = self.account_config_loader
            if self.comment_context_service is not None:
                self.comment_context_service.config_loader = self.account_config_loader

            # 凭据从 None 补齐时，下游组件（ContextBuilder/CommentContextService/
            # Scheduler）在 initialize() 时已绑定旧引用（None），需重建以注入新 bili
            if bili_was_none and self.bili is not None:
                self._rebuild_bili_dependents()

            # 配置段热重载：视频理解 / 联网搜索 / 互动预算（不重启调度器）
            self._reload_runtime_services()

            return {"reloaded": True, "message": "凭据已热重载"}
        except Exception as e:
            logger.error(f"[{self.account_id}] 热重载失败: {e}", exc_info=True)
            return {"reloaded": False, "message": str(e)}

    def _reload_runtime_services(self) -> None:
        """热重载账号运行时服务配置（VU / web_search / interactions）。

        在 config.yaml 被 PATCH 或磁盘 reload 后调用，使 Web 保存立即进入运行中的
        Scheduler / VideoUnderstandingService，而不必重启账号。
        """
        raw = {}
        try:
            raw = (
                self.account_config_loader.get_raw_config()
                if self.account_config_loader is not None
                else {}
            )
        except Exception as e:
            logger.warning(f"[{self.account_id}] 读取配置失败，跳过服务热重载: {e}")
            return

        # 1) 视频理解：重建 cfg 快照
        try:
            vu = self.video_understanding
            if vu is not None and hasattr(vu, "reload_config"):
                vu.reload_config(self.account_config_loader)
            elif vu is None and self.llm_manager is not None:
                va = raw.get("video_analysis") or {}
                if isinstance(va, dict) and va.get("enabled"):
                    from bilibot.video_understanding import VideoUnderstandingService
                    self.video_understanding = VideoUnderstandingService(
                        self.llm_manager, self.account_config_loader
                    )
                    logger.info(f"[{self.account_id}] 视频理解服务已按配置新建")
            # 同步 scheduler / bangumi 上的 VU 引用
            if self.scheduler is not None:
                self.scheduler.video_understanding = self.video_understanding
                bangumi = getattr(self.scheduler, "bangumi_service", None)
                if bangumi is not None and hasattr(bangumi, "video_service"):
                    bangumi.video_service = self.video_understanding
                if bangumi is not None and hasattr(bangumi, "config"):
                    bangumi.config = self.account_config_loader
                # 兼容若未来改名 config_loader
                if bangumi is not None and hasattr(bangumi, "config_loader"):
                    bangumi.config_loader = self.account_config_loader
        except Exception as e:
            logger.warning(f"[{self.account_id}] 视频理解热重载失败: {e}")

        # 2) 联网搜索：已有实例 reload；新启用则创建
        try:
            sched = self.scheduler
            if sched is not None:
                ws_cfg = raw.get("web_search") or {}
                # 仅以 web_search.enabled 为准（features.web_search 已废弃双读）
                ws_enabled = bool(
                    isinstance(ws_cfg, dict) and ws_cfg.get("enabled", False)
                )
                existing = getattr(sched, "web_search", None)
                if existing is not None and hasattr(existing, "reload_config"):
                    existing.reload_config(raw)
                elif ws_enabled and existing is None:
                    from bilibot.services.web_search import WebSearchService
                    sched.web_search = WebSearchService(
                        raw,
                        llm_provider=getattr(sched, "llm", None),
                        data_store=getattr(sched, "ds", None),
                        audit_store=getattr(sched, "audit_store", None),
                        account_id=self.account_id,
                    )
                    logger.info(f"[{self.account_id}] 联网搜索服务已按配置新建")
                # 同步 reply / comment_context 上的引用（构造时一次性注入，必须再绑一次）
                reply_gen = getattr(sched, "reply_gen", None)
                if reply_gen is not None and hasattr(reply_gen, "web_search"):
                    reply_gen.web_search = getattr(sched, "web_search", None)
                if reply_gen is not None and hasattr(reply_gen, "config"):
                    reply_gen.config = self.account_config_loader
                ccs = self.comment_context_service
                if ccs is not None and hasattr(ccs, "web_search"):
                    ccs.web_search = getattr(sched, "web_search", None)
        except Exception as e:
            logger.warning(f"[{self.account_id}] 联网搜索热重载失败: {e}")

        # 3) 互动预算 / 主动评论策略
        try:
            sched = self.scheduler
            if sched is not None:
                policy = getattr(sched, "interaction_policy", None)
                if policy is not None and hasattr(policy, "reload_config"):
                    policy.reload_config(raw)
                comment_policy = getattr(sched, "comment_policy", None)
                if comment_policy is not None and hasattr(comment_policy, "reload_config"):
                    comment_policy.reload_config(raw)
        except Exception as e:
            logger.warning(f"[{self.account_id}] 互动策略热重载失败: {e}")

        # 4) 陪伴生活层：热读 companion.*；同步 web_search / draft / memory_brain 引用
        try:
            if self.companion is not None:
                self.companion.config_loader = self.account_config_loader
                self.companion.llm = self.llm
                self.companion.persona_store = self.persona_store
                self.companion.reload_config()
                # 热重载后 brain 仍指向当前账号（enabled=false 时也不写脏）
                if hasattr(self.companion, "rebind_memory_brain"):
                    try:
                        self.companion.rebind_memory_brain(self.memory_brain)
                    except Exception:
                        self.companion.memory_brain = self.memory_brain
                else:
                    self.companion.memory_brain = self.memory_brain
                if hasattr(self.companion, "rebind_safety_checker"):
                    self.companion.rebind_safety_checker(self.safety_checker)
                else:
                    self.companion.safety_checker = self.safety_checker
                if self.scheduler is not None:
                    self.companion.web_search = getattr(self.scheduler, "web_search", None)
                    try:
                        self.companion.draft_store = self.scheduler.get_draft_store()
                    except Exception:
                        pass
                if self.context_builder is not None:
                    self.context_builder.companion = self.companion
                # 保持 scheduler 引用一致
                if self.scheduler is not None:
                    self.scheduler.companion = self.companion
            elif self.companion is None and self.account_config_loader is not None:
                # 运行中从未初始化过 companion 时，按配置补建
                from bilibot.companion import load_companion_config

                cfg = load_companion_config(raw)
                if cfg.enabled:
                    from bilibot.companion import CompanionLifeService

                    self.companion = CompanionLifeService(
                        self.account_id,
                        self.account_data_dir,
                        config_loader=self.account_config_loader,
                        llm=self.llm,
                        persona_store=self.persona_store,
                        memory_brain=self.memory_brain,
                        safety_checker=self.safety_checker,
                        web_search=getattr(self.scheduler, "web_search", None) if self.scheduler else None,
                        draft_store=(
                            self.scheduler.get_draft_store()
                            if self.scheduler is not None
                            else None
                        ),
                    )
                    if self.context_builder is not None:
                        self.context_builder.companion = self.companion
                    if self.scheduler is not None:
                        self.scheduler.companion = self.companion
                    logger.info(f"[{self.account_id}] 陪伴生活层已按配置新建")
            # companion.enabled 从 true→false 时：不销毁对象，但确保引用同步且不写脏
            # （service 内部 _archive_text / get_prompt_surface 已检查 enabled）
            if self.companion is not None and self.context_builder is not None:
                self.context_builder.companion = self.companion
            if self.companion is not None and self.scheduler is not None:
                self.scheduler.companion = self.companion
        except Exception as e:
            logger.warning(f"[{self.account_id}] 陪伴生活层热重载失败: {e}")

        # 4) PersonalitySystem / UserStateSystem 持有旧 ConfigLoader，必须换新
        try:
            if self.personality is not None and hasattr(self.personality, "config"):
                self.personality.config = self.account_config_loader
            sched = self.scheduler
            if sched is not None:
                personality = getattr(sched, "personality", None)
                if personality is not None and hasattr(personality, "config"):
                    personality.config = self.account_config_loader
            if self.user_state is not None and hasattr(self.user_state, "config"):
                self.user_state.config = self.account_config_loader
        except Exception as e:
            logger.warning(f"[{self.account_id}] Personality/UserState 配置热重载失败: {e}")


        # 5) features.bangumi 开关：新建或清空 BangumiService；热重载同步 brain
        try:
            sched = self.scheduler
            if sched is not None:
                want_bangumi = bool((raw.get("features") or {}).get("bangumi", False))
                existing_bg = getattr(sched, "bangumi_service", None)
                brain = getattr(self, "memory_brain", None) or getattr(
                    sched, "memory_brain", None
                )
                if want_bangumi and brain is None:
                    logger.error(
                        f"[{self.account_id}] features.bangumi=true 但 memory_brain 缺失，"
                        "不创建追番服务（fail-closed）"
                    )
                    if existing_bg is not None:
                        sched.bangumi_service = None
                elif want_bangumi and existing_bg is None:
                    from bilibot.bangumi import BangumiService
                    data_dir = (
                        str(getattr(brain, "data_dir", "") or "")
                        or raw.get("data_dir", self.account_data_dir)
                        or self.account_data_dir
                    )
                    sched.bangumi_service = BangumiService(
                        bili_api=self.bili,
                        llm_manager=self.llm_manager or getattr(sched, "llm", None),
                        video_service=self.video_understanding,
                        config_loader=self.account_config_loader,
                        data_dir=data_dir,
                        memory_brain=brain,
                        account_id=self.account_id,
                        companion=getattr(self, "companion", None),
                        safety_checker=self.safety_checker,
                    )
                    logger.info(f"[{self.account_id}] 番剧追番服务已按配置新建")
                elif not want_bangumi and existing_bg is not None:
                    sched.bangumi_service = None
                    logger.info(f"[{self.account_id}] 番剧追番服务已按配置关闭")
                elif want_bangumi and existing_bg is not None:
                    if hasattr(existing_bg, "config"):
                        existing_bg.config = self.account_config_loader
                    if hasattr(existing_bg, "config_loader"):
                        existing_bg.config_loader = self.account_config_loader
                    if hasattr(existing_bg, "video_service"):
                        existing_bg.video_service = self.video_understanding
                    # 热重载后确保仍指向当前账号 brain + companion 生活面
                    existing_bg.companion = getattr(self, "companion", None)
                    if hasattr(existing_bg, "rebind_safety_checker"):
                        existing_bg.rebind_safety_checker(self.safety_checker)
                    else:
                        existing_bg.safety_checker = self.safety_checker
                    if brain is not None and hasattr(existing_bg, "rebind_memory_brain"):
                        try:
                            existing_bg.rebind_memory_brain(brain)
                        except Exception as rebind_exc:
                            logger.warning(
                                f"[{self.account_id}] bangumi rebind brain 失败: {rebind_exc}"
                            )
                            # fail-closed: drop service rather than write to wrong/no brain
                            sched.bangumi_service = None
                    elif brain is not None:
                        existing_bg.memory_brain = brain
                    if hasattr(existing_bg, "account_id"):
                        existing_bg.account_id = self.account_id
        except Exception as e:
            logger.warning(f"[{self.account_id}] 番剧服务热重载失败: {e}")

    def __repr__(self) -> str:
        return f"<AccountInstance id={self.account_id!r} name={self.name!r} running={self._started}>"
