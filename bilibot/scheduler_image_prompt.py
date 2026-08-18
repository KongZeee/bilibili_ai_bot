"""_generate_image_prompt extracted from scheduler.py."""

from __future__ import annotations

import logging
from typing import Optional


logger = logging.getLogger("bilibot.scheduler_image_prompt")


async def generate_image_prompt(
    self,
    content: str,
    persona_id: str = "",
) -> Optional[str]:
    """Generate a dynamic-image prompt with a deterministic persona lock.

    The persona appearance is included twice on purpose: once in the prompt
    writer's instructions and again in the final provider prompt.  The latter
    prevents the prompt-writing LLM from silently dropping the character
    identity before the image model sees it.
    """
    if not self.llm:
        return None
    try:
        from bilibot.image.styles import apply_image_style, resolve_image_style

        try:
            raw_config = self.config_loader.get_raw_config()
        except Exception:
            raw_config = {}
        dynamic_config = raw_config.get("dynamic_publish", {})
        if not isinstance(dynamic_config, dict):
            dynamic_config = {}
        image_style, image_style_label, style_instruction = resolve_image_style(
            dynamic_config.get("image_style", "cinematic"),
            dynamic_config.get("image_style_custom", ""),
        )

        # Resolve the persona used by this dynamic.  A task persona wins over
        # the account fallback so a later persona switch cannot rewrite an
        # already-selected dynamic's visual identity.
        persona = None
        persona_store = getattr(self, "persona_store", None)
        if persona_store is not None:
            resolver = getattr(persona_store, "resolve_persona", None)
            if callable(resolver):
                try:
                    persona = resolver(
                        account_id=getattr(self, "account_id", "") or "",
                        task_persona_id=str(persona_id or "").strip(),
                    )
                except TypeError:
                    try:
                        persona = resolver(
                            getattr(self, "account_id", "") or "",
                            str(persona_id or "").strip(),
                        )
                    except Exception:
                        persona = None
                except Exception:
                    persona = None
            if persona is None:
                try:
                    if getattr(self, "account_id", ""):
                        persona = persona_store.get_persona_for_account(
                            self.account_id
                        )
                except Exception:
                    persona = None
            if persona is None:
                try:
                    persona = persona_store.get_current()
                except Exception:
                    persona = None

        persona_name = str(getattr(persona, "name", "") or "").strip()
        resolved_persona_id = str(getattr(persona, "id", "") or "").strip()
        appearance_desc = str(
            getattr(persona, "appearance", "") or ""
        ).strip()
        if persona_name or resolved_persona_id or appearance_desc:
            persona_label = persona_name or resolved_persona_id or "当前人格"
            persona_block = (
                "画面主角必须固定为以下人格形象，不可替换为其他人物：\n"
                f"人格名称: {persona_label}\n"
                f"人格 ID: {resolved_persona_id or str(persona_id or '').strip()}\n"
                f"主角外貌: {appearance_desc or '保持该人格的既定外观，不要生成泛化人物'}\n\n"
                "如果动态内容与人物无关（如风景/物品），则主角不一定要入镜；"
                "但只要出现人物，就必须是上述同一人格形象，不能换成随机少女或其他角色。\n\n"
            )
            # This suffix is appended after the prompt-writing LLM returns.
            # It is the actual image-provider contract, not merely guidance to
            # another model that may omit details.
            persona_lock = (
                "Character identity lock: if a visible character appears, it "
                f"must be {persona_label} and must match this exact appearance: "
                f"{appearance_desc or 'the active persona appearance'}. "
                "Do not substitute a generic girl, another anime character, "
                "or an unrelated human character."
            )
            logger.info(
                "动态配图人格锁定: name=%s id=%s",
                persona_label,
                resolved_persona_id or str(persona_id or "").strip() or "unknown",
            )
        else:
            persona_block = ""
            persona_lock = ""

        prompt = (
            "根据以下动态内容，生成一个适合配图的英文图片描述 prompt（1-2 句话）。\n"
            "要求：\n"
            "- 只输出 prompt 本身，不要任何解释或前缀\n"
            f"- 用户选择的视觉风格：{image_style_label}（{image_style}）\n"
            f"- 必须体现这些风格特征：{style_instruction}\n"
            "- 保持 high quality, detailed, no text, no watermark\n"
            "- 画面应与动态内容相关但不重复文字\n"
            f"{persona_block}"
            f"\n动态内容: {content}\n\nImage prompt:"
        )
        from bilibot.services.token_usage import usage_context
        with usage_context(scene="image_prompt", account_id=self.account_id or ""):
            result = await self.llm.generate(prompt, max_tokens=800, temperature=0.7)
        if not result:
            return None
        # Provider prompt receives deterministic style and persona locks even
        # if the prompt-writing model ignores or weakens either instruction.
        provider_prompt = str(result).strip()
        if persona_lock:
            provider_prompt = f"{provider_prompt}. {persona_lock}"
        return apply_image_style(
            provider_prompt,
            style=image_style,
            custom_style=dynamic_config.get("image_style_custom", ""),
        )
    except Exception as e:
        logger.warning(f"生成图片 prompt 失败: {e}")
        return None
