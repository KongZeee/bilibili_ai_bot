"""
tests/conftest.py - 全局测试 fixtures

所有测试共享的 fixture 定义：
- tmp_data_dir: 临时数据目录（自动清理）
- config_loader: ConfigLoader 实例
- persona_store: PersonaStore 实例
- audit_store: AuditStore 实例
- knowledge_memory: KnowledgeBaseMemory 实例（Mock LLM）
- mock_llm: Mock LLM adapter
"""
import os
import shutil
import tempfile
import json
import time
from pathlib import Path

import pytest

# ── 路径 ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data_dir(tmp_path):
    """临时数据目录，测试结束后自动清理"""
    d = tmp_path / "data"
    d.mkdir()
    return str(d)


@pytest.fixture
def config_loader(tmp_data_dir):
    """ConfigLoader 实例（使用空配置）"""
    from bilibot.app.config_loader import ConfigLoader
    return ConfigLoader(config_dict={
        "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
        "llm": {"api_key": "test-key", "base_url": "http://localhost:8000/v1", "model": "test"},
        "web": {"enabled": True, "host": "127.0.0.1", "port": 8080,
                "secret_key": "test-secret", "admin_username": "admin", "admin_password": "test123"},
        "data_dir": tmp_data_dir,
    })


@pytest.fixture
def persona_store(tmp_data_dir):
    """PersonaStore 实例"""
    from bilibot.services.persona_store import PersonaStore
    return PersonaStore(data_dir=tmp_data_dir)


@pytest.fixture
def audit_store(tmp_data_dir):
    """AuditStore 实例"""
    from bilibot.services.audit_store import AuditStore
    return AuditStore(data_dir=tmp_data_dir)


@pytest.fixture
def mock_llm():
    """Mock LLM adapter（不需要真实 API）"""
    class MockLLM:
        client = True

        async def generate(self, prompt, system_prompt=None, max_tokens=1024, **kwargs):
            return "这是模拟的 LLM 回复"

        async def get_embedding(self, text):
            # 返回固定维度向量
            return [0.1] * 1536

    return MockLLM()


@pytest.fixture
def knowledge_memory(tmp_data_dir, mock_llm):
    """KnowledgeBaseMemory 实例（使用 Mock LLM）"""
    from bilibot.knowledge_memory import KnowledgeBaseMemory
    from bilibot.services.persona_store import PersonaStore
    ps = PersonaStore(data_dir=tmp_data_dir)
    return KnowledgeBaseMemory(
        data_dir=tmp_data_dir,
        llm_adapter=mock_llm,
        personality_system=ps,
    )


@pytest.fixture
def personaconfig_loader(tmp_data_dir):
    """ConfigLoader 含有效 LLM 配置（用于测试 LLM 相关路径）"""
    from bilibot.app.config_loader import ConfigLoader
    return ConfigLoader(config_dict={
        "bilibili": {"sessdata": "test", "bili_jct": "test", "dede_user_id": "123"},
        "llm": {"api_key": "sk-test", "base_url": "http://localhost:8000/v1", "model": "test-model"},
        "web": {"enabled": True},
        "data_dir": tmp_data_dir,
    })
