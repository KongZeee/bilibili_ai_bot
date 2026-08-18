"""
账号管理器 — 管理多个 AccountInstance

职责：
- 从 config 的 `accounts` 列表加载多个账号
- 兼容 V1 配置（单组 `bilibili` 配置自动转为单个账号）
- 启动/停止/增删改查账号
- 参考 AstrBot PlatformManager

V2 配置结构：
```yaml
accounts:
  - id: main
    name: 主账号
    sessdata: xxx
    bili_jct: xxx
    dede_user_id: 12345
    buvid3: xxx
    refresh_token: xxx
    persona_id: default
    llm_id: siliconflow
    enabled: true
default_account: main
```

V1 兼容：
```yaml
bilibili:
  sessdata: xxx
  bili_jct: xxx
  ...
```
"""
import asyncio
import logging
from typing import Dict, List, Optional, TYPE_CHECKING

from bilibot.account.instance import AccountInstance
from bilibot.account.config_registry import AccountConfigRegistry

logger = logging.getLogger("bilibot.account")

if TYPE_CHECKING:
    from bilibot.app.config_loader import ConfigLoader
    from bilibot.services.persona_store import PersonaStore
    from bilibot.llm.manager import LLMManager
    from bilibot.services.audit_store import AuditStore
    from bilibot.prompts.orchestrator import PromptOrchestrator
    from bilibot.context_builder import ContextBuilder


