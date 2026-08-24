# -*- coding: utf-8 -*-
"""
API routes for Cross-Meeting Analysis

Endpoints для анализа связей между встречами
"""
import logging
from typing import List, Optional

from litestar import Controller, Router, get, post
from pydantic import BaseModel

from backend.core.capture.agents.cross_meeting_analyzer import CrossMeetingAnalyzer
from backend.core.llm.router import LLMRouter
from backend.core.store.graph_builder import GraphBuilder

logger = logging.getLogger(__name__)


# === Pydantic Models ===

class MeetingLinkResponse(BaseModel):
    """Связь между встречами"""
    source_meeting_id: str
    target_meeting_id: str
    relevance_score: float
    link_type: str
    common_participants: List[str]
    common_projects: List[str]
    common_topics: List[str]
    temporal_distance_days: int


class AnalyzeMeetingRequest(BaseModel):
    """Запрос на анализ встречи"""
    meeting_id: str
    participants: Optional[List[str]] = []
    projects: Optional[List[str]] = []
    tasks: Optional[List[dict]] = []
    decisions: Optional[List[dict]] = []
    entities: Optional[List[str]] = []
    date: Optional[str] = None
    max_links: Optional[int] = 10


class AnalyzeMeetingResponse(BaseModel):
    """Ответ с результатами анализа"""
    meeting_id: str
    links: List[dict]
    entity_evolution: List[dict]
    recurring_topics: List[dict]
    decision_chains: List[dict]
    stats: dict


class MeetingContextResponse(BaseModel):
    """Контекст встречи с учётом связей"""
    meeting_id: str
    direct_links: List[dict]
    indirect_links: List[dict]
    key_participants: List[str]
    key_projects: List[str]


# === Dependencies ===

async def get_cross_meeting_analyzer() -> CrossMeetingAnalyzer:
    """Dependency для CrossMeetingAnalyzer"""
    graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
    await graph.connect()

    llm = LLMRouter()

    return CrossMeetingAnalyzer(graph_builder=graph, llm_router=llm)


# === Controller ===

