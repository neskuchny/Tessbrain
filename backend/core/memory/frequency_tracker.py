# -*- coding: utf-8 -*-
"""
Frequency Tracker - Отслеживание частоты упоминаний сущностей.

Принцип: Часто упоминаемые сущности более важны и должны получать приоритет.

Применение в Tessbrain:
- Отслеживание общего количества упоминаний
- Расчёт frequency_score для ранжирования
- Определение trending сущностей за период
- Приоритизация при поиске

Использование:
```python
from backend.core.memory.frequency_tracker import FrequencyTracker

tracker = FrequencyTracker(graph_builder)

# При обработке встречи
await tracker.track_meeting_entities(
    meeting_id="meeting_123",
    entities=[{"id": "entity_1", "name": "Проект А"}]
)

# Получить топ упоминаемых
top = tracker.get_top_mentioned(limit=10)

# Получить trending за неделю
trending = tracker.get_trending(days=7, limit=5)
```
"""
import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FrequencyTracker:
    """
    Отслеживание частоты упоминаний сущностей.

    Основные функции:
    1. Подсчёт упоминаний сущностей
    2. Расчёт нормализованного frequency_score
    3. Определение trending сущностей
    4. Хранение истории упоминаний
    """

    def __init__(self, graph_builder):
        """
        Args:
            graph_builder: Экземпляр GraphBuilder
        """
        self.graph = graph_builder

        # Кэш для быстрого доступа к частотам
        # {entity_id: {"total": N, "by_meeting": {meeting_id: count}, "last_updated": datetime}}
        self._frequency_cache: Dict[str, Dict[str, Any]] = {}

        # История упоминаний для trending
        # [(entity_id, meeting_id, timestamp, count)]
        self._mention_history: List[tuple] = []

        # Кэш для top_mentioned (обновляется раз в 5 минут)
        self._top_mentioned_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._top_mentioned_cache_time: Dict[str, datetime] = {}
        self._cache_ttl_seconds: int = 300  # 5 минут

    async def track_mention(
        self,
        entity_id: str,
        meeting_id: str,
        count: int = 1,
        entity_name: Optional[str] = None
    ) -> bool:
        """
        Отследить упоминание сущности.

        Args:
            entity_id: ID сущности
            meeting_id: ID встречи
            count: Количество упоминаний
            entity_name: Имя сущности (для логирования)

        Returns:
            True если успешно
        """
        try:
            from backend.core.config import flags
            if not flags.enable_frequency_tracking:
                return True

            # Обновляем граф
            success = await self.graph.increment_node_mentions(
                node_id=entity_id,
                meeting_id=meeting_id,
                count=count
            )

            if success:
                # Обновляем кэш
                if entity_id not in self._frequency_cache:
                    self._frequency_cache[entity_id] = {
                        "total": 0,
                        "by_meeting": {},
                        "last_updated": None,
                        "name": entity_name or "",
                    }

                cache = self._frequency_cache[entity_id]
                cache["total"] += count
                cache["by_meeting"][meeting_id] = cache["by_meeting"].get(meeting_id, 0) + count
                cache["last_updated"] = datetime.now(timezone.utc)

                if entity_name:
                    cache["name"] = entity_name

                # Добавляем в историю
                self._mention_history.append((
                    entity_id,
                    meeting_id,
                    datetime.now(timezone.utc),
                    count
                ))

                # Ограничиваем размер истории
                if len(self._mention_history) > 10000:
                    self._mention_history = self._mention_history[-5000:]

                logger.debug(f"Tracked mention: {entity_name or entity_id} x{count}")

            return success

        except Exception as e:
            logger.error(f"❌ Failed to track mention: {e}")
            return False

    async def track_meeting_entities(
        self,
        meeting_id: str,
        entities: List[Dict[str, Any]],
        participants: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Отследить все сущности из встречи.

        Args:
            meeting_id: ID встречи
            entities: Список сущностей
            participants: Список участников (опционально)

        Returns:
            Статистика отслеживания
        """
        stats = {
            "meeting_id": meeting_id,
            "entities_tracked": 0,
            "participants_tracked": 0,
            "total_mentions": 0,
            "errors": 0,
        }

        try:
            from backend.core.config import flags
            if not flags.enable_frequency_tracking:
                return stats

            # Отслеживаем сущности
            for entity in entities:
                entity_id = entity.get("entity_id") or entity.get("id")
                entity_name = entity.get("name", "")

                if not entity_id:
                    continue

                # Считаем упоминания (можно использовать importance как множитель)
                importance = entity.get("importance", "medium")
                count = {"high": 3, "medium": 2, "low": 1}.get(importance, 1)

                success = await self.track_mention(
                    entity_id=entity_id,
                    meeting_id=meeting_id,
                    count=count,
                    entity_name=entity_name
                )

                if success:
                    stats["entities_tracked"] += 1
                    stats["total_mentions"] += count
                else:
                    stats["errors"] += 1

            # Отслеживаем участников
            if participants:
                for participant in participants:
                    person_id = participant.get("person_id") or participant.get("id")
                    person_name = participant.get("name", "")

                    if not person_id:
                        continue

                    success = await self.track_mention(
                        entity_id=person_id,
                        meeting_id=meeting_id,
                        count=1,
                        entity_name=person_name
                    )

                    if success:
                        stats["participants_tracked"] += 1
                        stats["total_mentions"] += 1
                    else:
                        stats["errors"] += 1

            logger.info(
                f"✅ Frequency tracked for meeting {meeting_id}: "
                f"{stats['entities_tracked']} entities, "
                f"{stats['participants_tracked']} participants"
            )

            return stats

        except Exception as e:
            logger.error(f"❌ Failed to track meeting entities: {e}")
            stats["errors"] += 1
            return stats

    def get_top_mentioned(
        self,
        label: Optional[str] = None,
        limit: int = 20,
        use_cache: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Получить топ упоминаемых сущностей.

        Args:
            label: Фильтр по типу узла (опционально)
            limit: Максимальное количество
            use_cache: Использовать кэш (по умолчанию True)

        Returns:
            Список сущностей отсортированных по frequency_score
        """
        cache_key = f"{label or 'all'}_{limit}"
        now = datetime.now(timezone.utc)

        # Проверяем кэш
        if use_cache and cache_key in self._top_mentioned_cache:
            cache_time = self._top_mentioned_cache_time.get(cache_key)
            if cache_time and (now - cache_time).total_seconds() < self._cache_ttl_seconds:
                logger.debug(f"📦 Using cached top_mentioned for {cache_key}")
                return self._top_mentioned_cache[cache_key]

        # Получаем свежие данные
        result = self.graph.get_top_weighted_nodes(label=label, limit=limit)

        # Обновляем кэш
        self._top_mentioned_cache[cache_key] = result
        self._top_mentioned_cache_time[cache_key] = now

        logger.debug(f"🔄 Updated top_mentioned cache for {cache_key} ({len(result)} items)")
        return result

    def invalidate_cache(self):
        """Инвалидировать кэш top_mentioned."""
        self._top_mentioned_cache.clear()
        self._top_mentioned_cache_time.clear()
        logger.debug("🗑️ Top mentioned cache invalidated")

    def get_trending(
        self,
        days: int = 7,
        limit: int = 10,
        min_mentions: int = 2
    ) -> List[Dict[str, Any]]:
        """
        Получить trending сущности за период.

        Trending определяется как рост упоминаний по сравнению с предыдущим периодом.

        Args:
            days: Количество дней для анализа
            limit: Максимальное количество
            min_mentions: Минимум упоминаний для включения

        Returns:
            Список trending сущностей с метриками роста
        """
        try:
            now = datetime.now(timezone.utc)
            current_period_start = now - timedelta(days=days)
            previous_period_start = current_period_start - timedelta(days=days)

            # Подсчитываем упоминания за текущий и предыдущий периоды
            current_counts: Dict[str, int] = defaultdict(int)
            previous_counts: Dict[str, int] = defaultdict(int)
            entity_names: Dict[str, str] = {}

            for entity_id, _meeting_id, timestamp, count in self._mention_history:
                if timestamp >= current_period_start:
                    current_counts[entity_id] += count
                elif timestamp >= previous_period_start:
                    previous_counts[entity_id] += count

                # Сохраняем имя из кэша
                if entity_id in self._frequency_cache:
                    entity_names[entity_id] = self._frequency_cache[entity_id].get("name", "")

            # Вычисляем trending score
            trending = []
            for entity_id, current_count in current_counts.items():
                if current_count < min_mentions:
                    continue

                previous_count = previous_counts.get(entity_id, 0)

                # Формула trending: (current - previous) / (previous + 1)
                if previous_count > 0:
                    growth_rate = (current_count - previous_count) / previous_count
                else:
                    growth_rate = current_count  # Новая сущность

                trending.append({
                    "id": entity_id,
                    "name": entity_names.get(entity_id, ""),
                    "current_mentions": current_count,
                    "previous_mentions": previous_count,
                    "growth_rate": growth_rate,
                    "is_new": previous_count == 0,
                })

            # Сортируем по growth_rate
            trending.sort(key=lambda x: x["growth_rate"], reverse=True)

            return trending[:limit]

        except Exception as e:
            logger.error(f"❌ Failed to get trending: {e}")
            return []

    def get_entity_frequency(self, entity_id: str) -> Dict[str, Any]:
        """
        Получить детальную информацию о частоте упоминаний сущности.

        Args:
            entity_id: ID сущности

        Returns:
            Информация о частоте упоминаний
        """
        # Сначала проверяем кэш
        if entity_id in self._frequency_cache:
            cache = self._frequency_cache[entity_id]
            return {
                "id": entity_id,
                "name": cache.get("name", ""),
                "total_mentions": cache.get("total", 0),
                "meetings_count": len(cache.get("by_meeting", {})),
                "last_updated": cache.get("last_updated"),
                "source": "cache",
            }

        # Иначе получаем из графа
        node = self.graph.get_node_with_weights(entity_id)
        if node:
            return {
                "id": entity_id,
                "name": node.get("name", ""),
                "total_mentions": node.get("total_mentions", 0),
                "frequency_score": node.get("frequency_score", 0.0),
                "last_mentioned_in": node.get("last_mentioned_in"),
                "last_mentioned_at": node.get("last_mentioned_at"),
                "source": "graph",
            }

        return {
            "id": entity_id,
            "name": "",
            "total_mentions": 0,
            "frequency_score": 0.0,
            "source": "not_found",
        }

    def calculate_frequency_score(self, total_mentions: int) -> float:
        """
        Рассчитать нормализованный frequency_score.

        Используется логарифмическая шкала для сглаживания.

        Args:
            total_mentions: Общее количество упоминаний

        Returns:
            Нормализованный score (0.0 - 1.0+)
        """
        if total_mentions <= 0:
            return 0.0

        # log1p(x) = log(1 + x) для избежания log(0)
        # Делим на 10 для нормализации (100 упоминаний ≈ 0.46)
        return math.log1p(total_mentions) / 10.0

    def get_cooccurrence_matrix(
        self,
        entity_ids: List[str]
    ) -> Dict[str, Dict[str, int]]:
        """
        Построить матрицу совместных упоминаний.

        Args:
            entity_ids: Список ID сущностей

        Returns:
            Матрица {entity1: {entity2: count}}
        """
        # Группируем упоминания по встречам
        meetings: Dict[str, set] = defaultdict(set)

        for entity_id, meeting_id, _, _ in self._mention_history:
            if entity_id in entity_ids:
                meetings[meeting_id].add(entity_id)

        # Считаем совместные появления
        matrix: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for meeting_id, entities in meetings.items():
            entities_list = list(entities)
            for i, e1 in enumerate(entities_list):
                for e2 in entities_list[i+1:]:
                    matrix[e1][e2] += 1
                    matrix[e2][e1] += 1

        return dict(matrix)

    def clear_cache(self):
        """Очистить кэш частот."""
        self._frequency_cache.clear()
        self._mention_history.clear()
        logger.info("Frequency cache cleared")

    async def sync_from_graph(self):
        """
        Синхронизировать кэш с данными из графа.

        Полезно после перезапуска системы.
        """
        try:
            if not self.graph.use_networkx or not self.graph.nx_graph:
                return

            self._frequency_cache.clear()

            for node_id, attrs in self.graph.nx_graph.nodes(data=True):
                total_mentions = attrs.get("total_mentions", 0)
                if total_mentions > 0:
                    self._frequency_cache[node_id] = {
                        "total": total_mentions,
                        "by_meeting": {},  # Не восстанавливаем детали
                        "last_updated": attrs.get("last_mentioned_at"),
                        "name": attrs.get("name", ""),
                    }

            logger.info(f"Synced frequency cache: {len(self._frequency_cache)} entities")

        except Exception as e:
            logger.error(f"❌ Failed to sync frequency cache: {e}")


# Singleton
_tracker_instance: Optional[FrequencyTracker] = None


def get_frequency_tracker(graph_builder) -> FrequencyTracker:
    """Получить экземпляр FrequencyTracker."""
    global _tracker_instance
    if _tracker_instance is None or _tracker_instance.graph != graph_builder:
        _tracker_instance = FrequencyTracker(graph_builder)
    return _tracker_instance

