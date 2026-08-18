"""LLM prompt templates for companion life features."""

from __future__ import annotations

from typing import Any, Dict, List


def _clip(text: str, n: int = 800) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def build_daily_plan_prompt(
    *,
    date: str,
    weekday: str,
    persona_name: str,
    persona_prompt: str,
    life_background: str,
    interests: List[str],
    energy: int,
    mood_bias: str,
    sleep: str,
    dream_afterglow: str,
    item_count: int,
) -> tuple[str, str]:
    system = (
        "你是角色日程规划助手。为拟人化 B 站 AI 角色生成「像真人一天」的日程。"
        "只输出 JSON，不要 Markdown 代码块。"
    )
    interest_line = "、".join(interests[:8]) if interests else "B站视频、日常生活"
    user = f"""日期：{date}（{weekday}）
角色名：{persona_name or "Bot"}
人设摘要：{_clip(persona_prompt, 600)}
生活背景：{_clip(life_background, 400) or "普通青年，喜欢上网看 B 站"}
兴趣：{interest_line}
当前精力：{energy}/100
心情倾向：{mood_bias or "平稳"}
睡眠：{sleep or "正常"}
梦境余韵：{_clip(dream_afterglow, 200) or "无"}

请生成约 {item_count} 条日程，覆盖起床到入睡。
要求：
1. 第三人称活动描述，具体可感，避免空洞口号
2. 时段不重叠，time/end 用 HH:MM
3. 可包含刷 B 站、摸鱼、休息、创作念头，但不要编造真实约会/会议
4. message_seed 可为空；有则是可对外分享的短念头（一句话）

输出 JSON：
{{
  "schedule": [
    {{"time":"07:30","end":"08:00","activity":"...","mood":"...","message_seed":"","basis":"routine|interest|rest|creative","confidence":0.7}}
  ]
}}
"""
    return system, user


def build_detail_prompt(
    *,
    window: str,
    activity: str,
    mood: str,
    persona_name: str,
    energy: int,
    evidence: str = "",
) -> tuple[str, str]:
    system = (
        "你是角色生活细化助手。把一个时段展开成微事件。只输出 JSON。"
        "日程是计划，不是已经发生的事实；只有证据块明确记录完成的事才能用完成时。"
    )
    user = f"""角色：{persona_name or "Bot"}
时段：{window}
活动：{activity}
情绪：{mood}
精力：{energy}/100
真实经历证据（可能为空）：
{_clip(evidence, 1200) or "（暂无已完成事件证据）"}

硬性规则：
1. 当前或未来安排使用“准备、打算、可能、正在”，不要把计划写成已经完成。
2. 不得声称看完视频、发出动态、回复评论或完成其他平台操作，除非证据明确支持。
3. events 同时可包含已发生的小事和接下来准备做的事，但要在措辞中区分。

输出：
{{
  "summary": "该时段一两句概述",
  "events": ["微事件1","微事件2"],
  "proactive_hooks": ["可选：适合发动态或评论的话题种子，0-2条"]
}}
"""
    return system, user


def build_dream_prompt(
    *,
    persona_name: str,
    persona_prompt: str,
    fragments: List[str],
    plan_summary: str,
    diary_hint: str,
    memory_evidence: str = "",
) -> tuple[str, str]:
    system = (
        "你是角色梦境生成器。生成「醒来后仍残留」的梦，有情感线与具体碎片。"
        "可轻度呼应近期经历，但不要编造记忆里没有的具体视频/番名细节。"
        "只输出 JSON，不要解释。"
    )
    frag = "；".join(fragments[:12]) if fragments else "无"
    mem = _clip(memory_evidence, 900)
    mem_line = f"\n近期记忆/经历（可选呼应，勿编造）：\n{mem}\n" if mem else ""
    user = f"""角色：{persona_name or "Bot"}
人设：{_clip(persona_prompt, 400)}
近期碎片：{frag}
今日日程摘要：{_clip(plan_summary, 300)}
近日记提示：{_clip(diary_hint, 200) or "无"}
{mem_line}
输出：
{{
  "dream_type": "温柔日常|奇幻|荒诞|怀旧|悬疑 之一",
  "factors": ["具体碎片1","碎片2","碎片3"],
  "content": "180-500字第一人称梦境叙述，有起承转合，停在将醒处",
  "afterglow": "醒来后身体/情绪余韵一句话",
  "label": "短标题",
  "mood": "平稳|恍惚|柔和|低落|敏感|轻快 之一",
  "energy_delta": -8到4的整数,
  "duration_hours": 3到8的整数
}}
"""
    return system, user


