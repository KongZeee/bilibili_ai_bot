"""
音频轨模块

职责：
- 优先通过 OpenAI 兼容 API 进行 ASR（配置 asr_model 时）
- 未配置 API 时回退到本地 faster-whisper（需 local_whisper_enabled=true）
- 输出带时间戳的 JSON 片段
- 对长句按 8 秒最大跨度进行切分
- 无对白时返回空列表，由上层降级处理
"""
import base64
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import List, Optional, Union

from bilibot.llm.provider import ASRResponseError, extract_asr_transcript

logger = logging.getLogger("bilibot.video_u.audio")

# PRD-V5 §8.2 VID-503：本地 Whisper 独立工作池（懒初始化）
_whisper_executor: Optional[ThreadPoolExecutor] = None
_whisper_executor_workers: int = 0


@dataclass
class AudioEvent:
    """音频事件

    source: 文本来源。
        - "asr"：音频转写（声音轨）
        - "subtitle"：番剧字幕识别（替代声音轨，番剧一般都有字幕）
    """

    start: float
    end: float
    text: str
    source: str = "asr"


@dataclass
class ASRTranscriptionResult:
    """Structured ASR outcome used by completeness-sensitive callers."""

    events: List[AudioEvent]
    status: str
    error_code: str = ""
    error_reason: str = ""
    retryable: bool = False
    duration_seconds: float = 0.0
    timestamp_clamped_count: int = 0


class ASRTranscriptionError(RuntimeError):
    """Retryable ASR failure that must not be archived as complete extraction."""

    def __init__(self, code: str, reason: str, *, retryable: bool = True):
        self.code = code
        self.reason = reason
        self.retryable = retryable
        self.work_dir = ""
        super().__init__(f"{code}: {reason}")


def _estimate_audio_duration(audio_path: str) -> float:
    """使用 ffprobe 估算音频时长"""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, encoding="utf-8", errors="ignore",
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except Exception as e:
        logger.warning(f"估算音频时长失败: {e}")
    return 0.0


def _normalize_audio_timestamps(
    events: List[AudioEvent], duration_seconds: float
) -> tuple[List[AudioEvent], int]:
    """Clamp provider timestamps to the measured track without discarding text."""

    if duration_seconds <= 0:
        return events, 0

    normalized: List[AudioEvent] = []
    clamped = 0
    for event in events:
        start = min(duration_seconds, max(0.0, float(event.start)))
        end = min(duration_seconds, max(0.0, float(event.end)))
        end = max(start, end)
        if start != event.start or end != event.end:
            clamped += 1
        normalized.append(
            AudioEvent(
                start=round(start, 2),
                end=round(end, 2),
                text=event.text,
                source=event.source,
            )
        )
    return normalized, clamped


def _split_segment(start: float, end: float, text: str, max_span: float = 8.0) -> List[AudioEvent]:
    """将跨度超过 max_span 的句子按字符数均分"""
    if end - start <= max_span or not text:
        return [AudioEvent(start=start, end=end, text=text)]

    chars = list(text)
    if len(chars) <= 1:
        return [AudioEvent(start=start, end=end, text=text)]

    n_parts = max(2, int((end - start) / max_span) + 1)
    part_size = len(chars) // n_parts
    events = []
    total_duration = end - start
    for i in range(n_parts):
        c_start = i * part_size
        c_end = len(chars) if i == n_parts - 1 else (i + 1) * part_size
        sub_text = "".join(chars[c_start:c_end]).strip()
        sub_start = start + total_duration * i / n_parts
        sub_end = start + total_duration * (i + 1) / n_parts if i < n_parts - 1 else end
        events.append(AudioEvent(start=round(sub_start, 2), end=round(sub_end, 2), text=sub_text))
    return events


