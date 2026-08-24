# -*- coding: utf-8 -*-
"""
Automations API Routes - управление отложенными и повторяющимися задачами.

Endpoints:
- GET /automations - список автоматизаций
- GET /automations/{id} - получить автоматизацию
- POST /automations - создать автоматизацию
- PATCH /automations/{id} - обновить автоматизацию
- DELETE /automations/{id} - удалить автоматизацию
- POST /automations/{id}/pause - приостановить
- POST /automations/{id}/resume - возобновить
- POST /automations/{id}/cancel - отменить
- POST /automations/{id}/run - выполнить сейчас
- GET /automations/{id}/logs - история выполнения
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel, Field

from backend.core.automations.automation_service import (
    ScheduledAutomation,
    get_automation_service,
)
from backend.core.automations.meeting_workflows import (
    get_workflow_template,
    get_workflow_templates,
)

logger = logging.getLogger(__name__)


def _storage_error_detail(e: "httpx.HTTPStatusError") -> str:
    """Ошибка Supabase при сохранении → понятный пользователю текст.

    Раньше HTTPStatusError улетал наверх как «Uncaught exception» (500 без
    объяснения). Тело ответа уже в логе (_request); тут — что показать."""
    body = ""
    try:
        body = (e.response.text or "")[:300] if e.response is not None else ""
    except Exception:
        body = ""
    if "column" in body.lower():
        return ("Хранилище автоматизаций отстало от кода (нет нужных колонок). "
                "Прогоните миграцию backend/db/migrations/"
                "090_event_trigger_columns.sql в Supabase и повторите.")
    return f"Не удалось сохранить автоматизацию: {body or e!s}"


# ==================== Аутентификация (фикс аудита: IDOR) ====================
# Раньше роуты брали user_id из query/тела без проверки → любой мог читать/
# запускать/удалять чужие автоматизации по UUID и тратить чужие токены.
# Теперь: user_id из ВАЛИДНОГО токена (анти-спуфинг), а detail-роуты
# проверяют владельца.

def _auth_user(authorization: Optional[str], requested: Optional[str]) -> Optional[str]:
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, requested or "")
        if src != "unverified" and uid:
            return uid
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this user")
    except Exception:
        logger.debug("automations auth: trusted_user_id unavailable", exc_info=True)
    # без токена (strict-auth OFF) — совместимость: requested как есть
    return requested


async def _owned_or_403(service, automation_id: str, authorization: Optional[str]):
    """Загрузить автоматизацию и убедиться, что она принадлежит владельцу
    из токена. Без валидного токена — passthrough (совместимость)."""
    automation = await service.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")
    try:
        from backend.core.auth.service_token import trusted_user_id
        headers = {"authorization": authorization} if authorization else {}
        uid, src = trusted_user_id(headers, "")
    except Exception:
        uid, src = "", "unverified"
    if src != "unverified" and uid and getattr(automation, "user_id", None) not in (uid, None):
        raise HTTPException(status_code=403, detail="not your automation")
    return automation


# ==================== Request/Response Models ====================

class CreateAutomationRequest(BaseModel):
    """Запрос на создание автоматизации"""
    name: str = Field(..., description="Название задачи")
    action_type: str = Field(..., description="Тип действия: send_telegram, create_calendar_event, etc.")
    action_data: Dict[str, Any] = Field(default_factory=dict, description="Параметры действия")

    schedule_type: str = Field(default="once", description="once | recurring")
    execute_at: Optional[str] = Field(None, description="ISO datetime для одноразовых задач")
    cron_expression: Optional[str] = Field(None, description="Cron выражение для повторяющихся")
    interval_seconds: Optional[int] = Field(None, description="Интервал в секундах")

    description: Optional[str] = None
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    source_message: Optional[str] = None
    user_id: Optional[str] = None


class UpdateAutomationRequest(BaseModel):
    """Запрос на обновление автоматизации"""
    name: Optional[str] = None
    description: Optional[str] = None
    action_data: Optional[Dict[str, Any]] = None
    execute_at: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    status: Optional[str] = None


class AutomationResponse(BaseModel):
    """Ответ с данными автоматизации"""
    id: str
    user_id: str
    name: str
    description: Optional[str] = None
    action_type: str
    action_data: Dict[str, Any]
    schedule_type: str
    execute_at: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None
    status: str
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int = 0
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    # LLM Usage tracking
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    last_run_tokens: int = 0
    last_run_cost_usd: float = 0.0

    # Computed fields
    next_run_at: Optional[str] = None
    is_active: bool = True


class AutomationListResponse(BaseModel):
    """Ответ со списком автоматизаций"""
    automations: List[AutomationResponse]
    total: int
    has_more: bool = False


class ExecutionLogResponse(BaseModel):
    """Лог выполнения"""
    id: str
    automation_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None


class ActionTypesResponse(BaseModel):
    """Список доступных типов действий"""
    action_types: List[Dict[str, str]]


# ==================== Helper Functions ====================

def automation_to_response(auto: ScheduledAutomation) -> AutomationResponse:
    """Конвертировать модель в ответ API"""
    return AutomationResponse(
        id=auto.id,
        user_id=auto.user_id,
        name=auto.name,
        description=auto.description,
        action_type=auto.action_type,
        action_data=auto.action_data,
        schedule_type=auto.schedule_type,
        execute_at=auto.execute_at,
        cron_expression=auto.cron_expression,
        interval_seconds=auto.interval_seconds,
        status=auto.status,
        last_run_at=auto.last_run_at,
        last_run_status=auto.last_run_status,
        last_error=auto.last_error,
        run_count=auto.run_count,
        max_runs=auto.max_runs,
        expires_at=auto.expires_at,
        created_at=auto.created_at,
        updated_at=auto.updated_at,
        total_tokens_used=auto.total_tokens_used,
        total_cost_usd=auto.total_cost_usd,
        last_run_tokens=auto.last_run_tokens,
        last_run_cost_usd=auto.last_run_cost_usd,
        is_active=auto.status == "active",
    )


def parse_natural_time(time_str: str) -> datetime:
    """Парсинг естественного времени (через 5 минут, завтра в 10:00)"""
    import re

    now = datetime.now(timezone.utc)
    time_str = time_str.lower().strip()

    # Относительное время: "через X минут/часов/дней"
    match = re.match(r"через\s+(\d+)\s+(минут|час|часа|часов|день|дня|дней|секунд)", time_str)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)

        if "минут" in unit:
            return now + timedelta(minutes=amount)
        elif "час" in unit:
            return now + timedelta(hours=amount)
        elif "день" in unit or "дн" in unit:
            return now + timedelta(days=amount)
        elif "секунд" in unit:
            return now + timedelta(seconds=amount)

    # Завтра
    if "завтра" in time_str:
        tomorrow = now + timedelta(days=1)
        time_match = re.search(r"(\d{1,2}):(\d{2})", time_str)
        if time_match:
            hour, minute = int(time_match.group(1)), int(time_match.group(2))
            return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)

    # Сегодня в HH:MM
    if "сегодня" in time_str:
        time_match = re.search(r"(\d{1,2}):(\d{2})", time_str)
        if time_match:
            hour, minute = int(time_match.group(1)), int(time_match.group(2))
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # ISO формат
    try:
        return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except Exception:
        logger.debug("suppressed exception", exc_info=True)

    # Fallback: через 1 час
    return now + timedelta(hours=1)


# ==================== Route Handlers ====================

@get("/all")
async def list_all_tasks(
    user_id: Optional[str] = None,
    limit: int = 50,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Получить все задачи из обеих таблиц (новые автоматизации + legacy tasks)"""
    service = get_automation_service()
    uid = _auth_user(authorization, user_id)
    return await service.get_all_tasks_combined(user_id=uid, limit=limit)


