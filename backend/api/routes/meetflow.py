# -*- coding: utf-8 -*-
"""
MeetFlow API Routes - эндпоинты для интеграции с MeetFlow.

Предоставляет REST API для:
- Получения контекста для MeetFlow агентов
- Выполнения действий через Tessbrain
- Синхронизации данных
"""

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from litestar import Router, get, post
from litestar.datastructures import State
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.core.auth.user_guard import enforce_user_id_matches_token

logger = logging.getLogger(__name__)

# Ссылки на фоновые авто-синк таски (защита от GC до завершения)
_AUTO_SYNC_TASKS: set = set()


async def _auto_sync_new_meetings(user_id: str) -> None:
    """Обработать новые встречи пользователя в граф знаний (фоном).

    Идёт по подпискам с auto_process_new_meetings=true и запускает тот же
    конвейер, что ручной sync. Best-effort, идемпотентно (processed_meetings)."""
    if not user_id:
        return
    try:
        from backend.core.knowledge_sync import process_meetings_for_subscription
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()
        subs = await supabase._request(
            "GET", "/rest/v1/knowledge_sync_subscriptions",
            params={"user_id": f"eq.{user_id}", "status": "eq.active",
                    "select": "*"})
        for sub in subs or []:
            if not sub.get("auto_process_new_meetings", False):
                continue
            try:
                await process_meetings_for_subscription(
                    subscription_id=sub["id"], user_id=user_id,
                    project_id=sub.get("project_id"),
                    folder_id=sub.get("folder_id"))
            except Exception as e:
                logger.warning(f"auto-sync: sub {sub.get('id')} failed: {e}")
        # граф обновился — перечитываем runtime, чтобы чат сразу видел новое
        try:
            from backend.api.routes.chat import reload_graph_for_user
            await reload_graph_for_user(user_id)
        except Exception:
            logger.debug("auto-sync: graph reload skipped", exc_info=True)
    except Exception as e:
        logger.warning(f"auto-sync failed for {user_id}: {e}")


async def _notify_prism_map(user_id: str, meeting_id: str) -> None:
    """meeting_ended → POST внешней призменной карте (их контракт §5).

    Env PRISM_WEBHOOK_URL (например
    https://<карта>/api/v1/prism/events/meeting_ended); опциональный
    PRISM_WEBHOOK_TOKEN уходит заголовком X-Tessbrain-Token. Пусто → no-op."""
    url = (os.getenv("PRISM_WEBHOOK_URL", "") or "").strip()
    if not (url and user_id and meeting_id):
        return
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        token = (os.getenv("PRISM_WEBHOOK_TOKEN", "") or "").strip()
        if token:
            headers["X-Tessbrain-Token"] = token
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"company_id": user_id,
                                         "meeting_id": meeting_id},
                              headers=headers)
        logger.info("🗺️ prism map notified: meeting %s", meeting_id)
    except Exception:
        logger.warning("prism webhook failed (best-effort)", exc_info=True)


async def _auto_sync_and_notify_tasks(user_id: str, meeting_id: str,
                                      meeting_title: str) -> None:
    """meeting_ended: синк встречи → извлечь задачи → push внешнему пульту.

    Закрывает «идеальную механику» Minitest (§1.1 ТЗ): встреча прошла →
    внешняя система СРАЗУ получает событие meeting_tasks_ready со списком
    задач (заголовок/владелец/задачник) и показывает пульт разметки
    document/code/skip. Без TESSENT_HANDOFF_WEBHOOK_URL push — no-op
    (notify_external_webhook), сам синк идёт как раньше. Best-effort."""
    await _auto_sync_new_meetings(user_id)
    # Пуш призменной карте (их вопрос №3: push предпочтительнее). Env-гейт
    # PRISM_WEBHOOK_URL; best-effort, основной поток не трогает.
    try:
        await _notify_prism_map(user_id, meeting_id)
    except Exception:
        logger.debug("prism webhook skipped", exc_info=True)
    if not (user_id and meeting_id):
        return
    try:
        from backend.core.tasks.task_analysis import (
            extract_meeting_tasks,
            notify_external_webhook,
        )
        # без настроенного webhook не жжём LLM на извлечение задач
        if not (os.getenv("TESSENT_HANDOFF_WEBHOOK_URL", "") or "").strip():
            return
        res = await extract_meeting_tasks(user_id, meeting_id)
        tasks = (res or {}).get("tasks") or []
        await notify_external_webhook(user_id, "meeting_tasks_ready", {
            "meeting_id": meeting_id,
            "meeting_title": meeting_title,
            "tasks": [{
                "title": t.get("title") or "",
                "description": t.get("description") or "",
                "assignee": t.get("assignee") or "",
                "tracker": t.get("tracker") or t.get("source") or "",
                "tracker_task_id": t.get("tracker_task_id") or "",
            } for t in tasks[:50]],
            "total": len(tasks),
            "dispatch": "/api/v1/task-analysis/dispatch",
        })
        logger.info("📮 meeting_tasks_ready → внешний пульт: встреча %s, "
                    "задач %s", meeting_id, len(tasks))
    except Exception as e:
        logger.warning(f"meeting_tasks_ready push failed: {e}")


# === Request/Response Models ===

class ContextRequest(BaseModel):
    """Запрос контекста для агента"""
    agent_name: str
    query: str
    include_recommendations: bool = True


class ContextResponse(BaseModel):
    """Ответ с контекстом"""
    agent_name: str
    query: str
    relevant_entities: List[Dict[str, Any]] = []
    relevant_tasks: List[Dict[str, Any]] = []
    relevant_decisions: List[Dict[str, Any]] = []
    relevant_meetings: List[Dict[str, Any]] = []
    confidence: float = 0.0
    reasoning_steps: List[str] = []
    sources: List[str] = []
    suggested_actions: List[Dict[str, Any]] = []


