# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Snapshot Generator
==================================

Создаёт "снимки" состояния сущностей в определённый момент времени.

Снимок (Snapshot) — это срез данных, который показывает:
- Текущее состояние проекта/человека/задачи
- Агрегированные метрики за период
- Изменения с предыдущего снимка
- Тренды и динамику

Зачем нужны снимки:
1. Отслеживание прогресса во времени
2. Быстрый ответ на вопросы "как дела у проекта X"
3. Обнаружение изменений и аномалий
4. Построение временных рядов для аналитики

Типы снимков:
- ProjectSnapshot — состояние проекта
- PersonSnapshot — активность человека
- TeamSnapshot — состояние команды
- OrganizationSnapshot — обзор организации
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SnapshotType(str, Enum):
    """Типы снимков"""
    PROJECT = "project"
    PERSON = "person"
    TEAM = "team"
    ORGANIZATION = "organization"
    MEETING = "meeting"
    TASK = "task"


class SnapshotPeriod(str, Enum):
    """Периоды для снимков"""
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


@dataclass
class EntitySnapshot:
    """Снимок отдельной сущности"""
    entity_id: str
    entity_type: str
    entity_name: str
    timestamp: datetime

    # Метрики
    metrics: Dict[str, Any] = field(default_factory=dict)

    # Связи
    relationships: Dict[str, List[str]] = field(default_factory=dict)

    # Изменения с предыдущего снимка
    changes: Dict[str, Any] = field(default_factory=dict)

    # Тренды (up/down/stable)
    trends: Dict[str, str] = field(default_factory=dict)

    # Дополнительные данные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "timestamp": self.timestamp.isoformat(),
            "metrics": self.metrics,
            "relationships": self.relationships,
            "changes": self.changes,
            "trends": self.trends,
            "metadata": self.metadata
        }


@dataclass
class Snapshot:
    """Полный снимок состояния"""
    snapshot_id: str
    snapshot_type: SnapshotType
    period: SnapshotPeriod
    timestamp: datetime

    # Период снимка
    period_start: datetime
    period_end: datetime

    # Сущности в снимке
    entities: List[EntitySnapshot] = field(default_factory=list)

    # Агрегированные метрики
    summary: Dict[str, Any] = field(default_factory=dict)

    # Ключевые события периода
    highlights: List[Dict[str, Any]] = field(default_factory=list)

    # Предупреждения и риски
    warnings: List[Dict[str, Any]] = field(default_factory=list)

    # Метаданные
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_type": self.snapshot_type.value,
            "period": self.period.value,
            "timestamp": self.timestamp.isoformat(),
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "entities": [e.to_dict() for e in self.entities],
            "summary": self.summary,
            "highlights": self.highlights,
            "warnings": self.warnings,
            "metadata": self.metadata
        }


