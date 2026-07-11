"""
人格化行为系统 - HumanizedBehavior

让Bot的行为更像真人：
1. 模拟人类行为模式（随机延迟、情绪波动、兴趣变化）
2. 视频浏览时的"真实反应"
3. 评论时参考知识库记忆
4. 个性化回复风格
"""
import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("bilibot.humanized_behavior")


def _extract_json_object(text: str) -> Optional[Dict]:
    """PRD 4.10：从文本中提取 JSON 对象，支持嵌套大括号

    依次尝试：
    1. 直接 json.loads
    2. ```json ... ``` 代码块
    3. 平衡括号匹配（支持嵌套对象）
    """
    if not text:
        return None
    # 1. 直接解析
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    # 2. ```json ... ``` 块
    m = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, TypeError):
            pass
    # 3. 平衡括号匹配
    start = text.find('{')
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break
        start = text.find('{', start + 1)
    return None


# ═══════════════════════════════════════════
#  人类行为模拟器
# ═══════════════════════════════════════════

class HumanBehaviorSimulator:
    """
    模拟人类行为模式
    
    核心思想：人类不是机器，会有：
    - 随机思考时间
    - 情绪波动
    - 兴趣偏移
    - 疲劳效应
    - 社交习惯
    """
    
    def __init__(self, config: Any = None):
        self.config = config or {}
        
        # 行为参数
        self.min_delay = 1.0      # 最小操作间隔（秒）
        self.max_delay = 8.0      # 最大操作间隔（秒）
        self.fatigue_threshold = 5  # 连续操作多少次后休息
        self.fatigue_rest_min = 30  # 休息最短时间（秒）
        self.fatigue_rest_max = 120 # 休息最长时间（秒）
        
        # 当前状态
        self._consecutive_actions = 0
        self._is_resting = False
        self._rest_until = 0.0
        self._mood = "normal"  # normal/happy/excited/bored
        self._interest_bias: Dict[str, float] = {}  # 兴趣偏好
    
    async def simulate_human_delay(self, action_type: str = "general") -> float:
        """
        模拟人类的随机思考/操作时间
        
        Args:
            action_type: 动作类型 (browse/comment/watch/post)
            
        Returns:
            float: 延迟秒数
        """
        # 疲劳休息
        if self._is_resting and time.time() < self._rest_until:
            remaining = self._rest_until - time.time()
            logger.debug(f"Bot在休息中，还需 {remaining:.1f} 秒")
            await asyncio.sleep(remaining)
            self._is_resting = False
        
        # 疲劳检测
        if self._consecutive_actions >= self.fatigue_threshold:
            rest_time = random.uniform(self.fatigue_rest_min, self.fatigue_rest_max)
            self._is_resting = True
            self._rest_until = time.time() + rest_time
            self._consecutive_actions = 0
            logger.info(f"Bot累了，休息 {rest_time:.0f} 秒")
            await asyncio.sleep(rest_time)
        
        # 根据动作类型调整延迟
        delays = {
            "browse": (2.0, 15.0),   # 浏览视频：2-15秒
            "comment": (3.0, 12.0),  # 评论思考：3-12秒
            "watch": (5.0, 60.0),    # 观看视频：5-60秒
            "post": (5.0, 20.0),     # 发布动态：5-20秒
            "general": (1.0, 8.0),   # 一般操作
        }
        
        min_d, max_d = delays.get(action_type, delays["general"])
        
        # 情绪影响延迟
        mood_modifier = {
            "excited": 0.7,   # 兴奋时更快
            "happy": 0.85,
            "normal": 1.0,
            "bored": 1.3,     # 无聊时更慢
        }
        modifier = mood_modifier.get(self._mood, 1.0)
        
        delay = random.uniform(min_d, max_d) * modifier
        
        # 记录动作
        self._consecutive_actions += 1
        
        logger.debug(f"模拟人类延迟: {delay:.1f}s (动作={action_type}, 情绪={self._mood})")
        await asyncio.sleep(delay)
        return delay
    
    def update_mood(self, event: str = None) -> str:
        """
        根据事件更新情绪
        
        Args:
            event: 触发事件
            
        Returns:
            str: 新情绪
        """
        moods = ["normal", "happy", "excited", "bored", "curious"]
        weights = {
            "normal": 40,
            "happy": 25,
            "excited": 10,
            "bored": 15,
            "curious": 10,
        }
        
        # 事件影响情绪
        if event:
            event_mood_map = {
                "liked_video": ("happy", 30),
                "commented": ("excited", 20),
                "watched_video": ("curious", 15),
                "posted": ("happy", 25),
                "received_comment": ("excited", 20),
            }
            if event in event_mood_map:
                new_mood, weight = event_mood_map[event]
                weights[new_mood] += weight
        
        # 加权随机选择
        mood_list = list(weights.keys())
        weight_list = list(weights.values())
        self._mood = random.choices(mood_list, weights=weight_list, k=1)[0]
        
        return self._mood
    
    def get_mood_expression(self) -> Dict[str, str]:
        """获取当前情绪的表达式"""
        expressions = {
            "excited": {
                "prefix": ["哇！", "哈哈！", "太棒了！", "好耶！", "冲冲冲！"],
                "suffix": ["~", "！！", "！！！"],
                "emoji": ["😆", "🎉", "✨", "🔥"],
                "tone": "热情洋溢",
            },
            "happy": {
                "prefix": ["嗯~", "好的呢", "嘻嘻", "好呀"],
                "suffix": ["~", "呢", "哦"],
                "emoji": ["😊", "😄", "👍"],
                "tone": "轻松愉快",
            },
            "normal": {
                "prefix": ["", "嗯", "好的", "了解"],
                "suffix": ["。", "~", ""],
                "emoji": ["👀", "😌"],
                "tone": "自然平和",
            },
            "bored": {
                "prefix": ["嗯...", "哦", "好吧", "行吧"],
                "suffix": ["。", "...", ""],
                "emoji": ["😶", "🙃"],
                "tone": "平淡敷衍",
            },
            "curious": {
                "prefix": ["诶？", "咦？", "这个嘛", "有意思"],
                "suffix": ["？", "呢？", "是吗？"],
                "emoji": ["🤔", "🧐", "😮"],
                "tone": "好奇探究",
            },
        }
        return expressions.get(self._mood, expressions["normal"])
    
    def adjust_interest_bias(self, category: str, delta: float = 0.1):
        """调整兴趣偏好"""
        self._interest_bias[category] = self._interest_bias.get(category, 0.5) + delta
        # 限制范围
        self._interest_bias[category] = max(0.0, min(1.0, self._interest_bias[category]))
    
    def get_interest_score(self, category: str) -> float:
        """获取某类别的兴趣分数"""
        return self._interest_bias.get(category, 0.5)


