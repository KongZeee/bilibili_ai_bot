"""
视频接入与预处理模块

职责：
- 接收本地视频路径
- 使用 FFmpeg 提取 FPS、总时长
- 提取音频为 16kHz, 16bit, 单声道 WAV
- 检测无音频轨并返回标记
- PRD-V5 §8.2 VID-503：资源边界检查（时长/大小/磁盘空间）
"""
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger("bilibot.video_u.preprocess")


# PRD-V5 §8.2 VID-503：降级原因常量
DEGRADATION_DURATION_EXCEEDS_LIMIT = "duration_exceeds_limit"
DEGRADATION_SIZE_EXCEEDS_LIMIT = "size_exceeds_limit"
DEGRADATION_DOWNLOAD_SIZE_EXCEEDED = "download_size_exceeded"
DEGRADATION_INSUFFICIENT_DISK = "insufficient_disk"


@dataclass
class PreprocessResult:
    """预处理结果"""

    video_path: str
    audio_path: Optional[str]
    fps: float
    duration: float
    has_audio: bool
    work_dir: str


def _run_ffmpeg(args: list, timeout: int = 120) -> Tuple[int, str, str]:
    """执行 ffmpeg/ffprobe 命令"""
    cmd = [args[0]] + args[1:]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, encoding="utf-8", errors="ignore",
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:
        logger.error(f"运行 {' '.join(cmd)} 失败: {e}")
        return -1, "", str(e)


# ═══ PRD-V5 §8.2 VID-503：资源边界检查 ═══


def get_video_file_size(video_path: str) -> int:
    """获取视频文件大小（字节）"""
    try:
        return os.path.getsize(video_path)
    except Exception:
        return 0


def get_video_duration(video_path: str) -> float:
    """获取视频时长（秒），公开接口"""
    return _get_video_duration(video_path)


def check_disk_space(temp_dir: str, required_bytes: int) -> bool:
    """检查磁盘可用空间是否充足

    Args:
        temp_dir: 临时目录路径
        required_bytes: 需要的可用字节数

    Returns:
        True 表示空间充足
    """
    try:
        os.makedirs(temp_dir, exist_ok=True)
        usage = shutil.disk_usage(temp_dir)
        if usage.free < required_bytes:
            logger.warning(
                f"磁盘空间不足: 可用 {usage.free} bytes < 需要 {required_bytes} bytes"
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"磁盘空间检查失败: {e}")
        return False


def check_resource_limits(
    video_path: str,
    temp_dir: str,
    max_duration_seconds: int,
    max_download_bytes: int,
    temp_disk_quota_bytes: int,
) -> Optional[str]:
    """PRD-V5 §8.2 VID-503：下载前/分析前资源边界检查

    Args:
        video_path: 已下载的视频文件路径
        temp_dir: 临时目录
        max_duration_seconds: 时长上限
        max_download_bytes: 文件大小上限
        temp_disk_quota_bytes: 磁盘配额

    Returns:
        None 表示通过检查；否则返回降级原因字符串
    """
    # 1. 检查磁盘空间
    if not check_disk_space(temp_dir, temp_disk_quota_bytes):
        return DEGRADATION_INSUFFICIENT_DISK

    # 2. 检查视频时长
    duration = _get_video_duration(video_path)
    if max_duration_seconds > 0 and duration > max_duration_seconds:
        logger.warning(
            f"视频时长 {duration:.1f}s 超过上限 {max_duration_seconds}s，降级为元数据分析"
        )
        return DEGRADATION_DURATION_EXCEEDS_LIMIT

    # 3. 检查文件大小
    file_size = get_video_file_size(video_path)
    if max_download_bytes > 0 and file_size > max_download_bytes:
        logger.warning(
            f"视频大小 {file_size} bytes 超过上限 {max_download_bytes} bytes，降级为元数据分析"
        )
        return DEGRADATION_SIZE_EXCEEDS_LIMIT

    return None