def _convert_to_mp3(audio_path: str) -> str:
    """使用 ffmpeg 将音频转为 MP3，压缩 ASR 上传大小"""
    base, _ = os.path.splitext(audio_path)
    mp3_path = f"{base}_compressed.mp3"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", mp3_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=300, encoding="utf-8", errors="ignore",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"MP3 压缩失败: {proc.stderr[:200]}")
    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise RuntimeError("MP3 压缩后文件为空")
    logger.info(
        f"音频已压缩为 MP3: {os.path.getsize(audio_path) / 1024 / 1024:.2f}MB -> "
        f"{os.path.getsize(mp3_path) / 1024 / 1024:.2f}MB"
    )
    return mp3_path


def _transcribe_with_api(audio_path: str, asr_model: str, asr_api_key: str, asr_base_url: str) -> List[AudioEvent]:
    """通过 OpenAI 兼容 ASR API 转写音频"""
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise RuntimeError(f"openai 未安装: {e}")

    import asyncio

    client = AsyncOpenAI(api_key=asr_api_key, base_url=asr_base_url)

    upload_path = audio_path
    mime_type = "audio/wav"
    if os.path.getsize(audio_path) > 2 * 1024 * 1024:
        upload_path = _convert_to_mp3(audio_path)
        mime_type = "audio/mpeg"

    with open(upload_path, "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")

    async def _request():
        # PRD 4.2：确保 AsyncOpenAI 客户端在请求结束后被关闭，避免资源泄漏
        try:
            response = await client.chat.completions.create(
                model=asr_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:{mime_type};base64,{audio_base64}",
                                },
                            }
                        ],
                    }
                ],
                extra_body={"asr_options": {"language": "zh"}},
            )
            return response
        finally:
            try:
                await client.close()
            except Exception:
                pass

    response = asyncio.run(_request())
    events: List[AudioEvent] = []

    try:
        text = extract_asr_transcript(response)
    except ASRResponseError as response_error:
        logger.warning(
            "ASR API 响应无可用转写: code=%s reason=%s",
            response_error.code,
            response_error.reason,
        )
        raise

    if not text:
        logger.info("ASR API 明确返回无语音: status=no_speech")
        return events

    duration = _estimate_audio_duration(audio_path)
    if duration and duration > 8.0:
        events.extend(_split_segment(0.0, duration, text))
    else:
        events.append(AudioEvent(start=0.0, end=duration or 0.0, text=text))

    return events


