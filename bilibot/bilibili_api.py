"""
B站 API 适配器

封装B站所有API调用，包括：
- WBI签名
- 扫码登录
- 用户信息
- 视频信息
- 评论管理
- 私信管理
- 动态管理
- 搜索

"""
import asyncio
import hashlib
import hmac
import json
import os
import random
import re
import time
import base64
import urllib.parse
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime

import aiohttp
# PIL is lazily imported inside functions that need it
from io import BytesIO

logger = logging.getLogger("bilibot.bilibili")


async def _stream_download_to_file(
    session: aiohttp.ClientSession,
    url: str,
    path: str,
    headers: Dict[str, str],
    *,
    timeout: int = 600,
    chunk_size: int = 256 * 1024,
) -> bool:
    """Stream HTTP body to disk without buffering the whole file in memory.

    Returns True on HTTP 200 + complete write; False on non-200.
    Propagates network/IO exceptions to the caller.
    """
    async with session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status != 200:
            logger.warning(f"下载失败: HTTP {resp.status} for {url[:120]}")
            return False

        def _open_out():
            return open(path, "wb")

        out = await asyncio.to_thread(_open_out)
        try:
            async for chunk in resp.content.iter_chunked(chunk_size):
                if not chunk:
                    continue
                await asyncio.to_thread(out.write, chunk)
        finally:
            await asyncio.to_thread(out.close)
        return True

# WBI混键表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

# B站RSA公钥
BILI_RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
AUTH_REQUIRED_CODE = -101
AUTH_BACKOFF_BASE_SECONDS = 300
AUTH_BACKOFF_MAX_SECONDS = 3600


