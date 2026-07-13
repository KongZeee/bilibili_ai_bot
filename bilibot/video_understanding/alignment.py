"""
时序缝合与对齐引擎

职责：
- 将音频事件与视觉事件按时间对齐
- 每个音频片段 [start, end] 匹配落在 [start-1.5, end+1.5] 的视觉帧
- 长段空白（>30秒无音频也无视觉）插入静止提示
- 生成 Markdown 结构化行为日志
"""
import logging
from dataclasses import dataclass
from typing import List

from .audio_track import AudioEvent
from .visual_track import VisualEvent

logger = logging.getLogger("bilibot.video_u.alignment")


@dataclass
class TimeBlock:
    """时序事件块"""

    start: float
    end: float
    audio: List[AudioEvent]
    visuals: List[VisualEvent]
    is_gap_fill: bool = False


def _format_time(seconds: float) -> str:
    seconds = max(0, seconds)
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes:02d}:{secs:02d}"


def _match_visuals(
    audio_event: AudioEvent,
    visual_events: List[VisualEvent],
    tolerance: float = 1.5,
) -> List[VisualEvent]:
    matched = []
    lower = audio_event.start - tolerance
    upper = audio_event.end + tolerance
    for ve in visual_events:
        if lower <= ve.timestamp <= upper:
            matched.append(ve)
    return matched


def align_events(
    audio_events: List[AudioEvent],
    visual_events: List[VisualEvent],
    duration: float,
    is_static: bool = False,
) -> List[TimeBlock]:
    """对齐音频与视觉事件"""
    blocks: List[TimeBlock] = []
    if not audio_events and not visual_events:
        logger.warning("无任何音频或视觉事件")
        return blocks

    if is_static and visual_events:
        first_desc = visual_events[0].description
        for ve in visual_events:
            ve.description = first_desc

    for ae in audio_events:
        matched = _match_visuals(ae, visual_events)
        blocks.append(
            TimeBlock(start=ae.start, end=ae.end, audio=[ae], visuals=matched)
        )

    if not audio_events and visual_events:
        for ve in visual_events:
            blocks.append(
                TimeBlock(
                    start=ve.timestamp, end=ve.timestamp, audio=[], visuals=[ve]
                )
            )

    blocks.sort(key=lambda b: b.start)

    merged: List[TimeBlock] = []
    for block in blocks:
        if not merged:
            merged.append(block)
            continue
        last = merged[-1]
        if block.start <= last.end + 1.0:
            last.end = max(last.end, block.end)
            last.audio.extend(block.audio)
            existing = {v.image_path for v in last.visuals}
            for v in block.visuals:
                if v.image_path not in existing:
                    last.visuals.append(v)
                    existing.add(v.image_path)
        else:
            merged.append(block)

    final_blocks: List[TimeBlock] = []
    prev_end = 0.0
    for block in merged:
        if block.start - prev_end > 30.0:
            final_blocks.append(
                TimeBlock(
                    start=prev_end, end=block.start, audio=[], visuals=[], is_gap_fill=True
                )
            )
        final_blocks.append(block)
        prev_end = block.end

    if duration - prev_end > 30.0:
        final_blocks.append(
            TimeBlock(start=prev_end, end=duration, audio=[], visuals=[], is_gap_fill=True)
        )

    return final_blocks


def build_behavior_log(
    blocks: List[TimeBlock], is_static: bool = False, no_audio: bool = False
) -> str:
    """生成 Markdown 结构化行为日志"""
    lines = ["### 视频结构化行为日志\n"]

    if is_static:
        lines.append("> 【系统提示】：该视频画面全局基本静止。\n")
    if no_audio:
        lines.append("> 【系统提示】：该视频无人类对白，视觉轨独立分析。\n")

    for block in blocks:
        start_str = _format_time(block.start)
        end_str = _format_time(block.end)
        lines.append(f"* [{start_str} - {end_str}]")

        if block.is_gap_fill:
            lines.append("  - 【系统提示】：画面保持静止，无声音。")
            continue

        for ae in block.audio:
            if getattr(ae, "source", "asr") == "subtitle":
                lines.append(f"  - 【字幕】{ae.text}")
            else:
                lines.append(f"  - 【听到声音】：{ae.text}")

        for ve in block.visuals:
            lines.append(f"  - 【看到画面】：{ve.description}")

        if not block.audio and not block.visuals:
            lines.append("  - 【系统提示】：该时段无有效视听信息。")

        lines.append("")

    return "\n".join(lines)
