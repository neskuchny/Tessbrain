# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Anomaly Detector
================================

Обнаружение аномалий и потенциальных проблем в данных.

Типы аномалий:
1. Застрявшие задачи — задачи без прогресса долгое время
2. Пропущенные дедлайны — просроченные сроки
3. Изолированные сущности — люди/проекты без связей
4. Аномальная активность — резкие изменения в поведении
5. Конфликты данных — противоречивая информация
6. Риски проектов — признаки проблем в проектах
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AnomalyType(str, Enum):
    """Типы аномалий"""
    STUCK_TASK = "stuck_task"               # Застрявшая задача
    MISSED_DEADLINE = "missed_deadline"     # Пропущенный дедлайн
    ISOLATED_ENTITY = "isolated_entity"     # Изолированная сущность
    UNUSUAL_ACTIVITY = "unusual_activity"   # Необычная активность
    DATA_CONFLICT = "data_conflict"         # Конфликт данных
    PROJECT_RISK = "project_risk"           # Риск проекта
    OVERDUE_DECISION = "overdue_decision"   # Решение без реализации
    ORPHAN_MEETING = "orphan_meeting"       # Встреча без результатов


class AnomalySeverity(str, Enum):
    """Серьёзность аномалии"""
    CRITICAL = "critical"   # Требует немедленного внимания
    HIGH = "high"           # Важно, но не срочно
    MEDIUM = "medium"       # Стоит обратить внимание
    LOW = "low"             # Информационно


@dataclass
class Anomaly:
    """Обнаруженная аномалия"""
    anomaly_id: str
    anomaly_type: AnomalyType
    severity: AnomalySeverity

    # Описание
    title: str
    description: str

    # Связанная сущность
    entity_id: str
    entity_type: str
    entity_name: str

    # Детали
    details: Dict[str, Any] = field(default_factory=dict)

    # Рекомендуемые действия
    suggested_actions: List[str] = field(default_factory=list)

    # Время обнаружения
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Статус
    is_resolved: bool = False
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "anomaly_type": self.anomaly_type.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "details": self.details,
            "suggested_actions": self.suggested_actions,
            "detected_at": self.detected_at.isoformat(),
            "is_resolved": self.is_resolved,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by
        }


