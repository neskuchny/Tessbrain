# -*- coding: utf-8 -*-
"""
Templates API Routes - API для управления шаблонами и workflows.

Endpoints:
- GET    /templates           - Список шаблонов
- POST   /templates           - Создать шаблон
- GET    /templates/{id}      - Получить шаблон
- PATCH  /templates/{id}      - Обновить шаблон
- DELETE /templates/{id}      - Удалить шаблон
- POST   /templates/{id}/execute - Выполнить шаблон
- GET    /templates/trigger   - Найти шаблон по триггеру
- GET    /templates/popular   - Популярные шаблоны
- GET    /templates/stats     - Статистика

- GET    /regulations         - Список регламентов
- POST   /regulations/extract - Извлечь регламенты из встречи
- GET    /regulations/search  - Поиск регламентов

- GET    /extracted-templates - Извлечённые шаблоны
- POST   /extracted-templates/extract - Извлечь шаблоны из встречи
"""

import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from litestar import Router, delete, get, patch, post
from backend.core.auth.user_guard import enforce_user_id_matches_token
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.store.tenant_paths import DEFAULT_USER_ID
from backend.core.templates import (
    RegulationExtractorAgent,
    TemplateCategory,
    TemplateExtractorAgent,
    TemplateStep,
    TemplateVisibility,
    WorkflowTemplate,
    get_template_registry,
)

logger = logging.getLogger(__name__)


def _resolve_user_id(authorization: Optional[str], user_id: Optional[str]) -> str:
    """Получить user_id из токена или параметра.

    БЕЗ DEFAULT-fallback (anti-leak): раньше возвращали DEFAULT_USER_ID
    (00000000-...) когда не было ни валидного токена, ни user_id — все
    неаутентифицированные клиенты делили один бакет, и на странице
    "Извлечённые → Шаблоны/Регламенты" появлялись чужие данные. После
    issue #107 (strict-auth по умолчанию) это срабатывало ЧАЩЕ.

    Теперь без идентификации — бросаем PermissionError; вызывающий
    эндпоинт превращает в 401/пустой список.
    """
    if user_id:
        return user_id
    token_user_id = get_user_id_from_token(authorization)
    if token_user_id:
        return token_user_id
    logger.warning(
        "templates: no user_id and no valid token — refusing DEFAULT_USER_ID "
        "fallback (anti-leak).")
    raise PermissionError("templates: authentication required")


# ============================================
# Request/Response Models
# ============================================

class TemplateStepCreate(BaseModel):
    """Модель для создания шага."""
    step_id: str
    agent: str
    action: str
    params: Dict[str, Any] = Field(default_factory=dict)
    output_variable: str = ""
    condition: Optional[str] = None
    timeout_seconds: int = 60
    order: int = 0


class TemplateCreate(BaseModel):
    """Модель для создания шаблона."""
    name: str
    description: str
    category: str = "analysis"
    visibility: str = "private"
    trigger_patterns: List[str] = Field(default_factory=list)
    steps: List[TemplateStepCreate] = Field(default_factory=list)
    output_format: str = "markdown"
    show_sources: bool = True
    show_anomalies: bool = True
    tags: List[str] = Field(default_factory=list)
    icon: str = "📋"
    color: str = "#3B82F6"


class TemplateUpdate(BaseModel):
    """Модель для обновления шаблона."""
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    visibility: Optional[str] = None
    trigger_patterns: Optional[List[str]] = None
    steps: Optional[List[TemplateStepCreate]] = None
    output_format: Optional[str] = None
    show_sources: Optional[bool] = None
    show_anomalies: Optional[bool] = None
    tags: Optional[List[str]] = None
    icon: Optional[str] = None
    color: Optional[str] = None


class RegulationExtractRequest(BaseModel):
    """Запрос на извлечение регламентов."""
    meeting_id: Optional[str] = None
    meeting_ids: List[str] = Field(default_factory=list)  # Несколько встреч
    transcript: Optional[str] = None
    decisions: List[Dict[str, Any]] = Field(default_factory=list)
    tasks: List[Dict[str, Any]] = Field(default_factory=list)
    desired_regulation: Optional[str] = None  # Описание желаемого регламента
    model_tier: str = "standard"  # standard или premium


