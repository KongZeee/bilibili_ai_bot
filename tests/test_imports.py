"""
tests/test_imports.py - 核心模块导入测试

PRD V3 §5.3 必需测试文件。
验证核心模型和服务可正常导入，且不因可选依赖（如 openai）缺失而崩溃。
"""
import importlib


def test_import_bilibot_package():
    import bilibot
    assert hasattr(bilibot, "__version__")


def test_import_models():
    from bilibot.models import Persona, SceneType, ReplyContext
    assert Persona is not None
    assert SceneType is not None
    assert ReplyContext is not None


def test_import_scene_type_values():
    from bilibot.models import SceneType
    # 必须包含 PRD 要求的核心场景
    assert SceneType.REPLY_COMMENT.value == "reply_comment"
    assert SceneType.DYNAMIC_POST.value == "dynamic_post"
    assert SceneType.WEEKLY_SUMMARY.value == "weekly_summary"
    assert SceneType.PROACTIVE_COMMENT.value == "proactive_comment"


def test_import_persona_store():
    from bilibot.services.persona_store import PersonaStore
    assert PersonaStore is not None


def test_import_prompt_orchestrator():
    from bilibot.prompts.orchestrator import PromptOrchestrator
    assert PromptOrchestrator is not None


def test_import_audit_store():
    from bilibot.services.audit_store import AuditStore
    assert AuditStore is not None


def test_import_context_builder():
    from bilibot.context_builder import ContextBuilder
    assert ContextBuilder is not None


def test_import_llm_adapter_optional_dep_safe():
    """openai 未安装时 LLMAdapter 模块仍可导入（PRD V3 §5.2）"""
    from bilibot.llm_adapter import LLMAdapter
    assert LLMAdapter is not None


def test_import_config_loader():
    from bilibot.app.config_loader import ConfigLoader
    assert ConfigLoader is not None


def test_import_memory_writer():
    from bilibot.services.memory_writer import write_memory_atom
    assert callable(write_memory_atom)


def test_import_web_panel():
    from bilibot.web.panel import create_web_app
    assert callable(create_web_app)


def test_import_api_modules():
    """API 路由模块可导入"""
    importlib.import_module("bilibot.api.config")
    importlib.import_module("bilibot.api.memory")
    importlib.import_module("bilibot.api.audit")


def test_no_astrbot_dependency():
    """PRD V3 §12：bilibot 不得依赖 astrbot"""
    import bilibot
    import sys
    # 不应存在 astrbot 模块被导入
    astrbot_mods = [k for k in sys.modules if k.startswith("astrbot")]
    assert astrbot_mods == [], f"发现 astrbot 依赖: {astrbot_mods}"
