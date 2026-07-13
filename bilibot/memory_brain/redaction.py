"""Private-message redaction for the account-scoped memory brain.

This module deliberately has no logging.  A caller can safely pass the result to
the archive, jobs, embeddings and prompts without accidentally logging the input.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_MARKER_RE = re.compile(r"\[REDACTED:[a-z_]+\]")

# Order matters: consume labelled or structurally specific secrets before broad
# numeric identifiers.  Replacements never retain the captured value.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "cookie",
        re.compile(
            r"(?i)(?:SESSDATA|bili_jct|buvid3|buvid4|DedeUserID|DedeUserID__ckMd5|"
            r"access_token|refresh_token|LIVE_BUVID|STOKEN|sid|b_nut)"
            r"""\s*[=:]\s*['"]?[^\s,;'"]+['"]?"""
        ),
    ),
    (
        "token",
        re.compile(
            r"(?i)(?:access_key|api[_-]?key|token|secret|auth|signature|password|"
            r"""passwd)\s*[=:]\s*['"]?[^\s,;'"]+['"]?"""
        ),
    ),
    ("token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("token", re.compile(r"(?i)\b(?:sk|ak)-[A-Za-z0-9_-]{10,}\b")),
    ("email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")),
    (
        "order",
        re.compile(
            r"(?i)(?:order(?:\s*(?:id|no|number))?|订单(?:号|编号)?|交易号)"
            r"\s*[：:=#]?\s*[A-Za-z0-9_-]{6,}"
        ),
    ),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    (
        "uid",
        re.compile(r"(?i)(?:UID|user[_ -]?id|用户ID|用户编号)\s*[：:=#]?\s*\d{1,20}"),
    ),
    (
        "order",
        re.compile(
            r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]{12,}(?![A-Za-z0-9]))"
            r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+"
        ),
    ),
    ("uid", re.compile(r"(?<!\d)\d{8,12}(?!\d)")),
)

_SENSITIVE_VALUE_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "sessdata",
        "bili_jct",
        "access_token",
        "refresh_token",
        "access_key",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "phone",
        "mobile",
        "email",
        "order_id",
        "order_no",
    }
)
_ACTOR_KEYS = frozenset({"actor_id", "user_id", "uid", "sender_id", "mid"})


@dataclass(frozen=True)
class RedactionResult:
    """A value that is safe to persist outside the raw PM boundary."""

    text: str
    detected_types: tuple[str, ...] = ()
    actor_pseudonym: str = ""

    @property
    def field_types(self) -> str:
        """Compatibility form used by the existing privacy audit code."""

        return ",".join(self.detected_types)


def _salt_bytes(account_salt: bytes | str) -> bytes:
    if isinstance(account_salt, bytes):
        value = account_salt
    elif isinstance(account_salt, str):
        value = account_salt.encode("utf-8")
    else:
        raise TypeError("account_salt must be bytes or str")
    if not value:
        raise ValueError("account_salt must not be empty")
    return value


def pseudonymize_actor(actor_id: str | int, account_salt: bytes | str) -> str:
    """Return a stable, account-specific pseudonym for a platform actor ID."""

    raw_actor = str(actor_id).strip()
    if not raw_actor:
        return ""
    digest = hmac.new(
        _salt_bytes(account_salt),
        b"bilibot-memory-brain:actor:v1\0" + raw_actor.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:20]
    return f"actor_{token}"


def _replace_username(text: str, username: str) -> tuple[str, bool]:
    username = str(username or "").strip()
    if not username or _MARKER_RE.fullmatch(username):
        return text, False
    replaced, count = re.subn(
        re.escape(username),
        "[REDACTED:username]",
        text,
        flags=re.IGNORECASE if username.isascii() else 0,
    )
    return replaced, count > 0


def redact_sensitive_text(text: str, *, current_username: str = "") -> RedactionResult:
    """Redact PM-sensitive values without retaining reversible mappings."""

    if text is None:
        text = ""
    if not isinstance(text, str):
        text = str(text)
    redacted, username_found = _replace_username(text, current_username)
    detected: list[str] = ["username"] if username_found else []
    for kind, pattern in _PATTERNS:
        replaced, count = pattern.subn(f"[REDACTED:{kind}]", redacted)
        if count:
            if kind not in detected:
                detected.append(kind)
            redacted = replaced
    return RedactionResult(redacted, tuple(detected))


def redact_private_message(
    text: str,
    *,
    actor_id: str | int,
    account_salt: bytes | str,
    current_username: str = "",
) -> RedactionResult:
    """Redact a PM and pseudonymize its sender before any durable write."""

    result = redact_sensitive_text(text, current_username=current_username)
    return RedactionResult(
        text=result.text,
        detected_types=result.detected_types,
        actor_pseudonym=pseudonymize_actor(actor_id, account_salt),
    )


def redact_private_payload(
    value: Any,
    *,
    account_salt: bytes | str,
    current_username: str = "",
) -> Any:
    """Recursively redact structured PM data before storing it in source JSON.

    Actor-like fields are pseudonymized, known secret fields are replaced in
    full, and every other string receives the same text redaction as the body.
    The input object is never mutated.
    """

    salt = _salt_bytes(account_salt)

    def walk(item: Any, key: str = "") -> Any:
        normalized_key = key.casefold()
        if normalized_key in _ACTOR_KEYS and item not in (None, ""):
            return pseudonymize_actor(str(item), salt)
        if normalized_key in _SENSITIVE_VALUE_KEYS and item not in (None, ""):
            return f"[REDACTED:{normalized_key}]"
        if isinstance(item, str):
            return redact_sensitive_text(item, current_username=current_username).text
        if isinstance(item, Mapping):
            return {str(k): walk(v, str(k)) for k, v in item.items()}
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            return [walk(child) for child in item]
        return item

    return walk(value)


class PrivateMessageRedactor:
    """Account-bound facade that makes forgetting the account salt difficult."""

    def __init__(self, account_salt: bytes | str):
        self._account_salt = _salt_bytes(account_salt)

    def redact(
        self,
        text: str,
        *,
        actor_id: str | int,
        current_username: str = "",
    ) -> RedactionResult:
        return redact_private_message(
            text,
            actor_id=actor_id,
            account_salt=self._account_salt,
            current_username=current_username,
        )

    def redact_payload(self, value: Any, *, current_username: str = "") -> Any:
        return redact_private_payload(
            value,
            account_salt=self._account_salt,
            current_username=current_username,
        )

    def pseudonymize_identifier(self, value: str | int, *, namespace: str) -> str:
        raw = str(value or "").encode("utf-8")
        label = re.sub(r"[^a-z0-9_]+", "_", str(namespace).casefold()).strip("_")
        label = label or "id"
        digest = hmac.new(
            self._account_salt,
            b"bilibot-memory-brain:" + label.encode("ascii") + b":v1\0" + raw,
            hashlib.sha256,
        ).digest()
        token = base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:24]
        return f"{label}_{token}"


__all__ = [
    "PrivateMessageRedactor",
    "RedactionResult",
    "pseudonymize_actor",
    "redact_private_message",
    "redact_private_payload",
    "redact_sensitive_text",
]
