"""Dynamic post publishing extracted from scheduler.py."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger("bilibot.scheduler_dynamic_post")


async def do_post_dynamic(self, task_id: Optional[str] = None):
    """发布动态

    PRD V3 §8.4 / §9.3：
    - 主流程使用 orchestrator.build_dynamic_prompt
    - LLM 失败时不得硬编码万能动态自动发布
    - 主写入账号级 V6 memory brain

    PRD-V5 §7 / TASK-501：通过 TaskRunStore 跟踪生命周期。
    平台返回 success 但本地写入失败时不会 mark_result_unknown（动态无回查接口）。
    """
    logger.info("准备发布动态...")
    if not self.bili or not self.llm:
        if task_id:
            self._fail_task(task_id, "NO_BILI_OR_LLM", "bili/llm 未初始化")
        return

    # PRD §5.9：全局暂停或账号风险暂停时跳过（DYN-604：safety_checker None → fail-closed）
    if self.safety_checker is None:
        logger.error("safety_checker 未初始化，拒绝发布动态（DYN-604 fail-closed）")
        if task_id:
            self._fail_task(task_id, "NO_SAFETY_CHECKER", "safety_checker 未初始化", retryable=False)
        return
    if (
        self.safety_checker.is_paused()
        or self.safety_checker.is_account_paused(self.account_id)
    ):
        logger.info("跳过发布动态（暂停状态）")
        if task_id:
            self._fail_task(task_id, "ACCOUNT_PAUSED", "账号暂停状态", retryable=True)
        return

    # PRD-V5 §7：claim → start
    # scheduled（manual / retry）须先 claim；已 claimed 由调度/dispatch 完成
    if task_id:
        from bilibot.services.task_store import STATUS_SCHEDULED, STATUS_CLAIMED
        _task = self.task_store.get(task_id)
        if _task is None:
            logger.warning(f"TaskRun {task_id} 不存在")
            return
        if _task.status == STATUS_SCHEDULED:
            if not self.task_store.claim(task_id):
                logger.warning(f"TaskRun {task_id} claim 失败（可能已被处理）")
                return
        elif _task.status != STATUS_CLAIMED:
            logger.warning(
                f"TaskRun {task_id} 状态不可 start: {_task.status}"
            )
            return
        if not self.task_store.start(task_id):
            logger.warning(f"TaskRun {task_id} start 失败（可能已被处理）")
            return

    try:
        # 1. 收集主题池（视频体验/完整观看/番剧等多源，不再只扫 video_experience）
        dynamic_activity_key = task_id or f"manual_{time.time_ns()}"
        brain = getattr(self, "memory_brain", None)
        related_videos: List[str] = []
        if brain:
            try:
                related_videos = await asyncio.to_thread(
                    self._collect_related_titles_for_dynamic, brain, limit=5
                )
            except Exception:
                related_videos = []

        # PRD V4 DYN-001：主题选择
        # dynamic_publish.topics 非空时按权重或轮换选择主题
        # 最近已使用主题需记录，避免连续重复
        # topics 为空才允许自由发挥，代码不得固定传 topic=None 忽略配置
        # 陪伴层开启时优先用生活种子（日记/念头/探索），更像「此刻想说什么」
        selected_topic: Optional[str] = None
        companion = getattr(self, "companion", None)
        companion_life_block = ""
        if companion is not None and getattr(companion, "enabled", False):
            try:
                if hasattr(companion, "build_proactive_context_block"):
                    companion_life_block = companion.build_proactive_context_block() or ""
            except Exception:
                companion_life_block = ""
        _cfg_loader = getattr(self, "config_loader", None)
        dp_cfg = _cfg_loader.get_raw_config().get("dynamic_publish", {}) if _cfg_loader else {}
        topics_cfg = dp_cfg.get("topics", []) or []
        if companion is not None and getattr(companion, "enabled", False) and hasattr(companion, "pick_dynamic_topic"):
            try:
                selected_topic = companion.pick_dynamic_topic(topics_cfg)
                if selected_topic:
                    logger.info(f"DYN-001 陪伴生活选中动态主题: {selected_topic}")
            except Exception as e:
                logger.debug("companion pick_dynamic_topic failed: %s", e)
                selected_topic = None
        if not selected_topic and topics_cfg:
            try:
                recent_topics: List[str] = []
                if self.ds:
                    recent_topics = self.ds.load_json("recent_dynamic_topics.json", []) or []
                # 过滤掉最近用过的主题（避免连续重复）；若全部用过则允许复用最旧的
                available = [t for t in topics_cfg if t not in recent_topics]
                if not available:
                    available = list(topics_cfg)
                selected_topic = random.choice(available)
                # 记录最近使用主题（保留最近 5 个）
                recent_topics.append(selected_topic)
                self.ds.save_json("recent_dynamic_topics.json", recent_topics[-5:])
                logger.info(f"DYN-001 选中动态主题: {selected_topic}")
            except Exception as e:
                logger.warning(f"主题选择失败: {e}")
                selected_topic = None
        elif selected_topic and self.ds:
            try:
                recent_topics = self.ds.load_json("recent_dynamic_topics.json", []) or []
                recent_topics.append(selected_topic)
                self.ds.save_json("recent_dynamic_topics.json", recent_topics[-5:])
            except Exception:
                pass
        # selected_topic 为 None 表示 topics 为空，允许自由发挥

        # Before generation, persist what the Bot is doing now and attach a
        # guaranteed recent-self lane plus topic-relevant hybrid recall.
        recall_query_text = (
            f"最近看的视频、番剧、心情、日记、想法"
            f"{': ' + selected_topic if selected_topic else ''}"
        )
        if related_videos:
            recall_query_text += " " + " ".join(related_videos[:3])
        activity_context = await self._begin_activity_context(
            action_key=f"dynamic:{dynamic_activity_key}",
            action_type="dynamic_post",
            current_activity=(
                "正在准备一条新动态，先回顾最近做过的事、正在经历的生活和相关记忆，再决定此刻想说什么。"
            ),
            query=recall_query_text,
            scene="dynamic_post",
            # Keep idempotent retries stable even if topic selection changes.
            title="动态发布",
            metadata={"task_id": task_id or ""},
        )
        memory_evidence = str(
            getattr(activity_context, "prompt_text", "") or ""
        ).strip()
        if not memory_evidence and brain:
            try:
                from bilibot.memory_brain import RecallQuery
                recall_result = await brain.recall(
                    RecallQuery(
                        current_message=recall_query_text,
                        account_id=self.account_id,
                        scene="dynamic_post",
                    )
                )
                if recall_result and recall_result.prompt_evidence:
                    memory_evidence = recall_result.prompt_evidence
                    logger.info(
                        "动态发布召回记忆: %s 条事件 sources=memory_brain",
                        len(getattr(recall_result, "events", []) or []),
                    )
            except Exception as recall_exc:
                logger.debug(f"动态发布记忆召回失败: {recall_exc}")

        # companion 生活面单独保留一份，再并入 evidence 供 orchestrator
        memory_evidence_for_prompt = memory_evidence
        if companion_life_block:
            memory_evidence_for_prompt = (
                f"{companion_life_block}\n\n{memory_evidence}".strip()
                if memory_evidence
                else companion_life_block
            )

        # 2. 通过 orchestrator 构建 prompt
        system_prompt = ""
        user_prompt = ""
        persona_id = "unknown"
        persona = None
        if self.persona_store is not None:
            try:
                # PRD V4 ACC-002：使用账号绑定人格，避免多账号取全局人格
                persona = self.persona_store.get_persona_for_account(self.account_id)
                persona_id = persona.id if persona else "unknown"
            except Exception:
                persona = None

        if self.orchestrator is not None:
            try:
                extra_ctx = {
                    "companion_life": companion_life_block,
                    "sources": [
                        s
                        for s, ok in (
                            ("memory_brain", bool(memory_evidence)),
                            ("companion_life", bool(companion_life_block)),
                            ("related_videos", bool(related_videos)),
                        )
                        if ok
                    ],
                }
                if not extra_ctx["companion_life"] and not extra_ctx["sources"]:
                    extra_ctx = None
                prompt_dict = self.orchestrator.build_dynamic_prompt(
                    topic=selected_topic,
                    related_videos=related_videos,
                    persona=persona,
                    memory_evidence=memory_evidence_for_prompt,
                    extra_context=extra_ctx,
                )
                system_prompt = prompt_dict.get("system", "")
                user_prompt = prompt_dict.get("user", "")
            except TypeError:
                # 兼容旧签名
                try:
                    prompt_dict = self.orchestrator.build_dynamic_prompt(
                        topic=selected_topic,
                        related_videos=related_videos,
                        persona=persona,
                        memory_evidence=memory_evidence_for_prompt,
                    )
                    system_prompt = prompt_dict.get("system", "")
                    user_prompt = prompt_dict.get("user", "")
                except Exception as e:
                    logger.warning(f"orchestrator.build_dynamic_prompt 失败: {e}")
            except Exception as e:
                logger.warning(f"orchestrator.build_dynamic_prompt 失败: {e}")

        if not system_prompt or not user_prompt:
            # DynamicPoster 路径：注入记忆/companion，禁止只靠随机默认文案
            poster = getattr(self, "dynamic_poster", None)
            if poster is not None and hasattr(poster, "generate_dynamic"):
                try:
                    content_fallback = await poster.generate_dynamic(
                        topic=selected_topic or "",
                        memory_evidence=memory_evidence,
                        companion_context=companion_life_block,
                        related_videos=related_videos,
                    )
                except TypeError:
                    content_fallback = await poster.generate_dynamic(
                        context=memory_evidence_for_prompt or companion_life_block
                    )
                except Exception as e:
                    logger.warning("DynamicPoster.generate_dynamic 失败: %s", e)
                    content_fallback = ""
                if content_fallback:
                    # 直接作为生成结果走后续安全/发布；system/user 留空标记
                    system_prompt = "__dynamic_poster__"
                    user_prompt = content_fallback
            if not system_prompt or not user_prompt:
                # 仅在 orchestrator/poster 都不可用时回退到 legacy personality
                system_prompt = (
                    self.personality.get_system_prompt()
                    if self.personality
                    else "你是一个真实的B站用户。"
                )
                if selected_topic:
                    user_prompt = (
                        f"现在轮到你发B站动态了，主题是「{selected_topic}」。"
                        "20-80字，口语化，像真人发动态的感觉。"
                    )
                else:
                    user_prompt = (
                        "现在轮到你发B站动态了，想发点什么？20-80字，口语化，像真人发动态的感觉。"
                    )
                # companion / memory 是外部派生内容：只进 user prompt。
                if companion_life_block:
                    user_prompt = f"{user_prompt}\n\n{companion_life_block}"
                if memory_evidence:
                    user_prompt = f"{user_prompt}\n\n{memory_evidence[:1500]}"

        # 3. 调用 LLM（DynamicPoster 已直接产出正文时跳过二次生成）
        if system_prompt == "__dynamic_poster__":
            content = user_prompt
        else:
            from bilibot.services.token_usage import usage_context
            with usage_context(scene="dynamic_post", account_id=self.account_id or ""):
                content = await self.llm.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    max_tokens=1500,
                )

        # LLM 失败时不再硬编码万能动态自动发布（PRD V3 §8.4）
        if not content:
            logger.warning("LLM 生成动态失败，跳过本次发布（不自动发万能动态）")
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_activity_key}",
                    action_type="dynamic_post",
                    text="动态生成失败：LLM 返回空内容",
                    published=False,
                    status="failed",
                    title="动态发布",
                    scene="dynamic_post",
                    metadata={"task_id": task_id or "", "reason_code": "LLM_EMPTY"},
                )
            except Exception as archive_exc:
                logger.warning(
                    "动态空回复的活动意图归档失败: %s", archive_exc
                )
            if task_id:
                self._fail_task(
                    task_id, "LLM_EMPTY", "LLM 生成动态返回空内容", retryable=True,
                )
            return

        content = content.strip().replace("\n\n", "\n")
        # PRD V3 §5.4 (P2-4)：截断不加省略号
        if len(content) > 200:
            content = content[:200]

        # The planned seed is provenance; the published body is canonical.
        # Derive the UI/memory topic from the final text so topic and body
        # cannot drift after generation or safety rewriting.
        first_clause = re.split(r"[\n。！？!?；;]", content, maxsplit=1)[0]
        first_clause = re.sub(r"^[#＃\s]+|[#＃\s]+$", "", first_clause)
        canonical_topic = self._clean_platform_text(
            first_clause or selected_topic or "动态", 60
        )

        dynamic_key = dynamic_activity_key
        # PRD V6：动态发布作为内部任务，不单独记录 intent；
        # 仅在最终结果（成功/失败/拒绝/异常）时归档一次，避免同一动态出现两条记忆。
        dynamic_meta_base = {
            "topic": canonical_topic,
            "planned_topic": selected_topic or "",
            "task_id": task_id or "",
            "memory_grounded": bool(memory_evidence or companion_life_block),
        }

        # 4. 写入 audit（含 persona_id，PRD V3 §8.6）
        audit_id = None
        if self.audit_store:
            try:
                audit_id = await self.audit_store.record_async(
                    scene="dynamic_post",
                    persona_id=persona_id or "unknown",
                    input_summary=user_prompt[:200],
                    context_summary="",
                    prompt_preview=system_prompt[:2000],
                    output=content[:2000],
                    published=False,
                    target={"kind": "dynamic"},
                )
            except Exception:
                audit_id = None

        # PRD-V5 §4.1 DYN-501：动态审核强制生效
        # review_before_publish=true 时，生成内容 + 可选图片（不上传）→ 写入草稿，
        # 不调用任何 B站 publish / image upload API。管理员审核通过后独立发布。
        review_before_publish = bool(dp_cfg.get("review_before_publish", False))
        if review_before_publish:
            await self._handle_dynamic_review_mode(
                task_id=task_id,
                action_key=dynamic_key,
                content=content,
                persona_id=persona_id or "unknown",
                audit_id=audit_id,
                dp_cfg=dp_cfg,
            )
            return

        # PRD §5.9：发布前内容检查 + 频率限制（DYN-604：safety_checker None → fail-closed）
        rate_reserved = False
        if self.safety_checker is None:
            logger.error("safety_checker 未初始化，拒绝发布动态（DYN-604 fail-closed）")
            if audit_id and self.audit_store:
                try:
                    self.audit_store.mark_published(
                        audit_id, published=False,
                        target={"kind": "dynamic"},
                        failure_reason="safety_checker unavailable",
                    )
                except Exception:
                    pass
            if task_id:
                self._fail_task(task_id, "NO_SAFETY_CHECKER", "safety_checker 未初始化", retryable=False)
            await self._archive_bot_action(
                action_key=f"dynamic:{dynamic_key}",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="rejected",
                title=canonical_topic,
                scene="dynamic_post",
                metadata={**dynamic_meta_base, "reason_code": "NO_SAFETY_CHECKER"},
            )
            return
        try:
            passed, reason = await self.safety_checker.check_content(
                content, scene="dynamic_post",
                persona_id=persona_id or "unknown",
                account_id=self.account_id,
            )
        except Exception as e:
            # PRD V4 DYN-003 / §4.2：安全检查异常必须拒绝发布，不得降级放行
            logger.error(f"动态安全检查异常（拒绝发布，DYN-003）: {e}", exc_info=True)
            if audit_id and self.audit_store:
                try:
                    self.audit_store.mark_published(
                        audit_id, published=False,
                        target={"kind": "dynamic"},
                        failure_reason=f"safety_check_exception: {e}",
                    )
                except Exception:
                    pass
            await self._archive_bot_action(
                action_key=f"dynamic:{dynamic_key}",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="rejected",
                title=canonical_topic,
                scene="dynamic_post",
                metadata={**dynamic_meta_base, "reason_code": "SAFETY_CHECK_ERROR"},
            )
            if task_id:
                self._fail_task(
                    task_id, "SAFETY_CHECK_ERROR",
                    f"safety_check_exception: {type(e).__name__}",
                    retryable=True,
                )
            return

        if not passed:
            logger.warning(f"动态内容安全检查未通过: {reason}")
            if audit_id and self.audit_store:
                try:
                    self.audit_store.mark_published(
                        audit_id, published=False,
                        target={"kind": "dynamic"},
                        failure_reason=f"safety_check: {reason}",
                    )
                except Exception:
                    pass
            await self._archive_bot_action(
                action_key=f"dynamic:{dynamic_key}",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="rejected",
                title=canonical_topic,
                scene="dynamic_post",
                metadata={**dynamic_meta_base, "reason_code": "SAFETY_REJECTED"},
            )
            if task_id:
                self._fail_task(
                    task_id, "SAFETY_REJECTED",
                    f"safety_check: {reason}",
                    retryable=False,
                )
            return
        rate_ok, rate_reason = self.safety_checker.check_and_record_rate_limit(
            scene="dynamic_post", account_id=self.account_id,
        )
        if not rate_ok:
            logger.warning("动态发布频率限制触发，跳过本次: %s", rate_reason)
            if audit_id and self.audit_store:
                try:
                    self.audit_store.mark_published(
                        audit_id, published=False, failure_reason="rate_limited",
                    )
                except Exception:
                    pass
            await self._archive_bot_action(
                action_key=f"dynamic:{dynamic_key}",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="deferred",
                title=canonical_topic,
                scene="dynamic_post",
                metadata={**dynamic_meta_base, "reason_code": "RATE_LIMITED"},
            )
            if task_id:
                self._fail_task(
                    task_id, "RATE_LIMITED",
                    f"rate_limited: {rate_reason}",
                    retryable=True,
                )
            return
        rate_reserved = True

        # 4.5 生成配图（如果配置了 with_image 且 image_provider 可用）
        image_list = []
        has_image = False
        _img_provider = getattr(self, 'image_provider', None)
        if _img_provider and _img_provider.is_available():
            try:
                _raw = self.config_loader.get_raw_config()
            except Exception:
                _raw = {}
            dp_config = _raw.get("dynamic_publish", {})
            if dp_config.get("with_image", False):
                try:
                    image_prompt = await self._generate_image_prompt(
                        content,
                        persona_id=persona_id if persona_id != "unknown" else "",
                    )
                    if image_prompt:
                        logger.info(f"正在生成动态配图: {image_prompt[:60]}...")
                        image_bytes = await _img_provider.generate(image_prompt)
                        if image_bytes:
                            img_info = await self.bili.upload_dynamic_image(image_bytes)
                            if img_info:
                                image_list = [img_info]
                                has_image = True
                                logger.info("动态配图生成并上传成功")
                            else:
                                logger.warning("B站图片上传失败，降级为纯文字动态")
                        else:
                            logger.warning("文生图失败，降级为纯文字动态")
                except Exception as e:
                    logger.warning(f"配图生成失败（降级为纯文字动态）: {e}")

        # 5. 发布（根据配置）
        publish_attempted = False
        try:
            success = await self.bili.post_dynamic_text(
                content, images=image_list if image_list else None
            )
            publish_attempted = True
        except Exception as publish_exc:
            publish_attempted = True
            if task_id:
                self._mark_task_result_unknown(
                    task_id, f"dynamic publish exception: {type(publish_exc).__name__}"
                )
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="result_unknown",
                    title=canonical_topic,
                    scene="dynamic_post",
                    metadata={
                        **dynamic_meta_base,
                        "has_image": has_image,
                        "reason_code": type(publish_exc).__name__,
                    },
                )
            except Exception:
                logger.error("unknown dynamic publish result could not be archived")
            return
        if success is None:
            logger.error("动态发布结果不确定（不自动重发）")
            if task_id:
                self._mark_task_result_unknown(
                    task_id, "post_dynamic_text transport uncertainty"
                )
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=False,
                    status="result_unknown",
                    title=canonical_topic,
                    scene="dynamic_post",
                    metadata={**dynamic_meta_base, "reason_code": "RESULT_UNKNOWN"},
                )
            except Exception:
                logger.error("unknown dynamic result could not be archived")
            return

        if success is False:
            self._check_bili_risk_control("dynamic_post")

        # PRD §5.9：发布成功后记录内容（频率已在预占时记录）
        if success and self.safety_checker is not None:
            try:
                self.safety_checker.record_content(content, account_id=self.account_id)
            except Exception:
                pass

        # PRD V4 §4.5.2：发布结果同步到 audit
        if audit_id and self.audit_store:
            try:
                if success:
                    self.audit_store.mark_published(
                        audit_id, published=True,
                        target={
                            "kind": "dynamic",
                            "published_at": datetime.now().isoformat(),
                            "platform": "bilibili",
                        },
                    )
                else:
                    self.audit_store.mark_published(
                        audit_id, published=False,
                        target={"kind": "dynamic"},
                        failure_reason="bili.post_dynamic_text 返回 False",
                    )
            except Exception as e:
                logger.debug(f"audit mark_published 失败: {e}")

        if success:
            try:
                await self._archive_bot_action(
                    action_key=f"dynamic:{dynamic_key}",
                    action_type="dynamic_post",
                    text=content,
                    published=True,
                    title=canonical_topic,
                    scene="dynamic_post",
                    metadata={
                        **dynamic_meta_base,
                        "has_image": has_image,
                    },
                )
            except Exception:
                logger.error("published dynamic result could not be archived")
                if task_id:
                    self._mark_task_result_unknown(
                        task_id, "dynamic published but V6 result archive failed"
                    )
                return

            logger.info(f"动态发布成功: {content[:50]}...")
            self._notify_companion_dynamic_posted(
                content=content or "",
                topic=canonical_topic,
                task_id=task_id or "",
            )
            # PRD-V5 §7：只有真正成功才 succeed
            if task_id:
                self._succeed_task(task_id, {
                    "success": True,
                    "summary": f"动态发布成功: {content[:50]}",
                    "published_at": datetime.now().isoformat(),
                    "platform": "bilibili",
                    "kind": "dynamic",
                })
        else:
            logger.error("动态发布失败")
            if rate_reserved and self.safety_checker is not None:
                try:
                    self.safety_checker.refund_publish(
                        scene="dynamic_post", account_id=self.account_id,
                    )
                except Exception:
                    pass
            await self._archive_bot_action(
                action_key=f"dynamic:{dynamic_key}",
                action_type="dynamic_post",
                text=content,
                published=False,
                status="failed",
                title=canonical_topic,
                scene="dynamic_post",
                metadata={
                    **dynamic_meta_base,
                    "reason_code": "BILI_API_FALSE",
                },
            )
            # PRD-V5 §7：发布失败 → retry_wait/failed
            if task_id:
                self._fail_task(task_id, "DYNAMIC_PUBLISH_FAILED",
                                "bili.post_dynamic_text 返回 False", retryable=True)

    except Exception as e:
        logger.error(f"发布动态失败: {e}", exc_info=True)
        # Close the activity intent opened before generation so a failed
        # LLM/publish path never leaves an open "准备中" intent in memory.
        try:
            activity_key = locals().get("dynamic_activity_key")
            if activity_key:
                await self._archive_bot_action(
                    action_key=f"dynamic:{activity_key}",
                    action_type="dynamic_post",
                    text=f"动态发布异常: {type(e).__name__}",
                    published=False,
                    status=(
                        "result_unknown"
                        if locals().get("publish_attempted")
                        else "failed"
                    ),
                    title="动态发布",
                    scene="dynamic_post",
                    metadata={"task_id": task_id or "", "reason_code": "DYNAMIC_ERROR"},
                )
        except Exception as archive_exc:
            logger.warning("动态异常路径活动意图归档失败: %s", archive_exc)
        # After a platform write attempt, prefer result_unknown over retryable fail
        # to avoid double-post. Pre-publish failures remain retryable.
        if task_id:
            if locals().get("publish_attempted"):
                self._mark_task_result_unknown(
                    task_id, f"DYNAMIC_ERROR_AFTER_PUBLISH: {type(e).__name__}"
                )
            else:
                self._fail_task(task_id, "DYNAMIC_ERROR", str(e), retryable=True)

# ══════════════════════════════════════════
#  PRD-V5 §4.1 DYN-501：动态草稿审核流程
# ══════════════════════════════════════════
