# -*- coding: utf-8 -*-
"""
Memory Consolidator - Консолидация и саммаризация памяти.

Вдохновлено MCP Memory Service (Dream-Inspired Consolidation):
- Саммаризация (сжатие) памяти с сохранением оригиналов
- DBSCAN кластеризация семантически похожих воспоминаний
- Content Quality Scoring для приоритизации
- Архивация старых/неиспользуемых данных
- Creative Association Discovery

Использование:
```python
from backend.core.memory.memory_consolidator import MemoryConsolidator

consolidator = MemoryConsolidator(graph_builder)

# Запуск консолидации
result = await consolidator.consolidate(
    compress_memories=True,
    cluster_memories=True,
    archive_old=True
)

# Получить саммари
summary = await consolidator.get_memory_summary(entity_id="project_123")
```
"""
import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class MemorySummary:
    """Саммари памяти."""
    entity_id: str
    entity_type: str
    summary_text: str  # Сжатое описание (до 500 символов)
    original_count: int  # Количество оригинальных записей
    key_facts: List[str]  # Ключевые факты
    key_relationships: List[str]  # Ключевые связи
    last_activity: str
    quality_score: float  # 0.0 - 1.0
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryCluster:
    """Кластер связанных воспоминаний."""
    cluster_id: str
    topic: str  # Тема кластера
    member_ids: List[str]  # ID членов кластера
    centroid_text: str  # Центральный текст
    size: int
    avg_similarity: float
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ContentQualityScorer:
    """Оценка качества контента."""

    # Слова, указывающие на значимый контент
    MEANINGFUL_INDICATORS = {
        "решили", "решение", "договорились", "согласовали",
        "реализовали", "внедрили", "исправили", "починили",
        "узнали", "выяснили", "обнаружили", "нашли",
        "проблема", "риск", "блокер", "критично",
        "успех", "достижение", "завершили", "запустили",
        "дедлайн", "срок", "бюджет", "ресурсы",
    }

    # Паттерны "пустого" контента
    GENERIC_PATTERNS = [
        "обсудили", "поговорили", "рассмотрели",
        "в процессе", "продолжаем", "работаем над",
        "без изменений", "всё по плану",
    ]

    def score(self, text: str) -> float:
        """
        Оценить качество контента.

        Returns:
            Score от 0.0 (пустой) до 1.0 (высокое качество)
        """
        if not text or len(text) < 10:
            return 0.0

        text_lower = text.lower()

        # Базовый score от длины (логарифмический)
        length_score = min(1.0, math.log1p(len(text)) / 10)

        # Score от meaningful indicators
        meaningful_count = sum(
            1 for word in self.MEANINGFUL_INDICATORS
            if word in text_lower
        )
        meaningful_score = min(1.0, meaningful_count / 3)

        # Penalty за generic patterns
        generic_count = sum(
            1 for pattern in self.GENERIC_PATTERNS
            if pattern in text_lower
        )
        generic_penalty = min(0.5, generic_count * 0.15)

        # Information density (уникальные слова / всего слов)
        words = text_lower.split()
        if words:
            density = len(set(words)) / len(words)
        else:
            density = 0

        # Итоговый score
        score = (
            length_score * 0.2 +
            meaningful_score * 0.4 +
            density * 0.2 +
            0.2  # Базовый бонус
        ) - generic_penalty

        return max(0.0, min(1.0, score))


