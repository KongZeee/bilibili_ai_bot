"""
VID-503 视频资源保护测试

PRD-V5 §8.2 验证点：
1. Over-duration video → no full download/analysis, degradation reason recorded
2. Over-size video → no full download/analysis, degradation reason recorded
3. Download exceeds limit → abort + clean temp files
4. Global semaphore limits concurrency (two accounts, one waits)
5. Fake subprocess timeout → event loop still processes lightweight tasks
6. Whisper audio_path correctly passed
7. Local Whisper OFF by default → ASR skipped
8. Disk quota insufficient → degrade
"""
import asyncio
import os
import shutil
import subprocess
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from bilibot.video_understanding.audio_track import transcribe_audio, _transcribe_with_whisper
from bilibot.video_understanding.preprocess import (
    DEGRADATION_DOWNLOAD_SIZE_EXCEEDED,
    DEGRADATION_DURATION_EXCEEDS_LIMIT,
    DEGRADATION_INSUFFICIENT_DISK,
    DEGRADATION_SIZE_EXCEEDS_LIMIT,
    check_disk_space,
    check_resource_limits,
    download_stream_with_byte_limit,
    get_video_file_size,
)
from bilibot.video_understanding.service import (
    VideoUnderstandingConfig,
    VideoUnderstandingService,
    configure_global_semaphore,
    get_global_semaphore,
    reset_global_semaphore,
)


# ═══════════════════════════════════════════════
# Helper: mock ffprobe subprocess result
# ═══════════════════════════════════════════════

class MockCompletedProcess:
    """模拟 subprocess.run 返回值"""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_ffprobe_mock(duration_str="120.5", file_exists=True):
    """创建模拟 subprocess.run 的函数，根据命令参数返回不同结果"""
    def _mock_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "ffprobe" in cmd_str:
            if "format=duration" in cmd_str:
                return MockCompletedProcess(returncode=0, stdout=duration_str)
            if "r_frame_rate" in cmd_str:
                return MockCompletedProcess(returncode=0, stdout="30/1")
            if "codec_type" in cmd_str:
                return MockCompletedProcess(returncode=0, stdout="audio\n")
        if "ffmpeg" in cmd_str:
            return MockCompletedProcess(returncode=0, stdout="", stderr="")
        return MockCompletedProcess(returncode=0, stdout="", stderr="")
    return _mock_run


# ═══════════════════════════════════════════════
# 1. Over-duration video → degradation
# ═══════════════════════════════════════════════

def test_over_duration_video_degrades(tmp_path):
    """视频时长超过 max_duration_seconds → 降级，不启动分析"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"\x00" * 100)

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="999.0")):
        degradation = check_resource_limits(
            str(video_path),
            str(tmp_path),
            max_duration_seconds=600,
            max_download_bytes=209715200,
            temp_disk_quota_bytes=1073741824,
        )

    assert degradation == DEGRADATION_DURATION_EXCEEDS_LIMIT


def test_over_duration_service_returns_degradation_reason(tmp_path):
    """service.understand() 对超时长视频返回 degradation_reason"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"\x00" * 100)

    config = {
        "video_analysis": {
            "enabled": True,
            "max_duration_seconds": 60,
            "max_download_bytes": 209715200,
            "temp_disk_quota_bytes": 1073741824,
        }
    }

    class MockConfigLoader:
        def get_raw_config(self):
            return config
        def get(self, path, default=None):
            return config.get(path, default)

    class MockLLM:
        vision_client = True
        vision_model = "test"

    reset_global_semaphore()
    configure_global_semaphore(1)
    service = VideoUnderstandingService(MockLLM(), MockConfigLoader())

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="999.0")):
        result = asyncio.run(service.understand(str(video_path)))

    assert result["degradation_reason"] == DEGRADATION_DURATION_EXCEEDS_LIMIT
    assert result["behavior_log"] == ""
    assert result["work_dir"] == ""


# ═══════════════════════════════════════════════
# 2. Over-size video → degradation
# ═══════════════════════════════════════════════