def build_diary_prompt(
    *,
    date: str,
    persona_name: str,
    persona_prompt: str,
    plan_summary: str,
    evidence: str,
    dream_summary: str,
    energy: int,
    mood_bias: str,
) -> tuple[str, str]:
    system = (
        "你是角色本人，写私密日记。第一人称，像真人随笔，不要报告体。"
        "只输出 JSON。"
    )
    user = f"""日期：{date}
我是：{persona_name or "Bot"}
人设：{_clip(persona_prompt, 400)}
今日精力：{energy} 心情倾向：{mood_bias}
今日日程：{_clip(plan_summary, 400)}
今日证据（看过/做过/互动/记忆召回，勿编造证据外事件；记忆块可含近期视频/番剧/评论/动态）：
{_clip(evidence, 1800) or "（今天比较平淡）"}
梦境余韵：{_clip(dream_summary, 300) or "无"}

输出：
{{
  "summary": "一句话概括",
  "body": "200-600字日记正文",
  "share_seed": "若愿意发动态，可公开的一句种子；否则空字符串",
  "tags": ["标签1","标签2"],
  "dream_fragments": ["从梦或今日留下的具体碎片，0-5个"]
}}
"""
    return system, user


def build_explore_query_prompt(
    *,
    persona_name: str,
    interests: List[str],
    activity: str,
    mood_bias: str,
    recent_topics: str,
    plan_summary: str = "",
) -> tuple[str, str]:
    system = (
        "你为拟人角色生成「可联网检索」的搜索关键词。"
        "只输出 JSON，不要解释。"
    )
    interest_line = "、".join(interests[:10]) if interests else "科技、动漫、B站、日常生活"
    user = f"""角色：{persona_name or "Bot"}
兴趣方向：{interest_line}
当前活动（仅作氛围，不要把虚构日常当事实去搜）：{activity or "空闲"}
心情：{mood_bias or "平稳"}
今日日程摘要：{_clip(plan_summary, 200) or "无"}
最近念头/话题：{_clip(recent_topics, 300) or "无"}

硬性要求：
1. query 必须是**公开信息/百科/新闻/作品/知识点**，搜索引擎能命中真实网页
2. **禁止**虚构个人隐私或无法检索的句子，例如：
   - 「XX今天做了什么」「XX现在在干嘛」
   - 「我的日程」「值得分享的事」
   - 只含角色私生活、无公开实体的问句
3. 优先形式：
   - 「作品名/题材 + 设定/百科/剧情/背景」
   - 「兴趣领域 + 2024/2025/最新/入门/推荐」
   - 「具体概念 + 是什么/怎么做」
4. query 8–30 字为宜，中文优先，可带专有名词
5. motive 用角色第一人称写「为什么想了解」（可带人设口吻），但 query 本身要可搜

示例（仅示范风格，勿照抄）：
	- query:「ATRI 亚托莉 世界观设定 百科」 motive:「想再确认一下角色背景设定」
	- query:「B站 2025 AI 区 热门话题」 motive:「看看最近大家在聊什么」

输出：
{{"query":"...","motive":"..."}}
"""
    return system, user


def build_explore_note_prompt(
    *,
    query: str,
    motive: str,
    results_text: str,
    persona_prompt: str,
) -> tuple[str, str]:
    system = (
        "你是角色本人，读完搜索结果后写探索笔记。"
        "只输出 JSON。若结果为空或无关，诚实写没搜到，不要编造网页内容。"
    )
    user = f"""人设：{_clip(persona_prompt, 300)}
我搜了：{query}
动机：{motive}
结果：
{_clip(results_text, 2000) or "（无结果/搜索失败）"}

输出：
{{
  "impression": "我的观感 2-5 句；无结果时说明没搜到",
  "self_link": "与我兴趣/日程/创作的关联（可弱关联）",
  "should_share": true或false,
  "highlights": ["从结果摘到的要点1","要点2"]
}}
"""
    return system, user


def build_creative_project_prompt(
    *,
    persona_name: str,
    persona_prompt: str,
    inspiration: str,
) -> tuple[str, str]:
    system = "你为角色开启一个私下创作项目。只输出 JSON。"
    user = f"""角色：{persona_name or "Bot"}
人设：{_clip(persona_prompt, 400)}
灵感：{_clip(inspiration, 400)}

输出：
{{
  "title":"作品名",
  "work_type":"短篇小说|散文|诗歌|短剧 之一",
  "premise":"一句话设定",
  "tone":"基调",
  "target_chars":800到2500的整数,
  "outline":["要点1","要点2","要点3"],
  "next_hint":"下一段写什么"
}}
"""
    return system, user


def build_creative_chunk_prompt(
    *,
    title: str,
    work_type: str,
    premise: str,
    outline: List[str],
    previous: str,
    next_hint: str,
    budget: int,
    persona_prompt: str,
) -> tuple[str, str]:
    system = "你是作者，续写作品正文。只输出纯文本正文，不要标题与解释。"
    ol = "；".join(outline[:6]) if outline else "自由推进"
    user = f"""作品：{title}（{work_type}）
设定：{premise}
大纲：{ol}
人设语气：{_clip(persona_prompt, 300)}
已有结尾：{_clip(previous, 600) or "（开头）"}
下一段提示：{next_hint or "自然推进"}
字数约：{budget}

直接写续写正文：
"""
    return system, user


def format_plan_summary(items: List[Dict[str, Any]], limit: int = 8) -> str:
    lines = []
    for it in items[:limit]:
        t = it.get("time") or ""
        e = it.get("end") or ""
        a = it.get("activity") or ""
        if a:
            lines.append(f"{t}-{e} {a}".strip())
    return "；".join(lines)
