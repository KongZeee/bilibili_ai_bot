"""回复审计 API 路由（PRD V4 §7）

提供 Bot 评论回复记录的查询：
- GET  /api/replies                    列出回复记录（分页 + status 筛选）
- GET  /api/replies/{reply_id}/context 查看某条回复使用的完整上下文摘要
- POST /api/replies/{reply_id}/retry   重试发送失败的评论
"""
import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional, Set

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .responses import ok, fail, fail_not_found, fail_internal

logger = logging.getLogger("bilibot.api.replies")

# 进程内重试锁：防止同一审计/同一 bvid 并发连点双发
_retry_lock = threading.Lock()
_retrying_keys: Set[str] = set()


def _parse_target(target_field: Any) -> Dict[str, Any]:
    """解析 target 字段（容忍 JSON 字符串 / dict / None）"""
    if not target_field:
        return {}
    if isinstance(target_field, dict):
        return target_field
    try:
        return json.loads(target_field)
    except Exception:
        return {}


def _has_failure_reason(item: Dict[str, Any]) -> bool:
    """判断审计记录是否带失败原因（target.failure_reason）"""
    target = _parse_target(item.get("target"))
    return bool(target.get("failure_reason"))


def _get_scheduler_for_account(account_manager, account_id: str):
    """从 account_manager 获取指定账号的 scheduler 实例"""
    if account_manager is None:
        return None
    instance = account_manager.get_account(account_id)
    if instance is None:
        return None
    return getattr(instance, "scheduler", None)


def _find_account_id_for_audit(audit_record: Dict[str, Any], account_manager) -> Optional[str]:
    """根据审计记录推断 account_id

    优先 target.account_id；其次 scheduler.account_id 精确匹配；
    仅当系统只有一个账号时才回退到该账号。
    """
    target = _parse_target(audit_record.get("target"))
    account_id = str(target.get("account_id") or "").strip()
    if account_id:
        return account_id
    if account_manager is None:
        return None
    ids: list[str] = []
    for st in account_manager.list_accounts():
        aid = str(st.get("id") or st.get("account_id") or "").strip()
        if aid:
            ids.append(aid)
    if len(ids) == 1:
        return ids[0]
    # 多账号且 target 未写 account_id：尝试默认账号
    getter = getattr(account_manager, "get_default_id", None)
    default_id = str(getter() if callable(getter) else "").strip()
    if not default_id:
        default_id = str(getattr(account_manager, "_default_id", "") or "").strip()
    if default_id and default_id in ids:
        return default_id
    logger.warning(
        "审计记录缺少 target.account_id，且无法在多账号中唯一推断（ids=%s）",
        ids,
    )
    return None