class AnomalyDetector:
    """
    Детектор аномалий в графе знаний.

    Анализирует данные и выявляет потенциальные проблемы:
    - Застрявшие задачи
    - Пропущенные дедлайны
    - Изолированные сущности
    - Риски проектов

    Пример использования:
    ```python
    detector = AnomalyDetector(graph_builder=graph)

    # Запустить полное сканирование
    anomalies = await detector.scan_all()

    # Проверить конкретный тип
    stuck_tasks = await detector.detect_stuck_tasks()
    ```
    """

    # Пороги для обнаружения
    STUCK_TASK_DAYS = 7          # Задача без прогресса более 7 дней
    ISOLATED_MIN_EDGES = 2       # Минимум связей для "связанной" сущности
    INACTIVE_PROJECT_DAYS = 14   # Проект без активности более 14 дней

    def __init__(
        self,
        graph_builder=None,
        thresholds: Optional[Dict[str, Any]] = None
    ):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            thresholds: Кастомные пороги обнаружения
        """
        self.graph = graph_builder

        # Применяем кастомные пороги
        if thresholds:
            self.STUCK_TASK_DAYS = thresholds.get("stuck_task_days", self.STUCK_TASK_DAYS)
            self.ISOLATED_MIN_EDGES = thresholds.get("isolated_min_edges", self.ISOLATED_MIN_EDGES)
            self.INACTIVE_PROJECT_DAYS = thresholds.get("inactive_project_days", self.INACTIVE_PROJECT_DAYS)

        # Кеш обнаруженных аномалий
        self._anomalies: List[Anomaly] = []

        # Счётчик для ID
        self._counter = 0

    def _generate_anomaly_id(self, anomaly_type: AnomalyType) -> str:
        """Генерировать ID аномалии"""
        self._counter += 1
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"anom_{anomaly_type.value}_{timestamp}_{self._counter}"

    def _get_graph_data(self):
        """Получить данные графа"""
        if not self.graph:
            return None
        if hasattr(self.graph, 'nx_graph') and self.graph.nx_graph:
            return self.graph.nx_graph
        return None

    async def scan_all(self) -> List[Anomaly]:
        """
        Запустить полное сканирование на все типы аномалий.

        Returns:
            Список всех обнаруженных аномалий
        """
        logger.info("🔍 Starting full anomaly scan...")

        all_anomalies: List[Anomaly] = []

        # Запускаем все детекторы
        detectors = [
            self.detect_stuck_tasks,
            self.detect_missed_deadlines,
            self.detect_isolated_entities,
            self.detect_project_risks,
            self.detect_orphan_meetings,
        ]

        for detector in detectors:
            try:
                anomalies = await detector()
                all_anomalies.extend(anomalies)
            except Exception as e:
                logger.error(f"Detector {detector.__name__} failed: {e}")

        # Сохраняем в кеш
        self._anomalies.extend(all_anomalies)

        logger.info(f"🔍 Scan complete. Found {len(all_anomalies)} anomalies.")

        return all_anomalies

    async def detect_all(self) -> Dict[str, Any]:
        """
        Обнаружить все аномалии и вернуть сгруппированный результат.

        Returns:
            Словарь с аномалиями по категориям
        """
        anomalies = await self.scan_all()

        # Группируем по типам
        result = {
            "stuck_tasks": [],
            "missed_deadlines": [],
            "isolated_entities": [],
            "project_risks": [],
            "orphan_meetings": [],
            "other": []
        }

        for anomaly in anomalies:
            atype = anomaly.anomaly_type.value if hasattr(anomaly.anomaly_type, 'value') else str(anomaly.anomaly_type)

            if "stuck" in atype.lower():
                result["stuck_tasks"].append(anomaly.__dict__ if hasattr(anomaly, '__dict__') else str(anomaly))
            elif "deadline" in atype.lower() or "overdue" in atype.lower():
                result["missed_deadlines"].append(anomaly.__dict__ if hasattr(anomaly, '__dict__') else str(anomaly))
            elif "isolated" in atype.lower() or "orphan" in atype.lower():
                result["isolated_entities"].append(anomaly.__dict__ if hasattr(anomaly, '__dict__') else str(anomaly))
            elif "risk" in atype.lower():
                result["project_risks"].append(anomaly.__dict__ if hasattr(anomaly, '__dict__') else str(anomaly))
            elif "meeting" in atype.lower():
                result["orphan_meetings"].append(anomaly.__dict__ if hasattr(anomaly, '__dict__') else str(anomaly))
            else:
                result["other"].append(anomaly.__dict__ if hasattr(anomaly, '__dict__') else str(anomaly))

        return result

    async def detect_stuck_tasks(self) -> List[Anomaly]:
        """
        Обнаружить застрявшие задачи.

        Задача считается застрявшей если:
        - Статус "in_progress" более N дней
        - Нет обновлений более N дней
        """
        anomalies = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return anomalies

        now = datetime.now(timezone.utc)
        now - timedelta(days=self.STUCK_TASK_DAYS)

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") != "Task":
                continue

            status = data.get("status", "").lower()
            if status not in ("in_progress", "active", "pending"):
                continue

            # Проверяем дату обновления
            updated_at = data.get("updated_at", data.get("created_at", ""))
            if updated_at:
                try:
                    if isinstance(updated_at, str):
                        # Парсим ISO формат
                        updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    else:
                        updated_dt = updated_at

                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)

                    days_stuck = (now - updated_dt).days

                    if days_stuck >= self.STUCK_TASK_DAYS:
                        anomaly = Anomaly(
                            anomaly_id=self._generate_anomaly_id(AnomalyType.STUCK_TASK),
                            anomaly_type=AnomalyType.STUCK_TASK,
                            severity=AnomalySeverity.HIGH if days_stuck > 14 else AnomalySeverity.MEDIUM,
                            title=f"Застрявшая задача: {data.get('title', node_id)[:50]}",
                            description=f"Задача в статусе '{status}' без обновлений {days_stuck} дней",
                            entity_id=node_id,
                            entity_type="task",
                            entity_name=data.get("title", data.get("name", node_id)),
                            details={
                                "status": status,
                                "days_stuck": days_stuck,
                                "last_updated": updated_at,
                                "assignee": data.get("assignee", "не назначен")
                            },
                            suggested_actions=[
                                "Проверить статус задачи с исполнителем",
                                "Обновить прогресс или переназначить",
                                "Рассмотреть закрытие если неактуально"
                            ]
                        )
                        anomalies.append(anomaly)

                except (ValueError, TypeError) as e:
                    logger.debug(f"Could not parse date for task {node_id}: {e}")

        return anomalies

    async def detect_missed_deadlines(self) -> List[Anomaly]:
        """
        Обнаружить пропущенные дедлайны.
        """
        anomalies = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return anomalies

        now = datetime.now(timezone.utc)

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") != "Task":
                continue

            # Пропускаем завершённые задачи
            status = data.get("status", "").lower()
            if status in ("completed", "done", "closed"):
                continue

            deadline = data.get("deadline", data.get("due_date", ""))
            if not deadline:
                continue

            try:
                if isinstance(deadline, str):
                    # Пробуем разные форматы
                    if "T" in deadline:
                        deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                    else:
                        deadline_dt = datetime.strptime(deadline[:10], "%Y-%m-%d")
                        deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
                else:
                    deadline_dt = deadline

                if deadline_dt.tzinfo is None:
                    deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)

                if deadline_dt < now:
                    days_overdue = (now - deadline_dt).days

                    anomaly = Anomaly(
                        anomaly_id=self._generate_anomaly_id(AnomalyType.MISSED_DEADLINE),
                        anomaly_type=AnomalyType.MISSED_DEADLINE,
                        severity=AnomalySeverity.CRITICAL if days_overdue > 7 else AnomalySeverity.HIGH,
                        title=f"Просроченная задача: {data.get('title', node_id)[:50]}",
                        description=f"Дедлайн пропущен {days_overdue} дней назад",
                        entity_id=node_id,
                        entity_type="task",
                        entity_name=data.get("title", data.get("name", node_id)),
                        details={
                            "deadline": str(deadline),
                            "days_overdue": days_overdue,
                            "status": status,
                            "assignee": data.get("assignee", "не назначен")
                        },
                        suggested_actions=[
                            "Срочно связаться с исполнителем",
                            "Пересмотреть сроки",
                            "Эскалировать если критично"
                        ]
                    )
                    anomalies.append(anomaly)

            except (ValueError, TypeError) as e:
                logger.debug(f"Could not parse deadline for task {node_id}: {e}")

        return anomalies

    async def detect_isolated_entities(self) -> List[Anomaly]:
        """
        Обнаружить изолированные сущности.

        Сущность изолирована если имеет менее N связей.
        """
        anomalies = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return anomalies

        for node_id, data in graph_data.nodes(data=True):
            label = data.get("_label", "")

            # Проверяем только важные типы
            if label not in ("Person", "Project"):
                continue

            # Считаем связи
            in_degree = graph_data.in_degree(node_id)
            out_degree = graph_data.out_degree(node_id)
            total_edges = in_degree + out_degree

            if total_edges < self.ISOLATED_MIN_EDGES:
                severity = AnomalySeverity.LOW
                if label == "Project" and total_edges == 0:
                    severity = AnomalySeverity.MEDIUM

                anomaly = Anomaly(
                    anomaly_id=self._generate_anomaly_id(AnomalyType.ISOLATED_ENTITY),
                    anomaly_type=AnomalyType.ISOLATED_ENTITY,
                    severity=severity,
                    title=f"Изолированная сущность: {data.get('name', node_id)}",
                    description=f"{label} имеет только {total_edges} связей в графе знаний",
                    entity_id=node_id,
                    entity_type=label.lower(),
                    entity_name=data.get("name", node_id),
                    details={
                        "in_edges": in_degree,
                        "out_edges": out_degree,
                        "total_edges": total_edges
                    },
                    suggested_actions=[
                        "Проверить актуальность данных",
                        "Добавить связи если сущность активна",
                        "Рассмотреть удаление если неактуальна"
                    ]
                )
                anomalies.append(anomaly)

        return anomalies

    async def detect_project_risks(self) -> List[Anomaly]:
        """
        Обнаружить риски в проектах.

        Признаки риска:
        - Много незавершённых задач
        - Мало участников
        - Нет недавних встреч
        """
        anomalies = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return anomalies

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") != "Project":
                continue

            project_name = data.get("name", data.get("title", node_id))

            # Собираем статистику проекта
            tasks_total = 0
            tasks_completed = 0
            tasks_overdue = 0
            participants = set()
            meetings = 0

            for _, target, _ in graph_data.out_edges(node_id, data=True):
                target_data = graph_data.nodes[target]
                target_label = target_data.get("_label", "")

                if target_label == "Task":
                    tasks_total += 1
                    status = target_data.get("status", "").lower()
                    if status in ("completed", "done"):
                        tasks_completed += 1
                    # Проверяем просрочку
                    deadline = target_data.get("deadline", "")
                    if deadline and status not in ("completed", "done"):
                        try:
                            if isinstance(deadline, str):
                                deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                            else:
                                deadline_dt = deadline
                            if deadline_dt < datetime.now(timezone.utc):
                                tasks_overdue += 1
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)

                elif target_label == "Meeting":
                    meetings += 1
                elif target_label == "Person":
                    participants.add(target)

            # Входящие связи (люди работающие над проектом)
            for source, _, _ in graph_data.in_edges(node_id, data=True):
                source_data = graph_data.nodes[source]
                if source_data.get("_label") == "Person":
                    participants.add(source)

            # Анализируем риски
            risks = []
            severity = AnomalySeverity.LOW

            # Много просроченных задач
            if tasks_overdue >= 3:
                risks.append(f"{tasks_overdue} просроченных задач")
                severity = AnomalySeverity.HIGH
            elif tasks_overdue >= 1:
                risks.append(f"{tasks_overdue} просроченная задача")
                severity = max(severity, AnomalySeverity.MEDIUM)

            # Низкий процент завершения при большом количестве задач
            if tasks_total >= 5:
                completion_rate = tasks_completed / tasks_total
                if completion_rate < 0.2:
                    risks.append(f"Низкий прогресс: {completion_rate:.0%} завершено")
                    severity = max(severity, AnomalySeverity.MEDIUM)

            # Мало участников
            if tasks_total >= 5 and len(participants) < 2:
                risks.append(f"Мало участников ({len(participants)}) для {tasks_total} задач")
                severity = max(severity, AnomalySeverity.MEDIUM)

            # Нет встреч
            if tasks_total >= 3 and meetings == 0:
                risks.append("Нет связанных встреч")

            if risks:
                anomaly = Anomaly(
                    anomaly_id=self._generate_anomaly_id(AnomalyType.PROJECT_RISK),
                    anomaly_type=AnomalyType.PROJECT_RISK,
                    severity=severity,
                    title=f"Риски проекта: {project_name}",
                    description="; ".join(risks),
                    entity_id=node_id,
                    entity_type="project",
                    entity_name=project_name,
                    details={
                        "tasks_total": tasks_total,
                        "tasks_completed": tasks_completed,
                        "tasks_overdue": tasks_overdue,
                        "participants_count": len(participants),
                        "meetings_count": meetings,
                        "risks": risks
                    },
                    suggested_actions=[
                        "Провести ревью проекта",
                        "Пересмотреть приоритеты задач",
                        "Рассмотреть добавление ресурсов"
                    ]
                )
                anomalies.append(anomaly)

        return anomalies

    async def detect_orphan_meetings(self) -> List[Anomaly]:
        """
        Обнаружить встречи без результатов.

        Встреча-сирота — это встреча без:
        - Задач
        - Решений
        - Участников
        """
        anomalies = []
        graph_data = self._get_graph_data()

        if not graph_data:
            return anomalies

        for node_id, data in graph_data.nodes(data=True):
            if data.get("_label") != "Meeting":
                continue

            meeting_title = data.get("title", data.get("name", node_id))

            # Считаем связи
            tasks = 0
            decisions = 0
            participants = 0

            for _, target, _ in graph_data.out_edges(node_id, data=True):
                target_data = graph_data.nodes[target]
                label = target_data.get("_label", "")

                if label == "Task":
                    tasks += 1
                elif label == "Decision":
                    decisions += 1
                elif label == "Person":
                    participants += 1

            for source, _, _ in graph_data.in_edges(node_id, data=True):
                source_data = graph_data.nodes[source]
                if source_data.get("_label") == "Person":
                    participants += 1

            # Встреча без результатов
            if tasks == 0 and decisions == 0:
                anomaly = Anomaly(
                    anomaly_id=self._generate_anomaly_id(AnomalyType.ORPHAN_MEETING),
                    anomaly_type=AnomalyType.ORPHAN_MEETING,
                    severity=AnomalySeverity.LOW,
                    title=f"Встреча без результатов: {meeting_title[:50]}",
                    description=f"Встреча не имеет связанных задач ({tasks}) или решений ({decisions})",
                    entity_id=node_id,
                    entity_type="meeting",
                    entity_name=meeting_title,
                    details={
                        "tasks_count": tasks,
                        "decisions_count": decisions,
                        "participants_count": participants
                    },
                    suggested_actions=[
                        "Проверить были ли результаты встречи",
                        "Добавить задачи/решения если они есть",
                        "Рассмотреть эффективность подобных встреч"
                    ]
                )
                anomalies.append(anomaly)

        return anomalies

    def get_anomalies(
        self,
        anomaly_type: Optional[AnomalyType] = None,
        severity: Optional[AnomalySeverity] = None,
        unresolved_only: bool = True
    ) -> List[Anomaly]:
        """Получить аномалии с фильтрацией"""
        result = self._anomalies

        if anomaly_type:
            result = [a for a in result if a.anomaly_type == anomaly_type]

        if severity:
            result = [a for a in result if a.severity == severity]

        if unresolved_only:
            result = [a for a in result if not a.is_resolved]

        return result

    def resolve_anomaly(
        self,
        anomaly_id: str,
        resolved_by: Optional[str] = None
    ) -> bool:
        """Отметить аномалию как решённую"""
        for anomaly in self._anomalies:
            if anomaly.anomaly_id == anomaly_id:
                anomaly.is_resolved = True
                anomaly.resolved_at = datetime.now(timezone.utc)
                anomaly.resolved_by = resolved_by
                return True
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику детектора"""
        return {
            "total_anomalies": len(self._anomalies),
            "unresolved": len([a for a in self._anomalies if not a.is_resolved]),
            "by_type": {
                t.value: len([a for a in self._anomalies if a.anomaly_type == t])
                for t in AnomalyType
            },
            "by_severity": {
                s.value: len([a for a in self._anomalies if a.severity == s])
                for s in AnomalySeverity
            }
        }

