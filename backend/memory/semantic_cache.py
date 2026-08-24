# -*- coding: utf-8 -*-
"""
Semantic Cache - Кеширование похожих запросов.

Функции:
- Семантический поиск в кеше
- Кеширование ответов
- Автоматическая инвалидация
- TTL и LRU политики
"""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Запись в кеше."""
    cache_id: str
    query: str
    query_hash: str
    query_embedding: Optional[List[float]]

    # Ответ
    response: str
    response_data: Dict[str, Any]

    # Метаданные
    created_at: str
    expires_at: str
    hit_count: int = 0
    last_hit_at: Optional[str] = None

    # Контекст (для инвалидации)
    context_hash: str = ""
    related_entities: List[str] = field(default_factory=list)

    def is_expired(self) -> bool:
        """Проверить истёк ли срок."""
        return datetime.fromisoformat(self.expires_at) < datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cache_id": self.cache_id,
            "query": self.query,
            "query_hash": self.query_hash,
            "response": self.response,
            "response_data": self.response_data,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "hit_count": self.hit_count,
            "last_hit_at": self.last_hit_at,
            "context_hash": self.context_hash,
            "related_entities": self.related_entities
        }


class SemanticCache:
    """
    Семантический кеш для ответов.

    Использует:
    - Exact match по хешу запроса
    - Semantic match по embedding (если доступен)
    - TTL для автоматической инвалидации
    - LRU для управления размером

    Использование:
    ```python
    cache = SemanticCache()
    await cache.initialize()

    # Проверка кеша
    cached = await cache.get("Какой статус проекта Alpha?")
    if cached:
        return cached.response

    # Сохранение в кеш
    await cache.set(
        query="Какой статус проекта Alpha?",
        response="Проект Alpha на 70% готов...",
        response_data={"progress": 70},
        ttl_hours=24
    )

    # Инвалидация при изменении данных
    await cache.invalidate_by_entity("project_alpha")
    ```
    """

    DEFAULT_TTL_HOURS = 24
    MAX_CACHE_SIZE = 10000
    SIMILARITY_THRESHOLD = 0.85  # Порог для семантического совпадения

    def __init__(
        self,
        db_path: str = "data/semantic_cache.db",
        embedding_model: Optional[str] = None
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = None
        self._initialized = False

        # Embedding модель
        self.embedding_model = embedding_model
        self._embedder = None

    async def initialize(self):
        """Инициализация."""
        if self._initialized:
            return

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Создаём таблицы
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS cache_entries (
                cache_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                query_hash TEXT NOT NULL,
                query_embedding BLOB,
                response TEXT NOT NULL,
                response_data TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                hit_count INTEGER DEFAULT 0,
                last_hit_at TEXT,
                context_hash TEXT,
                related_entities TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_cache_query_hash ON cache_entries(query_hash);
            CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
            CREATE INDEX IF NOT EXISTS idx_cache_hits ON cache_entries(hit_count DESC);
        """)

        self.conn.commit()

        # Инициализируем embedder если указан
        if self.embedding_model:
            await self._init_embedder()

        # Очищаем устаревшие записи
        await self._cleanup_expired()

        self._initialized = True
        logger.info("✅ SemanticCache initialized")

    async def _init_embedder(self):
        """Инициализация embedding модели."""
        try:
            from sentence_transformers import SentenceTransformer
            self._embedder = SentenceTransformer(self.embedding_model)
            logger.info(f"✅ Embedder loaded: {self.embedding_model}")
        except ImportError:
            logger.warning("⚠️ sentence-transformers не установлен")
        except Exception as e:
            logger.warning(f"⚠️ Failed to load embedder: {e}")

    def _hash_query(self, query: str) -> str:
        """Хеш запроса."""
        normalized = query.lower().strip()
        return hashlib.sha256(normalized.encode()).hexdigest()[:16]

    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Получить embedding текста."""
        if self._embedder is None:
            return None

        try:
            embedding = self._embedder.encode(text)
            return embedding.tolist()
        except Exception as e:
            logger.warning(f"⚠️ Embedding failed: {e}")
            return None

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Косинусное сходство."""
        a_np = np.array(a)
        b_np = np.array(b)

        dot = np.dot(a_np, b_np)
        norm_a = np.linalg.norm(a_np)
        norm_b = np.linalg.norm(b_np)

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return float(dot / (norm_a * norm_b))

    async def get(
        self,
        query: str,
        use_semantic: bool = True
    ) -> Optional[CacheEntry]:
        """
        Получить из кеша.

        Args:
            query: Запрос
            use_semantic: Использовать семантический поиск

        Returns:
            CacheEntry если найден, иначе None
        """
        if not self._initialized:
            await self.initialize()

        query_hash = self._hash_query(query)

        # 1. Exact match по хешу
        cursor = self.conn.execute("""
            SELECT * FROM cache_entries
            WHERE query_hash = ? AND expires_at > ?
        """, (query_hash, datetime.now().isoformat()))

        row = cursor.fetchone()
        if row:
            entry = self._row_to_entry(row)
            await self._record_hit(entry.cache_id)
            logger.debug(f"🎯 Cache HIT (exact): {query[:50]}...")
            return entry

        # 2. Semantic match
        if use_semantic and self._embedder:
            query_embedding = self._get_embedding(query)
            if query_embedding:
                entry = await self._semantic_search(query_embedding)
                if entry:
                    await self._record_hit(entry.cache_id)
                    logger.debug(f"🎯 Cache HIT (semantic): {query[:50]}...")
                    return entry

        logger.debug(f"❌ Cache MISS: {query[:50]}...")
        return None

    async def _semantic_search(self, query_embedding: List[float]) -> Optional[CacheEntry]:
        """Семантический поиск в кеше."""
        cursor = self.conn.execute("""
            SELECT * FROM cache_entries
            WHERE query_embedding IS NOT NULL AND expires_at > ?
            ORDER BY hit_count DESC
            LIMIT 100
        """, (datetime.now().isoformat(),))

        best_entry = None
        best_similarity = 0.0

        for row in cursor.fetchall():
            try:
                stored_embedding = json.loads(row["query_embedding"])
                similarity = self._cosine_similarity(query_embedding, stored_embedding)

                if similarity > self.SIMILARITY_THRESHOLD and similarity > best_similarity:
                    best_similarity = similarity
                    best_entry = self._row_to_entry(row)
            except Exception:
                continue

        return best_entry

    async def set(
        self,
        query: str,
        response: str,
        response_data: Optional[Dict[str, Any]] = None,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        related_entities: Optional[List[str]] = None,
        context_hash: Optional[str] = None
    ) -> str:
        """
        Сохранить в кеш.

        Args:
            query: Запрос
            response: Ответ
            response_data: Структурированные данные ответа
            ttl_hours: Время жизни в часах
            related_entities: Связанные сущности (для инвалидации)
            context_hash: Хеш контекста (для инвалидации при изменениях)

        Returns:
            cache_id
        """
        if not self._initialized:
            await self.initialize()

        query_hash = self._hash_query(query)
        cache_id = f"cache_{query_hash}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        now = datetime.now()
        expires_at = now + timedelta(hours=ttl_hours)

        # Получаем embedding
        query_embedding = self._get_embedding(query)
        embedding_json = json.dumps(query_embedding) if query_embedding else None

        self.conn.execute("""
            INSERT OR REPLACE INTO cache_entries
            (cache_id, query, query_hash, query_embedding, response, response_data,
             created_at, expires_at, hit_count, context_hash, related_entities)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (
            cache_id,
            query,
            query_hash,
            embedding_json,
            response,
            json.dumps(response_data or {}, ensure_ascii=False),
            now.isoformat(),
            expires_at.isoformat(),
            context_hash or "",
            json.dumps(related_entities or [])
        ))

        self.conn.commit()

        # Проверяем размер кеша
        await self._enforce_max_size()

        logger.debug(f"💾 Cache SET: {query[:50]}... (ttl={ttl_hours}h)")
        return cache_id

    async def _record_hit(self, cache_id: str):
        """Записать попадание."""
        self.conn.execute("""
            UPDATE cache_entries
            SET hit_count = hit_count + 1, last_hit_at = ?
            WHERE cache_id = ?
        """, (datetime.now().isoformat(), cache_id))
        self.conn.commit()

    async def invalidate(self, cache_id: str):
        """Инвалидировать запись."""
        self.conn.execute("DELETE FROM cache_entries WHERE cache_id = ?", (cache_id,))
        self.conn.commit()
        logger.debug(f"🗑️ Cache invalidated: {cache_id}")

    async def invalidate_by_query(self, query: str):
        """Инвалидировать по запросу."""
        query_hash = self._hash_query(query)
        self.conn.execute("DELETE FROM cache_entries WHERE query_hash = ?", (query_hash,))
        self.conn.commit()

    async def invalidate_by_entity(self, entity_id: str):
        """
        Инвалидировать записи связанные с сущностью.

        Используется когда данные сущности изменились.
        """
        cursor = self.conn.execute(
            "SELECT cache_id, related_entities FROM cache_entries"
        )

        to_delete = []
        for row in cursor.fetchall():
            entities = json.loads(row["related_entities"] or "[]")
            if entity_id in entities:
                to_delete.append(row["cache_id"])

        if to_delete:
            placeholders = ",".join("?" * len(to_delete))
            self.conn.execute(
                f"DELETE FROM cache_entries WHERE cache_id IN ({placeholders})",
                to_delete
            )
            self.conn.commit()
            logger.info(f"🗑️ Invalidated {len(to_delete)} cache entries for entity {entity_id}")

    async def invalidate_by_context(self, context_hash: str):
        """Инвалидировать по хешу контекста."""
        self.conn.execute(
            "DELETE FROM cache_entries WHERE context_hash = ?",
            (context_hash,)
        )
        self.conn.commit()
        logger.debug(f"🗑️ Invalidated cache entries for context {context_hash}")

    async def clear(self):
        """Очистить весь кеш."""
        self.conn.execute("DELETE FROM cache_entries")
        self.conn.commit()
        logger.info("🗑️ Cache cleared")

    async def _cleanup_expired(self):
        """Очистить устаревшие записи."""
        cursor = self.conn.execute(
            "DELETE FROM cache_entries WHERE expires_at < ?",
            (datetime.now().isoformat(),)
        )
        deleted = cursor.rowcount
        self.conn.commit()

        if deleted > 0:
            logger.info(f"🧹 Cleaned up {deleted} expired cache entries")

    async def _enforce_max_size(self):
        """Применить ограничение размера (LRU)."""
        cursor = self.conn.execute("SELECT COUNT(*) as cnt FROM cache_entries")
        count = cursor.fetchone()["cnt"]

        if count > self.MAX_CACHE_SIZE:
            # Удаляем наименее используемые
            to_delete = count - self.MAX_CACHE_SIZE
            self.conn.execute("""
                DELETE FROM cache_entries
                WHERE cache_id IN (
                    SELECT cache_id FROM cache_entries
                    ORDER BY hit_count ASC, last_hit_at ASC NULLS FIRST
                    LIMIT ?
                )
            """, (to_delete,))
            self.conn.commit()
            logger.info(f"🧹 LRU cleanup: removed {to_delete} entries")

    def _row_to_entry(self, row: sqlite3.Row) -> CacheEntry:
        """Преобразовать строку в CacheEntry."""
        return CacheEntry(
            cache_id=row["cache_id"],
            query=row["query"],
            query_hash=row["query_hash"],
            query_embedding=json.loads(row["query_embedding"]) if row["query_embedding"] else None,
            response=row["response"],
            response_data=json.loads(row["response_data"]) if row["response_data"] else {},
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            hit_count=row["hit_count"],
            last_hit_at=row["last_hit_at"],
            context_hash=row["context_hash"] or "",
            related_entities=json.loads(row["related_entities"]) if row["related_entities"] else []
        )

    async def get_stats(self) -> Dict[str, Any]:
        """Получить статистику кеша."""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.execute("""
            SELECT
                COUNT(*) as total_entries,
                SUM(hit_count) as total_hits,
                AVG(hit_count) as avg_hits,
                COUNT(CASE WHEN expires_at < ? THEN 1 END) as expired_entries
            FROM cache_entries
        """, (datetime.now().isoformat(),))

        row = cursor.fetchone()

        return {
            "total_entries": row["total_entries"] or 0,
            "total_hits": row["total_hits"] or 0,
            "avg_hits_per_entry": round(row["avg_hits"] or 0, 2),
            "expired_entries": row["expired_entries"] or 0,
            "max_size": self.MAX_CACHE_SIZE,
            "similarity_threshold": self.SIMILARITY_THRESHOLD
        }

    async def close(self):
        """Закрыть соединение."""
        if self.conn:
            self.conn.close()
            logger.info("✅ SemanticCache closed")


# Глобальный экземпляр
_semantic_cache: Optional[SemanticCache] = None


def get_semantic_cache() -> SemanticCache:
    """Получить глобальный экземпляр кеша."""
    global _semantic_cache
    if _semantic_cache is None:
        _semantic_cache = SemanticCache(
            # embedding_model берётся из config.py
        )
    return _semantic_cache


