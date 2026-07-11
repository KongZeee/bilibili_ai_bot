"""
GenerationOutcome — 可判别生成结果（PRD-V5 §6.1 / REP-501）

区分"不想回复"(skip) 与"临时失败"(retryable_error) 与"永久失败"(permanent_error)，
让调用方（scheduler）能选择正确的回复状态（ignored / deferred / rejected），
而不是把所有非成功路径都塞进 None。

状态映射（PRD-V5 §6.1）：
| Situation                          | outcome.status     | Reply state    |
|------------------------------------|--------------------|----------------|
| Business filter / model skip       | skip               | ignored        |
| LLM timeout, 429, 5xx, conn error  | retryable_error    | deferred       |
| Search failed but can degrade      | (continue)         | does not term. |
| Safety policy permanent reject     | permanent_error    | rejected       |
| Parse failure that retry may fix   | retryable_error    | deferred       |
| Valid text generated               | generated          | safety_pending |
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════
# 状态常量
# ═══════════════════════════════════════════════

STATUS_GENERATED = "generated"
STATUS_SKIP = "skip"
STATUS_RETRYABLE_ERROR = "retryable_error"
STATUS_PERMANENT_ERROR = "permanent_error"

VALID_STATUSES = frozenset({
    STATUS_GENERATED,
    STATUS_SKIP,
    STATUS_RETRYABLE_ERROR,
    STATUS_PERMANENT_ERROR,
})


# ═══════════════════════════════════════════════
# 错误码常量
# ═══════════════════════════════════════════════

# LLM 配置 / 客户端
LLM_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"            # permanent: self.llm 为 None
LLM_CLIENT_UNAVAILABLE = "LLM_CLIENT_UNAVAILABLE"    # retryable: client 未初始化

# LLM 调用错误（retryable）
LLM_TIMEOUT = "LLM_TIMEOUT"
LLM_CONNECTION_ERROR = "LLM_CONNECTION_ERROR"
LLM_RATE_LIMITED = "LLM_RATE_LIMITED"
LLM_SERVER_ERROR = "LLM_SERVER_ERROR"
LLM_UNKNOWN_ERROR = "LLM_UNKNOWN_ERROR"

# 模型行为
MODEL_EMPTY_REPLY = "MODEL_EMPTY_REPLY"              # skip: 模型明确不回复

# 安全
SAFETY_PERMANENT_REJECT = "SAFETY_PERMANENT_REJECT"  # permanent

# 解析
PARSE_FAILURE = "PARSE_FAILURE"                      # retryable


@dataclass
class GenerationOutcome:
    """可判别的生成结果

    取代 generate_reply() 返回 None 的做法：所有路径都返回 GenerationOutcome，
    通过 status 区分"成功 / 跳过 / 可重试 / 永久失败"，让调用方正确驱动回复状态机。
    """

    status: str
    text: str = ""
    error_code: str = ""
    retry_after: Optional[float] = None
    audit_id: Optional[str] = None
    context_meta: dict = field(default_factory=dict)

    # ─── 便捷属性 ───

    @property
    def is_generated(self) -> bool:
        return self.status == STATUS_GENERATED

    @property
    def is_skip(self) -> bool:
        return self.status == STATUS_SKIP

    @property
    def is_retryable(self) -> bool:
        return self.status == STATUS_RETRYABLE_ERROR

    @property
    def is_permanent_error(self) -> bool:
        return self.status == STATUS_PERMANENT_ERROR

    @property
    def has_text(self) -> bool:
        return bool(self.text)

    # ─── 工厂方法 ───

    @classmethod
    def generated(
        cls,
        text: str,
        audit_id: Optional[str] = None,
        context_meta: Optional[dict] = None,
    ) -> "GenerationOutcome":
        """成功生成文本 → safety_pending"""
        return cls(
            status=STATUS_GENERATED,
            text=text,
            audit_id=audit_id,
            context_meta=context_meta or {},
        )

    @classmethod
    def skip(cls, reason: str = "") -> "GenerationOutcome":
        """业务过滤 / 模型明确不回复 → ignored

        Args:
            reason: 跳过原因（同时作为 error_code 记录）
        """
        return cls(status=STATUS_SKIP, error_code=reason or "SKIP")

    @classmethod
    def retryable(
        cls,
        error_code: str,
        retry_after: Optional[float] = None,
    ) -> "GenerationOutcome":
        """临时失败可重试 → deferred"""
        return cls(
            status=STATUS_RETRYABLE_ERROR,
            error_code=error_code or LLM_UNKNOWN_ERROR,
            retry_after=retry_after,
        )

    @classmethod
    def permanent(cls, error_code: str) -> "GenerationOutcome":
        """永久失败 → rejected"""
        return cls(
            status=STATUS_PERMANENT_ERROR,
            error_code=error_code or "PERMANENT_ERROR",
        )

    # ─── 序列化 ───

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "text": self.text,
            "error_code": self.error_code,
            "retry_after": self.retry_after,
            "audit_id": self.audit_id,
            "context_meta": dict(self.context_meta),
        }

    def __repr__(self) -> str:
        if self.is_generated:
            return (
                f"GenerationOutcome(generated, {len(self.text)} chars, "
                f"audit={self.audit_id!r})"
            )
        if self.is_skip:
            return f"GenerationOutcome(skip, code={self.error_code!r})"
        if self.is_retryable:
            return (
                f"GenerationOutcome(retryable, code={self.error_code!r}, "
                f"retry_after={self.retry_after})"
            )
        return f"GenerationOutcome(permanent, code={self.error_code!r})"


__all__ = [
    "GenerationOutcome",
    "STATUS_GENERATED",
    "STATUS_SKIP",
    "STATUS_RETRYABLE_ERROR",
    "STATUS_PERMANENT_ERROR",
    "VALID_STATUSES",
    "LLM_NOT_CONFIGURED",
    "LLM_CLIENT_UNAVAILABLE",
    "LLM_TIMEOUT",
    "LLM_CONNECTION_ERROR",
    "LLM_RATE_LIMITED",
    "LLM_SERVER_ERROR",
    "LLM_UNKNOWN_ERROR",
    "MODEL_EMPTY_REPLY",
    "SAFETY_PERMANENT_REJECT",
    "PARSE_FAILURE",
]