class SnapshotGenerator:
    """
    Генератор снимков состояния.

    Создаёт периодические снимки сущностей из графа знаний,
    агрегирует метрики и отслеживает изменения.

    Пример использования:
    ```python
    generator = SnapshotGenerator(graph_builder=graph)

    # Создать снимок проекта
    snapshot = await generator.create_project_snapshot(
        project_id="proj_123",
        period=SnapshotPeriod.WEEKLY
    )

    # Создать снимок всех проектов
    snapshots = await generator.create_organization_snapshot(
        period=SnapshotPeriod.DAILY
    )
    ```
    """

    def __init__(
        self,
        graph_builder=None,
        vector_indexer=None,
        storage_path: Optional[str] = None
    ):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            vector_indexer: VectorIndexer для семантического поиска
            storage_path: Путь для хранения снимков
        """
        self.graph = graph_builder
        self.vectors = vector_indexer
        self.storage_path = storage_path or "data/snapshots"

        # Кеш предыдущих снимков для сравнения
        self._previous_snapshots: Dict[str, Snapshot] = {}

        # История снимков
        self._snapshot_history: List[str] = []

    def _generate_snapshot_id(
        self,
        snapshot_type: SnapshotType,
        period: SnapshotPeriod,
        timestamp: datetime,
        entity_id: Optional[str] = None
    ) -> str:
        """Генерировать уникальный ID снимка"""
        parts = [
            snapshot_type.value,
            period.value,
            timestamp.strftime("%Y%m%d_%H%M"),
            entity_id or "all"
        ]
        raw = "_".join(parts)
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _get_period_bounds(
        self,
        period: SnapshotPeriod,
        reference_time: Optional[datetime] = None
    ) -> Tuple[datetime, datetime]:
        """Получить границы периода"""
        now = reference_time or datetime.now(timezone.utc)

        if period == SnapshotPeriod.DAILY:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif period == SnapshotPeriod.WEEKLY:
            # Начало недели (понедельник)
            start = now - timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(weeks=1)
        elif period == SnapshotPeriod.MONTHLY:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Следующий месяц
            if now.month == 12:
                end = start.replace(year=now.year + 1, month=1)
            else:
                end = start.replace(month=now.month + 1)
        elif period == SnapshotPeriod.QUARTERLY:
            quarter = (now.month - 1) // 3
            start_month = quarter * 3 + 1
            start = now.replace(month=start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_month = start_month + 3
            if end_month > 12:
                end = start.replace(year=now.year + 1, month=end_month - 12)
            else:
                end = start.replace(month=end_month)
        else:
            start = now
            end = now

        return start, end

    def _calculate_trend(
        self,
        current_value: float,
        previous_value: float,
        threshold: float = 0.1
    ) -> str:
        """Рассчитать тренд"""
        if previous_value == 0:
            return "new" if current_value > 0 else "stable"

        change = (current_value - previous_value) / previous_value

        if change > threshold:
            return "up"
        elif change < -threshold:
            return "down"
        else:
            return "stable"

    async def create_project_snapshot(
        self,
        project_id: str,
        period: SnapshotPeriod = SnapshotPeriod.WEEKLY
    ) -> Optional[Snapshot]:
        """
        Создать снимок проекта.

        Включает:
        - Количество встреч за период
        - Количество задач (новых, завершённых, в работе)
        - Активность участников
        - Принятые решения
        - Ключевые метрики (KPI)
        """
        if not self.graph:
            logger.warning("No graph available for snapshot")
            return None

        # Получаем граф
        graph_data = None
        if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            graph_data = self.graph.nx_graph

        if not graph_data:
            logger.warning("Graph not initialized")
            return None

        now = datetime.now(timezone.utc)
        period_start, period_end = self._get_period_bounds(period, now)

        snapshot_id = self._generate_snapshot_id(
            SnapshotType.PROJECT, period, now, project_id
        )

        # Собираем данные о проекте
        project_node = None
        for node_id, data in graph_data.nodes(data=True):
            label = data.get("_label", "")
            if label == "Project" and (
                data.get("project_id") == project_id or
                node_id == project_id
            ):
                project_node = (node_id, data)
                break

        if not project_node:
            logger.warning(f"Project {project_id} not found in graph")
            return None

        node_id, project_data = project_node

        # Собираем связанные сущности
        meetings = []
        tasks = []
        decisions = []
        participants = set()

        # Исходящие связи проекта
        for _, target, _edge_data in graph_data.out_edges(node_id, data=True):
            target_data = graph_data.nodes[target]
            target_label = target_data.get("_label", "")

            if target_label == "Meeting":
                meetings.append(target_data)
            elif target_label == "Task":
                tasks.append(target_data)
            elif target_label == "Decision":
                decisions.append(target_data)

        # Входящие связи (люди работают над проектом)
        for source, _, _edge_data in graph_data.in_edges(node_id, data=True):
            source_data = graph_data.nodes[source]
            if source_data.get("_label") == "Person":
                participants.add(source_data.get("name", source))

        # Также ищем участников через встречи
        for meeting in meetings:
            meeting.get("meeting_id", "")
            for _, target, _ in graph_data.out_edges(data=True):
                target_data = graph_data.nodes.get(target, {})
                if target_data.get("_label") == "Person":
                    participants.add(target_data.get("name", target))

        # Подсчитываем метрики
        tasks_new = len([t for t in tasks if t.get("status") in ("pending", "new")])
        tasks_done = len([t for t in tasks if t.get("status") in ("completed", "done")])
        tasks_in_progress = len([t for t in tasks if t.get("status") in ("in_progress", "active")])

        # Создаём EntitySnapshot для проекта
        entity_snapshot = EntitySnapshot(
            entity_id=project_id,
            entity_type="project",
            entity_name=project_data.get("name", project_data.get("title", "Unknown")),
            timestamp=now,
            metrics={
                "meetings_count": len(meetings),
                "tasks_total": len(tasks),
                "tasks_new": tasks_new,
                "tasks_completed": tasks_done,
                "tasks_in_progress": tasks_in_progress,
                "decisions_count": len(decisions),
                "participants_count": len(participants),
                "completion_rate": tasks_done / len(tasks) if tasks else 0
            },
            relationships={
                "participants": list(participants),
                "meetings": [m.get("meeting_id", "") for m in meetings[:10]],
            },
            metadata={
                "project_status": project_data.get("status", "active"),
                "created_at": project_data.get("created_at", "")
            }
        )

        # Сравниваем с предыдущим снимком
        prev_key = f"project_{project_id}"
        if prev_key in self._previous_snapshots:
            prev = self._previous_snapshots[prev_key]
            if prev.entities:
                prev_entity = prev.entities[0]
                prev_metrics = prev_entity.metrics

                # Вычисляем изменения
                entity_snapshot.changes = {
                    "meetings_delta": entity_snapshot.metrics["meetings_count"] - prev_metrics.get("meetings_count", 0),
                    "tasks_completed_delta": entity_snapshot.metrics["tasks_completed"] - prev_metrics.get("tasks_completed", 0),
                    "participants_delta": entity_snapshot.metrics["participants_count"] - prev_metrics.get("participants_count", 0)
                }

                # Вычисляем тренды
                entity_snapshot.trends = {
                    "activity": self._calculate_trend(
                        entity_snapshot.metrics["meetings_count"],
                        prev_metrics.get("meetings_count", 0)
                    ),
                    "completion": self._calculate_trend(
                        entity_snapshot.metrics["completion_rate"],
                        prev_metrics.get("completion_rate", 0)
                    ),
                    "team_size": self._calculate_trend(
                        entity_snapshot.metrics["participants_count"],
                        prev_metrics.get("participants_count", 0)
                    )
                }

        # Формируем highlights
        highlights = []
        if decisions:
            for d in decisions[:3]:
                highlights.append({
                    "type": "decision",
                    "text": d.get("summary", d.get("decision_summary", ""))[:100],
                    "importance": "high"
                })

        # Формируем warnings
        warnings = []
        if tasks_in_progress > tasks_done * 2:
            warnings.append({
                "type": "bottleneck",
                "message": f"Много задач в работе ({tasks_in_progress}) при малом количестве завершённых ({tasks_done})",
                "severity": "medium"
            })

        if len(participants) < 2 and len(tasks) > 5:
            warnings.append({
                "type": "resource",
                "message": f"Мало участников ({len(participants)}) для количества задач ({len(tasks)})",
                "severity": "low"
            })

        # Создаём снимок
        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            snapshot_type=SnapshotType.PROJECT,
            period=period,
            timestamp=now,
            period_start=period_start,
            period_end=period_end,
            entities=[entity_snapshot],
            summary={
                "project_name": entity_snapshot.entity_name,
                "health_score": self._calculate_health_score(entity_snapshot),
                "activity_level": "high" if len(meetings) > 3 else "medium" if len(meetings) > 0 else "low"
            },
            highlights=highlights,
            warnings=warnings,
            metadata={
                "generator_version": "1.0",
                "period_label": f"{period_start.strftime('%Y-%m-%d')} - {period_end.strftime('%Y-%m-%d')}"
            }
        )

        # Сохраняем для сравнения
        self._previous_snapshots[prev_key] = snapshot
        self._snapshot_history.append(snapshot_id)

        return snapshot

    def _calculate_health_score(self, entity: EntitySnapshot) -> float:
        """Рассчитать оценку здоровья проекта (0-100)"""
        score = 50.0  # Базовый score

        metrics = entity.metrics

        # Completion rate влияет на score
        completion = metrics.get("completion_rate", 0)
        score += completion * 20  # +0-20 баллов

        # Активность (встречи)
        meetings = metrics.get("meetings_count", 0)
        if meetings > 0:
            score += min(meetings * 5, 15)  # +0-15 баллов

        # Размер команды
        participants = metrics.get("participants_count", 0)
        if participants >= 2:
            score += min(participants * 2, 10)  # +0-10 баллов

        # Тренды
        trends = entity.trends
        if trends.get("completion") == "up":
            score += 5
        elif trends.get("completion") == "down":
            score -= 5

        if trends.get("activity") == "up":
            score += 3
        elif trends.get("activity") == "down":
            score -= 3

        return max(0, min(100, score))

    async def create_person_snapshot(
        self,
        person_name: str,
        period: SnapshotPeriod = SnapshotPeriod.WEEKLY
    ) -> Optional[Snapshot]:
        """
        Создать снимок активности человека.

        Включает:
        - Количество встреч
        - Назначенные задачи
        - Принятые решения
        - Проекты, в которых участвует
        """
        if not self.graph:
            return None

        graph_data = None
        if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            graph_data = self.graph.nx_graph

        if not graph_data:
            return None

        now = datetime.now(timezone.utc)
        period_start, period_end = self._get_period_bounds(period, now)

        # Ищем человека
        person_node = None
        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") == "Person" and (
                data.get("name", "").lower() == person_name.lower() or
                person_name.lower() in data.get("name", "").lower()
            ):
                person_node = (node_id, data)
                break

        if not person_node:
            logger.warning(f"Person {person_name} not found")
            return None

        node_id, person_data = person_node

        # Собираем связи
        meetings = []
        tasks = []
        decisions = []
        projects = set()

        # Исходящие связи
        for _, target, edge_data in graph_data.out_edges(node_id, data=True):
            target_data = graph_data.nodes[target]
            label = target_data.get("_label", "")
            rel_type = edge_data.get("type", edge_data.get("_type", ""))

            if label == "Meeting" or rel_type == "PARTICIPATED_IN":
                meetings.append(target_data)
            elif label == "Task" or rel_type == "ASSIGNED_TO":
                tasks.append(target_data)
            elif label == "Decision" or rel_type == "MADE_DECISION":
                decisions.append(target_data)
            elif label == "Project" or rel_type == "WORKS_ON":
                projects.add(target_data.get("name", target))

        # Входящие связи (задачи назначенные на человека)
        for source, _, edge_data in graph_data.in_edges(node_id, data=True):
            source_data = graph_data.nodes[source]
            label = source_data.get("_label", "")
            if label == "Task":
                tasks.append(source_data)

        snapshot_id = self._generate_snapshot_id(
            SnapshotType.PERSON, period, now, node_id
        )

        entity_snapshot = EntitySnapshot(
            entity_id=node_id,
            entity_type="person",
            entity_name=person_data.get("name", person_name),
            timestamp=now,
            metrics={
                "meetings_attended": len(meetings),
                "tasks_assigned": len(tasks),
                "tasks_completed": len([t for t in tasks if t.get("status") == "completed"]),
                "decisions_made": len(decisions),
                "projects_count": len(projects),
                "activity_score": len(meetings) * 2 + len(tasks) + len(decisions) * 3
            },
            relationships={
                "projects": list(projects),
                "recent_meetings": [m.get("title", "")[:50] for m in meetings[:5]]
            },
            metadata={
                "role": person_data.get("role", "unknown"),
                "department": person_data.get("department", "")
            }
        )

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            snapshot_type=SnapshotType.PERSON,
            period=period,
            timestamp=now,
            period_start=period_start,
            period_end=period_end,
            entities=[entity_snapshot],
            summary={
                "person_name": entity_snapshot.entity_name,
                "role": person_data.get("role", ""),
                "activity_level": "high" if entity_snapshot.metrics["activity_score"] > 10 else "medium" if entity_snapshot.metrics["activity_score"] > 3 else "low"
            },
            highlights=[],
            warnings=[],
            metadata={}
        )

        return snapshot

    async def create_organization_snapshot(
        self,
        period: SnapshotPeriod = SnapshotPeriod.DAILY
    ) -> Optional[Snapshot]:
        """
        Создать снимок всей организации.

        Агрегирует данные по всем проектам и людям.
        """
        if not self.graph:
            return None

        graph_data = None
        if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            graph_data = self.graph.nx_graph

        if not graph_data:
            return None

        now = datetime.now(timezone.utc)
        period_start, period_end = self._get_period_bounds(period, now)

        # Подсчитываем все сущности
        counts = {
            "persons": 0,
            "projects": 0,
            "meetings": 0,
            "tasks": 0,
            "decisions": 0
        }

        for _node_id, data in graph_data.nodes(data=True):
            label = data.get("_label", "").lower()
            if label == "person":
                counts["persons"] += 1
            elif label == "project":
                counts["projects"] += 1
            elif label == "meeting":
                counts["meetings"] += 1
            elif label == "task":
                counts["tasks"] += 1
            elif label == "decision":
                counts["decisions"] += 1

        snapshot_id = self._generate_snapshot_id(
            SnapshotType.ORGANIZATION, period, now
        )

        entity_snapshot = EntitySnapshot(
            entity_id="organization",
            entity_type="organization",
            entity_name="Tessbrain Organization",
            timestamp=now,
            metrics={
                "total_persons": counts["persons"],
                "total_projects": counts["projects"],
                "total_meetings": counts["meetings"],
                "total_tasks": counts["tasks"],
                "total_decisions": counts["decisions"],
                "total_nodes": graph_data.number_of_nodes(),
                "total_edges": graph_data.number_of_edges()
            },
            relationships={},
            metadata={}
        )

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            snapshot_type=SnapshotType.ORGANIZATION,
            period=period,
            timestamp=now,
            period_start=period_start,
            period_end=period_end,
            entities=[entity_snapshot],
            summary={
                "knowledge_graph_size": f"{counts['persons']} людей, {counts['projects']} проектов, {counts['meetings']} встреч",
                "activity_level": "high" if counts["meetings"] > 10 else "medium"
            },
            highlights=[],
            warnings=[],
            metadata={}
        )

        return snapshot

    def save_snapshot(self, snapshot: Snapshot) -> str:
        """Сохранить снимок в файл"""
        import os

        os.makedirs(self.storage_path, exist_ok=True)

        filename = f"{snapshot.snapshot_type.value}_{snapshot.snapshot_id}.json"
        filepath = os.path.join(self.storage_path, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)

        logger.info(f"Snapshot saved: {filepath}")
        return filepath

    def get_snapshot_history(self) -> List[str]:
        """Получить историю снимков"""
        return self._snapshot_history.copy()

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику генератора"""
        return {
            "snapshots_created": len(self._snapshot_history),
            "cached_snapshots": len(self._previous_snapshots),
            "storage_path": self.storage_path
        }

    async def get_current_snapshot_text(self) -> str:
        """
        Получить текстовое представление текущего состояния организации
        для использования в LLM промптах.
        """
        snapshot = await self.create_organization_snapshot()
        if not snapshot:
            return "Нет данных о состоянии организации."

        entity = snapshot.entities[0]
        metrics = entity.metrics

        text = f"""
# ТЕКУЩЕЕ СОСТОЯНИЕ ОРГАНИЗАЦИИ (Snapshot {snapshot.timestamp.strftime('%Y-%m-%d %H:%M')})

## Общие метрики
- Проектов: {metrics.get('total_projects', 0)}
- Встреч: {metrics.get('total_meetings', 0)}
- Задач: {metrics.get('total_tasks', 0)}
- Решений: {metrics.get('total_decisions', 0)}
- Людей: {metrics.get('total_persons', 0)}

## Граф знаний
- Узлов: {metrics.get('total_nodes', 0)}
- Связей: {metrics.get('total_edges', 0)}
"""

        # Собираем данные из графа
        if self.graph and hasattr(self.graph, 'nx_graph'):
            projects = []
            meetings = []
            folders = []
            org_info = None

            for _node_id, data in self.graph.nx_graph.nodes(data=True):
                label = data.get("_label", "")

                if label == "Project":
                    projects.append({
                        "name": data.get("name", "Unknown"),
                        "description": data.get("description", "")
                    })
                elif label == "Meeting":
                    meetings.append({
                        "title": data.get("title", "Unknown"),
                        "created_at": data.get("created_at", "")
                    })
                elif label == "Folder":
                    folders.append(data.get("name", "Unknown"))
                elif label == "Organization":
                    org_info = data

            # Информация об организации
            if org_info:
                text += "\n## Организация\n"
                text += f"- Название: {org_info.get('name', 'Не указано')}\n"
                if org_info.get('description'):
                    text += f"- Описание: {org_info.get('description')}\n"

            # Проекты
            if projects:
                text += f"\n## Проекты ({len(projects)})\n"
                for p in projects[:10]:
                    desc = f" - {p['description'][:50]}..." if p['description'] else ""
                    text += f"- **{p['name']}**{desc}\n"

            # Папки (категории)
            if folders:
                text += f"\n## Папки/Категории ({len(folders)})\n"
                for f in folders[:15]:
                    text += f"- {f}\n"

            # Последние встречи
            if meetings:
                # Сортируем по дате
                meetings_sorted = sorted(
                    meetings,
                    key=lambda x: x.get("created_at", ""),
                    reverse=True
                )[:10]

                text += f"\n## Последние встречи ({len(meetings)} всего)\n"
                for m in meetings_sorted:
                    text += f"- {m['title']}\n"

        return text

    async def create_person_profile(
        self,
        person_name: str,
        period: SnapshotPeriod = SnapshotPeriod.MONTHLY
    ) -> Dict[str, Any]:
        """
        Создать детальный профиль сотрудника.

        Включает:
        - Участие во встречах
        - Назначенные задачи
        - Принятые решения
        - Связи с проектами
        - Активность и вовлечённость
        """
        if not self.graph or not hasattr(self.graph, 'nx_graph'):
            return {"error": "Graph not available"}

        graph = self.graph.nx_graph
        now = datetime.now(timezone.utc)
        period_start, period_end = self._get_period_bounds(period, now)

        # Находим человека
        person_node = None
        for node_id, data in graph.nodes(data=True):
            if data.get("_label") == "Person":
                name = data.get("name", "").lower()
                if person_name.lower() in name or name in person_name.lower():
                    person_node = (node_id, data)
                    break

        if not person_node:
            return {"error": f"Person '{person_name}' not found"}

        node_id, person_data = person_node

        # Собираем связанные данные
        meetings = []
        tasks = []
        decisions = []
        projects = set()

        # Исходящие связи
        for _, target, _edge_data in graph.out_edges(node_id, data=True):
            target_data = graph.nodes.get(target, {})
            label = target_data.get("_label", "")

            if label == "Meeting":
                meetings.append(target_data)
            elif label == "Task":
                tasks.append(target_data)
            elif label == "Decision":
                decisions.append(target_data)
            elif label == "Project":
                projects.add(target_data.get("name", target))

        # Входящие связи
        for source, _, _edge_data in graph.in_edges(node_id, data=True):
            source_data = graph.nodes.get(source, {})
            label = source_data.get("_label", "")

            if label == "Meeting":
                meetings.append(source_data)
            elif label == "Task":
                tasks.append(source_data)

        # Статистика задач
        tasks_total = len(tasks)
        tasks_completed = len([t for t in tasks if t.get("status") in ("completed", "done")])
        tasks_in_progress = len([t for t in tasks if t.get("status") in ("in_progress", "active")])
        tasks_pending = len([t for t in tasks if t.get("status") in ("pending", "new", "todo")])

        # Формируем профиль
        profile = {
            "person_id": node_id,
            "name": person_data.get("name", person_name),
            "role": person_data.get("role", ""),
            "email": person_data.get("email", ""),
            "generated_at": now.isoformat(),
            "period": {
                "type": period.value,
                "start": period_start.isoformat(),
                "end": period_end.isoformat()
            },
            "activity": {
                "meetings_count": len(meetings),
                "tasks_total": tasks_total,
                "tasks_completed": tasks_completed,
                "tasks_in_progress": tasks_in_progress,
                "tasks_pending": tasks_pending,
                "decisions_count": len(decisions),
                "projects_count": len(projects)
            },
            "projects": list(projects),
            "recent_meetings": [
                {
                    "title": m.get("title", "Unknown"),
                    "date": m.get("date") or m.get("created_at", "")
                }
                for m in sorted(meetings, key=lambda x: x.get("created_at", ""), reverse=True)[:5]
            ],
            "recent_tasks": [
                {
                    "title": t.get("title", t.get("name", "Unknown")),
                    "status": t.get("status", "unknown")
                }
                for t in tasks[:10]
            ],
            "recent_decisions": [
                {
                    "summary": d.get("summary", d.get("title", "Unknown"))
                }
                for d in decisions[:5]
            ],
            "engagement_score": self._calculate_engagement_score(
                meetings_count=len(meetings),
                tasks_count=tasks_total,
                decisions_count=len(decisions)
            )
        }

        return profile

    def _calculate_engagement_score(
        self,
        meetings_count: int,
        tasks_count: int,
        decisions_count: int
    ) -> float:
        """Рассчитать оценку вовлечённости."""
        # Простая формула: встречи * 1 + задачи * 2 + решения * 3
        # Нормализуем к 100
        raw_score = meetings_count * 1 + tasks_count * 2 + decisions_count * 3
        # Логарифмическая шкала для нормализации
        import math
        if raw_score == 0:
            return 0.0
        return min(100.0, round(math.log(raw_score + 1) * 20, 1))

    async def create_detailed_project_snapshot(
        self,
        project_id: str,
        include_timeline: bool = True
    ) -> Dict[str, Any]:
        """
        Создать детальный снапшот проекта.

        Включает:
        - Полную информацию о проекте
        - Все встречи с хронологией
        - Все задачи с статусами
        - Все решения
        - Всех участников
        - KPI
        - Timeline событий
        """
        if not self.graph or not hasattr(self.graph, 'nx_graph'):
            return {"error": "Graph not available"}

        graph = self.graph.nx_graph
        now = datetime.now(timezone.utc)

        # Находим проект
        project_node = None
        for node_id, data in graph.nodes(data=True):
            if data.get("_label") == "Project":
                if data.get("project_id") == project_id or node_id == project_id or \
                   project_id.lower() in data.get("name", "").lower():
                    project_node = (node_id, data)
                    break

        if not project_node:
            return {"error": f"Project '{project_id}' not found"}

        node_id, project_data = project_node

        # Собираем все связанные данные
        meetings = []
        tasks = []
        decisions = []
        participants = {}
        kpis = []
        entities = []

        # Обходим все связи
        for _, target, _edge_data in graph.out_edges(node_id, data=True):
            target_data = graph.nodes.get(target, {})
            label = target_data.get("_label", "")

            if label == "Meeting":
                meetings.append(target_data)
            elif label == "Task":
                tasks.append(target_data)
            elif label == "Decision":
                decisions.append(target_data)
            elif label == "Person":
                name = target_data.get("name", target)
                participants[name] = target_data
            elif label == "KPI":
                kpis.append(target_data)
            elif label == "Entity":
                entities.append(target_data)

        for source, _, _edge_data in graph.in_edges(node_id, data=True):
            source_data = graph.nodes.get(source, {})
            label = source_data.get("_label", "")

            if label == "Person":
                name = source_data.get("name", source)
                participants[name] = source_data

        # Также ищем связи через встречи
        for meeting in meetings:
            meeting_id = meeting.get("meeting_id", meeting.get("id", ""))
            for m_node_id, m_data in graph.nodes(data=True):
                if m_data.get("meeting_id") == meeting_id or m_node_id == meeting_id:
                    for _, target, _ in graph.out_edges(m_node_id, data=True):
                        target_data = graph.nodes.get(target, {})
                        if target_data.get("_label") == "Person":
                            name = target_data.get("name", target)
                            participants[name] = target_data
                        elif target_data.get("_label") == "Task":
                            if target_data not in tasks:
                                tasks.append(target_data)
                        elif target_data.get("_label") == "Decision":
                            if target_data not in decisions:
                                decisions.append(target_data)

        # Статистика задач
        tasks_by_status = {}
        for task in tasks:
            status = task.get("status", "unknown")
            tasks_by_status[status] = tasks_by_status.get(status, 0) + 1

        # Timeline
        timeline = []
        if include_timeline:
            # Добавляем события
            for m in meetings:
                date = m.get("date") or m.get("created_at", "")
                timeline.append({
                    "type": "meeting",
                    "date": date,
                    "title": m.get("title", "Meeting"),
                    "description": f"Встреча: {m.get('title', '')}"
                })

            for d in decisions:
                date = d.get("created_at", "")
                timeline.append({
                    "type": "decision",
                    "date": date,
                    "title": d.get("summary", "Decision")[:50],
                    "description": d.get("summary", "")
                })

            for t in tasks:
                if t.get("status") in ("completed", "done"):
                    date = t.get("completed_at") or t.get("updated_at", "")
                    timeline.append({
                        "type": "task_completed",
                        "date": date,
                        "title": t.get("title", t.get("name", "Task")),
                        "description": "Задача завершена"
                    })

            # Сортируем по дате
            timeline.sort(key=lambda x: x.get("date", ""), reverse=True)

        # Формируем снапшот
        snapshot = {
            "project_id": project_id,
            "name": project_data.get("name", "Unknown"),
            "description": project_data.get("description", ""),
            "status": project_data.get("status", "active"),
            "generated_at": now.isoformat(),
            "summary": {
                "meetings_count": len(meetings),
                "tasks_total": len(tasks),
                "tasks_by_status": tasks_by_status,
                "decisions_count": len(decisions),
                "participants_count": len(participants),
                "kpis_count": len(kpis),
                "entities_count": len(entities)
            },
            "participants": [
                {
                    "name": name,
                    "role": data.get("role", "")
                }
                for name, data in participants.items()
            ],
            "meetings": [
                {
                    "id": m.get("meeting_id", m.get("id", "")),
                    "title": m.get("title", "Unknown"),
                    "date": m.get("date") or m.get("created_at", "")
                }
                for m in sorted(meetings, key=lambda x: x.get("created_at", ""), reverse=True)
            ],
            "tasks": [
                {
                    "title": t.get("title", t.get("name", "Unknown")),
                    "status": t.get("status", "unknown"),
                    "assignee": t.get("assignee", ""),
                    "priority": t.get("priority", "")
                }
                for t in tasks
            ],
            "decisions": [
                {
                    "summary": d.get("summary", ""),
                    "date": d.get("created_at", "")
                }
                for d in decisions
            ],
            "kpis": [
                {
                    "name": k.get("name", ""),
                    "value": k.get("value", ""),
                    "unit": k.get("unit", "")
                }
                for k in kpis
            ],
            "timeline": timeline[:30] if include_timeline else []
        }

        return snapshot

    async def get_all_people_profiles(self, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Получить лёгкие профили всех людей (id/name/role/department) для
        списков и пикеров.

        Backend-agnostic: раньше код читал ТОЛЬКО self.graph.nx_graph.nodes →
        на Neo4j (nx_graph=None) падал с «'NoneType' object has no attribute
        'nodes'», и список людей (напр. выпадашка руководителей) был пуст.
        Теперь через find_nodes_by_label («Person») с tenant-фильтром.
        """
        if not self.graph:
            return []

        nodes: List[Dict[str, Any]] = []
        try:
            nodes = await self.graph.find_nodes_by_label(
                "Person", limit=2000, tenant_id=tenant_id, strict_tenant=True
            )
        except Exception as e:
            logger.debug(f"find_nodes_by_label(Person) failed: {e}")

        # Fallback: прямой обход NetworkX, если он есть и find ничего не дал
        if not nodes and getattr(self.graph, "nx_graph", None) is not None:
            for nid, data in self.graph.nx_graph.nodes(data=True):
                if data.get("_label") == "Person":
                    nodes.append({"id": nid, **data})

        profiles: List[Dict[str, Any]] = []
        seen: set = set()
        for n in nodes:
            name = (n.get("name") or "").strip()
            if not name:
                continue
            pid = n.get("id") or n.get("person_id") or ""
            key = pid or name.lower()
            if key in seen:
                continue
            seen.add(key)
            profiles.append({
                "person_id": pid,
                "id": pid,
                "name": name,
                "role": n.get("role") or "",
                "department": n.get("department") or "",
                "engagement_score": n.get("total_mentions", 0) or 0,
            })

        profiles.sort(key=lambda x: x.get("engagement_score", 0), reverse=True)
        return profiles