async def download_stream_with_byte_limit(
    url: str,
    save_path: str,
    max_bytes: int,
    headers: Optional[dict] = None,
    timeout_seconds: int = 90,
    session=None,
) -> Tuple[bool, str]:
    """PRD-V5 §8.2 VID-503：带字节计数的流式下载

    下载过程中计算实际字节数，超过 max_bytes 立即中止并清理临时文件。

    Args:
        url: 下载地址
        save_path: 保存路径
        max_bytes: 最大字节数
        headers: 请求头
        timeout_seconds: 超时秒数
        session: 可选的 aiohttp ClientSession

    Returns:
        (success, reason): success=True 表示下载成功；
        success=False 时 reason 为失败原因（降级原因常量）
    """
    import aiohttp
    import asyncio

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession()

    try:
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        bytes_downloaded = 0

        try:
            async with session.get(
                url, headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"下载失败: HTTP {resp.status}")
                    return False, f"http_{resp.status}"

                with open(save_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        bytes_downloaded += len(chunk)
                        if bytes_downloaded > max_bytes:
                            logger.warning(
                                f"下载字节数 {bytes_downloaded} 超过上限 {max_bytes}，中止下载"
                            )
                            f.close()
                            try:
                                os.remove(save_path)
                            except Exception:
                                pass
                            return False, DEGRADATION_DOWNLOAD_SIZE_EXCEEDED
                        f.write(chunk)

            logger.info(f"下载完成: {save_path} ({bytes_downloaded} bytes)")
            return True, ""
        except asyncio.TimeoutError:
            logger.warning(f"下载超时（{timeout_seconds}s）")
            try:
                os.remove(save_path)
            except Exception:
                pass
            return False, "download_timeout"
    finally:
        if owns_session:
            await session.close()


def _get_video_fps(video_path: str) -> float:
    code, stdout, stderr = _run_ffmpeg(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ]
    )
    if code == 0 and stdout.strip():
        try:
            parts = stdout.strip().split("/")
            if len(parts) == 2:
                return float(parts[0]) / float(parts[1])
            return float(parts[0])
        except Exception:
            pass
    logger.warning(f"无法获取 FPS，使用默认值 30: {stderr[:120]}")
    return 30.0


def _get_video_duration(video_path: str) -> float:
    code, stdout, stderr = _run_ffmpeg(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ]
    )
    if code == 0 and stdout.strip():
        try:
            return float(stdout.strip())
        except ValueError:
            pass
    logger.warning(f"无法获取时长，使用默认值 0: {stderr[:120]}")
    return 0.0


def _has_audio_stream(video_path: str) -> bool:
    code, stdout, _ = _run_ffmpeg(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path,
        ]
    )
    return code == 0 and "audio" in stdout.lower()


def _extract_audio(video_path: str, output_wav: str) -> bool:
    code, _, stderr = _run_ffmpeg(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", output_wav],
        timeout=300,
    )
    if code != 0:
        logger.error(f"音频提取失败: {stderr[:200]}")
        return False
    return os.path.exists(output_wav) and os.path.getsize(output_wav) > 0


def preprocess_video(
    video_path: str,
    temp_dir: str,
    task_id: Optional[str] = None,
    preprocess_timeout: int = 180,
) -> PreprocessResult:
    """
    视频预处理入口

    Args:
        video_path: 输入视频文件路径
        temp_dir: 临时目录根
        task_id: 可选任务ID
        preprocess_timeout: 预处理超时秒数（PRD-V5 §8.2 VID-503）
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    task_id = task_id or uuid.uuid4().hex[:12]
    work_dir = os.path.join(temp_dir, task_id)
    os.makedirs(work_dir, exist_ok=True)

    # 复制原始视频到工作目录（方便清理）
    local_video = os.path.join(work_dir, os.path.basename(video_path))
    if os.path.abspath(video_path) != os.path.abspath(local_video):
        shutil.copy2(video_path, local_video)
        video_path = local_video

    fps = _get_video_fps(video_path)
    duration = _get_video_duration(video_path)
    has_audio = _has_audio_stream(video_path)

    audio_path: Optional[str] = None
    if has_audio:
        audio_path = os.path.join(work_dir, "audio.wav")
        if not _extract_audio(video_path, audio_path):
            audio_path = None
            has_audio = False
            logger.warning("音频提取失败，按无声视频降级处理")
    else:
        logger.info("视频无音频轨，触发无声视频降级机制")

    logger.info(f"预处理完成: fps={fps:.2f}, duration={duration:.2f}s, has_audio={has_audio}")
    return PreprocessResult(
        video_path=video_path, audio_path=audio_path, fps=fps,
        duration=duration, has_audio=has_audio, work_dir=work_dir,
    )