class TemplateExtractRequest(BaseModel):
    """Запрос на извлечение шаблонов."""
    meeting_id: Optional[str] = None
    meeting_ids: List[str] = Field(default_factory=list)  # Несколько встреч
    transcript: Optional[str] = None
    tasks: List[Dict[str, Any]] = Field(default_factory=list)
    ideas: List[Dict[str, Any]] = Field(default_factory=list)
    decisions: List[Dict[str, Any]] = Field(default_factory=list)
    desired_template: Optional[str] = None  # Описание желаемого шаблона
    model_tier: str = "standard"  # standard или premium


# ============================================
# Templates Endpoints
# ============================================

@get("/templates")
async def list_templates(
    category: Optional[str] = None,
    visibility: Optional[str] = None,
    search: Optional[str] = None,
    tags: Optional[str] = None,
    limit: int = Parameter(default=50, ge=1, le=100),
    offset: int = Parameter(default=0, ge=0),
    user_id: Optional[str] = None,
    organization_id: Optional[str] = None
) -> Dict[str, Any]:
    """Получить список шаблонов."""
    registry = get_template_registry()

    # Парсим теги
    tag_list = tags.split(",") if tags else None

    # Парсим категорию
    cat = None
    if category:
        try:
            cat = TemplateCategory(category)
        except ValueError:
            logger.debug("suppressed exception", exc_info=True)

    # Парсим видимость
    vis = None
    if visibility:
        try:
            vis = TemplateVisibility(visibility)
        except ValueError:
            logger.debug("suppressed exception", exc_info=True)

    templates = registry.list(
        user_id=user_id,
        organization_id=organization_id,
        category=cat,
        visibility=vis,
        search=search,
        tags=tag_list,
        limit=limit,
        offset=offset
    )

    return {
        "templates": [t.to_dict() for t in templates],
        "total": len(templates),
        "limit": limit,
        "offset": offset
    }


@post("/templates")
async def create_template(
    data: TemplateCreate,
    user_id: Optional[str] = None,
    organization_id: Optional[str] = None
) -> Dict[str, Any]:
    """Создать новый шаблон."""
    registry = get_template_registry()

    # Генерируем ID
    template_id = str(uuid4())[:8]

    # Преобразуем шаги
    steps = [
        TemplateStep(
            step_id=s.step_id,
            agent=s.agent,
            action=s.action,
            params=s.params,
            output_variable=s.output_variable,
            condition=s.condition,
            timeout_seconds=s.timeout_seconds,
            order=s.order
        )
        for s in data.steps
    ]

    # Создаём шаблон
    template = WorkflowTemplate(
        template_id=template_id,
        name=data.name,
        description=data.description,
        category=TemplateCategory(data.category) if data.category else TemplateCategory.ANALYSIS,
        visibility=TemplateVisibility(data.visibility) if data.visibility else TemplateVisibility.PRIVATE,
        trigger_patterns=data.trigger_patterns,
        steps=steps,
        output_format=data.output_format,
        show_sources=data.show_sources,
        show_anomalies=data.show_anomalies,
        tags=data.tags,
        icon=data.icon,
        color=data.color,
        organization_id=organization_id
    )

    created = registry.create(template, user_id=user_id)

    return {
        "success": True,
        "template": created.to_dict()
    }


@get("/templates/trigger")
async def find_by_trigger(
    query: str,
    user_id: Optional[str] = None,
    organization_id: Optional[str] = None
) -> Dict[str, Any]:
    """Найти шаблон по триггеру."""
    registry = get_template_registry()

    template = registry.find_by_trigger(
        query=query,
        user_id=user_id,
        organization_id=organization_id
    )

    if not template:
        return {"found": False, "query": query}

    return {
        "found": True,
        "template": template.to_dict(),
        "match": template.matches_query(query)
    }


@get("/templates/popular")
async def get_popular(
    limit: int = Parameter(default=10, ge=1, le=50)
) -> Dict[str, Any]:
    """Получить популярные шаблоны."""
    registry = get_template_registry()

    templates = registry.get_popular(limit=limit)

    return {
        "templates": [t.to_dict() for t in templates]
    }


@get("/templates/stats")
async def get_stats() -> Dict[str, Any]:
    """Получить статистику шаблонов."""
    registry = get_template_registry()

    return registry.get_stats()


