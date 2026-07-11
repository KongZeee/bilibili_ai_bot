"""
tests/test_app_startup.py - 真实服务启动测试

PRD V4 §4.2.1 / §7.2 必需测试文件。
覆盖：
- BiliBotApp.start() 真实调用 uvicorn.Server.serve()（monkeypatch fake uvicorn.Server）
- web.enabled=false 时仅运行 Scheduler，不启动 Web
- Web 异常退出时主进程应退出（不假装运行）
- /api/status/public 在 Web 启动后可访问（使用 TestClient）
"""
import asyncio
import sys
import types

import pytest

from bilibot.app.app import BiliBotApp


# ═══════════════════════════════════════════════════════
#  Fake Uvicorn（用于验证 serve() 被调用）
# ═══════════════════════════════════════════════════════

class FakeUvicornServer:
    """模拟 uvicorn.Server，记录 serve() 是否被调用"""
    instances = []

    def __init__(self, config):
        self.config = config
        self.serve_called = False
        self.should_exit = False
        self._exc = None
        FakeUvicornServer.instances.append(self)

    async def serve(self):
        self.serve_called = True
        while not self.should_exit:
            await asyncio.sleep(0.02)
            if self._exc:
                raise self._exc
        if self._exc:
            raise self._exc

    def set_exception(self, exc):
        self._exc = exc
        self.should_exit = True


class FakeConfig:
    def __init__(self, app, host="0.0.0.0", port=8080, log_level="warning", **kwargs):
        self.app = app
        self.host = host
        self.port = port
        self.log_level = log_level


@pytest.fixture
def fake_uvicorn(monkeypatch):
    """注入 fake uvicorn 模块"""
    FakeUvicornServer.instances = []

    fake_module = types.ModuleType("uvicorn")
    fake_module.Config = FakeConfig
    fake_module.Server = FakeUvicornServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_module)
    return fake_module


def _make_config(tmp_data_dir, web_enabled=True, port=18080):
    return {
        "bilibili": {"sessdata": "", "bili_jct": "", "dede_user_id": ""},
        "llm": {"api_key": "", "base_url": "http://localhost:8000/v1", "model": "test"},
        "web": {"enabled": web_enabled, "host": "127.0.0.1", "port": port,
                "secret_key": "test-secret", "admin_username": "admin",
                "admin_password": "test123"},
        "data_dir": tmp_data_dir,
        "reply": {"auto_reply": False},
        "proactive": {"video_count": 0, "dynamic_count": 0},
    }


@pytest.fixture
def patch_scheduler_noop(monkeypatch):
    """让 Scheduler.start() / cleanup() 立即返回，避免进入主循环"""
    async def fake_start(self):
        self.running = True
        return

    def fake_cleanup(self):
        pass

    monkeypatch.setattr("bilibot.scheduler.Scheduler.start", fake_start)
    monkeypatch.setattr("bilibot.scheduler.Scheduler.cleanup", fake_cleanup)


# ═══════════════════════════════════════════════════════
#  测试用例
# ═══════════════════════════════════════════════════════

class TestStartCallsServe:
    """PRD V4 §4.2.1：BiliBotApp.start() 必须真实调用 server.serve()"""

    def test_start_calls_serve(self, tmp_data_dir, fake_uvicorn, patch_scheduler_noop):
        """start() 会调用 uvicorn.Server.serve()，不会只创建 server 不 serve"""
        config = _make_config(tmp_data_dir, web_enabled=True)
        app = BiliBotApp(config, "/tmp/nonexistent.yaml")

        async def run():
            async def stop_soon():
                await asyncio.sleep(0.2)
                for inst in FakeUvicornServer.instances:
                    inst.should_exit = True
            await asyncio.gather(app.start(), stop_soon())

        asyncio.run(asyncio.wait_for(run(), timeout=5.0))

        # 核心断言：至少一个 server 实例的 serve() 被调用
        assert any(inst.serve_called for inst in FakeUvicornServer.instances), \
            "BiliBotApp.start() 未调用 server.serve()"

    def test_start_creates_server_task(self, tmp_data_dir, fake_uvicorn, patch_scheduler_noop):
        """start() 创建 server_task"""
        config = _make_config(tmp_data_dir, web_enabled=True)
        app = BiliBotApp(config, "/tmp/nonexistent.yaml")

        async def run():
            async def stop_soon():
                await asyncio.sleep(0.1)
                for inst in FakeUvicornServer.instances:
                    inst.should_exit = True
            await asyncio.gather(app.start(), stop_soon())

        asyncio.run(asyncio.wait_for(run(), timeout=3.0))

        assert len(FakeUvicornServer.instances) >= 1


