"""
人格 API 路由

提供人格库的完整 CRUD API：
- GET /api/personas - 列出所有人格
- POST /api/personas - 创建新人格
- GET /api/personas/current - 获取当前人格
- GET /api/personas/{id} - 获取指定人格
- PATCH /api/personas/{id} - 更新人格
- DELETE /api/personas/{id} - 删除人格
- POST /api/personas/{id}/activate - 设为当前人格
- POST /api/personas/{id}/copy - 复制人格
- POST /api/personas/import - 导入人格 JSON
- GET /api/personas/{id}/export - 导出人格 JSON
- GET /api/personas/preview - 预览系统提示词
- POST /api/personas/test - 测试人格回复
- GET /api/personas/market - 人格市场（含 github_url 的）
- POST /api/personas/import/github - 从 GitHub URL 导入
- POST /api/personas/{id}/evaluate - 自动评测人格
"""
import json
import logging
import uuid
from starlette.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger("bilibot.api.personas")


def create_personas_routes(persona_store, orchestrator, llm_manager=None):
    """创建人格相关路由"""
    from starlette.routing import Route
    from starlette.responses import JSONResponse

    async def list_personas(request: Request) -> JSONResponse:
        personas = persona_store.list_personas()
        return JSONResponse({"success": True, "data": personas})

    async def create_persona(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            persona = persona_store.create_persona(body)
            return JSONResponse({
                "success": True,
                "message": "人格创建成功",
                "data": persona,
            })
        except Exception as e:
            logger.error(f"创建人格失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "CREATE_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    async def get_current_persona(request: Request) -> JSONResponse:
        current = persona_store.get_current_dict()
        return JSONResponse({"success": True, "data": current})

    async def get_persona(request: Request) -> JSONResponse:
        persona_id = request.path_params.get("id")
        persona = persona_store.get_persona(persona_id)
        if not persona:
            return JSONResponse({
                "success": False,
                "error": {"code": "NOT_FOUND", "message": f"人格 {persona_id} 不存在", "details": {}},
            }, status_code=404)
        return JSONResponse({"success": True, "data": persona})

    async def update_persona(request: Request) -> JSONResponse:
        persona_id = request.path_params.get("id")
        try:
            body = await request.json()
            persona = persona_store.update_persona(persona_id, body)
            if not persona:
                return JSONResponse({
                    "success": False,
                    "error": {"code": "NOT_FOUND", "message": f"人格 {persona_id} 不存在", "details": {}},
                }, status_code=404)
            return JSONResponse({
                "success": True,
                "message": "人格更新成功",
                "data": persona,
            })
        except Exception as e:
            logger.error(f"更新人格失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "UPDATE_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    async def delete_persona(request: Request) -> JSONResponse:
        persona_id = request.path_params.get("id")
        if persona_id == persona_store._current_id:
            return JSONResponse({
                "success": False,
                "error": {
                    "code": "CANNOT_DELETE_CURRENT",
                    "message": "不能删除当前人格，请先切换到其他人格",
                    "details": {},
                },
            }, status_code=400)
        success = persona_store.delete_persona(persona_id)
        if not success:
            return JSONResponse({
                "success": False,
                "error": {"code": "NOT_FOUND", "message": f"人格 {persona_id} 不存在", "details": {}},
            }, status_code=404)
        return JSONResponse({"success": True, "message": "人格已删除"})

    async def activate_persona(request: Request) -> JSONResponse:
        persona_id = request.path_params.get("id")
        success = persona_store.set_current(persona_id)
        if not success:
            return JSONResponse({
                "success": False,
                "error": {"code": "ACTIVATE_FAILED", "message": f"无法激活人格 {persona_id}", "details": {}},
            }, status_code=400)
        persona = persona_store.get_persona(persona_id)
        return JSONResponse({
            "success": True,
            "message": f"已切换到人格: {persona['name']}",
            "data": persona,
        })

    async def copy_persona(request: Request) -> JSONResponse:
        persona_id = request.path_params.get("id")
        body = await request.json() if request.method == "POST" else {}
        new_name = body.get("name")
        persona = persona_store.copy_persona(persona_id, new_name)
        if not persona:
            return JSONResponse({
                "success": False,
                "error": {"code": "NOT_FOUND", "message": f"人格 {persona_id} 不存在", "details": {}},
            }, status_code=404)
        return JSONResponse({"success": True, "message": "人格复制成功", "data": persona})

    async def test_persona(request: Request) -> JSONResponse:
        """测试人格（只返回 prompt，不真实发布）"""
        try:
            body = await request.json()
            test_input = body.get("input", "")
            persona_id = body.get("persona_id")
            use_llm = body.get("use_llm", False)
            llm_provider_id = body.get("llm_provider_id", "")

            if not test_input:
                return JSONResponse({
                    "success": False,
                    "error": {"code": "INVALID_INPUT", "message": "请提供测试输入", "details": {}},
                }, status_code=400)

            persona = persona_store._personas.get(persona_id) if persona_id else None

            # 构建 system prompt
            system_prompt = orchestrator.build_system_prompt(
                scene="reply_comment",
                persona=persona,
            )
            user_prompt = orchestrator.build_user_prompt(
                scene="reply_comment",
                content=test_input,
                persona=persona,
            )

            result = {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "test_input": test_input,
                "persona_id": persona_id or persona_store._current_id,
                "output": "",
            }

            # 可选：真实 LLM 调用
            if use_llm:
                provider = llm_manager.resolve_chat(llm_provider_id) if llm_manager else None
                if provider is None:
                    if not llm_provider_id:
                        # 未指定 Provider 且无默认 LLM
                        return JSONResponse({
                            "success": False,
                            "error": {"code": "LLM_NOT_CONFIGURED", "message": "请先配置 LLM", "details": {}},
                        }, status_code=400)
                    else:
                        # 指定了 Provider 但找不到
                        return JSONResponse({
                            "success": False,
                            "error": {"code": "LLM_NOT_FOUND", "message": "找不到 LLM Provider", "details": {}},
                        }, status_code=400)
                try:
                    output = await provider.generate(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        max_tokens=200,
                    )
                    result["output"] = output or ""
                except Exception as e:
                    result["output"] = f"[LLM调用失败: {e}]"

            return JSONResponse({"success": True, "data": result})
        except Exception as e:
            logger.error(f"测试人格失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "TEST_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    async def preview_prompt(request: Request) -> JSONResponse:
        persona_id = request.query_params.get("persona_id")
        scene = request.query_params.get("scene", "reply_comment")
        preview = persona_store.preview_system_prompt(persona_id, scene)
        current = persona_store.get_current_dict()
        return JSONResponse({
            "success": True,
            "data": {"preview": preview, "current_persona": current},
        })

    async def import_persona(request: Request) -> JSONResponse:
        from .responses import fail, fail_invalid_input
        try:
            try:
                body = await request.json()
            except Exception:
                return fail_invalid_input("请求体不是合法 JSON")
            if isinstance(body, dict) and "persona" in body:
                persona_data = body["persona"]
            elif isinstance(body, dict):
                persona_data = body
            else:
                return fail_invalid_input("请求体必须是 JSON 对象")
            if not isinstance(persona_data, dict):
                return fail_invalid_input("persona 数据必须是 JSON 对象")
            persona = persona_store.import_persona(persona_data)
            return JSONResponse({
                "success": True,
                "message": "人格导入成功",
                "data": persona,
            })
        except Exception as e:
            logger.error(f"导入人格失败: {e}")
            return JSONResponse({
                "success": False,
                "error": {"code": "IMPORT_FAILED", "message": str(e), "details": {}},
            }, status_code=500)

    async def export_persona(request: Request) -> JSONResponse:
        persona_id = request.path_params.get("id")
        persona = persona_store.export_persona(persona_id)
        if not persona:
            return JSONResponse({
                "success": False,
                "error": {"code": "NOT_FOUND", "message": f"人格 {persona_id} 不存在", "details": {}},
            }, status_code=404)
        return JSONResponse({"success": True, "data": persona})

    # ── P3: 人格市场 ──

    async def list_personas_market(request: Request) -> JSONResponse:
        """人格市场：列出所有含 github_url 的人格"""
        from .responses import fail_internal
        try:
            items = persona_store.list_personas()
            market = [p for p in items if p.get("github_url")]
            return JSONResponse({"success": True, "data": market})
        except Exception as e:
            logger.error(f"列出人格市场失败: {e}", exc_info=True)
            return fail_internal()

    async def import_from_github(request: Request) -> JSONResponse:
        """从 GitHub raw URL 导入人格 JSON"""
        from .responses import fail, fail_internal
        try:
            body = await request.json()
            url = body.get("github_url", "")
            if not url:
                return fail("INVALID_INPUT", "缺少 github_url 参数", status_code=400)

            # SSRF 防护：只允许 https + GitHub 域名
            from urllib.parse import urlparse
            import ipaddress
            parsed = urlparse(url)
            if parsed.scheme != "https":
                return fail("INVALID_INPUT", "仅支持 https:// 协议", status_code=400)
            allowed_hosts = ("raw.githubusercontent.com", "github.com", "gist.githubusercontent.com")
            if parsed.hostname not in allowed_hosts:
                return fail("INVALID_INPUT", f"仅支持 GitHub 域名: {allowed_hosts}", status_code=400)
            # 防止通过域名解析到内网 IP（再校验一次解析结果）
            try:
                import socket
                resolved_ips = socket.getaddrinfo(parsed.hostname, None)
                for _, _, _, sockaddr in resolved_ips:
                    ip = ipaddress.ip_address(sockaddr[0])
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        return fail("INVALID_INPUT", "目标地址解析到内网 IP，拒绝请求", status_code=400)
            except (socket.gaierror, ValueError):
                pass

            import urllib.request, json as _json
            req = urllib.request.Request(url, headers={"User-Agent": "BiliBot-Market/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            persona = persona_store.import_persona(data)
            return JSONResponse({"success": True, "message": "imported", "data": persona})
        except Exception as e:
            logger.error(f"从 GitHub 导入人格失败: {e}", exc_info=True)
            return fail_internal()

    # ── P3: 自动人格评测 ──

    async def evaluate_persona(request: Request) -> JSONResponse:
        """自动评测人格：用固定测试集打分"""
        from .responses import fail, fail_internal
        try:
            persona_id = request.path_params.get("id")
            body = await request.json() if request.method == "POST" else {}
            use_llm = body.get("use_llm", False)
            llm_provider_id = body.get("llm_provider_id", "")

            persona = persona_store._personas.get(persona_id) if persona_id else None
            if not persona:
                return fail("NOT_FOUND", f"人格 {persona_id} 不存在", status_code=404)

            # 解析 LLM Provider（仅在 use_llm 时）
            provider = None
            if use_llm:
                provider = llm_manager.resolve_chat(llm_provider_id) if llm_manager else None
                if provider is None:
                    if not llm_provider_id:
                        # 未指定 Provider 且无默认 LLM
                        return fail("LLM_NOT_CONFIGURED", "请先配置 LLM", status_code=400)
                    else:
                        # 指定了 Provider 但找不到
                        return fail("LLM_NOT_FOUND", "找不到 LLM Provider", status_code=400)

            test_cases = [
                ("UP主更新啦！快来看~", "reply_comment"),
                ("这条视频太好看了555", "reply_comment"),
                ("会不会有第三季？", "reply_comment"),
                ("我也想去！", "reply_comment"),
                ("笑死我了哈哈哈哈", "reply_comment"),
            ]

            results = []
            score_acc = 0
            for user_input, scene in test_cases:
                sp = orchestrator.build_system_prompt(scene=scene, persona=persona)
                up = orchestrator.build_user_prompt(scene=scene, content=user_input, persona=persona)
                sc, checks = _score_persona_rules(persona)
                output_text = ""
                llm_err = None
                if use_llm and provider:
                    try:
                        output_text = await provider.generate(
                            prompt=up, system_prompt=sp, max_tokens=120,
                        )
                    except Exception as e:
                        llm_err = str(e)
                        sc = max(0, sc - 10)
                score_acc += sc
                results.append({
                    "input": user_input,
                    "scene": scene,
                    "output": output_text or "",
                    "score": sc,
                    "checks": checks,
                    "llm_error": llm_err,
                })

            n = len(results) or 1
            avg = round(score_acc / n, 1)
            grade = "A" if avg >= 80 else "B" if avg >= 60 else "C" if avg >= 40 else "D"

            return JSONResponse({"success": True, "data": {
                "persona_id": persona_id,
                "persona_name": persona.name,
                "avg_score": avg,
                "grade": grade,
                "results": results,
            }})
        except Exception as e:
            logger.error(f"评估人格失败: {e}", exc_info=True)
            return fail_internal()

    def _score_persona_rules(persona) -> tuple:
        score = 60
        checks = {"base_prompt": False, "boundaries": False, "speaking_style": False}
        if persona.base_prompt and len(persona.base_prompt) > 20:
            score += 10
            checks["base_prompt"] = True
        if persona.boundaries and len(persona.boundaries) > 5:
            score += 10
            checks["boundaries"] = True
        if persona.speaking_style and len(persona.speaking_style) > 5:
            score += 10
            checks["speaking_style"] = True
        if persona.reply_rules:
            score += 5
        if persona.examples:
            score += 5
        return min(score, 100), checks

    return [
        Route("/api/personas", list_personas, methods=["GET"]),
        Route("/api/personas", create_persona, methods=["POST"]),
        Route("/api/personas/current", get_current_persona, methods=["GET"]),
        Route("/api/personas/preview", preview_prompt, methods=["GET"]),
        Route("/api/personas/test", test_persona, methods=["POST"]),
        Route("/api/personas/market", list_personas_market, methods=["GET"]),
        Route("/api/personas/import", import_persona, methods=["POST"]),
        Route("/api/personas/import/github", import_from_github, methods=["POST"]),
        Route("/api/personas/{id}", get_persona, methods=["GET"]),
        Route("/api/personas/{id}", update_persona, methods=["PATCH"]),
        Route("/api/personas/{id}", delete_persona, methods=["DELETE"]),
        Route("/api/personas/{id}/activate", activate_persona, methods=["POST"]),
        Route("/api/personas/{id}/copy", copy_persona, methods=["POST"]),
        Route("/api/personas/{id}/export", export_persona, methods=["GET"]),
        Route("/api/personas/{id}/evaluate", evaluate_persona, methods=["POST"]),
    ]