@get("/templates/{template_id:str}")
async def get_template(template_id: str) -> Dict[str, Any]:
    """Получить шаблон по ID."""
    registry = get_template_registry()

    template = registry.get(template_id)

    if not template:
        return {"error": "Template not found", "template_id": template_id}

    return {"template": template.to_dict()}


@patch("/templates/{template_id:str}")
async def update_template(
    template_id: str,
    data: TemplateUpdate,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Обновить шаблон."""
    registry = get_template_registry()

    # Собираем обновления
    updates = {}
    for field, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            if field == "steps" and value:
                updates["steps"] = [
                    TemplateStep(
                        step_id=s["step_id"],
                        agent=s["agent"],
                        action=s["action"],
                        params=s.get("params", {}),
                        output_variable=s.get("output_variable", ""),
                        condition=s.get("condition"),
                        timeout_seconds=s.get("timeout_seconds", 60),
                        order=s.get("order", 0)
                    )
                    for s in value
                ]
            elif field == "category":
                try:
                    updates["category"] = TemplateCategory(value)
                except ValueError:
                    logger.debug("suppressed exception", exc_info=True)
            elif field == "visibility":
                try:
                    updates["visibility"] = TemplateVisibility(value)
                except ValueError:
                    logger.debug("suppressed exception", exc_info=True)
            else:
                updates[field] = value

    try:
        updated = registry.update(template_id, updates, user_id=user_id)

        if not updated:
            return {"error": "Template not found", "template_id": template_id}

        return {"success": True, "template": updated.to_dict()}

    except PermissionError as e:
        return {"error": str(e)}


@delete("/templates/{template_id:str}", status_code=200)
async def delete_template(
    template_id: str,
    user_id: Optional[str] = None
) -> Dict[str, Any]:
    """Удалить шаблон."""
    registry = get_template_registry()

    try:
        success = registry.delete(template_id, user_id=user_id)

        if not success:
            return {"error": "Template not found", "template_id": template_id}

        return {"success": True, "template_id": template_id}

    except PermissionError as e:
        return {"error": str(e)}


# РОУТ /templates/{id}/execute УДАЛЁН (2026-07-03). Исполнитель шаблонов был
# заглушкой: llm_router=None, agent_registry пустой, 5 из 7 «агентов» —
# несуществующие классы; шаги «выполнялись» за 0 мс (мгновенный return ошибки,
# замаскированный под success=true). Функционально дублировал вкладку
# «Автоматизации». Workflow-таб убран из UI, роут выпилен. Каталог builtin
# остаётся read-only (GET /templates). См. docs/KNOWLEDGE_TEMPLATES.md.


# ============================================
# Regulations Endpoints
# ============================================

async def _get_regulation_extractor(user_id: str) -> RegulationExtractorAgent:
    """Получить extractor для конкретного пользователя"""
    from backend.core.llm.router import get_llm_router
    from backend.core.store.graph_view import merged_graph_view_for_user

    llm = get_llm_router()
    # Wave 2.4: federated read — extractor видит шаблоны из коллективного опыта.
    graph = await merged_graph_view_for_user(user_id, use_networkx=None)
    return RegulationExtractorAgent(llm_router=llm, graph_builder=graph)


@get("/regulations")
async def list_regulations(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    regulation_type: Optional[str] = None,
    department: Optional[str] = None,
    mandatory_only: bool = False,
    limit: int = Parameter(default=50, ge=1, le=100),
    offset: int = Parameter(default=0, ge=0)
) -> Dict[str, Any]:
    """Получить список регламентов."""
    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_regulation_extractor(effective_user_id)

    regulations = await extractor.get_all_regulations(
        regulation_type=regulation_type,
        department=department,
        mandatory_only=mandatory_only
    )

    # Пагинация
    paginated = regulations[offset:offset + limit]

    return {
        "regulations": paginated,
        "total": len(regulations),
        "limit": limit,
        "offset": offset
    }


@post("/regulations/extract")
async def extract_regulations(
    data: RegulationExtractRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Извлечь регламенты из встречи или нескольких встреч."""
    from backend.core.llm.usage_tracker import UsageContext, get_usage_tracker
    from backend.db.supabase_client import get_supabase_client

    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_regulation_extractor(effective_user_id)

    # Собираем транскрипты из выбранных встреч
    combined_transcript = data.transcript or ""
    meeting_ids_used = []

    # Если указаны meeting_ids, загружаем транскрипты из БД
    if data.meeting_ids:
        supabase = get_supabase_client()
        for mid in data.meeting_ids:
            try:
                meetings = await supabase._request(
                    "GET",
                    "/rest/v1/meetings",
                    params={
                        "id": f"eq.{mid}",
                        "user_id": f"eq.{effective_user_id}",
                        "select": "id,title,transcription_text"
                    }
                )
                if meetings and meetings[0].get("transcription_text"):
                    meeting = meetings[0]
                    combined_transcript += f"\n\n=== Встреча: {meeting.get('title', mid)} ===\n"
                    combined_transcript += meeting.get("transcription_text", "")
                    meeting_ids_used.append(mid)
            except Exception as e:
                logger.warning(f"Could not load meeting {mid}: {e}")

    # Если указан один meeting_id (для обратной совместимости)
    if data.meeting_id and data.meeting_id not in meeting_ids_used:
        meeting_ids_used.append(data.meeting_id)

    meeting_data = {
        "meeting_id": data.meeting_id or (meeting_ids_used[0] if meeting_ids_used else f"manual_{uuid4().hex[:8]}"),
        "transcript_raw": combined_transcript,
        "decisions": data.decisions,
        "tasks": data.tasks,
        "desired_regulation": data.desired_regulation  # Передаём описание желаемого регламента
    }

    # Трекинг использования с выбранной моделью
    tracker = get_usage_tracker()
    usage_before = tracker.get_session_stats()

    async with UsageContext(
        user_id=effective_user_id,
        agent_mode="templates",
        request_type="regulation_extraction",
        model_tier=data.model_tier
    ):
        regulations = await extractor.extract_regulations(meeting_data)

    # Подсчёт затрат
    usage_after = tracker.get_session_stats()
    tokens_used = (usage_after.get("total_input_tokens", 0) + usage_after.get("total_output_tokens", 0)) - \
                  (usage_before.get("total_input_tokens", 0) + usage_before.get("total_output_tokens", 0))
    cost_used = usage_after.get("total_cost", 0) - usage_before.get("total_cost", 0)

    return {
        "success": True,
        "meeting_id": data.meeting_id,
        "meeting_ids": meeting_ids_used,
        "regulations": [r.to_dict() for r in regulations],
        "count": len(regulations),
        "model_tier": data.model_tier,
        "usage": {
            "tokens": tokens_used,
            "cost": round(cost_used, 6),
            "cost_formatted": f"${cost_used:.4f}"
        }
    }


@get("/regulations/search")
async def search_regulations(
    query: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    limit: int = Parameter(default=10, ge=1, le=50)
) -> Dict[str, Any]:
    """Поиск регламентов."""
    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_regulation_extractor(effective_user_id)

    results = await extractor.search_regulations(query, limit=limit)

    return {
        "query": query,
        "results": results,
        "count": len(results)
    }


# ============================================
# Meetings List for Selection
# ============================================

@get("/meetings-for-extraction")
async def list_meetings_for_extraction(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    search: Optional[str] = None,
    project_id: Optional[str] = Parameter(query="project_id", default=None),
    folder_id: Optional[str] = Parameter(query="folder_id", default=None),
    limit: int = Parameter(default=10, ge=1, le=100),
) -> Dict[str, Any]:
    """
    Список встреч для выбора при создании документа.

    Поиск и фильтры выполняются НА СТОРОНЕ БД (PostgREST), а не в Python по
    последним 100 встречам: раньше старые встречи не находились никогда, а
    поиск шёл только по первым 200 символам транскрипта. Теперь:
    - search: ilike по названию И полному тексту транскрипта, по всем встречам;
    - project_id / folder_id: фильтр по проекту/папке (жалоба «нельзя выбрать»).
    """
    from backend.db.supabase_client import get_supabase_client

    effective_user_id = _resolve_user_id(authorization, user_id)
    supabase = get_supabase_client()

    try:
        # Над-выборка: источник (MeetFlow) кладёт по НЕСКОЛЬКО строк на одну
        # встречу (ре-синк/пере-запись) — напр. «Remontio» = 6 строк за час.
        # Тянем с запасом и схлопываем ниже, иначе дубли съедали бы весь топ-N
        # и пользователю казалось, что встреч больше, чем есть.
        db_limit = min(max(limit * 6, 60), 200)
        params = {
            "user_id": f"eq.{effective_user_id}",
            "select": "id,meeting_id,title,created_at,transcription_text,project_id,folder_id",
            "order": "created_at.desc",
            "limit": str(db_limit),
        }
        if project_id:
            params["project_id"] = f"eq.{project_id}"
        if folder_id:
            params["folder_id"] = f"eq.{folder_id}"
        if search:
            # Экранируем спецсимволы LIKE/PostgREST-or, ищем по title и тексту
            q = search.strip()
            for ch in ("\\", "%", "_", ",", "(", ")"):
                q = q.replace(ch, "\\" + ch)
            params["or"] = f"(title.ilike.%{q}%,transcription_text.ilike.%{q}%)"

        meetings = await supabase._request("GET", "/rest/v1/meetings", params=params)

        # Схлопываем дубли. Ключ: один и тот же внешний meeting_id (точно одна и
        # та же встреча) ИЛИ одинаковое название В ТОТ ЖЕ ДЕНЬ (ре-синк без общего
        # id). Разные встречи с одинаковым названием в РАЗНЫЕ дни (напр. «тест»
        # ×10 за год) остаются раздельными — их не теряем.
        def _dedup_key(m: Dict[str, Any]):
            mid = str(m.get("meeting_id") or "").strip()
            if mid:
                return ("mid", mid)
            title_norm = " ".join(str(m.get("title") or "").lower().split())
            day = str(m.get("created_at") or "")[:10]
            return ("td", title_norm, day)

        groups: Dict[Any, List[Dict[str, Any]]] = {}
        order: List[Any] = []
        for m in (meetings or []):
            key = _dedup_key(m)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(m)

        all_meetings: List[Dict[str, Any]] = []
        for key in order:
            rows = groups[key]
            # Лучшая карточка группы: сперва с транскриптом, затем самая свежая.
            best = max(rows, key=lambda m: (
                1 if len(m.get("transcription_text") or "") > 100 else 0,
                str(m.get("created_at") or "")))
            transcript = best.get("transcription_text") or ""
            transcript_length = len(transcript)
            all_meetings.append({
                "id": best.get("id"),
                "title": best.get("title") or "Без названия",
                "created_at": best.get("created_at"),
                "project_id": best.get("project_id"),
                "folder_id": best.get("folder_id"),
                "transcript_length": transcript_length,
                "has_transcript": transcript_length > 100,
                "duplicate_count": len(rows),  # >1 → в UI бейдж «×N копий»
                "preview": (transcript[:200] + "...") if transcript_length > 200
                           else (transcript if transcript else "Нет транскрипта"),
            })
            if len(all_meetings) >= limit:
                break

        logger.info(
            f"📋 meetings-for-extraction: {len(meetings or [])} строк → "
            f"{len(all_meetings)} встреч после схлопывания дублей "
            f"(search={search}, project={project_id}, folder={folder_id})")

        return {
            "meetings": all_meetings,
            "total": len(all_meetings),
            "search_mode": bool(search or project_id or folder_id),
        }
    except Exception as e:
        logger.error(f"Error listing meetings: {e}")
        return {"meetings": [], "total": 0, "error": str(e)}


# ============================================
# Extracted Templates Endpoints
# ============================================

async def _get_template_extractor(user_id: str) -> TemplateExtractorAgent:
    """Получить extractor для конкретного пользователя"""
    from backend.core.llm.router import get_llm_router
    from backend.core.store.graph_view import merged_graph_view_for_user

    llm = get_llm_router()
    # Wave 2.4: federated read — extractor видит шаблоны из коллективного опыта.
    graph = await merged_graph_view_for_user(user_id, use_networkx=None)
    return TemplateExtractorAgent(llm_router=llm, graph_builder=graph)


@get("/extracted-templates")
async def list_extracted_templates(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    template_type: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Parameter(default=50, ge=1, le=100),
    offset: int = Parameter(default=0, ge=0)
) -> Dict[str, Any]:
    """Получить список извлечённых шаблонов."""
    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_template_extractor(effective_user_id)

    templates = await extractor.get_all_templates(
        template_type=template_type,
        category=category
    )

    # Пагинация
    paginated = templates[offset:offset + limit]

    return {
        "templates": paginated,
        "total": len(templates),
        "limit": limit,
        "offset": offset
    }


@post("/extracted-templates/extract")
async def extract_templates(
    data: TemplateExtractRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Извлечь шаблоны из встречи или нескольких встреч."""
    from backend.core.llm.usage_tracker import UsageContext, get_usage_tracker
    from backend.db.supabase_client import get_supabase_client

    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_template_extractor(effective_user_id)

    # Собираем транскрипты из выбранных встреч
    combined_transcript = data.transcript or ""
    meeting_ids_used = []

    # Если указаны meeting_ids, загружаем транскрипты из БД
    if data.meeting_ids:
        supabase = get_supabase_client()
        for mid in data.meeting_ids:
            try:
                meetings = await supabase._request(
                    "GET",
                    "/rest/v1/meetings",
                    params={
                        "id": f"eq.{mid}",
                        "user_id": f"eq.{effective_user_id}",
                        "select": "id,title,transcription_text"
                    }
                )
                if meetings and meetings[0].get("transcription_text"):
                    meeting = meetings[0]
                    combined_transcript += f"\n\n=== Встреча: {meeting.get('title', mid)} ===\n"
                    combined_transcript += meeting.get("transcription_text", "")
                    meeting_ids_used.append(mid)
            except Exception as e:
                logger.warning(f"Could not load meeting {mid}: {e}")

    # Если указан один meeting_id (для обратной совместимости)
    if data.meeting_id and data.meeting_id not in meeting_ids_used:
        meeting_ids_used.append(data.meeting_id)

    meeting_id = data.meeting_id or (meeting_ids_used[0] if meeting_ids_used else f"manual_{uuid4().hex[:8]}")

    meeting_data = {
        "meeting_id": meeting_id,
        "transcript_raw": combined_transcript,
        "tasks": data.tasks,
        "ideas": data.ideas,
        "decisions": data.decisions,
        "desired_template": data.desired_template  # Передаём описание желаемого шаблона
    }

    # Трекинг использования с выбранной моделью
    tracker = get_usage_tracker()
    usage_before = tracker.get_session_stats()

    async with UsageContext(
        user_id=effective_user_id,
        agent_mode="templates",
        request_type="template_extraction",
        model_tier=data.model_tier
    ):
        templates = await extractor.extract_templates(
            meeting_id=meeting_id,
            meeting_data=meeting_data
        )

    # Подсчёт затрат
    usage_after = tracker.get_session_stats()
    tokens_used = (usage_after.get("total_input_tokens", 0) + usage_after.get("total_output_tokens", 0)) - \
                  (usage_before.get("total_input_tokens", 0) + usage_before.get("total_output_tokens", 0))
    cost_used = usage_after.get("total_cost", 0) - usage_before.get("total_cost", 0)

    return {
        "success": True,
        "meeting_id": meeting_id,
        "meeting_ids": meeting_ids_used,
        "templates": [t.to_dict() for t in templates],
        "count": len(templates),
        "model_tier": data.model_tier,
        "usage": {
            "tokens": tokens_used,
            "cost": round(cost_used, 6),
            "cost_formatted": f"${cost_used:.4f}"
        }
    }


@get("/extracted-templates/search")
async def search_extracted_templates(
    query: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    limit: int = Parameter(default=10, ge=1, le=50)
) -> Dict[str, Any]:
    """Поиск извлечённых шаблонов."""
    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_template_extractor(effective_user_id)

    results = await extractor.search_templates(query, limit=limit)

    return {
        "query": query,
        "results": results,
        "count": len(results)
    }


@post("/extracted-templates/{template_id:str}/use")
async def use_extracted_template(
    template_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Использовать извлечённый шаблон."""
    effective_user_id = _resolve_user_id(authorization, user_id)
    extractor = await _get_template_extractor(effective_user_id)

    result = await extractor.use_template(
        template_id=template_id,
        params=params or {}
    )

    if "error" in result:
        return {"success": False, **result}

    return {"success": True, **result}


# ============================================
# Router
# ============================================

router = Router(
    path="",
    guards=[enforce_user_id_matches_token],
    route_handlers=[
        list_templates,
        create_template,
        find_by_trigger,
        get_popular,
        get_stats,
        get_template,
        update_template,
        delete_template,
        list_regulations,
        extract_regulations,
        search_regulations,
        list_meetings_for_extraction,  # Новый endpoint для выбора встреч
        list_extracted_templates,
        extract_templates,
        search_extracted_templates,
        use_extracted_template,
    ],
    tags=["Templates & Workflows"],
)