class TestWebDisabled:
    """PRD V4 §4.1.2：web.enabled=false 时不启动 Web"""

    def test_web_disabled_does_not_create_server(
        self, tmp_data_dir, fake_uvicorn, monkeypatch
    ):
        """web.enabled=False 时不创建 uvicorn.Server"""
        # 让 scheduler 立即返回
        async def fake_start(self):
            self.running = True
            return

        monkeypatch.setattr("bilibot.scheduler.Scheduler.start", fake_start)
        monkeypatch.setattr("bilibot.scheduler.Scheduler.cleanup", lambda self: None)

        # 拦截 _install_signal_handlers 拿到 stop_event
        captured = {}

        def fake_install(self, stop_event):
            captured["stop_event"] = stop_event

        monkeypatch.setattr(
            "bilibot.app.app.BiliBotApp._install_signal_handlers", fake_install
        )

        config = _make_config(tmp_data_dir, web_enabled=False)
        app = BiliBotApp(config, "/tmp/nonexistent.yaml")

        async def run():
            async def stop_soon():
                # 等待 stop_event 被注册
                for _ in range(50):
                    if "stop_event" in captured:
                        break
                    await asyncio.sleep(0.02)
                se = captured.get("stop_event")
                if se is not None:
                    se.set()

            await asyncio.gather(app.start(), stop_soon())

        asyncio.run(asyncio.wait_for(run(), timeout=3.0))

        assert len(FakeUvicornServer.instances) == 0, \
            "web.enabled=False 时不应创建 uvicorn.Server"


class TestWebExceptionPropagates:
    """PRD V4 §4.1.1 / §5.1：Web 异常退出时主进程应退出"""

    def test_web_exception_raises(self, tmp_data_dir, fake_uvicorn, patch_scheduler_noop):
        config = _make_config(tmp_data_dir, web_enabled=True)
        app = BiliBotApp(config, "/tmp/nonexistent.yaml")

        web_exc = RuntimeError("模拟 Web 启动失败")

        async def run():
            async def trigger_exc():
                await asyncio.sleep(0.2)
                for inst in FakeUvicornServer.instances:
                    inst.set_exception(web_exc)

            await asyncio.gather(app.start(), trigger_exc())

        # 应该 propagate 异常
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(asyncio.wait_for(run(), timeout=3.0))
        assert exc_info.value is web_exc


class TestPublicStatusAccessible:
    """PRD V4 §4.1.2：/api/status/public 在 Web 启动后可访问"""

    def test_public_status_no_auth_required(self, tmp_data_dir, persona_store):
        """通过 TestClient 验证 /api/status/public 无需登录"""
        from starlette.testclient import TestClient
        from bilibot.web.panel import create_web_app
        from bilibot.app.config_loader import ConfigLoader
        from bilibot.prompts.orchestrator import PromptOrchestrator
        from bilibot.services.audit_store import AuditStore

        config = _make_config(tmp_data_dir, web_enabled=True)
        cl = ConfigLoader(config)
        orch = PromptOrchestrator(persona_store)
        audit = AuditStore(data_dir=tmp_data_dir)

        web_app = create_web_app(
            config_loader=cl,
            persona_store=persona_store,
            orchestrator=orch,
            scheduler=None,
            config_path="/tmp/nonexistent.yaml",
            audit_store=audit,
        )
        client = TestClient(web_app)
        resp = client.get("/api/status/public")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