class BilibiliAPI:
    """B站API封装"""
    
    def __init__(self, config):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        # M2：保护 session 创建，避免并发时产生多个 session
        self._session_lock = asyncio.Lock()
        self._wbi_imgs: Optional[Dict[str, str]] = None
        self._wbi_mixkey: Optional[str] = None
        # M4：WBI 混键缓存时间戳，24 小时过期刷新
        self._wbi_mixkey_ts: float = 0
        # WBI 连续获取失败计数 + 退避截止时间（避免 nav API 暂时不可用时反复重试）
        self._wbi_fail_count: int = 0
        self._wbi_backoff_until: float = 0.0
        # 与 session 相同：并发 miss 时只允许一个协程刷新 mixkey
        self._wbi_lock = asyncio.Lock()
        self._csrf_token: str = config.bilibili.bili_jct or ""
        # PRD 4.9：记录最近 API 错误码，scheduler 据此判断风控
        self.last_api_code: int = 0
        # Task 14：保护 last_api_code 写入，避免并发 POST 调用互相覆盖返回码，
        # 导致 scheduler 风控检测（-352）读到另一个调用的返回码。
        self._api_code_lock = asyncio.Lock()
        # Authentication-only polling (notifications/private messages) backs off
        # after Bilibili reports -101. Public APIs remain available.
        self._auth_failure_count: int = 0
        self._auth_backoff_until: float = 0.0
        # 凭据变更回调：refresh_cookie / ensure_buvid 写回 config 后通知 AccountInstance
        self._credential_update_cb = None
        self._cookie_refresh_lock = asyncio.Lock()
        self._last_cookie_check_ts: float = 0.0

    def set_credential_update_callback(self, cb) -> None:
        """Register callback(updates: dict) after SESSDATA/buvid/etc. change."""
        self._credential_update_cb = cb

    def reload_credentials(self, config=None):
        """PRD V3 §3.3：热重载凭据（不重建 session）

        Web 配置保存后调用，避免重启账号即可更新 cookie。
        注意：_get_headers 每次都从 self.config 读取 sessdata/dede_user_id/buvid*，
        所以只需更新 config 引用 + _csrf_token。
        """
        if config is not None:
            self.config = config
        self._csrf_token = self.config.bilibili.bili_jct or ""
        self.last_api_code = 0
        self.clear_auth_backoff()

    def clear_auth_backoff(self) -> None:
        """Allow authenticated polling immediately (used after credential reload)."""
        self._auth_failure_count = 0
        self._auth_backoff_until = 0.0

    def auth_poll_allowed(self, now: Optional[float] = None) -> bool:
        """Return whether an authentication-required poll may contact Bilibili."""
        current = time.monotonic() if now is None else float(now)
        return current >= self._auth_backoff_until

    def auth_backoff_remaining(self, now: Optional[float] = None) -> float:
        current = time.monotonic() if now is None else float(now)
        return max(0.0, self._auth_backoff_until - current)

    def _record_authenticated_response(self, data: Optional[Dict]) -> None:
        """Update authenticated-poll backoff from an auth-required API response.

        Counter updates are guarded by a threading.Lock so concurrent polls
        cannot race failure count / backoff window mutations.
        """
        if not isinstance(data, dict):
            return
        code = data.get("code")
        if not hasattr(self, "_auth_counter_lock"):
            import threading
            self._auth_counter_lock = threading.Lock()
        with self._auth_counter_lock:
            if code == AUTH_REQUIRED_CODE:
                self._auth_failure_count += 1
                delay = min(
                    AUTH_BACKOFF_MAX_SECONDS,
                    AUTH_BACKOFF_BASE_SECONDS
                    * (2 ** min(self._auth_failure_count - 1, 4)),
                )
                self._auth_backoff_until = time.monotonic() + delay
                logger.warning(
                    "Bilibili auth poll paused %d s (consecutive -101 count=%d)",
                    int(delay),
                    self._auth_failure_count,
                )
            elif code == 0:
                if self._auth_failure_count or self._auth_backoff_until:
                    self._auth_failure_count = 0
                    self._auth_backoff_until = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建aiohttp会话"""
        # M2：double-check 模式，避免并发创建多个 session
        if self.session is not None and not self.session.closed:
            return self.session
        async with self._session_lock:
            # 拿到锁后再次检查，防止等待期间已被其他协程创建
            if self.session is not None and not self.session.closed:
                return self.session
            connector = aiohttp.TCPConnector(limit=100, force_close=False)
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                },
                connector=connector,
            )
        return self.session
    
    async def close(self):
        """关闭会话"""
        async with self._session_lock:
            if self.session and not self.session.closed:
                await self.session.close()
            # 清空引用，避免 close 后误用旧 session；_get_session 会按需重建
            self.session = None
    
    def _get_headers(self, extra_cookies: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """获取请求头，包含Cookie"""
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        cookies = f"SESSDATA={self.config.bilibili.sessdata}"
        if self._csrf_token:
            cookies += f"; bili_jct={self._csrf_token}"
        if self.config.bilibili.dede_user_id:
            cookies += f"; DedeUserID={self.config.bilibili.dede_user_id}"
        if self.config.bilibili.buvid3:
            cookies += f"; buvid3={self.config.bilibili.buvid3}"
        buvid4 = getattr(self.config.bilibili, "buvid4", "") or ""
        if buvid4:
            cookies += f"; buvid4={buvid4}"

        if extra_cookies:
            for k, v in extra_cookies.items():
                cookies += f"; {k}={v}"

        headers["Cookie"] = cookies
        return headers
    
    async def _http_get(
        self,
        url: str,
        params: Optional[Dict] = None,
        timeout: int = 10,
        *,
        allow_not_found: bool = False,
    ) -> Tuple[Optional[Dict], Optional[str]]:
        """
        HTTP GET请求
        
        Returns:
            (parsed_json, error_text)
        """
        if url == "https://api.bilibili.com/x/v2/reply/wbi/root":
            logger.warning("????????? /x/v2/reply/wbi/root??? /x/v2/reply")
            url = "https://api.bilibili.com/x/v2/reply"
            if params is not None:
                params = dict(params)
                params.pop("w_rid", None)
                params.pop("wts", None)
                params.setdefault("sort", 0)

        session = await self._get_session()
        try:
            async with session.get(
                url,
                params=params,
                headers=self._get_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                        if data.get("code") != 0:
                            logger.warning(f"API返回错误: {data.get('message', '')} for {url}")
                        return data, None
                    except json.JSONDecodeError:
                        logger.error(f"JSON解析失败: {text[:200]}")
                        return None, text
                else:
                    if resp.status == 404 and allow_not_found:
                        logger.debug(f"可选 API 不存在: HTTP 404 for {url}")
                    else:
                        logger.error(f"HTTP {resp.status} for {url}")
                    return None, f"HTTP {resp.status}"
        except asyncio.TimeoutError:
            logger.error(f"请求超时: {url}")
            return None, "timeout"
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None, str(e)
    
    async def _http_post(self, url: str, data: Optional[Dict] = None, timeout: int = 10) -> Tuple[Optional[Dict], Optional[str]]:
        """HTTP POST请求。

        Returns (json, error). error prefixes:
        - ``timeout:`` / ``network:`` / ``http_5xx:`` / ``empty_body:`` / ``non_json:``
          → transport uncertainty (platform may have accepted the write)
        - ``http_4xx:`` → likely definitive client/request failure
        - other → treat as uncertain when used by publish helpers
        """
        session = await self._get_session()
        try:
            async with session.post(
                url,
                data=data,
                headers=self._get_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if not (text or "").strip():
                        return None, "empty_body: empty HTTP 200 body"
                    try:
                        resp_data = json.loads(text)
                        # MISC-601：所有 POST API 调用统一记录返回码，供风控检测（-352）使用
                        # Task 14：加锁保护写入，避免并发 POST 互相覆盖返回码
                        async with self._api_code_lock:
                            self.last_api_code = (resp_data or {}).get("code", -1)
                        return resp_data, None
                    except json.JSONDecodeError:
                        return None, f"non_json: {text[:200]}"
                if 500 <= resp.status <= 599:
                    return None, f"http_5xx: HTTP {resp.status}"
                if 400 <= resp.status <= 499:
                    return None, f"http_4xx: HTTP {resp.status}"
                return None, f"http: HTTP {resp.status}"
        except asyncio.TimeoutError:
            logger.error(f"POST请求超时: {url}")
            return None, "timeout: request timed out"
        except aiohttp.ClientError as e:
            logger.error(f"POST网络错误: {e}")
            return None, f"network: {type(e).__name__}: {e}"
        except Exception as e:
            logger.error(f"POST请求失败: {e}")
            return None, f"network: {type(e).__name__}: {e}"
    
    # ══════════════════════════════════════
    #  WBI 签名
    # ══════════════════════════════════════
    
    async def _get_wbi_mixkey(self) -> Optional[str]:
        """获取WBI混键（double-checked lock + 指数退避，避免并发 stampede）"""
        # M4：缓存 24 小时内有效，过期则刷新
        if self._wbi_mixkey and (time.time() - self._wbi_mixkey_ts < 86400):
            return self._wbi_mixkey

        # 退避期内不重试，直接返回缓存（可能过期但仍比 None 好）
        now = time.time()
        if self._wbi_fail_count > 0 and now < self._wbi_backoff_until:
            return self._wbi_mixkey  # 过期但可用的缓存，或 None

        async with self._wbi_lock:
            # 等待锁期间可能已被其他协程刷新
            if self._wbi_mixkey and (time.time() - self._wbi_mixkey_ts < 86400):
                return self._wbi_mixkey
            if self._wbi_fail_count > 0 and time.time() < self._wbi_backoff_until:
                return self._wbi_mixkey

            session = await self._get_session()
            try:
                async with session.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers=self._get_headers(),
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # B站 nav API 字段为 wbi_img（单数），非 wbi_imgs
                        imgs = data.get("data", {}).get("wbi_img", {}) or data.get("data", {}).get("wbi_imgs", {})
                        img_url = imgs.get("img_url", "")
                        sub_url = imgs.get("sub_url", "")

                        if not img_url or not sub_url:
                            logger.warning("nav API 未返回 wbi_img，WBI 签名不可用")
                            self._record_wbi_failure()
                            return self._wbi_mixkey

                        # 从 URL 中提取文件名（去掉扩展名）
                        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                        mix_key = img_key + sub_key
                        self._wbi_imgs = imgs
                        self._wbi_mixkey = self._encrypt_mixkey(mix_key)
                        # M4：更新缓存时间戳
                        self._wbi_mixkey_ts = time.time()
                        # 成功：重置退避状态
                        self._wbi_fail_count = 0
                        self._wbi_backoff_until = 0.0
                        return self._wbi_mixkey
            except Exception as e:
                logger.error(f"获取WBI混键失败: {e}")
            self._record_wbi_failure()
            return self._wbi_mixkey

    def _record_wbi_failure(self) -> None:
        """记录 WBI 获取失败，指数退避（30s → 60s → 120s → … 最大 1h）"""
        self._wbi_fail_count += 1
        delay = min(30 * (2 ** (self._wbi_fail_count - 1)), 3600)
        self._wbi_backoff_until = time.time() + delay
        logger.warning(
            "WBI 混键获取失败，退避 %d 秒（连续失败 %d 次）",
            delay, self._wbi_fail_count,
        )
    
    def _encrypt_mixkey(self, mix_key: str) -> str:
        """加密混键"""
        salt = ""
        qu = MIXIN_KEY_ENC_TAB
        for i in range(52):
            if i < len(mix_key):
                salt += mix_key[qu[i]]
        return salt[:32]
    
    async def sign_wbi(self, params: Dict) -> Dict:
        """
        WBI签名
        
        Args:
            params: 原始参数字典
            
        Returns:
            带签名的参数字典

        Raises:
            RuntimeError: WBI 混键不可用（nav API 连续失败、退避中）
        """
        mix_key = await self._get_wbi_mixkey()
        if not mix_key:
            raise RuntimeError(
                f"WBI mix_key 不可用（连续失败 {self._wbi_fail_count} 次），"
                f"无法签名请求。退避至 {self._wbi_backoff_until:.0f}"
            )

        # 添加timestamp（wts 必须参与签名计算）
        params = dict(params)  # 拷贝避免修改入参
        params["wts"] = int(time.time())

        # 按键排序（含 wts）
        params = dict(sorted(params.items()))

        # 构造签名字符串（wts 在内）
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        w_rid = hashlib.md5((query + mix_key).encode()).hexdigest()
        params["w_rid"] = w_rid

        return params
    
    # ══════════════════════════════════════
    #  buvid 设备指纹 + Cookie 自动刷新
    #  （对齐 astrbot_plugin_bilibili_ai_bot）
    # ══════════════════════════════════════

    def _apply_local_credential_updates(self, updates: Dict[str, str]) -> None:
        """Write credential fields into in-memory config (bilibili section + BiliConfig)."""
        if not updates:
            return
        bili = self.config.bilibili
        raw = None
        try:
            raw = self.config.get_raw_config()
        except Exception:
            raw = None
        bili_raw = None
        if isinstance(raw, dict):
            bili_raw = raw.setdefault("bilibili", {})

        field_map = {
            "sessdata": "sessdata",
            "bili_jct": "bili_jct",
            "dede_user_id": "dede_user_id",
            "buvid3": "buvid3",
            "buvid4": "buvid4",
            "refresh_token": "refresh_token",
        }
        for src, attr in field_map.items():
            if src not in updates:
                continue
            val = str(updates[src] or "")
            if not val:
                continue
            if hasattr(bili, attr):
                setattr(bili, attr, val)
            if isinstance(bili_raw, dict):
                bili_raw[attr] = val
            if attr == "bili_jct":
                self._csrf_token = val

    def _notify_credential_update(self, updates: Dict[str, str]) -> None:
        cb = self._credential_update_cb
        if not cb or not updates:
            return
        try:
            cb(dict(updates))
        except Exception as e:
            logger.warning("凭据回调失败: %s", type(e).__name__)

    async def ensure_buvid(self, force: bool = False) -> bool:
        """领取并缓存 buvid3/buvid4（设备指纹，降低风控概率）

        GET https://api.bilibili.com/x/frontend/finger/spi
        → data.b_3 / data.b_4

        已有 buvid3 且非 force 时跳过。失败不阻断主流程。
        """
        if not force and (self.config.bilibili.buvid3 or ""):
            return True
        try:
            data, err = await self._http_get(
                "https://api.bilibili.com/x/frontend/finger/spi",
                timeout=10,
            )
            if not data or data.get("code") != 0:
                logger.warning(
                    "获取 buvid 失败: %s",
                    (data or {}).get("message") or err or "unknown",
                )
                return False
            payload = data.get("data") or {}
            buvid3 = str(payload.get("b_3") or "").strip()
            buvid4 = str(payload.get("b_4") or "").strip()
            if not buvid3 and not buvid4:
                logger.warning("finger/spi 未返回 buvid")
                return False
            updates: Dict[str, str] = {}
            if buvid3:
                updates["buvid3"] = buvid3
            if buvid4:
                updates["buvid4"] = buvid4
            self._apply_local_credential_updates(updates)
            self._notify_credential_update(updates)
            logger.info(
                "已获取设备指纹 buvid3=%s... buvid4=%s",
                (buvid3[:16] + "...") if buvid3 else "-",
                "yes" if buvid4 else "no",
            )
            return True
        except Exception as e:
            logger.warning("获取 buvid 异常（不影响基本功能）: %s", e)
            return False

    async def check_need_cookie_refresh(self) -> Tuple[bool, str]:
        """查询登录 Cookie 是否需要刷新。

        GET passport.../cookie/info?csrf=
        → data.refresh == True 表示需要刷新
        """
        if not self.config.bilibili.is_authenticated:
            return False, "未登录"
        csrf = self._csrf_token or self.config.bilibili.bili_jct or ""
        try:
            data, err = await self._http_get(
                "https://passport.bilibili.com/x/passport-login/web/cookie/info",
                params={"csrf": csrf},
                timeout=10,
            )
            if not data:
                return False, f"检查失败: {err or 'empty'}"
            if data.get("code") != 0:
                return False, f"检查失败: {data.get('message', data.get('code'))}"
            need = bool((data.get("data") or {}).get("refresh", False))
            return (True, "需要刷新") if need else (False, "Cookie 仍然有效")
        except Exception as e:
            return False, f"检查出错: {e}"

    def _generate_correspond_path(self, ts_ms: int) -> str:
        """RSA-OAEP 加密 refresh_{ts} 得到 correspond path（Cookie 刷新协议）"""
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes, serialization

        pk = serialization.load_pem_public_key(BILI_RSA_PUBLIC_KEY.encode())
        return pk.encrypt(
            f"refresh_{ts_ms}".encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        ).hex()

    async def _http_get_text(self, url: str, timeout: int = 10) -> Tuple[str, Optional[str]]:
        """GET 返回纯文本（Cookie 刷新取 refresh_csrf 用）"""
        session = await self._get_session()
        try:
            async with session.get(
                url,
                headers=self._get_headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return text, f"HTTP {resp.status}"
                return text, None
        except Exception as e:
            return "", str(e)

    async def refresh_cookie(self, force: bool = False) -> Tuple[bool, str]:
        """使用 refresh_token 刷新 SESSDATA / bili_jct（官方 Web 协议）

        流程：
        1. cookie/info 判断是否需要刷新（force=True 跳过）
        2. RSA 加密时间戳 → correspond 页提取 refresh_csrf
        3. POST cookie/refresh
        4. POST confirm/refresh（旧 refresh_token）
        5. 写回本地 config + 凭据回调
        """
        rt = (self.config.bilibili.refresh_token or "").strip()
        if not rt:
            return False, "没有 refresh_token（请重新扫码登录）"
        if not self.config.bilibili.sessdata:
            return False, "SESSDATA 为空"
        bjct = self._csrf_token or self.config.bilibili.bili_jct or ""

        async with self._cookie_refresh_lock:
            try:
                if not force:
                    need, msg = await self.check_need_cookie_refresh()
                    if not need:
                        return True, msg

                ts_ms = int(time.time() * 1000)
                cp = self._generate_correspond_path(ts_ms)
                html, herr = await self._http_get_text(
                    f"https://www.bilibili.com/correspond/1/{cp}", timeout=15,
                )
                if herr and not html:
                    return False, f"无法获取 correspond 页: {herr}"
                m = re.search(r'<div\s+id="1-name"\s*>([^<]+)</div>', html or "")
                if not m:
                    return False, "无法提取 refresh_csrf"

                refresh_csrf = m.group(1).strip()
                session = await self._get_session()
                async with session.post(
                    "https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
                    headers=self._get_headers(),
                    data={
                        "csrf": bjct,
                        "refresh_csrf": refresh_csrf,
                        "source": "main_web",
                        "refresh_token": rt,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    try:
                        result = await resp.json(content_type=None)
                    except Exception:
                        body = await resp.text()
                        return False, f"刷新响应非 JSON: {body[:200]}"
                    if not isinstance(result, dict) or result.get("code") != 0:
                        return False, (
                            f"刷新失败: "
                            f"{(result or {}).get('message', (result or {}).get('code', resp.status))}"
                        )
                    updates: Dict[str, str] = {}
                    nrt = (result.get("data") or {}).get("refresh_token") or ""
                    if nrt:
                        updates["refresh_token"] = str(nrt)
                    for k, cookie in resp.cookies.items():
                        name = getattr(cookie, "key", None) or k
                        val = getattr(cookie, "value", None) or str(cookie)
                        if name == "SESSDATA" and val:
                            updates["sessdata"] = val
                        elif name == "bili_jct" and val:
                            updates["bili_jct"] = val
                        elif name == "DedeUserID" and val:
                            updates["dede_user_id"] = val
                        elif name == "buvid3" and val:
                            updates["buvid3"] = val
                        elif name == "buvid4" and val:
                            updates["buvid4"] = val

                if "sessdata" not in updates:
                    return False, "刷新响应中未找到新 SESSDATA"

                # confirm 使用旧 refresh_token
                try:
                    new_jct = updates.get("bili_jct", bjct)
                    confirm_headers = dict(self._get_headers())
                    confirm_headers["Cookie"] = (
                        f"SESSDATA={updates['sessdata']}; bili_jct={new_jct}"
                    )
                    await self._http_post(
                        "https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
                        data={"csrf": new_jct, "refresh_token": rt},
                        timeout=10,
                    )
                except Exception as ce:
                    logger.warning("confirm/refresh 失败（可忽略）: %s", ce)

                self._apply_local_credential_updates(updates)
                self.clear_auth_backoff()
                self._notify_credential_update(updates)
                self._last_cookie_check_ts = time.time()
                logger.info("B站 Cookie 刷新成功")
                return True, "Cookie 刷新成功"
            except Exception as e:
                logger.error("Cookie 刷新异常: %s", e, exc_info=True)
                return False, f"刷新出错: {e}"

    async def maybe_refresh_cookie(self, interval_hours: float = 6.0) -> Tuple[bool, str]:
        """主循环节流入口：默认每 interval_hours 检查一次是否需要刷新 Cookie。"""
        now = time.time()
        interval = max(300.0, float(interval_hours) * 3600.0)
        if self._last_cookie_check_ts and (now - self._last_cookie_check_ts) < interval:
            return True, "skip"
        self._last_cookie_check_ts = now
        if not self.config.bilibili.is_authenticated:
            return False, "未登录"
        # 先看登录是否还活着
        try:
            nav = await self.get_nav_status()
            if not nav or nav.get("code") != 0:
                # 登录可能已失效，尝试 refresh
                if self.config.bilibili.refresh_token:
                    return await self.refresh_cookie(force=True)
                return False, "Cookie 可能已失效且无 refresh_token"
        except Exception:
            pass
        return await self.refresh_cookie(force=False)

    # ══════════════════════════════════════
    #  用户信息
    # ══════════════════════════════════════
    
    async def get_nav_status(self) -> Optional[Dict]:
        """获取用户登录状态"""
        data, _ = await self._http_get("https://api.bilibili.com/x/web-interface/nav")
        if isinstance(data, dict):
            nav_data = data.get("data") or {}
            if data.get("code") == AUTH_REQUIRED_CODE:
                self._record_authenticated_response(data)
            elif data.get("code") == 0 and nav_data.get("isLogin") is True:
                self.clear_auth_backoff()
        return data
    
    async def get_user_info(self, mid: int) -> Optional[Dict]:
        """获取用户信息"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params={"mid": mid},
        )
        return data.get("data") if data else None
    
    # ══════════════════════════════════════
    #  视频信息
    # ══════════════════════════════════════
    
    async def get_video_info(self, oid: int) -> Optional[Dict]:
        """获取视频信息（通过oid）"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"aid": oid},
        )
        return data.get("data") if data else None
    
    async def get_video_oid_by_bvid(self, bvid: str) -> Optional[int]:
        """通过bvid获取aid"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
        )
        if data and data.get("data"):
            return data["data"].get("aid")
        return None
    
    async def get_video_tags(
        self,
        bvid: str,
        video_info: Optional[Dict] = None,
    ) -> List[str]:
        """获取视频标签，接口不可用时回退到已有视频元数据。"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/tag/archive/tags",
            params={"bvid": bvid},
            allow_not_found=True,
        )

        tags: List[str] = []

        def add_tag(value) -> None:
            if not isinstance(value, str):
                return
            value = value.strip()
            if value and value not in tags:
                tags.append(value)

        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    add_tag(item.get("tag_name") or item.get("name"))
                else:
                    add_tag(item)
        if tags:
            return tags

        info = video_info if isinstance(video_info, dict) else {}
        for field in ("tags", "tag"):
            raw_tags = info.get(field)
            if isinstance(raw_tags, str):
                for item in raw_tags.replace("，", ",").split(","):
                    add_tag(item)
            elif isinstance(raw_tags, (list, tuple)):
                for item in raw_tags:
                    if isinstance(item, dict):
                        add_tag(item.get("tag_name") or item.get("name"))
                    else:
                        add_tag(item)

        # view/popular 的分区字段并非真正标签，但在标签接口不可用时
        # 比完全丢失主题信息更有用。两类响应的 v2 字段命名不同。
        for field in ("tname_v2", "tnamev2", "tname", "pid_name_v2"):
            add_tag(info.get(field))
        return tags
    
    async def get_hot_comments(self, oid: int, limit: int = 5) -> List[Dict]:
        """获取热门评论（结构化）。

        Returns list of dicts so archive/memory can keep mid/rpid, while
        prompt builders still accept plain strings for backward compatibility::

            {
              "rpid": str,
              "mid": str,
              "name": str,
              "content": str,   # alias: message / text
              "like": int,
            }
        """
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/v2/reply/hot",
            params={"oid": oid, "pn": 1, "ps": limit, "type": 1},
        )
        if not (data and data.get("data") and data["data"].get("replies")):
            return []
        rows: List[Dict] = []
        for reply in data["data"]["replies"][:limit]:
            if not isinstance(reply, dict):
                continue
            content = reply.get("content") or {}
            if not isinstance(content, dict):
                content = {}
            member = reply.get("member") or {}
            if not isinstance(member, dict):
                member = {}
            message = str(content.get("message") or "").strip()
            if not message:
                continue
            mid = str(member.get("mid") or reply.get("mid") or "")
            name = str(
                member.get("uname")
                or member.get("name")
                or reply.get("uname")
                or ""
            )
            rpid = str(reply.get("rpid") or reply.get("id") or "")
            rows.append(
                {
                    "rpid": rpid,
                    "mid": mid,
                    "user_id": mid,
                    "name": name,
                    "uname": name,
                    "content": message,
                    "message": message,
                    "text": message,
                    "like": int(reply.get("like") or 0),
                }
            )
        return rows
    
    async def get_video_subtitles(self, bvid: str, cid: int) -> Optional[str]:
        """获取视频字幕"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/player/v2",
            params={"bvid": bvid, "cid": cid},
        )
        if not data or not data.get("data"):
            return None
        
        play_info = data["data"].get("play_info")
        if not play_info:
            return None
        
        subtitles = []
        for label in play_info.get("subtitles", []):
            subtitle_url = label.get("subtitle_url", "")
            if subtitle_url:
                # 下载字幕
                session = await self._get_session()
                try:
                    async with session.get(
                        subtitle_url,
                        headers=self._get_headers(),
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 200:
                            caption_data = await resp.json()
                            for caption in caption_data.get("body", []):
                                subtitles.append(caption.get("text", ""))
                except Exception:
                    pass
        
        return "\n".join(subtitles) if subtitles else None

    async def get_video_play_url(self, bvid: str, cid: int, quality: int = 80) -> Optional[Dict]:
        """
        获取视频播放流地址（DASH 格式）

        Args:
            bvid: 视频 BV 号
            cid: 视频 CID（通过 get_video_info 获取）
            quality: 清晰度代码（80=1080P, 64=720P, 32=480P, 16=360P）

        Returns:
            data.dash 字段，包含 video / audio 流列表
        """
        # B站 playurl 接口需要 WBI 签名（2024+），否则返回 -400 请求错误
        # fnval=80 = DASH(64) + MP4(16)，避免使用 404（含无效标志位 4 会被拒绝）
        params = await self.sign_wbi({
            "bvid": bvid,
            "cid": cid,
            "qn": quality,
            "fnval": 80,  # DASH + MP4 回退
            "fnver": 0,
            "fourk": 1,
        })
        data, err = await self._http_get(
            "https://api.bilibili.com/x/player/wbi/playurl",
            params=params,
        )
        if not data or data.get("code") != 0:
            logger.warning(f"获取视频流失败: {err or data}")
            return None
        return data.get("data", {}).get("dash")

    async def download_video(self, bvid: str, cid: int, save_path: str, quality: int = 64) -> Optional[str]:
        """
        下载 B站视频到本地（DASH 格式，分别下载视频和音频流）

        Args:
            bvid: 视频 BV 号
            cid: 视频 CID
            save_path: 保存路径（含文件名，不含扩展名）
            quality: 清晰度（默认 64=720P，节省带宽）

        Returns:
            成功返回 mp4 文件路径，失败返回 None
        """
        import tempfile

        dash = await self.get_video_play_url(bvid, cid, quality=quality)
        if not dash:
            logger.warning(f"无法获取视频流: {bvid}")
            return None

        # 选最低清晰度的视频流（节省带宽，视频理解不需要高清）
        videos = dash.get("video", [])
        audios = dash.get("audio", [])
        if not videos:
            logger.warning("无可用视频流")
            return None

        # 按 id 降序选第一个 ≤ 目标清晰度的流，否则取最低
        videos_sorted = sorted(videos, key=lambda v: v.get("id", 0), reverse=True)
        target_video = None
        for v in videos_sorted:
            if v.get("id", 0) <= quality:
                target_video = v
                break
        if not target_video:
            target_video = videos_sorted[-1]

        video_url = target_video.get("baseUrl") or target_video.get("base_url") or target_video.get("url")
        if not video_url:
            logger.warning("视频流 URL 为空")
            return None

        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        video_file = f"{save_path}_video.m4s"
        audio_file = f"{save_path}_audio.m4s"
        output_file = f"{save_path}.mp4"

        headers = self._get_headers()
        headers["Referer"] = "https://www.bilibili.com/"

        session = await self._get_session()

        # 下载视频流（流式写盘，避免整文件进内存）
        try:
            ok = await _stream_download_to_file(
                session, video_url, video_file, headers, timeout=600
            )
            if not ok:
                # 清理可能的部分下载文件
                for tmp in [video_file]:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:
                        pass
                return None
            logger.info(f"视频流下载完成: {video_file}")
        except Exception as e:
            logger.error(f"下载视频流异常: {e}")
            # 清理可能的部分下载文件
            for tmp in [video_file]:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
            return None

        # 下载音频流（可选，失败不影响）
        audio_downloaded = False
        if audios:
            audio_url = audios[0].get("baseUrl") or audios[0].get("base_url") or audios[0].get("url")
            if audio_url:
                try:
                    ok = await _stream_download_to_file(
                        session, audio_url, audio_file, headers, timeout=300
                    )
                    if ok:
                        audio_downloaded = True
                        logger.info(f"音频流下载完成: {audio_file}")
                except Exception as e:
                    logger.warning(f"下载音频流失败（不影响视频分析）: {e}")

        # 合并视频和音频（ffmpeg 子进程异步化，避免阻塞事件循环）
        try:
            if audio_downloaded:
                cmd = ["ffmpeg", "-y", "-i", video_file, "-i", audio_file,
                       "-c", "copy", "-movflags", "+faststart", output_file]
            else:
                cmd = ["ffmpeg", "-y", "-i", video_file,
                       "-c", "copy", "-movflags", "+faststart", output_file]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise TimeoutError("ffmpeg timeout")
            if proc.returncode != 0:
                err_text = (stderr or b"").decode("utf-8", errors="ignore")[:300]
                logger.warning(f"ffmpeg 合并失败: {err_text}")
                os.replace(video_file, output_file)
                for tmp in [audio_file]:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            else:
                for tmp in [video_file, audio_file]:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            logger.info(f"视频下载完成: {output_file}")
            return output_file
        except Exception as e:
            logger.error(f"ffmpeg 合并异常: {e}")
            if os.path.exists(video_file):
                os.replace(video_file, output_file)
            for tmp in [audio_file]:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            if os.path.exists(output_file):
                return output_file
            return None

    # ══════════════════════════════════════
    #  评论管理
    # ══════════════════════════════════════
    
    async def get_replies(
        self,
        oid: int,
        comment_type: int = 1,
        pn: int = 1,
        ps: int = 20,
        sort: int = 0,
    ) -> Optional[Dict]:
        """获取评论列表"""
        # /x/v2/reply/wbi/root 曾经被当作根评论列表接口使用，但当前
        # B站 Web 端会直接返回 404。优先使用稳定的非 WBI 主列表接口，
        # 失败时再尝试新版 wbi/main，避免每轮自动态补扫刷 ERROR。
        base_params = {
            "oid": oid,
            "type": comment_type,
            "pn": pn,
            "ps": ps,
            "sort": sort,  # 0=最新评论；自动态补扫必须拉最新评论
        }
        data, err = await self._http_get(
            "https://api.bilibili.com/x/v2/reply",
            params=base_params,
            allow_not_found=True,
        )
        if data and data.get("code") == 0:
            return data

        logger.debug(f"评论主列表接口失败，尝试 wbi/main: {err or data}")
        try:
            wbi_params = await self.sign_wbi({
                "oid": oid,
                "type": comment_type,
                "mode": 3,
                "pagination_str": json.dumps({"offset": ""}, separators=(",", ":")),
                "plat": 1,
                "web_location": 1315875,
            })
        except RuntimeError as e:
            logger.warning(f"WBI 签名不可用，评论列表获取失败: {e}")
            return data  # 返回第一次非 WBI 请求的结果（可能为 None）
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/v2/reply/wbi/main",
            params=wbi_params,
            allow_not_found=True,
        )
        return data
    
    @staticmethod
    def _is_uncertain_transport_error(err: Optional[str]) -> bool:
        """True when the write may have succeeded on Bilibili despite local failure."""
        if not err:
            return False
        e = err.lower()
        return e.startswith((
            "timeout:",
            "network:",
            "http_5xx:",
            "empty_body:",
            "non_json:",
            "http:",  # unexpected status class
        ))

    async def post_comment(
        self,
        oid: int,
        content: str,
        comment_type: int = 1,
        rpid: int = 0,
        parent: int = 0,
        plat: int = 1,
    ) -> Optional[bool]:
        """
        发表评论

        Returns:
            True  — 明确成功 (code==0)
            False — 明确失败 (业务 code!=0 或未登录/4xx)
            None  — 结果不确定 (超时/网络/5xx/非JSON)，调用方不得自动重发
        """
        if not self.config.bilibili.is_authenticated:
            logger.error("未登录B站")
            return False

        # B站回复API：root=根评论rpid, parent=要回复的评论rpid, message=内容
        params = {
            "oid": oid,
            "type": comment_type,
            "message": content,
            "plat": plat,
            "csrf": self._csrf_token,
        }
        if rpid:
            params["root"] = rpid
            params["parent"] = parent if parent else rpid

        # /x/v2/reply/add 不需要 WBI 签名；超时 30s 避免因 B站响应慢导致无谓重试
        data, err = await self._http_post(
            "https://api.bilibili.com/x/v2/reply/add",
            data=params,
            timeout=30,
        )

        if data and data.get("code") == 0:
            async with self._api_code_lock:
                self.last_api_code = 0
            logger.info(f"评论成功: oid={oid}")
            return True
        if data is None and self._is_uncertain_transport_error(err):
            async with self._api_code_lock:
                self.last_api_code = -1
            logger.error(f"评论结果不确定（不自动重发）: {err}")
            return None
        async with self._api_code_lock:
            self.last_api_code = (data or {}).get("code", -1)
        logger.error(f"评论失败: {err or data}")
        return False

    async def get_comment_replies(self, oid: int, root: int, comment_type: int = 1,
                                   ps: int = 20, pn: int = 1) -> Optional[Dict]:
        """
        获取评论楼中楼（回复列表），用于构建对话上下文

        Args:
            oid: 评论区ID
            root: 根评论rpid
            comment_type: 评论类型
            ps: 每页条数
            pn: 页码

        Returns:
            API 返回的 data 字典
        """
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/v2/reply/reply",
            params={
                "oid": oid,
                "type": comment_type,
                "root": root,
                "ps": ps,
                "pn": pn,
            },
        )
        return data

    async def like_reply(
        self,
        rpid: int,
        action: int = 1,
        *,
        oid: Optional[int] = None,
        comment_type: int = 1,
    ) -> bool:
        """
        点赞/取消点赞评论（/x/v2/reply/action）

        Args:
            rpid: 评论 rpid
            action: 1=点赞, 0=取消点赞（兼容旧调用传入 2 时按取消处理）
            oid: 评论所属资源 id（视频 aid / 动态 id 等）；缺省时接口会失败
            comment_type: 评论区类型，视频=1
        """
        if action == 2:
            action = 0
        if not oid:
            logger.warning("like_reply 需要 oid（评论所属资源 id）")
            return False
        data, _ = await self._http_post(
            "https://api.bilibili.com/x/v2/reply/action",
            data={
                "oid": oid,
                "type": comment_type,
                "rpid": rpid,
                "action": action,
                "csrf": self._csrf_token,
            },
        )
        return bool(data and data.get("code") == 0)
    
    # ══════════════════════════════════════
    #  私信管理
    # ══════════════════════════════════════
    
    async def get_private_sessions(self, limit: int = 20) -> Optional[Dict]:
        """获取私信会话列表"""
        data, _ = await self._http_get(
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/new_sessions",
            params={
                "session_type": 1,
                "group_fold": 1,
                "unfollow_fold": 0,
                "sort_rule": 2,
                "build": 0,
                "mobi_app": "web",
            },
        )
        self._record_authenticated_response(data)
        return data

    async def get_session_messages(
        self,
        talker_id: Optional[int] = None,
        session_type: int = 1,
        size: int = 20,
        begin_seqno: int = 0,
        *,
        sender_uid: Optional[int] = None,
        receiver_uid: Optional[int] = None,
        next_seq: int = 0,
        limit: Optional[int] = None,
    ) -> Optional[Dict]:
        """Fetch session messages via web IM fetch_session_msgs.

        Bilibili expects talker_id / session_type / size / begin_seqno.
        Legacy kwargs sender_uid/receiver_uid/next_seq/limit are mapped.
        """
        if not talker_id:
            talker_id = sender_uid or receiver_uid or 0
        if limit is not None:
            size = limit
        if next_seq and not begin_seqno:
            begin_seqno = next_seq
        data, _ = await self._http_get(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
            params={
                "talker_id": talker_id,
                "session_type": session_type,
                "size": size,
                "begin_seqno": begin_seqno,
                "build": 0,
                "mobi_app": "web",
            },
        )
        self._record_authenticated_response(data)
        return data

    async def send_private_message(self, receiver_id: int, msg: str, msg_type: int = 1) -> Optional[bool]:
        """
        发送私信（web端）

        Returns True/False/None (None = transport uncertainty, do not auto-resend).
        """
        if not self.config.bilibili.is_authenticated:
            logger.error("未登录B站")
            return False

        import uuid
        # M7：dede_user_id 可能是非数字字符串，转换失败时记录并返回
        try:
            sender_uid = int(self.config.bilibili.dede_user_id or 0)
        except (TypeError, ValueError) as e:
            logger.warning(f"dede_user_id 非法，无法发送私信: {e}")
            return False
        if not sender_uid:
            logger.error("发送私信需要 dede_user_id")
            return False

        dev_id = str(uuid.uuid4()).upper()
        timestamp = int(time.time())
        # msg[content] 是 JSON 字符串 {"content":"消息内容"}，不需要 base64
        content_json = json.dumps({"content": msg}, ensure_ascii=False)

        # B站私信 API 要求 msg[xxx] 格式的参数
        data, err = await self._http_post(
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            data={
                "msg[sender_uid]": sender_uid,
                "msg[receiver_id]": receiver_id,
                "msg[receiver_type]": 1,
                "msg[msg_type]": msg_type,
                "msg[msg_status]": 0,
                "msg[dev_id]": dev_id,
                "msg[timestamp]": timestamp,
                "msg[content]": content_json,
                "from_firework": 0,
                "build": 0,
                "mobi_app": "web",
                "csrf": self._csrf_token,
            },
        )

        if data and data.get("code") == 0:
            logger.info(f"私信发送成功 -> {receiver_id}")
            return True
        if data is None and self._is_uncertain_transport_error(err):
            logger.error(f"私信结果不确定（不自动重发）: {err}")
            return None
        logger.error(f"私信发送失败: {err or data}")
        return False
    
    async def ack_session(
        self,
        talker_id: int,
        session_type: int = 1,
        ack_seqno: int = 0,
        *,
        sender_uid: Optional[int] = None,
        receiver_uid: Optional[int] = None,
    ) -> bool:
        """标记私信会话已读（web IM update_ack）。

        兼容旧调用 ack_session(sender_uid, receiver_uid)：
        将 sender_uid 视为 talker_id。
        """
        if not talker_id and sender_uid:
            talker_id = sender_uid
        data, err = await self._http_post(
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/update_ack",
            data={
                "talker_id": talker_id,
                "session_type": session_type,
                "ack_seqno": ack_seqno,
                "build": 0,
                "mobi_app": "web",
                "csrf_token": self._csrf_token,
                "csrf": self._csrf_token,
            },
        )
        ok = bool(data and data.get("code") == 0)
        if not ok:
            logger.debug("ack_session failed talker_id=%s err=%s data=%s", talker_id, err, data)
        return ok

    # ══════════════════════════════════════
    #  动态管理
    # ══════════════════════════════════════
    
    async def upload_dynamic_image(self, image_bytes: bytes) -> Optional[Dict]:
        """
        上传图片到 B站动态图床

        Args:
            image_bytes: 图片二进制数据 (PNG/JPG)

        Returns:
            {image_url, width, height} 或 None
        """
        if not self.config.bilibili.is_authenticated:
            return None

        import aiohttp as _aiohttp

        session = await self._get_session()

        async def _post_multipart(url: str, fields: Dict[str, str], file_field: str) -> Optional[Dict]:
            form = _aiohttp.FormData()
            for key, value in fields.items():
                form.add_field(key, value)
            form.add_field(
                file_field,
                image_bytes,
                filename="image.png",
                content_type="image/png",
            )
            headers = self._get_headers()
            # aiohttp must set the multipart boundary itself.  Keeping the
            # default x-www-form-urlencoded header makes B站 return an HTML
            # error page instead of JSON.
            headers.pop("Content-Type", None)
            headers["Origin"] = "https://www.bilibili.com"
            headers["Referer"] = "https://www.bilibili.com/"
            async with session.post(
                url,
                data=form,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"图片上传返回非 JSON({resp.status}) {url}: {text[:200]}")
                    return None

        def _normalize_uploaded_image(result: Optional[Dict], *, endpoint: str) -> Optional[Dict]:
            if not result:
                return None
            if result.get("code") != 0:
                logger.warning(f"图片上传失败({endpoint}): {result}")
                return None
            img_data = result.get("data", {}) or {}
            image_url = (
                img_data.get("image_url")
                or img_data.get("img_src")
                or img_data.get("url")
                or ""
            )
            width = (
                img_data.get("image_width")
                or img_data.get("img_width")
                or img_data.get("width")
                or 0
            )
            height = (
                img_data.get("image_height")
                or img_data.get("img_height")
                or img_data.get("height")
                or 0
            )
            img_size = img_data.get("img_size") or 0
            if not image_url:
                logger.warning(f"图片上传成功但缺少 image_url({endpoint}): {result}")
                return None
            logger.info(f"图片上传成功({endpoint}): {image_url} ({width}x{height})")
            return {
                "img_src": image_url,
                "img_width": int(width or 0),
                "img_height": int(height or 0),
                "img_size": float(img_size or 0),
            }

        try:
            # New web dynamic upload endpoint.  The legacy dynamic_svr endpoint
            # often returns an HTML error page for current web sessions.
            result = await _post_multipart(
                "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
                {"biz": "new_dyn", "category": "daily", "csrf": self._csrf_token},
                "file_up",
            )
            normalized = _normalize_uploaded_image(result, endpoint="upload_bfs")
            if normalized:
                return normalized

            # Fallback for accounts where B站 still accepts the older endpoint.
            result = await _post_multipart(
                "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/upload_pic",
                {"biz": "draw", "csrf": self._csrf_token},
                "file",
            )
            return _normalize_uploaded_image(result, endpoint="dynamic_svr")
        except Exception as e:
            logger.error(f"图片上传异常: {e}")
            return None

    async def post_dynamic_text(self, content: str, images: Optional[List[Dict]] = None) -> Optional[bool]:
        """
        发布动态（支持纯文字和图文）

        Args:
            content: 动态内容
            images: 图片列表，每项为 upload_dynamic_image 返回的 dict
                    {img_src, img_width, img_height}，为空则发纯文字动态
        """
        if not self.config.bilibili.is_authenticated:
            return False

        async def _post_new_dynamic() -> Tuple[Optional[Dict], Optional[str]]:
            # 官方文档 (bilibili-api-collect-new/docs/dynamic/publish.md):
            # - URL 必须带 ?csrf={bili_jct}
            # - Content-Type: application/json
            # - 请求体为 JSON 对象，顶层只有 dyn_req（csrf 通过 URL/Cookie 传递）
            # - dyn_req.scene: 1=纯文本 2=图文 4=转发
            # - dyn_req.pics[] 字段: img_src/img_width/img_height/img_size
            contents = [{"raw_text": content, "type": 1, "biz_id": ""}]
            dyn_req: Dict[str, object] = {
                "content": {"contents": contents},
                "scene": 2 if images else 1,
                "meta": {
                    "app_meta": {
                        "from": "create.dynamic.web",
                        "mobi_app": "web",
                    }
                },
            }
            if images:
                dyn_req["pics"] = [
                    {
                        "img_src": img.get("img_src") or img.get("image_url") or "",
                        "img_width": int(img.get("img_width") or img.get("width") or 0),
                        "img_height": int(img.get("img_height") or img.get("height") or 0),
                        "img_size": float(img.get("img_size") or 0),
                    }
                    for img in images
                    if img.get("img_src") or img.get("image_url")
                ]
                if not dyn_req["pics"]:
                    return None, "empty image list"

            # B 站新版动态接口要求 JSON 请求体
            payload = {"dyn_req": dyn_req}
            headers = self._get_headers()
            headers["Content-Type"] = "application/json"
            headers["Origin"] = "https://www.bilibili.com"
            headers["Referer"] = "https://www.bilibili.com/"
            session = await self._get_session()
            try:
                async with session.post(
                    f"https://api.bilibili.com/x/dynamic/feed/create/dyn?csrf={self._csrf_token}",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        if 500 <= resp.status <= 599:
                            return None, f"http_5xx: HTTP {resp.status}"
                        if 400 <= resp.status <= 499:
                            return None, f"http_4xx: HTTP {resp.status}"
                        return None, f"http: HTTP {resp.status}"
                    if not (body or "").strip():
                        return None, "empty_body: empty HTTP 200 body"
                    try:
                        resp_data = json.loads(body)
                    except json.JSONDecodeError:
                        return None, f"non_json: {body[:200]}"
                    async with self._api_code_lock:
                        self.last_api_code = (resp_data or {}).get("code", -1)
                    return resp_data, None
            except asyncio.TimeoutError:
                return None, "timeout: request timed out"
            except aiohttp.ClientError as exc:
                return None, f"network: {type(exc).__name__}: {exc}"
            except Exception as exc:
                return None, f"network: {type(exc).__name__}: {exc}"

        if images:
            logger.info(f"发布图文动态: {len(images)} 张配图")

        data, err = await _post_new_dynamic()

        if data and data.get("code") == 0:
            async with self._api_code_lock:
                self.last_api_code = 0
            logger.info(f"动态发布成功: {content[:50]}...")
            return True

        # Uncertain new-path failure: never fall back to old create (double-dynamic risk).
        if data is None and self._is_uncertain_transport_error(err):
            async with self._api_code_lock:
                self.last_api_code = -1
            logger.error(f"新版动态结果不确定，不回退旧接口: {err}")
            return None

        logger.warning(f"新版动态发布失败，尝试旧接口兜底: {err or data}")

        post_data = {
            "uid": self.config.bilibili.dede_user_id,
            "content": content,
            "up_choose_comment": 0,
            "csrf": self._csrf_token,
            "csrf_token": self._csrf_token,
        }

        if images:
            # 旧接口 type: 2=带图 4=纯文本（注意：旧 dynamic_svr 接口仅支持图片 URL 数组）
            post_data["type"] = 2
            post_data["pictures"] = json.dumps(
                [{"img_src": img.get("img_src"), "img_width": img.get("img_width", 0), "img_height": img.get("img_height", 0)} for img in images],
                ensure_ascii=False,
            )
        else:
            post_data["type"] = 4

        data, err = await self._http_post(
            "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create",
            data=post_data,
            timeout=30,
        )

        if data and data.get("code") == 0:
            async with self._api_code_lock:
                self.last_api_code = 0
            logger.info(f"动态发布成功(旧接口兜底): {content[:50]}...")
            return True
        if data is None and self._is_uncertain_transport_error(err):
            async with self._api_code_lock:
                self.last_api_code = -1
            logger.error(f"动态发布结果不确定（不自动重发）: {err}")
            return None
        async with self._api_code_lock:
            self.last_api_code = (data or {}).get("code", -1)
        logger.error(f"动态发布失败: {err or data}")
        return False
    
    async def get_user_dynamics(self, host_uid: int, offset: int = 0, limit: int = 20) -> Optional[Dict]:
        """获取用户动态"""
        params = {
            "host_mid": host_uid,
            "visit_id": "",
            "offset": str(offset) if offset else "",
            "timezone_offset": -480,
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,forwardListHidden,ugcDelete",
        }
        data, err = await self._http_get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params=params,
        )
        if data and data.get("code") == 0:
            return data
        logger.warning(f"新版动态列表获取失败，尝试旧接口兜底: {err or data}")
        data, _ = await self._http_get(
            "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/new_dyn",
            params={
                "host_uid": host_uid,
                "offset_dynamic_id": offset,
                "need_top": 1,
            },
        )
        return data
    
    # ══════════════════════════════════════
    #  搜索
    # ══════════════════════════════════════
    
    async def search_videos(self, keyword: str, order: str = "totalrank", 
                           page: int = 1, ps: int = 20) -> Optional[Dict]:
        """搜索视频"""
        params = await self.sign_wbi({
            "keyword": keyword,
            "order": order,
            "page": page,
            "pagesize": ps,
            "search_type": "video",
        })
        
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
        )
        return data
    
    async def search_users(self, keyword: str, page: int = 1, ps: int = 20) -> Optional[Dict]:
        """搜索用户"""
        params = await self.sign_wbi({
            "keyword": keyword,
            "page": page,
            "pagesize": ps,
            "search_type": "user",
        })
        
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
        )
        return data
    
    async def search_bangumi(self, keyword: str, ps: int = 5) -> Optional[Dict]:
        """搜索番剧"""
        params = await self.sign_wbi({
            "keyword": keyword,
            "search_type": "media_bangumi",
            "page": 1,
            "page_size": ps,
        })
        
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/wbi/search/type",
            params=params,
        )
        return data
    
    # ══════════════════════════════════════
    #  互动操作
    # ══════════════════════════════════════
    
    async def like_video(self, oid: int, like: int = 1) -> Optional[bool]:
        """点赞/取消点赞视频

        Args:
            oid: 视频 aid
            like: 1=点赞, 2=取消点赞（B站 archive/like 约定）

        Returns:
            True 明确成功 / False 明确失败 / None transport 不确定
            （超时/5xx/非 JSON 等；平台可能已接受，禁止自动当失败重试）
        """
        data, err = await self._http_post(
            "https://api.bilibili.com/x/web-interface/archive/like",
            data={
                "aid": oid,
                "like": like,
                "csrf": self._csrf_token,
            },
        )
        if err is not None or data is None:
            return None
        return bool(data.get("code") == 0)

    async def coin_video(self, oid: int, num: int = 1) -> Optional[bool]:
        """
        投币

        Args:
            oid: 视频aid
            num: 投币数量 (1或2)

        Returns:
            True 明确成功 / False 明确失败 / None transport 不确定
            （超时后可能已扣币，禁止当 failed 自动重试）
        """
        # MISC-602：投币数量仅允许 1 或 2，越界时夹紧到合法区间
        num = max(1, min(2, int(num)))
        data, err = await self._http_post(
            "https://api.bilibili.com/x/web-interface/coin/add",
            data={
                "sid": oid,
                "multiply": num,
                "cross_domain": "true",
                "csrf": self._csrf_token,
            },
        )
        if err is not None or data is None:
            return None
        return bool(data.get("code") == 0)

    async def fav_video(self, oid: int, fav_id: int = 0) -> Optional[bool]:
        """收藏视频

        Returns:
            True 明确成功 / False 明确失败 / None transport 不确定
        """
        # 先获取收藏夹列表
        if fav_id == 0:
            data, err = await self._http_get(
                "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                params={"up_mid": self.config.bilibili.dede_user_id},
            )
            if err is not None or data is None:
                return None
            if data.get("data") and data["data"].get("list"):
                fav_id = data["data"]["list"][0].get("id", 0)

        if fav_id == 0:
            return False

        data, err = await self._http_post(
            "https://api.bilibili.com/x/v3/fav/resource/deal",
            data={
                "rid": oid,
                "type": 2,  # 视频
                "add_media_ids": fav_id,
                "del_media_ids": "",
                "csrf": self._csrf_token,
            },
        )
        if err is not None or data is None:
            return None
        return bool(data.get("code") == 0)
    
    async def follow_user(self, mid: int) -> bool:
        """关注用户"""
        data, _ = await self._http_post(
            "https://api.bilibili.com/x/relation/modify",
            data={
                "fid": mid,
                "act": 1,  # 关注
                "re_src": 11,  # 空间主页
                "csrf": self._csrf_token,
            },
        )
        return data and data.get("code") == 0
    
    async def get_following_updates(self, limit: int = 20) -> Optional[Dict]:
        """获取关注UP主的新视频"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params={
                "host_mid": self.config.bilibili.dede_user_id,
                "visit_id": "",
                "offset": "",
                "feature": 0,
            },
        )
        return data
    
    # ══════════════════════════════════════
    #  番剧 (PGC)
    # ══════════════════════════════════════
    
    async def get_bangumi_detail(self, season_id: Optional[int] = None, 
                                  ep_id: Optional[int] = None) -> Optional[Dict]:
        """获取番剧详情"""
        params = {}
        if season_id:
            params["season_id"] = season_id
        elif ep_id:
            params["ep_id"] = ep_id
        else:
            return None
        
        data, _ = await self._http_get(
            "https://api.bilibili.com/pgc/view/web/season",
            params=params,
        )
        return data.get("result") if data else None
    
    async def get_bangumi_trending(self, season_type: int = 1, day: int = 3) -> Optional[Dict]:
        """获取番剧排行榜"""
        if season_type == 1:
            url = "https://api.bilibili.com/pgc/web/rank/list"
        else:
            url = "https://api.bilibili.com/pgc/season/rank/web/list"
        
        data, _ = await self._http_get(
            url,
            params={"season_type": season_type, "day": day},
        )
        return data
    
    async def get_bangumi_timeline(self, day_before: int = 0, day_after: int = 6) -> Optional[Dict]:
        """获取番剧时间表"""
        # 优先v1 API
        data, _ = await self._http_get(
            "https://api.bilibili.com/pgc/web/timeline",
            params={"types": 1, "before": day_before, "after": day_after},
        )
        if data and data.get("result"):
            return data

        # 回退v2
        data, _ = await self._http_get(
            "https://api.bilibili.com/pgc/web/timeline/v2",
            params={"season_type": 1, "day_before": day_before, "day_after": day_after},
        )
        return data

    async def get_bangumi_play_url(self, ep_id: int, cid: int, quality: int = 64) -> Optional[Dict]:
        """获取番剧（PGC）播放流地址（DASH 格式）

        PGC 番剧使用 /pgc/player/web/playurl，不需要 WBI 签名，但需要 Cookie。
        某些番剧需要大会员才能获取高清晰度。

        Args:
            ep_id: 番剧剧集 ID
            cid: 视频 CID
            quality: 清晰度（64=720P, 32=480P, 16=360P）

        Returns:
            DASH 流字典，包含 video/audio 流列表
        """
        params = {
            "ep_id": ep_id,
            "cid": cid,
            "qn": quality,
            "fnval": 80,  # DASH + MP4 回退
            "fnver": 0,
            "fourk": 0,
        }
        data, err = await self._http_get(
            "https://api.bilibili.com/pgc/player/web/playurl",
            params=params,
        )
        if not data or data.get("code") != 0:
            logger.warning(f"获取番剧视频流失败: ep_id={ep_id} err={err or data}")
            return None
        # 注意：/pgc/player/web/playurl 的播放信息在 result 字段，不是 data
        payload = data.get("result") or data.get("data") or {}
        dash = payload.get("dash")
        if not dash:
            logger.warning(f"番剧视频流无 dash: ep_id={ep_id} err={err or data}")
            return None
        return dash

    async def get_bangumi_subtitles(self, ep_id: int, cid: int) -> Optional[List[Dict]]:
        """获取番剧（PGC）字幕段（带时间轴）

        番剧一般都有字幕。字幕来自 PGC playurl 响应的 `subtitle.subtitles`，
        每个字幕文件是 JSON，body 为 [{"from": 秒, "to": 秒, "content": 文本}]。

        Args:
            ep_id: 番剧剧集 ID
            cid: 视频 CID

        Returns:
            字幕段列表 [{"from": float, "to": float, "content": str}]；无字幕返回 None
        """
        data, err = await self._http_get(
            "https://api.bilibili.com/pgc/player/web/playurl",
            params={"ep_id": ep_id, "cid": cid, "qn": 64, "fnval": 0, "fnver": 0, "fourk": 0},
        )
        if not data or data.get("code") != 0:
            logger.warning(f"获取番剧字幕失败(playurl): ep_id={ep_id} err={err or data}")
            return None

        # 注意：/pgc/player/web/playurl 的字幕信息在 result 字段，不是 data
        payload = data.get("result") or data.get("data") or {}
        subtitle_info = payload.get("subtitle") or {}
        subs = subtitle_info.get("subtitles") or []
        if not subs:
            logger.info(f"该番剧无字幕轨道: ep_id={ep_id}")
            return None

        session = await self._get_session()
        headers = self._get_headers()
        segments: List[Dict] = []
        for label in subs:
            subtitle_url = label.get("subtitle_url", "")
            if not subtitle_url:
                continue
            # 字幕 URL 可能是协议相对地址
            if subtitle_url.startswith("//"):
                subtitle_url = "https:" + subtitle_url
            try:
                async with session.get(
                    subtitle_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        continue
                    caption_data = await resp.json()
                    for cap in caption_data.get("body", []):
                        content = (cap.get("content") or "").strip()
                        if content:
                            segments.append({
                                "from": float(cap.get("from", 0) or 0),
                                "to": float(cap.get("to", 0) or 0),
                                "content": content,
                            })
            except Exception as e:
                logger.warning(f"下载番剧字幕文件失败: {e}")

        if not segments:
            return None
        logger.info(f"番剧字幕获取成功: ep_id={ep_id} 共 {len(segments)} 段")
        return segments

    async def download_bangumi_video(self, ep_id: int, cid: int, save_path: str,
                                      quality: int = 64, with_audio: bool = True) -> Optional[str]:
        """下载番剧视频到本地（DASH 格式，分别下载视频和音频流后合并）

        PGC 内容需要 Cookie + 正确的 Referer。低清晰度(360P/480P)通常不需要大会员。

        Args:
            ep_id: 番剧剧集 ID
            cid: 视频 CID
            save_path: 保存路径（含文件名，不含扩展名）
            quality: 清晰度（默认 64=720P）
            with_audio: 是否下载并合并音频流。番剧走字幕识别时设为 False，
                完全不消耗音频带宽（"不用声音"）。

        Returns:
            成功返回 mp4 文件路径，失败返回 None
        """
        dash = await self.get_bangumi_play_url(ep_id, cid, quality=quality)
        if not dash:
            logger.warning(f"无法获取番剧视频流: ep_id={ep_id}")
            return None

        videos = dash.get("video", [])
        audios = dash.get("audio", [])
        if not videos:
            logger.warning("番剧无可用视频流（可能需要大会员或地区限制）")
            return None

        # 选最低清晰度
        videos_sorted = sorted(videos, key=lambda v: v.get("id", 0), reverse=True)
        target_video = None
        for v in videos_sorted:
            if v.get("id", 0) <= quality:
                target_video = v
                break
        if not target_video:
            target_video = videos_sorted[-1]

        video_url = target_video.get("baseUrl") or target_video.get("base_url") or target_video.get("url")
        if not video_url:
            logger.warning("番剧视频流 URL 为空")
            return None

        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        video_file = f"{save_path}_video.m4s"
        audio_file = f"{save_path}_audio.m4s"
        output_file = f"{save_path}.mp4"

        headers = self._get_headers()
        # PGC 必须设置正确的 Referer，否则 CDN 会返回 403
        headers["Referer"] = f"https://www.bilibili.com/bangumi/play/ep{ep_id}"

        session = await self._get_session()

        # 下载视频流（流式写盘，避免整文件进内存）
        try:
            ok = await _stream_download_to_file(
                session, video_url, video_file, headers, timeout=600
            )
            if not ok:
                return None
            logger.info(f"番剧视频流下载完成: {video_file}")
        except Exception as e:
            logger.error(f"下载番剧视频流异常: {e}")
            return None

        # 下载音频流（可选；番剧字幕识别模式可跳过以节省带宽）
        audio_downloaded = False
        if with_audio and audios:
            audio_url = audios[0].get("baseUrl") or audios[0].get("base_url") or audios[0].get("url")
            if audio_url:
                try:
                    ok = await _stream_download_to_file(
                        session, audio_url, audio_file, headers, timeout=300
                    )
                    if ok:
                        audio_downloaded = True
                        logger.info(f"番剧音频流下载完成: {audio_file}")
                except Exception as e:
                    logger.warning(f"下载番剧音频流失败（不影响视频分析）: {e}")

        # ffmpeg 合并（异步子进程，避免阻塞事件循环）
        try:
            if audio_downloaded:
                cmd = ["ffmpeg", "-y", "-i", video_file, "-i", audio_file,
                       "-c", "copy", "-movflags", "+faststart", output_file]
            else:
                cmd = ["ffmpeg", "-y", "-i", video_file,
                       "-c", "copy", "-movflags", "+faststart", output_file]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise TimeoutError("ffmpeg timeout")
            if proc.returncode != 0:
                err_text = (stderr or b"").decode("utf-8", errors="ignore")[:300]
                logger.warning(f"ffmpeg 合并番剧失败: {err_text}")
                import shutil as _shutil
                _shutil.move(video_file, output_file)
        except Exception as e:
            logger.error(f"ffmpeg 合并番剧异常: {e}")
            import shutil as _shutil
            if os.path.exists(video_file):
                _shutil.move(video_file, output_file)

        # 清理临时文件
        for tmp in (video_file, audio_file):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

        logger.info(f"番剧下载完成: {output_file}")
        return output_file

    async def follow_bangumi(self, season_id: int) -> bool:
        """追番（点追番按钮）

        Args:
            season_id: 番剧 season_id

        Returns:
            成功返回 True
        """
        data, err = await self._http_post(
            "https://api.bilibili.com/pgc/web/follow/add",
            data={"season_id": season_id, "csrf": self.config.bilibili.bili_jct or ""},
        )
        if isinstance(data, dict) and data.get("code") == 0:
            logger.info(f"追番成功: season_id={season_id}")
            return True
        logger.debug(f"追番失败: {err or data}")
        return False

    async def get_followed_bangumi(self, follow_status: int = 0, page: int = 1,
                                    page_size: int = 30) -> list:
        """获取已追番列表

        Args:
            follow_status: 0=全部 1=想看 2=在看 3=看过
            page: 页码
            page_size: 每页条数

        Returns:
            追番列表，每项含 season_id/title/new_ep_id/new_ep_index/total_count
        """
        vmid = self.config.bilibili.dede_user_id or ""
        if not vmid:
            logger.warning("获取追番列表需要 dede_user_id")
            return []
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/space/bangumi/follow/list",
            params={
                "vmid": vmid, "type": 1,
                "follow_status": follow_status,
                "pn": page, "ps": page_size,
            },
        )
        if not isinstance(data, dict) or data.get("code") != 0:
            logger.debug(f"获取追番列表失败: {str(data)[:200]}")
            return []
        items = (data.get("data") or {}).get("list") or []
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            new_ep = item.get("new_ep") or {}
            result.append({
                "season_id": item.get("season_id", 0),
                "title": item.get("title", ""),
                "new_ep_id": new_ep.get("id", 0) if isinstance(new_ep, dict) else 0,
                "new_ep_index": new_ep.get("index_show", "") if isinstance(new_ep, dict) else "",
                "total_count": item.get("total_count", 0),
            })
        logger.info(f"获取追番列表: {len(result)} 部")
        return result
    
    async def get_hot_videos(self, page: int = 1) -> Optional[Dict]:
        """获取热门视频"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/popular",
            params={"ps": 50, "pn": page},
        )
        return data

    async def get_recommend_videos(self, ps: int = 20) -> Optional[Dict]:
        """获取首页推荐流视频（需要登录态）

        返回格式归一化为 {"data": {"list": [...]}}，与 get_hot_videos 一致。
        """
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/index/top/feed/rcmd",
            params={"ps": ps, "fresh_idx": 1, "fresh_type": 4, "version": 1},
        )
        if not data:
            return None
        items = []
        try:
            items = data.get("data", {}).get("item", []) or []
        except Exception:
            items = []
        return {"data": {"list": items}}

    async def get_region_hot_videos(self, rid: int = 0, page: int = 1, ps: int = 30) -> Optional[Dict]:
        """获取分区热门视频

        rid=0 时随机选一个分区。返回格式归一化为 {"data": {"list": [...]}}。
        """
        regions = [
            1, 3, 4, 5, 36, 188, 234, 223, 160, 211,
            217, 119, 155, 202, 181, 177, 129, 251,
        ]
        if rid == 0:
            rid = random.choice(regions)
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/ranking/region",
            params={"ps": ps, "pn": page, "rid": rid, "day": 7},
        )
        if not data:
            return None
        raw_list = []
        try:
            d = data.get("data")
            if isinstance(d, list):
                raw_list = d
            elif isinstance(d, dict):
                raw_list = d.get("list", d.get("archives", [])) or []
        except Exception:
            raw_list = []
        return {"data": {"list": raw_list}}
    
    async def get_reply_notifications(self) -> Optional[Dict]:
        """获取回复我的评论通知（msgfeed/reply）"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/msgfeed/reply",
            params={"platform": "web", "build": 0, "mobi_app": "web"},
        )
        self._record_authenticated_response(data)
        return data

    async def get_at_notifications(self) -> Optional[Dict]:
        """获取@我的通知（msgfeed/at）

        返回结构与 get_reply_notifications 一致（items[]/user/item），
        item.type 通常为 "at"，business_id 视场景而定（视频评论/动态评论等）。
        """
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/msgfeed/at",
            params={"platform": "web", "build": 0, "mobi_app": "web"},
        )
        self._record_authenticated_response(data)
        return data
    
    async def get_videos_by_uid(self, uid: int, page: int = 1, ps: int = 30) -> Optional[Dict]:
        """获取指定用户投稿的视频列表"""
        params = await self.sign_wbi({
            "mid": uid,
            "ps": ps,
            "pn": page,
            "order": "pubdate",
        })
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/space/wbi/arc/search",
            params=params,
        )
        return data