# ═══════════════════════════════════════════
#  视频浏览模拟器
# ═══════════════════════════════════════════

class VideoBrowser:
    """
    模拟真人刷视频
    
    行为特征：
    - 不会每个视频都看，会快速滑动
    - 对感兴趣的内容停留更久
    - 会点赞、收藏喜欢的视频
    - 偶尔发弹幕或评论
    """
    
    def __init__(self, behavior_sim: HumanBehaviorSimulator, config: Any = None):
        self.behavior = behavior_sim
        self.config = config or {}
    
    async def browse_videos(self, video_list: List[Dict]) -> List[Dict]:
        """
        模拟刷视频过程
        
        Args:
            video_list: 候选视频列表
            
        Returns:
            选中的视频及浏览行为记录
        """
        if not video_list:
            return []
        
        results = []
        total_videos = len(video_list)
        
        # 模拟滑动浏览
        for i, video in enumerate(video_list):
            # 随机决定是否"看完"这个视频
            should_watch = random.random() < 0.6  # 60%概率看完
            
            if should_watch:
                # 模拟观看时长
                watch_duration = random.randint(30, 600)  # 30秒到10分钟
                
                # 根据兴趣调整观看时长
                category = video.get("category", "")
                interest = self.behavior.get_interest_score(category)
                watch_duration = int(watch_duration * (0.5 + interest))
                
                # 模拟观看
                await self.behavior.simulate_human_delay("watch")
                
                # 决定是否点赞
                liked = random.random() < (0.1 + interest * 0.3)
                
                # 决定是否评论
                commented = random.random() < (0.05 + interest * 0.15)
                
                result = {
                    "video": video,
                    "watched": True,
                    "watch_duration": watch_duration,
                    "liked": liked,
                    "commented": commented,
                }
                
                if liked:
                    self.behavior.update_mood("liked_video")
                
                if commented:
                    self.behavior.update_mood("commented")
                
                results.append(result)
            else:
                # 快速滑动，只看几秒
                await asyncio.sleep(random.uniform(1.0, 3.0))
        
        # 模拟休息
        if results:
            rest_time = random.uniform(5.0, 30.0)
            await asyncio.sleep(rest_time)
        
        logger.info(f"刷视频完成: 浏览 {total_videos} 个, 选中 {len(results)} 个")
        return results
    
    def generate_video_reaction(self, video_title: str, mood: str = None) -> str:
        """
        生成对视频的"真实反应"
        
        就像真人看完视频后的随口评论
        """
        mood_expressions = self.behavior.get_mood_expression()
        mood = mood or self.behavior._mood
        
        reactions = {
            "excited": [
                f"哇这个视频真的绝了！{mood_expressions['suffix'][0]}",
                f"太好看了吧！忍不住看完{mood_expressions['suffix'][0]}",
                f"这个UP主太厉害了{mood_expressions['emoji'][0]}",
                f"一口气看完根本停不下来{mood_expressions['suffix'][0]}",
            ],
            "happy": [
                f"挺不错的视频呢~",
                f"哈哈这个很有意思{mood_expressions['emoji'][0]}",
                f"不错不错，推荐{mood_expressions['suffix'][0]}",
                f"看完心情变好了{mood_expressions['emoji'][0]}",
            ],
            "normal": [
                f"看了这个视频",
                f"还不错",
                f"挺有意思的",
                f"看完了",
            ],
            "bored": [
                f"...看了",
                f"还行吧",
                f"就那样",
                f"...",
            ],
            "curious": [
                f"这个视频有点意思{mood_expressions['emoji'][0]}",
                f"想知道更多细节",
                f"这个角度没见过{mood_expressions['emoji'][0]}",
            ],
        }
        
        pool = reactions.get(mood, reactions["normal"])
        reaction = random.choice(pool)
        
        # 加入视频标题元素
        if video_title:
            title_keywords = re.findall(r'[\u4e00-\u9fff]{2,4}', video_title)
            if title_keywords:
                keyword = random.choice(title_keywords)
                reaction = f"{keyword}{reaction}"
        
        return reaction


