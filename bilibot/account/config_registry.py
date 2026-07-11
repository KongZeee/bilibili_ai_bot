"""AccountConfigRegistry — 加载并管理 config.yaml 中所有账号配置

PRD-V5 §4.2 ACC-501：禁用账号配置零丢失

职责：
- 加载 config.yaml 中 ALL 合法账号（含 enabled=false 和初始化失败账号）
- 与运行时注册表分离：运行时只创建 enabled=true 校验通过的实例
- 敏感字段占位符规则：PATCH 未提交时保留原值
- 删除账号必须显式 DELETE，PATCH 永不删除账号

设计要点：
- 配置注册表是 save_to_config() 的事实来源
- 运行时实例（AccountInstance）仅用于 enabled 账号
- 外部写入（如 qrlogin）通过 sync_from_raw() 同步到注册表
"""
import copy
import logging
import uuid
from typing import Dict, List, Optional, Any

logger = logging.getLogger("bilibot.account")

# 敏感字段：PATCH 时若值为占位符 / None / 空字符串则保留原值
SENSITIVE_FIELDS = {"sessdata", "bili_jct", "buvid3", "refresh_token", "access_token"}

# 占位符：表示"不修改此敏感字段"
REDACTED_PLACEHOLDER = "__REDACTED__"


def is_sensitive_placeholder(value: Any) -> bool:
    """判断值是否为敏感字段占位符（应保留原值）"""
    if value is None:
        return True
    if isinstance(value, str) and (value == REDACTED_PLACEHOLDER or value == ""):
        return True
    return False


class AccountConfigRegistry:
    """账号配置注册表：加载 config.yaml 全部合法账号配置

    与 AccountRuntimeRegistry（AccountManager._accounts）分离：
    - ConfigRegistry：ALL 账号（含 disabled / init-failed），事实来源
    - RuntimeRegistry：仅 enabled=true 校验通过的 AccountInstance
    """

    def __init__(self):
        self._configs: Dict[str, dict] = {}
        self._order: List[str] = []

    def load_from_raw(self, raw_config: dict):
        """从原始配置字典加载所有账号（含 disabled / init-failed）

        Args:
            raw_config: config.yaml 的原始字典（含 accounts 列表）
        """
        self._configs.clear()
        self._order.clear()
        accounts_list = raw_config.get("accounts", []) or []
        for i, acc_config in enumerate(accounts_list):
            if not isinstance(acc_config, dict):
                continue
            acc_id = acc_config.get("id") or f"account_{i}"
            cfg = copy.deepcopy(acc_config)
            cfg.setdefault("id", acc_id)
            self._configs[acc_id] = cfg
            self._order.append(acc_id)
        logger.info(f"配置注册表已加载 {len(self._configs)} 个账号: {self._order}")

    def sync_from_raw(self, raw_config: dict):
        """从原始配置同步（拾取外部写入如 qrlogin）

        - raw 中已有的账号：更新注册表中的配置（保留外部写入的敏感值）
        - raw 中新增的账号：添加到注册表
        - 注册表中已有但 raw 中没有的账号：保留不删除（避免丢失未保存的添加）

        Args:
            raw_config: config.yaml 的原始字典
        """
        accounts_list = raw_config.get("accounts", []) or []
        for i, acc_config in enumerate(accounts_list):
            if not isinstance(acc_config, dict):
                continue
            acc_id = acc_config.get("id") or f"account_{i}"
            cfg = copy.deepcopy(acc_config)
            cfg.setdefault("id", acc_id)
            self._configs[acc_id] = cfg
            if acc_id not in self._order:
                self._order.append(acc_id)

    def get(self, acc_id: str) -> Optional[dict]:
        """获取账号配置（含敏感字段原值），返回深拷贝"""
        cfg = self._configs.get(acc_id)
        return copy.deepcopy(cfg) if cfg is not None else None

    def list_all(self) -> List[dict]:
        """列出所有账号配置（保持配置顺序），返回深拷贝列表"""
        return [copy.deepcopy(self._configs[aid]) for aid in self._order if aid in self._configs]

    def list_ids(self) -> List[str]:
        """列出所有账号 ID（保持配置顺序）"""
        return list(self._order)

    def list_enabled(self) -> List[str]:
        """列出 enabled=true 的账号 ID"""
        return [
            aid for aid in self._order
            if aid in self._configs and self._configs[aid].get("enabled", True)
        ]

    def has(self, acc_id: str) -> bool:
        """检查账号是否存在（含禁用账号）"""
        return acc_id in self._configs

    def add(self, acc_config: dict) -> str:
        """添加账号配置

        Returns:
            新账号 ID

        Raises:
            ValueError: 账号 ID 已存在
        """
        acc_id = acc_config.get("id") or f"account_{uuid.uuid4().hex[:8]}"
        if acc_id in self._configs:
            raise ValueError(f"账号 ID 已存在: {acc_id}")
        cfg = copy.deepcopy(acc_config)
        cfg["id"] = acc_id
        self._configs[acc_id] = cfg
        self._order.append(acc_id)
        logger.info(f"配置注册表已添加账号: {acc_id}")
        return acc_id

    def update(self, acc_id: str, patch: dict) -> bool:
        """更新账号配置（敏感字段占位符保留原值）

        PRD-V5 ACC-501：PATCH 永不删除账号，只更新字段。

        敏感字段保留规则：
        - 值为 __REDACTED__ / None / 空字符串 → 保留原值
        - 值为真实新值 → 更新

        Args:
            acc_id: 账号 ID
            patch: 要更新的字段字典

        Returns:
            True 如果账号存在且已更新
        """
        if acc_id not in self._configs:
            return False
        cfg = self._configs[acc_id]
        for key, value in patch.items():
            if key == "id":
                continue  # 不允许通过 PATCH 修改 ID
            if key in SENSITIVE_FIELDS and is_sensitive_placeholder(value):
                logger.debug(f"账号 {acc_id} 字段 {key} 为占位符，保留原值")
                continue
            cfg[key] = value
        return True

    def delete(self, acc_id: str) -> bool:
        """删除账号配置（显式 DELETE 操作）

        PRD-V5 ACC-501：PATCH 永不删除账号，删除必须通过此方法。

        Returns:
            True 如果账号存在且已删除
        """
        if acc_id not in self._configs:
            return False
        del self._configs[acc_id]
        self._order.remove(acc_id)
        logger.info(f"配置注册表已删除账号: {acc_id}")
        return True

    def __len__(self) -> int:
        return len(self._configs)

    def __contains__(self, acc_id: str) -> bool:
        return acc_id in self._configs

    def __repr__(self) -> str:
        return f"<AccountConfigRegistry count={len(self._configs)} ids={self._order}>"