class ActionRequest(BaseModel):
    """Запрос на выполнение действия"""
    action_type: str  # create_task, send_telegram, update_graph, etc.
    params: Dict[str, Any] = {}
    priority: str = "normal"
    scheduled_at: Optional[str] = None  # ISO datetime


class ActionResponse(BaseModel):
    """Ответ на действие"""
    status: str
    action_type: str
    message: str = ""
    result: Optional[Dict[str, Any]] = None


class SearchRequest(BaseModel):
    """Запрос поиска"""
    query: str
    entity_types: List[str] = []  # Person, Task, Decision, Meeting, Entity
    limit: int = 20


class SearchResponse(BaseModel):
    """Ответ поиска"""
    query: str
    total: int
    results: List[Dict[str, Any]]
    sources: List[str] = []


class TaskCreateRequest(BaseModel):
    """Запрос на создание задачи"""
    title: str
    description: str = ""
    assignee: str = ""
    deadline: str = ""
    priority: str = "normal"
    meeting_id: str = ""


class PersonInfoRequest(BaseModel):
    """Запрос информации о человеке"""
    name: str


class MeetingDeliverRequest(BaseModel):
    """Доставить артефакты встречи (саммари/задачи/решения/участники) в Telegram."""
    user_id: str = Field(..., description="ID пользователя-владельца встречи")
    meeting_id: str = Field(..., description="ID встречи (UUID из списка встреч)")
    chat_id: str = Field(default="", description="Явный адресат Telegram (пусто = привязанный по умолчанию)")
    sections: List[str] = Field(
        default_factory=list,
        description="Какие блоки слать: summary|decisions|tasks|participants (пусто = все)")


class MeetingArtifactRequest(BaseModel):
    """Вытянуть один артефакт встречи (для показа или доставки)."""
    user_id: str = Field(..., description="ID пользователя-владельца встречи")
    meeting_id: str = Field(..., description="ID встречи (UUID из списка встреч)")
    kind: str = Field(default="report",
                      description="summary|tasks|decisions|participants|transcript|agenda|report")
    deliver: bool = Field(default=False, description="Ещё и отправить в Telegram")
    chat_id: str = Field(default="", description="Явный адресат (пусто = привязанный)")


class MeetingShareRequest(BaseModel):
    """Создать публичную ссылку на встречу через MeetFlow (meeting_shares).
    Уровень — только view|comment (ограничение БД MeetFlow)."""
    user_id: str = Field(..., description="ID пользователя-владельца/админа встречи")
    meeting_id: str = Field(..., description="ID встречи (UUID)")
    access_level: str = Field(default="view", description="view|comment")
    expires_in_days: Optional[int] = Field(default=7, description="Срок в днях; null = бессрочно")
    max_views: Optional[int] = Field(default=None, description="Лимит открытий (опц.)")
    allowed_domains: List[str] = Field(default_factory=list, description="Домены-ограничение (опц.)")
    password: str = Field(default="", description="Пароль на ссылку (опц.)")


class MeetingGrantRequest(BaseModel):
    """Дать доступ к встрече по EMAIL/ID (внутренние права MeetFlow)."""
    user_id: str = Field(..., description="ID пользователя-владельца/админа встречи")
    meeting_id: str = Field(..., description="ID встречи (UUID)")
    grantee: str = Field(..., description="Email или UUID получателя")
    permission_type: str = Field(default="read", description="read|write|admin")


class MeetflowWebhookRequest(BaseModel):
    """Универсальный payload вебхука MeetFlow/внешних источников."""
    event_type: str = Field(..., description="Тип внешнего события, например meeting_created")
    user_id: str = Field(..., description="ID пользователя-владельца события")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Данные события")
    source: str = Field(default="meetflow_webhook", description="Источник события")


def _normalize_meeting_event_type(raw_event_type: str) -> Optional[str]:
    """Привести внешние имена событий к внутренним типам EventBus."""
    value = (raw_event_type or "").strip().lower()
    mapping = {
        "meeting_created": "new_meeting_scheduled",
        "meeting.create": "new_meeting_scheduled",
        "meeting.created": "new_meeting_scheduled",
        "meeting_scheduled": "new_meeting_scheduled",
        "new_meeting_scheduled": "new_meeting_scheduled",
        "meeting_ended": "meeting_ended",
        "meeting.end": "meeting_ended",
        "meeting.ended": "meeting_ended",
        "meeting_finished": "meeting_ended",
    }
    return mapping.get(value)


# === API Endpoints ===

@get("/health")
async def meetflow_health() -> Dict[str, Any]:
    """Проверка здоровья MeetFlow интеграции"""
    return {
        "status": "ok",
        "service": "meetflow_integration",
        "timestamp": datetime.utcnow().isoformat()
    }


