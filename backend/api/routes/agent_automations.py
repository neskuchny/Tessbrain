# -*- coding: utf-8 -*-
"""
Agent Automations API Routes — агентные автоматизации в стиле OpenClaw.

Задачи описываются на естественном языке, AI-агент сам выбирает инструменты.

Endpoints:
- GET /agent-automations — список
- POST /agent-automations — создать
- GET /agent-automations/{id} — получить
- PATCH /agent-automations/{id} — обновить
- DELETE /agent-automations/{id} — удалить
- POST /agent-automations/{id}/pause — приостановить
- POST /agent-automations/{id}/resume — возобновить
- POST /agent-automations/{id}/cancel — отменить
- POST /agent-automations/{id}/run — запустить сейчас
- GET /agent-automations/{id}/logs — история выполнения
- GET /agent-automations/{id}/trace — trace последнего выполнения
- POST /agent-automations/parse-schedule — NL → cron
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.core.automations.agent_automation_service import (
    AgentAutomation,
    get_agent_automation_service,
)

logger = logging.getLogger(__name__)


# Аутентификация (фикс аудита: те же IDOR, что в classic-автоматизациях)
def _auth_user(authorization, requested):
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, requested or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this user")
    except Exception:
        logger.debug("agent-auto auth unavailable", exc_info=True)
    return requested


async def _owned_or_403(service, automation_id, authorization):
    a = await service.get(automation_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent automation not found")
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, "")
    except Exception:
        uid, src = "", "unverified"
    if src != "unverified" and uid and getattr(a, "user_id", None) not in (uid, None):
        raise HTTPException(status_code=403, detail="not your automation")
    return a


# ==================== Request/Response Models ====================

class CreateAgentAutomationRequest(BaseModel):
    """Запрос на создание агентной автоматизации."""
    name: str = Field(..., description="Название")
    instruction: str = Field(..., description="Текстовая инструкция для агента (NL)")
    description: Optional[str] = None

    schedule_type: str = Field(default="once", description="once | recurring | event")
    execute_at: Optional[str] = Field(None, description="ISO datetime для once")
    cron_expression: Optional[str] = Field(None, description="Cron для recurring")
    interval_seconds: Optional[int] = Field(None, description="Интервал в секундах")
    schedule_text: Optional[str] = Field(None, description="NL расписание: 'каждый день в 9:00'")

    trigger_type: str = Field(default="schedule", description="schedule | event")
    event_type: Optional[str] = None
    event_filter: Optional[Dict[str, Any]] = None
    conditions: Optional[List[Dict[str, Any]]] = None

    model_tier: str = Field(default="standard", description="standard | premium")
    max_react_steps: int = Field(default=5, description="Макс. шагов агента")
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    user_id: Optional[str] = None


class UpdateAgentAutomationRequest(BaseModel):
    """Запрос на обновление."""
    name: Optional[str] = None
    instruction: Optional[str] = None
    description: Optional[str] = None
    execute_at: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    model_tier: Optional[str] = None
    max_react_steps: Optional[int] = None
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    status: Optional[str] = None


class AgentAutomationResponse(BaseModel):
    """Ответ с данными автоматизации."""
    id: str
    user_id: str
    name: str
    instruction: str
    description: Optional[str] = None
    schedule_type: str
    execute_at: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    trigger_type: str = "schedule"
    event_type: Optional[str] = None
    model_tier: str = "standard"
    max_react_steps: int = 5
    status: str
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    last_run_trace: Optional[List[Dict[str, Any]]] = None
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    last_run_tokens: int = 0
    last_run_cost_usd: float = 0.0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentAutomationListResponse(BaseModel):
    """Список."""
    automations: List[AgentAutomationResponse]
    total: int


class ParseScheduleRequest(BaseModel):
    """Запрос парсинга NL-расписания."""
    text: str = Field(..., description="NL расписание: 'каждый день в 9:00'")


class ParseScheduleResponse(BaseModel):
    """Результат парсинга."""
    schedule_type: str
    cron_expression: Optional[str] = None
    execute_at: Optional[str] = None
    interval_seconds: Optional[int] = None
    human_readable: str


class RunResultResponse(BaseModel):
    """Результат запуска."""
    success: bool
    message: str
    trace: List[Dict[str, Any]] = []
    final_answer: str = ""
    tokens: int = 0
    duration_ms: int = 0
    steps_count: int = 0


# ==================== Helpers ====================

def _to_response(a: AgentAutomation) -> AgentAutomationResponse:
    return AgentAutomationResponse(
        id=a.id,
        user_id=a.user_id,
        name=a.name,
        instruction=a.instruction,
        description=a.description,
        schedule_type=a.schedule_type,
        execute_at=a.execute_at,
        cron_expression=a.cron_expression,
        interval_seconds=a.interval_seconds,
        trigger_type=a.trigger_type,
        event_type=a.event_type,
        model_tier=a.model_tier,
        max_react_steps=a.max_react_steps,
        status=a.status,
        last_run_at=a.last_run_at,
        last_run_status=a.last_run_status,
        last_error=a.last_error,
        run_count=a.run_count,
        max_runs=a.max_runs,
        expires_at=a.expires_at,
        last_run_trace=a.last_run_trace,
        total_tokens_used=a.total_tokens_used,
        total_cost_usd=a.total_cost_usd,
        last_run_tokens=a.last_run_tokens,
        last_run_cost_usd=a.last_run_cost_usd,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


# ==================== Endpoints ====================

@get("/")
async def list_agent_automations(
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    schedule_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationListResponse:
    """Получить список агентных автоматизаций."""
    service = get_agent_automation_service()
    _uid = _auth_user(authorization, user_id)
    items = await service.list(
        user_id=_uid,
        status=status,
        schedule_type=schedule_type,
        limit=limit,
        offset=offset,
    )
    # скиллы (is_skill=true) живут в своей вкладке — не дублируем их здесь
    items = [a for a in items if not getattr(a, "is_skill", False)]
    return AgentAutomationListResponse(
        automations=[_to_response(a) for a in items],
        total=len(items),
    )


@post("/")
async def create_agent_automation(
    data: CreateAgentAutomationRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationResponse:
    """Создать агентную автоматизацию."""
    service = get_agent_automation_service()
    _uid = _auth_user(authorization, data.user_id)

    cron_expression = data.cron_expression
    execute_at = data.execute_at
    interval_seconds = data.interval_seconds
    schedule_type = data.schedule_type

    # NL schedule parsing
    if data.schedule_text and not cron_expression and not execute_at:
        try:
            parsed = await _parse_schedule_nl(data.schedule_text)
            schedule_type = parsed.get("schedule_type", schedule_type)
            cron_expression = parsed.get("cron_expression")
            execute_at = parsed.get("execute_at")
            interval_seconds = parsed.get("interval_seconds")
        except Exception as e:
            logger.warning(f"[AgentAuto API] NL schedule parse failed: {e}")

    # (фикс аудита) не создаём молчаливо неработающую автоматизацию:
    # once требует момент, recurring — cron/интервал. Event-триггеры — ок.
    if data.trigger_type != "event":
        if schedule_type == "once" and not execute_at:
            raise HTTPException(status_code=400, detail=(
                "Укажите когда выполнить: «через 2 часа», «завтра в 9:00» "
                "или конкретное время."))
        if schedule_type == "recurring" and not (cron_expression or interval_seconds):
            raise HTTPException(status_code=400, detail=(
                "Опишите расписание: «каждый день в 9:00», «по будням» "
                "или cron/интервал."))

    automation = await service.create(
        user_id=_uid or "default_user",
        name=data.name,
        instruction=data.instruction,
        schedule_type=schedule_type,
        execute_at=execute_at,
        cron_expression=cron_expression,
        interval_seconds=interval_seconds,
        trigger_type=data.trigger_type,
        event_type=data.event_type,
        event_filter=data.event_filter,
        conditions=data.conditions,
        model_tier=data.model_tier,
        max_react_steps=data.max_react_steps,
        description=data.description,
        max_runs=data.max_runs,
        expires_at=data.expires_at,
    )
    return _to_response(automation)


@get("/{automation_id:str}")
async def get_agent_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationResponse:
    """Получить автоматизацию по ID."""
    service = get_agent_automation_service()
    a = await _owned_or_403(service, automation_id, authorization)
    return _to_response(a)


@patch("/{automation_id:str}")
async def update_agent_automation(
    automation_id: str, data: UpdateAgentAutomationRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationResponse:
    """Обновить автоматизацию."""
    service = get_agent_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    updates = {k: v for k, v in data.dict(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    await service.update(automation_id, updates)
    a = await service.get(automation_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent automation not found")
    return _to_response(a)


@delete("/{automation_id:str}", status_code=200)
async def delete_agent_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Удалить автоматизацию."""
    service = get_agent_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    await service.delete(automation_id)
    return {"success": True, "message": "Deleted"}


