"""
知识库记忆系统 - KnowledgeBaseMemory

基于 astrbot_plugin_livingmemory 的设计思想，为 BiliBot 打造一个
真正的知识库记忆系统：

1. SQLite 存储 + BM25 全文检索 + FAISS 向量检索
2. 记忆原子化分类（事实/关系/偏好/情景/计划）
3. 时间衰减和重要性评估
4. 每次回复前先检索知识库
5. 支持人格化记忆（Bot以第一人称视角记录）
"""
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jieba
import jieba.analyse

logger = logging.getLogger("bilibot.knowledge_memory")


# ═══════════════════════════════════════════
#  枚举和数据模型
# ═══════════════════════════════════════════

class MemoryCategory(str, Enum):
    """记忆分类"""
    FACTUAL = "factual"       # 事实性记忆（用户信息、偏好等）
    RELATIONAL = "relational"  # 关系性记忆（用户之间的关系）
    PREFERENCE = "preference"  # 偏好记忆（喜欢什么、讨厌什么）
    EPISODIC = "episodic"      # 情景记忆（具体事件、对话）
    PLANNED = "planned"        # 计划记忆（未来的事）
    PERSONALITY = "personality"  # 人格记忆（Bot自身的性格特点）


class MemoryImportance(str, Enum):
    """记忆重要性"""
    LOW = "low"          # 不重要，容易遗忘
    MEDIUM = "medium"    # 一般重要
    HIGH = "high"        # 重要
    CORE = "core"        # 核心记忆，几乎不遗忘


@dataclass
class MemoryAtom:
    """记忆原子 - 最小的记忆单元"""
    content: str
    category: MemoryCategory = MemoryCategory.EPISODIC
    importance: MemoryImportance = MemoryImportance.MEDIUM
    importance_score: float = 0.5
    entities: List[str] = field(default_factory=list)
    user_id: Optional[str] = None
    username: Optional[str] = None
    session_id: Optional[str] = None
    persona_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    ttl_days: float = 30.0
    decay_type: str = "exponential"  # linear, exponential, step
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # 衰减分数
    @property
    def decay_score(self) -> float:
        """计算当前衰减分数 (0-1)"""
        days_since = (time.time() - self.last_accessed) / 86400.0
        if self.decay_type == "linear":
            return max(0.0, 1.0 - days_since / self.ttl_days)
        elif self.decay_type == "step":
            return 1.0 if days_since <= self.ttl_days else 0.05
        else:  # exponential
            half_life = self.ttl_days / 2.0
            return math.exp(-math.log(2) * days_since / max(0.5, half_life))
    
    def reinforce(self):
        """强化记忆（增加访问时间，延长TTL）"""
        self.last_accessed = time.time()
        self.access_count += 1
        # 每次访问延长一点TTL
        self.ttl_days = min(self.ttl_days * 1.05, 365)


# ═══════════════════════════════════════════
#  SQLite 存储层
# ═══════════════════════════════════════════

