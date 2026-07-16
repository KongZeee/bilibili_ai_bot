"""BiliBot API 路由汇总"""
from .config import create_config_routes
from .personas import create_personas_routes
from .memory import create_memory_routes
from .audit import create_audit_routes
from .accounts import create_accounts_routes
from .llm_providers import create_llm_providers_routes
from .video_analysis import create_video_analysis_routes
from .image_generation import create_image_generation_routes
from .companion import create_companion_routes

__all__ = [
    "create_config_routes",
    "create_personas_routes",
    "create_memory_routes",
    "create_audit_routes",
    "create_accounts_routes",
    "create_llm_providers_routes",
    "create_video_analysis_routes",
    "create_image_generation_routes",
    "create_companion_routes",
]
