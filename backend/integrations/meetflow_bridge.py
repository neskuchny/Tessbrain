# -*- coding: utf-8 -*-
"""
MeetFlow Bridge - мост между Tessbrain и MeetFlow агентами.

Позволяет:
1. MeetFlow агентам запрашивать контекст из Tessbrain
2. Tessbrain отправлять команды в MeetFlow (создание задач, отправка сообщений)
3. Синхронизировать данные между системами

Пример использования:
```python
bridge = MeetFlowBridge(graph_builder, reasoning_engine)
await bridge.connect()

# Запрос контекста для агента
context = await bridge.get_context_for_agent("TaskManager", "Какие задачи у Ивана?")

# Выполнение действия через MeetFlow
result = await bridge.execute_action("create_task", {
    "title": "Подготовить отчёт",
    "assignee": "Иван"
})
```
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MeetFlowAction:
    """Действие для выполнения через MeetFlow"""
    action_type: str  # create_task, send_telegram, create_notion_page, schedule
    params: Dict[str, Any] = field(default_factory=dict)
    priority: str = "normal"  # low, normal, high, urgent
    scheduled_at: Optional[datetime] = None
    callback: Optional[Callable] = None


@dataclass
class AgentContext:
    """Контекст для MeetFlow агента"""
    agent_name: str
    query: str

    # Данные из Tessbrain
    relevant_entities: List[Dict[str, Any]] = field(default_factory=list)
    relevant_tasks: List[Dict[str, Any]] = field(default_factory=list)
    relevant_decisions: List[Dict[str, Any]] = field(default_factory=list)
    relevant_meetings: List[Dict[str, Any]] = field(default_factory=list)

    # Метаданные
    confidence: float = 0.0
    reasoning_steps: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    # Рекомендации
    suggested_actions: List[Dict[str, Any]] = field(default_factory=list)


class MeetFlowBridge:
    """
    Мост между Tessbrain и MeetFlow.

    Обеспечивает двустороннюю интеграцию:
    - Tessbrain → MeetFlow: отправка действий
    - MeetFlow → Tessbrain: запрос контекста
    """

    def __init__(
        self,
        graph_builder=None,
        reasoning_engine=None,
        vector_indexer=None,
        meetflow_api_url: Optional[str] = None,
        meetflow_api_key: Optional[str] = None
    ):
        """
        Args:
            graph_builder: GraphBuilder instance
            reasoning_engine: ReasoningEngine instance
            vector_indexer: VectorIndexer instance
            meetflow_api_url: URL MeetFlow API (если есть внешний сервис)
            meetflow_api_key: API ключ для MeetFlow
        """
        self.graph = graph_builder
        self.reasoning = reasoning_engine
        self.vectors = vector_indexer

        self.meetflow_url = meetflow_api_url or os.getenv("MEETFLOW_API_URL", "")
        self.meetflow_key = meetflow_api_key or os.getenv("MEETFLOW_API_KEY", "")

        # Очередь действий
        self._action_queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

        # Кэш контекстов
        self._context_cache: Dict[str, AgentContext] = {}
        self._cache_ttl = 300  # 5 минут

        self.connected = False

        # Маппинг агентов на типы запросов
        self.agent_query_types = {
            "TaskManager": ["task_status", "search_tasks", "person_status"],
            "MeetingManager": ["search_meetings", "meeting_status", "project_status"],
            "ContextAnalyzer": ["general", "summary", "anomaly"],
            "AutomationDirector": ["general", "recommendations"],
            "SchedulerAgent": ["search_tasks", "anomaly"],
            "IntegrationAgent": ["general"]
        }

    async def connect(self):
        """Подключиться к MeetFlow и запустить воркер"""
        if self.connected:
            return

        # Запускаем воркер для обработки очереди действий
        self._worker_task = asyncio.create_task(self._action_worker())

        self.connected = True
        logger.info("✅ MeetFlow Bridge connected")

    async def disconnect(self):
        """Отключиться от MeetFlow"""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                logger.debug("suppressed exception", exc_info=True)

        self.connected = False
        logger.info("MeetFlow Bridge disconnected")

    async def get_context_for_agent(
        self,
        agent_name: str,
        query: str,
        include_recommendations: bool = True
    ) -> AgentContext:
        """
        Получить контекст из Tessbrain для MeetFlow агента.

        Args:
            agent_name: Имя агента (TaskManager, MeetingManager, etc.)
            query: Запрос пользователя
            include_recommendations: Включить рекомендации по действиям

        Returns:
            AgentContext с релевантными данными
        """
        # Проверяем кэш
        cache_key = f"{agent_name}:{query}"
        if cache_key in self._context_cache:
            cached = self._context_cache[cache_key]
            # TODO: проверить TTL
            return cached

        context = AgentContext(agent_name=agent_name, query=query)

        try:
            # Используем ReasoningEngine для получения данных
            if self.reasoning:
                result = await self.reasoning.reason(query)

                context.confidence = result.confidence
                context.reasoning_steps = result.reasoning_steps
                context.sources = result.sources

                # Парсим результаты
                if result.data:
                    context.relevant_entities = result.data.get("entities", [])
                    context.relevant_tasks = result.data.get("tasks", [])
                    context.relevant_decisions = result.data.get("decisions", [])
                    context.relevant_meetings = result.data.get("meetings", [])

            # Добавляем специфичный контекст в зависимости от агента
            if agent_name == "TaskManager":
                context = await self._enrich_task_context(context)
            elif agent_name == "MeetingManager":
                context = await self._enrich_meeting_context(context)
            elif agent_name == "SchedulerAgent":
                context = await self._enrich_scheduler_context(context)

            # Генерируем рекомендации
            if include_recommendations:
                context.suggested_actions = await self._generate_action_recommendations(
                    agent_name, context
                )

            # Кэшируем
            self._context_cache[cache_key] = context

        except Exception as e:
            logger.error(f"Error getting context for agent {agent_name}: {e}")

        return context

    async def _enrich_task_context(self, context: AgentContext) -> AgentContext:
        """Обогатить контекст для TaskManager"""
        if not self.graph or not self.graph.connected:
            return context

        try:
            # Получаем все задачи с их статусами
            from backend.core.think.graph_traverser import GraphTraverser
            traverser = GraphTraverser(self.graph, self.vectors)

            tasks_summary = await traverser.get_all_tasks_summary(limit=50)

            if tasks_summary:
                context.relevant_tasks = tasks_summary.get("tasks", [])

                # Добавляем метрики
                context.sources.append(f"Всего задач: {tasks_summary.get('total', 0)}")

                by_status = tasks_summary.get("by_status", {})
                for status, count in by_status.items():
                    context.sources.append(f"  - {status}: {count}")

        except Exception as e:
            logger.error(f"Error enriching task context: {e}")

        return context

    async def _enrich_meeting_context(self, context: AgentContext) -> AgentContext:
        """Обогатить контекст для MeetingManager"""
        if not self.graph or not self.graph.connected:
            return context

        try:
            from backend.core.think.graph_traverser import GraphTraverser
            traverser = GraphTraverser(self.graph, self.vectors)

            # Получаем последние встречи
            meetings = await traverser.find_nodes("Meeting", limit=10)
            context.relevant_meetings = meetings

        except Exception as e:
            logger.error(f"Error enriching meeting context: {e}")

        return context

    async def _enrich_scheduler_context(self, context: AgentContext) -> AgentContext:
        """Обогатить контекст для SchedulerAgent"""
        if not self.graph or not self.graph.connected:
            return context

        try:
            from backend.core.think.graph_traverser import GraphTraverser
            traverser = GraphTraverser(self.graph, self.vectors)

            # Получаем задачи с дедлайнами
            tasks = await traverser.find_nodes("Task", limit=30)

            # Фильтруем задачи с дедлайнами
            scheduled_tasks = [
                t for t in tasks
                if t.get("deadline") or t.get("scheduled_at")
            ]

            context.relevant_tasks = scheduled_tasks

        except Exception as e:
            logger.error(f"Error enriching scheduler context: {e}")

        return context

    async def _generate_action_recommendations(
        self,
        agent_name: str,
        context: AgentContext
    ) -> List[Dict[str, Any]]:
        """Сгенерировать рекомендации по действиям"""
        recommendations = []

        # Рекомендации на основе контекста
        if agent_name == "TaskManager":
            # Проверяем просроченные задачи
            overdue = [
                t for t in context.relevant_tasks
                if t.get("status") == "overdue" or t.get("is_overdue")
            ]

            if overdue:
                recommendations.append({
                    "action": "send_telegram",
                    "reason": f"Есть {len(overdue)} просроченных задач",
                    "priority": "high",
                    "params": {
                        "message": f"⚠️ Внимание! {len(overdue)} задач просрочены"
                    }
                })

            # Проверяем неназначенные задачи
            unassigned = [
                t for t in context.relevant_tasks
                if not t.get("assignee")
            ]

            if unassigned:
                recommendations.append({
                    "action": "notify",
                    "reason": f"Есть {len(unassigned)} неназначенных задач",
                    "priority": "normal"
                })

        elif agent_name == "MeetingManager":
            # Проверяем встречи без результатов
            no_results = [
                m for m in context.relevant_meetings
                if not m.get("has_summary") and not m.get("tasks_count")
            ]

            if no_results:
                recommendations.append({
                    "action": "analyze_meeting",
                    "reason": f"Есть {len(no_results)} встреч без обработанных результатов",
                    "priority": "normal"
                })

        return recommendations

    async def execute_action(
        self,
        action_type: str,
        params: Dict[str, Any],
        priority: str = "normal",
        scheduled_at: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Выполнить действие через MeetFlow.

        Args:
            action_type: Тип действия (create_task, send_telegram, etc.)
            params: Параметры действия
            priority: Приоритет (low, normal, high, urgent)
            scheduled_at: Когда выполнить (None = сейчас)

        Returns:
            Результат выполнения
        """
        action = MeetFlowAction(
            action_type=action_type,
            params=params,
            priority=priority,
            scheduled_at=scheduled_at
        )

        # Если есть внешний MeetFlow API - отправляем туда
        if self.meetflow_url:
            return await self._send_to_meetflow_api(action)

        # Иначе добавляем в очередь для локальной обработки
        await self._action_queue.put(action)

        return {
            "status": "queued",
            "action_type": action_type,
            "message": f"Action {action_type} queued for execution"
        }

    async def _send_to_meetflow_api(self, action: MeetFlowAction) -> Dict[str, Any]:
        """Отправить действие во внешний MeetFlow API"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.meetflow_url}/api/v1/actions",
                    headers={
                        "Authorization": f"Bearer {self.meetflow_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "action_type": action.action_type,
                        "params": action.params,
                        "priority": action.priority,
                        "scheduled_at": action.scheduled_at.isoformat() if action.scheduled_at else None
                    }
                )
                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.error(f"Failed to send action to MeetFlow API: {e}")
            return {"status": "error", "error": str(e)}

    async def _action_worker(self):
        """Воркер для обработки очереди действий"""
        logger.info("Action worker started")

        while True:
            try:
                action = await self._action_queue.get()

                logger.info(f"Processing action: {action.action_type}")

                # Обрабатываем действие
                result = await self._process_action(action)

                # Вызываем callback если есть
                if action.callback:
                    try:
                        if asyncio.iscoroutinefunction(action.callback):
                            await action.callback(result)
                        else:
                            action.callback(result)
                    except Exception as e:
                        logger.error(f"Action callback failed: {e}")

                self._action_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Action worker error: {e}")
                await asyncio.sleep(1)

    async def _process_action(self, action: MeetFlowAction) -> Dict[str, Any]:
        """Обработать действие локально"""
        try:
            if action.action_type == "create_task":
                return await self._action_create_task(action.params)

            elif action.action_type == "send_telegram":
                return await self._action_send_telegram(action.params)

            elif action.action_type == "create_notion_page":
                return await self._action_create_notion(action.params)

            elif action.action_type == "update_graph":
                return await self._action_update_graph(action.params)

            else:
                return {
                    "status": "unknown",
                    "error": f"Unknown action type: {action.action_type}"
                }

        except Exception as e:
            logger.error(f"Failed to process action {action.action_type}: {e}")
            return {"status": "error", "error": str(e)}

    async def _action_create_task(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Создать задачу в YouGile через MeetFlow tools"""
        try:
            # Импортируем tools из MeetFlow (если доступны)
            # В продакшене это будет вызов к automation_tools_async
            logger.info(f"Creating task: {params.get('title')}")

            # Также добавляем задачу в граф Tessbrain
            if self.graph and self.graph.connected:
                task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                await self.graph.create_node(
                    node_id=task_id,
                    label="Task",
                    properties={
                        "title": params.get("title", ""),
                        "description": params.get("description", ""),
                        "assignee": params.get("assignee", ""),
                        "status": "new",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "source": "meetflow_bridge"
                    }
                )

            return {
                "status": "success",
                "message": f"Task created: {params.get('title')}"
            }

        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _action_send_telegram(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Отправить сообщение в Telegram"""
        try:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = params.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", "")
            message = params.get("message", "")

            if not bot_token or not chat_id or not message:
                return {"status": "error", "error": "Missing Telegram credentials or message"}

            # markdown → Telegram-HTML с фолбэком в чистый текст
            from backend.core.notifications.telegram_format import (
                post_telegram_text,
            )
            res = await post_telegram_text(bot_token, chat_id, message,
                                           timeout=10.0)
            if not res["ok"]:
                return {"status": "error", "error": "telegram send failed"}

            return {"status": "success", "message": "Telegram message sent"}

        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _action_create_notion(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Создать страницу в Notion"""
        # Заглушка - в продакшене использовать Notion API
        logger.info(f"Creating Notion page: {params.get('title')}")
        return {"status": "success", "message": "Notion page creation simulated"}

    async def _action_update_graph(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Обновить граф знаний"""
        try:
            if not self.graph or not self.graph.connected:
                return {"status": "error", "error": "Graph not connected"}

            node_id = params.get("node_id")
            properties = params.get("properties", {})

            if node_id and properties:
                await self.graph.update_node(node_id, properties)
                return {"status": "success", "message": f"Node {node_id} updated"}

            return {"status": "error", "error": "Missing node_id or properties"}

        except Exception as e:
            return {"status": "error", "error": str(e)}

    # === API для AutoGen агентов ===

    def get_tessent_brain_tools(self) -> List[Dict[str, Any]]:
        """
        Получить список инструментов Tessbrain для регистрации в AutoGen.

        Returns:
            Список описаний инструментов
        """
        return [
            {
                "name": "get_project_context",
                "description": "Получить контекст проекта из базы знаний Tessbrain",
                "function": self.tool_get_project_context
            },
            {
                "name": "get_person_context",
                "description": "Получить информацию о человеке из базы знаний",
                "function": self.tool_get_person_context
            },
            {
                "name": "get_tasks_summary",
                "description": "Получить сводку по задачам из базы знаний",
                "function": self.tool_get_tasks_summary
            },
            {
                "name": "search_knowledge_base",
                "description": "Поиск по базе знаний Tessbrain",
                "function": self.tool_search_knowledge_base
            },
            {
                "name": "get_anomalies",
                "description": "Получить список аномалий и рисков",
                "function": self.tool_get_anomalies
            },
            {
                "name": "add_to_knowledge_base",
                "description": "Добавить информацию в базу знаний",
                "function": self.tool_add_to_knowledge_base
            }
        ]

    async def tool_get_project_context(self, project_name: str) -> str:
        """Инструмент: получить контекст проекта"""
        try:
            if self.reasoning:
                result = await self.reasoning.reason(
                    f"Какой статус проекта {project_name}?"
                )
                return json.dumps({
                    "project": project_name,
                    "response": result.response,
                    "data": result.data
                }, ensure_ascii=False)
            return json.dumps({"error": "Reasoning engine not available"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def tool_get_person_context(self, person_name: str) -> str:
        """Инструмент: получить информацию о человеке"""
        try:
            if self.reasoning:
                result = await self.reasoning.reason(
                    f"Чем занимается {person_name}?"
                )
                return json.dumps({
                    "person": person_name,
                    "response": result.response,
                    "data": result.data
                }, ensure_ascii=False)
            return json.dumps({"error": "Reasoning engine not available"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def tool_get_tasks_summary(self, filter_by: str = "") -> str:
        """Инструмент: получить сводку по задачам"""
        try:
            if not self.graph or not self.graph.connected:
                return json.dumps({"error": "Graph not connected"})

            from backend.core.think.graph_traverser import GraphTraverser
            traverser = GraphTraverser(self.graph, self.vectors)

            summary = await traverser.get_all_tasks_summary(limit=50)

            return json.dumps(summary, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def tool_search_knowledge_base(self, query: str) -> str:
        """Инструмент: поиск по базе знаний"""
        try:
            if self.reasoning:
                result = await self.reasoning.reason(query)
                return json.dumps({
                    "query": query,
                    "response": result.response,
                    "sources": result.sources
                }, ensure_ascii=False)
            return json.dumps({"error": "Reasoning engine not available"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def tool_get_anomalies(self) -> str:
        """Инструмент: получить аномалии"""
        try:
            if not self.graph or not self.graph.connected:
                return json.dumps({"error": "Graph not connected"})

            from backend.core.sleep.anomaly_detector import AnomalyDetector
            detector = AnomalyDetector(self.graph)

            anomalies = await detector.detect_all()

            return json.dumps({
                "anomalies": anomalies,
                "total": sum(len(v) for v in anomalies.values() if isinstance(v, list))
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def tool_add_to_knowledge_base(
        self,
        entity_type: str,
        name: str,
        properties: Dict[str, Any]
    ) -> str:
        """Инструмент: добавить в базу знаний"""
        try:
            if not self.graph or not self.graph.connected:
                return json.dumps({"error": "Graph not connected"})

            node_id = f"{entity_type.lower()}_{name.lower().replace(' ', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

            await self.graph.create_node(
                node_id=node_id,
                label=entity_type,
                properties={
                    "name": name,
                    **properties,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "source": "meetflow_tool"
                }
            )

            return json.dumps({
                "success": True,
                "node_id": node_id,
                "message": f"{entity_type} '{name}' added to knowledge base"
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

