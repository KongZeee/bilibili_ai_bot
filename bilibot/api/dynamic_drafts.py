"""
动态草稿管理 API 路由（PRD-V5 §4.1 DYN-501）

提供：
- GET    /api/accounts/{account_id}/dynamic-drafts               - 分页列出草稿（可按 status 过滤）
- GET    /api/accounts/{account_id}/dynamic-drafts/{draft_id}    - 草稿详情
- PATCH  /api/accounts/{account_id}/dynamic-drafts/{draft_id}    - 编辑草稿（需 expected_revision）
- POST   /api/accounts/{account_id}/dynamic-drafts/{draft_id}/approve  - 审核通过（需 expected_revision）
- POST   /api/accounts/{account_id}/dynamic-drafts/{draft_id}/reject   - 审核拒绝
- POST   /api/accounts/{account_id}/dynamic-drafts/{draft_id}/retry    - 重试发布（approved/retry_wait/failed/result_unknown）
"""
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

from .responses import ok, fail, fail_internal
from .sole_account import _guard_nested_account_id, _inject_sole_path_params

logger = logging.getLogger("bilibot.api.dynamic_drafts")


def _get_draft_store_for_account(account_manager, acc_id: str):
    """获取账号对应的 DynamicDraftStore（与 scheduler 共享同一 DB 文件）"""
    from bilibot.services.dynamic_draft_store import DynamicDraftStore
    acc = account_manager.get_account(acc_id)
    if not acc or not acc.scheduler:
        return None
    # 复用 scheduler 的懒加载 store（同 DB 路径）
    return acc.scheduler.get_draft_store()