@post("/context")
async def get_agent_context(
    data: ContextRequest,
    state: State
) -> ContextResponse:
    """
    Получить контекст для MeetFlow агента.

    Используется агентами MeetFlow для получения релевантной информации
    из базы знаний Tessbrain.
    """
    try:
        graph_builder = getattr(state, 'graph', None)
        llm_router = getattr(state, 'llm_router', None)
        vector_indexer = getattr(state, 'vector_indexer', None)

        if not graph_builder or not graph_builder.connected:
            return ContextResponse(
                agent_name=data.agent_name,
                query=data.query,
                sources=["Error: Graph not connected"]
            )

        # Создаём ReasoningEngine для обработки запроса
        from backend.core.think.reasoning_engine import ReasoningEngine
        reasoning_engine = ReasoningEngine(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer,
            llm_router=llm_router
        )

        # Обрабатываем запрос
        result = await reasoning_engine.reason(data.query)

        # Формируем ответ
        # ReasoningResult имеет: answer, data, reasoning_chain, sources, anomalies, confidence
        reasoning_steps = [
            step.get("description", str(step))
            for step in result.reasoning_chain
        ] if result.reasoning_chain else []

        source_strs = [
            src.get("source", str(src)) if isinstance(src, dict) else str(src)
            for src in result.sources
        ] if result.sources else []

        response = ContextResponse(
            agent_name=data.agent_name,
            query=data.query,
            confidence=result.confidence,
            reasoning_steps=reasoning_steps,
            sources=source_strs
        )

        # Парсим данные из результата
        if result.data:
            # Группируем по типам
            for item in result.data.get("nodes", []):
                label = item.get("_label", item.get("type", ""))

                if label == "Person":
                    response.relevant_entities.append(item)
                elif label == "Task":
                    response.relevant_tasks.append(item)
                elif label == "Decision":
                    response.relevant_decisions.append(item)
                elif label == "Meeting":
                    response.relevant_meetings.append(item)
                else:
                    response.relevant_entities.append(item)

        # Добавляем рекомендации если нужно
        if data.include_recommendations:
            response.suggested_actions = _generate_recommendations(
                data.agent_name,
                response
            )

        return response

    except Exception as e:
        logger.error(f"Error getting context for agent {data.agent_name}: {e}")
        return ContextResponse(
            agent_name=data.agent_name,
            query=data.query,
            sources=[f"Error: {e!s}"]
        )


@post("/action")
async def execute_action(
    data: ActionRequest,
    state: State
) -> ActionResponse:
    """
    Выполнить действие через Tessbrain.

    Позволяет MeetFlow агентам выполнять действия:
    - create_task: создать задачу в графе
    - update_graph: обновить узел в графе
    - send_notification: отправить уведомление
    """
    try:
        graph_builder = getattr(state, 'graph', None)

        if data.action_type == "create_task":
            return await _action_create_task(data.params, graph_builder)

        elif data.action_type == "update_graph":
            return await _action_update_graph(data.params, graph_builder)

        elif data.action_type == "add_decision":
            return await _action_add_decision(data.params, graph_builder)

        else:
            return ActionResponse(
                status="error",
                action_type=data.action_type,
                message=f"Unknown action type: {data.action_type}"
            )

    except Exception as e:
        logger.error(f"Error executing action {data.action_type}: {e}")
        return ActionResponse(
            status="error",
            action_type=data.action_type,
            message=str(e)
        )


@post("/search")
async def search_knowledge(
    data: SearchRequest,
    state: State
) -> SearchResponse:
    """
    Поиск по базе знаний.

    Семантический поиск с фильтрацией по типам сущностей.
    """
    try:
        graph_builder = getattr(state, 'graph', None)
        llm_router = getattr(state, 'llm_router', None)
        vector_indexer = getattr(state, 'vector_indexer', None)

        if not graph_builder or not graph_builder.connected:
            return SearchResponse(
                query=data.query,
                total=0,
                results=[],
                sources=["Error: Graph not connected"]
            )

        from backend.core.think.reasoning_engine import ReasoningEngine
        reasoning_engine = ReasoningEngine(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer,
            llm_router=llm_router
        )

        result = await reasoning_engine.reason(data.query)

        # Фильтруем по типам если указаны
        results = []
        if result.data:
            for item in result.data.get("nodes", []):
                label = item.get("_label", item.get("type", ""))

                if not data.entity_types or label in data.entity_types:
                    results.append(item)

        source_strs = [
            src.get("source", str(src)) if isinstance(src, dict) else str(src)
            for src in result.sources
        ] if result.sources else []

        return SearchResponse(
            query=data.query,
            total=len(results),
            results=results[:data.limit],
            sources=source_strs
        )

    except Exception as e:
        logger.error(f"Error searching knowledge: {e}")
        return SearchResponse(
            query=data.query,
            total=0,
            results=[],
            sources=[f"Error: {e!s}"]
        )


@post("/tasks/create")
async def create_task(
    data: TaskCreateRequest,
    state: State
) -> ActionResponse:
    """Создать задачу в графе знаний"""
    return await _action_create_task(data.model_dump(), getattr(state, 'graph', None))


@post("/person/info")
async def get_person_info(
    data: PersonInfoRequest,
    state: State
) -> Dict[str, Any]:
    """Получить информацию о человеке"""
    try:
        graph_builder = getattr(state, 'graph', None)
        vector_indexer = getattr(state, 'vector_indexer', None)

        if not graph_builder or not graph_builder.connected:
            return {"error": "Graph not connected"}

        from backend.core.think.graph_traverser import GraphTraverser
        traverser = GraphTraverser(graph_builder, vector_indexer)

        context = await traverser.find_person_with_context(data.name)

        if context.get("person"):
            person = context["person"]
            return {
                "status": "found",
                "person": {
                    "name": person.get("name", ""),
                    "role": person.get("role", ""),
                    "id": person.get("id", "")
                },
                "tasks": [
                    {"title": t.get("title", ""), "status": t.get("status", "")}
                    for t in context.get("tasks", [])
                ],
                "meetings_count": len(context.get("meetings", [])),
                "decisions_count": len(context.get("decisions", []))
            }

        return {"status": "not_found", "name": data.name}

    except Exception as e:
        logger.error(f"Error getting person info: {e}")
        return {"error": str(e)}


