# -*- coding: utf-8 -*-
"""
Tessbrain Tools - инструменты для интеграции с AutoGen/AG2.

Предоставляет async функции, которые можно зарегистрировать
в AutoGen агентах для доступа к базе знаний Tessbrain.

Пример использования с AutoGen:
```python
from backend.integrations.tessent_brain_tools import TessentBrainTools

tools = TessentBrainTools(graph_builder, reasoning_engine)

# Регистрация в AutoGen агенте
register_function(
    tools.get_project_status,
    caller=context_analyzer,
    executor=context_analyzer,
    name="get_project_status",
    description="Получить статус проекта из базы знаний Tessbrain"
)
```
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

# Import Supabase client for direct database access
from backend.db.supabase_client import get_supabase_client, set_user_context

logger = logging.getLogger(__name__)

# Add MeetFlow path for importing automation tools
from backend.core.utils.meetflow_path import default_meetflow_path as _dmp

MEETFLOW_PATH = _dmp()
if MEETFLOW_PATH not in sys.path:
    sys.path.insert(0, MEETFLOW_PATH)


def build_enhanced_search(graph_builder=None, vector_indexer=None,
                          llm_router=None, user_id=None):
    """Собрать EnhancedSearchOrchestrator из УЖЕ имеющихся у агента движков.

    Зачем: search_knowledge умеет Enhanced (vector + BM25 + graph + RRF и все
    усиления гибридного поиска), но во всех местах создания TessentBrainTools
    аргумент enhanced_search не передавался — предпочтительный путь лежал
    мёртвым, и агентные автоматизации всегда падали в legacy ReasoningEngine
    (аудит: docs/SEARCH_CONSUMERS_AUDIT.md).

    Собирается из тех же graph_builder/vector_indexer, которыми агент и так
    пользуется, — тенантность не меняется. Never-raise: чего-то нет или сбой
    → None, и search_knowledge честно уходит в прежний legacy-фолбэк.

    Выключатель: TESSENT_AUTO_ENHANCED=off возвращает прежнее поведение.
    """
    import os
    if (os.getenv("TESSENT_AUTO_ENHANCED", "") or "").strip().lower() in (
            "off", "0", "false", "no"):
        return None
    if graph_builder is None or vector_indexer is None:
        return None
    try:
        from backend.core.search.enhanced_search_orchestrator import (
            EnhancedSearchOrchestrator,
        )
        from backend.core.search.hybrid_search_orchestrator import (
            get_hybrid_search_orchestrator,
        )
        hybrid = get_hybrid_search_orchestrator(
            vector_indexer=vector_indexer,
            graph_builder=graph_builder,
            user_id=user_id,
        )
        if llm_router is None:
            from backend.core.llm.router import LLMRouter
            llm_router = LLMRouter()
        return EnhancedSearchOrchestrator(
            hybrid_search=hybrid, llm_router=llm_router)
    except Exception as exc:
        logger.warning("build_enhanced_search: не собрался, legacy-фолбэк: %s", exc)
        return None


class TessentBrainTools:
    """
    Набор инструментов Tessbrain для AutoGen агентов.

    Все методы возвращают JSON-строки для совместимости с AutoGen.

    Архитектура:
    - Граф знаний (NetworkX) - локальные данные из встреч
    - Supabase - прямой доступ к задачам, профилям, встречам
    - Векторный поиск - семантический поиск по базе знаний
    """

    def __init__(
        self,
        graph_builder=None,
        reasoning_engine=None,
        vector_indexer=None,
        user_id: str | None = None,
        enhanced_search=None
    ):
        """
        Args:
            graph_builder: GraphBuilder instance
            reasoning_engine: ReasoningEngine instance
            vector_indexer: VectorIndexer instance
            user_id: User ID for Supabase queries (RLS)
            enhanced_search: EnhancedSearchOrchestrator instance (preferred over reasoning_engine)
        """
        self.graph = graph_builder
        self.reasoning = reasoning_engine
        self.vectors = vector_indexer
        self.user_id = user_id
        self.enhanced_search = enhanced_search

        # Lazy import traverser
        self._traverser = None

        # Lazy import TaskSpecGenerator
        self._spec_generator = None

        # Supabase client for direct DB access
        self._supabase = None

    async def _ensure_graph(self):
        """Ленивая загрузка TENANT-графа по user_id.

        app.state.graph намеренно None (мультитенантность: глобальный граф
        смешивал бы данные юзеров), но из-за этого граф-инструменты Auto
        (get_person_info, get_all_people, решения, знания) были ВЕЧНО
        пустыми (аудит: «треть каталога — пустые ответы»). Берём merged-граф
        конкретного пользователя при первом обращении."""
        if self.graph is not None or not self.user_id:
            return self.graph
        try:
            from backend.core.store.graph_view import merged_graph_view_for_user
            self.graph = await merged_graph_view_for_user(self.user_id, use_networkx=None)
            self._traverser = None  # пересоздать поверх живого графа
            logger.info(f"🕸️ TessentBrainTools: lazy graph loaded for {self.user_id}")
        except Exception:
            logger.debug("lazy tenant graph load failed", exc_info=True)
        return self.graph

    def _get_traverser(self):
        """Lazy load GraphTraverser"""
        if self._traverser is None:
            from backend.core.think.graph_traverser import GraphTraverser
            self._traverser = GraphTraverser(self.graph, self.vectors)
        return self._traverser

    def _get_spec_generator(self):
        """Lazy load TaskSpecGenerator"""
        if self._spec_generator is None and self.reasoning and self.reasoning.llm:
            from backend.core.think.task_spec_generator import TaskSpecGenerator
            self._spec_generator = TaskSpecGenerator(self.reasoning, self.reasoning.llm)
        return self._spec_generator

    def _get_supabase(self):
        """Lazy load Supabase client"""
        if self._supabase is None:
            self._supabase = get_supabase_client()
            if self.user_id:
                set_user_context(user_id=self.user_id)
        return self._supabase

    def _setup_meetflow_context(self):
        """Setup MeetFlow user context for API calls"""
        try:
            import automation_tools_async as meetflow_tools
            # Set user context - use provided user_id or default
            user_id = self.user_id or os.getenv("USER_ID", "")
            if user_id:
                meetflow_tools.set_user_context(user_id=user_id)
                logger.info(f"MeetFlow context set for user: {user_id}")
            else:
                logger.warning("No user_id available for MeetFlow context")
            return meetflow_tools
        except ImportError as e:
            logger.warning(f"MeetFlow tools not available: {e}")
            return None

    # === Основные инструменты для агентов ===

    async def get_project_status(self, project_name: str) -> str:
        """
        Получить статус проекта из базы знаний.

        Args:
            project_name: Название проекта

        Returns:
            JSON с информацией о проекте
        """
        try:
            if self.reasoning:
                result = await self.reasoning.reason(
                    f"Какой статус проекта {project_name}?"
                )

                return json.dumps({
                    "project": project_name,
                    "status": "found" if result.data else "not_found",
                    "summary": result.answer,
                    "tasks_count": len(result.data.get("tasks", [])) if result.data else 0,
                    "decisions_count": len(result.data.get("decisions", [])) if result.data else 0,
                    "participants": result.data.get("participants", []) if result.data else [],
                    "confidence": result.confidence,
                    "sources": result.sources
                }, ensure_ascii=False)

            return json.dumps({
                "error": "Reasoning engine not available",
                "project": project_name
            })

        except Exception as e:
            logger.error(f"Error getting project status: {e}")
            return json.dumps({"error": str(e), "project": project_name})

    async def get_person_info(self, person_name: str) -> str:
        """
        Получить информацию о человеке.

        Args:
            person_name: Имя человека

        Returns:
            JSON с информацией о человеке
        """
        try:
            await self._ensure_graph()
            traverser = self._get_traverser()

            # Используем специализированный метод
            context = await traverser.find_person_with_context(person_name)

            if context.get("person"):
                person = context["person"]
                return json.dumps({
                    "person": person_name,
                    "status": "found",
                    "name": person.get("name", ""),
                    "role": person.get("role", ""),
                    "tasks": [
                        {
                            "title": t.get("title", ""),
                            "status": t.get("status", "")
                        }
                        for t in context.get("tasks", [])[:10]
                    ],
                    "meetings_count": len(context.get("meetings", [])),
                    "decisions_count": len(context.get("decisions", [])),
                    "entities": [
                        e.get("name", "")
                        for e in context.get("entities", [])[:5]
                    ]
                }, ensure_ascii=False)

            return json.dumps({
                "person": person_name,
                "status": "not_found",
                "message": f"Человек '{person_name}' не найден в базе знаний"
            })

        except Exception as e:
            logger.error(f"Error getting person info: {e}")
            return json.dumps({"error": str(e), "person": person_name})

    async def get_all_tasks(
        self,
        status_filter: str = "",
        assignee_filter: str = "",
        limit: int = 50
    ) -> str:
        """
        Получить список задач из MeetFlow (meeting_tasks) и графа знаний.

        Args:
            status_filter: Фильтр по статусу (todo, in_progress, done, review)
            assignee_filter: Фильтр по исполнителю
            limit: Максимум задач

        Returns:
            JSON со списком задач
        """
        try:
            tasks = []
            by_status = {}
            sources_checked = []

            # 1. Get tasks from MeetFlow Supabase (meeting_tasks table)
            try:
                import httpx
                supabase_url = os.getenv("SUPABASE_URL", "https://vvowgzcnmzebyjwbiank.supabase.co")
                supabase_key = os.getenv("SUPABASE_KEY", "")

                if supabase_url and supabase_key:
                    headers = {
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json"
                    }

                    # Build query params
                    params = {
                        "select": "id,title,description,assignee,due_date,status,priority,created_at,meeting_id",
                        "order": "created_at.desc",
                        "limit": str(limit)
                    }

                    # Add status filter if provided
                    if status_filter:
                        params["status"] = f"eq.{status_filter}"

                    # Add assignee filter if provided
                    if assignee_filter:
                        params["assignee"] = f"ilike.*{assignee_filter}*"

                    async with httpx.AsyncClient(timeout=15.0) as client:
                        url = f"{supabase_url.rstrip('/')}/rest/v1/meeting_tasks"
                        response = await client.get(url, headers=headers, params=params)

                        if response.status_code == 200:
                            db_tasks = response.json()
                            sources_checked.append("meeting_tasks")

                            for t in db_tasks:
                                status = t.get("status", "todo")
                                tasks.append({
                                    "id": str(t.get("id", "")),
                                    "title": t.get("title", ""),
                                    "description": (t.get("description", "") or "")[:200],
                                    "status": status,
                                    "priority": t.get("priority", "medium"),
                                    "assignee": t.get("assignee", ""),
                                    "due_date": t.get("due_date", ""),
                                    "created_at": t.get("created_at", ""),
                                    "meeting_id": t.get("meeting_id", ""),
                                    "source": "meeting_tasks"
                                })
                                by_status[status] = by_status.get(status, 0) + 1

                            logger.info(f"Found {len(db_tasks)} tasks from meeting_tasks")
                        else:
                            logger.warning(f"meeting_tasks query returned {response.status_code}: {response.text[:200]}")

            except Exception as db_err:
                logger.warning(f"MeetFlow meeting_tasks query failed: {db_err}")

            # 2. Try external task systems (YouGile, Trello, Jira) if configured
            try:
                import sys

                from backend.core.utils.meetflow_path import default_meetflow_path as _dmp2
                meetflow_path = _dmp2()
                if meetflow_path not in sys.path:
                    sys.path.insert(0, meetflow_path)

                import automation_tools_async as meetflow

                # Check which systems are configured
                systems_json = await meetflow.get_configured_task_systems()
                systems_data = json.loads(systems_json)
                configured_systems = systems_data.get("systems", [])

                logger.info(f"Configured external task systems: {configured_systems}")

                # Try each configured system
                for system_info in configured_systems:
                    system = system_info.get("system") if isinstance(system_info, dict) else system_info
                    try:
                        boards_json = await meetflow.list_task_boards(system)
                        boards_data = json.loads(boards_json)

                        if "error" in boards_data:
                            continue

                        boards = boards_data.get("boards", [])
                        sources_checked.append(system)

                        for board in boards[:3]:
                            board_id = board.get("id", "")
                            if not board_id:
                                continue

                            tasks_json = await meetflow.list_tasks(
                                system=system,
                                board_id=board_id,
                                status=status_filter,
                                limit=max(10, limit // max(1, len(boards)))
                            )
                            tasks_data = json.loads(tasks_json)

                            if "error" not in tasks_data:
                                for t in tasks_data.get("tasks", []):
                                    status = t.get("status", "todo")
                                    if not any(existing.get("title") == t.get("title") for existing in tasks):
                                        tasks.append({
                                            "id": str(t.get("id", "")),
                                            "title": t.get("title", t.get("name", "")),
                                            "description": (t.get("description", "") or "")[:200],
                                            "status": status,
                                            "priority": t.get("priority", "medium"),
                                            "assignee": t.get("assignee", ""),
                                            "due_date": t.get("due_date", t.get("deadline", "")),
                                            "board": board.get("name", board_id),
                                            "source": system
                                        })
                                        by_status[status] = by_status.get(status, 0) + 1

                    except Exception as sys_err:
                        logger.warning(f"Error getting tasks from {system}: {sys_err}")

            except Exception as ext_err:
                logger.warning(f"External task systems query failed: {ext_err}")

            # 3. Also check graph for additional tasks
            try:
                await self._ensure_graph()
                traverser = self._get_traverser()
                graph_summary = await traverser.get_all_tasks_summary(limit=limit)
                graph_tasks = graph_summary.get("tasks", [])

                for t in graph_tasks:
                    if not any(existing.get("title") == t.get("title") for existing in tasks):
                        status = t.get("status", "new")
                        tasks.append({**t, "source": "graph"})
                        by_status[status] = by_status.get(status, 0) + 1

                if graph_tasks:
                    sources_checked.append("graph")
                logger.info(f"Found {len(graph_tasks)} tasks from graph")
            except Exception as graph_err:
                logger.warning(f"Graph tasks query failed: {graph_err}")

            return json.dumps({
                "total": len(tasks),
                "by_status": by_status,
                "tasks": tasks[:limit],
                "sources_checked": sources_checked,
                "filters_applied": {
                    "status": status_filter or "all",
                    "assignee": assignee_filter or "all"
                }
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error getting tasks: {e}")
            return json.dumps({"error": str(e)})

    async def get_all_people(self, limit: int = 50) -> str:
        """
        Получить список всех людей в системе.

        Args:
            limit: Максимум результатов

        Returns:
            JSON со списком людей
        """
        try:
            await self._ensure_graph()
            traverser = self._get_traverser()
            summary = await traverser.get_all_people_summary(limit=limit)

            return json.dumps({
                "total": summary.get("total", 0),
                "people": summary.get("people", [])
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error getting people: {e}")
            return json.dumps({"error": str(e)})

    async def get_recent_meetings(self, limit: int = 10) -> str:
        """
        Получить последние встречи.

        Args:
            limit: Максимум встреч

        Returns:
            JSON со списком встреч
        """
        try:
            # Prefer real MeetFlow DB (Supabase) over knowledge graph (graph can be disconnected)
            meetings: List[Dict[str, Any]] = []
            try:
                import httpx
                supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
                supabase_key = os.getenv("SUPABASE_KEY", "")

                if supabase_url and supabase_key:
                    headers = {
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                    }
                    params = {
                        # Keep it light and stable (avoid columns that might not exist like "date")
                        "select": "id,title,created_at",
                        "order": "created_at.desc",
                        "limit": str(limit),
                    }
                    # IMPORTANT: scope to the current user to avoid leaking other accounts' meetings
                    # meetings table uses user_id (UUID). If user_id is not provided or invalid, fallback will occur.
                    if self.user_id:
                        params["user_id"] = f"eq.{self.user_id}"

                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(
                            f"{supabase_url}/rest/v1/meetings",
                            headers=headers,
                            params=params,
                        )
                        resp.raise_for_status()
                        meetings = resp.json() or []
            except Exception as db_err:
                logger.warning(f"MeetFlow meetings query failed (will try graph): {db_err}")

            # Fallback: try knowledge graph (may be empty if graph is not connected)
            if not meetings:
                try:
                    await self._ensure_graph()
                    traverser = self._get_traverser()
                    meetings = await traverser.find_nodes("Meeting", limit=limit)
                except Exception as graph_err:
                    logger.warning(f"Graph meetings query failed: {graph_err}")
                    meetings = []

            return json.dumps(
                {
                    "total": len(meetings),
                    "meetings": [
                        {
                            "id": m.get("id", ""),
                            "title": m.get("title", ""),
                            "created_at": m.get("created_at", m.get("date", "")),
                            "updated_at": m.get("updated_at", ""),
                        }
                        for m in meetings
                    ],
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"Error getting meetings: {e}")
            return json.dumps({"error": str(e)})

    async def get_recent_decisions(self, limit: int = 20) -> str:
        """
        Получить последние решения.

        Args:
            limit: Максимум решений

        Returns:
            JSON со списком решений
        """
        try:
            await self._ensure_graph()
            traverser = self._get_traverser()
            decisions = await traverser.find_nodes("Decision", limit=limit)

            return json.dumps({
                "total": len(decisions),
                "decisions": [
                    {
                        "id": d.get("id", ""),
                        "summary": d.get("summary", d.get("decision_summary", "")),
                        "category": d.get("category", ""),
                        "urgency": d.get("urgency", ""),
                        "date": d.get("date", d.get("created_at", ""))
                    }
                    for d in decisions
                ]
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error getting decisions: {e}")
            return json.dumps({"error": str(e)})

    async def search_knowledge(self, query: str) -> str:
        """
        Семантический поиск по базе знаний.
        Использует Enhanced Search (если доступен) для более глубокого анализа.

        Args:
            query: Поисковый запрос

        Returns:
            JSON с результатами поиска
        """
        try:
            # Предпочитаем Enhanced Search — он включает vector + BM25 + graph + consulting
            if self.enhanced_search:
                try:
                    result = await self.enhanced_search.search(
                        query=query,
                        search_depth="standard"
                    )

                    # SearchResult dataclass: answer, sources, confidence, reasoning_steps, metadata, processing_time_ms
                    answer = getattr(result, 'answer', str(result))
                    confidence = getattr(result, 'confidence', 0.8)
                    sources = getattr(result, 'sources', [])
                    steps = getattr(result, 'reasoning_steps', [])

                    return json.dumps({
                        "query": query,
                        "answer": answer,
                        "confidence": confidence,
                        "sources": sources[:15] if sources else [],
                        "reasoning_steps": steps,
                        "search_engine": "enhanced"
                    }, ensure_ascii=False, default=str)
                except Exception as e:
                    logger.warning(f"Enhanced search failed in search_knowledge, falling back to legacy: {e}")

            # Fallback: Legacy ReasoningEngine
            if self.reasoning:
                result = await self.reasoning.reason(query)

                # ReasoningResult имеет: answer, data, reasoning_chain, sources, anomalies, confidence
                reasoning_steps = [
                    step.get("description", str(step))
                    for step in result.reasoning_chain
                ] if result.reasoning_chain else []

                return json.dumps({
                    "query": query,
                    "answer": result.answer,
                    "confidence": result.confidence,
                    "sources": result.sources or [],
                    "reasoning_steps": reasoning_steps,
                    "data": result.data,
                    "search_engine": "legacy"
                }, ensure_ascii=False, default=str)

            return json.dumps({
                "error": "No search engine available",
                "query": query
            })

        except Exception as e:
            logger.error(f"Error searching knowledge: {e}")
            return json.dumps({"error": str(e), "query": query})

    async def get_anomalies_and_risks(self) -> str:
        """
        Получить аномалии и риски.

        Returns:
            JSON с аномалиями
        """
        try:
            await self._ensure_graph()
            if not self.graph or not self.graph.connected:
                return json.dumps({"error": "Graph not connected"})

            from backend.core.sleep.anomaly_detector import AnomalyDetector
            detector = AnomalyDetector(self.graph)

            anomalies = await detector.detect_all()

            # Подсчитываем общее количество
            total = 0
            summary = {}

            for key, value in anomalies.items():
                if isinstance(value, list):
                    count = len(value)
                    total += count
                    summary[key] = count

            return json.dumps({
                "total_anomalies": total,
                "summary": summary,
                "details": anomalies
            }, ensure_ascii=False, default=str)

        except Exception as e:
            logger.error(f"Error getting anomalies: {e}")
            return json.dumps({"error": str(e)})

    async def get_meeting_context(self, meeting_id: str = "", title: str = "") -> str:
        """
        Получить полный контекст встречи.

        Args:
            meeting_id: ID встречи
            title: Название встречи (альтернатива ID)

        Returns:
            JSON с контекстом встречи
        """
        try:
            await self._ensure_graph()
            traverser = self._get_traverser()
            context = await traverser.find_meeting_with_context(
                meeting_id=meeting_id if meeting_id else None,
                title=title if title else None
            )

            if context.get("meeting"):
                meeting = context["meeting"]
                return json.dumps({
                    "status": "found",
                    "meeting": {
                        "id": meeting.get("id", ""),
                        "title": meeting.get("title", ""),
                        "date": meeting.get("date", meeting.get("created_at", ""))
                    },
                    "participants": [
                        {"name": p.get("name", ""), "role": p.get("role", "")}
                        for p in context.get("participants", [])
                    ],
                    "tasks": [
                        {"title": t.get("title", ""), "status": t.get("status", "")}
                        for t in context.get("tasks", [])
                    ],
                    "decisions": [
                        {"summary": d.get("summary", d.get("decision_summary", ""))}
                        for d in context.get("decisions", [])
                    ],
                    "entities": [
                        e.get("name", "")
                        for e in context.get("entities", [])
                    ]
                }, ensure_ascii=False)

            return json.dumps({
                "status": "not_found",
                "message": "Meeting not found"
            })

        except Exception as e:
            logger.error(f"Error getting meeting context: {e}")
            return json.dumps({"error": str(e)})

    async def add_task_to_graph(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        deadline: str = "",
        priority: str = "normal",
        meeting_id: str = ""
    ) -> str:
        """
        Добавить задачу в граф знаний.

        Args:
            title: Название задачи
            description: Описание
            assignee: Исполнитель
            deadline: Дедлайн
            priority: Приоритет
            meeting_id: ID связанной встречи

        Returns:
            JSON с результатом
        """
        try:
            await self._ensure_graph()
            if not self.graph or not self.graph.connected:
                return json.dumps({"error": "Graph not connected"})

            # Генерация ТЗ, если описание короткое
            if len(description) < 50:
                spec_gen = self._get_spec_generator()
                if spec_gen:
                    try:
                        logger.info(f"Generating spec for task: {title}")
                        description = await spec_gen.generate_spec(title, description)
                    except Exception as spec_error:
                        logger.warning(f"Failed to generate spec: {spec_error}")

            task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{title[:20].replace(' ', '_')}"

            await self.graph.create_node(
                node_id=task_id,
                label="Task",
                properties={
                    "title": title,
                    "description": description,
                    "assignee": assignee,
                    "deadline": deadline,
                    "priority": priority,
                    "status": "new",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source": "autogen_tool"
                }
            )

            # Создаём связь с встречей если указана
            if meeting_id:
                await self.graph._create_relationship(
                    task_id, meeting_id, "CREATED_AT", {}
                )

            # Создаём связь с исполнителем если указан
            if assignee:
                # Ищем человека
                await self._ensure_graph()
                traverser = self._get_traverser()
                person = await traverser.find_node_by_name(assignee, "Person")
                if person:
                    await self.graph._create_relationship(
                        task_id, person.get("id"), "ASSIGNED_TO", {}
                    )

            return json.dumps({
                "success": True,
                "task_id": task_id,
                "message": f"Task '{title}' added to knowledge graph"
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error adding task: {e}")
            return json.dumps({"error": str(e)})

    async def add_decision_to_graph(
        self,
        summary: str,
        category: str = "",
        urgency: str = "normal",
        participants: List[str] | None = None,
        meeting_id: str = ""
    ) -> str:
        """
        Добавить решение в граф знаний.

        Args:
            summary: Краткое описание решения
            category: Категория
            urgency: Срочность
            participants: Участники принятия решения
            meeting_id: ID связанной встречи

        Returns:
            JSON с результатом
        """
        try:
            await self._ensure_graph()
            if not self.graph or not self.graph.connected:
                return json.dumps({"error": "Graph not connected"})

            decision_id = f"decision_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            await self.graph.create_node(
                node_id=decision_id,
                label="Decision",
                properties={
                    "summary": summary,
                    "decision_summary": summary,
                    "category": category,
                    "urgency": urgency,
                    "status": "pending",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source": "autogen_tool"
                }
            )

            # Связь с встречей
            if meeting_id:
                await self.graph._create_relationship(
                    decision_id, meeting_id, "MADE_AT", {}
                )

            # Связи с участниками
            if participants:
                await self._ensure_graph()
                traverser = self._get_traverser()
                for name in participants:
                    person = await traverser.find_node_by_name(name, "Person")
                    if person:
                        await self.graph._create_relationship(
                            person.get("id"), decision_id, "MADE_DECISION", {}
                        )

            return json.dumps({
                "success": True,
                "decision_id": decision_id,
                "message": "Decision added to knowledge graph"
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error adding decision: {e}")
            return json.dumps({"error": str(e)})

    async def send_telegram_message(
        self,
        message: str,
        chat_id: str = ""
    ) -> str:
        """
        Send a message to Telegram.

        Args:
            message: The message text to send
            chat_id: Telegram chat ID or @username (optional)

        Returns:
            JSON with result
        """
        try:
            # Try to use MeetFlow's send_telegram_message if available
            if self.meetflow_tools:
                try:
                    from alfa_asynk_meetflow.automation_tools_async import (
                        send_telegram_message as mf_send_telegram,
                    )
                    result = await mf_send_telegram(message=message, chat_id=chat_id if chat_id else None)
                    return result
                except ImportError:
                    logger.debug("suppressed exception", exc_info=True)

            # Fallback: direct Telegram API call
            import os
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            default_chat = os.getenv("TELEGRAM_CHAT_ID", "")

            if not bot_token:
                return json.dumps({
                    "error": "Telegram bot token not configured",
                    "hint": "Set TELEGRAM_BOT_TOKEN environment variable"
                }, ensure_ascii=False)

            target_chat = chat_id or default_chat
            if not target_chat:
                return json.dumps({
                    "error": "No chat_id provided and no default configured",
                    "hint": "Provide chat_id or set TELEGRAM_CHAT_ID environment variable"
                }, ensure_ascii=False)

            # parse_mode="Markdown" терял сообщения с непарной */_ (400) и не
            # рендерил **жирный**/таблицы. Общий хелпер: markdown → HTML,
            # фолбэк в чистый текст.
            from backend.core.notifications.telegram_format import (
                post_telegram_text,
            )
            res = await post_telegram_text(bot_token, target_chat, message)
            if res["ok"]:
                return json.dumps({
                    "success": True,
                    "message": "✅ Message sent to Telegram"
                }, ensure_ascii=False)
            return json.dumps({
                "error": "Telegram API error: message not delivered"
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return json.dumps({"error": str(e)})

    # === Supabase Direct Access Tools ===

    async def get_tasks_from_database(
        self,
        status: str = "",
        limit: int = 20
    ) -> str:
        """
        Получить задачи напрямую из базы данных Supabase.

        Args:
            status: Фильтр по статусу (backlog, in_progress, done)
            limit: Максимум задач

        Returns:
            JSON со списком задач
        """
        try:
            supabase = self._get_supabase()

            params = {
                "select": "id,title,description,status,priority,assignee,due_date,created_at",
                "order": "created_at.desc",
                "limit": str(limit)
            }
            if status:
                params["status"] = f"eq.{status}"

            tasks = await supabase._request("GET", "/rest/v1/tasks", params=params)

            if not tasks:
                return json.dumps({
                    "total": 0,
                    "tasks": [],
                    "message": "No tasks found in database"
                })

            return json.dumps({
                "total": len(tasks),
                "tasks": [
                    {
                        "id": str(t.get("id", "")),
                        "title": t.get("title", ""),
                        "description": (t.get("description", "") or "")[:300],
                        "status": t.get("status", ""),
                        "priority": t.get("priority", 0),
                        "assignee": t.get("assignee", ""),
                        "due_date": t.get("due_date", ""),
                        "created_at": t.get("created_at", "")
                    }
                    for t in tasks
                ]
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error getting tasks from DB: {e}")
            return json.dumps({"error": str(e)})

    async def get_user_profile(self, username: str = "") -> str:
        """
        Получить профиль пользователя с аналитикой производительности.

        Args:
            username: Имя пользователя (username из Telegram/системы)

        Returns:
            JSON с профилем пользователя
        """
        try:
            supabase = self._get_supabase()

            params = {
                "select": "*",
                "limit": "5"
            }
            if username:
                params["username"] = f"ilike.%{username}%"

            profiles = await supabase._request("GET", "/rest/v1/user_profiles", params=params)

            if not profiles:
                return json.dumps({
                    "found": False,
                    "message": f"User profile not found for: {username}"
                })

            profile = profiles[0]
            dossier = profile.get("employee_dossier_summary") or {}

            return json.dumps({
                "found": True,
                "user_id": profile.get("user_id", ""),
                "username": profile.get("username", ""),
                "analysis_summary": profile.get("analysis_summary", ""),
                "reliability_score": dossier.get("reliability_score", "N/A"),
                "skill_level": dossier.get("current_skill_level", "N/A"),
                "strengths": dossier.get("key_strengths_summary", ""),
                "weaknesses": dossier.get("key_weaknesses_summary", ""),
                "ai_recommendations": profile.get("ai_recommendations", []),
                "work_patterns": profile.get("work_patterns", {}),
                "performance_history": (profile.get("performance_history") or [])[:3],  # Last 3 weeks
                "last_analyzed": profile.get("last_analyzed_at", "")
            }, ensure_ascii=False, default=str)

        except Exception as e:
            logger.error(f"Error getting user profile: {e}")
            return json.dumps({"error": str(e)})

    async def get_team_overview(self) -> str:
        """
        Получить обзор команды: все участники, их статусы и задачи.

        Returns:
            JSON с обзором команды
        """
        try:
            supabase = self._get_supabase()

            # Get all user profiles
            params = {
                "select": "user_id,username,analysis_summary,employee_dossier_summary",
                "limit": "20"
            }
            profiles = await supabase._request("GET", "/rest/v1/user_profiles", params=params)

            # Get all tasks to count per user (Supabase REST doesn't support GROUP BY)
            task_params = {"select": "assignee,status", "limit": "500"}
            all_tasks = await supabase._request("GET", "/rest/v1/tasks", params=task_params)

            # Build task map manually
            task_map = {}
            for t in (all_tasks or []):
                assignee = str(t.get("assignee", ""))
                status = t.get("status", "unknown")
                if assignee not in task_map:
                    task_map[assignee] = {}
                task_map[assignee][status] = task_map[assignee].get(status, 0) + 1

            team_members = []
            for p in (profiles or []):
                user_id = str(p.get("user_id", ""))
                dossier = p.get("employee_dossier_summary") or {}

                team_members.append({
                    "user_id": user_id,
                    "username": p.get("username", ""),
                    "summary": (p.get("analysis_summary", "") or "")[:200],
                    "reliability_score": dossier.get("reliability_score", "N/A"),
                    "skill_level": dossier.get("current_skill_level", "N/A"),
                    "tasks": task_map.get(user_id, {})
                })

            return json.dumps({
                "total_members": len(team_members),
                "team": team_members
            }, ensure_ascii=False, default=str)

        except Exception as e:
            logger.error(f"Error getting team overview: {e}")
            return json.dumps({"error": str(e)})

    async def search_in_database(self, query: str, table: str = "tasks") -> str:
        """
        Поиск по базе данных (tasks, user_profiles, sprints).

        Args:
            query: Поисковый запрос
            table: Таблица для поиска (tasks, user_profiles, sprints)

        Returns:
            JSON с результатами поиска
        """
        try:
            supabase = self._get_supabase()

            if table == "tasks":
                params = {
                    "select": "id,title,description,status,assignee",
                    "or": f"(title.ilike.%{query}%,description.ilike.%{query}%)",
                    "limit": "20"
                }
                path = "/rest/v1/tasks"
            elif table == "user_profiles":
                params = {
                    "select": "user_id,username,analysis_summary",
                    "or": f"(username.ilike.%{query}%,analysis_summary.ilike.%{query}%)",
                    "limit": "10"
                }
                path = "/rest/v1/user_profiles"
            elif table == "sprints":
                params = {
                    "select": "id,name,start_date,end_date,status",
                    "name": f"ilike.%{query}%",
                    "order": "start_date.desc",
                    "limit": "10"
                }
                path = "/rest/v1/sprints"
            else:
                return json.dumps({"error": f"Unknown table: {table}"})

            results = await supabase._request("GET", path, params=params)

            return json.dumps({
                "query": query,
                "table": table,
                "total": len(results or []),
                "results": results or []
            }, ensure_ascii=False, default=str)

        except Exception as e:
            logger.error(f"Error searching database: {e}")
            return json.dumps({"error": str(e)})

    async def get_sprints_info(self, status: str = "") -> str:
        """
        Получить информацию о спринтах.

        Args:
            status: Фильтр по статусу (active, completed, planned)

        Returns:
            JSON со списком спринтов
        """
        try:
            supabase = self._get_supabase()

            params = {
                "select": "*",
                "order": "start_date.desc",
                "limit": "10"
            }
            if status:
                params["status"] = f"eq.{status}"

            sprints = await supabase._request("GET", "/rest/v1/sprints", params=params)

            return json.dumps({
                "total": len(sprints or []),
                "sprints": [
                    {
                        "id": s.get("id", ""),
                        "name": s.get("name", ""),
                        "status": s.get("status", ""),
                        "start_date": s.get("start_date", ""),
                        "end_date": s.get("end_date", ""),
                        "goal": s.get("goal", "")
                    }
                    for s in (sprints or [])
                ]
            }, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Error getting sprints: {e}")
            return json.dumps({"error": str(e)})

    # === MeetFlow Integration Tools ===

    async def get_meetflow_meetings(self, days_back: int = 7,
                                    days: int = 0, limit: int = 0) -> str:
        """
        Получить встречи из MeetFlow за последние N дней.

        Args:
            days_back: Количество дней назад для поиска (default 7)

        Returns:
            JSON со списком встреч
        """
        days_back = days or days_back  # алиас (LLM часто зовёт days=)
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.list_meetings_by_period(days_back=days_back)
            return result

        except Exception as e:
            logger.error(f"Error getting MeetFlow meetings: {e}")
            return json.dumps({"error": str(e)})

    async def search_meetflow_meetings(self, search_term: str = "",
                                       query: str = "") -> str:
        """
        Поиск встреч в MeetFlow по названию или содержимому.

        Args:
            search_term: Поисковый запрос

        Returns:
            JSON со списком найденных встреч
        """
        search_term = search_term or query  # алиас (LLM часто зовёт query=)
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.search_meetings(search_term=search_term)
            return result

        except Exception as e:
            logger.error(f"Error searching MeetFlow meetings: {e}")
            return json.dumps({"error": str(e)})

    async def get_meeting_details_meetflow(self, meeting_id: str) -> str:
        """
        Получить детали встречи из MeetFlow.

        Args:
            meeting_id: ID встречи или название

        Returns:
            JSON с деталями встречи
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.get_meeting_details(meeting_id)
            return result

        except Exception as e:
            logger.error(f"Error getting meeting details: {e}")
            return json.dumps({"error": str(e)})

    async def get_meeting_tasks_meetflow(self, meeting_id: str) -> str:
        """
        Получить задачи встречи из ОБОИХ источников: таблица meeting_tasks
        (извлечение MeetFlow) + Task-узлы графа знаний (извлечение мозга,
        CREATED_FROM → Meeting). Дедуп по названию.

        Реальный кейс: агент отправлял «3 задачи из встречи», хотя мозг
        извлёк больше — таблица и граф пополняются РАЗНЫМИ пайплайнами, и
        каждый видит не всё. По одному источнику список неполный.

        Args:
            meeting_id: ID встречи

        Returns:
            JSON со списком задач из встречи (source: meetflow | brain_graph)
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.get_meeting_tasks(meeting_id)

            # Добор из графа знаний — best-effort, сбой не ломает основной путь
            try:
                parsed = json.loads(result) if isinstance(result, str) else (result or {})
                base = parsed.get("tasks")
                if isinstance(base, list):
                    from backend.core.tasks.task_analysis import (
                        collect_meeting_tasks,
                    )
                    graph_tasks = await collect_meeting_tasks(
                        self.user_id or "", meeting_id) or []

                    def _norm(t: str) -> str:
                        return " ".join(str(t or "").lower().split())

                    seen = {_norm(t.get("title")) for t in base
                            if isinstance(t, dict)}
                    seen.discard("")
                    added = 0
                    for gt in graph_tasks:
                        k = _norm(gt.get("title"))
                        if not k or k in seen:
                            continue
                        seen.add(k)
                        base.append({**gt, "source": "brain_graph"})
                        added += 1
                    if added:
                        parsed["note"] = ((parsed.get("note") or "") +
                                          f" +{added} задач(и) из графа знаний "
                                          "(извлечены мозгом из транскрипта)"
                                          ).strip()
                    return json.dumps(parsed, ensure_ascii=False)
            except Exception:
                logger.warning("meeting tasks: graph merge skipped",
                               exc_info=True)
            return result

        except Exception as e:
            logger.error(f"Error getting meeting tasks: {e}")
            return json.dumps({"error": str(e)})

    async def get_yougile_tasks(self, board_id: str = "", status: str = "") -> str:
        """
        Получить задачи из YouGile.

        Args:
            board_id: ID доски (опционально)
            status: Фильтр по статусу

        Returns:
            JSON со списком задач
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            if board_id:
                result = await meetflow.get_tasks_from_board(board_id, status=status)
            else:
                # Try universal task list
                result = await meetflow.list_tasks(system="yougile", status=status)
            return result

        except Exception as e:
            logger.error(f"Error getting YouGile tasks: {e}")
            return json.dumps({"error": str(e)})

    async def get_yougile_boards(self) -> str:
        """
        Получить список досок YouGile.

        Returns:
            JSON со списком досок
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.get_task_boards()
            return result

        except Exception as e:
            logger.error(f"Error getting YouGile boards: {e}")
            return json.dumps({"error": str(e)})

    async def create_yougile_task(
        self,
        title: str,
        column_id: str = "",
        description: str = "",
        assignee: str = ""
    ) -> str:
        """
        Создать задачу в YouGile.

        Args:
            title: Название задачи
            column_id: ID колонки
            description: Описание
            assignee: Исполнитель

        Returns:
            JSON с результатом
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.create_task(
                column_id=column_id,
                title=title,
                description=description,
                assignee=assignee
            )
            return result

        except Exception as e:
            logger.error(f"Error creating YouGile task: {e}")
            return json.dumps({"error": str(e)})

    async def send_telegram_message(self, message: str, chat_id: str = "") -> str:
        """
        Отправить сообщение в Telegram.

        Args:
            message: Текст сообщения
            chat_id: ID чата (опционально, используется default)

        Returns:
            JSON с результатом
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.send_telegram_message(message=message, chat_id=chat_id)
            return result

        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return json.dumps({"error": str(e)})

    async def list_calendar_events(self, days_ahead: int = 7) -> str:
        """
        Получить события из Google Calendar.

        Args:
            days_ahead: Количество дней вперёд

        Returns:
            JSON со списком событий
        """
        try:
            meetflow = self._setup_meetflow_context()
            if not meetflow:
                return json.dumps({"error": "MeetFlow not available"})

            result = await meetflow.list_calendar_events(days_ahead=days_ahead)
            return result

        except Exception as e:
            logger.error(f"Error getting calendar events: {e}")
            return json.dumps({"error": str(e)})

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        Получить определения всех инструментов для AutoGen.

        Returns:
            Список определений инструментов
        """
        return [
            {
                "name": "get_project_status",
                "description": "Получить статус проекта из базы знаний Tessbrain. Возвращает информацию о задачах, решениях и участниках проекта.",
                "function": self.get_project_status,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project_name": {
                            "type": "string",
                            "description": "Название проекта"
                        }
                    },
                    "required": ["project_name"]
                }
            },
            {
                "name": "get_person_info",
                "description": "Получить информацию о человеке из базы знаний. Показывает задачи, встречи и решения связанные с человеком.",
                "function": self.get_person_info,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "person_name": {
                            "type": "string",
                            "description": "Имя человека"
                        }
                    },
                    "required": ["person_name"]
                }
            },
            {
                "name": "get_all_tasks",
                "description": "Получить список всех задач из базы знаний с возможностью фильтрации.",
                "function": self.get_all_tasks,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status_filter": {
                            "type": "string",
                            "description": "Фильтр по статусу (new, in_progress, done, overdue)"
                        },
                        "assignee_filter": {
                            "type": "string",
                            "description": "Фильтр по исполнителю"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Максимальное количество задач"
                        }
                    }
                }
            },
            {
                "name": "get_all_people",
                "description": "Получить список всех людей в системе с их активностью.",
                "function": self.get_all_people,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Максимальное количество результатов"
                        }
                    }
                }
            },
            {
                "name": "get_recent_meetings",
                "description": "Получить список последних встреч.",
                "function": self.get_recent_meetings,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Максимальное количество встреч"
                        }
                    }
                }
            },
            {
                "name": "get_recent_decisions",
                "description": "Получить список последних решений.",
                "function": self.get_recent_decisions,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Максимальное количество решений"
                        }
                    }
                }
            },
            {
                "name": "search_knowledge",
                "description": "Семантический поиск по базе знаний Tessbrain. Можно задавать вопросы на естественном языке.",
                "function": self.search_knowledge,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Поисковый запрос на естественном языке"
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_anomalies_and_risks",
                "description": "Получить список аномалий и рисков: просроченные задачи, застрявшие процессы, изолированные сущности.",
                "function": self.get_anomalies_and_risks,
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_meeting_context",
                "description": "Получить полный контекст встречи: участники, задачи, решения, упомянутые сущности.",
                "function": self.get_meeting_context,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "meeting_id": {
                            "type": "string",
                            "description": "ID встречи"
                        },
                        "title": {
                            "type": "string",
                            "description": "Название встречи (альтернатива ID)"
                        }
                    }
                }
            },
            {
                "name": "add_task_to_graph",
                "description": "Добавить новую задачу в граф знаний.",
                "function": self.add_task_to_graph,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Название задачи"
                        },
                        "description": {
                            "type": "string",
                            "description": "Описание задачи"
                        },
                        "assignee": {
                            "type": "string",
                            "description": "Исполнитель"
                        },
                        "deadline": {
                            "type": "string",
                            "description": "Дедлайн в формате ISO"
                        },
                        "priority": {
                            "type": "string",
                            "description": "Приоритет (low, normal, high, urgent)"
                        },
                        "meeting_id": {
                            "type": "string",
                            "description": "ID связанной встречи"
                        }
                    },
                    "required": ["title"]
                }
            },
            {
                "name": "add_decision_to_graph",
                "description": "Добавить новое решение в граф знаний.",
                "function": self.add_decision_to_graph,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Краткое описание решения"
                        },
                        "category": {
                            "type": "string",
                            "description": "Категория решения"
                        },
                        "urgency": {
                            "type": "string",
                            "description": "Срочность (low, normal, high, urgent)"
                        },
                        "participants": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Список участников принятия решения"
                        },
                        "meeting_id": {
                            "type": "string",
                            "description": "ID связанной встречи"
                        }
                    },
                    "required": ["summary"]
                }
            },
            {
                "name": "send_telegram_message",
                "description": "Send a message to Telegram. Use this to send information, reports, or meeting summaries to users via Telegram.",
                "function": self.send_telegram_message,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The message text to send"
                        },
                        "chat_id": {
                            "type": "string",
                            "description": "Telegram chat ID or @username (optional, uses default if not provided)"
                        }
                    },
                    "required": ["message"]
                }
            }
        ]

