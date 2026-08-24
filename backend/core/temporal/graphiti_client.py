# -*- coding: utf-8 -*-
"""
Graphiti Client - Интеграция temporal knowledge graph для Tessbrain.

Обеспечивает:
- Хранение эпизодов (встречи, события, разговоры)
- Временные запросы (что было в определённый период)
- Извлечение сущностей и связей
- Историческая навигация по данным
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Попытка импорта Graphiti
try:
    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType
    GRAPHITI_AVAILABLE = True
except ImportError:
    GRAPHITI_AVAILABLE = False
    logger.warning("⚠️ graphiti-core not installed. Temporal features disabled.")


class GraphitiClient:
    """
    Клиент для работы с Graphiti - temporal knowledge graph.

    Особенности:
    - Автоматическое извлечение сущностей из эпизодов
    - Временные метки для всех данных
    - Поиск по времени и контенту
    - Группировка эпизодов
    - Интеграция с feature flags
    """

    def __init__(
        self,
        neo4j_uri: str | None = None,
        neo4j_user: str | None = None,
        neo4j_password: str | None = None,
        use_fallback: bool = True
    ):
        """
        Инициализация Graphiti клиента.

        Args:
            neo4j_uri: URI Neo4j (bolt://localhost:7687)
            neo4j_user: Пользователь Neo4j
            neo4j_password: Пароль Neo4j
            use_fallback: Использовать fallback если Neo4j недоступен
        """
        self.uri = neo4j_uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = neo4j_user or os.getenv("NEO4J_USER", "neo4j")
        self.password = neo4j_password or os.getenv("NEO4J_PASSWORD", "neo4j_secret")
        self.use_fallback = use_fallback

        self.graphiti: Optional[Graphiti] = None
        self._connected = False
        self._fallback_mode = False

        # Fallback storage (in-memory)
        self._episodes: List[Dict[str, Any]] = []
        self._entities: Dict[str, Dict[str, Any]] = {}
        self._relationships: List[Dict[str, Any]] = []

        # Статистика
        self._stats = {
            "episodes_added": 0,
            "searches_performed": 0,
            "fallback_operations": 0,
        }

    def is_enabled(self) -> bool:
        """Проверить включён ли Graphiti через feature flags."""
        try:
            from backend.core.config import flags
            return flags.enable_graphiti
        except ImportError:
            return False

    def is_fallback_mode(self) -> bool:
        """Проверить работает ли в fallback режиме."""
        return self._fallback_mode

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику использования."""
        return {
            **self._stats,
            "connected": self._connected,
            "fallback_mode": self._fallback_mode,
            "episodes_count": len(self._episodes),
            "entities_count": len(self._entities),
        }

    async def connect(self) -> bool:
        """
        Подключиться к Graphiti/Neo4j.

        Returns:
            True если подключение успешно
        """
        if not GRAPHITI_AVAILABLE:
            logger.warning("⚠️ Graphiti not available, using fallback mode")
            self._fallback_mode = True
            self._connected = True
            return True

        try:
            self.graphiti = Graphiti(
                uri=self.uri,
                user=self.user,
                password=self.password
            )

            # Создаём индексы
            await self.graphiti.build_indices_and_constraints()

            self._connected = True
            self._fallback_mode = False
            logger.info(f"✅ GraphitiClient connected to {self.uri}")
            return True

        except Exception as e:
            logger.warning(f"⚠️ Neo4j connection failed: {e}")

            if self.use_fallback:
                logger.info("📦 Using in-memory fallback for temporal data")
                self._fallback_mode = True
                self._connected = True
                return True

            self._connected = False
            return False

    async def close(self):
        """Закрыть соединение."""
        if self.graphiti:
            await self.graphiti.close()
            self.graphiti = None
        self._connected = False

    async def add_episode(
        self,
        name: str,
        content: str,
        episode_type: str = "text",
        source_description: str = "",
        reference_time: Optional[datetime] = None,
        group_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Добавить эпизод (встречу, событие, разговор).

        Args:
            name: Название эпизода
            content: Содержимое (транскрипт, текст, JSON)
            episode_type: Тип (text, message, json)
            source_description: Описание источника
            reference_time: Время эпизода
            group_id: ID группы (проект, пользователь)
            metadata: Дополнительные данные

        Returns:
            Результат с UUID эпизода и извлечёнными сущностями
        """
        ref_time = reference_time or datetime.now(timezone.utc)

        if self._fallback_mode:
            return await self._add_episode_fallback(
                name, content, episode_type, source_description,
                ref_time, group_id, metadata
            )

        try:
            # Определяем тип эпизода
            source_type = {
                "text": EpisodeType.text,
                "message": EpisodeType.message,
                "json": EpisodeType.json
            }.get(episode_type, EpisodeType.text)

            result = await self.graphiti.add_episode(
                name=name,
                episode_body=content,
                source=source_type,
                source_description=source_description,
                reference_time=ref_time,
                group_id=group_id
            )

            return {
                "status": "success",
                "episode_uuid": result.episode.uuid,
                "entities_count": len(result.nodes),
                "relationships_count": len(result.edges),
                "entities": [{"name": n.name, "uuid": n.uuid} for n in result.nodes],
                "relationships": [{"fact": e.fact, "uuid": e.uuid} for e in result.edges]
            }

        except Exception as e:
            logger.error(f"❌ Failed to add episode: {e}")
            return {"status": "error", "error": str(e)}

    async def _add_episode_fallback(
        self,
        name: str,
        content: str,
        episode_type: str,
        source_description: str,
        reference_time: datetime,
        group_id: Optional[str],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Fallback добавление эпизода в память."""
        import uuid

        episode_uuid = str(uuid.uuid4())

        episode = {
            "uuid": episode_uuid,
            "name": name,
            "content": content,
            "type": episode_type,
            "source_description": source_description,
            "reference_time": reference_time.isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "group_id": group_id,
            "metadata": metadata or {}
        }

        self._episodes.append(episode)

        # Простое извлечение сущностей (в production использовать LLM)
        entities = self._extract_simple_entities(content)

        return {
            "status": "success",
            "episode_uuid": episode_uuid,
            "entities_count": len(entities),
            "relationships_count": 0,
            "entities": entities,
            "relationships": [],
            "fallback_mode": True
        }

    def _extract_simple_entities(self, content: str) -> List[Dict[str, str]]:
        """Простое извлечение сущностей (fallback)."""
        import re
        import uuid

        entities = []

        # Ищем имена (слова с заглавной буквы)
        names = re.findall(r'\b[A-ZА-ЯЁ][a-zа-яё]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+)*\b', content)
        for name in set(names[:10]):  # Лимит 10
            if len(name) > 2:
                entities.append({
                    "name": name,
                    "uuid": str(uuid.uuid4())[:8]
                })

        return entities

    async def search(
        self,
        query: str,
        group_ids: Optional[List[str]] = None,
        num_results: int = 10,
        valid_after: Optional[datetime] = None,
        valid_before: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """
        Поиск по temporal graph.

        Args:
            query: Поисковый запрос
            group_ids: Фильтр по группам
            num_results: Количество результатов
            valid_after: Начало временного диапазона
            valid_before: Конец временного диапазона

        Returns:
            Список найденных фактов/связей
        """
        if self._fallback_mode:
            return await self._search_fallback(
                query, group_ids, num_results, valid_after, valid_before
            )

        try:
            from graphiti_core.search.search_filters import SearchFilters

            search_filter = None
            if valid_after or valid_before:
                search_filter = SearchFilters(
                    valid_after=valid_after,
                    valid_before=valid_before
                )

            results = await self.graphiti.search(
                query=query,
                group_ids=group_ids,
                num_results=num_results,
                search_filter=search_filter
            )

            return [
                {
                    "uuid": edge.uuid,
                    "fact": edge.fact,
                    "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
                    "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
                    "episodes_count": len(edge.episodes) if hasattr(edge, 'episodes') else 0
                }
                for edge in results
            ]

        except Exception as e:
            logger.error(f"❌ Search failed: {e}")
            return []

    async def _search_fallback(
        self,
        query: str,
        group_ids: Optional[List[str]],
        num_results: int,
        valid_after: Optional[datetime],
        valid_before: Optional[datetime]
    ) -> List[Dict[str, Any]]:
        """Fallback поиск по эпизодам в памяти."""
        query_lower = query.lower()
        results = []

        for episode in self._episodes:
            # Фильтр по группе
            if group_ids and episode.get("group_id") not in group_ids:
                continue

            # Фильтр по времени
            ref_time = datetime.fromisoformat(episode["reference_time"])
            if valid_after and ref_time < valid_after:
                continue
            if valid_before and ref_time > valid_before:
                continue

            # Простой текстовый поиск
            if query_lower in episode["content"].lower() or query_lower in episode["name"].lower():
                results.append({
                    "uuid": episode["uuid"],
                    "fact": f"{episode['name']}: {episode['content'][:200]}...",
                    "valid_at": episode["reference_time"],
                    "invalid_at": None,
                    "episodes_count": 1
                })

        return results[:num_results]

    async def retrieve_episodes(
        self,
        reference_time: Optional[datetime] = None,
        last_n: int = 10,
        group_ids: Optional[List[str]] = None,
        episode_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Получить эпизоды по времени.

        Args:
            reference_time: Точка отсчёта
            last_n: Количество последних эпизодов
            group_ids: Фильтр по группам
            episode_type: Фильтр по типу

        Returns:
            Список эпизодов
        """
        if self._fallback_mode:
            return await self._retrieve_episodes_fallback(
                reference_time, last_n, group_ids, episode_type
            )

        try:
            ref_time = reference_time or datetime.now(timezone.utc)

            source_type = None
            if episode_type:
                source_type = {
                    "text": EpisodeType.text,
                    "message": EpisodeType.message,
                    "json": EpisodeType.json
                }.get(episode_type)

            episodes = await self.graphiti.retrieve_episodes(
                reference_time=ref_time,
                last_n=last_n,
                group_ids=group_ids,
                source=source_type
            )

            return [
                {
                    "uuid": ep.uuid,
                    "name": ep.name,
                    "content": ep.content[:500] if ep.content else "",
                    "created_at": ep.created_at.isoformat() if ep.created_at else None,
                    "valid_at": ep.valid_at.isoformat() if ep.valid_at else None,
                    "source": ep.source.value if ep.source else None
                }
                for ep in episodes
            ]

        except Exception as e:
            logger.error(f"❌ Retrieve episodes failed: {e}")
            return []

    async def _retrieve_episodes_fallback(
        self,
        reference_time: Optional[datetime],
        last_n: int,
        group_ids: Optional[List[str]],
        episode_type: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Fallback получение эпизодов."""
        ref_time = reference_time or datetime.now(timezone.utc)

        filtered = []
        for ep in self._episodes:
            # Фильтр по группе
            if group_ids and ep.get("group_id") not in group_ids:
                continue

            # Фильтр по типу
            if episode_type and ep.get("type") != episode_type:
                continue

            # Фильтр по времени
            ep_time = datetime.fromisoformat(ep["reference_time"])
            if ep_time <= ref_time:
                filtered.append(ep)

        # Сортировка по времени (новые первые)
        filtered.sort(key=lambda x: x["reference_time"], reverse=True)

        return [
            {
                "uuid": ep["uuid"],
                "name": ep["name"],
                "content": ep["content"][:500],
                "created_at": ep["created_at"],
                "valid_at": ep["reference_time"],
                "source": ep["type"]
            }
            for ep in filtered[:last_n]
        ]

    async def delete_episode(self, episode_uuid: str) -> bool:
        """
        Удалить эпизод.

        Args:
            episode_uuid: UUID эпизода

        Returns:
            Успешность удаления
        """
        if self._fallback_mode:
            self._episodes = [e for e in self._episodes if e["uuid"] != episode_uuid]
            return True

        try:
            await self.graphiti.remove_episode(episode_uuid)
            return True
        except Exception as e:
            logger.error(f"❌ Delete episode failed: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику."""
        return {
            "connected": self._connected,
            "fallback_mode": self._fallback_mode,
            "episodes_count": len(self._episodes) if self._fallback_mode else "N/A",
            "entities_count": len(self._entities) if self._fallback_mode else "N/A"
        }


class TemporalManager:
    """
    Высокоуровневый менеджер temporal данных.

    Объединяет:
    - Graphiti для temporal graph
    - Интеграцию со встречами
    - Временные запросы
    """

    def __init__(
        self,
        graphiti_client: Optional[GraphitiClient] = None
    ):
        """
        Инициализация менеджера.

        Args:
            graphiti_client: Клиент Graphiti
        """
        self.graphiti = graphiti_client or GraphitiClient()
        self._initialized = False

    async def initialize(self) -> bool:
        """Инициализировать менеджер."""
        if self._initialized:
            return True

        success = await self.graphiti.connect()
        self._initialized = success

        if success:
            logger.info("✅ TemporalManager initialized")

        return success

    async def add_meeting_episode(
        self,
        meeting_id: str,
        title: str,
        transcript: str,
        meeting_time: datetime,
        project_id: Optional[str] = None,
        participants: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Добавить встречу как temporal episode.

        Args:
            meeting_id: ID встречи
            title: Название
            transcript: Транскрипт
            meeting_time: Время встречи
            project_id: ID проекта
            participants: Участники

        Returns:
            Результат добавления
        """
        if not self._initialized:
            await self.initialize()

        metadata = {
            "meeting_id": meeting_id,
            "participants": participants or []
        }

        return await self.graphiti.add_episode(
            name=f"Meeting: {title}",
            content=transcript,
            episode_type="text",
            source_description=f"Meeting transcript from {meeting_time.date()}",
            reference_time=meeting_time,
            group_id=project_id or "general",
            metadata=metadata
        )

    async def add_chat_episode(
        self,
        session_id: str,
        user_id: str,
        messages: List[Dict[str, str]],
        chat_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Добавить чат как temporal episode.

        Args:
            session_id: ID сессии
            user_id: ID пользователя
            messages: Сообщения
            chat_time: Время чата

        Returns:
            Результат добавления
        """
        if not self._initialized:
            await self.initialize()

        # Форматируем сообщения
        content = "\n".join([
            f"{m['role'].upper()}: {m['content']}"
            for m in messages
        ])

        return await self.graphiti.add_episode(
            name=f"Chat session {session_id[:8]}",
            content=content,
            episode_type="message",
            source_description="Chat conversation",
            reference_time=chat_time or datetime.now(timezone.utc),
            group_id=user_id,
            metadata={"session_id": session_id}
        )

    async def get_timeline(
        self,
        group_id: str,
        days_back: int = 30,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Получить timeline событий.

        Args:
            group_id: ID группы (проект, пользователь)
            days_back: Дней назад
            limit: Лимит событий

        Returns:
            Список событий с временными метками
        """
        if not self._initialized:
            await self.initialize()

        reference_time = datetime.now(timezone.utc)

        episodes = await self.graphiti.retrieve_episodes(
            reference_time=reference_time,
            last_n=limit,
            group_ids=[group_id]
        )

        return episodes

    async def search_temporal(
        self,
        query: str,
        group_ids: Optional[List[str]] = None,
        time_range_days: int = 30,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        Temporal поиск с учётом времени.

        Args:
            query: Поисковый запрос
            group_ids: Группы для поиска
            time_range_days: Временной диапазон
            limit: Лимит результатов

        Returns:
            Результаты с временным контекстом
        """
        if not self._initialized:
            await self.initialize()

        now = datetime.now(timezone.utc)
        valid_after = now - timedelta(days=time_range_days)

        results = await self.graphiti.search(
            query=query,
            group_ids=group_ids,
            num_results=limit,
            valid_after=valid_after,
            valid_before=now
        )

        return {
            "query": query,
            "time_range": {
                "from": valid_after.isoformat(),
                "to": now.isoformat()
            },
            "results_count": len(results),
            "results": results
        }

    async def get_entity_history(
        self,
        entity_name: str,
        group_ids: Optional[List[str]] = None,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Получить историю упоминаний сущности.

        Args:
            entity_name: Имя сущности
            group_ids: Группы для поиска
            limit: Лимит результатов

        Returns:
            История упоминаний
        """
        if not self._initialized:
            await self.initialize()

        results = await self.graphiti.search(
            query=entity_name,
            group_ids=group_ids,
            num_results=limit
        )

        # Сортируем по времени
        sorted_results = sorted(
            results,
            key=lambda x: x.get("valid_at", ""),
            reverse=True
        )

        return sorted_results


# Singleton instances
_graphiti_client: Optional[GraphitiClient] = None
_temporal_manager: Optional[TemporalManager] = None


def get_graphiti_client() -> GraphitiClient:
    """Получить singleton instance GraphitiClient."""
    global _graphiti_client
    if _graphiti_client is None:
        _graphiti_client = GraphitiClient()
    return _graphiti_client


def get_temporal_manager() -> TemporalManager:
    """Получить singleton instance TemporalManager."""
    global _temporal_manager
    if _temporal_manager is None:
        _temporal_manager = TemporalManager(get_graphiti_client())
    return _temporal_manager