@post("/webhook/event")
async def meetflow_event_webhook(data: MeetflowWebhookRequest) -> Dict[str, Any]:
    """
    Входная точка внешнего вебхука событий встреч.

    Принимает событие от MeetFlow (или другого источника), нормализует тип
    и эмитит его в EventBus для запуска event-автоматизаций.
    """
    normalized = _normalize_meeting_event_type(data.event_type)
    if not normalized:
        return {
            "status": "ignored",
            "message": f"Unsupported event_type: {data.event_type}",
            "supported": ["meeting_created", "meeting_ended", "meeting.created", "meeting.ended"],
        }

    try:
        from backend.core.automations.event_bus import get_event_bus

        bus = get_event_bus()
        # P1: лениво и идемпотентно подписываем meeting-конвейер на
        # `meeting_ended` (startup-хука нет — регистрируем при первом
        # webhook'е). Безопасно при многократном вызове.
        try:
            from backend.core.automations.meeting_pipeline import (
                register_meeting_pipeline,
            )
            register_meeting_pipeline(bus)
        except Exception as _reg_exc:  # регистрация не должна ломать webhook
            logger.debug("meeting_pipeline registration skipped: %s", _reg_exc)

        # Догружаем project_id/folder_id встречи в payload (best-effort) —
        # нужны автоматизациям с фильтром «только встречи из папки/проекта»
        # (event_filter сравнивает по ключам payload).
        _payload = dict(data.payload or {})
        _mid = _payload.get("meeting_id")
        if _mid and not (_payload.get("project_id") and _payload.get("folder_id")):
            try:
                from backend.db.supabase_client import get_supabase_client
                _rows = await get_supabase_client()._request(
                    "GET", "/rest/v1/meetings",
                    params={"meeting_id": f"eq.{_mid}",
                            "select": "project_id,folder_id", "limit": "1"})
                if _rows:
                    _payload.setdefault("project_id", _rows[0].get("project_id"))
                    _payload.setdefault("folder_id", _rows[0].get("folder_id"))
            except Exception as _hyd_exc:
                logger.debug("payload hydration skipped: %s", _hyd_exc)

        event = await bus.emit(
            event_type=normalized,
            payload=_payload,
            user_id=data.user_id,
            source=data.source or "meetflow_webhook",
        )

        # АВТО-СИНК (аудит: auto_process_new_meetings сохранялся, но нигде не
        # потреблялся — встречи попадали в граф знаний только ручным sync).
        # Теперь по meeting_ended запускаем обработку новых встреч для подписок
        # пользователя fire-and-forget (идемпотентно: processed_meetings не
        # даёт обработать дважды).
        if normalized in ("meeting_ended", "new_meeting_scheduled"):
            try:
                import asyncio as _asyncio
                # meeting_ended: после синка ещё извлекаем задачи и пушим
                # meeting_tasks_ready внешнему пульту (Minitest); прочие
                # события — только синк, как раньше.
                if normalized == "meeting_ended" and _payload.get("meeting_id"):
                    _coro = _auto_sync_and_notify_tasks(
                        data.user_id, str(_payload.get("meeting_id") or ""),
                        str(_payload.get("title")
                            or _payload.get("meeting_title") or ""))
                else:
                    _coro = _auto_sync_new_meetings(data.user_id)
                _t = _asyncio.create_task(_coro)
                _AUTO_SYNC_TASKS.add(_t)
                _t.add_done_callback(_AUTO_SYNC_TASKS.discard)
            except Exception as _as_exc:
                logger.warning(f"auto-sync schedule failed: {_as_exc}")

        return {
            "status": "success",
            "normalized_event_type": normalized,
            "event": {
                "id": event.id,
                "event_type": event.event_type,
                "user_id": event.user_id,
                "source": event.source,
                "timestamp": event.timestamp,
            },
        }
    except Exception as e:
        logger.error(f"Error in meetflow webhook event emit: {e}")
        return {"status": "error", "message": str(e)}


@get("/tasks/summary")
async def get_tasks_summary(state: State) -> Dict[str, Any]:
    """Получить сводку по задачам"""
    try:
        graph_builder = getattr(state, 'graph', None)
        vector_indexer = getattr(state, 'vector_indexer', None)

        if not graph_builder or not graph_builder.connected:
            return {"error": "Graph not connected"}

        from backend.core.think.graph_traverser import GraphTraverser
        traverser = GraphTraverser(graph_builder, vector_indexer)

        summary = await traverser.get_all_tasks_summary(limit=100)

        return summary

    except Exception as e:
        logger.error(f"Error getting tasks summary: {e}")
        return {"error": str(e)}


@get("/people/summary")
async def get_people_summary(state: State) -> Dict[str, Any]:
    """Получить сводку по людям"""
    try:
        graph_builder = getattr(state, 'graph', None)
        vector_indexer = getattr(state, 'vector_indexer', None)

        if not graph_builder or not graph_builder.connected:
            return {"error": "Graph not connected"}

        from backend.core.think.graph_traverser import GraphTraverser
        traverser = GraphTraverser(graph_builder, vector_indexer)

        summary = await traverser.get_all_people_summary(limit=100)

        return summary

    except Exception as e:
        logger.error(f"Error getting people summary: {e}")
        return {"error": str(e)}


