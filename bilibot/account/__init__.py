"""Account 模块 — 多账号管理（PRD V2）"""
from bilibot.account.instance import AccountInstance
from bilibot.account.manager import AccountManager
from bilibot.account.config_registry import AccountConfigRegistry

__all__ = ["AccountInstance", "AccountManager", "AccountConfigRegistry"]