@post("/{automation_id:str}/pause")
async def pause_agent_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationResponse:
    """Приостановить."""
    service = get_agent_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    await service.pause(automation_id)
    a = await service.get(automation_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent automation not found")
    return _to_response(a)


@post("/{automation_id:str}/resume")
async def resume_agent_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationResponse:
    """Возобновить."""
    service = get_agent_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    await service.resume(automation_id)
    a = await service.get(automation_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent automation not found")
    return _to_response(a)


@post("/{automation_id:str}/cancel")
async def cancel_agent_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AgentAutomationResponse:
    """Отменить."""
    service = get_agent_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    await service.cancel(automation_id)
    a = await service.get(automation_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent automation not found")
    return _to_response(a)


@post("/{automation_id:str}/run")
async def run_agent_automation_now(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> RunResultResponse:
    """Запустить автоматизацию прямо сейчас."""
    service = get_agent_automation_service()
    a = await _owned_or_403(service, automation_id, authorization)
    result = await service.execute(a)
    return RunResultResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        trace=result.get("trace", []),
        final_answer=result.get("final_answer", ""),
        tokens=result.get("tokens", 0),
        duration_ms=result.get("duration_ms", 0),
        steps_count=result.get("steps_count", 0),
    )


@get("/{automation_id:str}/logs")
async def get_agent_automation_logs(
    automation_id: str, limit: int = 20,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> List[Dict[str, Any]]:
    """История выполнений."""
    service = get_agent_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    return await service.get_logs(automation_id, limit=limit)


@get("/{automation_id:str}/trace")
async def get_agent_automation_trace(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Trace последнего выполнения."""
    service = get_agent_automation_service()
    a = await _owned_or_403(service, automation_id, authorization)
    return {
        "automation_id": a.id,
        "name": a.name,
        "last_run_status": a.last_run_status,
        "last_run_at": a.last_run_at,
        "trace": a.last_run_trace or [],
    }


@post("/parse-schedule")
async def parse_schedule_endpoint(data: ParseScheduleRequest) -> ParseScheduleResponse:
    """Парсинг NL-расписания в cron/datetime."""
    parsed = await _parse_schedule_nl(data.text)
    return ParseScheduleResponse(
        schedule_type=parsed.get("schedule_type", "once"),
        cron_expression=parsed.get("cron_expression"),
        execute_at=parsed.get("execute_at"),
        interval_seconds=parsed.get("interval_seconds"),
        human_readable=parsed.get("human_readable", data.text),
    )


# ==================== NL Schedule Parser ====================

async def _parse_schedule_nl(text: str) -> Dict[str, Any]:
    """Parse natural language schedule into cron/datetime using LLM."""
    try:
        from backend.core.llm.router import LLMRouter
        llm = LLMRouter()

        now = datetime.now(timezone.utc)
        prompt = f"""Ты парсер расписания. Преобразуй текст в расписание.

Текущая дата/время (UTC): {now.isoformat()}

Текст: "{text}"

Верни JSON (строго) одного из вариантов:

1. Разовое:
{{"schedule_type": "once", "execute_at": "ISO datetime", "human_readable": "описание"}}

2. Повторяющееся (cron):
{{"schedule_type": "recurring", "cron_expression": "* * * * *", "human_readable": "описание"}}

3. Повторяющееся (интервал):
{{"schedule_type": "recurring", "interval_seconds": 3600, "human_readable": "описание"}}

Примеры:
- "каждый день в 9:00" → {{"schedule_type": "recurring", "cron_expression": "0 9 * * *", "human_readable": "Каждый день в 09:00"}}
- "через 2 часа" → {{"schedule_type": "once", "execute_at": "<текущее время + 2 часа в ISO формате>", "human_readable": "Через 2 часа"}}
- "каждые 30 минут" → {{"schedule_type": "recurring", "interval_seconds": 1800, "human_readable": "Каждые 30 минут"}}
- "по будням в 10:00" → {{"schedule_type": "recurring", "cron_expression": "0 10 * * 1-5", "human_readable": "По будням в 10:00"}}
- "каждый понедельник в 9:00" → {{"schedule_type": "recurring", "cron_expression": "0 9 * * 1", "human_readable": "Каждый понедельник в 09:00"}}

Верни ТОЛЬКО JSON, без пояснений."""

        response = await llm.generate(prompt=prompt, max_tokens=300, temperature=0.1)
        response_text = str(response).strip()

        import json as json_mod
        import re
        json_match = re.search(r'\{[^{}]+\}', response_text)
        if json_match:
            return json_mod.loads(json_match.group())

        return {"schedule_type": "once", "human_readable": text}
    except Exception as e:
        logger.warning(f"[AgentAuto] NL schedule parse error: {e}")
        return {"schedule_type": "once", "human_readable": text}


# ==================== Router ====================

router = Router(
    path="/agent-automations",
    route_handlers=[
        parse_schedule_endpoint,     # /parse-schedule (POST) - before parametrized
        list_agent_automations,      # / (GET)
        create_agent_automation,     # / (POST)
        get_agent_automation,        # /{id} (GET)
        update_agent_automation,     # /{id} (PATCH)
        delete_agent_automation,     # /{id} (DELETE)
        pause_agent_automation,      # /{id}/pause
        resume_agent_automation,     # /{id}/resume
        cancel_agent_automation,     # /{id}/cancel
        run_agent_automation_now,    # /{id}/run
        get_agent_automation_logs,   # /{id}/logs
        get_agent_automation_trace,  # /{id}/trace
    ],
    tags=["Agent Automations"],
)
