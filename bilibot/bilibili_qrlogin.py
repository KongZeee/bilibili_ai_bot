"""
B站扫码登录 API 适配器
"""
import logging
import time
import json
from typing import Optional, Dict, Tuple
import aiohttp
import qrcode
import io
import base64

logger = logging.getLogger("bilibot.bilibili")


class BilibiliQRLogin:
    """B站扫码登录封装"""

    def __init__(self, config, config_path: str = "config.yaml"):
        self.config = config
        self.config_path = config_path
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建aiohttp会话"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/",
                }
            )
        return self.session

    async def close(self):
        """关闭会话"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_qrcode(self) -> Dict:
        """
        获取登录二维码（新版 B站 API）

        Returns:
            {"key": str, "url": str, "data_url": str, "expire_ts": int}
        """
        try:
            session = await self._get_session()

            async with session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}

                data = await resp.json()
                if data.get("code") != 0:
                    return {"error": data.get("message", "获取二维码失败")}

                result = data.get("data", {})
                qr_key = result.get("qrcode_key")
                qr_url = result.get("url")

                if not qr_key or not qr_url:
                    return {"error": "未获取到二维码key或URL"}

                # 生成二维码图片
                qr = qrcode.QRCode(
                    version=1,
                    error_correction=qrcode.constants.ERROR_CORRECT_L,
                    box_size=10,
                    border=4,
                )
                qr.add_data(qr_url)
                qr.make(fit=True)

                img = qr.make_image(fill_color="black", back_color="white")

                # 转换为Base64
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                img_b64 = base64.b64encode(buf.getvalue()).decode()
                qr_data_url = f"data:image/png;base64,{img_b64}"

                # 3分钟后过期（新版 API 有效期 180 秒）
                expire_ts = int(time.time()) + 180

                return {
                    "key": qr_key,
                    "url": qr_url,
                    "data_url": qr_data_url,
                    "expire_ts": expire_ts,
                }

        except Exception as e:
            logger.error(f"获取二维码失败: {e}")
            return {"error": str(e)}

    async def check_status(self, qr_key: str) -> Dict:
        """
        查询扫码登录状态（新版 B站 API）

        状态码：
            86101 = 未扫码
            86090 = 已扫码，等待确认
            86038 = 二维码过期
            0     = 登录成功

        Returns:
            {"status": str, "message": str, "cookies": Dict}
        """
        try:
            session = await self._get_session()

            async with session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com",
                },
                params={"qrcode_key": qr_key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return {"status": "error", "message": f"HTTP {resp.status}"}

                data = await resp.json()
                if data.get("code") != 0:
                    return {"status": "error", "message": data.get("message", "查询失败")}

                result = data.get("data", {})
                inner_code = result.get("code", -1)

                if inner_code == 86101:
                    return {"status": "waiting", "message": "等待扫码..."}
                elif inner_code == 86090:
                    return {"status": "scanned", "message": "已扫码，请在手机上确认"}
                elif inner_code == 86038:
                    return {"status": "expired", "message": "二维码已过期"}
                elif inner_code == 0:
                    url = result.get("url", "")
                    cookies = self._parse_cookies_from_url(url)
                    return {
                        "status": "confirmed",
                        "message": "登录成功！正在保存配置...",
                        "cookies": cookies,
                    }
                else:
                    return {"status": "error", "message": result.get("message", f"未知状态 code={inner_code}")}

        except Exception as e:
            logger.error(f"查询登录状态失败: {e}")
            return {"status": "error", "message": str(e)}

    def _parse_cookies_from_url(self, url: str) -> Dict[str, str]:
        """从重定向URL中解析Cookie"""
        from urllib.parse import unquote, parse_qs, urlparse
        cookies = {}
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            for key, vals in params.items():
                if vals:
                    cookies[key] = unquote(vals[0])
        except Exception as e:
            logger.error(f"解析Cookie失败: {e}")
        return cookies

    def update_config(self, cookies: Dict[str, str], account_id: str = "") -> Dict:
        """更新配置中的登录信息

        Args:
            cookies: 登录成功后的 cookie 字典
            account_id: V2 多账号架构下的账号 ID。
                        非空且 accounts 列表中存在时写入对应账号；
                        否则回退到 V1 bilibili 段（兼容单账号）。
        """
        try:
            sessdata = cookies.get("SESSDATA", "")
            bili_jct = cookies.get("bili_jct", "")
            dede_user_id = cookies.get("DedeUserID", "")
            buvid3 = cookies.get("buvid3", "")

            if not sessdata:
                return {"success": False, "message": "SESSDATA为空"}

            raw = self.config.get_raw_config()
            accounts_list = raw.get("accounts", [])

            # V2：account_id 非空且 accounts 列表存在对应账号 → 写入账号段
            if account_id and accounts_list:
                updated = False
                for acc in accounts_list:
                    if acc.get("id") == account_id:
                        acc["sessdata"] = sessdata
                        acc["bili_jct"] = bili_jct
                        acc["dede_user_id"] = dede_user_id
                        if buvid3:
                            acc["buvid3"] = buvid3
                        updated = True
                        break
                if not updated:
                    return {"success": False, "message": f"未找到账号 id={account_id}"}
            else:
                # V1 兼容：写入 bilibili 段
                bili = raw.setdefault("bilibili", {})
                bili["sessdata"] = sessdata
                bili["bili_jct"] = bili_jct
                bili["dede_user_id"] = dede_user_id
                if buvid3:
                    bili["buvid3"] = buvid3

            # 用 save_config 保存到文件并热重载（更新属性对象 + _raw_config）
            self.config.save_config(raw, self.config_path)

            return {
                "success": True,
                "message": "登录信息已保存",
                "info": {
                    "uid": dede_user_id,
                    "account_id": account_id or None,
                    "has_sessdata": True,
                }
            }
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return {"success": False, "message": str(e)}