@get("/anomalies")
async def get_anomalies(
    state: State,
    user_id: str | None = None
) -> Dict[str, Any]:
    """Получить аномалии и риски"""
    try:
        from backend.core.sleep.anomaly_detector import AnomalyDetector
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user

        if not user_id:
            # anomalies должно быть массивом для фронта (HomeTab/insights .map).
            return {"error": "user_id required", "anomalies": [], "total": 0}

        # Загружаем граф пользователя
        graph_builder = GraphBuilder(
            use_networkx=None,  # Auto-detect backend
            graph_storage_path=graph_path_for_user(user_id)
        )
        await graph_builder.connect()

        if not graph_builder.connected or not graph_builder.nx_graph:
            return {"anomalies": [], "total": 0, "message": "Graph not loaded"}

        detector = AnomalyDetector(graph_builder)

        anomalies_data = await detector.detect_all()

        # Преобразуем аномалии в формат для UI
        formatted_anomalies = []
        for category, items in anomalies_data.items():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        # Извлекаем данные из __dict__ если это dataclass
                        anomaly_type = item.get("anomaly_type", category)
                        if hasattr(anomaly_type, "value"):
                            anomaly_type = anomaly_type.value

                        severity = item.get("severity", "medium")
                        if hasattr(severity, "value"):
                            severity = severity.value

                        formatted_anomalies.append({
                            "id": item.get("anomaly_id", f"anom_{len(formatted_anomalies)}"),
                            "type": anomaly_type,
                            "severity": severity,
                            "title": item.get("title", "Аномалия"),
                            "description": item.get("description", ""),
                            "entity": item.get("entity_type", "Unknown"),
                            "entity_id": item.get("entity_id", ""),
                            "entity_name": item.get("entity_name", ""),
                            "suggested_actions": item.get("suggested_actions", []),
                            "details": item.get("details", {})
                        })

        return {
            "total": len(formatted_anomalies),
            "anomalies": formatted_anomalies
        }

    except Exception as e:
        logger.error(f"Error getting anomalies: {e}")
        return {"error": str(e), "anomalies": [], "total": 0}


@get("/insights")
async def get_insights(
    state: State,
    user_id: str | None = None
) -> Dict[str, Any]:
    """Лента инсайтов — агрегатор всех продуцентов.

    Live-скан вынесен в core/sleep/insight_collector (его же гоняет
    ПРОАКТИВНЫЙ цикл планировщика без участия человека); endpoint
    запускает скан best-effort и отдаёт накопленную ленту с ранжированием
    (анти-повтор: отклонённые скрыты, источники с отказами топятся).
    """
    try:
        from backend.core.sleep.insight_store import InsightStore
        from backend.core.store.tenant_paths import insights_path_for_user

        if not user_id:
            return {"error": "user_id required", "insights": [], "total": 0}

        from backend.core.sleep.insight_collector import (
            collect_and_store_insights,
        )
        collected: Dict[str, Any] = {}
        try:
            collected = await collect_and_store_insights(user_id)
        except Exception as e:
            logger.warning(f"insights feed: live collect failed: {e}")

        store = InsightStore(persist_path=insights_path_for_user(user_id))

        def _to_ui(d: dict) -> dict:
            """Стор хранит NightInsight-форму (insight_id/insight_type/read) —
            нормализуем к полям, которые ждёт InsightsPanel."""
            return {
                "id": d.get("id") or d.get("insight_id"),
                "type": d.get("type") or d.get("insight_type", "unknown"),
                "title": d.get("title", ""),
                "description": d.get("description", ""),
                "priority": d.get("priority", "medium"),
                "confidence": d.get("confidence", 0.6),
                "entities": d.get("entities", []),
                "actions": d.get("actions", []),
                "discovered_at": d.get("discovered_at") or d.get("stored_at"),
                "is_read": bool(d.get("is_read", d.get("read", False))),
                "source": d.get("source", "unknown"),
                # outcome-проверка: persists = проблема не решена через N
                # дней (важнее), resolved = ушла из сканов (можно закрывать)
                "outcome": d.get("outcome"),
                "persist_checks": d.get("persist_checks", 0),
            }

        # Ранжирование (порт формулы проактивного диспетчера meetflow):
        # отклонённые скрыты; приоритет + уверенность − пенальти источника,
        # у которого юзер часто отказывал; нерешённые (persists) поднимаются,
        # решённые (resolved) топятся; свежее выше при равенстве.
        declined_by_source = store.declined_counts_by_source()
        _prio = {"high": 2.0, "critical": 2.5, "medium": 1.0, "low": 0.5}

        def _rank(d: dict) -> tuple:
            penalty = min(1.0, declined_by_source.get(d.get("source", ""), 0) / 3.0)
            score = (_prio.get(str(d.get("priority", "medium")).lower(), 1.0)
                     + float(d.get("confidence") or 0.5) - penalty)
            outcome = d.get("outcome")
            if outcome == "persists":   # хроническая проблема — важнее
                score += min(1.0, 0.4 * int(d.get("persist_checks", 1)))
            elif outcome == "resolved":  # решена — вниз ленты
                score -= 1.0
            return (score, str(d.get("discovered_at") or ""))

        raw = [d for d in store.list(limit=300)
               if d.get("reaction") != "declined"]
        merged = sorted((_to_ui(d) for d in raw), key=_rank, reverse=True)[:200]
        if not merged:  # стор недоступен → хотя бы live-результат
            merged = [_to_ui(f) for f in collected.get("live", [])]

        return {
            "total": len(merged),
            "insights": merged,
            "stats": collected.get("stats", {}),
            "sources": collected.get("sources", {}),
        }

    except Exception as e:
        logger.error(f"Error getting insights: {e}")
        return {"error": str(e), "insights": [], "total": 0}


