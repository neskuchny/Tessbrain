# -*- coding: utf-8 -*-
"""
Graph Synchronizer - Синхронизация NetworkX и Graphiti.

Обеспечивает:
- Синхронизацию встреч в Graphiti
- Получение timeline для сущностей
- Обогащение узлов temporal данными
- Fallback при недоступности Graphiti

Использование:
```python
from backend.core.temporal.graph_sync import GraphSynchronizer, get_graph_synchronizer

sync = await get_graph_synchronizer(graph_builder)

# Синхронизировать встречу
result = await sync.sync_meeting_to_graphiti(
    meeting_id="meeting_123",
    transcript="...",
    metadata={"title": "Встреча по проекту"}
)

# Получить timeline сущности
timeline = await sync.get_entity_timeline("Проект Alpha")
```
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class GraphSynchronizer:
    """
    Синхронизатор между NetworkX и Graphiti.

    Обеспечивает:
    1. Добавление встреч в Graphiti как эпизодов
    2. Получение temporal данных для сущностей
    3. Обогащение узлов NetworkX временной информацией
    """

    def __init__(self, graph_builder, graphiti_client=None):
        """
        Args:
            graph_builder: Экземпляр GraphBuilder (NetworkX)
            graphiti_client: Экземпляр GraphitiClient (опционально)
        """
        self.graph = graph_builder
        self.graphiti = graphiti_client
        self._initialized = False

    async def initialize(self) -> bool:
        """
        Инициализация синхронизатора.

        Returns:
            True если успешно
        """
        try:
            from backend.core.config import flags

            if not flags.enable_graphiti:
                logger.info("ℹ️ Graphiti disabled via feature flags")
                self._initialized = True
                return True

            # Инициализируем Graphiti если не передан
            if self.graphiti is None:
                from .graphiti_client import GraphitiClient
                self.graphiti = GraphitiClient()
                await self.graphiti.connect()

            self._initialized = True
            logger.info("✅ GraphSynchronizer initialized")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize GraphSynchronizer: {e}")
            self._initialized = True  # Продолжаем работу в fallback режиме
            return False

    async def sync_meeting_to_graphiti(
        self,
        meeting_id: str,
        transcript: str,
        metadata: Optional[Dict[str, Any]] = None,
        entities: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Синхронизировать встречу в Graphiti.

        Args:
            meeting_id: ID встречи
            transcript: Транскрипт встречи
            metadata: Метаданные (title, date, project_id)
            entities: Извлечённые сущности

        Returns:
            Результат синхронизации
        """
        result = {
            "meeting_id": meeting_id,
            "synced": False,
            "episode_uuid": None,
            "entities_extracted": 0,
            "fallback_mode": True,
        }

        try:
            from backend.core.config import flags

            if not flags.enable_graphiti:
                return result

            if not self.graphiti:
                logger.warning("⚠️ Graphiti client not available")
                return result

            # Формируем название эпизода
            title = metadata.get("title", f"Meeting {meeting_id}") if metadata else f"Meeting {meeting_id}"

            # Формируем время
            meeting_date = None
            if metadata and metadata.get("date"):
                try:
                    meeting_date = datetime.fromisoformat(metadata["date"])
                except (ValueError, TypeError):
                    meeting_date = datetime.now(timezone.utc)
            else:
                meeting_date = datetime.now(timezone.utc)

            # Добавляем эпизод
            episode_result = await self.graphiti.add_episode(
                name=title,
                content=transcript[:50000],  # Ограничиваем размер
                episode_type="text",
                source_description=f"Meeting transcript: {meeting_id}",
                reference_time=meeting_date,
                group_id=metadata.get("project_id") if metadata else None,
                metadata={
                    "meeting_id": meeting_id,
                    "entities_count": len(entities) if entities else 0,
                    **(metadata or {})
                }
            )

            if episode_result.get("status") == "success":
                result["synced"] = True
                result["episode_uuid"] = episode_result.get("episode_uuid")
                result["entities_extracted"] = episode_result.get("entities_count", 0)
                result["fallback_mode"] = episode_result.get("fallback_mode", False)

                logger.info(f"✅ Meeting {meeting_id} synced to Graphiti")

            # Обновляем статистику в Graphiti
            if hasattr(self.graphiti, '_stats'):
                self.graphiti._stats["episodes_added"] += 1

            return result

        except Exception as e:
            logger.error(f"❌ Failed to sync meeting to Graphiti: {e}")
            return result

    async def get_entity_timeline(
        self,
        entity_name: str,
        limit: int = 20,
        days_back: int = 90
    ) -> List[Dict[str, Any]]:
        """
        Получить timeline упоминаний сущности.

        Args:
            entity_name: Имя сущности
            limit: Максимальное количество записей
            days_back: Сколько дней назад искать

        Returns:
            Список упоминаний с временными метками
        """
        timeline = []

        try:
            from backend.core.config import flags

            if not flags.enable_graphiti:
                # Fallback: используем NetworkX
                return await self._get_timeline_from_networkx(entity_name, limit)

            if not self.graphiti:
                return await self._get_timeline_from_networkx(entity_name, limit)

            # Поиск в Graphiti
            valid_after = datetime.now(timezone.utc) - timedelta(days=days_back)

            results = await self.graphiti.search(
                query=entity_name,
                num_results=limit,
                valid_after=valid_after
            )

            for r in results:
                timeline.append({
                    "fact": r.get("fact", ""),
                    "timestamp": r.get("valid_at"),
                    "source": "graphiti",
                    "uuid": r.get("uuid"),
                })

            # Дополняем из NetworkX если мало результатов
            if len(timeline) < limit // 2:
                nx_timeline = await self._get_timeline_from_networkx(entity_name, limit - len(timeline))
                timeline.extend(nx_timeline)

            # Сортируем по времени (новые первые)
            timeline.sort(
                key=lambda x: x.get("timestamp") or "1970-01-01",
                reverse=True
            )

            return timeline[:limit]

        except Exception as e:
            logger.error(f"❌ Failed to get entity timeline: {e}")
            return await self._get_timeline_from_networkx(entity_name, limit)

    async def _get_timeline_from_networkx(
        self,
        entity_name: str,
        limit: int
    ) -> List[Dict[str, Any]]:
        """Fallback: получить timeline из NetworkX."""
        timeline = []

        if not self.graph or not self.graph.use_networkx or not self.graph.nx_graph:
            return timeline

        # Ищем узел по имени
        target_node_id = None
        for node_id, attrs in self.graph.nx_graph.nodes(data=True):
            if attrs.get("name", "").lower() == entity_name.lower():
                target_node_id = node_id
                break

        if not target_node_id:
            return timeline

        # Получаем связи с встречами
        for _, target, attrs in self.graph.nx_graph.out_edges(target_node_id, data=True):
            target_attrs = self.graph.nx_graph.nodes.get(target, {})
            if target_attrs.get("_label") == "Meeting":
                timeline.append({
                    "fact": f"Упомянут на встрече: {target_attrs.get('title', 'Без названия')}",
                    "timestamp": target_attrs.get("date") or attrs.get("_created_at"),
                    "source": "networkx",
                    "meeting_id": target_attrs.get("meeting_id"),
                })

        for source, _, attrs in self.graph.nx_graph.in_edges(target_node_id, data=True):
            source_attrs = self.graph.nx_graph.nodes.get(source, {})
            if source_attrs.get("_label") == "Meeting":
                timeline.append({
                    "fact": f"Упомянут на встрече: {source_attrs.get('title', 'Без названия')}",
                    "timestamp": source_attrs.get("date") or attrs.get("_created_at"),
                    "source": "networkx",
                    "meeting_id": source_attrs.get("meeting_id"),
                })

        # Сортируем и ограничиваем
        timeline.sort(
            key=lambda x: x.get("timestamp") or "1970-01-01",
            reverse=True
        )

        return timeline[:limit]

    async def enrich_node_with_temporal(
        self,
        node_id: str,
        entity_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Обогатить узел temporal данными.

        Args:
            node_id: ID узла в NetworkX
            entity_name: Имя сущности (если не указано, берётся из узла)

        Returns:
            Temporal данные для узла
        """
        temporal_data = {
            "first_mention": None,
            "last_mention": None,
            "total_mentions": 0,
            "mention_frequency": 0.0,
            "timeline": [],
        }

        try:
            # Получаем имя из узла если не указано
            if not entity_name and self.graph.use_networkx:
                node = self.graph.nx_graph.nodes.get(node_id, {})
                entity_name = node.get("name", "")

            if not entity_name:
                return temporal_data

            # Получаем timeline
            timeline = await self.get_entity_timeline(entity_name, limit=50)

            if timeline:
                temporal_data["timeline"] = timeline[:10]  # Последние 10
                temporal_data["total_mentions"] = len(timeline)
                temporal_data["last_mention"] = timeline[0].get("timestamp")
                temporal_data["first_mention"] = timeline[-1].get("timestamp")

                # Вычисляем частоту (упоминаний в неделю)
                if len(timeline) >= 2:
                    try:
                        first = datetime.fromisoformat(timeline[-1]["timestamp"].replace("Z", "+00:00"))
                        last = datetime.fromisoformat(timeline[0]["timestamp"].replace("Z", "+00:00"))
                        days = (last - first).days or 1
                        temporal_data["mention_frequency"] = len(timeline) / (days / 7)
                    except (ValueError, TypeError, KeyError):
                        pass

            return temporal_data

        except Exception as e:
            logger.error(f"❌ Failed to enrich node with temporal data: {e}")
            return temporal_data

    async def get_temporal_context(
        self,
        query: str,
        time_range_days: int = 30
    ) -> Dict[str, Any]:
        """
        Получить temporal контекст для запроса.

        Args:
            query: Поисковый запрос
            time_range_days: Временной диапазон в днях

        Returns:
            Temporal контекст
        """
        context = {
            "query": query,
            "time_range_days": time_range_days,
            "facts": [],
            "entities": [],
            "source": "fallback",
        }

        try:
            from backend.core.config import flags

            if not flags.enable_graphiti or not self.graphiti:
                return context

            valid_after = datetime.now(timezone.utc) - timedelta(days=time_range_days)

            results = await self.graphiti.search(
                query=query,
                num_results=20,
                valid_after=valid_after
            )

            context["facts"] = [
                {
                    "fact": r.get("fact", ""),
                    "timestamp": r.get("valid_at"),
                }
                for r in results
            ]
            context["source"] = "graphiti" if not self.graphiti.is_fallback_mode() else "fallback"

            return context

        except Exception as e:
            logger.error(f"❌ Failed to get temporal context: {e}")
            return context


# Singleton
_synchronizer_instance: Optional[GraphSynchronizer] = None


async def get_graph_synchronizer(graph_builder) -> GraphSynchronizer:
    """
    Получить экземпляр GraphSynchronizer.

    Args:
        graph_builder: GraphBuilder instance

    Returns:
        Инициализированный GraphSynchronizer
    """
    global _synchronizer_instance

    if _synchronizer_instance is None or _synchronizer_instance.graph != graph_builder:
        _synchronizer_instance = GraphSynchronizer(graph_builder)
        await _synchronizer_instance.initialize()

    return _synchronizer_instance


