# -*- coding: utf-8 -*-
"""
MemGPT-Style Memory Manager - Трёхуровневая память для Tessbrain.

Архитектура:
1. Working Context (8-16K tokens) - активная сессия
   - Хранение текущего разговора
   - User profile
   - Active task context
   - Warning at 70%, flush at 100%

2. Recall Storage (SQLite) - история запросов
   - Full conversation history
   - Query logs
   - Session metadata
   - Interaction patterns

3. Archival Storage (KG + Vector) - полная база знаний
   - ВСЕ встречи, задачи, проекты
   - Граф знаний + векторы
   - Vector-searchable, unlimited length
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Попытка импорта tiktoken для точного подсчёта токенов
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("⚠️ tiktoken not installed, using character approximation for token counting")


@dataclass
class ContextItem:
    """Элемент контекста."""
    content: str
    timestamp: datetime
    importance: float  # 0.0-1.0
    token_count: int
    item_type: str = "message"  # message, system, task, memory
    metadata: Dict[str, Any] = field(default_factory=dict)


class WorkingContextManager:
    """
    Управление Working Context (8-16K tokens).

    Уровень 1: Активный контекст сессии.
    - Хранение текущего разговора
    - User profile
    - Active task context
    - Warning at 70%, flush at 100%
    """

    def __init__(
        self,
        max_tokens: int = 16000,
        warning_threshold: float = 0.7,
        flush_threshold: float = 1.0,
        eviction_ratio: float = 0.5
    ):
        self.max_tokens = max_tokens
        self.warning_threshold = warning_threshold
        self.flush_threshold = flush_threshold
        self.eviction_ratio = eviction_ratio

        # Контексты по пользователям
        self.contexts: Dict[str, List[ContextItem]] = {}

        # Tokenizer
        if TIKTOKEN_AVAILABLE:
            try:
                self.tokenizer = tiktoken.encoding_for_model("gpt-4")
            except Exception:
                self.tokenizer = tiktoken.get_encoding("cl100k_base")
        else:
            self.tokenizer = None

        logger.info(f"✅ WorkingContextManager initialized: max={max_tokens} tokens")

    def _count_tokens(self, text: str) -> int:
        """Подсчёт токенов."""
        if self.tokenizer:
            try:
                return len(self.tokenizer.encode(text))
            except Exception as e:
                logger.warning(f"⚠️ Token counting failed: {e}")

        # Приблизительно 4 символа = 1 токен
        return len(text) // 4

    def get_context(self, user_id: str) -> List[ContextItem]:
        """Получить контекст пользователя."""
        return self.contexts.get(user_id, [])

    def get_total_tokens(self, user_id: str) -> int:
        """Получить общее количество токенов в контексте."""
        context = self.get_context(user_id)
        return sum(item.token_count for item in context)

    def get_utilization(self, user_id: str) -> float:
        """Получить процент использования контекста."""
        total = self.get_total_tokens(user_id)
        return total / self.max_tokens if self.max_tokens > 0 else 0.0

    def is_warning_threshold(self, user_id: str) -> bool:
        """Проверка достижения warning threshold (70%)."""
        return self.get_utilization(user_id) >= self.warning_threshold

    def is_flush_needed(self, user_id: str) -> bool:
        """Проверка необходимости flush (100%)."""
        return self.get_utilization(user_id) >= self.flush_threshold

    async def append(
        self,
        user_id: str,
        content: str,
        importance: float = 0.5,
        item_type: str = "message",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Добавить элемент в контекст.

        Args:
            user_id: ID пользователя
            content: Содержимое
            importance: Важность (0.0-1.0)
            item_type: Тип элемента
            metadata: Метаданные

        Returns:
            Dict с информацией о состоянии контекста
        """
        token_count = self._count_tokens(content)

        item = ContextItem(
            content=content,
            timestamp=datetime.now(timezone.utc),
            importance=importance,
            token_count=token_count,
            item_type=item_type,
            metadata=metadata or {}
        )

        # Инициализация контекста если нужно
        if user_id not in self.contexts:
            self.contexts[user_id] = []

        self.contexts[user_id].append(item)

        # Проверка статуса
        utilization = self.get_utilization(user_id)
        total_tokens = self.get_total_tokens(user_id)

        status = {
            "total_tokens": total_tokens,
            "utilization": utilization,
            "warning": self.is_warning_threshold(user_id),
            "flush_needed": self.is_flush_needed(user_id),
            "flushed": False,
            "evicted_items": []
        }

        if status["warning"] and not status["flush_needed"]:
            logger.warning(
                f"⚠️ Context warning for user {user_id[:8]}...: "
                f"{utilization:.1%} ({total_tokens}/{self.max_tokens} tokens)"
            )

        if status["flush_needed"]:
            logger.warning(
                f"🔴 Context flush needed for user {user_id[:8]}...: "
                f"{utilization:.1%} ({total_tokens}/{self.max_tokens} tokens)"
            )
            evicted = await self.flush(user_id)
            status["flushed"] = True
            status["evicted_items"] = evicted

        return status

    async def flush(self, user_id: str) -> List[ContextItem]:
        """
        Flush контекста - удаление 50% наименее важных элементов.

        Returns:
            Список удалённых элементов (для сохранения в Recall Storage)
        """
        context = self.get_context(user_id)
        if not context:
            return []

        # Сортировка по важности (ascending) - наименее важные первыми
        sorted_context = sorted(context, key=lambda x: (x.importance, x.timestamp.timestamp()))

        # Удаление 50% наименее важных
        eviction_count = int(len(sorted_context) * self.eviction_ratio)
        evicted = sorted_context[:eviction_count]
        remaining = sorted_context[eviction_count:]

        self.contexts[user_id] = remaining

        total_before = sum(item.token_count for item in context)
        total_after = sum(item.token_count for item in remaining)

        logger.info(
            f"🔄 Context flushed for user {user_id[:8]}...: "
            f"{len(evicted)} items removed, "
            f"{total_before} → {total_after} tokens "
            f"({total_after/self.max_tokens:.1%} utilization)"
        )

        return evicted

    async def clear(self, user_id: str):
        """Полная очистка контекста пользователя."""
        if user_id in self.contexts:
            del self.contexts[user_id]
            logger.info(f"🗑️ Context cleared for user {user_id[:8]}...")

    def get_context_text(self, user_id: str, max_tokens: Optional[int] = None) -> str:
        """
        Получить текст контекста для передачи в LLM.

        Args:
            user_id: ID пользователя
            max_tokens: Максимальное количество токенов

        Returns:
            Объединённый текст контекста
        """
        context = self.get_context(user_id)
        if not context:
            return ""

        # Сортировка по важности (descending) и времени
        sorted_context = sorted(
            context,
            key=lambda x: (x.importance, x.timestamp.timestamp()),
            reverse=True
        )

        if max_tokens is None:
            return "\n\n".join(item.content for item in sorted_context)

        # Собирать до max_tokens
        result = []
        total_tokens = 0

        for item in sorted_context:
            if total_tokens + item.token_count <= max_tokens:
                result.append(item.content)
                total_tokens += item.token_count
            else:
                break

        return "\n\n".join(result)

    def get_stats(self, user_id: str) -> Dict[str, Any]:
        """Получить статистику контекста."""
        context = self.get_context(user_id)
        total_tokens = self.get_total_tokens(user_id)
        utilization = self.get_utilization(user_id)

        return {
            "user_id": user_id,
            "items_count": len(context),
            "total_tokens": total_tokens,
            "max_tokens": self.max_tokens,
            "utilization": utilization,
            "warning": self.is_warning_threshold(user_id),
            "flush_needed": self.is_flush_needed(user_id),
            "avg_importance": sum(item.importance for item in context) / len(context) if context else 0.0,
            "item_types": {
                item_type: len([i for i in context if i.item_type == item_type])
                for item_type in set(i.item_type for i in context)
            } if context else {}
        }