@post("/insights/{insight_id:str}/react")
async def react_insight(
    insight_id: str,
    data: Dict[str, Any],
    user_id: str | None = None,
) -> Dict[str, Any]:
    """Реакция на инсайт: {"reaction": "useful"|"declined"}.

    declined скрывает инсайт и топит его источник в ранжировании ленты
    (анти-повтор — система перестаёт бубнить то, что юзер отверг)."""
    if not user_id:
        return {"status": "error", "message": "user_id required"}
    reaction = str((data or {}).get("reaction") or "")
    if reaction not in ("useful", "declined"):
        return {"status": "error", "message": "reaction must be useful|declined"}
    try:
        from backend.core.sleep.insight_store import InsightStore
        from backend.core.store.tenant_paths import insights_path_for_user
        store = InsightStore(persist_path=insights_path_for_user(user_id))
        ok = store.react(insight_id, reaction)
        return {"status": "success" if ok else "error",
                "message": None if ok else "insight not found"}
    except Exception as e:
        logger.error(f"react_insight failed: {e}")
        return {"status": "error", "message": str(e)}


# === Helper Functions ===

def _generate_recommendations(
    agent_name: str,
    context: ContextResponse
) -> List[Dict[str, Any]]:
    """Генерировать рекомендации по действиям"""
    recommendations = []

    if agent_name == "TaskManager":
        # Проверяем задачи
        overdue = [
            t for t in context.relevant_tasks
            if t.get("status") == "overdue"
        ]
        if overdue:
            recommendations.append({
                "action": "notify",
                "reason": f"{len(overdue)} просроченных задач",
                "priority": "high"
            })

    elif agent_name == "MeetingManager":
        # Проверяем встречи без результатов
        if context.relevant_meetings:
            no_tasks = [
                m for m in context.relevant_meetings
                if not m.get("tasks_count")
            ]
            if no_tasks:
                recommendations.append({
                    "action": "analyze",
                    "reason": f"{len(no_tasks)} встреч без извлечённых задач",
                    "priority": "normal"
                })

    return recommendations


async def _action_create_task(
    params: Dict[str, Any],
    graph_builder
) -> ActionResponse:
    """Создать задачу"""
    try:
        if not graph_builder or not graph_builder.connected:
            return ActionResponse(
                status="error",
                action_type="create_task",
                message="Graph not connected"
            )

        title = params.get("title", "")
        if not title:
            return ActionResponse(
                status="error",
                action_type="create_task",
                message="Title is required"
            )

        task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{title[:20].replace(' ', '_')}"

        await graph_builder.create_node(
            node_id=task_id,
            label="Task",
            properties={
                "title": title,
                "description": params.get("description", ""),
                "assignee": params.get("assignee", ""),
                "deadline": params.get("deadline", ""),
                "priority": params.get("priority", "normal"),
                "status": "new",
                "created_at": datetime.utcnow().isoformat(),
                "source": "meetflow_api"
            }
        )

        return ActionResponse(
            status="success",
            action_type="create_task",
            message=f"Task '{title}' created",
            result={"task_id": task_id}
        )

    except Exception as e:
        return ActionResponse(
            status="error",
            action_type="create_task",
            message=str(e)
        )


async def _action_update_graph(
    params: Dict[str, Any],
    graph_builder
) -> ActionResponse:
    """Обновить узел в графе"""
    try:
        if not graph_builder or not graph_builder.connected:
            return ActionResponse(
                status="error",
                action_type="update_graph",
                message="Graph not connected"
            )

        node_id = params.get("node_id")
        properties = params.get("properties", {})

        if not node_id:
            return ActionResponse(
                status="error",
                action_type="update_graph",
                message="node_id is required"
            )

        await graph_builder.update_node(node_id, properties)

        return ActionResponse(
            status="success",
            action_type="update_graph",
            message=f"Node {node_id} updated"
        )

    except Exception as e:
        return ActionResponse(
            status="error",
            action_type="update_graph",
            message=str(e)
        )


async def _action_add_decision(
    params: Dict[str, Any],
    graph_builder
) -> ActionResponse:
    """Добавить решение"""
    try:
        if not graph_builder or not graph_builder.connected:
            return ActionResponse(
                status="error",
                action_type="add_decision",
                message="Graph not connected"
            )

        summary = params.get("summary", "")
        if not summary:
            return ActionResponse(
                status="error",
                action_type="add_decision",
                message="Summary is required"
            )

        decision_id = f"decision_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        await graph_builder.create_node(
            node_id=decision_id,
            label="Decision",
            properties={
                "summary": summary,
                "decision_summary": summary,
                "category": params.get("category", ""),
                "urgency": params.get("urgency", "normal"),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
                "source": "meetflow_api"
            }
        )

        return ActionResponse(
            status="success",
            action_type="add_decision",
            message="Decision added",
            result={"decision_id": decision_id}
        )

    except Exception as e:
        return ActionResponse(
            status="error",
            action_type="add_decision",
            message=str(e)
        )


# === Projects & Folders Endpoints ===

@get("/projects")
async def get_projects(
    user_id: Optional[str] = None,
    status: str = "active"
) -> Dict[str, Any]:
    """
    Получить проекты пользователя.

    Args:
        user_id: ID пользователя (обязательно)
        status: Статус проектов (active, archived)
    """
    if not user_id:
        return {"status": "error", "message": "user_id is required", "projects": []}

    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()

        projects = await supabase.get_projects(user_id=user_id, status=status)

        return {
            "status": "success",
            "projects": projects or [],
            "total": len(projects or [])
        }
    except Exception as e:
        logger.error(f"Error fetching projects: {e}")
        return {"status": "error", "message": str(e), "projects": []}


