"""
BiliBot 主应用

独立运行的 B 站 AI Bot 服务。

特性：
- 完全独立，不依赖 AstrBot
- 支持多个人格管理和切换
- 统一的 Prompt 编排
- 强化的上下文理解
- 现代化的 Web 控制台
"""
import asyncio
import argparse
import logging
import logging.handlers
import os
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()
logger = logging.getLogger("bilibot")


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
# 记录当前文件 handler 路径，热更 level 时复用；路径变更则替换 handler
_logging_file_path: Optional[str] = None


class _Utf8SafeConsoleStream:
    """Console stream that never crashes on characters the Windows code page rejects.

    Logging messages may legitimately contain emoji (⚠️) or rare CJK glyphs while
    the console wrapper uses cp936/cp1252.  Instead of raising UnicodeEncodeError
    inside StreamHandler.emit, encode with the console's own codec and replace
    unrepresentable characters.
    """

    def __init__(self):
        self._buffer = getattr(sys.stdout, "buffer", None)
        self._fallback = None if self._buffer is not None else sys.stdout
        self._encoding = getattr(sys.stdout, "encoding", None) or "utf-8"

    def write(self, text: str) -> int:
        if self._buffer is not None:
            try:
                payload = text.encode(self._encoding, errors="replace")
                return self._buffer.write(payload)
            except Exception:
                pass
        if self._fallback is None:
            self._fallback = sys.stdout
        try:
            return self._fallback.write(text)
        except UnicodeEncodeError:
            safe = text.encode("ascii", errors="replace").decode("ascii")
            return self._fallback.write(safe)

    def flush(self) -> None:
        target = self._buffer if self._buffer is not None else self._fallback
        flush = getattr(target, "flush", None)
        if callable(flush):
            flush()

    @property
    def encoding(self) -> str:
        return self._encoding


def _resolve_log_file_path(log_file: str, data_dir: str) -> str:
    """将 logging.file 规范到 data_dir 内，防止路径穿越写出系统文件。"""
    data_root = Path(data_dir or "./data").resolve()
    candidate = Path(log_file or "./data/bililog.log")
    if not candidate.is_absolute():
        # 相对路径：相对 CWD 解析后再校验；越界则落到 data_dir 默认名
        resolved = candidate.resolve()
    else:
        resolved = candidate.resolve()
    try:
        resolved.relative_to(data_root)
        return str(resolved)
    except ValueError:
        safe = data_root / "bililog.log"
        logger.warning(
            "logging.file=%s 不在 data_dir=%s 内，已回退为 %s",
            log_file,
            data_root,
            safe,
        )
        return str(safe)


