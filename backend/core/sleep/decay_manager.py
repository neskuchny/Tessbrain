# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Decay Manager
=============================

Управление устареванием данных в графе знаний.

Концепция "decay" (затухания) основана на том, что информация
со временем теряет актуальность:
- Решения принятые год назад менее релевантны
- Старые задачи могут быть неактуальны
- Контакты без взаимодействия "остывают"

Decay Manager:
1. Отслеживает "возраст" данных
2. Понижает приоритет устаревшей информации
3. Архивирует неактуальные данные
4. Очищает мусор (orphan nodes, дубликаты)
5. Оптимизирует граф для быстрого поиска

Это аналог "забывания" у человека — мозг освобождает место
для новой информации, забывая неиспользуемое.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DecayPolicy(str, Enum):
    """Политики устаревания"""
    SOFT = "soft"       # Только понижение приоритета
    ARCHIVE = "archive" # Перемещение в архив
    DELETE = "delete"   # Удаление


class EntityFreshness(str, Enum):
    """Свежесть сущности"""
    FRESH = "fresh"         # Актуальная (< 7 дней)
    RECENT = "recent"       # Недавняя (7-30 дней)
    AGING = "aging"         # Устаревающая (30-90 дней)
    STALE = "stale"         # Устаревшая (90-365 дней)
    ANCIENT = "ancient"     # Очень старая (> 365 дней)


@dataclass
class DecayConfig:
    """Конфигурация устаревания для типа сущности"""
    entity_type: str

    # Пороги в днях
    fresh_days: int = 7
    recent_days: int = 30
    aging_days: int = 90
    stale_days: int = 365

    # Политика для каждого уровня
    aging_policy: DecayPolicy = DecayPolicy.SOFT
    stale_policy: DecayPolicy = DecayPolicy.ARCHIVE
    ancient_policy: DecayPolicy = DecayPolicy.DELETE

    # Исключения (не применять decay)
    exclude_if_has_edges: int = 5  # Не трогать если много связей
    exclude_statuses: List[str] = field(default_factory=lambda: ["active", "important"])


@dataclass
class DecayResult:
    """Результат операции decay"""
    entity_id: str
    entity_type: str
    entity_name: str

    old_freshness: EntityFreshness
    new_freshness: EntityFreshness

    action_taken: str  # "none", "priority_lowered", "archived", "deleted"

    age_days: int
    last_updated: Optional[datetime] = None