# ═══════════════════════════════════════════
#  评论生成器（人格化）
# ═══════════════════════════════════════════

class HumanizedCommentGenerator:
    """
    人格化评论生成器
    
    让Bot的评论更像真人：
    1. 参考知识库记忆（"我记得你说过..."）
    2. 考虑当前情绪
    3. 保持人格一致性
    4. 随机变化回复风格
    """
    
    SYSTEM_PROMPT = """你是一个真实的B站用户，正在回复评论。

## 你的特点
- 你有自己的性格和说话风格
- 你会记住之前和这个人的对话
- 你的回复会根据心情变化
- 你不会每次都回复，有时会选择忽略
- 你的回复简短自然，像真人聊天

## 回复规则
1. 参考提供的记忆上下文，如果记得之前的对话，可以自然地延续
2. 根据你的心情调整语气
3. 回复要简短（10-50字），像真人发消息
4. 可以适当使用网络用语、emoji
5. 如果是熟人，可以更随意；如果是陌生人，保持礼貌
6. 不要每次都回复，对于无意义评论可以选择忽略

## 输出格式
只输出你的回复内容，不要其他文字。
如果认为不应该回复，输出 "__IGNORE__"。"""
    
    def __init__(self, llm_adapter, personality_system, knowledge_memory, behavior_sim,
                 persona_store=None, account_id: str = ""):
        self.llm = llm_adapter
        self.personality = personality_system
        self.knowledge_memory = knowledge_memory
        self.behavior = behavior_sim
        self.persona_store = persona_store
        self.account_id = account_id

    def _get_persona_prompt(self, scene: str = "reply_comment") -> str:
        """获取当前人格的系统提示词（优先用 persona_store，降级用 personality）

        多账号架构下，通过 account_id 解析账号绑定的人格。
        """
        if self.persona_store:
            try:
                return self.persona_store.build_scene_prompt(
                    scene=scene, account_id=self.account_id or None
                )
            except Exception as e:
                logger.debug(f"persona_store 构建提示词失败，降级: {e}")
        if self.personality:
            try:
                return self.personality.get_system_prompt()
            except Exception:
                pass
        return ""
    
    async def should_reply(self, comment: str, user_id: str, username: str) -> Tuple[bool, str]:
        """
        判断是否应该回复
        
        Returns:
            (should_reply: bool, reason: str)
        """
        # 1. 检查冷却时间
        # 2. 检查评论内容质量
        # 3. 检查心情影响
        
        # 心情影响回复意愿
        mood = self.behavior._mood
        reply_probability = {
            "excited": 0.85,
            "happy": 0.75,
            "normal": 0.60,
            "bored": 0.30,
            "curious": 0.70,
        }
        
        prob = reply_probability.get(mood, 0.6)
        should = random.random() < prob
        
        if not should:
            return False, f"心情{mood}，回复率降低"
        
        # 4. 检查是否是自己（某些配置下不回复自己）
        # 5. 检查黑名单
        
        return True, "通过心情检查"
    
    async def generate_comment(
        self,
        comment: str,
        user_id: str,
        username: str,
        thread_id: str,
        oid: int = 0,
        persona_id: str = None,
    ) -> Optional[str]:
        """
        生成人格化评论

        流程：
        1. 从知识库检索与该用户相关的记忆
        2. 构建包含记忆上下文的Prompt
        3. 调用LLM生成回复
        4. 根据人格调整语气

        MEM-603：persona_id 用于记忆检索的硬过滤，防止跨人格记忆泄漏。
        未显式传入时回退到 self.account_id。
        """
        # 1. 检索知识库
        knowledge_context = ""
        if self.knowledge_memory:
            try:
                memories = await self.knowledge_memory.search_memories(
                    query=f"{username} 的对话",
                    limit=5,
                    user_id=user_id,
                    persona_id=persona_id or self.account_id,
                    categories=["episodic", "factual", "preference"]
                )
                if memories:
                    knowledge_context = "\n".join([
                        f"- {m.get('content', '')}" for m in memories[:3]
                    ])
                    logger.info(f"检索到 {len(memories)} 条相关知识")
            except Exception as e:
                logger.debug(f"知识库检索失败: {e}")
        
        # 2. 获取人格信息（含 reply_comment 场景规则）
        personality_info = self._get_persona_prompt(scene="reply_comment")
        
        # 3. 获取当前情绪
        mood_expr = self.behavior.get_mood_expression()
        mood_context = f"当前情绪: {mood_expr['tone']}"
        
        # 4. 构建Prompt
        memory_section = f"你们之前的对话记忆:\n{knowledge_context}" if knowledge_context else ""
        
        prompt = f"""用户 {username} 在评论区说了: "{comment}"

{memory_section}

{mood_context}

请像一个真实的B站用户一样回复。回复要简短自然。"""
        
        system_prompt = self.SYSTEM_PROMPT
        if personality_info:
            system_prompt += f"\n\n你的性格设定:\n{personality_info}"
        
        # 5. 调用LLM（检查是否初始化）
        if not self.llm:
            logger.warning("LLM未初始化，跳过评论生成")
            return None
        
        try:
            response = await self.llm.generate(prompt, system_prompt=system_prompt, max_tokens=150)
            if not response:
                return None
            
            # 检查是否是忽略信号
            if "__IGNORE__" in response:
                return None
            
            # 6. 人格化后处理
            cleaned = self._post_process_comment(response.strip())
            
            if not cleaned or len(cleaned) < 2:
                return None
            
            # 7. 更新情绪
            self.behavior.update_mood("commented")
            
            return cleaned
            
        except Exception as e:
            logger.error(f"评论生成失败: {e}")
            return None
    
    def _post_process_comment(self, comment: str) -> str:
        """后处理评论，使其更像真人"""
        # 去除多余空白
        comment = ' '.join(comment.split())
        
        # PRD V3 §5.3 (P2-3)：统一截断到 100 字，不加省略号
        if len(comment) > 100:
            comment = comment[:100]
        
        # 随机添加语气词（增加人性化）
        if random.random() < 0.3:
            particles = ["~", "呢", "呀", "哦", "哈", "呗"]
            comment += random.choice(particles)
        
        return comment

    # ══════════════════════════════════════
    #  主动看视频：评价 + 评论生成
    # ══════════════════════════════════════

    async def evaluate_video(
        self,
        title: str,
        owner: str,
        desc: str,
        tags: List[str],
        hot_comments: List[str] = None,
        video_content: str = "",
    ) -> Optional[Dict]:
        """
        评价视频，返回评分、心情、评论等

        Args:
            video_content: 视频理解服务生成的视听行为日志（Markdown），为空则只用元数据

        Returns:
            {
                "score": 1-10,
                "mood": "开心/平静/无聊/感动/好笑/震撼/困惑",
                "comment": "15-30字评论区留言",
                "review": "50字以内视频评价",
                "want_like": bool,
                "want_coin": bool,
                "want_favorite": bool,
                "want_comment": bool,
            }

        VID-501 契约：互动意图字段统一为 want_favorite（不再产出 want_fav）。
        旧模型若返回 want_fav，会在解析时通过迁移适配器转换为 want_favorite。
        """
        if not self.llm:
            logger.warning("LLM未初始化，跳过视频评价")
            return None

        # 获取当前人格提示词（含 proactive_comment 场景规则）
        personality_info = self._get_persona_prompt(scene="proactive_comment")

        # 获取当前情绪
        mood_expr = self.behavior.get_mood_expression() if self.behavior else {"tone": "平静"}

        # 构建视频信息文本
        tags_text = "、".join(tags[:8]) if tags else "无"
        desc_text = desc[:300] if desc else "无"
        hot_comments_text = "\n".join(f"  - {c}" for c in (hot_comments or [])[:5]) if hot_comments else "无"

        # 视频内容段落（AI 视听分析）
        video_content_section = ""
        if video_content:
            # 截断过长的行为日志，避免 prompt 膨胀
            truncated = video_content[:2000]
            video_content_section = f"""
【视频内容】（AI 视听分析）
{truncated}
"""

        prompt = f"""请评价以下B站视频，以你的角色视角观看后给出真实反馈。

【视频信息】
标题: {title}
UP主: {owner}
简介: {desc_text}
标签: {tags_text}
{video_content_section}
【热门评论】
{hot_comments_text}

【当前情绪】{mood_expr['tone']}

请输出JSON格式（不要其他文字）:
{{
  "score": 1-10的整数评分,
  "mood": "看完后的心情(开心/平静/无聊/感动/好笑/震撼/困惑)",
  "comment": "15-30字的评论区留言，像真人随手打的",
  "review": "50字以内的视频评价",
  "want_like": true/false,
  "want_coin": true/false,
  "want_favorite": true/false,
  "want_comment": true/false
}}"""

        system_prompt = "你是一个真实的B站用户，正在看视频并评价。"
        if personality_info:
            system_prompt += f"\n\n你的性格设定:\n{personality_info}"

        try:
            response = await self.llm.generate(
                prompt, system_prompt=system_prompt, max_tokens=300
            )
            if not response:
                return None

            # 提取JSON（PRD 4.10：支持嵌套对象的平衡括号匹配）
            result = _extract_json_object(response)
            if result is None:
                logger.warning(f"无法解析评价JSON: {response[:200]}")
                return None

            # VID-501：互动意图字段契约归一化
            # 通过 InteractionSuggestion.from_dict 做严格类型校验 + 迁移适配器
            # （want_fav → want_favorite）。归一化后写回 result，保证下游策略引擎
            # 拿到的永远是 want_favorite（合法 bool），且不再残留 want_fav。
            from bilibot.models.interaction import InteractionSuggestion
            suggestion = InteractionSuggestion.from_dict(result)
            result["want_like"] = suggestion.want_like
            result["want_coin"] = suggestion.want_coin
            result["want_favorite"] = suggestion.want_favorite
            result["want_comment"] = suggestion.want_comment
            # 移除旧字段，防止 want_fav 泄漏到下游
            result.pop("want_fav", None)

            logger.info(f"视频评价: score={result.get('score')}, mood={result.get('mood')}, comment={result.get('comment')}")
            return result

        except Exception as e:
            logger.error(f"视频评价失败: {e}")
            return None

    async def generate_proactive_comment(
        self,
        title: str,
        owner: str,
        desc: str,
        tags: List[str],
        review: str = "",
        mood: str = "",
        video_content: str = "",
    ) -> Optional[str]:
        """
        为视频生成主动评论（与 evaluate_video 的 comment 不同，这是更深入的评论）

        Args:
            video_content: 视频理解服务生成的视听行为日志（Markdown），为空则只用元数据

        Returns:
            评论文本（≤40字），或 None 表示不评论
        """
        if not self.llm:
            return None

        # 获取当前人格提示词（含 proactive_comment 场景规则）
        personality_info = self._get_persona_prompt(scene="proactive_comment")

        tags_text = "、".join(tags[:5]) if tags else "无"
        desc_text = desc[:200] if desc else "无"

        # 视频内容段落
        video_content_section = ""
        if video_content:
            truncated = video_content[:1500]
            video_content_section = f"\n【视频内容】\n{truncated}\n"

        prompt = f"""你刚看完一个B站视频，想发一条评论。

【视频】{title} (UP主: {owner})
【简介】{desc_text}
【标签】{tags_text}{video_content_section}
【你的评价】{review or '不错'}
【心情】{mood or '平静'}

请发一条评论，要求：
- 像真人随手打的，不要客套话
- ≤40字
- 可以用网络用语、emoji
- 不要@UP主
- 只输出评论内容，不要其他文字"""

        system_prompt = "你是一个真实的B站用户，正在发评论。"
        if personality_info:
            system_prompt += f"\n\n你的性格设定:\n{personality_info}"

        try:
            response = await self.llm.generate(
                prompt, system_prompt=system_prompt, max_tokens=80
            )
            if not response:
                return None

            comment = response.strip().strip('"').strip("'")
            # PRD V3 §5.3 (P2-3)：统一截断到 100 字，不加省略号
            if len(comment) > 100:
                comment = comment[:100]

            if len(comment) < 2:
                return None

            return comment

        except Exception as e:
            logger.error(f"主动评论生成失败: {e}")
            return None