@get("/folders")
async def get_folders(
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: str = "active"
) -> Dict[str, Any]:
    """
    Получить папки проекта.

    Args:
        project_id: ID проекта
        user_id: ID пользователя (обязательно)
        status: Статус папок
    """
    if not user_id:
        return {"status": "error", "message": "user_id is required", "folders": []}

    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()

        if project_id:
            folders = await supabase.get_folders(project_id=project_id, user_id=user_id, status=status)
        else:
            # Получаем все папки пользователя
            folders = await supabase._request(
                "GET",
                "/rest/v1/folders",
                params={
                    "user_id": f"eq.{user_id}",
                    "status": f"eq.{status}",
                    "select": "id,name,project_id,parent_folder_id,created_at",
                    "order": "created_at.desc"
                }
            )

        return {
            "status": "success",
            "folders": folders or [],
            "total": len(folders or [])
        }
    except Exception as e:
        logger.error(f"Error fetching folders: {e}")
        return {"status": "error", "message": str(e), "folders": []}


@get("/meetings")
async def get_meetings(
    user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    limit: int = 50
) -> Dict[str, Any]:
    """
    Получить встречи пользователя.

    Args:
        user_id: ID пользователя (обязательно)
        project_id: Фильтр по проекту
        folder_id: Фильтр по папке
        limit: Лимит результатов
    """
    if not user_id:
        return {"status": "error", "message": "user_id is required", "meetings": []}

    # JS-клиенты иногда шлют литеральные "undefined"/"null" — Supabase падал
    # 400 «invalid input syntax for type uuid», и список встреч был пуст
    # («не из чего создать ТЗ»). Мусорный фильтр = отсутствие фильтра.
    if project_id in ("undefined", "null", ""):
        project_id = None
    if folder_id in ("undefined", "null", ""):
        folder_id = None

    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()

        params = {
            "user_id": f"eq.{user_id}",
            "select": "id,title,created_at,project_id,folder_id,status",
            "order": "created_at.desc",
            "limit": str(limit)
        }

        if project_id:
            params["project_id"] = f"eq.{project_id}"
        if folder_id:
            params["folder_id"] = f"eq.{folder_id}"

        meetings = await supabase._request("GET", "/rest/v1/meetings", params=params)

        return {
            "status": "success",
            "meetings": meetings or [],
            "total": len(meetings or [])
        }
    except Exception as e:
        logger.error(f"Error fetching meetings: {e}")
        return {"status": "error", "message": str(e), "meetings": []}


async def _deliver_text_telegram(user_id: str, text: str,
                                 chat_id: str = "") -> Dict[str, Any]:
    """Доставить длинный текст в Telegram. {delivered, parts}.

    Конвертация markdown (жирный/заголовки/таблицы → HTML с фолбэком в чистый
    текст), нарезка на куски ≤4096 и повтор при отказе parse_mode — всё в общем
    хелпере `telegram_format.post_telegram_text`."""
    delivered_any = False
    parts = 0
    try:
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        if chat_id:
            from backend.core.messengers.links import (
                resolve_telegram_bot_token,
            )
            token = await resolve_telegram_bot_token(user_id)
            res = await post_telegram_text(token or "", chat_id, text)
        else:
            from backend.core.help.advice_push import _telegram_chat_for_user
            from backend.core.messengers.links import (
                resolve_telegram_bot_token,
            )
            token = await resolve_telegram_bot_token(user_id)
            target = await _telegram_chat_for_user(user_id)
            res = await post_telegram_text(token or "", target or "", text)
        delivered_any, parts = bool(res["ok"]), int(res["parts"])
    except Exception as e:
        logger.warning(f"deliver: telegram send failed: {e}")
    return {"delivered": delivered_any, "parts": parts}


