"""Retired V5 memory writer import shim.

V6 archives observations exclusively through an account-scoped
``MemoryBrainService``.  Keeping this module importable gives old extensions a
clear failure instead of silently recreating ``knowledge_base.db``.
"""

from __future__ import annotations

import warnings
from typing import Any, Dict, Optional


def write_memory_atom(
    data_dir: str,
    content: str,
    category: str = "episodic",
    metadata: Optional[Dict[str, Any]] = None,
    user_id: str = "self",
    username: str = "Bot",
    session_id: str = "",
    persona_id: str = "",
    importance: str = "medium",
    importance_score: float = 0.5,
) -> int:
    """Fail closed instead of writing a retired legacy memory database.

    Callers must inject the current account's ``MemoryBrainService`` and use
    ``archive_observation(ObservationEnvelope)``.  The compatibility signature
    intentionally remains so stale imports fail with an actionable error.
    """

    warnings.warn(
        "services.memory_writer.write_memory_atom is disabled by memory brain V6",
        DeprecationWarning,
        stacklevel=2,
    )
    raise RuntimeError(
        "legacy memory writer is disabled; inject the account-scoped "
        "MemoryBrainService and call archive_observation(ObservationEnvelope)"
    )