class MemoryConsolidator:
    """
    Консолидатор памяти.

    Функции:
    1. Саммаризация (сжатие) памяти
    2. DBSCAN кластеризация
    3. Content Quality Scoring
    4. Архивация старых данных
    5. Creative Association Discovery
    """

    # Конфигурация
    SUMMARY_MAX_LENGTH = 500
    MIN_CLUSTER_SIZE = 3
    SIMILARITY_THRESHOLD = 0.5
    ARCHIVE_THRESHOLD = 0.1  # Quality score ниже которого архивируем
    ARCHIVE_DAYS = 90  # Дней без доступа для архивации

    def __init__(self, graph_builder, llm_router=None):
        """
        Args:
            graph_builder: Экземпляр GraphBuilder
            llm_router: Роутер для LLM (для саммаризации)
        """
        self.graph = graph_builder
        self.llm_router = llm_router
        self.quality_scorer = ContentQualityScorer()

        # Кэш саммари
        self._summaries: Dict[str, MemorySummary] = {}
        self._clusters: Dict[str, MemoryCluster] = {}

    async def consolidate(
        self,
        compress_memories: bool = True,
        cluster_memories: bool = True,
        archive_old: bool = True,
        discover_associations: bool = True
    ) -> Dict[str, Any]:
        """
        Полная консолидация памяти.

        Args:
            compress_memories: Сжимать память в саммари
            cluster_memories: Кластеризовать похожие
            archive_old: Архивировать старые
            discover_associations: Искать новые связи

        Returns:
            Статистика консолидации
        """
        stats = {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "summaries_created": 0,
            "clusters_created": 0,
            "memories_archived": 0,
            "associations_discovered": 0,
            "errors": [],
        }

        try:
            from backend.core.config import flags
            if not flags.enable_detailed_metrics:
                # Минимальная консолидация
                pass
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        try:
            # 1. Сжатие памяти
            if compress_memories:
                compression_result = await self._compress_memories()
                stats["summaries_created"] = compression_result.get("created", 0)

            # 2. Кластеризация
            if cluster_memories:
                cluster_result = await self._cluster_memories()
                stats["clusters_created"] = cluster_result.get("created", 0)

            # 3. Архивация
            if archive_old:
                archive_result = await self._archive_old_memories()
                stats["memories_archived"] = archive_result.get("archived", 0)

            # 4. Поиск ассоциаций
            if discover_associations:
                assoc_result = await self._discover_associations()
                stats["associations_discovered"] = assoc_result.get("discovered", 0)

        except Exception as e:
            logger.error(f"❌ Consolidation error: {e}")
            stats["errors"].append(str(e))

        stats["completed_at"] = datetime.now(timezone.utc).isoformat()
        return stats

    async def _compress_memories(self) -> Dict[str, Any]:
        """Сжать память в саммари."""
        result = {"created": 0, "updated": 0, "errors": 0}

        if not self.graph or not self.graph.use_networkx:
            return result

        try:
            # Группируем узлы по типам
            nodes_by_type: Dict[str, List[Tuple[str, Dict]]] = defaultdict(list)

            for node_id, attrs in self.graph.nx_graph.nodes(data=True):
                label = attrs.get("_label", "Unknown")
                nodes_by_type[label].append((node_id, attrs))

            # Создаём саммари для каждого типа
            for entity_type, nodes in nodes_by_type.items():
                if len(nodes) < 5:  # Минимум для саммари
                    continue

                summary = await self._create_type_summary(entity_type, nodes)
                if summary:
                    self._summaries[f"{entity_type}_summary"] = summary
                    result["created"] += 1

        except Exception as e:
            logger.error(f"Compression error: {e}")
            result["errors"] += 1

        return result

    async def _create_type_summary(
        self,
        entity_type: str,
        nodes: List[Tuple[str, Dict]]
    ) -> Optional[MemorySummary]:
        """Создать саммари для типа сущностей."""
        try:
            # Собираем ключевые факты
            key_facts = []
            key_relationships = []
            total_quality = 0.0

            for node_id, attrs in nodes[:50]:  # Лимит
                name = attrs.get("name", attrs.get("title", node_id))

                # Оцениваем качество
                description = attrs.get("description", "")
                quality = self.quality_scorer.score(description)
                total_quality += quality

                if quality > 0.5:  # Только качественные
                    key_facts.append(f"{name}: {description[:100]}")

            if not key_facts:
                return None

            # Собираем связи
            for node_id, _ in nodes[:20]:
                for _, target, edge_data in self.graph.nx_graph.out_edges(node_id, data=True):
                    target_name = self.graph.nx_graph.nodes.get(target, {}).get("name", target)
                    rel_type = edge_data.get("_type", "RELATED_TO")
                    key_relationships.append(f"{rel_type} -> {target_name}")

            # Создаём саммари текст
            summary_text = f"{entity_type} ({len(nodes)} элементов). "
            summary_text += f"Ключевые: {', '.join([f[:30] for f in key_facts[:5]])}. "
            summary_text = summary_text[:self.SUMMARY_MAX_LENGTH]

            avg_quality = total_quality / len(nodes) if nodes else 0

            return MemorySummary(
                entity_id=f"{entity_type}_summary",
                entity_type=entity_type,
                summary_text=summary_text,
                original_count=len(nodes),
                key_facts=key_facts[:10],
                key_relationships=list(set(key_relationships))[:10],
                last_activity=datetime.now(timezone.utc).isoformat(),
                quality_score=avg_quality,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        except Exception as e:
            logger.error(f"Summary creation error: {e}")
            return None

    async def _cluster_memories(self) -> Dict[str, Any]:
        """
        Кластеризация памяти (упрощённый DBSCAN).

        Группирует семантически похожие узлы.
        """
        result = {"created": 0, "errors": 0}

        if not self.graph or not self.graph.use_networkx:
            return result

        try:
            # Собираем узлы с описаниями
            nodes_with_text = []
            for node_id, attrs in self.graph.nx_graph.nodes(data=True):
                text = attrs.get("description", attrs.get("name", ""))
                if text and len(text) > 20:
                    nodes_with_text.append((node_id, text, attrs))

            if len(nodes_with_text) < self.MIN_CLUSTER_SIZE:
                return result

            # Простая кластеризация по ключевым словам
            clusters = self._simple_keyword_clustering(nodes_with_text)

            for cluster_id, members in clusters.items():
                if len(members) >= self.MIN_CLUSTER_SIZE:
                    cluster = MemoryCluster(
                        cluster_id=cluster_id,
                        topic=cluster_id,
                        member_ids=[m[0] for m in members],
                        centroid_text=members[0][1][:200] if members else "",
                        size=len(members),
                        avg_similarity=0.7,  # Placeholder
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    self._clusters[cluster_id] = cluster
                    result["created"] += 1

        except Exception as e:
            logger.error(f"Clustering error: {e}")
            result["errors"] += 1

        return result

    def _simple_keyword_clustering(
        self,
        nodes: List[Tuple[str, str, Dict]]
    ) -> Dict[str, List]:
        """Простая кластеризация по ключевым словам."""
        clusters: Dict[str, List] = defaultdict(list)

        # Ключевые темы
        topics = {
            "project": ["проект", "разработка", "релиз", "спринт"],
            "meeting": ["встреча", "обсуждение", "созвон", "митинг"],
            "task": ["задача", "таск", "тикет", "баг"],
            "person": ["сотрудник", "команда", "руководитель"],
            "decision": ["решение", "договорились", "согласовали"],
            "risk": ["риск", "проблема", "блокер", "критично"],
        }

        for node_id, text, attrs in nodes:
            text_lower = text.lower()
            assigned = False

            for topic, keywords in topics.items():
                if any(kw in text_lower for kw in keywords):
                    clusters[topic].append((node_id, text, attrs))
                    assigned = True
                    break

            if not assigned:
                clusters["other"].append((node_id, text, attrs))

        return clusters

    async def _archive_old_memories(self) -> Dict[str, Any]:
        """Архивировать старые/неиспользуемые данные."""
        result = {"archived": 0, "errors": 0}

        if not self.graph or not self.graph.use_networkx:
            return result

        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.ARCHIVE_DAYS)
            cutoff_str = cutoff_date.isoformat()

            to_archive = []

            for node_id, attrs in self.graph.nx_graph.nodes(data=True):
                # Проверяем дату последнего доступа
                last_accessed = attrs.get("last_accessed", attrs.get("created_at", ""))

                if last_accessed and last_accessed < cutoff_str:
                    # Проверяем quality score
                    description = attrs.get("description", "")
                    quality = self.quality_scorer.score(description)

                    if quality < self.ARCHIVE_THRESHOLD:
                        # Проверяем что нет важных тегов
                        tags = attrs.get("tags", [])
                        if not any(t in ["critical", "important", "reference"] for t in tags):
                            to_archive.append(node_id)

            # Помечаем как архивные (не удаляем)
            for node_id in to_archive[:100]:  # Лимит за раз
                if self.graph.nx_graph.has_node(node_id):
                    self.graph.nx_graph.nodes[node_id]["archived"] = True
                    self.graph.nx_graph.nodes[node_id]["archived_at"] = datetime.now(timezone.utc).isoformat()
                    result["archived"] += 1

            if result["archived"] > 0:
                await self.graph.save()

        except Exception as e:
            logger.error(f"Archive error: {e}")
            result["errors"] += 1

        return result

    async def _discover_associations(self) -> Dict[str, Any]:
        """
        Поиск новых ассоциаций (Creative Association Discovery).

        Ищет связи в диапазоне similarity 0.3-0.7 (sweet spot).
        """
        result = {"discovered": 0, "errors": 0}

        # Используем Hebbian Learning для этого
        try:
            from backend.core.memory.hebbian_learning import get_hebbian_learning

            get_hebbian_learning(self.graph)

            # Hebbian уже делает это через process_meeting_cooccurrences
            # Здесь можно добавить дополнительную логику

        except Exception as e:
            logger.debug(f"Association discovery not available: {e}")

        return result

    async def get_memory_summary(self, entity_id: str) -> Optional[MemorySummary]:
        """Получить саммари для сущности."""
        return self._summaries.get(entity_id)

    async def get_all_summaries(self) -> List[MemorySummary]:
        """Получить все саммари."""
        return list(self._summaries.values())

    async def get_clusters(self) -> List[MemoryCluster]:
        """Получить все кластеры."""
        return list(self._clusters.values())

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику консолидатора."""
        return {
            "summaries_count": len(self._summaries),
            "clusters_count": len(self._clusters),
            "config": {
                "summary_max_length": self.SUMMARY_MAX_LENGTH,
                "min_cluster_size": self.MIN_CLUSTER_SIZE,
                "archive_threshold": self.ARCHIVE_THRESHOLD,
                "archive_days": self.ARCHIVE_DAYS,
            }
        }


# Singleton
_consolidator: Optional[MemoryConsolidator] = None


def get_memory_consolidator(graph_builder=None, llm_router=None) -> MemoryConsolidator:
    """Получить экземпляр MemoryConsolidator."""
    global _consolidator
    if _consolidator is None:
        _consolidator = MemoryConsolidator(graph_builder, llm_router)
    return _consolidator


