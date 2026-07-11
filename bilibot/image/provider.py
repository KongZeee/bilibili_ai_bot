"""
文生图 Provider — 支持 agnes-ai 及 OpenAI 兼容 images/generations API

API 文档: https://agnes-ai.com/zh-Hans/docs/agnes-image-21-flash
Endpoint: POST {base_url}/images/generations
返回格式: data[0].b64_json 或 data[0].url

使用方式:
    provider = ImageProvider(config_dict)
    if provider.is_available():
        image_bytes = await provider.generate("一只猫坐在窗台上")
        # 或直接保存到文件
        path = await provider.generate_and_save("prompt", "/tmp/img.png")
"""
import asyncio
import base64
import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger("bilibot.image.provider")


class ImageProvider:
    """文生图 Provider（agnes-ai / OpenAI 兼容）"""

    def __init__(self, config: dict):
        self.enabled: bool = config.get("enabled", False)
        self.api_key: str = config.get("api_key", "")
        self.base_url: str = config.get("base_url", "https://apihub.agnes-ai.com/v1")
        self.model: str = config.get("model", "agnes-image-2.1-flash")
        self.default_size: str = config.get("default_size", "1024x768")
        self.timeout: int = int(config.get("timeout", 120))
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    def is_available(self) -> bool:
        # PRD 3.14：必须 enabled=True 且配置了 api_key 和 model
        return bool(self.enabled and self.api_key and self.model)

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                )
            return self._session

    async def generate(self, prompt: str, size: str = "") -> Optional[bytes]:
        """
        生成图片

        Args:
            prompt: 图片描述提示词
            size: 输出尺寸 (如 "1024x768")，留空用默认值

        Returns:
            图片二进制数据 (PNG)，失败返回 None
        """
        if not self.is_available():
            logger.warning("文生图 Provider 未配置 api_key")
            return None

        url = f"{self.base_url.rstrip('/')}/images/generations"
        body = {
            "model": self.model,
            "prompt": prompt,
            "size": size or self.default_size,
            "return_base64": True,
        }

        session = await self._get_session()
        try:
            async with session.post(url, json=body) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"文生图 API 返回 HTTP {resp.status}: {text[:300]}")
                    return None
                result = await resp.json()

            data_list = result.get("data", [])
            if not data_list:
                logger.error(f"文生图 API 返回空 data: {result}")
                return None

            item = data_list[0]

            # 优先 b64_json
            b64 = item.get("b64_json")
            if b64:
                logger.info(f"文生图成功 (b64): prompt={prompt[:50]}...")
                return base64.b64decode(b64)

            # 回退 URL 下载
            img_url = item.get("url")
            if img_url:
                logger.info(f"文生图成功 (url): {img_url}")
                async with session.get(img_url) as img_resp:
                    if img_resp.status == 200:
                        return await img_resp.read()
                    logger.error(f"下载生成的图片失败: HTTP {img_resp.status}")
                    return None

            logger.error("文生图 API 返回无 b64_json 也无 url")
            return None
        except Exception as e:
            logger.error(f"文生图异常: {e}", exc_info=True)
            return None

    async def generate_and_save(self, prompt: str, save_path: str, size: str = "") -> Optional[str]:
        """
        生成图片并保存到文件

        Returns:
            成功返回文件路径，失败返回 None
        """
        image_bytes = await self.generate(prompt, size=size)
        if not image_bytes:
            return None

        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(image_bytes)
        logger.info(f"图片已保存: {save_path} ({len(image_bytes) / 1024:.1f} KB)")
        return save_path

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
