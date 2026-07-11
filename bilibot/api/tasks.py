"""任务触发 API 路由（PRD V4 §7 / PRD-V5 §7.4）

PRD-V5 §7.4 / TASK-501：
- 旧的默认账号任务端点 /api/tasks/* 已废弃 → 返回 410 Gone
- 新端点在 /api/accounts/{id}/tasks/* 下（见 accounts.py）

保留 create_tasks_routes 仅为向后兼容路由注册（返回 410）。
"""
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .responses import ok, fail, fail_not_found, fail_internal

logger = logging.getLogger("bilibot.api.tasks")


def create_tasks_routes(scheduler=None) -> list[Route]:
    """创建任务触发 API 路由

    PRD-V5 §7.4：旧默认账号任务端点返回 410 Gone。
    新端点：POST /api/accounts/{id}/tasks/proactive-video 等（见 accounts.py）。

    Args:
        scheduler: 向后兼容参数（不再使用）
    """

    async def gone_proactive_video(request: Request) -> JSONResponse:
        return fail(
            "GONE",
            "此端点已废弃，请使用 POST /api/accounts/{id}/tasks/proactive-video",
            status_code=410,
        )

    async def gone_dynamic(request: Request) -> JSONResponse:
        return fail(
            "GONE",
            "此端点已废弃，请使用 POST /api/accounts/{id}/tasks/dynamic",
            status_code=410,
        )

    return [
        Route("/api/tasks/proactive-video", gone_proactive_video, methods=["POST"]),
        Route("/api/tasks/dynamic", gone_dynamic, methods=["POST"]),
    ]
