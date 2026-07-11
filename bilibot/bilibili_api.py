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


class BilibiliAPI:
    """B站API封装"""
    
    def __init__(self, config):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self._wbi_imgs: Optional[Dict[str, str]] = None
        self._wbi_mixkey: Optional[str] = None
        self._csrf_token: str = config.bilibili.bili_jct or ""
        # PRD 4.9：记录最近 API 错误码，scheduler 据此判断风控
        self.last_api_code: int = 0

    def reload_credentials(self, config=None):
        """PRD V3 §3.3：热重载凭据（不重建 session）

        Web 配置保存后调用，避免重启账号即可更新 cookie。
        注意：_get_headers 每次都从 self.config 读取 sessdata/dede_user_id/buvid3，
        所以只需更新 config 引用 + _csrf_token。
        """
        if config is not None:
            self.config = config
        self._csrf_token = self.config.bilibili.bili_jct or ""
        self.last_api_code = 0
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建aiohttp会话"""
        if self.session is None or self.session.closed:
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
        if self.session and not self.session.closed:
            await self.session.close()
    
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
        
        if extra_cookies:
            for k, v in extra_cookies.items():
                cookies += f"; {k}={v}"
        
        headers["Cookie"] = cookies
        return headers
    
    async def _http_get(self, url: str, params: Optional[Dict] = None, timeout: int = 10) -> Tuple[Optional[Dict], Optional[str]]:
        """
        HTTP GET请求
        
        Returns:
            (parsed_json, error_text)
        """
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
                    logger.error(f"HTTP {resp.status} for {url}")
                    return None, f"HTTP {resp.status}"
        except asyncio.TimeoutError:
            logger.error(f"请求超时: {url}")
            return None, "timeout"
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return None, str(e)
    
    async def _http_post(self, url: str, data: Optional[Dict] = None, timeout: int = 10) -> Tuple[Optional[Dict], Optional[str]]:
        """HTTP POST请求"""
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
                    try:
                        data = json.loads(text)
                        # MISC-601：所有 POST API 调用统一记录返回码，供风控检测（-352）使用
                        self.last_api_code = (data or {}).get("code", -1)
                        return data, None
                    except json.JSONDecodeError:
                        return None, text
                else:
                    return None, f"HTTP {resp.status}"
        except Exception as e:
            logger.error(f"POST请求失败: {e}")
            return None, str(e)
    
    # ══════════════════════════════════════
    #  WBI 签名
    # ══════════════════════════════════════
    
    async def _get_wbi_mixkey(self) -> Optional[str]:
        """获取WBI混键"""
        if self._wbi_mixkey:
            return self._wbi_mixkey

        session = await self._get_session()
        try:
            async with session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=self._get_headers(),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # B站 nav API 字段为 wbi_img（单数），非 wbi_imgs
                    imgs = data.get("data", {}).get("wbi_img", {}) or data.get("data", {}).get("wbi_imgs", {})
                    img_url = imgs.get("img_url", "")
                    sub_url = imgs.get("sub_url", "")

                    if not img_url or not sub_url:
                        logger.warning("nav API 未返回 wbi_img，WBI 签名不可用")
                        return None

                    # 从 URL 中提取文件名（去掉扩展名）
                    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                    mix_key = img_key + sub_key
                    self._wbi_imgs = imgs
                    self._wbi_mixkey = self._encrypt_mixkey(mix_key)
                    return self._wbi_mixkey
        except Exception as e:
            logger.error(f"获取WBI混键失败: {e}")
        return None
    
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
        """
        mix_key = await self._get_wbi_mixkey()
        if not mix_key:
            return params

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
    #  用户信息
    # ══════════════════════════════════════
    
    async def get_nav_status(self) -> Optional[Dict]:
        """获取用户登录状态"""
        data, _ = await self._http_get("https://api.bilibili.com/x/web-interface/nav")
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
    
    async def get_video_tags(self, bvid: str) -> List[str]:
        """获取视频标签"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/view/tag",
            params={"bvid": bvid},
        )
        if data and data.get("data"):
            return [tag.get("tag_name", "") for tag in data["data"]]
        return []
    
    async def get_hot_comments(self, oid: int, limit: int = 5) -> List[str]:
        """获取热门评论"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/v2/reply/hot",
            params={"oid": oid, "pn": 1, "ps": limit, "type": 1},
        )
        if data and data.get("data") and data["data"].get("replies"):
            return [
                reply.get("content", {}).get("message", "")
                for reply in data["data"]["replies"][:limit]
            ]
        return []
    
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
                    async with session.get(subtitle_url, headers=self._get_headers()) as resp:
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

        # 下载视频流
        try:
            async with session.get(video_url, headers=headers, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status != 200:
                    logger.warning(f"下载视频流失败: HTTP {resp.status}")
                    return None
                with open(video_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        f.write(chunk)
            logger.info(f"视频流下载完成: {video_file}")
        except Exception as e:
            logger.error(f"下载视频流异常: {e}")
            return None

        # 下载音频流（可选，失败不影响）
        audio_downloaded = False
        if audios:
            audio_url = audios[0].get("baseUrl") or audios[0].get("base_url") or audios[0].get("url")
            if audio_url:
                try:
                    async with session.get(audio_url, headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                        if resp.status == 200:
                            with open(audio_file, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 256):
                                    f.write(chunk)
                            audio_downloaded = True
                            logger.info(f"音频流下载完成: {audio_file}")
                except Exception as e:
                    logger.warning(f"下载音频流失败（不影响视频分析）: {e}")

        # 合并视频和音频（需要 ffmpeg）
        import subprocess
        try:
            if audio_downloaded:
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", video_file, "-i", audio_file,
                     "-c", "copy", "-movflags", "+faststart", output_file],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=120, encoding="utf-8", errors="ignore",
                )
            else:
                proc = subprocess.run(
                    ["ffmpeg", "-y", "-i", video_file,
                     "-c", "copy", "-movflags", "+faststart", output_file],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=120, encoding="utf-8", errors="ignore",
                )
            if proc.returncode != 0:
                logger.warning(f"ffmpeg 合并失败: {proc.stderr[:300]}")
                # 合并失败时直接用视频流
                # PRD 4.3：os.replace 跨平台原子替换，避免 Windows 上目标已存在时失败
                os.replace(video_file, output_file)
                # VID-604：清理残留的音频临时文件
                for tmp in [audio_file]:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            else:
                # 清理临时流文件
                for tmp in [video_file, audio_file]:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
            logger.info(f"视频下载完成: {output_file}")
            return output_file
        except Exception as e:
            logger.error(f"ffmpeg 合并异常: {e}")
            # 降级：直接用视频流文件
            if os.path.exists(video_file):
                os.replace(video_file, output_file)
            # VID-604：清理残留的音频临时文件
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
    
    async def get_replies(self, oid: int, comment_type: int = 1, pn: int = 1, ps: int = 20) -> Optional[Dict]:
        """获取评论列表"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/v2/reply/wbi/root",
            params={
                "oid": oid,
                "type": comment_type,
                "pn": pn,
                "ps": ps,
                "sort": 1,  # 按热度
            },
        )
        return data
    
    async def post_comment(
        self,
        oid: int,
        content: str,
        comment_type: int = 1,
        rpid: int = 0,
        parent: int = 0,
        plat: int = 1,
    ) -> bool:
        """
        发表评论

        Args:
            oid: 目标评论区id
            content: 评论内容
            comment_type: 评论类型 (1=视频, 11=文章, 17=动态)
            rpid: 根评论ID (root)，0=主评论
            parent: 父评论ID (要回复的那条评论)，0=同 rpid
            plat: 平台 (1=web, 2=安卓, 3=iOS)

        Returns:
            是否成功
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
            self.last_api_code = 0
            logger.info(f"评论成功: oid={oid}")
            return True
        else:
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

    async def like_reply(self, rpid: int, action: int = 1) -> bool:
        """
        点赞/取消点赞评论
        
        Args:
            rpid: 评论ID
            action: 1=点赞, 2=取消点赞
        """
        data, _ = await self._http_post(
            "https://api.bilibili.com/x/reply/list/report",
            data={
                "rpid": rpid,
                "action": action,
                "csrf": self._csrf_token,
            },
        )
        return data and data.get("code") == 0
    
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
        return data

    async def get_session_messages(self, sender_uid: int, receiver_uid: int,
                                    next_seq: int = 0, limit: int = 20) -> Optional[Dict]:
        """获取会话消息"""
        data, _ = await self._http_get(
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
            params={
                "sender_uid_id": sender_uid,
                "receiver_uid_id": receiver_uid,
                "sender_seq": 0,
                "receiver_seq": 0,
                "build": 0,
                "mobi_app": "web",
            },
        )
        return data
    
    async def send_private_message(self, receiver_id: int, msg: str, msg_type: int = 1) -> bool:
        """
        发送私信（web端）

        Args:
            receiver_id: 接收者UID
            msg: 消息内容
            msg_type: 1=文本, 2=图片
        """
        if not self.config.bilibili.is_authenticated:
            logger.error("未登录B站")
            return False

        import uuid
        sender_uid = int(self.config.bilibili.dede_user_id or 0)
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
        else:
            logger.error(f"私信发送失败: {err or data}")
            return False
    
    async def ack_session(self, sender_uid: int, receiver_uid: int) -> bool:
        """标记私信已读"""
        data, _ = await self._http_post(
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/update_ack",
            data={
                "sender_uid_id": sender_uid,
                "receiver_uid_id": receiver_uid,
                "build": 0,
                "mobi_app": "web",
                "csrf_token": self._csrf_token,
            },
        )
        return data and data.get("code") == 0
    
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

        url = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/upload_pic"
        form = _aiohttp.FormData()
        form.add_field("biz", "draw")
        form.add_field(
            "file", image_bytes,
            filename="image.png",
            content_type="image/png",
        )

        session = await self._get_session()
        try:
            async with session.post(
                url, data=form,
                headers=self._get_headers(),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                try:
                    result = json.loads(text)
                except json.JSONDecodeError:
                    logger.error(f"图片上传返回非 JSON: {text[:200]}")
                    return None

                if result.get("code") == 0:
                    img_data = result.get("data", {})
                    image_url = img_data.get("image_url", "")
                    width = img_data.get("width", 0)
                    height = img_data.get("height", 0)
                    logger.info(f"图片上传成功: {image_url} ({width}x{height})")
                    return {
                        "img_src": image_url,
                        "img_width": width,
                        "img_height": height,
                    }
                else:
                    logger.error(f"图片上传失败: {result}")
                    return None
        except Exception as e:
            logger.error(f"图片上传异常: {e}")
            return None

    async def post_dynamic_text(self, content: str, images: Optional[List[Dict]] = None) -> bool:
        """
        发布动态（支持纯文字和图文）

        Args:
            content: 动态内容
            images: 图片列表，每项为 upload_dynamic_image 返回的 dict
                    {img_src, img_width, img_height}，为空则发纯文字动态
        """
        if not self.config.bilibili.is_authenticated:
            return False

        post_data = {
            "uid": self.config.bilibili.dede_user_id,
            "content": content,
            "up_choose_comment": 0,
            "csrf": self._csrf_token,
        }

        if images:
            # 图文动态 type=4
            post_data["type"] = 4
            post_data["pictures"] = json.dumps(images, ensure_ascii=False)
            logger.info(f"发布图文动态: {len(images)} 张配图")
        else:
            # 纯文字动态 type=1
            post_data["type"] = 1

        data, err = await self._http_post(
            "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create",
            data=post_data,
            timeout=30,
        )

        if data and data.get("code") == 0:
            self.last_api_code = 0
            logger.info(f"动态发布成功: {content[:50]}...")
            return True
        else:
            self.last_api_code = (data or {}).get("code", -1)
            logger.error(f"动态发布失败: {err or data}")
            return False
    
    async def get_user_dynamics(self, host_uid: int, offset: int = 0, limit: int = 20) -> Optional[Dict]:
        """获取用户动态"""
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
    
    async def like_video(self, oid: int) -> bool:
        """点赞视频"""
        data, _ = await self._http_post(
            "https://api.bilibili.com/x/web-interface/like/archive",
            data={
                "aid": oid,
                "like_state": 1,
                "csrf": self._csrf_token,
            },
        )
        return data and data.get("code") == 0
    
    async def coin_video(self, oid: int, num: int = 1) -> bool:
        """
        投币
        
        Args:
            oid: 视频aid
            num: 投币数量 (1或2)
        """
        # MISC-602：投币数量仅允许 1 或 2，越界时夹紧到合法区间
        num = max(1, min(2, int(num)))
        data, _ = await self._http_post(
            "https://api.bilibili.com/x/web-interface/coin/add",
            data={
                "sid": oid,
                "multiply": num,
                "cross_domain": "true",
                "csrf": self._csrf_token,
            },
        )
        return data and data.get("code") == 0
    
    async def fav_video(self, oid: int, fav_id: int = 0) -> bool:
        """收藏视频"""
        # 先获取收藏夹列表
        if fav_id == 0:
            data, _ = await self._http_get(
                "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                params={"up_mid": self.config.bilibili.dede_user_id},
            )
            if data and data.get("data") and data["data"].get("list"):
                fav_id = data["data"]["list"][0].get("id", 0)
        
        if fav_id == 0:
            return False
        
        data, _ = await self._http_post(
            "https://api.bilibili.com/x/v3/fav/resource/deal",
            data={
                "rid": oid,
                "type": 2,  # 视频
                "add_media_ids": fav_id,
                "del_media_ids": "",
                "csrf": self._csrf_token,
            },
        )
        return data and data.get("code") == 0
    
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
    
    async def get_hot_videos(self, page: int = 1) -> Optional[Dict]:
        """获取热门视频"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/web-interface/popular",
            params={"ps": 50, "pn": page},
        )
        return data
    
    async def get_reply_notifications(self) -> Optional[Dict]:
        """获取回复我的评论通知（msgfeed/reply）"""
        data, _ = await self._http_get(
            "https://api.bilibili.com/x/msgfeed/reply",
            params={"platform": "web", "build": 0, "mobi_app": "web"},
        )
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
