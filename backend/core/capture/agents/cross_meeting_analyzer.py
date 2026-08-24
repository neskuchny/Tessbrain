# -*- coding: utf-8 -*-
"""
Cross-Meeting Analyzer Agent - анализ связей между встречами

Находит и создаёт связи между встречами на основе:
1. Общих участников
2. Общих проектов
3. Общих тем/контекста
4. Временной близости
5. Эволюции сущностей (задачи, решения, KPI)
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class MeetingLink:
    """Связь между встречами"""
    source_meeting_id: str
    target_meeting_id: str
    relevance_score: float
    link_type: str  # continuation, follow_up, related, reference
    common_participants: List[str] = field(default_factory=list)
    common_projects: List[str] = field(default_factory=list)
    common_topics: List[str] = field(default_factory=list)
    common_entities: List[str] = field(default_factory=list)
    temporal_distance_days: int = 0
    evolution_notes: List[str] = field(default_factory=list)


@dataclass
class EntityEvolution:
    """Эволюция сущности между встречами"""
    entity_id: str
    entity_type: str  # task, decision, kpi, project
    entity_name: str
    first_meeting_id: str
    last_meeting_id: str
    meetings_mentioned: List[str] = field(default_factory=list)
    status_changes: List[Dict[str, Any]] = field(default_factory=list)
    value_changes: List[Dict[str, Any]] = field(default_factory=list)


class CrossMeetingAnalyzer:
    """
    Анализирует связи между встречами и отслеживает эволюцию сущностей

    Возможности:
    - Поиск связанных встреч по участникам, проектам, темам
    - Отслеживание эволюции задач между встречами
    - Анализ изменения KPI во времени
    - Создание цепочек решений
    - Выявление повторяющихся тем
    """

    def __init__(self, graph_builder=None, llm_router=None):
        self.name = "cross_meeting_analyzer"
        self.graph = graph_builder
        self.llm = llm_router

        # Веса для расчёта релевантности
        self.weights = {
            "participants": 0.35,  # Общие участники - сильный сигнал
            "projects": 0.30,      # Общие проекты
            "topics": 0.15,        # Общие темы
            "entities": 0.10,      # Общие сущности (задачи, решения)
            "temporal": 0.10       # Временная близость
        }

        logger.info(f"✅ {self.name} инициализирован")

    async def analyze_meeting_links(
        self,
        meeting_id: str,
        meeting_data: Dict[str, Any],
        max_links: int = 10
    ) -> Dict[str, Any]:
        """
        Анализирует связи новой встречи с существующими

        Args:
            meeting_id: ID анализируемой встречи
            meeting_data: Данные встречи (участники, проекты, задачи и т.д.)
            max_links: Максимальное количество связей

        Returns:
            Dict с найденными связями и эволюцией сущностей
        """
        try:
            logger.info(f"🔗 Анализ связей для встречи {meeting_id}")
            start_time = datetime.utcnow()

            result = {
                "meeting_id": meeting_id,
                "links": [],
                "entity_evolution": [],
                "recurring_topics": [],
                "decision_chains": [],
                "stats": {}
            }

            # Получаем все встречи из графа
            all_meetings = await self._get_all_meetings()
            if not all_meetings:
                logger.info("  ℹ️ Нет предыдущих встреч для анализа")
                return result

            # Извлекаем ключевые элементы текущей встречи
            current_data = self._extract_meeting_features(meeting_data)

            # Анализируем каждую встречу
            links = []
            for other_meeting in all_meetings:
                if other_meeting.get("id") == meeting_id or other_meeting.get("meeting_id") == meeting_id:
                    continue

                # Вычисляем релевантность
                link = await self._calculate_meeting_link(
                    meeting_id, current_data,
                    other_meeting
                )

                if link and link.relevance_score >= 0.15:
                    links.append(link)

            # Сортируем по релевантности
            links.sort(key=lambda x: x.relevance_score, reverse=True)
            result["links"] = [self._link_to_dict(l) for l in links[:max_links]]

            # Отслеживаем эволюцию сущностей
            if links:
                evolution = await self._track_entity_evolution(
                    meeting_id, current_data, links[:5]
                )
                result["entity_evolution"] = evolution

                # Находим повторяющиеся темы
                result["recurring_topics"] = self._find_recurring_topics(
                    current_data, links[:5]
                )

                # Строим цепочки решений
                result["decision_chains"] = await self._build_decision_chains(
                    meeting_id, current_data, links[:5]
                )

            # Создаём связи в графе
            if self.graph and links:
                await self._create_graph_links(meeting_id, links[:max_links])

            # Статистика
            duration = (datetime.utcnow() - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "meetings_analyzed": len(all_meetings),
                "links_found": len(result["links"]),
                "entities_tracked": len(result["entity_evolution"]),
                "recurring_topics": len(result["recurring_topics"])
            }

            logger.info(f"  ✅ Найдено {len(result['links'])} связей за {duration:.2f}s")
            return result

        except Exception as e:
            logger.error(f"❌ Ошибка анализа связей: {e}", exc_info=True)
            return {"meeting_id": meeting_id, "links": [], "error": str(e)}

    async def _get_all_meetings(self) -> List[Dict[str, Any]]:
        """Получает все встречи из графа"""
        if not self.graph:
            return []

        try:
            # Unified async graph read API (works for both NetworkX and Neo4j)
            nodes = await self.graph.get_all_nodes_async(label="Meeting")
            return nodes or []
        except Exception as e:
            logger.error(f"Ошибка получения встреч: {e}")
            return []

    async def _resolve_meeting_node_id(self, meeting_id: str) -> Optional[str]:
        """
        В графе id узла встречи может отличаться от meeting_id (например 'meeting_<uuid>').
        Этот метод пытается найти реальный node_id по meeting_id.
        """
        if not self.graph:
            return None
        try:
            nodes = await self.graph.get_all_nodes_async(label="Meeting")
            for n in nodes or []:
                if n.get("id") == meeting_id:
                    return n.get("id")
                if n.get("meeting_id") == meeting_id:
                    return n.get("id")
            return None
        except Exception:
            return None

    def _extract_meeting_features(self, data: Dict[str, Any]) -> Dict[str, Set[str]]:
        """Извлекает ключевые элементы встречи"""
        features = {
            "participants": set(),
            "projects": set(),
            "topics": set(),
            "tasks": set(),
            "decisions": set(),
            "entities": set(),
            "date": None
        }

        # Участники
        for p in data.get("participants", []):
            if isinstance(p, dict):
                name = p.get("name") or p.get("participant_name")
            else:
                name = str(p)
            if name:
                features["participants"].add(name.lower().strip())

        # Проекты
        for proj in data.get("projects", []):
            if isinstance(proj, dict):
                name = proj.get("name") or proj.get("project_name")
            else:
                name = str(proj)
            if name:
                features["projects"].add(name.lower().strip())

        # Задачи
        for task in data.get("tasks", []):
            if isinstance(task, dict):
                title = task.get("title") or task.get("task_title") or task.get("description")
                if title:
                    features["tasks"].add(title.lower().strip())
                    # Добавляем категорию как тему
                    category = task.get("category") or task.get("task_category")
                    if category:
                        features["topics"].add(category.lower())

        # Решения
        for dec in data.get("decisions", []):
            if isinstance(dec, dict):
                text = dec.get("decision") or dec.get("text") or dec.get("description")
                if text:
                    features["decisions"].add(text.lower().strip()[:100])
                    category = dec.get("category") or dec.get("decision_category")
                    if category:
                        features["topics"].add(category.lower())

        # Сущности
        for ent in data.get("entities", []):
            if isinstance(ent, dict):
                name = ent.get("name") or ent.get("entity_name")
            else:
                name = str(ent)
            if name:
                features["entities"].add(name.lower().strip())

        # Дата
        date = data.get("date") or data.get("created_at") or data.get("meeting_date")
        if date:
            if isinstance(date, str):
                try:
                    features["date"] = datetime.fromisoformat(date.replace("Z", "+00:00"))
                except Exception:
                    features["date"] = datetime.now()
            elif isinstance(date, datetime):
                features["date"] = date

        return features

    async def _calculate_meeting_link(
        self,
        source_id: str,
        source_features: Dict[str, Set[str]],
        target_meeting: Dict[str, Any]
    ) -> Optional[MeetingLink]:
        """Вычисляет связь между двумя встречами"""
        try:
            target_id = target_meeting.get("id") or target_meeting.get("meeting_id")
            if not target_id:
                return None

            # Извлекаем features целевой встречи
            target_features = self._extract_meeting_features(target_meeting)

            # Вычисляем пересечения
            common_participants = source_features["participants"] & target_features["participants"]
            common_projects = source_features["projects"] & target_features["projects"]
            common_topics = source_features["topics"] & target_features["topics"]
            common_entities = source_features["entities"] & target_features["entities"]

            # Вычисляем оценки по каждому критерию
            scores = {}

            # Участники
            if source_features["participants"] or target_features["participants"]:
                union = source_features["participants"] | target_features["participants"]
                scores["participants"] = len(common_participants) / len(union) if union else 0
            else:
                scores["participants"] = 0

            # Проекты
            if source_features["projects"] or target_features["projects"]:
                union = source_features["projects"] | target_features["projects"]
                scores["projects"] = len(common_projects) / len(union) if union else 0
            else:
                scores["projects"] = 0

            # Темы
            if source_features["topics"] or target_features["topics"]:
                union = source_features["topics"] | target_features["topics"]
                scores["topics"] = len(common_topics) / len(union) if union else 0
            else:
                scores["topics"] = 0

            # Сущности
            if source_features["entities"] or target_features["entities"]:
                union = source_features["entities"] | target_features["entities"]
                scores["entities"] = len(common_entities) / len(union) if union else 0
            else:
                scores["entities"] = 0

            # Временная близость
            temporal_score = 0
            temporal_distance = 0
            if source_features["date"] and target_features["date"]:
                days_diff = abs((source_features["date"] - target_features["date"]).days)
                temporal_distance = days_diff
                if days_diff <= 7:
                    temporal_score = 1.0
                elif days_diff <= 14:
                    temporal_score = 0.8
                elif days_diff <= 30:
                    temporal_score = 0.6
                elif days_diff <= 90:
                    temporal_score = 0.3
                else:
                    temporal_score = 0.1
            scores["temporal"] = temporal_score

            # Взвешенная сумма
            relevance = sum(
                scores[k] * self.weights[k]
                for k in self.weights.keys()
            )

            # Определяем тип связи
            link_type = self._determine_link_type(
                scores, temporal_distance, common_projects
            )

            return MeetingLink(
                source_meeting_id=source_id,
                target_meeting_id=target_id,
                relevance_score=round(relevance, 3),
                link_type=link_type,
                common_participants=list(common_participants),
                common_projects=list(common_projects),
                common_topics=list(common_topics),
                common_entities=list(common_entities),
                temporal_distance_days=temporal_distance
            )

        except Exception as e:
            logger.error(f"Ошибка расчёта связи: {e}")
            return None

    def _determine_link_type(
        self,
        scores: Dict[str, float],
        temporal_distance: int,
        common_projects: Set[str]
    ) -> str:
        """Определяет тип связи между встречами"""
        # Продолжение - та же тема, близко по времени
        if temporal_distance <= 7 and scores["projects"] > 0.5:
            return "continuation"

        # Follow-up - те же участники обсуждают тот же проект позже
        if temporal_distance <= 30 and scores["participants"] > 0.3 and common_projects:
            return "follow_up"

        # Связанная - общие темы или сущности
        if scores["topics"] > 0.3 or scores["entities"] > 0.3:
            return "related"

        # Ссылка - слабая связь
        return "reference"

    async def _track_entity_evolution(
        self,
        meeting_id: str,
        current_features: Dict[str, Set[str]],
        links: List[MeetingLink]
    ) -> List[Dict[str, Any]]:
        """Отслеживает эволюцию сущностей между встречами"""
        evolution = []

        try:
            # Собираем все связанные встречи
            related_meeting_ids = [l.target_meeting_id for l in links]

            # Для каждой задачи ищем её историю
            for task in current_features["tasks"]:
                history = await self._find_entity_history(
                    task, "task", related_meeting_ids
                )
                if history and len(history.get("mentions", [])) > 1:
                    evolution.append(history)

            # Для каждого проекта
            for project in current_features["projects"]:
                history = await self._find_entity_history(
                    project, "project", related_meeting_ids
                )
                if history and len(history.get("mentions", [])) > 1:
                    evolution.append(history)

        except Exception as e:
            logger.error(f"Ошибка отслеживания эволюции: {e}")

        return evolution

    async def _find_entity_history(
        self,
        entity_name: str,
        entity_type: str,
        meeting_ids: List[str]
    ) -> Optional[Dict[str, Any]]:
        """Находит историю упоминаний сущности"""
        if not self.graph:
            return None

        try:
            mentions = []

            # Ищем узлы с похожим именем
            nodes = await self.graph.get_all_nodes_async()
            for node in nodes:
                node_name = (
                    node.get("name") or
                    node.get("title") or
                    node.get("description", "")
                ).lower()

                if entity_name.lower() in node_name:
                    meeting_id = node.get("meeting_id") or node.get("source_meeting")
                    if meeting_id:
                        mentions.append({
                            "meeting_id": meeting_id,
                            "status": node.get("status"),
                            "value": node.get("value") or node.get("progress"),
                            "date": node.get("created_at") or node.get("date")
                        })

            if mentions:
                return {
                    "entity_name": entity_name,
                    "entity_type": entity_type,
                    "mentions": mentions,
                    "total_mentions": len(mentions)
                }

        except Exception as e:
            logger.error(f"Ошибка поиска истории сущности: {e}")

        return None

    def _find_recurring_topics(
        self,
        current_features: Dict[str, Set[str]],
        links: List[MeetingLink]
    ) -> List[Dict[str, Any]]:
        """Находит повторяющиеся темы"""
        topic_counts = {}

        # Считаем упоминания тем в связанных встречах
        for topic in current_features["topics"]:
            count = 1  # Текущая встреча
            meetings = []

            for link in links:
                if topic in link.common_topics:
                    count += 1
                    meetings.append(link.target_meeting_id)

            if count > 1:
                topic_counts[topic] = {
                    "topic": topic,
                    "mention_count": count,
                    "meetings": meetings
                }

        # Сортируем по частоте
        recurring = sorted(
            topic_counts.values(),
            key=lambda x: x["mention_count"],
            reverse=True
        )

        return recurring[:10]

    async def _build_decision_chains(
        self,
        meeting_id: str,
        current_features: Dict[str, Set[str]],
        links: List[MeetingLink]
    ) -> List[Dict[str, Any]]:
        """Строит цепочки связанных решений"""
        chains = []

        try:
            if not self.graph:
                return chains

            # Для каждого проекта ищем связанные решения
            for project in current_features["projects"]:
                project_decisions = []

                # Ищем решения по проекту в графе
                nodes = await self.graph.get_all_nodes_async()
                for node in nodes:
                    if node.get("_label") == "Decision":
                        node_project = (node.get("project") or "").lower()
                        if project.lower() in node_project:
                            project_decisions.append({
                                "decision": node.get("text") or node.get("description"),
                                "meeting_id": node.get("meeting_id"),
                                "date": node.get("created_at"),
                                "status": node.get("status")
                            })

                if len(project_decisions) > 1:
                    chains.append({
                        "project": project,
                        "decisions_count": len(project_decisions),
                        "decisions": project_decisions[:5]  # Топ 5
                    })

        except Exception as e:
            logger.error(f"Ошибка построения цепочек решений: {e}")

        return chains

    async def _create_graph_links(
        self,
        meeting_id: str,
        links: List[MeetingLink]
    ):
        """Создаёт связи в графе"""
        if not self.graph:
            return

        try:
            source_node_id = await self._resolve_meeting_node_id(meeting_id) or meeting_id
            for link in links:
                target_node_id = await self._resolve_meeting_node_id(link.target_meeting_id) or link.target_meeting_id
                # Создаём узел связи
                link_node_id = f"meeting_link:{meeting_id}:{link.target_meeting_id}"

                await self.graph.create_node(
                    node_id=link_node_id,
                    label="MeetingLink",
                    properties={
                        "source_meeting": meeting_id,
                        "target_meeting": link.target_meeting_id,
                        "relevance_score": link.relevance_score,
                        "link_type": link.link_type,
                        "common_participants": link.common_participants,
                        "common_projects": link.common_projects,
                        "common_topics": link.common_topics,
                        "temporal_distance_days": link.temporal_distance_days,
                        "created_at": datetime.utcnow().isoformat()
                    }
                )

                # Создаём связи с встречами
                await self.graph._create_relationship(
                    from_id=source_node_id,
                    to_id=link_node_id,
                    rel_type="HAS_LINK",
                    properties={"direction": "outgoing"}
                )

                await self.graph._create_relationship(
                    from_id=link_node_id,
                    to_id=target_node_id,
                    rel_type="LINKS_TO",
                    properties={"relevance": link.relevance_score}
                )

            logger.info(f"  📊 Создано {len(links)} связей в графе")

        except Exception as e:
            logger.error(f"Ошибка создания связей в графе: {e}")

    def _link_to_dict(self, link: MeetingLink) -> Dict[str, Any]:
        """Конвертирует MeetingLink в dict"""
        return {
            "source_meeting_id": link.source_meeting_id,
            "target_meeting_id": link.target_meeting_id,
            "relevance_score": link.relevance_score,
            "link_type": link.link_type,
            "common_participants": link.common_participants,
            "common_projects": link.common_projects,
            "common_topics": link.common_topics,
            "common_entities": link.common_entities,
            "temporal_distance_days": link.temporal_distance_days,
            "evolution_notes": link.evolution_notes
        }

    async def get_meeting_context(
        self,
        meeting_id: str,
        depth: int = 2
    ) -> Dict[str, Any]:
        """
        Получает расширенный контекст встречи с учётом связей

        Args:
            meeting_id: ID встречи
            depth: Глубина поиска связей (1 = прямые связи, 2 = связи связей)

        Returns:
            Dict с контекстом встречи и связанных встреч
        """
        try:
            context = {
                "meeting_id": meeting_id,
                "direct_links": [],
                "indirect_links": [],
                "timeline": [],
                "key_participants": [],
                "key_projects": []
            }

            if not self.graph:
                return context

            # Получаем прямые связи
            nodes = await self.graph.get_all_nodes_async()
            for node in nodes:
                if node.get("_label") == "MeetingLink":
                    if node.get("source_meeting") == meeting_id:
                        context["direct_links"].append({
                            "target": node.get("target_meeting"),
                            "relevance": node.get("relevance_score"),
                            "type": node.get("link_type"),
                            "common_participants": node.get("common_participants", []),
                            "common_projects": node.get("common_projects", [])
                        })

            # Сортируем по релевантности
            context["direct_links"].sort(
                key=lambda x: x.get("relevance", 0),
                reverse=True
            )

            # Собираем ключевых участников и проекты
            participants = set()
            projects = set()
            for link in context["direct_links"]:
                participants.update(link.get("common_participants", []))
                projects.update(link.get("common_projects", []))

            context["key_participants"] = list(participants)[:10]
            context["key_projects"] = list(projects)[:10]

            return context

        except Exception as e:
            logger.error(f"Ошибка получения контекста встречи: {e}")
            return {"meeting_id": meeting_id, "error": str(e)}


