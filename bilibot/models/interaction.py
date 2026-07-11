"""
互动建议 DTO - InteractionSuggestion

PRD V5 §4.4 VID-501：互动意图字段契约
- 唯一权威的互动意图数据结构，模型与策略引擎之间只通过本 DTO 交换意图
- want_fav 仅允许在迁移适配器（from_dict）中读取，随后立即转换为 want_favorite
  新代码不得再产生 want_fav
- 字段缺失 / 类型错误 / LLM 解析失败 → 对应 bool 字段一律按 False 处理
- 评分阈值只能进一步拒绝模型建议，不能把 False 升级为 True（由策略引擎保证）
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("bilibot.models.interaction")


@dataclass
class InteractionSuggestion:
    """LLM 互动意图建议 DTO（VID-501 契约）

    模型只输出建议，最终决策由 InteractionPolicyEngine 根据开关、
    日预算、评分阈值和去重状态决定。

    字段契约：
    - want_like / want_coin / want_favorite / want_comment: 严格 bool
    - score: int 0-10
    - reason: str

    任何字段缺失、类型错误或 LLM 解析失败 → bool 字段为 False，score 为 0。
    """

    want_like: bool = False
    want_coin: bool = False
    want_favorite: bool = False
    want_comment: bool = False
    score: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（只暴露 want_favorite，不暴露 want_fav）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "InteractionSuggestion":
        """从 dict 构建 InteractionSuggestion，严格校验类型。

        - 缺失 / 类型错误 → False（bool 字段）或 0（score）或 ""（reason）
        - want_fav 作为旧字段别名：仅当 want_favorite 未显式给出时才转换（迁移适配器）
        - data 为 None 或非 dict → 全 False 的空实例（代表 LLM 解析失败）

        Args:
            data: LLM 解析出的 dict（可能为 None / 非 dict）

        Returns:
            InteractionSuggestion 实例
        """
        if not isinstance(data, dict):
            return cls()

        want_like = cls._parse_bool(data.get("want_like"))
        want_coin = cls._parse_bool(data.get("want_coin"))
        want_comment = cls._parse_bool(data.get("want_comment"))

        # want_favorite：优先取 want_favorite；缺失时回退到旧字段 want_fav（迁移适配器）
        if "want_favorite" in data:
            want_favorite = cls._parse_bool(data.get("want_favorite"))
        elif "want_fav" in data:
            # 旧模型迁移：want_fav → want_favorite（仅当 want_favorite 未显式给出）
            want_favorite = cls._parse_bool(data.get("want_fav"))
            logger.debug(
                "迁移适配器：want_fav=%s → want_favorite=%s",
                data.get("want_fav"), want_favorite,
            )
        else:
            want_favorite = False

        score = cls._parse_int(data.get("score"))
        reason = cls._parse_str(data.get("reason"))

        return cls(
            want_like=want_like,
            want_coin=want_coin,
            want_favorite=want_favorite,
            want_comment=want_comment,
            score=score,
            reason=reason,
        )

    # ── 严格类型解析 ──

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        """严格 bool 校验：仅接受 True/False；其它类型一律 False。

        注意 bool 是 int 的子类，必须先判 bool。字符串 "yes"/"true"、
        数字 1 等均按"类型错误"处理为 False（VID-501 契约）。
        """
        if isinstance(value, bool):
            return value
        return False

    @staticmethod
    def _parse_int(value: Any) -> int:
        """严格 int 校验：int（非 bool）原样返回；float 取整；其它 → 0。"""
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        return 0

    @staticmethod
    def _parse_str(value: Any) -> str:
        if isinstance(value, str):
            return value
        return ""