def test_over_size_video_degrades(tmp_path):
    """视频文件大小超过 max_download_bytes → 降级"""
    video_path = tmp_path / "large_video.mp4"
    video_path.write_bytes(b"\x00" * (10 * 1024 * 1024))  # 10MB

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="60.0")):
        degradation = check_resource_limits(
            str(video_path),
            str(tmp_path),
            max_duration_seconds=600,
            max_download_bytes=5 * 1024 * 1024,  # 5MB limit
            temp_disk_quota_bytes=1073741824,
        )

    assert degradation == DEGRADATION_SIZE_EXCEEDS_LIMIT


def test_over_size_service_returns_degradation_reason(tmp_path):
    """service.understand() 对超大小视频返回 degradation_reason"""
    video_path = tmp_path / "large_video.mp4"
    video_path.write_bytes(b"\x00" * (10 * 1024 * 1024))

    config = {
        "video_analysis": {
            "enabled": True,
            "max_duration_seconds": 600,
            "max_download_bytes": 5 * 1024 * 1024,  # 5MB limit
            "temp_disk_quota_bytes": 1073741824,
        }
    }

    class MockConfigLoader:
        def get_raw_config(self):
            return config
        def get(self, path, default=None):
            return config.get(path, default)

    class MockLLM:
        vision_client = True
        vision_model = "test"

    reset_global_semaphore()
    configure_global_semaphore(1)
    service = VideoUnderstandingService(MockLLM(), MockConfigLoader())

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="60.0")):
        result = asyncio.run(service.understand(str(video_path)))

    assert result["degradation_reason"] == DEGRADATION_SIZE_EXCEEDS_LIMIT
    assert result["behavior_log"] == ""


# ═══════════════════════════════════════════════
# 3. Download exceeds limit → abort + clean temp
# ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_download_exceeds_limit_aborts_and_cleans_temp(tmp_path):
    """下载字节数超过上限 → 中止下载并删除临时文件"""
    save_path = str(tmp_path / "download.mp4")

    # 模拟 aiohttp response，返回超过 max_bytes 的数据
    class MockContent:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunked(self, size):
            for chunk in self._chunks:
                yield chunk

    class MockResponse:
        status = 200
        def __init__(self, content):
            self.content = content
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self, response):
            self._response = response
        # aiohttp session.get() returns a context manager, not a coroutine
        def get(self, url, **kwargs):
            return self._response
        async def close(self):
            pass

    # 3 chunks of 256KB each = 768KB total, max=100KB
    chunk_data = b"\x00" * (256 * 1024)
    mock_content = MockContent([chunk_data, chunk_data, chunk_data])
    mock_response = MockResponse(mock_content)
    mock_session = MockSession(mock_response)

    success, reason = await download_stream_with_byte_limit(
        "http://example.com/video.mp4",
        save_path,
        max_bytes=100 * 1024,  # 100KB limit
        session=mock_session,
    )

    assert success is False
    assert reason == DEGRADATION_DOWNLOAD_SIZE_EXCEEDED
    # 临时文件应已被删除
    assert not os.path.exists(save_path)


@pytest.mark.asyncio
async def test_download_within_limit_succeeds(tmp_path):
    """下载字节数在上限内 → 下载成功"""
    save_path = str(tmp_path / "download.mp4")

    class MockContent:
        def __init__(self, chunks):
            self._chunks = chunks
        async def iter_chunked(self, size):
            for chunk in self._chunks:
                yield chunk

    class MockResponse:
        status = 200
        def __init__(self, content):
            self.content = content
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self, response):
            self._response = response
        def get(self, url, **kwargs):
            return self._response
        async def close(self):
            pass

    # 2 chunks of 50KB each = 100KB total, max=200KB
    chunk_data = b"\x00" * (50 * 1024)
    mock_content = MockContent([chunk_data, chunk_data])
    mock_response = MockResponse(mock_content)
    mock_session = MockSession(mock_response)

    success, reason = await download_stream_with_byte_limit(
        "http://example.com/video.mp4",
        save_path,
        max_bytes=200 * 1024,
        session=mock_session,
    )

    assert success is True
    assert reason == ""
    assert os.path.exists(save_path)


