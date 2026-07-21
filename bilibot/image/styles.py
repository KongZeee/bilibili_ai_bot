"""Shared style presets for dynamic image generation.

The selected style is stored as a stable key while the provider receives a
concrete English rendering instruction.  Keeping this mapping in one module
prevents the web preview and automatic dynamic path from drifting apart.
"""
from __future__ import annotations

from typing import Any


DEFAULT_IMAGE_STYLE = "cinematic"
CUSTOM_IMAGE_STYLE = "custom"
MAX_CUSTOM_STYLE_CHARS = 300

IMAGE_STYLE_PRESETS: dict[str, dict[str, str]] = {
    "cinematic": {
        "label": "电影质感",
        "description": "电影构图、自然光影和有层次的氛围",
        "prompt": "cinematic composition, atmospheric natural lighting, rich depth and polished color grading",
    },
    "anime": {
        "label": "二次元",
        "description": "日系动画插画、清晰线稿和细腻赛璐璐上色",
        "prompt": "Japanese anime illustration, expressive character design, clean line art, refined cel shading and vibrant colors",
    },
    "photorealistic": {
        "label": "写实摄影",
        "description": "真实材质、自然光线和摄影镜头语言",
        "prompt": "photorealistic photography, realistic materials, natural light, professional lens rendering and lifelike detail",
    },
    "digital_illustration": {
        "label": "数字插画",
        "description": "精致商业插画、柔和笔触和完整画面设计",
        "prompt": "polished digital illustration, elegant brushwork, cohesive color palette and editorial artwork quality",
    },
    "watercolor": {
        "label": "水彩",
        "description": "透明水彩晕染、纸张纹理和柔和留白",
        "prompt": "delicate watercolor painting, translucent pigments, organic paper texture, soft edges and graceful negative space",
    },
    "cyberpunk": {
        "label": "赛博朋克",
        "description": "霓虹夜景、未来科技和高反差光影",
        "prompt": "cyberpunk visual style, neon-lit atmosphere, futuristic technology, dramatic contrast and vivid urban color",
    },
    "pixel_art": {
        "label": "像素艺术",
        "description": "精致像素画、有限色板和复古游戏氛围",
        "prompt": "detailed pixel art, deliberate pixel clusters, limited color palette and polished retro game aesthetic",
    },
    "minimalist": {
        "label": "极简",
        "description": "克制配色、清晰形状和大量留白",
        "prompt": "minimalist visual design, clean geometric forms, restrained color palette and generous negative space",
    },
    CUSTOM_IMAGE_STYLE: {
        "label": "自定义",
        "description": "使用你填写的风格描述",
        "prompt": "",
    },
}


def image_style_options() -> list[dict[str, str]]:
    """Return the public, JSON-safe style catalog in display order."""

    return [
        {
            "value": key,
            "label": value["label"],
            "description": value["description"],
        }
        for key, value in IMAGE_STYLE_PRESETS.items()
    ]


def is_supported_image_style(value: Any) -> bool:
    return str(value or "").strip().lower() in IMAGE_STYLE_PRESETS


def normalize_image_style(value: Any) -> str:
    key = str(value or "").strip().lower()
    return key if key in IMAGE_STYLE_PRESETS else DEFAULT_IMAGE_STYLE


def normalize_custom_style(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:MAX_CUSTOM_STYLE_CHARS]


def resolve_image_style(
    style: Any,
    custom_style: Any = "",
) -> tuple[str, str, str]:
    """Return ``(key, label, provider_instruction)`` for a selection."""

    key = normalize_image_style(style)
    preset = IMAGE_STYLE_PRESETS[key]
    instruction = str(preset["prompt"] or "").strip()
    if key == CUSTOM_IMAGE_STYLE:
        instruction = normalize_custom_style(custom_style)
        if not instruction:
            key = DEFAULT_IMAGE_STYLE
            preset = IMAGE_STYLE_PRESETS[key]
            instruction = preset["prompt"]
    return key, preset["label"], instruction


def apply_image_style(
    prompt: Any,
    *,
    style: Any = DEFAULT_IMAGE_STYLE,
    custom_style: Any = "",
) -> str:
    """Append a deterministic style lock to a provider prompt."""

    base = str(prompt or "").strip()
    _, _, instruction = resolve_image_style(style, custom_style)
    parts = [base.rstrip(" ."), f"Visual style: {instruction}"]
    return ". ".join(part for part in parts if part) + ". High quality, detailed, no text, no watermark."


__all__ = [
    "CUSTOM_IMAGE_STYLE",
    "DEFAULT_IMAGE_STYLE",
    "IMAGE_STYLE_PRESETS",
    "MAX_CUSTOM_STYLE_CHARS",
    "apply_image_style",
    "image_style_options",
    "is_supported_image_style",
    "normalize_custom_style",
    "normalize_image_style",
    "resolve_image_style",
]