class DecayManager:
    """
    Менеджер устаревания данных.

    Управляет жизненным циклом данных в графе:
    - Отслеживает возраст сущностей
    - Применяет политики устаревания
    - Архивирует и удаляет неактуальное

    Пример использования:
    ```python
    manager = DecayManager(graph_builder=graph)

    # Проверить свежесть сущности
    freshness = manager.get_freshness(entity_id)

    # Запустить процесс decay
    results = await manager.run_decay_cycle()

    # Получить статистику
    stats = manager.get_decay_stats()
    ```
    """

    # Конфигурации по умолчанию для разных типов
    DEFAULT_CONFIGS = {
        "Task": DecayConfig(
            entity_type="Task",
            fresh_days=3,
            recent_days=14,
            aging_days=30,
            stale_days=90,
            aging_policy=DecayPolicy.SOFT,
            stale_policy=DecayPolicy.ARCHIVE,
            ancient_policy=DecayPolicy.ARCHIVE,
            exclude_statuses=["in_progress", "active"]
        ),
        "Decision": DecayConfig(
            entity_type="Decision",
            fresh_days=7,
            recent_days=30,
            aging_days=90,
            stale_days=365,
            aging_policy=DecayPolicy.SOFT,
            stale_policy=DecayPolicy.SOFT,
            ancient_policy=DecayPolicy.ARCHIVE,
            exclude_statuses=["strategic", "important"]
        ),
        "Meeting": DecayConfig(
            entity_type="Meeting",
            fresh_days=7,
            recent_days=30,
            aging_days=180,
            stale_days=365,
            aging_policy=DecayPolicy.SOFT,
            stale_policy=DecayPolicy.ARCHIVE,
            ancient_policy=DecayPolicy.ARCHIVE
        ),
        "Person": DecayConfig(
            entity_type="Person",
            fresh_days=30,
            recent_days=90,
            aging_days=180,
            stale_days=365,
            aging_policy=DecayPolicy.SOFT,
            stale_policy=DecayPolicy.SOFT,
            ancient_policy=DecayPolicy.SOFT,  # Людей не удаляем
            exclude_if_has_edges=3
        ),
        "Project": DecayConfig(
            entity_type="Project",
            fresh_days=14,
            recent_days=60,
            aging_days=180,
            stale_days=365,
            aging_policy=DecayPolicy.SOFT,
            stale_policy=DecayPolicy.ARCHIVE,
            ancient_policy=DecayPolicy.ARCHIVE,
            exclude_statuses=["active", "in_progress"]
        )
    }

    def __init__(
        self,
        graph_builder=None,
        configs: Optional[Dict[str, DecayConfig]] = None,
        archive_path: Optional[str] = None
    ):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            configs: Кастомные конфигурации decay
            archive_path: Путь для архивных данных
        """
        self.graph = graph_builder
        self.archive_path = archive_path or "data/archive"

        # Объединяем дефолтные и кастомные конфиги
        self.configs = self.DEFAULT_CONFIGS.copy()
        if configs:
            self.configs.update(configs)

        # История операций
        self._decay_history: List[DecayResult] = []

        # Архив удалённых сущностей
        self._archive: List[Dict[str, Any]] = []

    def _get_graph_data(self):
        """Получить данные графа"""
        if not self.graph:
            return None
        if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            return self.graph.nx_graph
        return None

    def _get_entity_age(self, data: Dict[str, Any]) -> int:
        """Получить возраст сущности в днях"""
        now = datetime.now(timezone.utc)

        # Пробуем разные поля даты
        date_fields = ["updated_at", "created_at", "date", "_created_at"]

        for field in date_fields:
            date_str = data.get(field)
            if date_str:
                try:
                    if isinstance(date_str, str):
                        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    else:
                        dt = date_str

                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)

                    return (now - dt).days
                except (ValueError, TypeError):
                    continue

        # Если дата не найдена, считаем очень старым
        return 9999

    def get_freshness(
        self,
        entity_id: str,
        entity_type: Optional[str] = None
    ) -> EntityFreshness:
        """
        Определить свежесть сущности.

        Args:
            entity_id: ID сущности
            entity_type: Тип сущности (если известен)

        Returns:
            Уровень свежести
        """
        graph_data = self._get_graph_data()
        if not graph_data or not graph_data.has_node(entity_id):
            return EntityFreshness.ANCIENT

        data = graph_data.nodes[entity_id]
        label = entity_type or data.get("_label", "")

        config = self.configs.get(label, DecayConfig(entity_type=label))
        age_days = self._get_entity_age(data)

        if age_days <= config.fresh_days:
            return EntityFreshness.FRESH
        elif age_days <= config.recent_days:
            return EntityFreshness.RECENT
        elif age_days <= config.aging_days:
            return EntityFreshness.AGING
        elif age_days <= config.stale_days:
            return EntityFreshness.STALE
        else:
            return EntityFreshness.ANCIENT

    def _should_exclude(
        self,
        node_id: str,
        data: Dict[str, Any],
        config: DecayConfig,
        graph_data
    ) -> bool:
        """Проверить нужно ли исключить сущность из decay"""
        # Проверяем статус
        status = data.get("status", "").lower()
        if status in [s.lower() for s in config.exclude_statuses]:
            return True

        # Проверяем количество связей
        total_edges = graph_data.in_degree(node_id) + graph_data.out_degree(node_id)
        if total_edges >= config.exclude_if_has_edges:
            return True

        return False

    async def run_decay_cycle(self, dry_run: bool = False) -> List[DecayResult]:
        """
        Запустить цикл decay.

        Проходит по всем сущностям и применяет политики устаревания.

        Args:
            dry_run: Если True, только анализ без изменений

        Returns:
            Список результатов операций
        """
        logger.info(f"🕐 Starting decay cycle (dry_run={dry_run})...")

        results: List[DecayResult] = []
        graph_data = self._get_graph_data()

        if not graph_data:
            logger.warning("No graph available for decay")
            return results

        nodes_to_process = list(graph_data.nodes(data=True))

        for node_id, data in nodes_to_process:
            label = data.get("_label", "")

            # Пропускаем если нет конфига
            if label not in self.configs:
                continue

            config = self.configs[label]

            # Проверяем исключения
            if self._should_exclude(node_id, data, config, graph_data):
                continue

            # Определяем текущую свежесть
            age_days = self._get_entity_age(data)
            old_freshness = self.get_freshness(node_id, label)

            # Определяем действие
            action = "none"
            policy = DecayPolicy.SOFT

            if old_freshness == EntityFreshness.AGING:
                policy = config.aging_policy
            elif old_freshness == EntityFreshness.STALE:
                policy = config.stale_policy
            elif old_freshness == EntityFreshness.ANCIENT:
                policy = config.ancient_policy

            # Применяем политику
            if policy == DecayPolicy.SOFT and old_freshness in (EntityFreshness.AGING, EntityFreshness.STALE, EntityFreshness.ANCIENT):
                action = "priority_lowered"
                if not dry_run:
                    # Понижаем приоритет (добавляем метку)
                    data["_decay_level"] = old_freshness.value
                    data["_decay_date"] = datetime.now(timezone.utc).isoformat()

            elif policy == DecayPolicy.ARCHIVE:
                action = "archived"
                if not dry_run:
                    # Архивируем
                    self._archive_entity(node_id, data, graph_data)

            elif policy == DecayPolicy.DELETE:
                action = "deleted"
                if not dry_run:
                    # Удаляем
                    self._delete_entity(node_id, data, graph_data)

            if action != "none":
                result = DecayResult(
                    entity_id=node_id,
                    entity_type=label,
                    entity_name=data.get("name", data.get("title", node_id)),
                    old_freshness=old_freshness,
                    new_freshness=old_freshness,  # Свежесть не меняется, меняется статус
                    action_taken=action,
                    age_days=age_days
                )
                results.append(result)
                self._decay_history.append(result)

        logger.info(f"🕐 Decay cycle complete. Processed {len(results)} entities.")

        return results

    def _archive_entity(
        self,
        node_id: str,
        data: Dict[str, Any],
        graph_data
    ):
        """Архивировать сущность"""
        # Сохраняем в архив
        archive_entry = {
            "node_id": node_id,
            "data": dict(data),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "edges": []
        }

        # Сохраняем связи
        for source, target, edge_data in graph_data.edges(node_id, data=True):
            archive_entry["edges"].append({
                "source": source,
                "target": target,
                "data": dict(edge_data)
            })

        self._archive.append(archive_entry)

        # Помечаем как архивную (не удаляем физически)
        data["_archived"] = True
        data["_archived_at"] = datetime.now(timezone.utc).isoformat()

        logger.debug(f"Archived entity: {node_id}")

    def _delete_entity(
        self,
        node_id: str,
        data: Dict[str, Any],
        graph_data
    ):
        """Удалить сущность"""
        # Сначала архивируем
        self._archive_entity(node_id, data, graph_data)

        # Затем удаляем из графа
        try:
            graph_data.remove_node(node_id)
            logger.debug(f"Deleted entity: {node_id}")
        except Exception as e:
            logger.error(f"Failed to delete entity {node_id}: {e}")

    async def cleanup_orphans(self, dry_run: bool = False) -> List[str]:
        """
        Очистить orphan nodes (узлы без связей).

        Returns:
            Список ID удалённых узлов
        """
        deleted = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return deleted

        # Типы которые можно удалять если orphan

        for node_id, data in list(graph_data.nodes(data=True)):
            label = data.get("_label", "")

            # Пропускаем важные типы
            if label in ("Person", "Project", "Meeting"):
                continue

            # Проверяем связи
            total_edges = graph_data.in_degree(node_id) + graph_data.out_degree(node_id)

            if total_edges == 0:
                if not dry_run:
                    self._archive_entity(node_id, data, graph_data)
                    graph_data.remove_node(node_id)
                deleted.append(node_id)

        logger.info(f"🧹 Cleanup: {len(deleted)} orphan nodes {'would be' if dry_run else ''} removed")

        return deleted

    def get_decay_stats(self) -> Dict[str, Any]:
        """Получить статистику decay"""
        graph_data = self._get_graph_data()

        freshness_counts = {f.value: 0 for f in EntityFreshness}
        type_stats = {}

        if graph_data:
            for node_id, data in graph_data.nodes(data=True):
                label = data.get("_label", "")
                freshness = self.get_freshness(node_id, label)

                freshness_counts[freshness.value] += 1

                if label not in type_stats:
                    type_stats[label] = {f.value: 0 for f in EntityFreshness}
                type_stats[label][freshness.value] += 1

        return {
            "total_by_freshness": freshness_counts,
            "by_type": type_stats,
            "archived_count": len(self._archive),
            "decay_operations": len(self._decay_history),
            "configs": {k: v.entity_type for k, v in self.configs.items()}
        }

    def get_stale_entities(self, entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """Получить список устаревших сущностей"""
        stale = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return stale

        for node_id, data in graph_data.nodes(data=True):
            label = data.get("_label", "")

            if entity_type and label != entity_type:
                continue

            freshness = self.get_freshness(node_id, label)

            if freshness in (EntityFreshness.STALE, EntityFreshness.ANCIENT):
                stale.append({
                    "id": node_id,
                    "type": label,
                    "name": data.get("name", data.get("title", node_id)),
                    "freshness": freshness.value,
                    "age_days": self._get_entity_age(data)
                })

        return sorted(stale, key=lambda x: -x["age_days"])

    def save_archive(self) -> str:
        """Сохранить архив в файл"""
        import json
        import os

        os.makedirs(self.archive_path, exist_ok=True)

        filename = f"archive_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        filepath = os.path.join(self.archive_path, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self._archive, f, ensure_ascii=False, indent=2)

        logger.info(f"Archive saved: {filepath}")
        return filepath

