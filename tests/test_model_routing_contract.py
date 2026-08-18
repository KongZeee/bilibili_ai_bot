from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from bilibot.api.model_routing import create_model_routing_routes
from bilibot.app.config_loader import ConfigLoader
from bilibot.llm.manager import LLMManager


def _manager(tmp_path):
    loader = ConfigLoader(
        config_dict={
            "chat_providers": [
                {
                    "id": "default",
                    "name": "Default",
                    "api_key": "test-key",
                    "base_url": "https://example.invalid/v1",
                    "model": "agnes-2.0-flash",
                    "enabled": True,
                }
            ],
            "model_routing": {"chat": "default"},
            "allow_llm_fallback": False,
        }
    )
    manager = LLMManager(loader)
    manager.initialize()
    return loader, manager, str(tmp_path / "config.yaml")


def test_llm_manager_keeps_v2_and_v3_provider_signatures(tmp_path) -> None:
    _, manager, _ = _manager(tmp_path)

    legacy_id = manager.add_provider(
        {
            "id": "legacy",
            "name": "Legacy",
            "api_key": "legacy-key",
            "base_url": "https://example.invalid/v1",
            "model": "legacy-model",
        }
    )
    assert manager.update_provider(legacy_id, {"model": "legacy-updated"}) is True
    assert manager.get_provider(legacy_id).model == "legacy-updated"

    assert manager.update_provider(
        "chat", "default", {"model": "agnes-2.5-flash"}
    ) is True
    assert manager.get_provider("default").model == "agnes-2.5-flash"


def test_model_routing_patch_updates_provider_via_llm_manager(tmp_path) -> None:
    loader, manager, config_path = _manager(tmp_path)
    app = Starlette(
        routes=create_model_routing_routes(manager, loader, config_path=config_path)
    )

    response = TestClient(app).patch(
        "/api/model-routing/chat/default",
        json={"model": "agnes-2.5-flash"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert manager.get_provider("default").model == "agnes-2.5-flash"