@get("/")
async def list_automations(
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    schedule_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationListResponse:
    """Получить список автоматизаций"""
    service = get_automation_service()
    uid = _auth_user(authorization, user_id)

    automations = await service.list_automations(
        user_id=uid,
        status=status,
        schedule_type=schedule_type,
        limit=limit + 1,  # +1 для проверки has_more
        offset=offset,
    )

    has_more = len(automations) > limit
    if has_more:
        automations = automations[:limit]

    return AutomationListResponse(
        automations=[automation_to_response(a) for a in automations],
        total=len(automations),
        has_more=has_more,
    )


@get("/{automation_id:str}")
async def get_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Получить автоматизацию по ID"""
    service = get_automation_service()
    automation = await _owned_or_403(service, automation_id, authorization)
    return automation_to_response(automation)


@post("/")
async def create_automation(
    data: CreateAutomationRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Создать новую автоматизацию"""
    service = get_automation_service()
    _uid = _auth_user(authorization, data.user_id)

    # Парсим время выполнения
    execute_at = None
    if data.execute_at:
        execute_at = parse_natural_time(data.execute_at)

    # (фикс аудита «ловушка once без времени») — не даём создать
    # молчаливо неработающую автоматизацию: у once обязателен момент, у
    # recurring — cron ИЛИ интервал.
    if data.schedule_type == "once" and not execute_at:
        raise HTTPException(status_code=400, detail=(
            "Для разовой автоматизации укажите когда выполнить "
            "(«через 30 минут», «завтра в 10:00» или дату)."))
    if data.schedule_type == "recurring" and not (
            data.cron_expression or data.interval_seconds):
        raise HTTPException(status_code=400, detail=(
            "Для повторяющейся автоматизации укажите расписание "
            "(cron или интервал)."))

    expires_at = None
    if data.expires_at:
        expires_at = parse_natural_time(data.expires_at)

    try:
        automation = await service.create_automation(
            user_id=_uid or "default_user",
            name=data.name,
            action_type=data.action_type,
            action_data=data.action_data,
            schedule_type=data.schedule_type,
            execute_at=execute_at,
            cron_expression=data.cron_expression,
            interval_seconds=data.interval_seconds,
            description=data.description,
            max_runs=data.max_runs,
            expires_at=expires_at,
            source_message=data.source_message,
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=_storage_error_detail(e))

    logger.info(f"✅ Created automation: {automation.name} ({automation.id})")
    return automation_to_response(automation)


@patch("/{automation_id:str}")
async def update_automation(
    automation_id: str, data: UpdateAutomationRequest,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Обновить автоматизацию"""
    service = get_automation_service()
    existing = await _owned_or_403(service, automation_id, authorization)

    # Собираем обновления
    updates = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.description is not None:
        updates["description"] = data.description
    if data.action_data is not None:
        updates["action_data"] = data.action_data
    if data.execute_at is not None:
        updates["execute_at"] = parse_natural_time(data.execute_at).isoformat()
    if data.cron_expression is not None:
        updates["cron_expression"] = data.cron_expression
    if data.interval_seconds is not None:
        updates["interval_seconds"] = data.interval_seconds
    if data.max_runs is not None:
        updates["max_runs"] = data.max_runs
    if data.expires_at is not None:
        updates["expires_at"] = parse_natural_time(data.expires_at).isoformat()
    if data.status is not None:
        updates["status"] = data.status

    automation = await service.update_automation(automation_id, updates)

    if not automation:
        raise HTTPException(status_code=500, detail="Failed to update automation")

    logger.info(f"✅ Updated automation: {automation.name} ({automation.id})")
    return automation_to_response(automation)


@delete("/{automation_id:str}", status_code=200)
async def delete_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Удалить автоматизацию"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)

    success = await service.delete_automation(automation_id)

    if not success:
        raise HTTPException(status_code=404, detail="Automation not found or failed to delete")

    logger.info(f"🗑️ Deleted automation: {automation_id}")
    return {"success": True, "message": "Automation deleted"}


@post("/{automation_id:str}/pause")
async def pause_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Приостановить автоматизацию"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    automation = await service.pause_automation(automation_id)

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    logger.info(f"⏸️ Paused automation: {automation.name}")
    return automation_to_response(automation)


@post("/{automation_id:str}/resume")
async def resume_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Возобновить автоматизацию"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    automation = await service.resume_automation(automation_id)

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    logger.info(f"▶️ Resumed automation: {automation.name}")
    return automation_to_response(automation)


@post("/{automation_id:str}/cancel")
async def cancel_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Отменить автоматизацию"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)
    automation = await service.cancel_automation(automation_id)

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    logger.info(f"❌ Cancelled automation: {automation.name}")
    return automation_to_response(automation)


@post("/{automation_id:str}/run")
async def run_automation_now(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Выполнить автоматизацию немедленно"""
    service = get_automation_service()
    automation = await _owned_or_403(service, automation_id, authorization)
    result = await service.execute_automation(automation)

    logger.info(f"🚀 Executed automation: {automation.name}, success={result.get('success')}")
    return {
        "success": result.get("success", False),
        "message": result.get("message", ""),
        "data": result.get("data"),
    }


@post("/{automation_id:str}/rerun")
async def rerun_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Запустить выполненную/отменённую автоматизацию снова"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)

    # Сначала активируем автоматизацию
    await service.update_automation(automation_id, {"status": "active"})

    # Получаем обновлённую автоматизацию
    automation = await service.get_automation(automation_id)

    # Выполняем
    result = await service.execute_automation(automation)

    logger.info(f"🔄 Reran automation: {automation.name}, success={result.get('success')}")
    return {
        "success": result.get("success", False),
        "message": result.get("message", ""),
        "data": result.get("data"),
    }


@post("/{automation_id:str}/reactivate")
async def reactivate_automation(
    automation_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Активировать выполненную/отменённую автоматизацию без запуска"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)

    # Активируем автоматизацию
    updated = await service.update_automation(automation_id, {"status": "active"})

    if not updated:
        raise HTTPException(status_code=500, detail="Failed to reactivate automation")

    logger.info(f"✅ Reactivated automation: {updated.name}")
    return automation_to_response(updated)


@get("/{automation_id:str}/logs")
async def get_automation_logs(
    automation_id: str,
    limit: int = 20,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> List[ExecutionLogResponse]:
    """Получить историю выполнения автоматизации"""
    service = get_automation_service()
    await _owned_or_403(service, automation_id, authorization)

    # Проверяем существование
    automation = await service.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    # Получаем логи из Supabase
    try:
        params = {
            "automation_id": f"eq.{automation_id}",
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
        }
        logs = await service._request("GET", "/rest/v1/automation_execution_log", params=params)

        return [
            ExecutionLogResponse(
                id=log["id"],
                automation_id=log["automation_id"],
                status=log["status"],
                result=log.get("result"),
                error_message=log.get("error_message"),
                started_at=log["started_at"],
                completed_at=log.get("completed_at"),
                duration_ms=log.get("duration_ms"),
            )
            for log in (logs or [])
        ]
    except Exception as e:
        logger.error(f"Failed to get logs: {e}")
        return []


@get("/action-types")
async def get_action_types() -> ActionTypesResponse:
    """Получить список доступных типов действий"""
    return ActionTypesResponse(
        action_types=[
            {"id": "send_telegram", "name": "📱 Telegram", "description": "Отправить сообщение в Telegram"},
            {"id": "create_calendar_event", "name": "📅 Google Calendar", "description": "Создать событие в календаре"},
            {"id": "create_notion_page", "name": "📝 Notion", "description": "Создать страницу в Notion"},
            {"id": "send_email", "name": "📧 Email", "description": "Отправить email через Gmail"},
            {"id": "send_slack", "name": "💬 Slack", "description": "Отправить сообщение в Slack-канал"},
            {"id": "create_linear_issue", "name": "📐 Linear", "description": "Создать issue в Linear"},
            {"id": "create_yougile_task", "name": "✅ YouGile", "description": "Создать задачу в YouGile"},
            {"id": "create_jira_issue", "name": "📌 Jira", "description": "Создать задачу в Jira (проект, тип, приоритет)"},
            {"id": "create_trello_card", "name": "📋 Trello", "description": "Создать карточку в Trello-списке"},
            {"id": "create_github_issue", "name": "🐙 GitHub", "description": "Создать issue в репозитории GitHub"},
            {"id": "crm_write", "name": "💼 Запись в CRM", "description": "Создать сделку/лид или добавить комментарий в amoCRM / Bitrix24 / HubSpot / Pipedrive (нужен ENABLE_CRM_WRITEBACK на сервере)"},
            {"id": "integration_tool", "name": "🧰 Любой инструмент интеграции", "description": "Универсально: вызвать любой инструмент подключённых интеграций (tool_name + параметры)"},
            {"id": "custom_agent_task", "name": "🤖 Agent", "description": "Выполнить произвольную команду через агентов"},
            {"id": "run_analysis", "name": "🔬 Разбор документа", "description": "Запустить методику анализа (Анализ) по документам — по расписанию или событию"},
            {"id": "run_meeting_pipeline", "name": "🧩 Задачи → готовые ТЗ (vibe-tasking)", "description": "Задачи встречи прогнать через мозг: готовое ТЗ на контексте компании → доставка/гейт подтверждения/исполнитель"},
            {"id": "scheduled_research", "name": "🔬 Исследование / дайджест рынка", "description": "Собрать сайты/новости/публичные Telegram-каналы, разобрать под вашим углом (инсайты рынка, полезные нам) и прислать отчёт по расписанию"},
            {"id": "chat_digest", "name": "💬 Дайджест командного чата", "description": "Разобрать переписку команды (Slack; Telegram-группа — через userbot) за период: о чём говорили, проблемы, решения, на что обратить внимание"},
            # LLM Processing - NEW
            {"id": "llm_process", "name": "🧠 LLM Обработка", "description": "Обработать данные через LLM и отправить результат"},
            {"id": "llm_extract_and_send", "name": "🔍 Извлечь и отправить", "description": "Извлечь данные из текста через LLM и отправить куда-то"},
            # Meeting workflows
            {"id": "meeting_summary_telegram", "name": "📋 Саммари → Telegram", "description": "Извлечь саммари встречи и отправить в Telegram"},
            {"id": "meeting_tasks_yougile", "name": "✅ Задачи → YouGile", "description": "Извлечь задачи из встречи и создать в YouGile"},
            {"id": "meeting_report_email", "name": "📧 Отчёт → Email", "description": "Сформировать отчёт по встрече и отправить на email"},
            {"id": "meeting_notes_notion", "name": "📝 Заметки → Notion", "description": "Создать заметки встречи в Notion"},
            {"id": "meeting_document", "name": "📄 Документ по встрече", "description": "Собрать документ (КП/договор/карточку) по итогам встречи"},
            {"id": "client_followup", "name": "🤝 Follow-up клиенту", "description": "Письмо-продолжение по итогам клиентской встречи"},
            {"id": "hr_candidate_report", "name": "🧑‍💼 Отчёт по кандидату", "description": "HR-разбор интервью: сильные/слабые стороны, рекомендация"},
            {"id": "commitments_tracker", "name": "📎 Трекер обязательств", "description": "Кто что пообещал на встрече — список обязательств с владельцами и сроками"},
            {"id": "client_risk_alert", "name": "🚨 Риск-алерт по клиенту", "description": "Сигналы риска (недовольство, затягивание, конкуренты) из встречи"},
            {"id": "sales_call_scorecard", "name": "📈 Скоринг звонка продаж", "description": "Оценка звонка по методике продаж: что сработало, что улучшить"},
            {"id": "voice_of_customer", "name": "🗣 Голос клиента", "description": "Боли, запросы и цитаты клиента из встречи"},
            {"id": "one_on_one_pulse", "name": "🫶 Пульс 1-on-1", "description": "Настроение и риски сотрудника по встрече один-на-один"},
            {"id": "call_qa_review", "name": "🎧 QA-разбор звонка", "description": "Контроль качества разговора: скрипт, тон, ошибки"},
            {"id": "exec_weekly_brief", "name": "🧭 Weekly-бриф руководителя", "description": "Сводка недели для руководителя: главное из всех встреч"},
            {"id": "daily_meetings_digest", "name": "📅 Ежедневный дайджест", "description": "Ежедневная сводка по встречам"},
            {"id": "morning_briefing", "name": "☀️ Утренний брифинг", "description": "Утренний план на день"},
            {"id": "weekly_tasks_report", "name": "📊 Еженедельный отчёт", "description": "Еженедельный отчёт по задачам"},
            {"id": "weekly_meetings_summary", "name": "🗓 Итоги недели по встречам", "description": "Еженедельная сводка всех встреч"},
            {"id": "sync_overdue_tasks", "name": "⏰ Просроченные задачи", "description": "Свести просроченные задачи и напомнить"},
            {"id": "deadline_reminder", "name": "🔔 Напоминание о дедлайнах", "description": "Напомнить о приближающихся сроках задач"},
        ]
    )


@get("/meetings-list")
async def get_meetings_for_automation(
    user_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """
    Получить список встреч пользователя для выбора в автоматизациях.

    Returns:
        Список встреч с id, title, created_at
    """
    # Названия встреч — PII, только владельцу из токена (фикс аудита)
    user_id = _auth_user(authorization, user_id)
    if not user_id:
        return {"status": "error", "message": "user_id is required", "meetings": []}

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

        meetings = await supabase._request("GET", "/rest/v1/meetings", params=params)

        return {
            "status": "success",
            "meetings": [
                {
                    "id": m.get("id"),
                    "title": m.get("title", "Без названия"),
                    "created_at": m.get("created_at", "")[:10] if m.get("created_at") else "",
                    "project_id": m.get("project_id"),
                    "display": f"{m.get('title', 'Встреча')} ({m.get('created_at', '')[:10]})"
                }
                for m in (meetings or [])
            ],
            "total": len(meetings or [])
        }
    except Exception as e:
        logger.error(f"Error fetching meetings for automation: {e}")
        return {"status": "error", "message": str(e), "meetings": []}


@get("/meeting-tasks")
async def get_meeting_tasks_for_automation(
    meeting_id: str,
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Задачи конкретной встречи — для выбора галочками в vibe-tasking action.

    Используется UI-блоком run_meeting_pipeline: пользователь выбирает, какие
    задачи прогнать через мозг-конвейер. Изоляция по владельцу из токена."""
    uid = _auth_user(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "user_id is required", "tasks": []}
    if not meeting_id:
        return {"status": "error", "message": "meeting_id is required", "tasks": []}
    try:
        from backend.db.supabase_client import get_supabase_client
        supabase = get_supabase_client()
        # meeting_tasks изолированы через meetings.user_id — сверяем владельца
        # встречи, затем берём её задачи.
        owner = await supabase._request(
            "GET", "/rest/v1/meetings",
            params={"id": f"eq.{meeting_id}", "user_id": f"eq.{uid}",
                    "select": "id"})
        if not owner:
            return {"status": "error", "message": "meeting not found", "tasks": []}
        rows = await supabase._request(
            "GET", "/rest/v1/meeting_tasks",
            params={"meeting_id": f"eq.{meeting_id}",
                    "select": "task_id,title,description,assignee,due_date,status,priority",
                    "order": "created_at.asc", "limit": "100"}) or []
        return {
            "status": "success",
            "tasks": [
                {
                    "task_id": t.get("task_id"),
                    "title": t.get("title", "Задача"),
                    "assignee": t.get("assignee", ""),
                    "due_date": t.get("due_date", ""),
                    "status": t.get("status", ""),
                    "priority": t.get("priority", ""),
                }
                for t in rows
            ],
            "total": len(rows),
        }
    except Exception as e:
        logger.error(f"Error fetching meeting tasks for automation: {e}")
        return {"status": "error", "message": str(e), "tasks": []}


@get("/integrations-list")
async def get_integrations_for_automation(
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """
    Получить список интеграций пользователя для выбора в автоматизациях.

    Returns:
        Список интеграций с контактами (telegram chats, emails, etc.)
    """
    # PII (chat_id/email) — только владельцу из токена (фикс аудита: утечка)
    uid = _auth_user(authorization, user_id)
    if not uid:
        return {"status": "error", "message": "user_id is required", "integrations": []}

    try:
        from backend.api.routes.integrations import decrypt_secret
        from backend.db.supabase_client import get_supabase_client

        supabase = get_supabase_client()

        # Получаем все интеграции пользователя
        items = await supabase._request(
            "GET",
            "/rest/v1/user_integrations",
            params={
                "user_id": f"eq.{uid}",
                "select": "id,provider,key_name,secret_enc,meta"
            }
        ) or []

        # Группируем по провайдерам
        integrations = {}
        for item in items:
            provider = item.get("provider", "unknown")
            key_name = item.get("key_name", "")

            if provider not in integrations:
                integrations[provider] = {
                    "provider": provider,
                    "configured": False,
                    "contacts": [],
                    "keys": []
                }

            # Проверяем наличие ключей API
            if not key_name.startswith("contact_"):
                integrations[provider]["configured"] = True
                integrations[provider]["keys"].append(key_name)
            else:
                # Это контакт (telegram chat, email, etc.)
                try:
                    value = decrypt_secret(item.get("secret_enc", ""))
                    meta = item.get("meta", {})
                    integrations[provider]["contacts"].append({
                        "id": item.get("id"),
                        "type": meta.get("contact_type", key_name.replace("contact_", "")),
                        "value": value,
                        "display_name": meta.get("display_name", value),
                        "display": f"{meta.get('display_name', '')} ({value})" if meta.get("display_name") else value
                    })
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        return {
            "status": "success",
            "integrations": list(integrations.values()),
            "total": len(integrations)
        }
    except Exception as e:
        logger.error(f"Error fetching integrations for automation: {e}")
        return {"status": "error", "message": str(e), "integrations": []}


@get("/recipient-groups")
async def list_recipient_groups(
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Именованные группы получателей («Отдел продаж» = список чатов/почт)."""
    uid = _auth_user(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    from backend.core.automations.recipient_groups import list_groups
    return {"groups": await list_groups(uid)}


@post("/recipient-groups")
async def create_recipient_group(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _auth_user(authorization, data.get("user_id"))
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    recips = data.get("recipients") or []
    if not recips:
        raise HTTPException(status_code=400, detail="recipients required")
    from backend.core.automations.recipient_groups import add_group
    return await add_group(uid, str(data.get("name") or "Группа"),
                           str(data.get("channel") or "telegram"), list(recips))


@delete("/recipient-groups/{group_id:str}", status_code=200)
async def delete_recipient_group(
    group_id: str,
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _auth_user(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    from backend.core.automations.recipient_groups import delete_group
    if not await delete_group(uid, group_id):
        raise HTTPException(status_code=404, detail="Группа не найдена")
    return {"deleted": group_id}


@get("/projects-list")
async def get_projects_for_automation(
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    """Проекты и папки пользователя — для выбора «применять к встречам из
    папки» в настройке шаблона. Только по владельцу (auth)."""
    uid = _auth_user(authorization, user_id)
    if not uid:
        return {"status": "error", "projects": [], "folders": []}
    try:
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        projects = await sb._request(
            "GET", "/rest/v1/projects",
            params={"user_id": f"eq.{uid}", "status": "eq.active",
                    "select": "project_id,name", "order": "created_at.desc",
                    "limit": "100"}) or []
        folders = await sb._request(
            "GET", "/rest/v1/folders",
            params={"user_id": f"eq.{uid}", "status": "eq.active",
                    "select": "id,name,project_id", "limit": "300"}) or []
        return {"status": "success", "projects": projects, "folders": folders}
    except Exception as e:
        logger.warning(f"projects-list failed: {e}")
        return {"status": "error", "projects": [], "folders": []}


@get("/templates")
async def get_templates() -> List[Dict[str, Any]]:
    """Получить список готовых шаблонов автоматизаций для встреч"""
    return get_workflow_templates()


@get("/templates/{template_id:str}")
async def get_template(template_id: str) -> Dict[str, Any]:
    """Получить шаблон по ID"""
    template = get_workflow_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@post("/from-template")
async def create_from_template(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> AutomationResponse:
    """Создать автоматизацию из шаблона"""
    template_id = data.get("template_id")
    if not template_id:
        raise HTTPException(status_code=400, detail="template_id required")

    template = get_workflow_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    service = get_automation_service()
    _uid = _auth_user(authorization, data.get("user_id"))

    # Объединяем параметры шаблона с пользовательскими
    params = {**template.get("parameters", {}), **data.get("parameters", {})}

    # Определяем расписание
    execute_at = None
    cron_expression = None
    interval_seconds = None
    trigger_type = "schedule"
    event_type = None

    # apply_to (для meeting_processing): "all_new" — срабатывать ПОСЛЕ КАЖДОЙ
    # новой встречи (event-триггер, рекомендуется), "folder" — только для
    # новых встреч выбранной папки/проекта (event-триггер с фильтром),
    # "meeting" — по одной выбранной встрече (разово). Так шаблон «Саммари
    # встречи → Telegram» становится осмысленным: настроил раз — работает.
    apply_to = data.get("apply_to")
    event_filter: Optional[Dict[str, Any]] = None
    is_meeting = template.get("category") == "meeting_processing"
    if is_meeting and apply_to != "meeting":
        # по умолчанию для meeting-шаблонов — событие «встреча обработана»
        trigger_type = "event"
        event_type = "meeting_ended"
        # meeting_id при событийном триггере не нужен — применяется ко всем
        params.pop("meeting_id", None)
        if apply_to == "folder":
            # фильтр по папке/проекту: срабатывает только на встречи оттуда
            _flt: Dict[str, Any] = {}
            if data.get("folder_id"):
                _flt["folder_id"] = data["folder_id"]
            elif data.get("project_id"):
                _flt["project_id"] = data["project_id"]
            if not _flt:
                raise HTTPException(status_code=400, detail=(
                    "Для «встреч из папки» выберите проект или папку."))
            event_filter = _flt
    elif template["default_schedule"] == "once":
        execute_at_str = data.get("execute_at")
        if execute_at_str:
            execute_at = parse_natural_time(execute_at_str)
        if not execute_at:
            # «К одной встрече (разово)» без времени: выполняем СРАЗУ
            # (ближайший тик планировщика). Раньше once без execute_at
            # никогда не попадала в выборку due — молча висела вечно.
            execute_at = datetime.now(timezone.utc)
    else:
        cron_expression = data.get("cron_expression") or template.get("default_cron")
        interval_seconds = data.get("interval_seconds") or template.get("default_interval")

    try:
        automation = await service.create_automation(
            user_id=_uid or "default_user",
            name=data.get("name") or template["name"],
            action_type=template["action_type"],
            action_data=params,
            schedule_type=("once" if trigger_type == "event"
                           else template["default_schedule"]),
            execute_at=execute_at,
            cron_expression=cron_expression,
            interval_seconds=interval_seconds,
            trigger_type=trigger_type,
            event_type=event_type,
            event_filter=event_filter,
            description=data.get("description") or template["description"],
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=400, detail=_storage_error_detail(e))

    logger.info(f"✅ Created automation from template: {template_id}")
    return automation_to_response(automation)


# ==================== Router ====================

# ==================== Предложения автоматизации (SIMA P2) ====================
# Tessbrain предлагает автоматизировать процесс → «Принять» формирует ТЗ и
# заводит проект в SIMA (звено D, seed-from-spec). «Отклонить» — прячет.


class ProposalCreateRequest(BaseModel):
    title: str
    problem: Optional[str] = ""
    proposed_action: Optional[str] = ""
    expected_benefit: Optional[str] = ""
    evidence: Optional[str] = ""
    source: Optional[str] = "manual"


@get("/proposals")
async def list_automation_proposals(
    user_id: Optional[str] = None,
    status: str = "pending",
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict:
    """Лента предложений автоматизации пользователя (по умолчанию — pending)."""
    from backend.core.automations.proposals import list_proposals
    uid = _auth_user(authorization, user_id)
    if not uid:
        return {"proposals": []}
    items = await list_proposals(uid, status=status)
    return {"proposals": items, "total": len(items)}


@post("/proposals")
async def create_automation_proposal(
    data: ProposalCreateRequest,
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict:
    """Создать предложение (детектор потребности или ручное). Дедуп по title."""
    from backend.core.automations.proposals import create_proposal
    uid = _auth_user(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="auth required")
    p = await create_proposal(
        uid, data.title,
        problem=data.problem or "", proposed_action=data.proposed_action or "",
        expected_benefit=data.expected_benefit or "", evidence=data.evidence or "",
        source=data.source or "manual",
    )
    if not p:
        return {"error": "title is required"}
    return {"proposal": p}


@post("/proposals/{proposal_id:str}/accept")
async def accept_automation_proposal(
    proposal_id: str,
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict:
    """Согласие пользователя: формируем ТЗ из предложения и заводим проект в
    SIMA (звено D). Возвращаем { proposal, project }. Идемпотентно: повторный
    accept уже принятого вернёт связанный проект без повторного создания."""
    from backend.core.automations.proposals import (
        build_spec_text, get_proposal, mark_accepted,
    )
    from backend.core.sima.ai_service import seed_project_from_spec
    uid = _auth_user(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="auth required")

    proposal = await get_proposal(proposal_id, uid)
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal.get("status") == "accepted" and proposal.get("simaProjectId"):
        return {"proposal": proposal, "project": {"id": proposal["simaProjectId"]},
                "message": "уже принято"}
    if proposal.get("status") == "dismissed":
        raise HTTPException(status_code=409, detail="proposal was dismissed")

    spec = build_spec_text(proposal)
    seeded = await seed_project_from_spec(
        uid, tz_text=spec, title=proposal.get("title"),
        description=proposal.get("problem") or None,
    )
    if seeded.get("error"):
        return {"error": seeded["error"]}
    project = seeded.get("project") or {}
    project_id = str(project.get("id") or "")
    updated = await mark_accepted(proposal_id, uid, project_id) if project_id else None
    return {
        "proposal": updated or proposal,
        "project": project,
        "actions": seeded.get("actions", []),
        "message": seeded.get("message", "Проект создан из предложения"),
    }


@post("/proposals/{proposal_id:str}/dismiss")
async def dismiss_automation_proposal(
    proposal_id: str,
    user_id: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict:
    """Отклонить предложение (спрятать из ленты)."""
    from backend.core.automations.proposals import mark_dismissed
    uid = _auth_user(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="auth required")
    p = await mark_dismissed(proposal_id, uid)
    return {"proposal": p, "dismissed": bool(p)}


@post("/proposals/detect")
async def detect_automation_proposals(
    user_id: Optional[str] = None,
    max_items: int = 3,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict:
    """Звено A по требованию: Tessbrain сам находит, что стоит автоматизировать
    (по сводке компании), и заводит предложения в ленту. Дедуп в store —
    повторный прогон не плодит дубли. Возвращает { created, count }."""
    from backend.core.automations.opportunity_detector import run_detection
    uid = _auth_user(authorization, user_id)
    if not uid:
        raise HTTPException(status_code=401, detail="auth required")
    return await run_detection(uid, max_items=max_items, source="manual_detect")


router = Router(
    path="/automations",
    route_handlers=[
        # Специфичные роуты ПЕРВЫМИ (до параметризованных)
        get_action_types,        # /action-types
        list_automation_proposals,   # /proposals (SIMA P2)
        create_automation_proposal,  # /proposals (POST)
        detect_automation_proposals, # /proposals/detect (звено A — авто-детектор)
        accept_automation_proposal,  # /proposals/{id}/accept → ТЗ → SIMA
        dismiss_automation_proposal, # /proposals/{id}/dismiss
        get_projects_for_automation,  # /projects-list — для фильтра «из папки»
        list_recipient_groups,       # /recipient-groups
        create_recipient_group,
        delete_recipient_group,
        get_templates,           # /templates
        get_template,            # /templates/{template_id}
        create_from_template,    # /from-template
        list_all_tasks,          # /all - все задачи из обеих таблиц
        get_meetings_for_automation,    # /meetings-list - встречи для выбора
        get_meeting_tasks_for_automation, # /meeting-tasks - задачи встречи (vibe-tasking)
        get_integrations_for_automation, # /integrations-list - интеграции для выбора

        # Основные CRUD
        list_automations,        # /
        create_automation,       # / (POST)

        # Параметризованные роуты ПОСЛЕДНИМИ
        get_automation,          # /{automation_id}
        update_automation,       # /{automation_id} (PATCH)
        delete_automation,       # /{automation_id} (DELETE)
        pause_automation,        # /{automation_id}/pause
        resume_automation,       # /{automation_id}/resume
        cancel_automation,       # /{automation_id}/cancel
        run_automation_now,      # /{automation_id}/run
        rerun_automation,        # /{automation_id}/rerun
        reactivate_automation,   # /{automation_id}/reactivate
        get_automation_logs,     # /{automation_id}/logs
    ],
    tags=["Automations"],
)

