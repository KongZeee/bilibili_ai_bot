"""
策略引擎 - StrategyEngine

P3: 复杂主动行为策略
- 视频候选评分（热度 × 相关度 × 疲劳惩罚）
- 优先级队列（多因子加权排序）
- 疲劳系数（避免同类内容过度曝光）
- 时间衰减（旧内容权重下降）
"""
import logging
import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("bilibot.strategy")


class StrategyEngine:
    """主动行为策略引擎"""

    # 权重配置
    WEIGHT_HOT = 0.35        # 热度权重
    WEIGHT_RELEVANCE = 0.30  # 相关度权重
    WEIGHT_NOVELTY = 0.20    # 新颖性权重
    WEIGHT_TIME = 0.15       # 时间衰减权重

    # 疲劳惩罚参数
    FATIGUE_DECAY = 0.5      # 每次相同分区/UP主后疲劳衰减系数
    FATIGUE_MIN = 0.1       # 最低疲劳系数

    # 时间衰减参数（半衰期 24h）
    TIME_HALFLIFE = 86400.0

    def __init__(self, config: Optional[Dict] = None, data_store=None):
        self.config = config or {}
        self.ds = data_store
        self._history: List[Dict] = []  # 近期行为历史（内存）
        self._load_history()

    # ── 历史管理 ──

    def _load_history(self):
        """从 data_store 加载近期行为"""
        if not self.ds:
            return
        try:
            raw = self.ds.load_json("strategy_history.json", [])
            self._history = raw[-200:] if isinstance(raw, list) else []
        except Exception:
            self._history = []

    def _save_history(self):
        """保存行为历史"""
        if not self.ds:
            return
        try:
            self.ds.save_json("strategy_history.json", self._history[-200:])
        except Exception:
            pass

    def record_action(self, action_type: str, meta: Dict):
        """记录一次主动行为"""
        entry = {
            "type": action_type,
            "time": datetime.now().isoformat(),
            "meta": meta,
        }
        self._history.append(entry)
        self._save_history()

    # ── 疲劳计算 ──

    def _fatigue_factor(self, category: str = "", up_mid: str = "") -> float:
        """
        计算疲劳系数
        同类分区 / UP主 被操作越多，返回越小的系数
        """
        now = datetime.now()
        count_cat = 0
        count_up = 0
        for h in self._history:
            try:
                dt = datetime.fromisoformat(h["time"])
                if (now - dt).total_seconds() > 7200:  # 只看最近 2h
                    continue
                meta = h.get("meta", {})
                if category and meta.get("category") == category:
                    count_cat += 1
                if up_mid and meta.get("up_mid") == up_mid:
                    count_up += 1
            except Exception:
                pass

        # 乘数：每次叠加 FATIGUE_DECAY
        factor = 1.0
        factor *= (self.FATIGUE_DECAY ** count_cat)
        factor *= (self.FATIGUE_DECAY ** count_up)
        return max(self.FATIGUE_MIN, factor)

    # ── 时间衰减 ──

    def _time_decay(self, publish_ts: float) -> float:
        """
        时间衰减系数：发布时间越久，权重越低
        半衰期 24h
        """
        if not publish_ts:
            return 1.0
        age = max(0, time.time() - publish_ts)
        return 2 ** (-age / self.TIME_HALFLIFE)

    # ── 热度归一化 ──

    @staticmethod
    def _normalize(values: List[float]) -> List[float]:
        if not values:
            return []
        lo, hi = min(values), max(values)
        if hi == lo:
            return [1.0 / len(values)] * len(values)
        return [(v - lo) / (hi - lo) for v in values]

    # ── 评分主入口 ──

    def score_video(self, video: Dict) -> float:
        """
        对单个视频候选打分

        输入字段（可选）：
          - video["stat"]["view"]  或 video["view"]        播放量
          - video["stat"]["like"]  或 video["like"]        点赞
          - video["stat"]["reply"] 或 video["reply"]       评论
          - video["category"]      或 video["tname"]       分区
          - video["owner"]["mid"]                          UP主 mid
          - video["pubdate"]                               Unix 时间戳
          - video["title"]                                 标题（用于相关度）
        """
        stat = video.get("stat", {})
        view = stat.get("view", video.get("view", 0)) or 0
        like = stat.get("like", video.get("like", 0)) or 0
        reply = stat.get("reply", video.get("reply", 0)) or 0
        category = video.get("category") or video.get("tname", "")
        up_mid = video.get("owner", {}).get("mid", "")
        pubdate = video.get("pubdate", 0)

        # 1. 热度分（对数平滑）
        hot_raw = math.log1p(view) * 0.6 + math.log1p(like) * 0.3 + math.log1p(reply) * 0.1
        hot_score = hot_raw  # 先保留原始分，后面归一化

        # 2. 相关度（命中兴趣关键词数）
        interest_keywords = self.config.get("proactive", {}).get("interest_keywords", [])
        title = video.get("title", "")
        matched = sum(1 for kw in interest_keywords if kw.lower() in title.lower())
        relevance_score = min(1.0, matched / max(len(interest_keywords), 1))

        # 3. 新颖性（欢迎低疲劳项）
        fatigue = self._fatigue_factor(category, str(up_mid))
        novelty_score = fatigue

        # 4. 时间衰减
        time_score = self._time_decay(pubdate)

        # 归一化热度（需要 batch 内对比，这里用固定上限近似）
        hot_norm = min(1.0, hot_raw / 10.0)

        final = (
            hot_norm * self.WEIGHT_HOT
            + relevance_score * self.WEIGHT_RELEVANCE
            + novelty_score * self.WEIGHT_NOVELTY
            + time_score * self.WEIGHT_TIME
        )
        return round(final * 100, 2)  # 0-100 分

    def rank_videos(self, videos: List[Dict]) -> List[Tuple[Dict, float]]:
        """
        批量评分 + 排序，返回 (video, score) 列表
        """
        scored = [(v, self.score_video(v)) for v in videos]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def select_top(self, videos: List[Dict], top_n: int = 3) -> List[Dict]:
        """
        选出 top_n 个高优先级视频，并记录行为
        """
        ranked = self.rank_videos(videos)[:top_n]
        result = []
        for video, score in ranked:
            meta = {
                "category": video.get("category") or video.get("tname", ""),
                "up_mid": video.get("owner", {}).get("mid", ""),
                "bvid": video.get("bvid", ""),
                "score": score,
            }
            self.record_action("video_selected", meta)
            result.append(video)
        return result

    # ── 调度快照 ──

    def get_snapshot(self) -> Dict[str, Any]:
        """获取策略状态快照"""
        now = datetime.now()
        recent = [h for h in self._history
                  if (now - datetime.fromisoformat(h["time"])).total_seconds() < 3600]
        return {
            "history_count": len(self._history),
            "recent_1h": len(recent),
            "recent_types": list({h["type"] for h in recent}),
            "weights": {
                "hot": self.WEIGHT_HOT,
                "relevance": self.WEIGHT_RELEVANCE,
                "novelty": self.WEIGHT_NOVELTY,
                "time": self.WEIGHT_TIME,
            },
        }