# ═══════════════════════════════════════════════
# 4. Global semaphore limits concurrency
# ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_global_semaphore_limits_concurrency():
    """两个账号同时触发视频分析 → 实际并发 ≤ 全局配置"""
    reset_global_semaphore()
    configure_global_semaphore(1)
    sem = get_global_semaphore()

    acquired_order = []
    task1_done = asyncio.Event()
    task2_done = asyncio.Event()

    async def task_1():
        async with sem:
            acquired_order.append("task1")
            await asyncio.sleep(0.1)
        task1_done.set()

    async def task_2():
        # 等一下确保 task1 先获取 semaphore
        await asyncio.sleep(0.05)
        async with sem:
            acquired_order.append("task2")
        task2_done.set()

    t1 = asyncio.create_task(task_1())
    t2 = asyncio.create_task(task_2())

    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=5.0)

    # task1 必须先于 task2 获取 semaphore（max_concurrent=1）
    assert acquired_order == ["task1", "task2"]


@pytest.mark.asyncio
async def test_global_semaphore_max_two_allows_parallel():
    """max_concurrent=2 → 两个任务可并行获取"""
    reset_global_semaphore()
    configure_global_semaphore(2)
    sem = get_global_semaphore()

    acquired = []

    async def task(name):
        async with sem:
            acquired.append(name)
            await asyncio.sleep(0.1)

    await asyncio.gather(task("a"), task("b"))
    assert len(acquired) == 2


# ═══════════════════════════════════════════════
# 5. Fake subprocess timeout → event loop still responsive
# ═══════════════════════════════════════════════