@post("/meetings/deliver")
async def deliver_meeting(
    data: MeetingDeliverRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Доставить артефакты встречи в Telegram одной кнопкой.

    Собирает всё, что реально сохранено по встрече (саммари, решения, задачи,
    участники) и шлёт пользователю (или на явный chat_id). Длинный пакет
    режется на части. Never-mock: если по встрече ничего не извлечено — честно
    сообщаем, а не шлём пустышку."""
    meeting_id = (data.meeting_id or "").strip()
    # Анти-IDOR: user_id из тела ОБЯЗАН совпасть с токеном (guard роутера
    # проверяет только query/path). Чужой id при валидном токене → отказ.
    from backend.core.auth.user_guard import resolve_user_or_none
    user_id = (resolve_user_or_none(authorization, (data.user_id or "").strip(),
                                    scope="meetflow") or "").strip()
    if not (user_id and meeting_id):
        return {"status": "error", "message": "user_id и meeting_id обязательны", "delivered": False}

    from backend.core.board.meeting_artifacts import fetch_artifact
    art = await fetch_artifact(user_id, meeting_id, "report")
    if art.get("error") == "Встреча не найдена":
        return {"status": "error", "message": "Встреча не найдена", "delivered": False}
    if art.get("empty"):
        return {"status": "empty", "delivered": False,
                "message": ("По встрече пока нет извлечённых артефактов "
                            "(саммари/задач/решений). Дождитесь обработки встречи "
                            "в знания или запустите синхронизацию.")}

    text = art.get("text") or ""
    res = await _deliver_text_telegram(user_id, text, (data.chat_id or "").strip())
    if not res["delivered"]:
        return {"status": "error", "delivered": False,
                "message": ("Не доставлено: не настроен Telegram. Привяжите бота и "
                            "Chat ID в «Интеграции → Telegram» и повторите.")}
    return {"status": "success", "delivered": True, "parts": res["parts"],
            "chars": len(text)}


@post("/meetings/artifact")
async def pull_meeting_artifact(
    data: MeetingArtifactRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Вытянуть ОДИН артефакт встречи: summary|tasks|decisions|participants|
    transcript|agenda|report. Возвращает текст (для показа/копирования) и, если
    deliver=true, ещё и шлёт в Telegram. Never-mock."""
    from backend.core.auth.user_guard import resolve_user_or_none
    user_id = (resolve_user_or_none(authorization, (data.user_id or "").strip(),
                                    scope="meetflow") or "").strip()
    meeting_id = (data.meeting_id or "").strip()
    if not (user_id and meeting_id):
        return {"status": "error", "message": "user_id и meeting_id обязательны"}

    from backend.core.board.meeting_artifacts import fetch_artifact
    art = await fetch_artifact(user_id, meeting_id, (data.kind or "report").strip())
    if art.get("error") == "Встреча не найдена":
        return {"status": "error", "message": "Встреча не найдена"}
    if art.get("empty"):
        return {"status": "empty", "kind": art.get("kind"), "title": art.get("title"),
                "text": "", "delivered": False,
                "message": "По встрече нет этих данных (ещё не извлечены)."}

    out: Dict[str, Any] = {"status": "success", "kind": art.get("kind"),
                           "title": art.get("title"), "text": art.get("text") or ""}
    if data.deliver:
        res = await _deliver_text_telegram(user_id, out["text"], (data.chat_id or "").strip())
        out["delivered"] = res["delivered"]
        if not res["delivered"]:
            out["message"] = ("Текст получен, но НЕ отправлен в Telegram: канал не "
                              "настроен («Интеграции → Telegram»).")
    return out


@post("/meetings/share")
async def share_meeting(
    data: MeetingShareRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Создать публичную ссылку на встречу (MeetFlow) — «сторонний вход по ссылке».

    Проксируем в MeetFlow (владельца встреч и прав): токеном пользователя (тот же
    Supabase-JWT — общий auth) ИЛИ его PAT из «Интеграций». Права проверяет сам
    MeetFlow (только админ встречи может шарить). За флагом ENABLE_MEETFLOW_SHARE."""
    from backend.core.board.meeting_share import (
        create_public_share,
        meetflow_pat,
        share_enabled,
    )
    if not share_enabled():
        return {"status": "error",
                "message": "Расшаривание встреч выключено (ENABLE_MEETFLOW_SHARE)."}
    from backend.core.auth.user_guard import resolve_user_or_none
    user_id = (resolve_user_or_none(authorization, (data.user_id or "").strip(),
                                    scope="meetflow") or "").strip()
    meeting_id = (data.meeting_id or "").strip()
    if not (user_id and meeting_id):
        return {"status": "error", "message": "user_id и meeting_id обязательны"}

    # Токен для MeetFlow: живой Bearer пользователя (кнопка) → PAT (сервер).
    token = ""
    if authorization and authorization.strip().lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = await meetflow_pat(user_id) or ""

    res = await create_public_share(
        meeting_id, auth_token=token,
        access_level=(data.access_level or "view"),
        expires_in_days=data.expires_in_days,
        max_views=data.max_views,
        allowed_domains=data.allowed_domains,
        password=data.password)
    if not res.get("ok"):
        return {"status": "error", "message": res.get("error") or "не удалось создать ссылку"}
    return {"status": "success", "share_url": res.get("share_url"),
            "token": res.get("token"), "access_level": res.get("access_level")}


@post("/meetings/grant")
async def grant_meeting_access(
    data: MeetingGrantRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Дать доступ к встрече по EMAIL/ID (внутренние права MeetFlow, admin/write/
    read). MeetFlow сам резолвит email→user и проверяет права. За флагом."""
    from backend.core.board.meeting_share import (
        grant_meeting_permission,
        meetflow_pat,
        share_enabled,
    )
    if not share_enabled():
        return {"status": "error",
                "message": "Расшаривание встреч выключено (ENABLE_MEETFLOW_SHARE)."}
    from backend.core.auth.user_guard import resolve_user_or_none
    user_id = (resolve_user_or_none(authorization, (data.user_id or "").strip(),
                                    scope="meetflow") or "").strip()
    meeting_id = (data.meeting_id or "").strip()
    grantee = (data.grantee or "").strip()
    if not (user_id and meeting_id and grantee):
        return {"status": "error", "message": "user_id, meeting_id и получатель обязательны"}
    token = ""
    if authorization and authorization.strip().lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = await meetflow_pat(user_id) or ""
    res = await grant_meeting_permission(
        meeting_id, auth_token=token, grantee_id=grantee,
        permission_type=(data.permission_type or "read"))
    if not res.get("ok"):
        return {"status": "error", "message": res.get("error") or "не удалось выдать доступ"}
    return {"status": "success", "granted_to": res.get("granted_to"),
            "permission_type": res.get("permission_type"),
            "meeting_link": res.get("meeting_link")}


# === Router ===

router = Router(
    path="/meetflow",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        meetflow_health,
        get_agent_context,
        execute_action,
        search_knowledge,
        create_task,
        get_person_info,
        meetflow_event_webhook,
        get_tasks_summary,
        get_people_summary,
        get_anomalies,
        # аудит: endpoint существовал, но НЕ был зарегистрирован → страница
        # инсайтов получала 404 и падала
        get_insights,
        react_insight,
        get_projects,
        get_folders,
        get_meetings,
        deliver_meeting,
        pull_meeting_artifact,
        share_meeting,
        grant_meeting_access,
    ],
    tags=["MeetFlow Integration"],
)