class CrossMeetingController(Controller):
    """Controller для Cross-Meeting Analysis API"""

    path = "/cross-meeting"
    tags = ["Cross-Meeting Analysis"]

    @post("/analyze")
    async def analyze_meeting_links(
        self,
        data: AnalyzeMeetingRequest,
    ) -> AnalyzeMeetingResponse:
        """
        Анализирует связи встречи с другими встречами

        Находит:
        - Связанные встречи по участникам, проектам, темам
        - Эволюцию сущностей (задачи, решения)
        - Повторяющиеся темы
        - Цепочки решений
        """
        logger.info(f"🔗 Анализ связей для встречи {data.meeting_id}")

        analyzer = await get_cross_meeting_analyzer()

        # Подготавливаем данные встречи
        meeting_data = {
            "participants": [{"name": p} for p in data.participants] if data.participants else [],
            "projects": [{"name": p} for p in data.projects] if data.projects else [],
            "tasks": data.tasks or [],
            "decisions": data.decisions or [],
            "entities": [{"name": e} for e in data.entities] if data.entities else [],
            "date": data.date
        }

        result = await analyzer.analyze_meeting_links(
            meeting_id=data.meeting_id,
            meeting_data=meeting_data,
            max_links=data.max_links or 10
        )

        return AnalyzeMeetingResponse(
            meeting_id=result.get("meeting_id", data.meeting_id),
            links=result.get("links", []),
            entity_evolution=result.get("entity_evolution", []),
            recurring_topics=result.get("recurring_topics", []),
            decision_chains=result.get("decision_chains", []),
            stats=result.get("stats", {})
        )

    @get("/context/{meeting_id:str}")
    async def get_meeting_context(
        self,
        meeting_id: str,
        depth: int = 2
    ) -> MeetingContextResponse:
        """
        Получает расширенный контекст встречи

        Включает:
        - Прямые связи с другими встречами
        - Косвенные связи (связи связей)
        - Ключевых участников
        - Ключевые проекты
        """
        logger.info(f"📋 Получение контекста встречи {meeting_id}")

        analyzer = await get_cross_meeting_analyzer()

        context = await analyzer.get_meeting_context(
            meeting_id=meeting_id,
            depth=depth
        )

        return MeetingContextResponse(
            meeting_id=context.get("meeting_id", meeting_id),
            direct_links=context.get("direct_links", []),
            indirect_links=context.get("indirect_links", []),
            key_participants=context.get("key_participants", []),
            key_projects=context.get("key_projects", [])
        )

    @get("/links/{meeting_id:str}")
    async def get_meeting_links(
        self,
        meeting_id: str,
        limit: int = 10
    ) -> List[MeetingLinkResponse]:
        """
        Получает связи встречи из графа
        """
        logger.info(f"🔗 Получение связей встречи {meeting_id}")

        graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
        await graph.connect()

        links = []
        nodes = graph.get_all_nodes()

        for node in nodes:
            if node.get("_label") == "MeetingLink":
                if node.get("source_meeting") == meeting_id:
                    links.append(MeetingLinkResponse(
                        source_meeting_id=node.get("source_meeting", ""),
                        target_meeting_id=node.get("target_meeting", ""),
                        relevance_score=node.get("relevance_score", 0),
                        link_type=node.get("link_type", "reference"),
                        common_participants=node.get("common_participants", []),
                        common_projects=node.get("common_projects", []),
                        common_topics=node.get("common_topics", []),
                        temporal_distance_days=node.get("temporal_distance_days", 0)
                    ))

        # Сортируем по релевантности
        links.sort(key=lambda x: x.relevance_score, reverse=True)

        return links[:limit]

    @get("/timeline/{project:str}")
    async def get_project_timeline(
        self,
        project: str,
        limit: int = 20
    ) -> dict:
        """
        Получает временную линию проекта через встречи

        Показывает:
        - Все встречи где упоминался проект
        - Ключевые решения по проекту
        - Эволюцию задач
        """
        logger.info(f"📅 Получение timeline проекта {project}")

        graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
        await graph.connect()

        timeline = {
            "project": project,
            "meetings": [],
            "decisions": [],
            "tasks": [],
            "kpis": []
        }

        nodes = graph.get_all_nodes()
        project_lower = project.lower()

        for node in nodes:
            label = node.get("_label", "")

            # Встречи с проектом
            if label == "Meeting":
                meeting_project = (node.get("project") or "").lower()
                if project_lower in meeting_project:
                    timeline["meetings"].append({
                        "id": node.get("id"),
                        "title": node.get("title"),
                        "date": node.get("created_at") or node.get("date")
                    })

            # Решения по проекту
            elif label == "Decision":
                dec_project = (node.get("project") or "").lower()
                if project_lower in dec_project:
                    timeline["decisions"].append({
                        "id": node.get("id"),
                        "text": node.get("text") or node.get("description"),
                        "meeting_id": node.get("meeting_id"),
                        "date": node.get("created_at")
                    })

            # Задачи по проекту
            elif label == "Task":
                task_project = (node.get("project") or "").lower()
                if project_lower in task_project:
                    timeline["tasks"].append({
                        "id": node.get("id"),
                        "title": node.get("title") or node.get("description"),
                        "status": node.get("status"),
                        "meeting_id": node.get("meeting_id"),
                        "date": node.get("created_at")
                    })

            # KPI по проекту
            elif label == "KPI":
                kpi_project = (node.get("project") or "").lower()
                if project_lower in kpi_project:
                    timeline["kpis"].append({
                        "id": node.get("id"),
                        "name": node.get("name") or node.get("metric"),
                        "value": node.get("value"),
                        "meeting_id": node.get("meeting_id"),
                        "date": node.get("created_at")
                    })

        # Сортируем по дате
        for key in ["meetings", "decisions", "tasks", "kpis"]:
            timeline[key] = sorted(
                timeline[key],
                key=lambda x: x.get("date") or "",
                reverse=True
            )[:limit]

        timeline["stats"] = {
            "total_meetings": len(timeline["meetings"]),
            "total_decisions": len(timeline["decisions"]),
            "total_tasks": len(timeline["tasks"]),
            "total_kpis": len(timeline["kpis"])
        }

        return timeline

    @get("/participant/{participant:str}/history")
    async def get_participant_history(
        self,
        participant: str,
        limit: int = 20
    ) -> dict:
        """
        Получает историю участия человека во встречах

        Показывает:
        - Все встречи с участником
        - Назначенные задачи
        - Принятые решения
        """
        logger.info(f"👤 Получение истории участника {participant}")

        graph = GraphBuilder(graph_storage_path="data/tessent_brain_graph.json")
        await graph.connect()

        history = {
            "participant": participant,
            "meetings": [],
            "tasks_assigned": [],
            "decisions_involved": []
        }

        nodes = graph.get_all_nodes()
        participant_lower = participant.lower()

        for node in nodes:
            label = node.get("_label", "")

            # Встречи с участником
            if label == "Person":
                name = (node.get("name") or "").lower()
                if participant_lower in name:
                    # Находим связанные встречи
                    meeting_id = node.get("meeting_id") or node.get("source_meeting")
                    if meeting_id:
                        history["meetings"].append({
                            "meeting_id": meeting_id,
                            "role": node.get("role"),
                            "date": node.get("created_at")
                        })

            # Задачи назначенные участнику
            elif label == "Task":
                assignee = (node.get("assignee") or node.get("responsible") or "").lower()
                if participant_lower in assignee:
                    history["tasks_assigned"].append({
                        "id": node.get("id"),
                        "title": node.get("title") or node.get("description"),
                        "status": node.get("status"),
                        "deadline": node.get("deadline"),
                        "meeting_id": node.get("meeting_id")
                    })

            # Решения связанные с участником
            elif label == "Decision":
                responsible = (node.get("responsible") or node.get("owner") or "").lower()
                if participant_lower in responsible:
                    history["decisions_involved"].append({
                        "id": node.get("id"),
                        "text": node.get("text") or node.get("description"),
                        "meeting_id": node.get("meeting_id"),
                        "date": node.get("created_at")
                    })

        # Сортируем по дате
        history["meetings"] = sorted(
            history["meetings"],
            key=lambda x: x.get("date") or "",
            reverse=True
        )[:limit]

        history["stats"] = {
            "total_meetings": len(history["meetings"]),
            "total_tasks": len(history["tasks_assigned"]),
            "total_decisions": len(history["decisions_involved"])
        }

        return history


# Router
router = Router(
    path="",
    route_handlers=[CrossMeetingController]
)


