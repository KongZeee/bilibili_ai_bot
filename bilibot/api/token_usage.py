"""Token usage statistics API."""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger("bilibot.api.token_usage")

# Canonical scene → 中文（与前端 AUDIT_SCENE_LABELS 对齐，后端 summary 可附带）
SCENE_LABELS_ZH: dict[str, str] = {
    "reply_comment": "评论回复",
    "proactive_comment": "主动评论",
    "proactive_video": "主动视频",
    "private_message": "私信",
    "private_reply": "私信",
    "dynamic_post": "动态发布",
    "dynamic": "动态发布",
    "weekly_summary": "周总结",
    "memory_brain": "记忆大脑",
    "memory_embedding": "记忆向量",
    "memory_extract": "记忆抽取",
    "memory_admin": "记忆管理",
    "image_generation": "文生图",
    "image_prompt": "配图提示词",
    "video_analysis": "视频分析",
    "video_understanding": "视频理解",
    "video_vision": "视频视觉",
    "video_qa": "视频问答",
    "diary": "日记",
    "dream": "梦境",
    "life_plan": "日程",
    "exploration": "探索",
    "creative": "创作",
    "companion": "陪伴生活",
    "companion_exploration": "陪伴探索",
    "companion_diary": "陪伴日记",
    "companion_dream": "陪伴梦境",
    "companion_creative": "陪伴创作",
    "companion_plan": "陪伴日程",
    "bangumi": "追番",
    "bangumi_eval": "番剧评价",
    "bangumi_comment": "番剧评论",
    "persona_test": "人格测试",
    "persona_evaluate": "人格评测",
    "web_search_judge": "搜索判断",
    "web_search_video_judge": "视频搜索判断",
    "memory_list_quick_test": "列表快速召回",
    "memory_debug": "记忆调试",
    "memory_archive_failed": "记忆归档失败暂停",
    "safety_check": "安全检查",
    "tick": "陪伴 tick",
    # 任务/用量扩展（与前端 AUDIT_SCENE_LABELS 对齐）
    "publish_draft": "草稿真发",
    "dynamic_publish": "动态发布",
    "proactive_video_eval": "主动视频评价",
    "asr": "语音识别",
    "vision": "视觉理解",
    "embedding": "向量嵌入",
    "chat": "对话生成",
}


def _annotate_scenes(data: dict[str, Any]) -> dict[str, Any]:
    """Attach zh labels on by_scene / recent rows without breaking old clients."""
    if not isinstance(data, dict):
        return data
    by_scene = data.get("by_scene")
    if isinstance(by_scene, list):
        for row in by_scene:
            if not isinstance(row, dict):
                continue
            scene = str(row.get("scene") or "")
            row.setdefault("scene_label", SCENE_LABELS_ZH.get(scene, scene or "-"))
    recent = data.get("recent")
    if isinstance(recent, list):
        for row in recent:
            if not isinstance(row, dict):
                continue
            scene = str(row.get("scene") or "")
            row.setdefault("scene_label", SCENE_LABELS_ZH.get(scene, scene or "-"))
    data.setdefault("scene_labels", dict(SCENE_LABELS_ZH))
    return data


def create_token_usage_routes(token_store=None, data_dir: str = "./data") -> list:
    def _store():
        if token_store is not None:
            return token_store
        try:
            from bilibot.services.token_usage import get_global_token_store, TokenUsageStore
            s = get_global_token_store()
            if s is not None:
                return s
            # lazy init if app path skipped
            s = TokenUsageStore(data_dir=data_dir)
            from bilibot.services.token_usage import set_global_token_store
            set_global_token_store(s)
            return s
        except Exception as e:
            logger.warning("token store unavailable: %s", e)
            return None

    async def get_summary(request: Request) -> JSONResponse:
        store = _store()
        if store is None:
            return JSONResponse({
                "success": True,
                "data": {
                    "days": 7,
                    "today": {
                        "total_tokens": 0,
                        "calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cached_tokens": 0,
                    },
                    "totals": {
                        "total_tokens": 0,
                        "calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cached_tokens": 0,
                    },
                    "daily": [],
                    "by_kind": [],
                    "by_scene": [],
                    "by_model": [],
                    "by_account": [],
                    "recent": [],
                    "scene_labels": dict(SCENE_LABELS_ZH),
                    "note": "token store not initialized",
                },
            })
        try:
            days = int(request.query_params.get("days") or 7)
        except Exception:
            days = 7
        account_id = (request.query_params.get("account_id") or "").strip()
        try:
            data = store.summary(days=days, account_id=account_id)
            return JSONResponse({"success": True, "data": _annotate_scenes(data)})
        except Exception as e:
            logger.error("token summary failed: %s", e, exc_info=True)
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL", "message": "统计失败", "details": {}},
            }, status_code=500)

    async def get_today(request: Request) -> JSONResponse:
        store = _store()
        if store is None:
            return JSONResponse({"success": True, "data": {"total_tokens": 0, "calls": 0}})
        account_id = (request.query_params.get("account_id") or "").strip()
        try:
            data = store.summary(days=1, account_id=account_id)
            today = data.get("today") or {}
            # 与 summary 一致附带 scene 中文标签元数据，便于前端统一渲染
            if isinstance(today, dict):
                today = dict(today)
                today.setdefault("scene_labels", dict(SCENE_LABELS_ZH))
                if account_id:
                    today.setdefault("account_id", account_id)
            return JSONResponse({"success": True, "data": today})
        except Exception as e:
            logger.error("token today failed: %s", e, exc_info=True)
            return JSONResponse({
                "success": False,
                "error": {"code": "INTERNAL", "message": "统计失败", "details": {}},
            }, status_code=500)

    return [
        Route("/api/token-usage/summary", get_summary, methods=["GET"]),
        Route("/api/token-usage/today", get_today, methods=["GET"]),
    ]