def _get_session_hash(request: Request) -> str:
    """从请求 cookie 生成审核人 session 哈希（不存储原始 token）"""
    token = request.cookies.get("token", "")
    if not token:
        return ""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def create_dynamic_drafts_routes(account_manager, config_loader=None):
    """创建动态草稿管理路由"""

    def _resolve_account(acc_id: str):
        acc = account_manager.get_account(acc_id)
        if not acc:
            return None, None, fail("NOT_FOUND", f"账号不存在: {acc_id}", status_code=404)
        if acc.scheduler is None:
            return None, None, fail("SCHEDULER_UNAVAILABLE", "账号调度器未初始化", status_code=503)
        store = acc.scheduler.get_draft_store()
        return acc, store, None

    def _nested_guard(request: Request):
        return _guard_nested_account_id(request, account_manager, param="account_id")

    def _as_flat(handler):
        async def _flat(request: Request) -> JSONResponse:
            _, err = _inject_sole_path_params(
                request, account_manager, id_keys=("account_id",)
            )
            if err is not None:
                return err
            return await handler(request)

        return _flat

    async def list_drafts(request: Request) -> JSONResponse:
        """GET /api/accounts/{account_id}/dynamic-drafts

        Query: status, page, page_size
        """
        try:
            guard = _nested_guard(request)
            if guard is not None:
                return guard
            acc_id = request.path_params.get("account_id")
            acc, store, err = _resolve_account(acc_id)
            if err is not None:
                return err
            status = request.query_params.get("status") or None
            try:
                page = int(request.query_params.get("page", "1"))
            except (ValueError, TypeError):
                page = 1
            page = max(1, page)
            try:
                page_size = int(request.query_params.get("page_size", "20"))
            except (ValueError, TypeError):
                page_size = 20
            page_size = max(1, min(page_size, 100))

            drafts = store.list_by_account(acc_id, status=status, page=page, page_size=page_size)
            total = store.count_by_account(acc_id, status=status)
            return ok({
                "items": [d.to_dict() for d in drafts],
                "total": total,
                "page": page,
                "page_size": page_size,
                "status_filter": status,
            })
        except Exception as e:
            logger.error(f"列出动态草稿失败: {e}", exc_info=True)
            return fail_internal()

    async def get_draft(request: Request) -> JSONResponse:
        """GET /api/accounts/{account_id}/dynamic-drafts/{draft_id}"""
        try:
            guard = _nested_guard(request)
            if guard is not None:
                return guard
            acc_id = request.path_params.get("account_id")
            draft_id = request.path_params.get("draft_id")
            _, store, err = _resolve_account(acc_id)
            if err is not None:
                return err
            draft = store.get(draft_id)
            if draft is None:
                return fail("NOT_FOUND", f"草稿不存在: {draft_id}", status_code=404)
            if draft.account_id != acc_id:
                return fail("DRAFT_ACCOUNT_MISMATCH",
                            "草稿不属于该账号", status_code=403)
            return ok(draft.to_dict())
        except Exception as e:
            logger.error(f"获取动态草稿失败: {e}", exc_info=True)
            return fail_internal()

    async def patch_draft(request: Request) -> JSONResponse:
        """PATCH /api/accounts/{account_id}/dynamic-drafts/{draft_id}

        Body: { expected_revision: int, content?: str, image_refs?: list }
        编辑草稿 → 自增 revision（乐观锁）。可重新安全检查并更新快照。
        """
        try:
            guard = _nested_guard(request)
            if guard is not None:
                return guard
            acc_id = request.path_params.get("account_id")
            draft_id = request.path_params.get("draft_id")
            acc, store, err = _resolve_account(acc_id)
            if err is not None:
                return err
            draft = store.get(draft_id)
            if draft is None:
                return fail("NOT_FOUND", f"草稿不存在: {draft_id}", status_code=404)
            if draft.account_id != acc_id:
                return fail("DRAFT_ACCOUNT_MISMATCH",
                            "草稿不属于该账号", status_code=403)
            if draft.status != "awaiting_review":
                return fail("INVALID_STATE",
                            f"草稿状态 {draft.status} 不可编辑（仅 awaiting_review 可编辑）",
                            status_code=409)
            try:
                body = await request.json()
            except Exception:
                return fail("INVALID_INPUT", "请求体必须是 JSON", status_code=400)
            if not isinstance(body, dict):
                return fail("INVALID_INPUT", "请求体必须是 JSON 对象", status_code=400)
            expected_revision = body.get("expected_revision")
            if expected_revision is None:
                return fail("INVALID_INPUT", "缺少 expected_revision 字段", status_code=400)
            try:
                expected_revision = int(expected_revision)
            except (ValueError, TypeError):
                return fail("INVALID_INPUT", "expected_revision 必须是整数", status_code=400)

            content = body.get("content")
            image_refs = body.get("image_refs")

            # 重新安全检查（如内容有变更且 safety_checker 可用）
            safety_snapshot = None
            if content is not None and content != draft.content:
                checker = getattr(acc.scheduler, "safety_checker", None)
                if checker is None:
                    return fail(
                        "NO_SAFETY_CHECKER",
                        "安全检查器未初始化，拒绝更新草稿内容",
                        status_code=503,
                    )
                try:
                    passed, reason = await checker.check_content(
                        content, scene="dynamic_post",
                        persona_id=draft.persona_id,
                        account_id=acc_id,
                    )
                    safety_snapshot = {
                        "passed": passed,
                        "reason": reason,
                        "checked_at": datetime.now().isoformat(),
                        "edited": True,
                    }
                except Exception as e:
                    logger.error(f"草稿安全检查异常: {e}", exc_info=True)
                    return fail(
                        "SAFETY_CHECK_FAILED",
                        "安全检查异常，拒绝更新草稿内容",
                        status_code=503,
                    )
                if not passed:
                    return fail(
                        "SAFETY_REJECTED",
                        f"内容未通过安全检查: {reason or 'blocked'}",
                        status_code=400,
                        details=safety_snapshot,
                    )

            updated = store.update(
                draft_id,
                content=content,
                image_refs=image_refs,
                expected_revision=expected_revision,
                safety_snapshot=safety_snapshot,
            )
            if not updated:
                # revision 不匹配或状态已变
                fresh = store.get(draft_id)
                cur_rev = fresh.revision if fresh else "?"
                cur_st = fresh.status if fresh else "?"
                return fail(
                    "REVISION_CONFLICT",
                    f"expected_revision={expected_revision} 不匹配（当前 revision={cur_rev}, status={cur_st}）",
                    status_code=409,
                )
            fresh = store.get(draft_id)
            return ok(fresh.to_dict(), "草稿已编辑，revision 已自增")
        except Exception as e:
            logger.error(f"编辑动态草稿失败: {e}", exc_info=True)
            return fail_internal()

    async def approve_draft(request: Request) -> JSONResponse:
        """POST /api/accounts/{account_id}/dynamic-drafts/{draft_id}/approve

        Body: { expected_revision: int }
        审核通过 → 创建独立 publish TaskRun 并异步执行。
        同一 revision 只能通过一次（乐观锁）。
        """
        try:
            guard = _nested_guard(request)
            if guard is not None:
                return guard
            acc_id = request.path_params.get("account_id")
            draft_id = request.path_params.get("draft_id")
            acc, store, err = _resolve_account(acc_id)
            if err is not None:
                return err
            draft = store.get(draft_id)
            if draft is None:
                return fail("NOT_FOUND", f"草稿不存在: {draft_id}", status_code=404)
            if draft.account_id != acc_id:
                return fail("DRAFT_ACCOUNT_MISMATCH",
                            "草稿不属于该账号", status_code=403)
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            expected_revision = body.get("expected_revision")
            if expected_revision is None:
                return fail("INVALID_INPUT", "缺少 expected_revision 字段", status_code=400)
            try:
                expected_revision = int(expected_revision)
            except (ValueError, TypeError):
                return fail("INVALID_INPUT", "expected_revision 必须是整数", status_code=400)

            # 拒绝审核通过未通过安全检查的草稿（编辑后可能 snapshot.passed=False）
            snap = draft.safety_snapshot or {}
            if snap and snap.get("passed") is False:
                return fail(
                    "SAFETY_REJECTED",
                    f"草稿未通过安全检查，无法审核通过: {snap.get('reason') or 'blocked'}",
                    status_code=400,
                    details=snap if isinstance(snap, dict) else {},
                )

            reviewer_hash = _get_session_hash(request)
            approved = store.approve(
                draft_id,
                expected_revision=expected_revision,
                reviewer_session_hash=reviewer_hash,
            )
            if not approved:
                fresh = store.get(draft_id)
                cur_rev = fresh.revision if fresh else "?"
                cur_st = fresh.status if fresh else "?"
                return fail(
                    "REVISION_CONFLICT",
                    f"expected_revision={expected_revision} 不匹配或状态不可审核（当前 revision={cur_rev}, status={cur_st}）",
                    status_code=409,
                )

            # 创建独立 publish TaskRun 并异步触发
            scheduler = acc.scheduler
            publish_task_id = scheduler.create_draft_publish_task(draft_id)
            if not publish_task_id:
                # 回滚 approve（任务创建失败, DYN-601）：approved → awaiting_review
                # reset_to_approved 不匹配 approved 状态会导致草稿永久卡死，
                # 这里改用 reset_to_awaiting_review 让管理员可重新审核。
                store.reset_to_awaiting_review(draft_id)
                return fail_internal("创建发布 TaskRun 失败")
            scheduler.spawn_publish_task(
                publish_task_id, draft_id, tag=f"publish_draft:{draft_id}"
            )
            return ok({
                "draft_id": draft_id,
                "status": "approved",
                "publish_task_id": publish_task_id,
            }, "草稿已审核通过，发布任务已创建", status_code=202)
        except Exception as e:
            logger.error(f"审核通过动态草稿失败: {e}", exc_info=True)
            return fail_internal()

    async def reject_draft(request: Request) -> JSONResponse:
        """POST /api/accounts/{account_id}/dynamic-drafts/{draft_id}/reject

        Body: { note?: string, expected_revision?: int }
        审核拒绝 → 永不发布。
        """
        try:
            guard = _nested_guard(request)
            if guard is not None:
                return guard
            acc_id = request.path_params.get("account_id")
            draft_id = request.path_params.get("draft_id")
            _, store, err = _resolve_account(acc_id)
            if err is not None:
                return err
            draft = store.get(draft_id)
            if draft is None:
                return fail("NOT_FOUND", f"草稿不存在: {draft_id}", status_code=404)
            if draft.account_id != acc_id:
                return fail("DRAFT_ACCOUNT_MISMATCH",
                            "草稿不属于该账号", status_code=403)
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            note = body.get("note")
            expected_revision = body.get("expected_revision")
            if expected_revision is not None:
                try:
                    expected_revision = int(expected_revision)
                except (ValueError, TypeError):
                    expected_revision = None

            reviewer_hash = _get_session_hash(request)
            rejected = store.reject(
                draft_id,
                reviewer_session_hash=reviewer_hash,
                note=note,
                expected_revision=expected_revision,
            )
            if not rejected:
                fresh = store.get(draft_id)
                cur_st = fresh.status if fresh else "?"
                return fail(
                    "INVALID_STATE",
                    f"草稿状态 {cur_st} 不可拒绝（仅 awaiting_review 可拒绝）",
                    status_code=409,
                )
            fresh = store.get(draft_id)
            return ok(fresh.to_dict(), "草稿已拒绝，永不会发布")
        except Exception as e:
            logger.error(f"拒绝动态草稿失败: {e}", exc_info=True)
            return fail_internal()

    async def retry_draft(request: Request) -> JSONResponse:
        """POST /api/accounts/{account_id}/dynamic-drafts/{draft_id}/retry

        重新入队发布（approved/retry_wait/failed/result_unknown 可重试）。
        approved 表示审核通过但 publish task 创建失败（DYN-601），直接重新创建任务。
        """
        try:
            guard = _nested_guard(request)
            if guard is not None:
                return guard
            acc_id = request.path_params.get("account_id")
            draft_id = request.path_params.get("draft_id")
            acc, store, err = _resolve_account(acc_id)
            if err is not None:
                return err
            draft = store.get(draft_id)
            if draft is None:
                return fail("NOT_FOUND", f"草稿不存在: {draft_id}", status_code=404)
            if draft.account_id != acc_id:
                return fail("DRAFT_ACCOUNT_MISMATCH",
                            "草稿不属于该账号", status_code=403)

            # 重试入队逻辑（DYN-601）：
            # - approved: 审核通过但 publish task 创建失败遗留的状态，直接重新创建任务
            # - retry_wait/failed/result_unknown: 先 reset_to_approved 再创建任务
            if draft.status == "approved":
                pass  # 已是 approved，无需 reset
            elif draft.status in ("retry_wait", "failed", "result_unknown"):
                reset = store.reset_to_approved(draft_id)
                if not reset:
                    return fail(
                        "INVALID_STATE",
                        f"草稿状态 {draft.status} 不可重试（仅 approved/retry_wait/failed/result_unknown 可重试）",
                        status_code=409,
                    )
            else:
                return fail(
                    "INVALID_STATE",
                    f"草稿状态 {draft.status} 不可重试（仅 approved/retry_wait/failed/result_unknown 可重试）",
                    status_code=409,
                )
            scheduler = acc.scheduler
            publish_task_id = scheduler.create_draft_publish_task(draft_id)
            if not publish_task_id:
                return fail_internal("创建发布 TaskRun 失败")
            scheduler.spawn_publish_task(
                publish_task_id, draft_id, tag=f"retry_draft:{draft_id}"
            )
            return ok({
                "draft_id": draft_id,
                "status": "approved",
                "publish_task_id": publish_task_id,
            }, "草稿已重新入队发布", status_code=202)
        except Exception as e:
            logger.error(f"重试动态草稿失败: {e}", exc_info=True)
            return fail_internal()

    return [
        # Flat single-account shell
        Route("/api/dynamic-drafts", _as_flat(list_drafts), methods=["GET"]),
        Route("/api/dynamic-drafts/{draft_id}", _as_flat(get_draft), methods=["GET"]),
        Route("/api/dynamic-drafts/{draft_id}", _as_flat(patch_draft), methods=["PATCH"]),
        Route("/api/dynamic-drafts/{draft_id}/approve", _as_flat(approve_draft), methods=["POST"]),
        Route("/api/dynamic-drafts/{draft_id}/reject", _as_flat(reject_draft), methods=["POST"]),
        Route("/api/dynamic-drafts/{draft_id}/retry", _as_flat(retry_draft), methods=["POST"]),
        # Nested (kept; wrong id → 404 via sole guard)
        Route("/api/accounts/{account_id}/dynamic-drafts", list_drafts, methods=["GET"]),
        Route("/api/accounts/{account_id}/dynamic-drafts/{draft_id}", get_draft, methods=["GET"]),
        Route("/api/accounts/{account_id}/dynamic-drafts/{draft_id}", patch_draft, methods=["PATCH"]),
        Route("/api/accounts/{account_id}/dynamic-drafts/{draft_id}/approve", approve_draft, methods=["POST"]),
        Route("/api/accounts/{account_id}/dynamic-drafts/{draft_id}/reject", reject_draft, methods=["POST"]),
        Route("/api/accounts/{account_id}/dynamic-drafts/{draft_id}/retry", retry_draft, methods=["POST"]),
    ]
