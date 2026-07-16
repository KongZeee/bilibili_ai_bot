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
        keys = []
        raw_keys = config.get("api_keys")
        if isinstance(raw_keys, list):
            keys.extend([k.strip() for k in raw_keys if isinstance(k, str) and k.strip()])
        primary = config.get("api_key", "") or ""
        if isinstance(primary, str) and primary.strip():
            keys.append(primary.strip())
        # de-dupe
        seen = set()
        ordered = []
        for k in keys:
            if k not in seen:
                seen.add(k)
                ordered.append(k)
        self.api_keys: list = ordered
        self.api_key: str = ordered[0] if ordered else ""
        self.base_url: str = config.get("base_url", "https://apihub.agnes-ai.com/v1")
        self.model: str = config.get("model", "agnes-image-2.1-flash")
        self.default_size: str = config.get("default_size", "1024x768")
        self.timeout: int = int(config.get("timeout", 120))
        try:
            self.rate_limit_cooldown_seconds: float = float(
                config.get("rate_limit_cooldown_seconds", 30)
            )
        except (TypeError, ValueError):
            self.rate_limit_cooldown_seconds = 30.0
        self.rate_limit_cooldown_seconds = max(1.0, min(self.rate_limit_cooldown_seconds, 600.0))
        self._rr = 0
        self._rr_lock = asyncio.Lock()
        # per-key 429 cooldown (monotonic timestamps)
        self._key_cooldown_until: dict = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    def is_available(self) -> bool:
        # PRD 3.14：必须 enabled=True 且配置了 api_key 和 model
        return bool(self.enabled and (self.api_key or self.api_keys) and self.model)

    async def _next_key(self) -> str:
        """Pick next ready key (not cooling). Raises when all keys are hot."""
        import time as _time
        keys = self.api_keys or ([self.api_key] if self.api_key else [])
        if not keys:
            return ""
        now = _time.monotonic()
        async with self._rr_lock:
            ready = [
                k for k in keys
                if float(self._key_cooldown_until.get(k, 0.0) or 0.0) <= now
            ]
            if not ready:
                # Do not force a still-cooling key — that turns 429 into a tight storm.
                soonest = min(
                    float(self._key_cooldown_until.get(k, 0.0) or 0.0) for k in keys
                )
                wait = max(0.0, soonest - now)
                raise RuntimeError(
                    f"文生图所有密钥均在冷却（约 {wait:.0f}s 后可重试）"
                )
            # round-robin among ready keys
            start = self._rr % len(ready)
            self._rr += 1
            return ready[start]

    def _mark_rate_limited(self, key: str) -> None:
        import time as _time
        if not key:
            return
        self._key_cooldown_until[key] = _time.monotonic() + self.rate_limit_cooldown_seconds
        logger.warning(
            "文生图密钥冷却 %.0fs after 429", self.rate_limit_cooldown_seconds
        )

    async def _get_session(self, api_key: str = "") -> aiohttp.ClientSession:
        key = api_key or self.api_key
        # One-shot session per call when multi-key; avoid sticky Authorization header.
        return aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {key}"},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

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

        keys = self.api_keys or ([self.api_key] if self.api_key else [])
        last_err = None
        for _ in range(max(1, min(len(keys), 4))):
            try:
                key = await self._next_key()
            except RuntimeError as exc:
                last_err = exc
                logger.warning("%s", exc)
                break
            if not key:
                break
            session = await self._get_session(key)
            try:
                async with session.post(url, json=body) as resp:
                    if resp.status == 429:
                        text = await resp.text()
                        last_err = f"HTTP 429: {text[:200]}"
                        self._mark_rate_limited(key)
                        logger.warning(f"文生图 429，切换密钥重试: {text[:120]}")
                        continue
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
                last_err = e
                logger.error(f"文生图异常: {e}", exc_info=True)
            finally:
                try:
                    await session.close()
                except Exception:
                    pass
        if last_err:
            logger.error(f"文生图全部密钥失败: {last_err}")
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