class KnowledgeBaseStore:
    """
    知识库存储层 - 基于 SQLite

    PRD V4 MEM-002：每次操作创建短连接（在工作线程内创建、使用、关闭），
    禁止主线程创建连接后交给 asyncio.to_thread 使用。
    写操作通过 threading.Lock 串行化，读操作依赖 WAL 并发。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()  # 写事务串行化
        self._ensure_data_dir()
        self._init_db()

    def _ensure_data_dir(self):
        """确保数据目录存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """PRD V4 MEM-002：创建短连接（check_same_thread=False + busy_timeout）"""
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self):
        """初始化数据库表结构（短连接：建完即关）"""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()

            # 记忆原子主表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memory_atoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'episodic',
                    importance TEXT NOT NULL DEFAULT 'medium',
                    importance_score REAL DEFAULT 0.5,
                    user_id TEXT,
                    username TEXT,
                    session_id TEXT,
                    persona_id TEXT,
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    ttl_days REAL DEFAULT 30.0,
                    decay_type TEXT DEFAULT 'exponential',
                    metadata TEXT DEFAULT '{}',
                    is_active INTEGER DEFAULT 1
                )
            """)

            # BM25 全文索引
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(content, content=memory_atoms, content_rowid=id)
            """)

            # 实体索引表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entity_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_name TEXT NOT NULL,
                    memory_id INTEGER NOT NULL,
                    FOREIGN KEY (memory_id) REFERENCES memory_atoms(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_entity_name ON entity_index(entity_name)")

            # PRD V4 MEM-003：persona_id 索引（硬过滤性能）
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON memory_atoms(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON memory_atoms(category)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_created ON memory_atoms(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_persona_id ON memory_atoms(persona_id)")

            conn.commit()
            logger.info(f"知识库数据库初始化完成: {self.db_path}")
        finally:
            conn.close()

    # ── 过滤条件构建（MEM-003 硬过滤）──

    @staticmethod
    def _build_filters(user_id: str = None, persona_id: str = None,
                       categories: List[str] = None) -> Tuple[str, list]:
        """构建硬过滤 WHERE 子句和参数

        PRD V4 MEM-003：user_id/persona_id/category 必须作为查询硬过滤条件，
        进入每条召回通路，而非 RRF 后过滤。
        """
        clauses = []
        params: list = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        if persona_id:
            clauses.append("persona_id = ?")
            params.append(str(persona_id))
        if categories:
            placeholders = ",".join("?" * len(categories))
            clauses.append(f"category IN ({placeholders})")
            params.extend(categories)
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        return where, params

    # ── 写操作（加锁串行化）──

    def add_memory(self, atom: MemoryAtom) -> int:
        """添加记忆原子到数据库（PRD V4 MEM-002：短连接 + 写锁）"""
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute("""
                    INSERT INTO memory_atoms
                    (content, category, importance, importance_score, user_id, username,
                     session_id, persona_id, created_at, last_accessed, access_count,
                     ttl_days, decay_type, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    atom.content, atom.category.value, atom.importance.value,
                    atom.importance_score, atom.user_id, atom.username,
                    atom.session_id, atom.persona_id, atom.created_at,
                    atom.last_accessed, atom.access_count, atom.ttl_days,
                    atom.decay_type, json.dumps(atom.metadata)
                ))
                memory_id = cursor.lastrowid

                # 同步到BM25索引
                try:
                    conn.execute(
                        "INSERT INTO memory_fts(rowid, content) VALUES (?, ?)",
                        (memory_id, atom.content)
                    )
                except Exception as e:
                    logger.debug(f"BM25同步失败: {e}")

                # 存储实体
                for entity in atom.entities:
                    conn.execute(
                        "INSERT INTO entity_index (entity_name, memory_id) VALUES (?, ?)",
                        (entity.lower(), memory_id)
                    )

                conn.commit()
                return memory_id
            finally:
                conn.close()

    def add_raw_atom(
        self,
        content: str,
        category: str = "episodic",
        metadata: Dict[str, Any] = None,
        user_id: str = "self",
        username: str = "Bot",
        session_id: str = "",
        persona_id: str = "",
        importance: str = "medium",
        importance_score: float = 0.5,
    ) -> int:
        """写入原始记忆原子（字符串参数，不依赖 enum）

        MEM-501：供 KnowledgeBaseMemory.write_atom 调用，
        替代 services.memory_writer.write_memory_atom 的直连 SQLite 写入。
        所有写入通过同一个 store 实例（共享锁 + 短连接）。
        """
        with self._lock:
            conn = self._get_conn()
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
                if rowid:
                    try:
                        conn.execute(
                            "INSERT INTO memory_fts(rowid, content) VALUES (?, ?)",
                            (rowid, str(content)[:2000]),
                        )
                        conn.commit()
                    except Exception as e:
                        logger.debug(f"FTS 同步失败: {e}")
                return rowid
            finally:
                conn.close()

    def purge_expired(self, max_age_days: int = 180) -> int:
        """清理过期记忆"""
        cutoff = time.time() - (max_age_days * 86400)
        with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "UPDATE memory_atoms SET is_active = 0 WHERE created_at < ? AND is_active = 1",
                    (cutoff,)
                )
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

    def prune_low_importance(self, max_count: int, forgetting_threshold: float) -> int:
        """PRD V5 Task 16：当记忆数量超过 max_count 时，遗忘 importance_score 低于阈值的记忆。

        遗忘策略（简单重要性裁剪）：
        1. 查询当前 is_active=1 的记忆总数
        2. 若总数 <= max_count，不操作
        3. 若总数 > max_count，将 importance_score < forgetting_threshold 的记忆按
           importance_score 升序、last_accessed 升序（先遗忘最不重要且最久未访问的）
           标记为 is_active=0，直到总数降至 max_count

        Args:
            max_count: 长期记忆上限（memory.max_long_term）
            forgetting_threshold: 遗忘重要性阈值（0-1，由 forgetting_score/10 换算）

        Returns:
            被遗忘的记忆数量
        """
        if max_count <= 0:
            return 0
        stats = self.get_stats()
        total = stats.get("total", 0)
        if total <= max_count:
            return 0
        excess = total - max_count
        with self._lock:
            conn = self._get_conn()
            try:
                # 先按 importance_score 升序、last_accessed 升序选择候选（仅低于阈值的）
                cursor = conn.execute(
                    "SELECT id FROM memory_atoms "
                    "WHERE is_active = 1 AND importance_score < ? "
                    "ORDER BY importance_score ASC, last_accessed ASC LIMIT ?",
                    (float(forgetting_threshold), excess)
                )
                ids = [row[0] for row in cursor.fetchall()]
                if not ids:
                    return 0
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"UPDATE memory_atoms SET is_active = 0 WHERE id IN ({placeholders})",
                    ids
                )
                # 同步清理 FTS 索引
                for mid in ids:
                    try:
                        conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (mid,))
                    except Exception:
                        pass
                conn.commit()
                return len(ids)
            finally:
                conn.close()

    def reinforce_memories_batch(self, memory_ids: List[int]):
        """批量强化记忆（单次事务）"""
        if not memory_ids:
            return
        now = time.time()
        placeholders = ",".join("?" * len(memory_ids))
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    f"UPDATE memory_atoms SET last_accessed = ?, access_count = access_count + 1 "
                    f"WHERE id IN ({placeholders})",
                    [now] + memory_ids
                )
                conn.commit()
            finally:
                conn.close()

    # ── 读操作（WAL 并发，无需锁）──

    def query(self, sql: str, params: Tuple = ()) -> List[Dict]:
        """执行查询并返回字典列表（短连接）"""
        conn = self._get_conn()
        try:
            cursor = conn.execute(sql, params)
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_by_id(self, memory_id: int) -> Optional[Dict]:
        """通过ID获取单条记忆"""
        rows = self.query(
            "SELECT * FROM memory_atoms WHERE id = ? AND is_active = 1",
            (memory_id,)
        )
        return rows[0] if rows else None

    def search_by_keyword(self, query: str, limit: int = 10,
                          user_id: str = None, persona_id: str = None,
                          categories: List[str] = None) -> List[Dict]:
        """BM25关键词搜索（jieba分词 + 词频打分）

        PRD V4 MEM-003：user_id/persona_id/categories 作为硬过滤条件进入 WHERE 子句。
        """
        if not query or not query.strip():
            return []

        filter_where, filter_params = self._build_filters(user_id, persona_id, categories)

        # 1. jieba 分词 + 提取关键词
        tokens = jieba.lcut(query.strip())
        tokens = [t for t in tokens if len(t) >= 2 and not t.isdigit()]
        if not tokens:
            tokens = [query.strip()]

        # 2. 用 TF-IDF 提取最重要的词
        try:
            tfidf_keywords = jieba.analyse.extract_tags(query, topK=5, withWeight=True)
        except Exception:
            tfidf_keywords = [(t, 1.0) for t in tokens[:3]]

        # 3. 对每个分词分别搜索
        all_results: Dict[int, Dict] = {}
        total_count = self.get_stats().get("total", 1) or 1

        for token, weight in tfidf_keywords[:5]:
            if len(token) < 2:
                continue

            # PRD 4.13：优先使用 FTS5 全文检索（硬过滤在 memory_atoms 侧）
            fts_token = token.replace('"', '""')
            fts_sql = (
                "SELECT m.* FROM memory_fts f "
                "JOIN memory_atoms m ON f.rowid = m.id "
                f'WHERE memory_fts MATCH ? AND m.is_active = 1{filter_where} LIMIT ?'
            )
            rows = self.query(fts_sql, (f'"{fts_token}"', *filter_params, limit * 3))
            if not rows:
                # FTS5 无结果时回退 LIKE
                like_token = f"%{token}%"
                like_sql = (
                    f"SELECT * FROM memory_atoms WHERE content LIKE ? AND is_active = 1{filter_where} LIMIT ?"
                )
                rows = self.query(like_sql, (like_token, *filter_params, limit * 3))
            if not rows:
                continue

            # 计算该词的文档频率 (DF)
            df_count = len(rows)
            idf = math.log(1 + (total_count - df_count + 0.5) / (df_count + 0.5))

            all_lengths = [len(r.get("content", "")) for r in rows]
            corpus_avg_len = max(sum(all_lengths) / len(all_lengths), 1) if all_lengths else 1

            for row in rows:
                doc_id = int(row.get("id", 0))
                content = row.get("content", "")

                tf = content.lower().count(token.lower())
                if tf == 0:
                    continue

                k1 = 1.5
                b = 0.75
                tf_score = ((k1 + 1) * tf) / (k1 * (1 - b + b * len(content) / corpus_avg_len) + tf)
                score = idf * tf_score * weight

                if doc_id not in all_results:
                    all_results[doc_id] = {"row": row, "score": score}
                else:
                    all_results[doc_id]["score"] += score

        sorted_results = sorted(all_results.values(), key=lambda x: x["score"], reverse=True)
        return [item["row"] for item in sorted_results[:limit]]

    def search_by_user(self, user_id: str, limit: int = 20,
                       persona_id: str = None,
                       categories: List[str] = None) -> List[Dict]:
        """查询特定用户的记忆（MEM-003：persona_id/category 硬过滤）"""
        filter_where, filter_params = self._build_filters(None, persona_id, categories)
        sql = (
            f"SELECT * FROM memory_atoms WHERE user_id = ? AND is_active = 1{filter_where} "
            f"ORDER BY created_at DESC LIMIT ?"
        )
        return self.query(sql, (str(user_id), *filter_params, limit))

    def search_by_category(self, category: str, limit: int = 20) -> List[Dict]:
        """查询特定类别的记忆"""
        return self.query(
            "SELECT * FROM memory_atoms WHERE category = ? AND is_active = 1 ORDER BY created_at DESC LIMIT ?",
            (category, limit)
        )

    def search_by_entity(self, entity: str, limit: int = 10,
                         user_id: str = None, persona_id: str = None,
                         categories: List[str] = None) -> List[Dict]:
        """通过实体搜索记忆（MEM-003：user_id/persona_id/category 硬过滤）"""
        filter_where, filter_params = self._build_filters(user_id, persona_id, categories)
        sql = (
            f"SELECT ma.* FROM memory_atoms ma "
            f"JOIN entity_index ei ON ma.id = ei.memory_id "
            f"WHERE ei.entity_name = ? AND ma.is_active = 1{filter_where} "
            f"ORDER BY ma.created_at DESC LIMIT ?"
        )
        return self.query(sql, (entity.lower(), *filter_params, limit))

    def get_recent(self, hours: int = 24, limit: int = 50) -> List[Dict]:
        """获取最近的记忆"""
        cutoff = time.time() - (hours * 3600)
        return self.query(
            "SELECT * FROM memory_atoms WHERE created_at > ? AND is_active = 1 ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit)
        )

    def get_stats(self) -> Dict[str, int]:
        """获取记忆统计"""
        total = self.query("SELECT COUNT(*) as cnt FROM memory_atoms WHERE is_active = 1")[0]["cnt"]
        by_category = {}
        for row in self.query("SELECT category, COUNT(*) as cnt FROM memory_atoms WHERE is_active = 1 GROUP BY category"):
            by_category[row["category"]] = row["cnt"]
        return {"total": total, "by_category": by_category}

    def close(self):
        """PRD V4 MEM-002：短连接模式无需关闭持久连接，方法保留为幂等空操作"""
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ═══════════════════════════════════════════
#  FAISS 向量检索层（简化版，不用FAISS库）
# ═══════════════════════════════════════════

class VectorIndex:
    """
    简化的向量索引 - 使用余弦相似度
    
    不使用FAISS库（避免依赖），而是：
    1. 使用OpenAI兼容API的embedding功能
    2. 在内存中维护向量索引
    3. 定期持久化到磁盘
    """
    
    def __init__(self, dimension: int = 0, persist_path: str = None):
        # PRD V4 MEM-005：dimension=0 表示未确定，首次 add 时自动从向量推断
        self.dimension = dimension
        self._vectors: Dict[int, List[float]] = {}  # id -> vector
        self._contents: Dict[int, str] = {}  # id -> content
        self._persist_path = persist_path
        # PRD 4.14：批量持久化，避免每次 add 都全量重写 JSON
        self._dirty_count = 0
        self._persist_batch_size = 10
        # PRD V4 MIG-004：若历史索引缺少维度元数据，标记需重建
        self.rebuild_required: bool = False

    def add(self, doc_id: int, vector: List[float], content: str):
        """添加向量

        PRD V4 MEM-005 / MIG-004：
        - dimension=0 时从首个向量自动推断并记录
        - 后续维度不匹配时停止写入并提示需要重建索引
        - rebuild_required=True 时禁止追加数据
        """
        if not vector:
            return
        # PRD V4 MIG-004：rebuild_required 时拒绝写入
        if self.rebuild_required:
            logger.error(
                "向量索引标记为 rebuild_required，禁止追加数据。"
                "请清理 vector_index.json 后重建索引。"
            )
            return
        actual_dim = len(vector)
        if self.dimension == 0:
            # 首次写入：自动设置维度
            self.dimension = actual_dim
            logger.info(f"向量索引维度已确定: {actual_dim}")
        elif actual_dim != self.dimension:
            logger.error(
                f"向量维度不匹配: 期望{self.dimension}, 实际{actual_dim}。"
                f"停止写入，需重建索引（切换 Embedding 模型后请清理 vector_index.json）"
            )
            # PRD V4 MIG-004：维度不匹配时标记 rebuild_required
            self.rebuild_required = True
            return

        self._vectors[doc_id] = vector
        self._contents[doc_id] = content

        # PRD 4.14：批量持久化（每 _persist_batch_size 次才写一次磁盘）
        self._dirty_count += 1
        if self._persist_path and self._dirty_count >= self._persist_batch_size:
            self._persist()
            self._dirty_count = 0

    def flush(self):
        """显式持久化所有待写数据"""
        if self._persist_path and self._dirty_count > 0:
            self._persist()
            self._dirty_count = 0
    
    def search(self, query_vector: List[float], top_k: int = 10) -> List[Tuple[int, float]]:
        """向量相似度搜索，返回 (doc_id, similarity)"""
        # MISC-606：rebuild_required 时维度可能不一致，numpy 路径会抛 ValueError
        # 被外层 try/except 静默吞掉，这里提前短路返回空结果并告警
        if self.rebuild_required:
            logger.warning(
                "向量索引标记为 rebuild_required，跳过搜索。"
                "请清理 vector_index.json 后重建索引。"
            )
            return []
        if not self._vectors:
            return []

        # PRD 5.2：优先使用 numpy 批量计算（性能优于纯 Python 遍历）
        try:
            import numpy as np
            ids = list(self._vectors.keys())
            matrix = np.array([self._vectors[i] for i in ids], dtype=np.float32)
            query = np.array(query_vector, dtype=np.float32)
            # 归一化后点积 = 余弦相似度
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1
            query_norm = np.linalg.norm(query) or 1
            sims = (matrix @ query) / (norms.squeeze() * query_norm)
            # 取 top_k
            top_indices = np.argsort(sims)[::-1][:top_k]
            return [(ids[i], float(sims[i])) for i in top_indices]
        except ImportError:
            # numpy 不可用时回退纯 Python
            pass

        similarities = []
        for doc_id, vec in self._vectors.items():
            sim = self._cosine_similarity(query_vector, vec)
            similarities.append((doc_id, sim))

        similarities.sort(key=lambda x: x[1], reverse=True)
        return similarities[:top_k]
    
    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """计算余弦相似度"""
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
    
    def _persist(self):
        """持久化到磁盘（PRD V4 MEM-010：临时文件 + 原子替换）"""
        if not self._persist_path:
            return
        try:
            data = {
                "dimension": self.dimension,
                "vectors": {str(k): v for k, v in self._vectors.items()},
                "contents": self._contents,
            }
            # PRD V4 MEM-010：写入临时文件后原子替换，避免写一半崩溃导致文件损坏
            tmp_path = self._persist_path + ".tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, self._persist_path)
        except Exception as e:
            logger.debug(f"向量索引持久化失败: {e}")

    def load(self):
        """从磁盘加载（PRD V4 MEM-005 / MIG-004）"""
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._vectors = {int(k): v for k, v in data.get("vectors", {}).items()}
            self._contents = data.get("contents", {})
            # PRD V4 MEM-005：恢复维度，后续 add 时校验
            file_dim = data.get("dimension", 0)
            if file_dim and file_dim > 0:
                self.dimension = int(file_dim)
            elif self._vectors:
                # PRD V4 MIG-004：历史索引缺少维度元数据但有向量数据
                # 尝试从首个向量推断维度
                first_vec = next(iter(self._vectors.values()), None)
                if first_vec:
                    self.dimension = len(first_vec)
                    logger.warning(
                        f"向量索引缺少 dimension 元数据，已从向量推断: {self.dimension}。"
                        f"建议清理后重建索引以确保一致性。"
                    )
                else:
                    # 有索引文件但无向量且无维度 → 标记需重建
                    self.rebuild_required = True
                    logger.warning("向量索引文件存在但无维度元数据且无向量，标记 rebuild_required")
            logger.info(f"向量索引已加载: {len(self._vectors)} 条向量, dimension={self.dimension}")
        except Exception as e:
            logger.warning(f"向量索引加载失败: {e}")
            # PRD V4 MIG-004：加载失败时标记需重建
            self.rebuild_required = True


# ═══════════════════════════════════════════
#  记忆处理器（LLM驱动）
# ═══════════════════════════════════════════

class MemoryProcessor:
    """
    使用LLM处理原始对话为结构化记忆
    
    核心功能：
    1. 将对话总结为记忆条目
    2. 提取实体和分类
    3. 评估重要性
    4. 以Bot的第一人称视角记录（人格化）
    """
    
    SYSTEM_PROMPT = """你是BiliBot的记忆处理器。你的任务是将对话历史转化为结构化的长期记忆。

## 记忆原则
1. **第一人称视角**：以Bot的口吻记录，如"我记得{username}说过..."
2. **保留关键信息**：用户提到的个人信息、偏好、重要事件
3. **分类标注**：将每条记忆归类为 factual/relational/preference/episodic/personality
4. **重要性评估**：1-10分，10分表示极其重要
5. **人格一致性**：记忆要符合Bot的人格设定

## 输出格式
请输出JSON数组，每个元素包含：
```json
[
  {{
    "content": "记忆内容（第一人称，简洁）",
    "category": "factual|relational|preference|episodic|personality",
    "importance_score": 7,
    "entities": ["实体1", "实体2"],
    "user_id": "用户ID",
    "username": "用户名",
    "metadata": {{}}
  }}
]
```

## 注意事项
- content不超过100字
- 将相对时间转为绝对时间（如"今天"→具体日期）
- 如果对话无聊无信息量，返回空数组[]
- 重要性评分：
  - 1-3: 闲聊寒暄
  - 4-6: 一般信息
  - 7-8: 重要偏好/事实
  - 9-10: 核心人格/重大事件
"""
    
    def __init__(self, llm_adapter, personality_system=None):
        self.llm = llm_adapter
        self.personality = personality_system
    
    async def process_conversation(self, conversation_history: List[Dict], persona_id: str = None, bot_name: str = "") -> List[MemoryAtom]:
        """
        处理对话历史，提取记忆原子
        
        Args:
            conversation_history: [{"role": "user"|"assistant"|"bot", "content": "...", "username": "..."}]
            persona_id: 人格ID
            bot_name: Bot 的名字，用于对话文本中标识 Bot 的发言
            
        Returns:
            List[MemoryAtom]
        """
        if not conversation_history:
            return []

        # 用"我"标识 Bot 的发言，让 LLM 以第一人称记录记忆
        bot_label = "我"

        # 构建对话文本
        dialog_lines = []
        for msg in conversation_history[-50:]:  # 最多50条
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if role == "user":
                msg_user = msg.get("username", "")
                prefix = f"用户({msg_user})" if msg_user else "用户"
                dialog_lines.append(f"{prefix}: {content}")
            elif role == "bot" or role == "assistant":
                dialog_lines.append(f"{bot_label}: {content}")

        dialog_text = "\n".join(dialog_lines)
        
        # 构建系统提示
        system_prompt = self.SYSTEM_PROMPT
        if persona_id and self.personality:
            try:
                persona_info = self.personality.get_personality_info(persona_id)
                if persona_info:
                    system_prompt += f"\n\nBot人格: {persona_info}"
            except:
                pass
        
        # 调用LLM
        prompt = f"""请分析以下对话，提取关键记忆：

{dialog_text}

只输出JSON数组，不要其他文字。"""
        
        # 检查LLM是否初始化
        if not self.llm:
            logger.warning("LLM未初始化，跳过记忆处理")
            return []
        
        try:
            response = await self.llm.generate(prompt, system_prompt=system_prompt, max_tokens=1500)
            if not response:
                return []
            
            # 解析JSON
            atoms = self._parse_memory_response(response)
            return atoms
            
        except Exception as e:
            logger.error(f"记忆处理失败: {e}")
            return []
    
    def _parse_memory_response(self, response: str) -> List[MemoryAtom]:
        """解析LLM返回的记忆JSON"""
        try:
            # 尝试提取JSON
            json_match = re.search(r'\[.*\]', response, re.DOTALL)
            if not json_match:
                return []
            
            data = json.loads(json_match.group())
            if not isinstance(data, list):
                return []
            
            atoms = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                
                category_map = {
                    "factual": MemoryCategory.FACTUAL,
                    "relational": MemoryCategory.RELATIONAL,
                    "preference": MemoryCategory.PREFERENCE,
                    "episodic": MemoryCategory.EPISODIC,
                    "personality": MemoryCategory.PERSONALITY,
                }
                
                cat_str = item.get("category", "episodic")
                cat = category_map.get(cat_str, MemoryCategory.EPISODIC)
                
                try:
                    raw_imp = float(item.get("importance_score", 5))
                except (ValueError, TypeError):
                    raw_imp = 5.0
                # PRD V4 MEM-006：LLM 输出 1-10，入库前除以 10 并 clamp 到 [0.0, 1.0]
                # MEM-602：raw_imp=1.0 属于 1-10 量表（最低重要性），应除以 10；
                # 用 >= 1.0 避免边界值走 else 分支被存为 1.0（最高重要性）
                if raw_imp >= 1.0:
                    imp_score = max(0.0, min(1.0, raw_imp / 10.0))
                else:
                    imp_score = max(0.0, min(1.0, raw_imp))

                importance = MemoryImportance.HIGH if imp_score >= 0.7 else MemoryImportance.MEDIUM

                atom = MemoryAtom(
                    content=item.get("content", ""),
                    category=cat,
                    importance=importance,
                    importance_score=imp_score,
                    entities=item.get("entities", []),
                    user_id=item.get("user_id"),
                    username=item.get("username"),
                    session_id=item.get("session_id"),
                    persona_id=item.get("persona_id"),
                    created_at=time.time(),
                    last_accessed=time.time(),
                    metadata=item.get("metadata", {}),
                )
                atoms.append(atom)
            
            return atoms
            
        except Exception as e:
            logger.error(f"解析记忆JSON失败: {e}")
            return []


# ═══════════════════════════════════════════
#  混合检索器
# ═══════════════════════════════════════════

class HybridRetriever:
    """
    混合检索器 - 结合关键词搜索和语义搜索
    
    检索策略：
    1. BM25关键词搜索
    2. 向量语义搜索（如果有embedding）
    3. 实体搜索
    4. 用户专属搜索
    5. RRF融合结果
    """
    
    def __init__(self, store: KnowledgeBaseStore, vector_index: VectorIndex, llm_adapter):
        self.store = store
        self.vector_index = vector_index
        self.llm = llm_adapter
    
    async def search(self, query: str, limit: int = 10,
                     user_id: str = None, persona_id: str = None,
                     categories: List[str] = None) -> List[Dict]:
        """
        混合检索记忆

        PRD V4 MEM-003：user_id/persona_id/categories 作为硬过滤条件，
        进入每条召回通路（BM25/用户/实体/向量），RRF 只融合已过滤结果。

        Args:
            query: 搜索查询
            limit: 返回数量
            user_id: 限定用户
            persona_id: 限定人格
            categories: 限定类别
        """
        if not query or not query.strip():
            return []

        # 1. BM25 / 用户 / 实体 搜索（硬过滤在 SQL WHERE 子句中，放线程执行）
        results = await asyncio.to_thread(
            self._sync_keyword_search, query, limit, user_id, persona_id, categories
        )

        # 2. 向量语义搜索（embedding 获取是 async，向量计算+get_by_id 是 sync）
        try:
            if self.llm and self.vector_index and len(self.vector_index._vectors) > 0:
                embedding = await self.llm.get_embedding(query)
                if embedding:
                    vector_rows = await asyncio.to_thread(
                        self._sync_vector_lookup, embedding, limit,
                        user_id, persona_id, categories
                    )
                    results.extend(vector_rows)
        except Exception as e:
            logger.debug(f"向量搜索跳过: {e}")

        # 3. RRF 融合（所有结果已在召回阶段硬过滤，无需后过滤）
        deduped = self._rrf_fusion(results, limit)

        # 4. 更新时间戳（SQLite UPDATE，放线程执行）
        ids_to_reinforce = [int(r.get("id", 0)) for r in deduped[:limit] if r.get("id")]
        await asyncio.to_thread(self.store.reinforce_memories_batch, ids_to_reinforce)

        return deduped[:limit]

    def _sync_keyword_search(self, query: str, limit: int,
                             user_id: str = None, persona_id: str = None,
                             categories: List[str] = None) -> List[Dict]:
        """同步执行 BM25 + 用户 + 实体搜索（硬过滤参数透传到 store 层）"""
        results: List[Dict] = []

        # BM25关键词搜索（硬过滤在 WHERE 子句）
        bm25_results = self.store.search_by_keyword(
            query, limit=limit * 2,
            user_id=user_id, persona_id=persona_id, categories=categories
        )
        for i, row in enumerate(bm25_results):
            row["search_rank"] = i + 1
            row["search_method"] = "bm25"
            results.append(row)

        # 用户专属搜索（硬过滤 persona_id + categories）
        if user_id:
            user_results = self.store.search_by_user(
                user_id, limit=limit,
                persona_id=persona_id, categories=categories
            )
            for i, row in enumerate(user_results):
                row["search_rank"] = i + 1
                row["search_method"] = "user_specific"
                results.append(row)

        # 实体搜索（硬过滤 user_id + persona_id + categories）
        entities = self._extract_entities(query)
        for entity in entities[:3]:
            entity_results = self.store.search_by_entity(
                entity, limit=5,
                user_id=user_id, persona_id=persona_id, categories=categories
            )
            for i, row in enumerate(entity_results):
                row["search_rank"] = i + 1
                row["search_method"] = f"entity:{entity}"
                results.append(row)

        return results

    def _sync_vector_lookup(self, embedding: List[float], limit: int,
                            user_id: str = None, persona_id: str = None,
                            categories: List[str] = None) -> List[Dict]:
        """同步执行向量搜索 + get_by_id（MEM-003：get_by_id 后内存硬过滤）"""
        out: List[Dict] = []
        vector_results = self.vector_index.search(embedding, limit=limit * 2)
        for i, (doc_id, score) in enumerate(vector_results):
            row = self.store.get_by_id(int(doc_id))
            if not row:
                continue
            # MEM-003：向量召回后立即硬过滤
            if user_id and row.get("user_id") != str(user_id):
                continue
            if persona_id and row.get("persona_id") != str(persona_id):
                continue
            if categories and row.get("category") not in categories:
                continue
            row["search_rank"] = i + 1
            row["search_method"] = "semantic"
            row["vector_score"] = score
            out.append(row)
        return out
    
    def _rrf_fusion(self, results: List[Dict], k: int = 10) -> List[Dict]:
        """
        Reciprocal Rank Fusion (RRF) 融合
        
        原理：每个文档的得分 = sum(1 / (k + rank))
        排名越靠前，得分越高
        """
        k_param = 60  # RRF常数
        
        doc_scores: Dict[int, float] = {}
        doc_data: Dict[int, Dict] = {}
        
        for r in results:
            doc_id = int(r.get("id", 0))
            rank = int(r.get("search_rank", 999))
            score = 1.0 / (k_param + rank)
            
            if doc_id not in doc_scores:
                doc_scores[doc_id] = 0
                doc_data[doc_id] = r
            
            doc_scores[doc_id] += score
        
        # 按RRF分数排序
        sorted_ids = sorted(doc_scores.keys(), key=lambda x: doc_scores[x], reverse=True)
        
        return [doc_data[doc_id] for doc_id in sorted_ids if doc_id in doc_data][:k]
    
    def _extract_entities(self, text: str) -> List[str]:
        """从文本中提取实体（jieba分词 + TF-IDF关键词）"""
        # 使用 jieba 的 TF-IDF 关键词提取
        try:
            keywords = jieba.analyse.extract_tags(text, topK=8)
            return [k for k in keywords if len(k) >= 2]
        except:
            pass
        
        # 降级：使用 jieba 基本分词
        tokens = jieba.lcut(text)
        # 过滤：保留 2-4 字的中文词
        entities = [t for t in tokens if 2 <= len(t) <= 4 and re.search(r'[\u4e00-\u9fff]', t)]
        # 按频率排序
        counter = Counter(entities)
        return [e for e, _ in counter.most_common(5)]
    


# ═══════════════════════════════════════════
#  主记忆引擎
# ═══════════════════════════════════════════

class KnowledgeBaseMemory:
    """
    知识库记忆系统 - 核心入口
    
    整合：
    1. 存储层（SQLite + BM25）
    2. 向量层（FAISS/余弦相似度）
    3. 处理器（LLM驱动的记忆提取）
    4. 检索器（混合检索 + RRF融合）
    5. 衰减管理（自动遗忘过期记忆）
    """
    
    def __init__(self, data_dir: str, llm_adapter, personality_system=None, memory_config=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 存储
        self.store = KnowledgeBaseStore(str(self.data_dir / "knowledge_base.db"))

        # PRD V4 MEM-005：向量维度不硬编码，从已存文件加载或首次 embedding 后自动记录
        self.vector_index = VectorIndex(
            dimension=0,  # 0 = 未确定，首次 add 时自动设置
            persist_path=str(self.data_dir / "vector_index.json")
        )
        self.vector_index.load()
        self.embedding_dimension = self.vector_index.dimension

        # LLM适配器
        self.llm = llm_adapter

        # 人格系统
        self.personality = personality_system

        # 处理器
        self.processor = MemoryProcessor(llm_adapter, personality_system)

        # 检索器
        self.retriever = HybridRetriever(self.store, self.vector_index, llm_adapter)

        # PRD V5 Task 16：记忆容量与遗忘配置（由 ConfigLoader.memory 注入）
        # memory_config 为 ConfigLoader.memory (MemoryConfig dataclass) 或 None（使用默认值）
        if memory_config is not None:
            self.max_today = getattr(memory_config, "max_today", 50)
            self.max_recent = getattr(memory_config, "max_recent", 200)
            self.max_long_term = getattr(memory_config, "max_long_term", 1000)
            self.enable_forgetting = getattr(memory_config, "enable_forgetting", True)
            self.forgetting_score = getattr(memory_config, "forgetting_score", 3.0)
            # MEM-606：max_memory_age_days 从 consolidation.long_term_age_days 读取
            self.max_memory_age_days = getattr(memory_config, "long_term_age_days", 180) or 180
        else:
            self.max_today = 50
            self.max_recent = 200
            self.max_long_term = 1000
            self.enable_forgetting = True
            self.forgetting_score = 3.0
            self.max_memory_age_days = 180

        # 旧配置（保留兼容）
        self.compress_threshold = 20

        logger.info(f"知识库记忆系统初始化完成 (向量维度={self.embedding_dimension or '未确定'})")

    def apply_memory_config(self, memory_config) -> None:
        """PRD V5 Task 16：热重载时更新记忆配置（next_task 级别生效）。

        Args:
            memory_config: ConfigLoader.memory (MemoryConfig dataclass)
        """
        if memory_config is None:
            return
        self.max_today = getattr(memory_config, "max_today", self.max_today)
        self.max_recent = getattr(memory_config, "max_recent", self.max_recent)
        self.max_long_term = getattr(memory_config, "max_long_term", self.max_long_term)
        self.enable_forgetting = getattr(memory_config, "enable_forgetting", self.enable_forgetting)
        self.forgetting_score = getattr(memory_config, "forgetting_score", self.forgetting_score)
        # MEM-606：热重载时同步更新 max_memory_age_days
        self.max_memory_age_days = getattr(memory_config, "long_term_age_days", self.max_memory_age_days) or self.max_memory_age_days
        logger.info(
            f"记忆配置已更新: max_today={self.max_today} max_recent={self.max_recent} "
            f"max_long_term={self.max_long_term} enable_forgetting={self.enable_forgetting} "
            f"forgetting_score={self.forgetting_score} max_memory_age_days={self.max_memory_age_days}"
        )
    
    async def save_memory(self, content: str, category: MemoryCategory = MemoryCategory.EPISODIC,
                          importance: MemoryImportance = MemoryImportance.MEDIUM,
                          importance_score: float = 0.5,
                          user_id: str = None, username: str = None,
                          entities: List[str] = None,
                          metadata: Dict = None,
                          session_id: str = None,
                          persona_id: str = None) -> int:
        """
        保存一条记忆到知识库

        PRD V4 REP-004：persona_id 用于记忆的账号/人格隔离。

        Args:
            content: 记忆内容
            category: 记忆类别
            importance: 重要性
            importance_score: 重要性分数(0-1)
            user_id: 关联用户ID
            username: 用户名
            entities: 实体列表
            metadata: 额外元数据
            session_id: 会话ID
            persona_id: 人格ID（记忆隔离键）

        Returns:
            int: 记忆ID
        """
        atom = MemoryAtom(
            content=content,
            category=category,
            importance=importance,
            importance_score=importance_score,
            user_id=user_id,
            username=username,
            entities=entities or [],
            metadata=metadata or {},
            session_id=session_id,
            persona_id=persona_id,
        )
        
        # 存入SQLite（PRD V3 §4.6：放线程执行，避免阻塞事件循环）
        memory_id = await asyncio.to_thread(self.store.add_memory, atom)
        
        # 获取embedding并存入向量索引（检查LLM是否初始化）
        try:
            if self.llm:
                embedding = await self.llm.get_embedding(content)
                if embedding:
                    self.vector_index.add(memory_id, embedding, content)
            else:
                logger.debug("LLM未初始化，跳过向量索引更新")
        except Exception as e:
            logger.debug(f"向量索引更新失败（不影响主流程）: {e}")
        
        return memory_id

    async def write_atom(
        self,
        content: str,
        category: str = "episodic",
        metadata: Dict[str, Any] = None,
        user_id: str = "self",
        username: str = "Bot",
        session_id: str = "",
        persona_id: str = "",
        importance: str = "medium",
        importance_score: float = 0.5,
    ) -> int:
        """MEM-501：轻量级记忆原子写入（供 MemoryWriteQueue 调用）

        替代 services.memory_writer.write_memory_atom 的直连 SQLite 写入，
        通过本实例的 store 写入，确保单一连接/锁路径。
        MEM-605：写入成功后尝试更新向量索引，使队列写入的记忆也能被
        向量语义搜索命中。embedding 失败时不阻塞写入，降级为 BM25-only。
        """
        memory_id = await asyncio.to_thread(
            self.store.add_raw_atom,
            content,
            category,
            metadata,
            user_id,
            username,
            session_id,
            persona_id,
            importance,
            importance_score,
        )

        # MEM-605：尝试更新向量索引（失败不阻塞写入，降级为 BM25-only）
        if memory_id and self.llm and content:
            try:
                embedding = await self.llm.get_embedding(content)
                if embedding:
                    self.vector_index.add(memory_id, embedding, content)
                else:
                    logger.warning(
                        f"write_atom 向量索引更新跳过：embedding 为空 "
                        f"(memory_id={memory_id})"
                    )
            except Exception as e:
                logger.warning(
                    f"write_atom 向量索引更新失败，降级为 BM25-only "
                    f"(memory_id={memory_id}): {e}"
                )

        return memory_id

    async def save_conversation_as_memory(self, conversation_history: List[Dict],
                                          user_id: str = None, username: str = None,
                                          session_id: str = None, bot_name: str = "",
                                          persona_id: str = None) -> List[int]:
        """
        将对话历史保存为结构化记忆

        PRD V4 REP-004：persona_id 用于记忆的账号/人格隔离。

        这是核心流程：
        1. 用LLM处理对话，提取记忆原子
        2. 逐个存入知识库
        3. 更新向量索引
        """
        atoms = await self.processor.process_conversation(conversation_history, persona_id=persona_id, bot_name=bot_name)

        # 获取 bot 的 user_id（从对话历史中找 assistant 消息的 user_id）
        bot_uid = None
        for msg in conversation_history:
            if msg.get("role") in ("assistant", "bot"):
                bot_uid = msg.get("user_id")
                if bot_uid:
                    break

        memory_ids = []
        for atom in atoms:
            # 判断原子来源：如果内容与 bot 回复相关，用 bot 的信息
            atom_user_id = user_id
            atom_username = username
            # 检查记忆原子是否描述的是 bot 自身的行为/回复
            atom_content = atom.content or ""
            if bot_name and (bot_name in atom_content or atom.category == "personality"):
                atom_username = bot_name
                if bot_uid:
                    atom_user_id = str(bot_uid)

            atom.user_id = atom_user_id
            atom.username = atom_username
            atom.session_id = session_id
            atom.persona_id = persona_id
            mid = await self.save_memory(
                content=atom.content,
                category=atom.category,
                importance=atom.importance,
                importance_score=atom.importance_score,
                user_id=atom_user_id,
                username=atom_username,
                entities=atom.entities,
                metadata=atom.metadata,
                session_id=session_id,
                persona_id=persona_id,
            )
            memory_ids.append(mid)
        
        logger.info(f"对话记忆已保存: {len(memory_ids)} 条原子记忆")
        return memory_ids
    
    async def search_memories(self, query: str, limit: int = 10,
                              user_id: str = None, persona_id: str = None,
                              categories: List[str] = None) -> List[Dict]:
        """
        搜索记忆 - 在每次回复前调用

        PRD V4 MEM-003：persona_id 作为硬过滤条件传入检索器。

        这是核心检索接口，会：
        1. BM25关键词搜索
        2. 用户专属搜索
        3. 实体搜索
        4. RRF融合
        """
        return await self.retriever.search(
            query=query,
            limit=limit,
            user_id=user_id,
            persona_id=persona_id,
            categories=categories
        )
    
    def get_user_memories(self, user_id: str, limit: int = 20,
                          persona_id: str = None) -> List[Dict]:
        """获取特定用户的所有记忆

        MEM-603：persona_id 作为硬过滤条件传入 store.search_by_user，
        防止多人格账号跨人格记忆泄漏。
        """
        return self.store.search_by_user(user_id, limit, persona_id=persona_id)

    async def get_user_memories_async(self, user_id: str, limit: int = 20,
                                      persona_id: str = None) -> List[Dict]:
        """PRD V3 §4.6：异步获取用户记忆（SQLite 查询放线程）

        MEM-603：persona_id 作为硬过滤条件传入 store.search_by_user，
        防止多人格账号跨人格记忆泄漏。
        """
        return await asyncio.to_thread(
            self.store.search_by_user, user_id, limit, persona_id
        )

    def get_stats(self) -> Dict:
        """获取记忆统计"""
        stats = self.store.get_stats()
        stats["vector_count"] = len(self.vector_index._vectors)
        return stats

    async def get_stats_async(self) -> Dict:
        """PRD V3 §4.6：异步获取统计（SQLite COUNT 查询放线程）"""
        return await asyncio.to_thread(self.get_stats)

    def cleanup_expired(self) -> int:
        """清理过期记忆"""
        purged = self.store.purge_expired(self.max_memory_age_days)
        if purged > 0:
            logger.info(f"清理了 {purged} 条过期记忆")
        return purged

    def forget_low_importance(self) -> int:
        """PRD V5 Task 16：基于重要性遗忘低分记忆。

        当 enable_forgetting=True 且记忆数量超过 max_long_term 时，
        将 importance_score < forgetting_score/10 的记忆标记为不活跃。

        forgetting_score 配置范围为 1-10（与 LLM 输出一致），
        内部换算为 0-1 阈值（forgetting_score / 10.0）。

        Returns:
            被遗忘的记忆数量
        """
        if not self.enable_forgetting:
            return 0
        # forgetting_score 范围 1-10，换算为 0-1 的重要性阈值
        # MEM-602：forgetting_score=1.0 属于 1-10 量表，应除以 10；
        # 用 >= 1.0 避免边界值走 else 分支使 threshold=1.0 清空整个记忆库
        threshold = float(self.forgetting_score) / 10.0 if self.forgetting_score >= 1.0 else float(self.forgetting_score)
        purged = self.store.prune_low_importance(self.max_long_term, threshold)
        if purged > 0:
            logger.info(
                f"遗忘 {purged} 条低重要性记忆 "
                f"(threshold={threshold:.2f}, max_long_term={self.max_long_term})"
            )
        return purged

    async def forget_low_importance_async(self) -> int:
        """PRD V5 Task 16：异步执行遗忘（SQLite 操作放线程）"""
        return await asyncio.to_thread(self.forget_low_importance)

    async def cleanup_expired_async(self) -> int:
        """PRD V3 §4.6：异步清理过期记忆（SQLite UPDATE 放线程）"""
        return await asyncio.to_thread(self.cleanup_expired)
    
    def migrate_old_memory(self, old_memory_list: List[Dict]):
        """
        从旧记忆系统迁移数据
        
        将现有的memory.json中的数据迁移到新的知识库系统
        """
        migrated = 0
        for mem in old_memory_list:
            content = mem.get("text", "")
            if not content:
                continue
            
            category_str = mem.get("memory_type", "chat")
            category_map = {
                "chat": MemoryCategory.EPISODIC,
                "user_summary": MemoryCategory.FACTUAL,
                "video": MemoryCategory.EPISODIC,
                "dynamic": MemoryCategory.EPISODIC,
            }
            category = category_map.get(category_str, MemoryCategory.EPISODIC)
            
            try:
                importance_map = {
                    "today": MemoryImportance.MEDIUM,
                    "recent": MemoryImportance.HIGH,
                    "long_term": MemoryImportance.CORE,
                }
                importance = importance_map.get(mem.get("level", "today"), MemoryImportance.MEDIUM)
                
                importance_score = mem.get("importance", 5) / 10.0
                
                entities = []
                if "username" in mem:
                    entities.append(mem["username"])
                
                mid = self.store.add_memory(MemoryAtom(
                    content=content,
                    category=category,
                    importance=importance,
                    importance_score=importance_score,
                    user_id=mem.get("user_id"),
                    username=mem.get("username"),
                    entities=entities,
                    metadata={"migrated_from": "old_memory", "original_rpid": mem.get("rpid", "")},
                ))
                
                # 尝试迁移embedding
                if "embedding" in mem and isinstance(mem["embedding"], list):
                    self.vector_index.add(mid, mem["embedding"], content)
                
                migrated += 1
                
            except Exception as e:
                logger.debug(f"迁移单条记忆失败: {e}")
        
        if migrated > 0:
            logger.info(f"记忆迁移完成: {migrated} 条")
        return migrated

    def flush(self):
        """MEM-501：显式 flush 向量索引（shutdown 时调用）

        与 close() 分离，支持 drain → flush → close 的优雅关闭顺序。
        flush 是幂等的（_dirty_count 清零后再次调用无副作用）。
        """
        try:
            if hasattr(self, 'vector_index') and self.vector_index:
                self.vector_index.flush()
        except Exception as e:
            logger.warning(f"flush 向量索引失败: {e}")

    def close(self):
        """关闭资源

        PRD V4 BOOT-004 / MEM-010：关闭前 flush 所有待写向量，
        确保正常关闭后最后 1~9 条尚未触发批量落盘的向量不丢失。
        """
        try:
            if hasattr(self, 'vector_index') and self.vector_index:
                self.vector_index.flush()
        except Exception as e:
            logger.warning(f"关闭时 flush 向量索引失败: {e}")
        try:
            self.store.close()
        except Exception as e:
            logger.warning(f"关闭 SQLite 存储失败: {e}")
        logger.info("知识库记忆系统已关闭")
    
    def __del__(self):
        try:
            self.close()
        except:
            pass


# ═══════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════

def importance_to_score(importance: MemoryImportance) -> float:
    """将重要性枚举转为分数"""
    return {
        MemoryImportance.LOW: 0.3,
        MemoryImportance.MEDIUM: 0.5,
        MemoryImportance.HIGH: 0.75,
        MemoryImportance.CORE: 1.0,
    }.get(importance, 0.5)


def compute_decay_score(decay_type, ttl_days, days_since):
    """计算衰减分数（供测试使用）"""
    if isinstance(decay_type, str):
        pass
    else:
        decay_type = decay_type.value if hasattr(decay_type, 'value') else str(decay_type)
    
    effective_ttl = max(1.0, ttl_days)
    days_since = max(0.0, days_since)
    
    if decay_type == "linear":
        return max(0.0, 1.0 - days_since / effective_ttl)
    elif decay_type == "step":
        return 1.0 if days_since <= effective_ttl else 0.05
    else:  # exponential
        half_life = effective_ttl / 2.0
        return math.exp(-math.log(2) * days_since / max(0.5, half_life))