# ═══════════════════════════════════════════
#  动态发布模拟器
# ═══════════════════════════════════════════

class DynamicPoster:
    """
    模拟真人发动态
    
    行为特征：
    - 不会机械地按时发
    - 内容多样化
    - 偶尔会发一些随想
    """
    
    def __init__(self, behavior_sim: HumanBehaviorSimulator, llm_adapter, config: Any = None):
        self.behavior = behavior_sim
        self.llm = llm_adapter
        self.config = config or {}
    
    async def generate_dynamic(self, context: str = "") -> str:
        """
        生成动态内容
        
        像真人一样发动态，而不是机械地重复
        """
        mood = self.behavior._mood
        mood_expressions = self.behavior.get_mood_expression()
        
        prompt = f"""你现在是一个真实的B站UP主，请发一条动态。

当前情绪: {mood_expressions['tone']}

{context}

要求：
1. 内容简短（20-80字）
2. 像一个真实的人在说话
3. 可以包含日常分享、感想、提问
4. 适当使用emoji
5. 不要过于正式或机械化

只输出动态内容，不要其他文字。"""
        
        # 检查LLM是否初始化
        if not self.llm:
            logger.warning("LLM未初始化，使用默认动态")
            return random.choice([
                "今天又是充实的一天呢~",
                "刚看完一个超棒的视频，推荐给大家！",
                "突然想到一件事...大家最近有什么好看的番推荐吗？",
                "天气真好，适合出去走走☀️",
                "又刷到了好多有趣的内容，B站真好玩~",
            ])
        
        try:
            response = await self.llm.generate(prompt, max_tokens=200)
            if response:
                # PRD 5.3：安全截断，避免在 emoji 多字节序列中间截断产生乱码
                text = response.strip()[:200]
                # 移除末尾可能的不完整 emoji（Unicode 替换字符）
                try:
                    text.encode("utf-8").decode("utf-8")
                except UnicodeDecodeError:
                    text = text[:-1]
                return text
        except Exception as e:
            logger.error(f"动态生成失败: {e}")
        
        # 降级：返回一个随机日常
        defaults = [
            "今天又是充实的一天呢~",
            "刚看完一个超棒的视频，推荐给大家！",
            "突然想到一件事...大家最近有什么好看的番推荐吗？",
            "天气真好，适合出去走走☀️",
            "又刷到了好多有趣的内容，B站真好玩~",
        ]
        return random.choice(defaults)
    
    async def post_dynamic(self, content: str) -> bool:
        """发布动态"""
        # 模拟发布前的"思考时间"
        await self.behavior.simulate_human_delay("post")
        logger.info(f"发布动态: {content[:50]}...")
        return True