class RecallStorageManager:
    """
    Управление Recall Storage (SQLite).

    Уровень 2: История запросов и сессий.
    - Full conversation history
    - Query logs
    - Session metadata
    - Interaction patterns
    """

    def __init__(self, db_path: str = "data/recall_storage.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn: Optional[sqlite3.Connection] = None
        self._initialized = False
        logger.info(f"✅ RecallStorageManager initialized: {db_path}")

    async def initialize(self):
        """Инициализация базы данных."""
        if self._initialized:
            return

        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Создание таблиц
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT,
                query TEXT NOT NULL,
                response TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT,
                importance REAL DEFAULT 0.5,
                token_count INTEGER DEFAULT 0
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                end_time DATETIME,
                metadata TEXT
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS interaction_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                pattern_type TEXT NOT NULL,
                pattern_data TEXT,
                frequency INTEGER DEFAULT 1,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS flushed_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                importance REAL,
                item_type TEXT,
                original_timestamp DATETIME,
                flushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT
            )
        """)

        # Индексы
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_time ON conversations(timestamp DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_patterns_user ON interaction_patterns(user_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_flushed_user ON flushed_context(user_id)")

        self.conn.commit()
        self._initialized = True
        logger.info("✅ Recall Storage database initialized")

    async def store_conversation(
        self,
        user_id: str,
        query: str,
        response: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        importance: float = 0.5,
        token_count: int = 0
    ) -> int:
        """Сохранить запись разговора."""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.execute("""
            INSERT INTO conversations (user_id, session_id, query, response, metadata, importance, token_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            session_id,
            query,
            response,
            json.dumps(metadata) if metadata else None,
            importance,
            token_count
        ))

        self.conn.commit()
        return cursor.lastrowid

    async def store_flushed_items(
        self,
        user_id: str,
        items: List[ContextItem]
    ) -> int:
        """Сохранить flushed элементы из Working Context."""
        if not self._initialized:
            await self.initialize()

        count = 0
        for item in items:
            self.conn.execute("""
                INSERT INTO flushed_context (user_id, content, importance, item_type, original_timestamp, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                item.content,
                item.importance,
                item.item_type,
                item.timestamp.isoformat(),
                json.dumps(item.metadata)
            ))
            count += 1

        self.conn.commit()
        logger.info(f"💾 Stored {count} flushed items for user {user_id[:8]}...")
        return count

    async def search_conversations(
        self,
        user_id: str,
        query: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
        min_importance: float = 0.0
    ) -> List[Dict[str, Any]]:
        """Поиск по истории разговоров."""
        if not self._initialized:
            await self.initialize()

        if query:
            cursor = self.conn.execute("""
                SELECT * FROM conversations
                WHERE user_id = ?
                  AND (query LIKE ? OR response LIKE ?)
                  AND importance >= ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (user_id, f"%{query}%", f"%{query}%", min_importance, limit, offset))
        else:
            cursor = self.conn.execute("""
                SELECT * FROM conversations
                WHERE user_id = ?
                  AND importance >= ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (user_id, min_importance, limit, offset))

        rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "session_id": row["session_id"],
                "query": row["query"],
                "response": row["response"],
                "timestamp": row["timestamp"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                "importance": row["importance"],
                "token_count": row["token_count"]
            }
            for row in rows
        ]

    async def search_flushed_context(
        self,
        user_id: str,
        query: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Поиск по flushed контексту."""
        if not self._initialized:
            await self.initialize()

        if query:
            cursor = self.conn.execute("""
                SELECT * FROM flushed_context
                WHERE user_id = ? AND content LIKE ?
                ORDER BY flushed_at DESC
                LIMIT ?
            """, (user_id, f"%{query}%", limit))
        else:
            cursor = self.conn.execute("""
                SELECT * FROM flushed_context
                WHERE user_id = ?
                ORDER BY flushed_at DESC
                LIMIT ?
            """, (user_id, limit))

        rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "content": row["content"],
                "importance": row["importance"],
                "item_type": row["item_type"],
                "original_timestamp": row["original_timestamp"],
                "flushed_at": row["flushed_at"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {}
            }
            for row in rows
        ]

    async def track_pattern(
        self,
        user_id: str,
        pattern_type: str,
        pattern_data: Dict[str, Any]
    ):
        """Отслеживание паттернов взаимодействия."""
        if not self._initialized:
            await self.initialize()

        pattern_json = json.dumps(pattern_data, sort_keys=True)

        cursor = self.conn.execute("""
            SELECT id, frequency FROM interaction_patterns
            WHERE user_id = ? AND pattern_type = ? AND pattern_data = ?
        """, (user_id, pattern_type, pattern_json))

        row = cursor.fetchone()

        if row:
            self.conn.execute("""
                UPDATE interaction_patterns
                SET frequency = frequency + 1, last_seen = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (row["id"],))
        else:
            self.conn.execute("""
                INSERT INTO interaction_patterns (user_id, pattern_type, pattern_data)
                VALUES (?, ?, ?)
            """, (user_id, pattern_type, pattern_json))

        self.conn.commit()

    async def get_user_patterns(
        self,
        user_id: str,
        pattern_type: Optional[str] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Получить паттерны пользователя."""
        if not self._initialized:
            await self.initialize()

        if pattern_type:
            cursor = self.conn.execute("""
                SELECT * FROM interaction_patterns
                WHERE user_id = ? AND pattern_type = ?
                ORDER BY frequency DESC, last_seen DESC
                LIMIT ?
            """, (user_id, pattern_type, limit))
        else:
            cursor = self.conn.execute("""
                SELECT * FROM interaction_patterns
                WHERE user_id = ?
                ORDER BY frequency DESC, last_seen DESC
                LIMIT ?
            """, (user_id, limit))

        rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "pattern_type": row["pattern_type"],
                "pattern_data": json.loads(row["pattern_data"]) if row["pattern_data"] else {},
                "frequency": row["frequency"],
                "last_seen": row["last_seen"]
            }
            for row in rows
        ]

    async def create_session(
        self,
        user_id: str,
        session_id: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """Создать новую сессию."""
        if not self._initialized:
            await self.initialize()

        try:
            cursor = self.conn.execute("""
                INSERT INTO sessions (user_id, session_id, metadata)
                VALUES (?, ?, ?)
            """, (user_id, session_id, json.dumps(metadata) if metadata else None))
            self.conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Сессия уже существует
            return 0

    async def end_session(self, session_id: str):
        """Завершить сессию."""
        if not self._initialized:
            await self.initialize()

        self.conn.execute("""
            UPDATE sessions
            SET end_time = CURRENT_TIMESTAMP
            WHERE session_id = ? AND end_time IS NULL
        """, (session_id,))
        self.conn.commit()

    async def get_stats(self, user_id: str) -> Dict[str, Any]:
        """Получить статистику пользователя."""
        if not self._initialized:
            await self.initialize()

        cursor = self.conn.execute(
            "SELECT COUNT(*) as total FROM conversations WHERE user_id = ?",
            (user_id,)
        )
        total_conversations = cursor.fetchone()["total"]

        cursor = self.conn.execute(
            "SELECT COUNT(*) as total FROM sessions WHERE user_id = ?",
            (user_id,)
        )
        total_sessions = cursor.fetchone()["total"]

        cursor = self.conn.execute(
            "SELECT COUNT(*) as total FROM interaction_patterns WHERE user_id = ?",
            (user_id,)
        )
        total_patterns = cursor.fetchone()["total"]

        cursor = self.conn.execute(
            "SELECT COUNT(*) as total FROM flushed_context WHERE user_id = ?",
            (user_id,)
        )
        total_flushed = cursor.fetchone()["total"]

        return {
            "user_id": user_id,
            "total_conversations": total_conversations,
            "total_sessions": total_sessions,
            "total_patterns": total_patterns,
            "total_flushed_items": total_flushed
        }

    async def close(self):
        """Закрыть соединение с базой данных."""
        if self.conn:
            self.conn.close()
            logger.info("✅ Recall Storage connection closed")


class ArchivalStorageManager:
    """
    Управление Archival Storage (KG + Vector).

    Уровень 3: Полная база знаний.
    - ВСЕ встречи, задачи, проекты
    - Граф знаний + векторы
    - Vector-searchable, unlimited length
    """

    def __init__(
        self,
        graph_builder=None,
        vector_indexer=None
    ):
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer
        self._initialized = False
        logger.info("✅ ArchivalStorageManager initialized")

    async def initialize(
        self,
        graph_builder=None,
        vector_indexer=None
    ):
        """Инициализация хранилищ."""
        if graph_builder:
            self.graph_builder = graph_builder
        if vector_indexer:
            self.vector_indexer = vector_indexer

        self._initialized = True
        logger.info("✅ Archival Storage initialized")

    async def search(
        self,
        query: str,
        search_type: str = "hybrid",
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Поиск в архивном хранилище.

        Args:
            query: Поисковый запрос
            search_type: Тип поиска (vector, graph, hybrid)
            limit: Максимальное количество результатов
            filters: Фильтры

        Returns:
            Список результатов поиска
        """
        results = []

        try:
            if search_type in ["vector", "hybrid"] and self.vector_indexer:
                vector_results = await self._vector_search(query, limit, filters)
                results.extend(vector_results)

            if search_type in ["graph", "hybrid"] and self.graph_builder:
                graph_results = await self._graph_search(query, limit, filters)
                results.extend(graph_results)

            # Дедупликация и сортировка
            results = self._deduplicate_results(results)
            results = sorted(results, key=lambda x: x.get("score", 0.0), reverse=True)

            logger.debug(f"🔍 Archival search: found {len(results)} results for '{query[:30]}...'")
            return results[:limit]

        except Exception as e:
            logger.error(f"❌ Archival search failed: {e}")
            return []

    async def _vector_search(
        self,
        query: str,
        limit: int,
        filters: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Векторный поиск."""
        if not self.vector_indexer:
            return []

        try:
            results = await self.vector_indexer.search(query, top_k=limit)
            return [
                {
                    "id": r.get("id"),
                    "score": r.get("score", 0.5),
                    "type": "vector",
                    "content": r.get("text", ""),
                    "metadata": r.get("metadata", {}),
                    "source": "vector_index"
                }
                for r in results
            ]
        except Exception as e:
            logger.warning(f"⚠️ Vector search failed: {e}")
            return []

    async def _graph_search(
        self,
        query: str,
        limit: int,
        filters: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Поиск по графу."""
        if not self.graph_builder:
            return []

        try:
            # Используем существующий метод поиска
            nodes = self.graph_builder.search_nodes(query, limit=limit)
            return [
                {
                    "id": n.get("id"),
                    "score": 0.7,  # Фиксированный score для graph
                    "type": "graph",
                    "content": n.get("name", "") + ": " + n.get("description", "")[:200],
                    "metadata": n,
                    "source": "knowledge_graph"
                }
                for n in nodes
            ]
        except Exception as e:
            logger.warning(f"⚠️ Graph search failed: {e}")
            return []

    def _deduplicate_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Дедупликация результатов."""
        seen_ids = set()
        unique_results = []

        for result in results:
            result_id = result.get("id")
            if result_id and result_id not in seen_ids:
                seen_ids.add(result_id)
                unique_results.append(result)
            elif not result_id:
                unique_results.append(result)

        return unique_results

    async def get_stats(self) -> Dict[str, Any]:
        """Получить статистику архивного хранилища."""
        stats = {
            "graph_nodes": 0,
            "graph_edges": 0,
            "total_vectors": 0
        }

        try:
            if self.graph_builder:
                stats["graph_nodes"] = self.graph_builder.graph.number_of_nodes()
                stats["graph_edges"] = self.graph_builder.graph.number_of_edges()

            if self.vector_indexer:
                stats["total_vectors"] = len(self.vector_indexer.memory_index) if hasattr(self.vector_indexer, 'memory_index') else 0
        except Exception as e:
            logger.error(f"❌ Failed to get archival stats: {e}")

        return stats


class MemGPTMemoryManager:
    """
    Главный оркестратор трёхуровневой памяти.

    Архитектура:
    1. Working Context (8-16K) - активная сессия
    2. Recall Storage (SQLite) - история запросов
    3. Archival Storage (KG+Vector) - полная база знаний

    Агенты автономно управляют памятью через методы:
    - get_context() - получить контекст для запроса
    - append() - добавить в контекст
    - search_recall() - поиск в истории
    - search_archival() - поиск в базе знаний
    """

    def __init__(
        self,
        working_max_tokens: int = 16000,
        recall_db_path: str = "data/recall_storage.db",
        graph_builder=None,
        vector_indexer=None
    ):
        self.working_context = WorkingContextManager(max_tokens=working_max_tokens)
        self.recall_storage = RecallStorageManager(db_path=recall_db_path)
        self.archival_storage = ArchivalStorageManager(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer
        )

        self._initialized = False
        logger.info("✅ MemGPTMemoryManager created")

    async def initialize(
        self,
        graph_builder=None,
        vector_indexer=None
    ):
        """Инициализация всех уровней памяти."""
        if self._initialized:
            logger.warning("⚠️ MemGPTMemoryManager already initialized")
            return

        try:
            await self.recall_storage.initialize()
            await self.archival_storage.initialize(graph_builder, vector_indexer)

            self._initialized = True
            logger.info("✅ MemGPTMemoryManager initialized (all 3 levels ready)")

        except Exception as e:
            logger.error(f"❌ Failed to initialize MemGPTMemoryManager: {e}")
            raise

    async def get_context(
        self,
        user_id: str,
        query: str,
        include_recall: bool = True,
        include_archival: bool = True,
        recall_limit: int = 5,
        archival_limit: int = 10
    ) -> Dict[str, Any]:
        """
        Получить контекст для запроса.

        Автоматически:
        1. Берёт Working Context
        2. Если недостаточно, ищет в Recall Storage
        3. Если всё ещё недостаточно, ищет в Archival Storage

        Args:
            user_id: ID пользователя
            query: Запрос пользователя
            include_recall: Включать ли Recall Storage
            include_archival: Включать ли Archival Storage
            recall_limit: Лимит результатов из Recall
            archival_limit: Лимит результатов из Archival

        Returns:
            Dict с контекстом из всех уровней
        """
        context = {
            "user_id": user_id,
            "query": query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "working_context": None,
            "recall_context": [],  # Always list, never None
            "archival_context": [],  # Always list, never None
            "total_tokens": 0,
            "sources": []
        }

        # УРОВЕНЬ 1: Working Context
        working_text = self.working_context.get_context_text(user_id)
        working_stats = self.working_context.get_stats(user_id)

        context["working_context"] = {
            "text": working_text,
            "stats": working_stats
        }
        context["total_tokens"] += working_stats["total_tokens"]
        context["sources"].append("working_context")

        logger.debug(
            f"📝 Working Context for {user_id[:8]}...: "
            f"{working_stats['items_count']} items, "
            f"{working_stats['total_tokens']} tokens "
            f"({working_stats['utilization']:.1%})"
        )

        # УРОВЕНЬ 2: Recall Storage
        if include_recall:
            recall_results = await self.recall_storage.search_conversations(
                user_id=user_id,
                query=query,
                limit=recall_limit
            )

            if recall_results:
                context["recall_context"] = recall_results
                context["sources"].append("recall_storage")
                logger.debug(f"🔍 Recall Storage: found {len(recall_results)} relevant conversations")

        # УРОВЕНЬ 3: Archival Storage
        if include_archival:
            archival_results = await self.archival_storage.search(
                query=query,
                search_type="hybrid",
                limit=archival_limit
            )

            if archival_results:
                context["archival_context"] = archival_results
                context["sources"].append("archival_storage")
                logger.debug(f"📚 Archival Storage: found {len(archival_results)} relevant items")

        logger.info(
            f"✅ Context assembled for {user_id[:8]}...: "
            f"sources={context['sources']}, "
            f"total_tokens={context['total_tokens']}"
        )

        return context

    async def append(
        self,
        user_id: str,
        content: str,
        importance: float = 0.5,
        item_type: str = "message",
        metadata: Optional[Dict[str, Any]] = None,
        save_to_recall: bool = True
    ) -> Dict[str, Any]:
        """
        Добавить элемент в контекст.

        Args:
            user_id: ID пользователя
            content: Содержимое
            importance: Важность (0.0-1.0)
            item_type: Тип элемента
            metadata: Метаданные
            save_to_recall: Сохранять ли flushed элементы в Recall Storage

        Returns:
            Статус операции
        """
        status = await self.working_context.append(
            user_id=user_id,
            content=content,
            importance=importance,
            item_type=item_type,
            metadata=metadata
        )

        # Если был flush, сохранить удалённые элементы в Recall Storage
        if status.get("flushed") and save_to_recall:
            evicted_items = status.get("evicted_items", [])
            if evicted_items:
                await self.recall_storage.store_flushed_items(user_id, evicted_items)

        return status

    async def store_conversation(
        self,
        user_id: str,
        query: str,
        response: str,
        session_id: Optional[str] = None,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Сохранить полный разговор (запрос + ответ).

        Сохраняется в:
        1. Working Context (если есть место)
        2. Recall Storage (всегда)
        """
        # Добавление в Working Context
        conversation_text = f"Q: {query}\nA: {response[:500]}..."
        await self.append(
            user_id=user_id,
            content=conversation_text,
            importance=importance,
            item_type="conversation",
            metadata=metadata,
            save_to_recall=False
        )

        # Сохранение в Recall Storage
        await self.recall_storage.store_conversation(
            user_id=user_id,
            query=query,
            response=response,
            session_id=session_id,
            importance=importance,
            metadata=metadata
        )

        # Отслеживание паттерна
        await self.recall_storage.track_pattern(
            user_id=user_id,
            pattern_type="query_type",
            pattern_data={"query_prefix": query[:50]}
        )

        logger.debug(f"💬 Conversation stored for {user_id[:8]}...")

    async def search_recall(
        self,
        user_id: str,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Поиск в Recall Storage."""
        return await self.recall_storage.search_conversations(
            user_id=user_id,
            query=query,
            limit=limit
        )

    async def search_archival(
        self,
        query: str,
        search_type: str = "hybrid",
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Поиск в Archival Storage."""
        return await self.archival_storage.search(
            query=query,
            search_type=search_type,
            limit=limit,
            filters=filters
        )

    async def clear_working_context(self, user_id: str):
        """Очистить Working Context пользователя."""
        await self.working_context.clear(user_id)

    async def get_stats(self, user_id: str) -> Dict[str, Any]:
        """Получить статистику по всем уровням памяти."""
        working_stats = self.working_context.get_stats(user_id)
        recall_stats = await self.recall_storage.get_stats(user_id)
        archival_stats = await self.archival_storage.get_stats()

        return {
            "user_id": user_id,
            "working_context": working_stats,
            "recall_storage": recall_stats,
            "archival_storage": archival_stats,
            "total_memory_levels": 3
        }

    # ============================================================================
    # AGENT MEMORY INTEGRATION
    # ============================================================================

    async def agent_remember(
        self,
        agent_id: str,
        content: Dict[str, Any],
        memory_type: str = "episodic",
        importance: float = 0.5,
        tags: Optional[List[str]] = None
    ) -> str:
        """
        Запомнить информацию для агента.

        Интеграция с AgentMemoryManager для персистентной памяти агентов.

        Args:
            agent_id: ID агента (например, "search_agent", "task_spec_agent")
            content: Содержимое для запоминания
            memory_type: Тип памяти (episodic, semantic, procedural)
            importance: Важность (0.0 - 1.0)
            tags: Теги для поиска

        Returns:
            ID созданного воспоминания
        """
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return ""

            from backend.core.memory.agent_memory import get_agent_memory_manager

            memory_manager = get_agent_memory_manager()
            memory_id = await memory_manager.remember(
                agent_id=agent_id,
                content=content,
                memory_type=memory_type,
                importance=importance,
                tags=tags
            )

            logger.debug(f"Agent {agent_id} remembered: {memory_id}")
            return memory_id

        except Exception as e:
            logger.error(f"❌ Agent remember failed: {e}")
            return ""

    async def agent_recall(
        self,
        agent_id: str,
        query: Optional[str] = None,
        memory_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Вспомнить информацию для агента.

        Args:
            agent_id: ID агента
            query: Поисковый запрос (для семантического поиска)
            memory_type: Фильтр по типу памяти
            tags: Фильтр по тегам
            limit: Максимальное количество результатов

        Returns:
            Список воспоминаний
        """
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return []

            from backend.core.memory.agent_memory import get_agent_memory_manager

            memory_manager = get_agent_memory_manager()
            memories = await memory_manager.recall(
                agent_id=agent_id,
                query=query,
                memory_type=memory_type,
                tags=tags,
                limit=limit
            )

            logger.debug(f"Agent {agent_id} recalled {len(memories)} memories")
            return memories

        except Exception as e:
            logger.error(f"❌ Agent recall failed: {e}")
            return []

    async def agent_consolidate(self, agent_id: str) -> Dict[str, Any]:
        """
        Консолидировать память агента (episodic → semantic).

        Args:
            agent_id: ID агента

        Returns:
            Статистика консолидации
        """
        try:
            from backend.core.config import flags
            if not flags.enable_agent_memory:
                return {"enabled": False}

            from backend.core.memory.agent_memory import get_agent_memory_manager

            memory_manager = get_agent_memory_manager()
            stats = await memory_manager.consolidate(agent_id=agent_id)

            logger.info(f"Agent {agent_id} consolidated: {stats}")
            return stats

        except Exception as e:
            logger.error(f"❌ Agent consolidate failed: {e}")
            return {"error": str(e)}

    async def close(self):
        """Закрыть все соединения."""
        await self.recall_storage.close()
        logger.info("✅ MemGPTMemoryManager closed")


# Singleton instance
_memgpt_manager: Optional[MemGPTMemoryManager] = None


def get_memgpt_manager(
    graph_builder=None,
    vector_indexer=None
) -> MemGPTMemoryManager:
    """Получить singleton instance MemGPTMemoryManager."""
    global _memgpt_manager
    if _memgpt_manager is None:
        _memgpt_manager = MemGPTMemoryManager(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer
        )
    return _memgpt_manager


async def initialize_memgpt_manager(
    graph_builder=None,
    vector_indexer=None
) -> MemGPTMemoryManager:
    """Инициализировать и получить MemGPTMemoryManager."""
    manager = get_memgpt_manager(graph_builder, vector_indexer)
    await manager.initialize(graph_builder, vector_indexer)
    return manager


