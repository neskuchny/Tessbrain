# -*- coding: utf-8 -*-
"""
Mem0 Client - Интеграция долгосрочной памяти для Tessbrain.

Обеспечивает:
- Хранение контекста разговоров
- Запоминание предпочтений пользователей
- Персонализацию ответов AI
- Семантический поиск по памяти
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

# mem0 — внешний пакет (PyPI: mem0ai). Импорт делаем мягким, чтобы при его
# отсутствии модуль всё ещё импортировался, а при попытке использования
# клиент сообщал о недоступности. Это не даёт mem0 ронять стартап api,
# когда memory-функционал не нужен.
try:
    from mem0 import Memory  # type: ignore[import-not-found]
    _MEM0_AVAILABLE = True
except ImportError:  # pragma: no cover - env without mem0ai
    Memory = None  # type: ignore[assignment, misc]
    _MEM0_AVAILABLE = False

logger = logging.getLogger(__name__)


class Mem0Client:
    """
    Клиент для работы с mem0 - слоем долгосрочной памяти.

    Особенности:
    - Автоматическое извлечение фактов из разговоров
    - Семантический поиск по памяти
    - Фильтрация по user_id, session_id, категориям
    - Поддержка метаданных
    """

    def __init__(
        self,
        llm_provider: str = "openai",
        llm_model: str = None,
        embedding_model: str = None,
        vector_store: str = "qdrant",
        qdrant_host: str = "localhost",
        qdrant_port: int = 6333,
        collection_name: str = "tessent_memory"
    ):
        """
        Инициализация mem0 клиента.

        Args:
            llm_provider: Провайдер LLM (openai, google, etc.)
            llm_model: Модель для извлечения фактов
            embedding_model: Модель для эмбеддингов
            vector_store: Хранилище векторов (qdrant, chroma, etc.)
            qdrant_host: Хост Qdrant
            qdrant_port: Порт Qdrant
            collection_name: Название коллекции
        """
        if not _MEM0_AVAILABLE:
            raise RuntimeError(
                "mem0ai package is not installed. Install it via "
                "`pip install mem0ai` to use Mem0Client."
            )
        self.collection_name = collection_name

        # Enterprise-переносимость: mem0 (вторичный слой) больше не прибит к
        # api.openai.com — llm и embedder читают OPENAI_BASE_URL и модели из
        # env, чтобы обслуживаться локальным OpenAI-совместимым endpoint'ом
        # (vLLM + локальный embeddings-сервер) в закрытом контуре.
        import os as _os
        llm_model = (llm_model
                     or _os.getenv("MEM0_LLM_MODEL")
                     or "gpt-4o-mini")
        embedding_model = (embedding_model
                           or _os.getenv("MEM0_EMBEDDING_MODEL")
                           or "text-embedding-3-small")
        _base = _os.getenv("OPENAI_BASE_URL", "").strip()
        _llm_cfg = {"model": llm_model, "temperature": 0.1,
                    "max_tokens": 2000}
        _emb_cfg = {"model": embedding_model}
        if _base:
            _llm_cfg["openai_base_url"] = _base
            _emb_cfg["openai_base_url"] = _base

        # Конфигурация mem0
        config = {
            "llm": {
                "provider": llm_provider,
                "config": _llm_cfg,
            },
            "embedder": {
                "provider": "openai",
                "config": _emb_cfg,
            },
            "vector_store": {
                "provider": vector_store,
                "config": {
                    "host": qdrant_host,
                    "port": qdrant_port,
                    "collection_name": collection_name,
                }
            },
            "version": "v1.1"  # Использовать новый формат
        }

        try:
            self.memory = Memory.from_config(config)
            logger.info(f"✅ Mem0Client initialized: {vector_store}:{collection_name}")
        except Exception as e:
            logger.warning(f"⚠️ Mem0 with Qdrant failed: {e}, using in-memory fallback")
            # Fallback на in-memory хранилище
            fallback_config = {
                "llm": {
                    "provider": llm_provider,
                    "config": {
                        "model": llm_model,
                        "temperature": 0.1,
                        "max_tokens": 2000,
                    }
                },
                "version": "v1.1"
            }
            self.memory = Memory.from_config(fallback_config)
            logger.info("✅ Mem0Client initialized with in-memory storage")

    async def add_memory(
        self,
        content: str,
        user_id: str,
        session_id: Optional[str] = None,
        agent_id: str = "tessent_brain",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Добавить память из текста.

        Args:
            content: Текст для запоминания
            user_id: ID пользователя
            session_id: ID сессии (опционально)
            agent_id: ID агента
            metadata: Дополнительные метаданные

        Returns:
            Результат добавления
        """
        try:
            meta = metadata or {}
            meta["timestamp"] = datetime.now().isoformat()

            result = self.memory.add(
                content,
                user_id=user_id,
                agent_id=agent_id,
                run_id=session_id,
                metadata=meta
            )

            logger.debug(f"✅ Memory added for user {user_id[:8]}...")
            return result

        except Exception as e:
            logger.error(f"❌ Failed to add memory: {e}")
            return {"error": str(e)}

    async def add_conversation(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        session_id: Optional[str] = None,
        agent_id: str = "tessent_brain",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Добавить память из разговора.
        Mem0 автоматически извлечёт важные факты.

        Args:
            messages: Список сообщений [{"role": "user/assistant", "content": "..."}]
            user_id: ID пользователя
            session_id: ID сессии
            agent_id: ID агента
            metadata: Дополнительные метаданные

        Returns:
            Результат добавления
        """
        try:
            meta = metadata or {}
            meta["timestamp"] = datetime.now().isoformat()
            meta["message_count"] = len(messages)

            result = self.memory.add(
                messages,
                user_id=user_id,
                agent_id=agent_id,
                run_id=session_id,
                metadata=meta
            )

            logger.debug(f"✅ Conversation ({len(messages)} msgs) added for user {user_id[:8]}...")
            return result

        except Exception as e:
            logger.error(f"❌ Failed to add conversation: {e}")
            return {"error": str(e)}

    async def search_memory(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Семантический поиск по памяти.

        Args:
            query: Поисковый запрос
            user_id: ID пользователя
            limit: Максимальное количество результатов
            filters: Дополнительные фильтры

        Returns:
            Список найденных воспоминаний
        """
        try:
            search_filters = {"user_id": user_id}
            if filters:
                search_filters = {
                    "AND": [
                        {"user_id": user_id},
                        *[{k: v} for k, v in filters.items()]
                    ]
                }

            results = self.memory.search(
                query=query,
                filters=search_filters,
                limit=limit
            )

            memories = results.get("results", []) if isinstance(results, dict) else results
            logger.debug(f"🔍 Found {len(memories)} memories for query '{query[:30]}...'")
            return memories

        except Exception as e:
            logger.error(f"❌ Memory search failed: {e}")
            return []

    async def get_all_memories(
        self,
        user_id: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Получить все воспоминания пользователя.

        Args:
            user_id: ID пользователя
            limit: Максимальное количество

        Returns:
            Список воспоминаний
        """
        try:
            results = self.memory.get_all(
                filters={"user_id": user_id}
            )

            memories = results.get("results", []) if isinstance(results, dict) else results
            return memories[:limit]

        except Exception as e:
            logger.error(f"❌ Failed to get memories: {e}")
            return []

    async def get_memory_by_id(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """
        Получить конкретное воспоминание по ID.

        Args:
            memory_id: ID воспоминания

        Returns:
            Воспоминание или None
        """
        try:
            return self.memory.get(memory_id)
        except Exception as e:
            logger.error(f"❌ Failed to get memory {memory_id}: {e}")
            return None

    async def update_memory(
        self,
        memory_id: str,
        content: str
    ) -> Dict[str, Any]:
        """
        Обновить воспоминание.

        Args:
            memory_id: ID воспоминания
            content: Новый контент

        Returns:
            Результат обновления
        """
        try:
            return self.memory.update(memory_id, content)
        except Exception as e:
            logger.error(f"❌ Failed to update memory {memory_id}: {e}")
            return {"error": str(e)}

    async def delete_memory(self, memory_id: str) -> bool:
        """
        Удалить воспоминание.

        Args:
            memory_id: ID воспоминания

        Returns:
            Успешность удаления
        """
        try:
            self.memory.delete(memory_id)
            return True
        except Exception as e:
            logger.error(f"❌ Failed to delete memory {memory_id}: {e}")
            return False

    async def delete_all_memories(self, user_id: str) -> bool:
        """
        Удалить все воспоминания пользователя.

        Args:
            user_id: ID пользователя

        Returns:
            Успешность удаления
        """
        try:
            self.memory.delete_all(filters={"user_id": user_id})
            logger.info(f"🗑️ All memories deleted for user {user_id[:8]}...")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to delete all memories: {e}")
            return False

    async def get_memory_history(
        self,
        memory_id: str
    ) -> List[Dict[str, Any]]:
        """
        Получить историю изменений воспоминания.

        Args:
            memory_id: ID воспоминания

        Returns:
            История изменений
        """
        try:
            return self.memory.history(memory_id)
        except Exception as e:
            logger.error(f"❌ Failed to get memory history: {e}")
            return []


class MemoryManager:
    """
    Высокоуровневый менеджер памяти для Tessbrain.

    Объединяет:
    - Mem0 для долгосрочной памяти
    - Граф для структурированных знаний
    - Сессионный контекст для краткосрочной памяти
    """

    def __init__(
        self,
        mem0_client: Optional[Mem0Client] = None,
        graph_builder = None
    ):
        """
        Инициализация менеджера памяти.

        Args:
            mem0_client: Клиент mem0
            graph_builder: GraphBuilder для работы с графом
        """
        self.mem0 = mem0_client or Mem0Client()
        self.graph = graph_builder
        self._session_context: Dict[str, List[Dict]] = {}  # session_id -> messages

        logger.info("✅ MemoryManager initialized")

    async def remember_conversation(
        self,
        session_id: str,
        user_id: str,
        messages: List[Dict[str, str]],
        extract_entities: bool = True
    ) -> Dict[str, Any]:
        """
        Запомнить разговор - добавить в память и опционально извлечь сущности в граф.

        Args:
            session_id: ID сессии
            user_id: ID пользователя
            messages: Сообщения разговора
            extract_entities: Извлекать ли сущности в граф

        Returns:
            Результат с добавленными воспоминаниями и сущностями
        """
        result = {
            "memories_added": 0,
            "entities_extracted": 0,
            "errors": []
        }

        # 1. Добавляем в mem0
        try:
            mem_result = await self.mem0.add_conversation(
                messages=messages,
                user_id=user_id,
                session_id=session_id,
                metadata={"source": "chat"}
            )

            if "results" in mem_result:
                result["memories_added"] = len(mem_result.get("results", []))
            elif not mem_result.get("error"):
                result["memories_added"] = 1

        except Exception as e:
            result["errors"].append(f"mem0: {e!s}")

        # 2. Извлекаем сущности в граф (если включено)
        if extract_entities and self.graph:
            try:
                # Извлекаем упомянутых людей, проекты и т.д.
                entities = await self._extract_entities_from_messages(messages)
                for entity in entities:
                    await self.graph.add_node(
                        node_id=entity["id"],
                        node_type=entity["type"],
                        properties=entity["properties"]
                    )
                result["entities_extracted"] = len(entities)
            except Exception as e:
                result["errors"].append(f"graph: {e!s}")

        return result

    async def recall(
        self,
        query: str,
        user_id: str,
        include_graph: bool = True,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Вспомнить релевантную информацию для запроса.

        Объединяет:
        - Семантический поиск по mem0
        - Поиск по графу знаний

        Args:
            query: Запрос
            user_id: ID пользователя
            include_graph: Включать ли поиск по графу
            limit: Лимит результатов

        Returns:
            Объединённые результаты
        """
        result = {
            "memories": [],
            "graph_context": [],
            "combined_context": ""
        }

        # 1. Поиск в mem0
        memories = await self.mem0.search_memory(
            query=query,
            user_id=user_id,
            limit=limit
        )
        result["memories"] = memories

        # 2. Поиск в графе
        if include_graph and self.graph:
            try:
                # Используем существующий метод поиска в графе
                graph_results = await self.graph.search_nodes(
                    query=query,
                    limit=limit
                )
                result["graph_context"] = graph_results
            except Exception as e:
                logger.warning(f"Graph search failed: {e}")

        # 2b. SUPERSEDE-оверлей: исправленные факты (через корректировки/
        # NL-редактор) перекрывают устаревшие. Поднимаем АКТУАЛЬНЫЕ значения
        # фактов, релевантных запросу, как авторитетный контекст — чтобы
        # правка памяти доходила и до этого пути поиска (best-effort).
        result["corrected_facts"] = self._supersede_overlay(query, user_id)

        # 3. Объединяем контекст
        context_parts = []

        if result["corrected_facts"]:
            corr_text = "\n".join(
                f"- {c['fact']}: {c['current_value']}"
                for c in result["corrected_facts"][:5])
            context_parts.append(
                "✅ Актуальные факты (после исправлений — приоритетны):\n" + corr_text)

        if memories:
            memory_text = "\n".join([
                f"- {m.get('memory', m.get('content', ''))}"
                for m in memories[:5]
            ])
            context_parts.append(f"📝 Из памяти:\n{memory_text}")

        if result["graph_context"]:
            graph_text = "\n".join([
                f"- {g.get('name', g.get('id', ''))}: {g.get('description', '')[:100]}"
                for g in result["graph_context"][:5]
            ])
            context_parts.append(f"🔗 Из графа знаний:\n{graph_text}")

        result["combined_context"] = "\n\n".join(context_parts)

        return result

    @staticmethod
    def _supersede_overlay(query: str, user_id: str) -> List[Dict[str, Any]]:
        """Актуальные значения исправленных фактов, релевантных запросу.

        Ключи supersede: '{uid}::{entity}::{field}'. Матчим термины запроса
        (≥4 симв.) против ключа; возвращаем CURRENT-значение. Best-effort:
        любая ошибка → []."""
        try:
            from backend.core.memory.supersede_store import SupersedeStore
            from backend.core.store.tenant_paths import (
                supersede_store_path_for_user,
            )
            path = supersede_store_path_for_user(user_id)  # требует UUID
            store = SupersedeStore(persist_path=path)
            terms = [t.lower() for t in (query or "").split() if len(t) >= 4]
            if not terms:
                return []
            seen, out = set(), []
            for term in terms:
                for key in store.keys(contains=term):
                    if key in seen:
                        continue
                    seen.add(key)
                    val = store.current(key)
                    if not val:
                        continue
                    # человекочитаемое имя факта: '{entity} {field}'
                    parts = key.split("::")
                    fact = " ".join(parts[1:]) if len(parts) > 1 else key
                    out.append({"fact": fact, "current_value": val,
                                "key": key})
            return out[:10]
        except Exception:
            return []

    async def get_user_profile(self, user_id: str) -> Dict[str, Any]:
        """
        Получить профиль пользователя на основе его воспоминаний.

        Args:
            user_id: ID пользователя

        Returns:
            Профиль с предпочтениями и фактами
        """
        memories = await self.mem0.get_all_memories(user_id=user_id, limit=50)

        profile = {
            "user_id": user_id,
            "total_memories": len(memories),
            "preferences": [],
            "facts": [],
            "interests": []
        }

        for mem in memories:
            content = mem.get("memory", mem.get("content", ""))
            categories = mem.get("categories", [])

            if "preference" in categories or "предпочт" in content.lower():
                profile["preferences"].append(content)
            elif "fact" in categories or "факт" in content.lower():
                profile["facts"].append(content)
            else:
                profile["interests"].append(content)

        return profile

    async def add_session_message(
        self,
        session_id: str,
        role: str,
        content: str
    ):
        """
        Добавить сообщение в сессионный контекст.

        Args:
            session_id: ID сессии
            role: Роль (user/assistant)
            content: Содержимое
        """
        if session_id not in self._session_context:
            self._session_context[session_id] = []

        self._session_context[session_id].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })

    async def get_session_context(
        self,
        session_id: str,
        limit: int = 10
    ) -> List[Dict[str, str]]:
        """
        Получить контекст сессии.

        Args:
            session_id: ID сессии
            limit: Количество последних сообщений

        Returns:
            Список сообщений
        """
        messages = self._session_context.get(session_id, [])
        return messages[-limit:]

    async def save_session_to_memory(
        self,
        session_id: str,
        user_id: str
    ) -> Dict[str, Any]:
        """
        Сохранить сессию в долгосрочную память.

        Args:
            session_id: ID сессии
            user_id: ID пользователя

        Returns:
            Результат сохранения
        """
        messages = self._session_context.get(session_id, [])

        if not messages:
            return {"status": "no_messages"}

        result = await self.remember_conversation(
            session_id=session_id,
            user_id=user_id,
            messages=messages
        )

        # Очищаем сессионный контекст после сохранения
        del self._session_context[session_id]

        return result

    async def _extract_entities_from_messages(
        self,
        messages: List[Dict[str, str]]
    ) -> List[Dict[str, Any]]:
        """
        Извлечь сущности из сообщений (упрощённая версия).

        Args:
            messages: Сообщения

        Returns:
            Список сущностей
        """
        # Простое извлечение - в production использовать NER или LLM
        entities = []

        for msg in messages:
            content = msg.get("content", "")

            # Ищем упоминания проектов
            if "проект" in content.lower():
                # Упрощённо - в реальности нужен NER
                pass

            # Ищем упоминания людей
            # В реальности использовать spaCy или LLM

        return entities


# Singleton instance
_mem0_client: Optional[Mem0Client] = None
_memory_manager: Optional[MemoryManager] = None


def get_mem0_client() -> Mem0Client:
    """Получить singleton instance Mem0Client."""
    global _mem0_client
    if _mem0_client is None:
        _mem0_client = Mem0Client()
    return _mem0_client


def get_memory_manager(graph_builder=None) -> MemoryManager:
    """Получить singleton instance MemoryManager."""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager(
            mem0_client=get_mem0_client(),
            graph_builder=graph_builder
        )
    return _memory_manager