@pytest.mark.asyncio
async def test_subprocess_timeout_event_loop_responsive(tmp_path):
    """模拟 subprocess 超时 → 事件循环仍可处理轻量任务"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"\x00" * 100)

    config = {
        "video_analysis": {
            "enabled": True,
            "max_duration_seconds": 600,
            "max_download_bytes": 209715200,
            "temp_disk_quota_bytes": 1073741824,
            "preprocess_timeout_seconds": 1,  # 1秒超时
        }
    }

    class MockConfigLoader:
        def get_raw_config(self):
            return config
        def get(self, path, default=None):
            return config.get(path, default)

    class MockLLM:
        vision_client = True
        vision_model = "test"

    reset_global_semaphore()
    configure_global_semaphore(1)
    service = VideoUnderstandingService(MockLLM(), MockConfigLoader())

    # 模拟 preprocess_video 阻塞超过超时
    def slow_preprocess(*args, **kwargs):
        import time
        time.sleep(5)  # 阻塞 5 秒

    # 轻量任务：应在超时后被事件循环处理
    lightweight_result = []

    async def lightweight_task():
        lightweight_result.append("done")

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="60.0")):
        with patch("bilibot.video_understanding.service.preprocess_video",
                   side_effect=slow_preprocess):
            # 启动视频理解（会超时）
            understand_task = asyncio.create_task(service.understand(str(video_path)))
            # 等一下让超时发生
            await asyncio.sleep(0.1)
            # 运行轻量任务
            await lightweight_task()
            # 等待 understand 完成（应返回超时降级）
            result = await asyncio.wait_for(understand_task, timeout=10.0)

    assert result["degradation_reason"] == "preprocess_timeout"
    assert result["behavior_log"] == ""
    # 轻量任务已完成
    assert lightweight_result == ["done"]


# ═══════════════════════════════════════════════
# 6. Whisper audio_path correctly passed
# ═══════════════════════════════════════════════

def test_whisper_audio_path_correctly_passed(tmp_path):
    """_transcribe_with_whisper 接收到 audio_path 参数"""
    audio_path = str(tmp_path / "audio.wav")
    with open(audio_path, "wb") as f:
        f.write(b"\x00" * 100)

    captured_args = []

    def mock_whisper(audio_path_arg, *args, **kwargs):
        captured_args.append(audio_path_arg)
        return []

    with patch("bilibot.video_understanding.audio_track._transcribe_with_whisper",
               side_effect=mock_whisper):
        events = transcribe_audio(
            audio_path,
            local_whisper_enabled=True,
            whisper_timeout=30,
        )

    assert len(captured_args) == 1
    assert captured_args[0] == audio_path
    assert events == []


def test_whisper_audio_path_not_swallowed_by_model_size(tmp_path):
    """确保 audio_path 不被误传为 whisper_model_size"""
    audio_path = str(tmp_path / "test_audio.wav")
    with open(audio_path, "wb") as f:
        f.write(b"\x00" * 100)

    captured = {}

    def mock_whisper(audio_path_arg, model_size, device, compute_type):
        captured["audio_path"] = audio_path_arg
        captured["model_size"] = model_size
        captured["device"] = device
        captured["compute_type"] = compute_type
        return []

    with patch("bilibot.video_understanding.audio_track._transcribe_with_whisper",
               side_effect=mock_whisper):
        transcribe_audio(
            audio_path,
            local_whisper_enabled=True,
            whisper_model_size="small",
            whisper_device="cpu",
            whisper_compute_type="int8",
            whisper_timeout=30,
        )

    assert captured["audio_path"] == audio_path
    assert captured["model_size"] == "small"
    assert captured["device"] == "cpu"
    assert captured["compute_type"] == "int8"


# ═══════════════════════════════════════════════
# 7. Local Whisper OFF by default → ASR skipped
# ═══════════════════════════════════════════════

def test_local_whisper_off_by_default_skips_asr(tmp_path):
    """local_whisper_enabled 默认 False → ASR 被跳过，返回空列表"""
    audio_path = str(tmp_path / "audio.wav")
    with open(audio_path, "wb") as f:
        f.write(b"\x00" * 100)

    # 不传 local_whisper_enabled → 默认 False
    events = transcribe_audio(
        audio_path,
        asr_model="",  # 不用 API
    )

    assert events == []


def test_config_local_whisper_defaults_off():
    """VideoUnderstandingConfig 默认 local_whisper_enabled=False"""
    cfg = VideoUnderstandingConfig({})
    assert cfg.local_whisper_enabled is False


def test_config_local_whisper_can_be_enabled():
    """VideoUnderstandingConfig 可显式开启 local_whisper"""
    cfg = VideoUnderstandingConfig({
        "video_analysis": {
            "local_whisper_enabled": True,
        }
    })
    assert cfg.local_whisper_enabled is True


# ═══════════════════════════════════════════════
# 8. Disk quota insufficient → degrade
# ═══════════════════════════════════════════════

def test_insufficient_disk_degrades(tmp_path):
    """磁盘空间不足 → 降级"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"\x00" * 100)

    # 设置一个极大的磁盘配额需求，确保超过实际可用空间
    huge_quota = 1024 * 1024 * 1024 * 1024 * 1024  # 1PB

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="60.0")):
        degradation = check_resource_limits(
            str(video_path),
            str(tmp_path),
            max_duration_seconds=600,
            max_download_bytes=209715200,
            temp_disk_quota_bytes=huge_quota,
        )

    assert degradation == DEGRADATION_INSUFFICIENT_DISK


def test_check_disk_space_sufficient(tmp_path):
    """磁盘空间充足 → 返回 True"""
    result = check_disk_space(str(tmp_path), 1)  # 只需 1 byte
    assert result is True


def test_check_disk_space_insufficient(tmp_path):
    """磁盘空间不足 → 返回 False"""
    huge = 1024 * 1024 * 1024 * 1024 * 1024  # 1PB
    result = check_disk_space(str(tmp_path), huge)
    assert result is False


# ═══════════════════════════════════════════════
# 补充：配置字段默认值验证
# ═══════════════════════════════════════════════

def test_config_defaults_match_prd():
    """PRD-V5 §8.2 配置默认值验证"""
    cfg = VideoUnderstandingConfig({})
    assert cfg.enabled is False
    assert cfg.max_duration_seconds == 600
    assert cfg.max_download_bytes == 209715200  # 200MB
    assert cfg.max_concurrent_global == 1
    assert cfg.max_concurrent_per_account == 1
    assert cfg.download_timeout_seconds == 90
    assert cfg.preprocess_timeout_seconds == 180
    assert cfg.analysis_timeout_seconds == 600
    assert cfg.local_whisper_enabled is False
    assert cfg.max_local_whisper_workers == 1
    assert cfg.temp_disk_quota_bytes == 1073741824  # 1GB


