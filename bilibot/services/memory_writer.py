"""
SQLite 记忆原子写入辅助

供 Scheduler / ReplyGenerator 等业务模块将主动行为写入 memory_atoms 表，
与 api/memory.py 共享同一份 schema。

PRD V3 §9.3 要求：主动视频/动态/周总结主写入 SQLite memory_atoms（保留 JSON 备份）。
"""
import json
import logging
import sqlite3
import time
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("bilibot.memory_writer")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_atoms (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  content         TEXT    NOT NULL,
  category        TEXT    NOT NULL DEFAULT 'episodic',
  importance      TEXT    NOT NULL DEFAULT 'medium',
  importance_score REAL   DEFAULT 0.5,
  user_id         TEXT,
  username        TEXT,
  session_id      TEXT,
  persona_id      TEXT,
  created_at      REAL    NOT NULL,
  last_accessed   REAL    NOT NULL,
  access_count    INTEGER DEFAULT 0,
  ttl_days        REAL    DEFAULT 30.0,
  decay_type      TEXT    DEFAULT 'exponential',
  metadata        TEXT    DEFAULT '{}',
  is_active       INTEGER DEFAULT 1
);
-- PRD 4.12：FTS5 全文索引（与 KnowledgeBaseMemory schema 一致）
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
  USING fts5(content, content=memory_atoms, content_rowid=id);
"""


def _db_path(data_dir: str) -> Path:
    return Path(data_dir) / "knowledge_base.db"


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
    """写入一条 memory_atom，返回新行 id。

    Args:
        data_dir: 数据目录
        content: 记忆文本
        category: episodic / factual / content_video / bot_action / summary / preference
        metadata: 任意附加元数据（JSON 序列化存储）
        user_id / username / session_id / persona_id: 关联信息
        importance: low / medium / high
        importance_score: 0.0 - 1.0

    Returns:
        新记录的 id；失败返回 0
    """
    warnings.warn(
        "memory_writer.write_memory_atom is deprecated. Use KnowledgeBaseMemory.write_atom via MemoryWriteQueue instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        path = _db_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA_SQL)
        # PRD 5.3：用 try/finally 确保 conn.close()
        try:
            now = time.time()
            meta_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
            cur = conn.execute(
                "INSERT INTO memory_atoms "
                "(content, category, importance, importance_score, user_id, username, "
                " session_id, persona_id, created_at, last_accessed, metadata, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    str(content)[:2000],
                    category,
                    importance,
                    float(importance_score),
                    str(user_id),
                    str(username),
                    str(session_id),
                    str(persona_id),
                    now,
                    now,
                    meta_json,
                ),
            )
            conn.commit()
            rowid = cur.lastrowid or 0
            # PRD 4.12：同步写入 FTS5 全文索引
            if rowid:
                try:
                    conn.execute(
                        "INSERT INTO memory_fts(rowid, content) VALUES (?, ?)",
                        (rowid, str(content)[:2000]),
                    )
                    conn.commit()
                except Exception as e:
                    logger.debug(f"FTS 同步失败: {e}")
        finally:
            conn.close()
        logger.debug(f"memory_atom 写入: id={rowid} category={category}")
        return rowid
    except Exception as e:
        logger.error(f"memory_atom 写入失败: {e}")
        return 0