def setup_logging(config: dict):
    """配置 / 热重载日志系统。

    - 首次调用：basicConfig 建 Stream + RotatingFileHandler
    - 再次调用：更新 root/bilibot 与各 handler 的 level；
      若 file 路径变化则替换 FileHandler（max_bytes/backup 变更亦重建文件 handler）
    - logging.file 必须 resolve 后位于 data_dir 下
    """
    global _logging_file_path
    log_cfg = (config or {}).get("logging", {}) or {}
    log_level = str(log_cfg.get("level", "INFO") or "INFO").upper()
    data_dir = (config or {}).get("data_dir", "./data") or "./data"
    log_file = _resolve_log_file_path(
        log_cfg.get("file", "./data/bililog.log") or "./data/bililog.log",
        data_dir,
    )
    max_bytes = int(log_cfg.get("max_bytes", 10485760) or 10485760)
    backup_count = int(log_cfg.get("backup_count", 5) or 5)
    level = getattr(logging, log_level, logging.INFO)

    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    console_stream = _Utf8SafeConsoleStream()
    # 首次：尚无 handler 时用 basicConfig
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format=_LOG_FORMAT,
            datefmt=_LOG_DATEFMT,
            handlers=[
                logging.StreamHandler(console_stream),
                logging.handlers.RotatingFileHandler(
                    log_file,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                ),
            ],
        )
        _logging_file_path = os.path.abspath(log_file)
    else:
        root.setLevel(level)
        for handler in list(root.handlers):
            handler.setLevel(level)
            # 热重载/测试注入时统一替换控制台流，避免 GBK 控制台编码崩溃
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.handlers.RotatingFileHandler
            ):
                handler.stream = console_stream
        # 文件路径或滚动参数变更 → 替换 RotatingFileHandler
        need_new_file = True
        for handler in list(root.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                same_path = os.path.abspath(getattr(handler, "baseFilename", "") or "") == os.path.abspath(log_file)
                same_roll = (
                    getattr(handler, "maxBytes", None) == max_bytes
                    and getattr(handler, "backupCount", None) == backup_count
                )
                if same_path and same_roll:
                    need_new_file = False
                else:
                    root.removeHandler(handler)
                    try:
                        handler.close()
                    except Exception:
                        pass
        if need_new_file:
            fh = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
            root.addHandler(fh)
            _logging_file_path = os.path.abspath(log_file)

    logging.getLogger("bilibot").setLevel(level)


class BiliBotApp:
    """BiliBot 主应用（V2 多账号架构）"""

    def __init__(self, config: dict, config_path: str = "config.yaml"):
        self.config = config
        self.config_path = config_path
        self.start_time = time.time()

        # Bind disk writes only when the supplied config actually came from the
        # declared file.  Tests/embedders often pass an in-memory config together
        # with the default relative path; treating that as authority can overwrite
        # an unrelated production config in the current working directory.
        bound_config_path: Optional[str] = None
        try:
            if config_path and os.path.isfile(config_path):
                import yaml

                with open(config_path, "r", encoding="utf-8") as fh:
                    disk_config = yaml.safe_load(fh) or {}
                if disk_config == config:
                    bound_config_path = config_path
                else:
                    logger.warning(
                        "传入配置与 config_path 内容不一致；禁用该路径的自动写入: %s",
                        config_path,
                    )
            elif config_path:
                # A non-existent explicit destination cannot alias an existing
                # production file. Treat it as an authorized future config path.
                bound_config_path = config_path
        except Exception as exc:
            logger.warning("无法验证 config_path，已禁用自动写入: %s", type(exc).__name__)
        self._config_path_verified = bound_config_path is not None

        # 配置加载器（应用级）
        from bilibot.app.config_loader import ConfigLoader
        self.config_loader = ConfigLoader(config, filepath=bound_config_path)

        # 数据根目录
        self.data_root = config.get("data_dir", "./data")

        # 人格库（应用级共享，账号通过绑定选择人格）
        # PRD V3 §7：注入 config_loader 用于读取 profiles 多人格组配置
        from bilibot.services.persona_store import PersonaStore
        self.persona_store = PersonaStore(data_dir=self.data_root, config_loader=self.config_loader)

        # 应用级 AuditStore 单例（PRD V3 §10.2）
        from bilibot.services.audit_store import AuditStore
        self.audit_store = AuditStore(data_dir=self.data_root)

        # Token 用量统计（全局，供控制台「用量统计」页）
        try:
            from bilibot.services.token_usage import TokenUsageStore, set_global_token_store
            self.token_usage_store = TokenUsageStore(data_dir=self.data_root)
            set_global_token_store(self.token_usage_store)
        except Exception as e:
            logging.getLogger("bilibot").warning("TokenUsageStore 初始化失败: %s", e)
            self.token_usage_store = None

        # PRD V4 BOOT-003：SafetyService 在任何 Scheduler 启动前创建，
        # 不依赖 Web 初始化。web.enabled=false 不影响限流、黑名单、
        # 全局暂停、内容检查和审计。
        from bilibot.services.safety import SafetyChecker, build_safety_config
        safety_config = build_safety_config(config)
        self.safety_checker = SafetyChecker(data_dir=self.data_root, config=safety_config)

        # Prompt 编排器（应用级，memory 在 initialize 后注入）
        from bilibot.prompts.orchestrator import PromptOrchestrator
        self.orchestrator = PromptOrchestrator(self.persona_store)

        # PRD-V5 §5.1 ACC-502：不再创建应用级共享 ContextBuilder。
        # 每个账号在 AccountInstance.initialize() 中用本账号依赖
        # (DataStore / UserState / BiliClient / KnowledgeMemory) 创建独立实例，
        # 避免非默认账号读取到默认账号的 recent behavior。
        self.context_builder = None

        # V2：LLM 管理器（应用级，管理多个 LLMProvider）
        from bilibot.llm import LLMManager
        self.llm_manager = LLMManager(self.config_loader)

        # V2：账号管理器（应用级，管理多个 AccountInstance）
        # ACC-502：context_builder=None，账号内部会自建账号级实例
        from bilibot.account import AccountManager
        self.account_manager = AccountManager(
            persona_store=self.persona_store,
            llm_manager=self.llm_manager,
            audit_store=self.audit_store,
            orchestrator=self.orchestrator,
            context_builder=None,
            app_config_loader=self.config_loader,
            data_root=self.data_root,
            safety_checker=self.safety_checker,
        )

        # 向后兼容属性（指向默认账号的组件，initialize 后填充）
        self.data_store = None
        self.memory = None
        self.llm = None
        self.bili = None
        self.scheduler = None
        self.knowledge_memory = None
        self.comment_context_service = None

        # PRD-V5 §8.2 VID-503：配置 App 级全局视频分析并发 semaphore
        # 限制所有账号合计的视频分析并发数（在账号初始化前配置）
        try:
            from bilibot.video_understanding import configure_global_semaphore
            va_config = config.get("video_analysis", {})
            max_concurrent_global = int(va_config.get("max_concurrent_global", 1))
            configure_global_semaphore(max_concurrent_global)
        except Exception as e:
            logger.warning(f"视频分析全局 semaphore 配置失败: {e}")

    async def initialize(self):
        """初始化各子系统（V2：LLMManager + AccountManager）"""
        # 1. LLM 管理器初始化（加载所有 llm_providers，兼容 V1 llm 配置）
        self.llm_manager.initialize()

        # 2. 账号管理器初始化（加载所有 accounts，兼容 V1 bilibili 配置）
        self.account_manager.initialize()

        # 3. 并发初始化所有账号实例（每个账号独立的 DataStore/Bili/Memory/Scheduler）
        await self.account_manager.initialize_all()

        # 4. 向后兼容：从默认账号同步组件引用
        self._sync_legacy_attrs()

        logger.info(
            f"BiliBot 初始化完成（V2）：{len(self.account_manager)} 个账号，"
            f"{len(self.llm_manager)} 个 LLM Provider"
        )

    def _sync_legacy_attrs(self):
        """从默认账号同步向后兼容属性（供 Web 面板等旧代码使用）

        PRD V4 BOOT-001：不再读取 acc.memory（AccountInstance 无此属性），
        统一使用 acc.knowledge_memory。self.memory 作为废弃别名保留，
        指向 knowledge_memory。

        PRD-V5 §5.1 ACC-502：不再回填共享 ContextBuilder 的 ds/bili。
        self.context_builder 指向默认账号的账号级 ContextBuilder，
        仅供 Web 面板等旧代码只读预览使用；账号生成流程使用各自
        AccountInstance.context_builder。
        """
        acc = self.account_manager.get_default()
        if acc:
            self.data_store = acc.data_store
            self.llm = acc.llm
            self.bili = acc.bili
            self.scheduler = acc.scheduler
            self.knowledge_memory = acc.knowledge_memory
            self.comment_context_service = acc.comment_context_service
            # 废弃别名：指向 knowledge_memory，供旧代码过渡
            self.memory = acc.knowledge_memory
            # ACC-502：指向默认账号的 ContextBuilder（只读预览用途）
            self.context_builder = acc.context_builder

        # 回填 orchestrator 依赖（PRD V3 §8.2）
        self.orchestrator.memory = self.knowledge_memory

    async def start(self):
        """启动服务

        PRD V4 §4.1.1：
        - 必须真实调用 `server.serve()`，不得只创建 server 不启动。
        - Web 异常退出时主进程应退出，不得假装运行。
        - 收到停止信号时优雅关闭所有账号 Scheduler / Bilibili API session / Uvicorn server。
        """
        from bilibot.web.panel import create_web_app

        if self.config.get("web", {}).get("enabled", True) and not self._config_path_verified:
            raise RuntimeError(
                "Web mode requires config_path to match the supplied configuration; "
                "refusing unsafe config writes"
            )

        # 初始化
        await self.initialize()

        # ── 启动时安全检查：默认密码 / secret_key ──
        # secret_key 仍为默认值时，随机生成并写回配置文件
        if self.config_loader.web.secret_key == "change-this-to-a-random-string":
            new_secret = secrets.token_hex(32)
            logger.warning("检测到 web.secret_key 仍为默认值，已自动随机生成")
            raw = self.config_loader.get_raw_config()
            raw.setdefault("web", {})["secret_key"] = new_secret
            if self._config_path_verified:
                self.config_loader.save_config(raw, self.config_path)
            else:
                logger.warning(
                    "config_path 未通过来源校验，secret_key 仅在本进程生效且配置接口为只读"
                )
            # 同步 self.config 供后续 start() 读取
            self.config = self.config_loader.get_raw_config()

        # admin_password 为弱口令（明文 admin123 或 bcrypt 哈希匹配）时告警；
        # 非本地监听则拒绝启动 Web 面板
        from bilibot.web.panel import is_weak_admin_password
        if is_weak_admin_password(self.config_loader.web.admin_password):
            logger.warning(
                "⚠️ 检测到 web.admin_password 仍为默认弱口令 'admin123'（明文或 bcrypt），"
                "存在被接管风险，请尽快修改！"
            )
            if (self.config_loader.web.enabled
                    and self.config_loader.web.host not in ("127.0.0.1", "localhost", "::1")):
                logger.error(
                    "拒绝启动 Web 面板：admin_password 为默认弱口令且监听非本地地址 "
                    f"({self.config_loader.web.host})，请修改 config.yaml 中 "
                    "web.admin_password 后重试"
                )
                raise RuntimeError(
                    "Refusing to start Web panel: default admin_password on non-localhost host"
                )

        # 启动所有账号调度器（始终启动，即使 Web 禁用也允许 Scheduler 运行）
        scheduler_task = asyncio.create_task(self._run_accounts_safe())

        # Task 19：secure_cookies 关闭且监听非 localhost 时，Cookie 将明文传输
        if self.config_loader.web.enabled and not self.config_loader.web.secure_cookies:
            host = self.config_loader.web.host
            if host not in ("127.0.0.1", "localhost", "::1"):
                logger.warning("web.secure_cookies=false 且 host 非 localhost，Cookie 将明文传输")

        # Web 服务
        web_config = self.config.get("web", {})
        web_enabled = bool(web_config.get("enabled", True))
        host = web_config.get("host", "127.0.0.1")
        port = int(web_config.get("port", 8080))

        if not web_enabled:
            logger.warning("Web 服务已在配置中禁用（web.enabled=false），仅运行 Scheduler")
            console.print(Panel(
                Text("BiliBot 已启动（Web 已禁用）", style="bold yellow"),
                subtitle=f"账号数: {len(self.account_manager)}",
                border_style="yellow",
            ))
            stop_event = asyncio.Event()
            self._install_signal_handlers(stop_event)
            try:
                await stop_event.wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            await self._graceful_shutdown(scheduler_task)
            return

        # 创建 Web 应用（传入默认账号的 scheduler 保持向后兼容）
        app = create_web_app(
            config_loader=self.config_loader,
            persona_store=self.persona_store,
            orchestrator=self.orchestrator,
            scheduler=self.scheduler,
            config_path=(self.config_path if self._config_path_verified else ""),
            audit_store=self.audit_store,
            context_builder=self.context_builder,
            account_manager=self.account_manager,
            llm_manager=self.llm_manager,
            safety_checker=self.safety_checker,
        )

        # PRD V4 BOOT-003：SafetyChecker 已在 App 层创建并注入所有 Scheduler，
        # 不再需要从 panel 回填。

        # 启动 Uvicorn
        import uvicorn
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        # PRD V4 §4.1.1：必须真实调用 serve()
        server_task = asyncio.create_task(server.serve())

        console.print(Panel(
            Text("BiliBot 服务已启动（V2 多账号）", style="bold green"),
            subtitle=f"Web面板: http://{host}:{port} | 账号数: {len(self.account_manager)}",
            border_style="blue",
        ))

        # 停止信号
        stop_event = asyncio.Event()
        self._install_signal_handlers(stop_event)

        # 等待「停止信号」或「Web 异常退出」任一先发生
        # M6：把 stop_event.wait() 任务存为变量，结束后 cancel 避免孤儿任务
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                [server_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            if not stop_task.done():
                stop_task.cancel()
                try:
                    await stop_task
                except (asyncio.CancelledError, Exception):
                    pass

        # 优雅关闭
        server.should_exit = True
        await self._graceful_shutdown(scheduler_task)
        # 等待 server 退出（如果还没退出）
        if not server_task.done():
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                try:
                    await server_task
                except asyncio.CancelledError:
                    pass

        # 如果 Web 异常退出，主进程也退出（PRD V4 §5.1）
        if server_task.done() and not server_task.cancelled():
            exc = server_task.exception()
            if exc is not None:
                logger.error(f"Web 服务异常退出: {exc}", exc_info=exc)
                raise exc

    def _install_signal_handlers(self, stop_event: asyncio.Event):
        """安装 SIGINT / SIGTERM 信号处理"""
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda: stop_event.set())
                except NotImplementedError:
                    # M9：Windows 不支持 add_signal_handler，改用 signal.signal 注册回调
                    # 仅对 SIGINT 注册（SIGTERM 在 Windows 上语义不同，交给默认处理）
                    if sig == signal.SIGINT:
                        try:
                            signal.signal(signal.SIGINT, lambda *_: stop_event.set())
                        except (ValueError, OSError) as e:
                            # 非主线程时 signal.signal 也会抛 ValueError，此时退回依赖 KeyboardInterrupt
                            logger.warning(f"Windows 信号注册失败，将依赖 KeyboardInterrupt: {e}")
                    pass
        except Exception:
            pass

    async def _run_accounts_safe(self):
        """安全运行所有账号调度器，捕获异常避免拖垮 Web"""
        try:
            await self.account_manager.start_all()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"账号管理器异常退出: {e}", exc_info=True)

    async def _graceful_shutdown(self, scheduler_task: asyncio.Task):
        """优雅关闭

        PRD V4 BOOT-004 关闭顺序：
        1. 将所有账号标记为 stopping（停止接受新任务）
        2. 等待正在进行的发布任务，超时后取消
        3. 保存任务和回复状态
        4. flush 向量索引与搜索缓存（账号 close 内 run_jobs_until_idle + queue drain）
        5. 关闭记忆、HTTP Session 和数据库
        """
        logger.info("正在关闭 BiliBot...")
        # 1-5: stop_all 调用每个 acc.close()，按顺序：
        #   stop scheduler → wait task → flush memory jobs → close bili session
        try:
            await self.account_manager.stop_all()
        except Exception as e:
            logger.warning(f"停止账号时出错: {e}")
        # 取消 start_all 协程（通常已立即返回，cancel 是 no-op）
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except (asyncio.CancelledError, Exception):
                pass
        # 二次保险：关闭全局 token store 连接（若实现了 close）
        try:
            from bilibot.services.token_usage import get_global_token_store
            store = get_global_token_store()
            closer = getattr(store, "close", None) if store is not None else None
            if callable(closer):
                await asyncio.to_thread(closer)
        except Exception as e:
            logger.debug("token store close skipped: %s", e)
        # 二次保险：关闭全局 Whisper 工作池与视频清理定时器（账号 close 已调，防漏）
        try:
            from bilibot.video_understanding.cleanup import cancel_all_scheduled
            cancel_all_scheduled()
        except Exception as e:
            logger.debug("video cleanup cancel skipped: %s", e)
        try:
            from bilibot.video_understanding.audio_track import shutdown_whisper_executor
            await asyncio.to_thread(shutdown_whisper_executor)
        except Exception as e:
            logger.debug("whisper executor shutdown skipped: %s", e)
        # 审计库：若暴露 close/checkpoint 则收尾
        try:
            audit = getattr(self, "audit_store", None)
            if audit is not None:
                closer = getattr(audit, "close", None)
                if callable(closer):
                    await asyncio.to_thread(closer)
                else:
                    ck = getattr(audit, "checkpoint", None)
                    if callable(ck):
                        await asyncio.to_thread(ck)
        except Exception as e:
            logger.debug("audit store close skipped: %s", e)
        # 备份根目录：进程退出前再扫一遍过期 .*.creating
        try:
            from pathlib import Path
            from bilibot.api.backup import _cleanup_stale_staging
            root = Path(self.data_root or "./data") / "backups"
            n = await asyncio.to_thread(_cleanup_stale_staging, root)
            if n:
                logger.info("关闭时清理过期备份 staging %s 个", n)
        except Exception as e:
            logger.debug("backup staging cleanup on shutdown skipped: %s", e)
        logger.info("BiliBot 已关闭")

    def get_status(self) -> dict:
        """获取运行状态"""
        uptime = time.time() - self.start_time
        days = int(uptime // 86400)
        hours = int((uptime % 86400) // 3600)
        mins = int((uptime % 3600) // 60)

        accounts_status = self.account_manager.list_accounts()
        default_acc = self.account_manager.get_default()

        return {
            "running": True,
            "uptime": f"{days}天{hours}时{mins}分",
            "uptime_seconds": int(uptime),
            "current_persona": self.persona_store.get_current_dict(),
            "accounts": accounts_status,
            "account_count": len(self.account_manager),
            "llm_providers": self.llm_manager.list_providers(),
            # 向后兼容：默认账号信息
            "bilibili": {
                "authenticated": default_acc.bili is not None if default_acc else False,
                "uid": default_acc.account_config.get("dede_user_id", "") if default_acc else "",
            },
            "llm": {
                "connected": self.llm_manager.get_default() is not None,
                "model": self.llm_manager.get_default().model if self.llm_manager.get_default() else "",
            },
        }


# 全局应用实例
_app_instance: Optional[BiliBotApp] = None


def get_app() -> Optional[BiliBotApp]:
    return _app_instance


def set_app(app: BiliBotApp):
    global _app_instance
    _app_instance = app


def main():
    """主入口"""
    parser = argparse.ArgumentParser(
        description="BiliBot - 独立版 B站 AI Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config.yaml",
        help="配置文件路径",
    )
    parser.add_argument(
        "--quickstart",
        action="store_true",
        help="快速配置向导",
    )

    args = parser.parse_args()

    # 检查配置文件
    config_path = args.config
    if not os.path.exists(config_path):
        console.print(f"[yellow]配置文件 {config_path} 不存在，复制示例配置...[/]")
        if os.path.exists("config.example.yaml"):
            import shutil
            shutil.copy("config.example.yaml", config_path)
            console.print(f"[green]已创建 {config_path}，请编辑后重新运行[/]")
            sys.exit(1)
        else:
            console.print("[red]错误: 找不到 config.example.yaml[/]")
            sys.exit(1)

    # PRD V4 MIG-001：CLI 启动边界负责迁移实际配置文件。BiliBotApp
    # 构造器只消费调用方传入的 dict，绝不能因为一个相对 config_path
    # 静默读写当前工作目录中的另一份生产配置。
    try:
        from bilibot.services.config_migrator import run_migration
        run_migration(config_path)
    except Exception as e:
        logging.getLogger("bilibot").warning("配置迁移失败（继续加载原配置）: %s", e)

    # 加载配置（迁移成功时读取迁移后的内容）
    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # 创建数据目录
    data_dir = config.get("data_dir", "./data")
    os.makedirs(data_dir, exist_ok=True)

    # 设置日志
    setup_logging(config)

    # 快速配置向导（BUG SYS-001：bilibot/cli/setup.py 缺失，延迟导入 + 友好降级）
    if args.quickstart:
        try:
            from bilibot.cli.setup import run_quickstart
        except ImportError:
            console.print(
                "[yellow]快速配置向导尚未实现，请直接编辑 config.yaml 后运行：[/]\n"
                f"  python -m bilibot --config {config_path}"
            )
            sys.exit(1)
        run_quickstart(config, config_path)
        return

    # 创建并运行应用
    app = BiliBotApp(config, config_path)
    set_app(app)

    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        console.print("\n[yellow]BiliBot 已关闭[/]")


if __name__ == "__main__":
    main()