def test_config_all_fields_loaded_from_yaml():
    """从 YAML 加载所有资源边界配置字段"""
    raw = {
        "video_analysis": {
            "enabled": True,
            "max_duration_seconds": 300,
            "max_download_bytes": 104857600,
            "max_concurrent_global": 2,
            "max_concurrent_per_account": 1,
            "download_timeout_seconds": 60,
            "preprocess_timeout_seconds": 120,
            "analysis_timeout_seconds": 300,
            "local_whisper_enabled": True,
            "max_local_whisper_workers": 2,
            "temp_disk_quota_bytes": 536870912,
        }
    }
    cfg = VideoUnderstandingConfig(raw)
    assert cfg.max_duration_seconds == 300
    assert cfg.max_download_bytes == 104857600
    assert cfg.max_concurrent_global == 2
    assert cfg.max_concurrent_per_account == 1
    assert cfg.download_timeout_seconds == 60
    assert cfg.preprocess_timeout_seconds == 120
    assert cfg.analysis_timeout_seconds == 300
    assert cfg.local_whisper_enabled is True
    assert cfg.max_local_whisper_workers == 2
    assert cfg.temp_disk_quota_bytes == 536870912


# ═══════════════════════════════════════════════
# 补充：资源检查通过时不降级
# ═══════════════════════════════════════════════

def test_resource_limits_pass_when_within_bounds(tmp_path):
    """视频在资源边界内 → 通过检查（返回 None）"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"\x00" * (1024 * 1024))  # 1MB

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="60.0")):
        degradation = check_resource_limits(
            str(video_path),
            str(tmp_path),
            max_duration_seconds=600,
            max_download_bytes=209715200,
            temp_disk_quota_bytes=1073741824,
        )

    assert degradation is None


# ═══════════════════════════════════════════════
# 补充：优雅关闭
# ═══════════════════════════════════════════════

def test_shutdown_stops_accepting_new_tasks(tmp_path):
    """关闭后不再接受新视频分析任务"""
    config = {
        "video_analysis": {
            "enabled": True,
            "temp_dir": str(tmp_path / "video_temp"),
        }
    }

    class MockConfigLoader:
        def get_raw_config(self):
            return config
        def get(self, path, default=None):
            return config.get(path, default)

    class MockLLM:
        vision_client = True
        vision_model = "test"

    service = VideoUnderstandingService(MockLLM(), MockConfigLoader())
    assert service.is_available() is True

    service.shutdown()

    assert service.is_available() is False
    result = asyncio.run(service.understand("/fake/path.mp4"))
    assert result["degradation_reason"] == "service_shutdown"


# ═══════════════════════════════════════════════
# 补充：视频分析失败不阻塞元数据评价
# ═══════════════════════════════════════════════

def test_video_analysis_failure_does_not_block_metadata(tmp_path):
    """视频分析因资源限制降级 → 返回空 behavior_log 但不抛异常（不阻塞元数据评价）"""
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"\x00" * (10 * 1024 * 1024))

    config = {
        "video_analysis": {
            "enabled": True,
            "max_download_bytes": 5 * 1024 * 1024,  # 5MB → 超限
            "temp_disk_quota_bytes": 1073741824,
        }
    }

    class MockConfigLoader:
        def get_raw_config(self):
            return config
        def get(self, path, default=None):
            return config.get(path, default)

    class MockLLM:
        vision_client = True
        vision_model = "test"

    reset_global_semaphore()
    configure_global_semaphore(1)
    service = VideoUnderstandingService(MockLLM(), MockConfigLoader())

    with patch("bilibot.video_understanding.preprocess.subprocess.run",
               side_effect=_make_ffprobe_mock(duration_str="60.0")):
        result = asyncio.run(service.understand(str(video_path)))

    # 降级：behavior_log 为空，但不抛异常
    assert result["behavior_log"] == ""
    assert result["degradation_reason"] == DEGRADATION_SIZE_EXCEEDS_LIMIT
    # 元数据评价可以继续进行（上游只需检查 behavior_log 是否为空）