def create_replies_routes(audit_store, account_manager=None) -> list[Route]:
    """创建回复相关 API 路由

    Args:
        audit_store: AuditStore 实例（与生成流程共享）
        account_manager: AccountManager 实例（用于重试时获取 scheduler）
    """

    async def list_replies(request: Request) -> JSONResponse:
        try:
            try:
                page = max(1, int(request.query_params.get("page", 1)))
                page_size = max(1, min(int(request.query_params.get("page_size", 20)), 100))
            except (ValueError, TypeError):
                return fail("INVALID_INPUT", "page/page_size 必须是正整数", status_code=400)
            status = request.query_params.get("status", "")
            keyword = (request.query_params.get("keyword") or "").strip()

            # 评论页同时展示：回复评论 + 主动看视频后发的评论
            result = await asyncio.to_thread(
                audit_store.list_by_status,
                scene=("reply_comment", "proactive_comment"),
                status=status,
                page=page,
                page_size=page_size,
                keyword=keyword,
            )

            return ok({
                "items": result["items"],
                "total": result["total"],
                "page": result["page"],
                "page_size": result["page_size"],
            })
        except Exception as e:
            logger.exception("list_replies 失败")
            return fail_internal()

    async def get_reply_context(request: Request) -> JSONResponse:
        try:
            reply_id = request.path_params.get("reply_id")
            item = await asyncio.to_thread(audit_store.get, reply_id)
            if not item:
                return fail_not_found("回复不存在")
            return ok({
                "reply": item,
                "context_summary": item.get("context_summary", ""),
                "prompt_preview": item.get("prompt_preview", ""),
            })
        except Exception as e:
            logger.exception("get_reply_context 失败")
            return fail_internal()

    async def retry_reply(request: Request) -> JSONResponse:
        """重试发送失败的评论

        支持两种场景：
        - proactive_comment：直接 post_comment，成功后只更新原审计
        - reply_comment：mark_manual_retry（不耗 attempts），由 scheduler 异步处理

        Query/body:
        - force=1：允许对 result_unknown 强制再发（默认禁止，防平台侧双发）
        """
        lock_key: Optional[str] = None
        try:
            reply_id = request.path_params.get("reply_id")
            record = await asyncio.to_thread(audit_store.get, reply_id)
            if not record:
                return fail_not_found("评论记录不存在")

            scene = record.get("scene", "")
            target = _parse_target(record.get("target"))
            output = record.get("output", "")

            # force：允许 result_unknown 再发
            force = False
            try:
                body = await request.json()
                if isinstance(body, dict) and body.get("force"):
                    force = True
            except Exception:
                pass
            if not force:
                qf = str(request.query_params.get("force") or "").strip().lower()
                force = qf in ("1", "true", "yes")

            if not output:
                return fail("NO_CONTENT", "评论内容为空，无法重试", status_code=400)

            if record.get("published") or record.get("status") == "published":
                return ok({"message": "该记录已是发布成功状态", "comment": output})

            # result_unknown：平台可能已发出，默认禁止再发
            if record.get("status") == "result_unknown" and not force:
                return fail(
                    "RESULT_UNKNOWN_NEEDS_CONFIRM",
                    "结果未知：平台可能已发出。请先到 B 站确认；确认未发出后使用 force=1 强制重试",
                    status_code=409,
                )

            account_id = _find_account_id_for_audit(record, account_manager)
            if not account_id:
                return fail("NO_ACCOUNT", "无法确定账号，无法重试", status_code=400)

            scheduler = _get_scheduler_for_account(account_manager, account_id)
            if scheduler is None:
                return fail("NO_SCHEDULER", "账号调度器未就绪，无法重试", status_code=503)

            if scene == "proactive_comment":
                # P0-B/C：禁止 API 直发。只把动作标为 retry_wait 立即可调度，
                # 发布统一走 scheduler，避免与 list_pending_retry 并发双发。
                bvid = str(target.get("bvid") or "").strip()
                oid = target.get("oid", 0)
                acc_key = account_id or "_default"

                if not bvid:
                    return fail("MISSING_TARGET", "缺少 bvid，无法重试", status_code=400)

                lock_key = f"proactive:{acc_key}:{bvid}"
                with _retry_lock:
                    if lock_key in _retrying_keys:
                        return fail(
                            "ALREADY_RETRYING",
                            "该评论正在重试中，请稍候",
                            status_code=409,
                        )
                    _retrying_keys.add(lock_key)

                store = getattr(scheduler, "proactive_comment_store", None)
                if store is None:
                    return fail(
                        "NO_PROACTIVE_STORE",
                        "主动评论状态存储未就绪",
                        status_code=503,
                    )

                if store.has_published(acc_key, bvid):
                    audit_store.mark_published(
                        reply_id,
                        published=True,
                        status="published",
                        target={"failure_reason": "", "account_id": acc_key},
                    )
                    return ok({
                        "message": "该视频评论已发布过，已更新本条记录状态",
                        "comment": output,
                    })

                ok_sched, code, action_id = store.schedule_manual_retry(
                    acc_key,
                    bvid,
                    force=force,
                    generation_text=output,
                    persona_id=str(record.get("persona_id") or ""),
                )
                if code == "already_published":
                    audit_store.mark_published(
                        reply_id,
                        published=True,
                        status="published",
                        target={"failure_reason": "", "account_id": acc_key},
                    )
                    return ok({
                        "message": "该视频评论已发布过，已更新本条记录状态",
                        "comment": output,
                    })
                if code == "needs_force":
                    return fail(
                        "RESULT_UNKNOWN_NEEDS_CONFIRM",
                        "结果未知：平台可能已发出。请先到 B 站确认；确认未发出后使用 force=1 强制重试",
                        status_code=409,
                    )
                if code == "in_progress":
                    return fail(
                        "IN_PROGRESS",
                        "该主动评论正在发布中，请稍候",
                        status_code=409,
                    )
                if code == "not_found":
                    return fail(
                        "NO_ACTION",
                        "未找到对应的主动评论动作记录，无法调度重试",
                        status_code=404,
                    )
                if not ok_sched:
                    return fail(
                        "SCHEDULE_FAILED",
                        f"无法调度重试（{code}）",
                        status_code=409,
                    )

                try:
                    audit_store.set_status(reply_id, "retry_wait")
                except Exception:
                    pass
                return ok({
                    "message": "已标记为立即重试，将在下一轮调度中处理",
                    "action_id": action_id,
                    "bvid": bvid,
                    "oid": oid,
                    "comment": output,
                })

            elif scene == "reply_comment":
                # 审计写入用 rpid；兼容 source_rpid / reply_id
                source_rpid = (
                    target.get("rpid")
                    or target.get("source_rpid")
                    or target.get("reply_id")
                    or ""
                )
                source_rpid = str(source_rpid).strip()
                if not source_rpid:
                    return fail(
                        "MISSING_TARGET",
                        "缺少 rpid/source_rpid，无法重试",
                        status_code=400,
                    )

                try:
                    comment_type = int(target.get("comment_type") or target.get("type") or 1)
                except (TypeError, ValueError):
                    comment_type = 1

                lock_key = f"reply:{account_id}:{comment_type}:{source_rpid}"
                with _retry_lock:
                    if lock_key in _retrying_keys:
                        return fail(
                            "ALREADY_RETRYING",
                            "该回复正在重试中，请稍候",
                            status_code=409,
                        )
                    _retrying_keys.add(lock_key)

                if scheduler.reply_state_store is None:
                    return fail("NO_REPLY_STORE", "回复状态存储未就绪", status_code=503)

                # 不消耗 attempts，next_retry_at=now，避免一点就 failed
                scheduler.reply_state_store.mark_manual_retry(
                    comment_type,
                    source_rpid,
                    reason="manual_retry",
                    error_code="MANUAL_RETRY",
                )
                # 同步把审计标为处理中，便于 UI 反馈
                try:
                    audit_store.set_status(reply_id, "retry_wait")
                except Exception:
                    pass
                return ok({"message": "已标记为立即重试，将在下一轮调度中处理"})

            else:
                return fail("UNSUPPORTED_SCENE", f"不支持的场景类型: {scene}", status_code=400)

        except Exception as e:
            logger.exception("retry_reply 失败")
            return fail_internal(f"重试失败: {e}")
        finally:
            if lock_key:
                with _retry_lock:
                    _retrying_keys.discard(lock_key)

    return [
        Route("/api/replies", list_replies, methods=["GET"]),
        Route("/api/replies/{reply_id}/context", get_reply_context, methods=["GET"]),
        Route("/api/replies/{reply_id}/retry", retry_reply, methods=["POST"]),
    ]
