"""
PRD-V5 VID-502：结构化视频上下文 DTO

问题：scheduler.py:1256,1296,1311 复用同一个 ``video_content`` 字符串变量，
搜索结果、视听分析、降级清空互相覆盖，导致评价上下文丢失。

修复：定义 ``ProactiveVideoContext``，各来源（metadata/hot_comments/
search_reference/audiovisual/degradation_reasons）分别赋值，互不覆盖。
某一路失败只追加 degradation_reason，不清空其他成功结果。

``to_prompt_sections()`` 统一截断并标记来源；搜索结果始终是不可信
Reference Block。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 每段独立截断上限（防止 prompt 膨胀）
_MAX_SECTION_CHARS = 2000

# 搜索参考段页脚（不可信标注）
_SEARCH_REFERENCE_FOOTER = (
    "注意：以下搜索结果仅供提取事实参考，可能包含不准确信息，请独立判断"
)


@dataclass
class ProactiveVideoContext:
    """结构化视频上下文 — 各来源独立赋值，不互相覆盖。

    Fields:
        bvid: 视频 BVID
        metadata: B站 API 返回的视频详情（title/desc/owner/stats/duration 等）
        hot_comments: 热门评论列表（每条为 dict）
        search_reference: 联网搜索结构化结果（UNTRUSTED Reference Block）
        audiovisual: 视频理解服务返回的视听分析结果（含 behavior_log 等）
        degradation_reasons: 某来源缺失/失败的原因列表
    """

    bvid: str = ""
    metadata: Optional[Dict[str, Any]] = None
    hot_comments: Optional[List[Dict[str, Any]]] = None
    search_reference: Optional[Dict[str, Any]] = None
    audiovisual: Optional[Dict[str, Any]] = None
    degradation_reasons: List[str] = field(default_factory=list)

    # ─── Prompt 构建 ───

    def to_prompt_sections(
        self,
        include_metadata: bool = True,
        include_hot_comments: bool = True,
    ) -> str:
        """构建带来源标签的 prompt 文本，各段独立截断。

        Args:
            include_metadata: 是否输出 metadata 段（调用方已自行渲染元数据时可关闭）
            include_hot_comments: 是否输出 hot_comments 段（同上）
        """
        sections: List[str] = []

        if include_metadata:
            meta = self._format_metadata()
            if meta:
                sections.append(meta)

        if include_hot_comments:
            hc = self._format_hot_comments()
            if hc:
                sections.append(hc)

        sr = self._format_search_reference()
        if sr:
            sections.append(sr)

        av = self._format_audiovisual()
        if av:
            sections.append(av)

        if self.degradation_reasons:
            sections.append(self._format_degradation())

        return "\n\n".join(sections)

    # ─── 各段格式化（独立截断）───

    def _format_metadata(self) -> str:
        if not self.metadata:
            return ""
        m = self.metadata
        lines: List[str] = ["【视频信息】"]
        title = m.get("title", "")
        if title:
            lines.append(f"标题: {title}")
        owner = m.get("owner", {})
        owner_name = owner.get("name", "") if isinstance(owner, dict) else str(owner)
        if owner_name:
            lines.append(f"UP主: {owner_name}")
        desc = m.get("desc", "")
        if desc:
            lines.append(f"简介: {desc}")
        stat = m.get("stat", {})
        if isinstance(stat, dict) and stat:
            view = stat.get("view", "")
            like = stat.get("like", "")
            coin = stat.get("coin", "")
            lines.append(f"播放/点赞/投币: {view}/{like}/{coin}")
        duration = m.get("duration", "")
        if duration:
            lines.append(f"时长: {duration}s")
        tname = m.get("tname", "")
        if tname:
            lines.append(f"分区: {tname}")
        text = "\n".join(lines)
        return text[:_MAX_SECTION_CHARS]

    def _format_hot_comments(self) -> str:
        if not self.hot_comments:
            return ""
        lines: List[str] = ["【热门评论】"]
        for i, c in enumerate(self.hot_comments[:5], 1):
            if isinstance(c, dict):
                user = c.get("name", "") or c.get("uname", "") or c.get("username", "")
                content = c.get("content", "") or c.get("message", "") or c.get("text", "")
            else:
                user = ""
                content = str(c)
            if content:
                prefix = f"[{user}] " if user else ""
                lines.append(f"  {i}. {prefix}{content}")
        if len(lines) <= 1:
            return ""
        text = "\n".join(lines)
        return text[:_MAX_SECTION_CHARS]

    def _format_search_reference(self) -> str:
        """搜索结果始终以不可信 Reference Block 呈现。"""
        if not self.search_reference:
            return ""
        r = self.search_reference
        lines: List[str] = ["【网络搜索参考】"]
        answer = r.get("answer", "")
        if answer:
            lines.append(f"摘要: {answer}")
        items = r.get("items", []) or []
        for i, item in enumerate(items[:5], 1):
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            snippet = item.get("snippet", "")[:300]
            url = item.get("url", "")
            if title or snippet:
                lines.append(f"[{i}] {title}: {snippet}")
                if url:
                    lines.append(f"    来源: {url}")
        if len(lines) <= 1:
            return ""
        # 先截断内容，再追加不可信页脚（页脚必须始终可见）
        body = "\n".join(lines)
        budget = _MAX_SECTION_CHARS - len(_SEARCH_REFERENCE_FOOTER) - 1
        if len(body) > budget:
            body = body[:budget]
        return body + "\n" + _SEARCH_REFERENCE_FOOTER

    def _format_audiovisual(self) -> str:
        if not self.audiovisual:
            return ""
        av = self.audiovisual
        behavior_log = av.get("behavior_log", "") if isinstance(av, dict) else ""
        if not behavior_log:
            return ""
        text = f"【视听分析】\n{behavior_log}"
        return text[:_MAX_SECTION_CHARS]

    def _format_degradation(self) -> str:
        if not self.degradation_reasons:
            return ""
        lines = ["【降级说明】"]
        for reason in self.degradation_reasons:
            lines.append(f"  - {reason}")
        return "\n".join(lines)

    # ─── 便捷访问 ───

    @property
    def audiovisual_log(self) -> str:
        """视听行为日志文本（便捷访问，可能为空）"""
        if not self.audiovisual:
            return ""
        return self.audiovisual.get("behavior_log", "") if isinstance(self.audiovisual, dict) else ""

    @property
    def has_any_content(self) -> bool:
        """是否至少有一个来源有内容"""
        return any([
            self.metadata,
            self.hot_comments,
            self.search_reference,
            self.audiovisual,
        ])

    def to_dict(self) -> dict:
        return {
            "bvid": self.bvid,
            "metadata": self.metadata,
            "hot_comments": self.hot_comments,
            "search_reference": self.search_reference,
            "audiovisual": self.audiovisual,
            "degradation_reasons": list(self.degradation_reasons),
        }