class AccountManager:
    """管理多个 AccountInstance"""

    def __init__(
        self,
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
        self.persona_store = persona_store
        self.llm_manager = llm_manager
        self.audit_store = audit_store
        self.orchestrator = orchestrator
        self.context_builder = context_builder
        self.app_config_loader = app_config_loader
        self.data_root = data_root
        # PRD V4 BOOT-003：应用级 SafetyChecker，传入每个 AccountInstance
        self.safety_checker = safety_checker

        # PRD-V5 ACC-501：运行时实例（仅 enabled=true 校验通过）
        self._accounts: Dict[str, AccountInstance] = {}
        self._default_id: str = ""
        # PRD-V5 ACC-501：配置注册表（ALL 账号含 disabled / init-failed），事实来源
        self._config_registry = AccountConfigRegistry()
        self._memory_brain_bootstrap = None

    def _maybe_migrate_flat_layout(self) -> None:
        """Best-effort accounts/{id}/ → bot/ migration before brain bootstrap."""
        account_ids = self._config_registry.list_ids()
        if not account_ids:
            return
        configured_default = str(
            self.app_config_loader.get_raw_config().get("default_account", "") or ""
        )
        sole_id = (
            configured_default if configured_default in account_ids else account_ids[0]
        )
        try:
            from bilibot.services.layout_migrate import maybe_auto_migrate

            maybe_auto_migrate(self.data_root, sole_id)
        except Exception as exc:  # noqa: BLE001 — boot must not die on migrate
            logger.error(
                "flat layout auto-migrate failed (continuing boot): %s",
                exc,
                exc_info=True,
            )

    def _bootstrap_memory_brains(self) -> None:
        """Health-gate every configured account before legacy memory cleanup."""
        account_ids = self._config_registry.list_ids()
        if not account_ids:
            return
        from bilibot.memory_brain.bootstrap import bootstrap_accounts

        configured_default = str(
            self.app_config_loader.get_raw_config().get("default_account", "") or ""
        )
        default_account_id = (
            configured_default if configured_default in account_ids else account_ids[0]
        )
        result = bootstrap_accounts(
            self.data_root,
            account_ids,
            cleanup_legacy=True,
            default_account_id=default_account_id,
        )
        self._memory_brain_bootstrap = result
        deleted = sum(1 for item in result.cleanup if item.status == "deleted")
        failed = sum(1 for item in result.cleanup if item.status == "failed")
        logger.info(
            "V6 memory brains healthy for %d configured accounts; legacy cleanup deleted=%d failed=%d",
            len(result.health),
            deleted,
            failed,
        )

    # ══════════════════════════════════════
    #  初始化
    # ══════════════════════════════════════

    def initialize(self):
        """从配置加载所有账号

        PRD-V5 ACC-501：
        - 配置注册表加载 ALL 账号（含 disabled / init-failed）
        - 运行时实例仅创建 enabled=true 的账号
        """
        self._accounts.clear()
        self._default_id = ""

        raw = self.app_config_loader.get_raw_config()

        # V2 配置：accounts 列表
        accounts_list = raw.get("accounts", [])
        if accounts_list:
            # ACC-501：配置注册表加载 ALL 账号（含 disabled / init-failed）
            self._config_registry.load_from_raw(raw)
            # Flat layout: migrate accounts/{sole}/ → bot/ before opening brains.
            self._maybe_migrate_flat_layout()
            # V6: disabled accounts receive a healthy empty brain too. Cleanup is
            # irreversible and therefore happens only after every configured DB passes.
            self._bootstrap_memory_brains()

            default_account = raw.get("default_account", "")
            # 运行时实例：仅创建 enabled=true 的账号
            for acc_id in self._config_registry.list_ids():
                acc_config = self._config_registry.get(acc_id)
                if not acc_config.get("enabled", True):
                    logger.info(f"账号 {acc_id} 已禁用，跳过创建运行时实例")
                    continue
                try:
                    inst = self._create_instance(acc_id, acc_config)
                    self._accounts[acc_id] = inst
                    logger.info(f"已加载账号: {acc_id} ({inst.name})")
                except Exception as e:
                    logger.error(f"加载账号 {acc_id} 失败: {e}")

            if default_account and self._config_registry.has(default_account):
                self._default_id = default_account
            elif self._accounts:
                self._default_id = next(iter(self._accounts))
            else:
                configured_ids = self._config_registry.list_ids()
                self._default_id = configured_ids[0] if configured_ids else ""
            logger.info(f"默认账号: {self._default_id or '(无)'}")
            return

        # V1 兼容：单组 bilibili 配置自动迁移为单个账号
        v1_bili = raw.get("bilibili", {})
        if v1_bili and v1_bili.get("sessdata"):
            acc_config = {
                "id": "default",
                "name": "默认账号",
                "sessdata": v1_bili.get("sessdata", ""),
                "bili_jct": v1_bili.get("bili_jct", ""),
                "dede_user_id": v1_bili.get("dede_user_id", ""),
                "buvid3": v1_bili.get("buvid3", ""),
                "buvid4": v1_bili.get("buvid4", ""),
                "refresh_token": v1_bili.get("refresh_token", ""),
                "persona_id": raw.get("persona_id", ""),
                "llm_id": raw.get("llm_id", ""),
                "enabled": True,
            }
            # ACC-501：V1 迁移也注册到配置注册表
            self._config_registry.load_from_raw({"accounts": [acc_config]})
            self._maybe_migrate_flat_layout()
            self._bootstrap_memory_brains()
            try:
                inst = self._create_instance("default", acc_config)
                self._accounts["default"] = inst
                self._default_id = "default"
                logger.info("V1 配置已迁移为默认账号: default")
            except Exception as e:
                logger.error(f"V1 配置迁移失败: {e}")
        else:
            logger.warning("未检测到账号配置（既无 accounts 也无 bilibili）")

    def _create_instance(self, account_id: str, acc_config: dict) -> AccountInstance:
        """创建 AccountInstance"""
        inst = AccountInstance(
            account_id=account_id,
            account_config=acc_config,
            persona_store=self.persona_store,
            llm_manager=self.llm_manager,
            audit_store=self.audit_store,
            orchestrator=self.orchestrator,
            context_builder=self.context_builder,
            app_config_loader=self.app_config_loader,
            data_root=self.data_root,
            safety_checker=self.safety_checker,
        )
        # 凭据回调写盘后同步 registry
        inst.account_manager = self
        return inst

    # ══════════════════════════════════════
    #  账号访问
    # ══════════════════════════════════════

    def get_account(self, account_id: Optional[str] = None) -> Optional[AccountInstance]:
        """获取指定账号，未指定则返回默认"""
        if account_id:
            return self._accounts.get(account_id)
        if self._default_id:
            return self._accounts.get(self._default_id)
        if self._accounts:
            return next(iter(self._accounts.values()))
        return None

    def get_default(self) -> Optional[AccountInstance]:
        """获取默认账号"""
        return self._accounts.get(self._default_id) if self._default_id else None

    def get_default_id(self) -> str:
        return self._default_id

    def sole_id(self) -> str:
        """单账号产品：返回唯一账号 ID（配置注册表优先，否则默认/运行时）。

        不枚举 data/accounts/ 目录，避免孤儿目录污染。
        """
        ids = self._config_registry.list_ids()
        if ids:
            if self._default_id and self._default_id in ids:
                return self._default_id
            return ids[0]
        if self._default_id:
            return self._default_id
        if self._accounts:
            return next(iter(self._accounts))
        return ""

    def get_sole(self) -> Optional[AccountInstance]:
        """单账号产品：返回唯一运行时实例（可能为 None：禁用/未创建实例）。"""
        acc_id = self.sole_id()
        if not acc_id:
            return None
        return self._accounts.get(acc_id) or self.get_account(acc_id)

    def require_sole_id(self) -> str:
        """返回 sole_id；无账号时返回空串（调用方映射为 NO_ACCOUNT）。"""
        return self.sole_id()

    def list_accounts(self) -> List[dict]:
        """列出所有账号状态（含禁用账号）

        PRD-V5 ACC-501：返回配置注册表中 ALL 账号，运行时状态合并。
        """
        from bilibot.app.config_loader import bili_credentials_are_configured

        result = []
        for cfg in self._config_registry.list_all():
            acc_id = cfg.get("id", "")
            acc = self._accounts.get(acc_id)
            if acc:
                # 运行时实例存在 → 用运行时状态
                status = acc.get_status()
            else:
                # 无运行时实例（禁用或初始化失败）→ 从配置构造状态
                enabled = cfg.get("enabled", True)
                status = {
                    "account_id": acc_id,
                    "id": acc_id,
                    "name": cfg.get("name", acc_id),
                    "enabled": enabled,
                    "running": False,
                    "state": "disabled" if not enabled else "stopped",
                    "last_error": "",
                    "profile_id": cfg.get("profile_id", ""),
                    "persona_id": cfg.get("persona_id", ""),
                    "available_personas": [],
                    "llm_id": cfg.get("llm_id", ""),
                    "uid": cfg.get("dede_user_id", ""),
                    "dede_user_id": cfg.get("dede_user_id", ""),
                    "authenticated": bili_credentials_are_configured(
                        cfg.get("sessdata"), cfg.get("bili_jct")
                    ),
                    "has_llm": False,
                    "has_bili": False,
                }
            status["is_default"] = (acc_id == getattr(self, "_default_id", ""))
            result.append(status)
        return result

    def list_account_ids(self) -> List[str]:
        """列出所有账号 ID（含禁用账号）"""
        return self._config_registry.list_ids()

    # ══════════════════════════════════════
    #  账号管理（运行时增删改）
    # ══════════════════════════════════════

    def add_account(self, acc_config: dict) -> str:
        """
        添加账号（单账号模式：已有账号时 raise ValueError SINGLE_ACCOUNT）

        PRD-V5 ACC-501：同时写入配置注册表和运行时实例。

        Args:
            acc_config: 账号配置

        Returns:
            新账号 ID

        Raises:
            ValueError: 已存在账号时含 SINGLE_ACCOUNT
        """
        if len(self._config_registry) >= 1:
            raise ValueError(
                "SINGLE_ACCOUNT: only one Bilibili account is allowed"
            )
        # ACC-501：先写入配置注册表（事实来源）
        acc_id = self._config_registry.add(acc_config)
        # A newly configured account must have a healthy brain before it can run.
        self._bootstrap_memory_brains()
        # 仅 enabled=true 才创建运行时实例
        if acc_config.get("enabled", True):
            try:
                inst = self._create_instance(acc_id, acc_config)
                self._accounts[acc_id] = inst
            except Exception as e:
                logger.error(f"创建运行时实例失败: {e}")
        if not self._default_id:
            self._default_id = acc_id
        logger.info(f"已添加账号: {acc_id}")
        return acc_id

    def remove_account(self, account_id: str) -> bool:
        """删除账号（停止后移除）

        单账号模式：禁止删除最后一个账号（返回 False）。

        PRD-V5 ACC-501：同时从配置注册表和运行时实例删除，写审计。
        """
        existed = self._config_registry.has(account_id) or account_id in self._accounts
        if not existed:
            return False
        # 最后一个配置账号不可删
        if self._config_registry.has(account_id) and len(self._config_registry) <= 1:
            logger.warning(
                "LAST_ACCOUNT: refuse to remove the only Bilibili account %s",
                account_id,
            )
            return False
        acc = self._accounts.get(account_id)
        if acc:
            try:
                acc.stop()
            except Exception:
                pass
            # M11：stop() 不会关闭 session/记忆系统等异步资源，
            # 检测是否有未关闭资源并提示用户
            unclosed = []
            bili = getattr(acc, "bili", None)
            if bili is not None:
                session = getattr(bili, "session", None)
                if session is not None and not getattr(session, "closed", True):
                    unclosed.append("bili_session")
            if getattr(acc, "knowledge_memory", None) is not None:
                unclosed.append("knowledge_memory")
            if unclosed:
                logger.warning(
                    f"账号 {account_id} 仍有未关闭的资源 ({', '.join(unclosed)})，"
                    f"同步删除无法释放，请重启服务或使用 remove_account_async 以完整关闭"
                )
            del self._accounts[account_id]
        # ACC-501：显式从配置注册表删除
        self._config_registry.delete(account_id)
        if account_id == self._default_id:
            # Task 15：优先回退到运行时实例，若为空则回退到配置注册表第一个账号 ID
            if self._accounts:
                self._default_id = next(iter(self._accounts))
            else:
                registry_ids = self._config_registry.list_ids()
                self._default_id = registry_ids[0] if registry_ids else ""
            logger.warning(f"默认账号 {account_id} 已删除，新默认: {self._default_id or '(无)'}")
        # ACC-501：写安全审计
        self._audit_delete(account_id)
        logger.info(f"已删除账号: {account_id}")
        return True

    async def remove_account_async(self, account_id: str) -> bool:
        """删除账号（含异步关闭 session）

        单账号模式：禁止删除最后一个账号（返回 False）。

        PRD-V5 ACC-501：同时从配置注册表和运行时实例删除，写审计。
        """
        existed = self._config_registry.has(account_id) or account_id in self._accounts
        if not existed:
            return False
        if self._config_registry.has(account_id) and len(self._config_registry) <= 1:
            logger.warning(
                "LAST_ACCOUNT: refuse to remove the only Bilibili account %s",
                account_id,
            )
            return False
        acc = self._accounts.get(account_id)
        try:
            if acc:
                await acc.close()
        finally:
            # 确保异常时也移除运行时实例和配置注册表项，避免账号卡在中间态
            self._accounts.pop(account_id, None)
            # ACC-501：显式从配置注册表删除
            self._config_registry.delete(account_id)
            if account_id == self._default_id:
                # Task 15：优先回退到运行时实例，若为空（剩余账号都是禁用的）
                # 则回退到配置注册表中的第一个账号 ID，避免 _default_id 变为空字符串。
                if self._accounts:
                    self._default_id = next(iter(self._accounts))
                else:
                    registry_ids = self._config_registry.list_ids()
                    self._default_id = registry_ids[0] if registry_ids else ""
            # ACC-501：写安全审计（Task 25：同步 I/O 卸载到线程）
            await asyncio.to_thread(self._audit_delete, account_id)
        logger.info(f"已删除账号: {account_id}")
        return True

    def _audit_delete(self, account_id: str):
        """ACC-501：删除账号写安全审计"""
        if not self.audit_store:
            return
        try:
            self.audit_store.record(
                scene="account_delete",
                persona_id="",
                input_summary=f"删除账号: {account_id}",
                output="",
                published=False,
                target={"account_id": account_id, "action": "delete"},
            )
        except Exception as e:
            logger.warning(f"写删除审计失败: {e}")

    def set_default(self, account_id: str) -> bool:
        """设置默认账号

        Task 15：检查配置注册表（含禁用账号）而非仅运行时实例，
        使禁用账号也能被设为默认（启用后即生效）。
        """
        if not self._config_registry.has(account_id):
            return False
        self._default_id = account_id
        logger.info(f"默认账号已设置为: {account_id}")
        return True

    # ══════════════════════════════════════
    #  批量生命周期
    # ══════════════════════════════════════

    async def initialize_all(self):
        """初始化所有账号（并发）

        B4：init 失败时 partial close 并从运行表移除，避免半初始化实例空转/泄漏。
        配置注册表仍保留该账号（disabled/失败可在面板查看）。
        """
        tasks = [acc.initialize() for acc in self._accounts.values()]
        if tasks:
            # 固定顺序，避免 dict 视图与 gather 结果错位
            items = list(self._accounts.items())
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed_ids = []
            for (acc_id, acc), result in zip(items, results):
                if isinstance(result, Exception):
                    logger.error(f"账号 {acc_id} 初始化失败: {result}", exc_info=result)
                    failed_ids.append(acc_id)
                    try:
                        await acc.close()
                    except Exception as close_err:
                        logger.warning(
                            f"账号 {acc_id} 初始化失败后 partial close 异常: {close_err}"
                        )
                else:
                    logger.info(f"账号 {acc_id} 初始化成功")
            for acc_id in failed_ids:
                self._accounts.pop(acc_id, None)
                if self._default_id == acc_id:
                    self._default_id = next(iter(self._accounts), "")
                    if self._default_id:
                        logger.warning(
                            f"默认账号 {acc_id} 初始化失败，回退默认账号为 {self._default_id}"
                        )
                    else:
                        logger.warning(
                            f"默认账号 {acc_id} 初始化失败，当前无可用运行时账号"
                        )

    async def start_all(self):
        """启动所有账号调度器（并发）

        PRD V4 BOOT-002：每个账号的 start() 是非阻塞的，只创建后台调度任务。
        此方法立即返回，调度器在后台独立运行。
        """
        tasks = [acc.start() for acc in self._accounts.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"已启动 {len(self._accounts)} 个账号")

    async def stop_all(self):
        """停止所有账号（含 session 关闭）"""
        tasks = [acc.close() for acc in self._accounts.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("所有账号已停止")

    async def reload_all(self):
        """PRD V3 §3.3：热重载所有账号（凭据 + 运行时服务配置，不重启调度器）

        Web 配置保存后调用：
        - 同步注册表
        - 运行中账号走 reload()（含 VU / web_search / interactions）
        - 已加载但未 running 的实例也刷运行时服务，避免下次 start 用旧快照
        """
        # Generic config PATCH and QR login both mutate the application loader.
        # Refresh the registry/runtime copies before AccountInstance rebuilds its
        # account-scoped loader, otherwise reload() reuses stale credentials.
        self.sync_registry_from_config()
        running = [acc for acc in self._accounts.values() if acc.is_running()]
        idle = [acc for acc in self._accounts.values() if not acc.is_running()]
        if running:
            results = await asyncio.gather(
                *[acc.reload() for acc in running], return_exceptions=True
            )
            success_count = sum(
                1
                for r in results
                if not isinstance(r, Exception)
                and isinstance(r, dict)
                and r.get("reloaded")
            )
            logger.info(f"热重载完成: {success_count}/{len(running)} 个运行中账号成功")
        for acc in idle:
            try:
                # 未运行也刷新 config_loader 与服务快照，保证 start 时是新配置
                acc.account_config_loader = acc._build_account_config_loader()
                if hasattr(acc, "_reload_runtime_services"):
                    acc._reload_runtime_services()
            except Exception as e:
                logger.warning(f"[{acc.account_id}] 空闲账号服务热重载失败: {e}")

    # ══════════════════════════════════════
    #  持久化
    # ══════════════════════════════════════

    def save_to_config(self) -> dict:
        """将配置注册表序列化为 V2 配置字典

        PRD-V5 ACC-501：以配置注册表为事实来源，保留 disabled 和 init-failed 账号。
        不从运行时实例反向重建整个数组。
        """
        accounts_list = []
        for cfg in self._config_registry.list_all():
            item = {
                "id": cfg.get("id", ""),
                "name": cfg.get("name", cfg.get("id", "")),
                "sessdata": cfg.get("sessdata", ""),
                "bili_jct": cfg.get("bili_jct", ""),
                "dede_user_id": cfg.get("dede_user_id", ""),
                "buvid3": cfg.get("buvid3", ""),
                "buvid4": cfg.get("buvid4", ""),
                "refresh_token": cfg.get("refresh_token", ""),
                # PRD V3 §7：profile_id 优先，向后兼容 persona_id
                "profile_id": cfg.get("profile_id", ""),
                "persona_id": cfg.get("persona_id", ""),
                "llm_id": cfg.get("llm_id", ""),
                "enabled": cfg.get("enabled", True),
            }
            accounts_list.append(item)
        return {
            "accounts": accounts_list,
            "default_account": self._default_id,
        }

    # ══════════════════════════════════════
    #  ACC-501：配置注册表辅助方法
    # ══════════════════════════════════════

    @property
    def config_registry(self) -> AccountConfigRegistry:
        """暴露配置注册表（只读访问推荐用下方封装方法）"""
        return self._config_registry

    def has_account(self, acc_id: str) -> bool:
        """检查账号是否存在（含禁用账号）"""
        return self._config_registry.has(acc_id)

    def get_config(self, acc_id: str) -> Optional[dict]:
        """获取任意账号配置（含禁用账号），返回深拷贝"""
        return self._config_registry.get(acc_id)

    def get_account_status(self, acc_id: str) -> Optional[dict]:
        """获取任意账号状态（含禁用账号，无运行时实例时从配置构造）"""
        from bilibot.app.config_loader import bili_credentials_are_configured

        cfg = self._config_registry.get(acc_id)
        if cfg is None:
            return None
        acc = self._accounts.get(acc_id)
        if acc:
            return acc.get_status()
        enabled = cfg.get("enabled", True)
        return {
            "account_id": acc_id,
            "name": cfg.get("name", acc_id),
            "enabled": enabled,
            "running": False,
            "state": "disabled" if not enabled else "stopped",
            "last_error": "",
            "profile_id": cfg.get("profile_id", ""),
            "persona_id": cfg.get("persona_id", ""),
            "available_personas": [],
            "llm_id": cfg.get("llm_id", ""),
            "uid": cfg.get("dede_user_id", ""),
            "authenticated": bili_credentials_are_configured(
                cfg.get("sessdata"), cfg.get("bili_jct")
            ),
            "has_llm": False,
            "has_bili": False,
        }

    def update_account_config(self, acc_id: str, patch: dict) -> bool:
        """更新账号配置（通过配置注册表，敏感字段保留原值）

        PRD-V5 ACC-501：PATCH 永不删除账号，只更新字段。
        敏感字段值为 __REDACTED__ / None / 空字符串时保留原值。

        Returns:
            True 如果账号存在且已更新
        """
        updated = self._config_registry.update(acc_id, patch)
        if not updated:
            return False
        # 同步运行时实例（如果存在）
        acc = self._accounts.get(acc_id)
        if acc:
            new_cfg = self._config_registry.get(acc_id)
            if new_cfg:
                acc.update_config(new_cfg)
        return True

    def sync_registry_from_config(self):
        """从 config_loader 同步配置注册表（拾取外部写入如 qrlogin）

        PRD-V5 ACC-501：在 PATCH 前调用，确保敏感字段保留的"原值"
        是 config.yaml 中的实际值，而非过期的内存副本。
        """
        raw = self.app_config_loader.get_raw_config()
        self._config_registry.sync_from_raw(raw)
        for acc_id, acc in self._accounts.items():
            refreshed = self._config_registry.get(acc_id)
            if refreshed is not None:
                acc.update_config(refreshed)

    def create_runtime_instance(self, acc_id: str) -> bool:
        """为已存在于配置注册表的账号创建运行时实例

        PRD-V5 ACC-501：UI 重新启用禁用账号后，需要创建运行时实例才能启动。
        仅对 enabled=true 且当前无运行时实例的账号生效。

        Returns:
            True 如果实例已存在或创建成功
        """
        if acc_id in self._accounts:
            return True
        cfg = self._config_registry.get(acc_id)
        if cfg is None:
            return False
        if not cfg.get("enabled", True):
            return False
        try:
            inst = self._create_instance(acc_id, cfg)
            self._accounts[acc_id] = inst
            logger.info(f"已创建运行时实例: {acc_id}")
            return True
        except Exception as e:
            logger.error(f"创建运行时实例失败 {acc_id}: {e}")
            return False

    def __len__(self) -> int:
        """返回配置注册表中的账号总数（含禁用账号）"""
        return len(self._config_registry)

    def __contains__(self, account_id: str) -> bool:
        """检查账号是否存在（含禁用账号）"""
        return self._config_registry.has(account_id)

    def __repr__(self) -> str:
        return f"<AccountManager accounts={len(self._config_registry)} runtime={len(self._accounts)} default={self._default_id!r}>"