def _transcribe_with_whisper(
    audio_path: str, whisper_model_size: str, whisper_device: str, whisper_compute_type: str
) -> List[AudioEvent]:
    """使用 faster-whisper 本地转写"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(f"faster-whisper 未安装: {e}")

    model = WhisperModel(whisper_model_size, device=whisper_device, compute_type=whisper_compute_type)
    segments, _ = model.transcribe(
        audio_path, beam_size=5, best_of=5, condition_on_previous_text=True, word_timestamps=False
    )

    events: List[AudioEvent] = []
    for seg in segments:
        start = round(float(seg.start), 2)
        end = round(float(seg.end), 2)
        text = seg.text.strip()
        if not text:
            continue
        if end - start > 8.0:
            events.extend(_split_segment(start, end, text))
        else:
            events.append(AudioEvent(start=start, end=end, text=text))

    return events


def _get_whisper_executor(max_workers: int = 1) -> ThreadPoolExecutor:
    """获取或创建本地 Whisper 独立工作池"""
    global _whisper_executor, _whisper_executor_workers
    if _whisper_executor is None or _whisper_executor_workers != max_workers:
        if _whisper_executor is not None:
            _whisper_executor.shutdown(wait=False)
        _whisper_executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="whisper-asr"
        )
        _whisper_executor_workers = max_workers
        logger.info(f"本地 Whisper 工作池已创建: max_workers={max_workers}")
    return _whisper_executor


def shutdown_whisper_executor() -> None:
    """关闭本地 Whisper 工作池（优雅关闭时调用）"""
    global _whisper_executor, _whisper_executor_workers
    if _whisper_executor is not None:
        _whisper_executor.shutdown(wait=False)
        _whisper_executor = None
        _whisper_executor_workers = 0
        logger.info("本地 Whisper 工作池已关闭")


def transcribe_audio(
    audio_path: str,
    asr_model: str = "",
    asr_api_key: str = "",
    asr_base_url: str = "",
    whisper_model_size: str = "base",
    whisper_device: str = "cpu",
    whisper_compute_type: str = "int8",
    local_whisper_enabled: bool = False,
    max_local_whisper_workers: int = 1,
    whisper_timeout: int = 600,
    return_result: bool = False,
    raise_on_error: bool = False,
) -> Union[List[AudioEvent], ASRTranscriptionResult]:
    """
    音频轨入口

    Args:
        audio_path: 音频文件路径
        asr_model: API ASR 模型名（非空则用 API）
        local_whisper_enabled: 是否启用本地 faster-whisper（默认 False）
        max_local_whisper_workers: 本地 Whisper 工作池大小
        whisper_timeout: 本地 Whisper 超时秒数

    Returns:
        音频事件列表，识别失败或无对白返回空列表
    """
    mode = "skipped"

    def _return(result: ASRTranscriptionResult):
        return result if return_result else result.events

    def _run_local_whisper() -> List[AudioEvent]:
        logger.info("使用本地 faster-whisper ASR（独立工作池+超时）")
        executor = _get_whisper_executor(max_local_whisper_workers)
        future = executor.submit(
            _transcribe_with_whisper,
            audio_path,
            whisper_model_size,
            whisper_device,
            whisper_compute_type,
        )
        try:
            return future.result(timeout=whisper_timeout)
        except FutureTimeoutError as exc:
            logger.warning(f"本地 Whisper 超时（{whisper_timeout}s）")
            future.cancel()
            raise ASRTranscriptionError(
                "ASR_LOCAL_TIMEOUT",
                f"local Whisper exceeded {whisper_timeout}s",
            ) from exc

    try:
        if asr_model:
            mode = "api"
            logger.info(f"使用 API ASR 模型: {asr_model}")
            try:
                events = _transcribe_with_api(
                    audio_path, asr_model, asr_api_key, asr_base_url
                )
            except Exception as api_error:
                if not local_whisper_enabled:
                    raise
                logger.warning(
                    "API ASR 未完成，回退本地 Whisper: error=%s code=%s",
                    type(api_error).__name__,
                    getattr(api_error, "code", ""),
                )
                mode = "local_fallback"
                events = _run_local_whisper()
        elif local_whisper_enabled:
            mode = "local"
            events = _run_local_whisper()
        else:
            # PRD-V5 §8.2 VID-503：本地 Whisper 默认关闭，未开启时跳过 ASR
            logger.info("本地 Whisper 未启用（local_whisper_enabled=false），跳过 ASR")
            return _return(ASRTranscriptionResult([], "skipped"))
        logger.info(f"ASR 完成: {len(events)} 段")
        duration_seconds = _estimate_audio_duration(audio_path)
        events, timestamp_clamped_count = _normalize_audio_timestamps(
            events, duration_seconds
        )
        if timestamp_clamped_count:
            logger.warning(
                "ASR timestamps clamped to measured audio duration: segments=%s duration=%.2fs",
                timestamp_clamped_count,
                duration_seconds,
            )
        return _return(
            ASRTranscriptionResult(
                events,
                "ok" if events else "no_speech",
                duration_seconds=duration_seconds,
                timestamp_clamped_count=timestamp_clamped_count,
            )
        )
    except Exception as e:
        if isinstance(e, ASRTranscriptionError):
            error = e
        elif isinstance(e, ASRResponseError):
            error = ASRTranscriptionError(e.code, e.reason)
        else:
            if mode == "api":
                code = "ASR_API_REQUEST_FAILED"
            elif mode == "local_fallback":
                code = "ASR_LOCAL_FALLBACK_FAILED"
            else:
                code = "ASR_LOCAL_FAILED"
            error = ASRTranscriptionError(code, f"{type(e).__name__}: {e}")
        logger.error(
            "ASR 失败: code=%s retryable=%s reason=%s",
            error.code,
            error.retryable,
            error.reason,
        )
        if raise_on_error:
            if error is e:
                raise
            raise error from e
        return _return(
            ASRTranscriptionResult(
                [], "failed", error.code, error.reason, error.retryable
            )
        )
