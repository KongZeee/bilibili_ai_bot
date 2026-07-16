"""Companion life layer — per-account persona living state, schedule, dreams, diary, exploration, creative writing.

Inspired by astrbot_plugin_private_companion concepts; rewritten for BiliBot multi-account architecture.
"""

from .service import CompanionLifeService
from .config import CompanionConfig, load_companion_config

__all__ = [
    "CompanionLifeService",
    "CompanionConfig",
    "load_companion_config",
]
