# -*- coding: utf-8 -*-
"""
Automation Service - управление отложенными и повторяющимися задачами.

Поддерживает:
- Одноразовые отложенные задачи (execute_at)
- Повторяющиеся задачи (cron или interval)
- CRUD операции
- Выполнение через агентов автоматизации
"""
import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from croniter import croniter

logger = logging.getLogger(__name__)

# Supabase config
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# MeetFlow path for direct execution
from backend.core.utils.meetflow_path import default_meetflow_path as _dmp
MEETFLOW_PATH = _dmp()


class ScheduleType(str, Enum):
    ONCE = "once"
    RECURRING = "recurring"


class AutomationStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionType(str, Enum):
    SEND_TELEGRAM = "send_telegram"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    CREATE_NOTION_PAGE = "create_notion_page"
    SEND_EMAIL = "send_email"
    CREATE_YOUGILE_TASK = "create_yougile_task"
    CUSTOM_AGENT_TASK = "custom_agent_task"
    # LLM Processing
    LLM_PROCESS = "llm_process"
    LLM_EXTRACT_AND_SEND = "llm_extract_and_send"
    # New integrations (via IntegrationRegistry)
    SEND_SLACK = "send_slack"
    CREATE_JIRA_ISSUE = "create_jira_issue"
    CREATE_TRELLO_CARD = "create_trello_card"
    SEND_GMAIL = "send_gmail"
    CREATE_GITHUB_ISSUE = "create_github_issue"
    CREATE_LINEAR_ISSUE = "create_linear_issue"
    # Universal integration action (delegates to any registered integration tool)
    INTEGRATION_TOOL = "integration_tool"
    # Meeting workflows
    MEETING_SUMMARY_TELEGRAM = "meeting_summary_telegram"
    MEETING_TASKS_YOUGILE = "meeting_tasks_yougile"
    MEETING_REPORT_EMAIL = "meeting_report_email"
    MEETING_NOTES_NOTION = "meeting_notes_notion"
    CLIENT_FOLLOWUP = "client_followup"
    HR_CANDIDATE_REPORT = "hr_candidate_report"
    MEETING_DOCUMENT = "meeting_document"  # собрать документ (КП/договор) по встрече
    RUN_ANALYSIS = "run_analysis"  # запустить методику разбора (Анализ) по документам
    RUN_MEETING_PIPELINE = "run_meeting_pipeline"  # vibe-tasking: задачи встречи → готовые ТЗ → доставка/гейт/исполнитель
    SCHEDULED_RESEARCH = "scheduled_research"  # исследование сайтов/новостей/каналов под углом X → отчёт по расписанию
    CHAT_DIGEST = "chat_digest"  # дайджест командного чата (Slack / TG-группа) за период
    COMMITMENTS_TRACKER = "commitments_tracker"
    CLIENT_RISK_ALERT = "client_risk_alert"
    SALES_CALL_SCORECARD = "sales_call_scorecard"
    VOICE_OF_CUSTOMER = "voice_of_customer"
    ONE_ON_ONE_PULSE = "one_on_one_pulse"
    CALL_QA_REVIEW = "call_qa_review"
    EXEC_WEEKLY_BRIEF = "exec_weekly_brief"
    DAILY_MEETINGS_DIGEST = "daily_meetings_digest"
    MORNING_BRIEFING = "morning_briefing"
    WEEKLY_TASKS_REPORT = "weekly_tasks_report"
    WEEKLY_MEETINGS_SUMMARY = "weekly_meetings_summary"
    SYNC_OVERDUE_TASKS = "sync_overdue_tasks"
    DEADLINE_REMINDER = "deadline_reminder"
    # Запись в CRM (amoCRM/Bitrix24/HubSpot/Pipedrive) — тот же модуль и тот же
    # deny-by-default env-гейт (ENABLE_CRM_WRITEBACK), что у узла доски crm_write.
    CRM_WRITE = "crm_write"


@dataclass
class ScheduledAutomation:
    """Модель отложенной/повторяющейся задачи"""
    id: str
    user_id: str
    name: str
    action_type: str
    action_data: Dict[str, Any]
    schedule_type: str = "once"

    # Планирование
    execute_at: Optional[str] = None
    cron_expression: Optional[str] = None
    interval_seconds: Optional[int] = None

    # Event-triggered: тип триггера и событие
    trigger_type: str = "schedule"  # "schedule" | "event" | "chain"
    event_type: Optional[str] = None  # Для trigger_type="event": meeting_ended, task_overdue, etc.
    event_filter: Optional[Dict[str, Any]] = None  # Фильтр на payload события

    # Условия (conditions): проверяются ПЕРЕД выполнением
    conditions: Optional[List[Dict[str, Any]]] = None

    # Цепочки (chains): следующая автоматизация после выполнения
    chain_next: Optional[str] = None  # ID следующей автоматизации в цепочке
    chain_parent: Optional[str] = None  # ID родительской автоматизации
    chain_pass_result: bool = False  # Передавать результат в action_data следующего шага

    # Статус
    status: str = "active"
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None
    last_run_result: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    run_count: int = 0

    # Ограничения
    max_runs: Optional[int] = None
    expires_at: Optional[str] = None
    # Подряд идущие ошибки: после N — автопауза (анти-«зомби», см. execute)
    failure_streak: int = 0

    # LLM Usage Tracking
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    last_run_tokens: int = 0
    last_run_cost_usd: float = 0.0

    # Метаданные
    description: Optional[str] = None
    source_message: Optional[str] = None
    session_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduledAutomation":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            user_id=data.get("user_id", ""),
            name=data.get("name", ""),
            action_type=data.get("action_type", ""),
            action_data=data.get("action_data", {}),
            schedule_type=data.get("schedule_type", "once"),
            execute_at=data.get("execute_at"),
            cron_expression=data.get("cron_expression"),
            interval_seconds=data.get("interval_seconds"),
            # Event-triggered
            trigger_type=data.get("trigger_type", "schedule"),
            event_type=data.get("event_type"),
            event_filter=data.get("event_filter"),
            # Conditions
            conditions=data.get("conditions"),
            # Chains
            chain_next=data.get("chain_next"),
            chain_parent=data.get("chain_parent"),
            chain_pass_result=data.get("chain_pass_result", False),
            # Status
            status=data.get("status", "active"),
            last_run_at=data.get("last_run_at"),
            last_run_status=data.get("last_run_status"),
            last_run_result=data.get("last_run_result"),
            last_error=data.get("last_error"),
            run_count=data.get("run_count", 0),
            max_runs=data.get("max_runs"),
            expires_at=data.get("expires_at"),
            failure_streak=data.get("failure_streak", 0),
            total_tokens_used=data.get("total_tokens_used", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            last_run_tokens=data.get("last_run_tokens", 0),
            last_run_cost_usd=data.get("last_run_cost_usd", 0.0),
            description=data.get("description"),
            source_message=data.get("source_message"),
            session_id=data.get("session_id"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class AutomationService:
    """Сервис управления автоматизациями"""

    def __init__(self):
        self.supabase_url = SUPABASE_URL
        self.supabase_key = SUPABASE_KEY
        self._headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None
    ) -> Any:
        """Выполнить HTTP запрос к Supabase с ретраем на транзиентных обрывах.

        Планировщик дёргает Supabase каждые 10с; под нагрузкой соединение
        рвётся («Server disconnected without sending a response») и сыпались
        `Scheduler check error`. Ретраим сетевые ошибки/5xx с backoff; 4xx
        (наша ошибка запроса) пробрасываем сразу.
        """
        url = f"{self.supabase_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(3):  # 1 попытка + 2 ретрая
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=self._headers,
                        params=params,
                        json=json_data
                    )
                    response.raise_for_status()
                    if response.status_code == 204:
                        return None
                    return response.json()
            except httpx.HTTPStatusError as e:
                # 4xx — ошибка запроса, ретрай бессмыслен; 5xx — транзиентно.
                if e.response is not None and 400 <= e.response.status_code < 500:
                    # Тело ошибки Supabase в лог — иначе «400 Bad Request»
                    # невозможно диагностировать (PGRST204 и т.п.)
                    logger.error("Supabase %s %s -> %s: %s",
                                 method, path, e.response.status_code,
                                 (e.response.text or "")[:500])
                    raise
                last_exc = e
            except (httpx.TransportError, httpx.TimeoutException) as e:
                # ConnectError/ReadError/RemoteProtocolError(disconnected)/timeout
                last_exc = e
            if attempt < 2:
                await asyncio.sleep(0.5 * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    # ==================== CRUD Operations ====================

    async def create_automation(
        self,
        user_id: str,
        name: str,
        action_type: str,
        action_data: Dict[str, Any],
        schedule_type: str = "once",
        execute_at: Optional[datetime] = None,
        cron_expression: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        description: Optional[str] = None,
        max_runs: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        source_message: Optional[str] = None,
        session_id: Optional[str] = None,
        # Event-triggered
        trigger_type: str = "schedule",
        event_type: Optional[str] = None,
        event_filter: Optional[Dict[str, Any]] = None,
        # Conditions
        conditions: Optional[List[Dict[str, Any]]] = None,
        # Chains
        chain_next: Optional[str] = None,
        chain_parent: Optional[str] = None,
        chain_pass_result: bool = False,
    ) -> ScheduledAutomation:
        """Создать новую автоматизацию"""

        automation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        data = {
            "id": automation_id,
            "user_id": user_id,
            "name": name,
            "action_type": action_type,
            "action_data": action_data,
            "schedule_type": schedule_type,
            "status": "active",
            "run_count": 0,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        if execute_at:
            data["execute_at"] = execute_at.isoformat()
        if cron_expression:
            data["cron_expression"] = cron_expression
        if interval_seconds:
            data["interval_seconds"] = interval_seconds
        if description:
            data["description"] = description
        # Event-triggered fields
        if trigger_type != "schedule":
            data["trigger_type"] = trigger_type
        if event_type:
            data["event_type"] = event_type
        if event_filter:
            data["event_filter"] = event_filter
        # Conditions
        if conditions:
            data["conditions"] = conditions
        # Chains
        if chain_next:
            data["chain_next"] = chain_next
        if chain_parent:
            data["chain_parent"] = chain_parent
        if chain_pass_result:
            data["chain_pass_result"] = chain_pass_result
        if max_runs:
            data["max_runs"] = max_runs
        if expires_at:
            data["expires_at"] = expires_at.isoformat()
        if source_message:
            data["source_message"] = source_message
        if session_id:
            data["session_id"] = session_id

        try:
            result = await self._request("POST", "/rest/v1/scheduled_automations", json_data=data)
        except httpx.HTTPStatusError as e:
            # Graceful degradation: в базе может не быть колонок событийных
            # триггеров/цепочек (миграция 090 не прогнана) — PostgREST отвечает
            # 400 PGRST204 «Could not find the '...' column». Повторяем insert
            # без опциональных колонок: расписание сохранится, event-подписка
            # ниже встанет в памяти (до рестарта). И громко просим миграцию.
            body = (e.response.text or "") if e.response is not None else ""
            _optional = ("trigger_type", "event_type", "event_filter",
                         "conditions", "chain_next", "chain_parent",
                         "chain_pass_result")
            _missing = [c for c in _optional if c in data and f"'{c}'" in body]
            if e.response is not None and e.response.status_code == 400 and _missing:
                logger.error(
                    "⚠️ В scheduled_automations нет колонок %s — прогоните "
                    "миграцию backend/db/migrations/090_event_trigger_columns.sql. "
                    "Создаю автоматизацию без них (event-подписка доживёт до рестарта).",
                    _missing)
                for c in _optional:
                    data.pop(c, None)
                result = await self._request(
                    "POST", "/rest/v1/scheduled_automations", json_data=data)
            else:
                raise

        automation = ScheduledAutomation.from_dict(result[0]) if result and len(result) > 0 else ScheduledAutomation.from_dict(data)

        # Если это event-triggered автоматизация — регистрируем в EventBus
        if trigger_type == "event" and event_type:
            try:
                from backend.core.automations.event_bus import get_event_bus
                bus = get_event_bus()
                bus.register_event_automation(
                    user_id=user_id,
                    event_type=event_type,
                    automation_id=automation.id,
                    filter_conditions=event_filter,
                )
                logger.info(f"[AutomationService] Registered event automation '{name}' for event '{event_type}'")
            except Exception as eb_err:
                logger.warning(f"[AutomationService] Failed to register in EventBus: {eb_err}")

        return automation

    async def get_automation(self, automation_id: str) -> Optional[ScheduledAutomation]:
        """Получить автоматизацию по ID"""
        params = {"id": f"eq.{automation_id}", "select": "*"}
        result = await self._request("GET", "/rest/v1/scheduled_automations", params=params)
        if result and len(result) > 0:
            return ScheduledAutomation.from_dict(result[0])
        return None

    async def list_automations(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        schedule_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ScheduledAutomation]:
        """Получить список автоматизаций"""
        params = {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
            "offset": str(offset),
        }

        if user_id:
            params["user_id"] = f"eq.{user_id}"
        if status:
            params["status"] = f"eq.{status}"
        if schedule_type:
            params["schedule_type"] = f"eq.{schedule_type}"

        result = await self._request("GET", "/rest/v1/scheduled_automations", params=params)
        return [ScheduledAutomation.from_dict(r) for r in (result or [])]

    async def update_automation(
        self,
        automation_id: str,
        updates: Dict[str, Any]
    ) -> Optional[ScheduledAutomation]:
        """Обновить автоматизацию"""
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Фильтруем только известные поля таблицы
        # Базовые поля из миграции 001
        allowed_fields = {
            "name", "description", "action_type", "action_data",
            "schedule_type", "execute_at", "cron_expression", "interval_seconds",
            "status", "last_run_at", "last_run_status", "last_run_result", "last_error",
            "run_count", "max_runs", "expires_at", "updated_at",
            "source_message", "session_id"
        }
        # LLM tracking поля из миграции 002
        llm_tracking_fields = {"total_tokens_used", "total_cost_usd", "last_run_tokens", "last_run_cost_usd"}
        # Включаем LLM tracking — при отсутствии колонок Supabase просто проигнорирует эти поля
        # Если ошибка — fallback на базовые поля (см. except ниже)
        allowed_fields.update(llm_tracking_fields)

        filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}

        try:
            result = await self._request(
                "PATCH",
                f"/rest/v1/scheduled_automations?id=eq.{automation_id}",
                json_data=filtered_updates
            )

            if result and len(result) > 0:
                return ScheduledAutomation.from_dict(result[0])
            return None
        except Exception as e:
            # Fallback: если ошибка из-за LLM tracking колонок — повторить без них
            if any(f in str(e) for f in ("total_tokens_used", "total_cost_usd", "last_run_tokens", "last_run_cost_usd", "column")):
                logger.warning(f"LLM tracking fields not available in DB, retrying without them: {e}")
                fallback_updates = {k: v for k, v in filtered_updates.items() if k not in llm_tracking_fields}
                try:
                    result = await self._request(
                        "PATCH",
                        f"/rest/v1/scheduled_automations?id=eq.{automation_id}",
                        json_data=fallback_updates
                    )
                    if result and len(result) > 0:
                        return ScheduledAutomation.from_dict(result[0])
                    return None
                except Exception as e2:
                    logger.error(f"Failed to update automation {automation_id} (fallback): {e2}")
                    raise
            logger.error(f"Failed to update automation {automation_id}: {e}")
            logger.error(f"Updates: {filtered_updates}")
            raise

    async def delete_automation(self, automation_id: str) -> bool:
        """Удалить автоматизацию"""
        try:
            await self._request("DELETE", f"/rest/v1/scheduled_automations?id=eq.{automation_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete automation {automation_id}: {e}")
            return False

    async def pause_automation(self, automation_id: str) -> Optional[ScheduledAutomation]:
        """Приостановить автоматизацию"""
        return await self.update_automation(automation_id, {"status": "paused"})

    async def resume_automation(self, automation_id: str) -> Optional[ScheduledAutomation]:
        """Возобновить автоматизацию"""
        return await self.update_automation(automation_id, {"status": "active"})

    async def cancel_automation(self, automation_id: str) -> Optional[ScheduledAutomation]:
        """Отменить автоматизацию"""
        return await self.update_automation(automation_id, {"status": "cancelled"})

    # ==================== Execution ====================

    async def get_due_automations(self) -> List[ScheduledAutomation]:
        """Получить автоматизации, готовые к выполнению"""
        now = datetime.now(timezone.utc).isoformat()

        # Одноразовые задачи с наступившим временем
        params_once = {
            "status": "eq.active",
            "schedule_type": "eq.once",
            "execute_at": f"lte.{now}",
            "select": "*"
        }
        once_tasks = await self._request("GET", "/rest/v1/scheduled_automations", params=params_once)

        # Повторяющиеся задачи (проверяем cron/interval)
        params_recurring = {
            "status": "eq.active",
            "schedule_type": "eq.recurring",
            "select": "*"
        }
        recurring_tasks = await self._request("GET", "/rest/v1/scheduled_automations", params=params_recurring)

        due_tasks = []

        # Добавляем одноразовые
        for task in (once_tasks or []):
            due_tasks.append(ScheduledAutomation.from_dict(task))

        # Проверяем повторяющиеся
        for task in (recurring_tasks or []):
            automation = ScheduledAutomation.from_dict(task)
            if self._is_recurring_due(automation):
                due_tasks.append(automation)

        return due_tasks

    def _is_recurring_due(self, automation: ScheduledAutomation) -> bool:
        """Проверить, пора ли выполнять повторяющуюся задачу"""
        now = datetime.now(timezone.utc)

        # Проверка expires_at
        if automation.expires_at:
            expires = datetime.fromisoformat(automation.expires_at.replace("Z", "+00:00"))
            if now > expires:
                return False

        # Проверка max_runs
        if automation.max_runs and automation.run_count >= automation.max_runs:
            return False

        last_run = None
        if automation.last_run_at:
            last_run = datetime.fromisoformat(automation.last_run_at.replace("Z", "+00:00"))

        # Проверка по cron
        if automation.cron_expression:
            try:
                # (фикс аудита) created_at из Supabase — ISO-СТРОКА; croniter
                # со строкой бросал исключение → return False → cron-задача
                # никогда не срабатывала в первый раз (а без первого запуска
                # не появлялся last_run_at — вечный тупик).
                base = last_run
                if base is None:
                    raw = automation.created_at
                    if isinstance(raw, str):
                        try:
                            base = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        except ValueError:
                            base = now - timedelta(days=1)
                    else:
                        base = raw or (now - timedelta(days=1))
                cron = croniter(automation.cron_expression, base)
                next_run = cron.get_next(datetime)
                if isinstance(next_run, datetime):
                    # Делаем timezone-aware если нужно
                    if next_run.tzinfo is None:
                        next_run = next_run.replace(tzinfo=timezone.utc)
                    return now >= next_run
            except Exception as e:
                logger.error(f"Invalid cron expression: {automation.cron_expression}, error: {e}")
                return False

        # Проверка по интервалу
        if automation.interval_seconds:
            if not last_run:
                return True  # Никогда не запускалась
            next_run = last_run + timedelta(seconds=automation.interval_seconds)
            return now >= next_run

        return False

    async def check_conditions(self, automation: ScheduledAutomation) -> bool:
        """
        Проверить условия (conditions) автоматизации.

        Условия — список проверок, которые должны быть TRUE для выполнения.
        Если хотя бы одно условие FALSE — автоматизация пропускается.

        Формат conditions:
        [
            {"type": "has_data", "source": "meetings", "min_count": 1},
            {"type": "time_range", "after": "08:00", "before": "22:00"},
            {"type": "day_of_week", "days": [1,2,3,4,5]},
            {"type": "custom_check", "tool_name": "get_meetflow_meetings", "check": "non_empty"},
        ]

        Returns:
            True если все условия выполнены, False если хотя бы одно нет
        """
        if not automation.conditions:
            return True  # Нет условий — всегда выполняется

        now = datetime.now(timezone.utc)

        for condition in automation.conditions:
            cond_type = condition.get("type", "")

            if cond_type == "time_range":
                # Проверка временного окна
                after = condition.get("after", "00:00")
                before = condition.get("before", "23:59")
                current_time = now.strftime("%H:%M")
                if not (after <= current_time <= before):
                    logger.info(f"[Conditions] Time range check failed: {current_time} not in {after}-{before}")
                    return False

            elif cond_type == "day_of_week":
                # Проверка дня недели (0=Mon, 6=Sun)
                allowed_days = condition.get("days", [0, 1, 2, 3, 4, 5, 6])
                if now.weekday() not in allowed_days:
                    logger.info(f"[Conditions] Day of week check failed: {now.weekday()} not in {allowed_days}")
                    return False

            elif cond_type == "has_data":
                # Проверка наличия данных (не пустой результат от source)
                source = condition.get("source", "")
                min_count = condition.get("min_count", 1)

                try:
                    from backend.integrations.tessent_brain_tools import TessentBrainTools
                    tools = TessentBrainTools(user_id=automation.user_id)

                    if source == "meetings":
                        result_str = await tools.get_meetflow_meetings(days_back=1)
                        data = json.loads(result_str) if isinstance(result_str, str) else result_str
                        count = len(data.get("meetings", [])) if isinstance(data, dict) else 0
                    elif source == "tasks":
                        result_str = await tools.get_all_tasks(limit=5)
                        data = json.loads(result_str) if isinstance(result_str, str) else result_str
                        count = data.get("total", 0) if isinstance(data, dict) else 0
                    else:
                        count = 1  # Неизвестный source — пропускаем проверку

                    if count < min_count:
                        logger.info(f"[Conditions] has_data check failed: {source} count={count} < min={min_count}")
                        return False
                except Exception as e:
                    logger.warning(f"[Conditions] has_data check error: {e}")
                    # При ошибке проверки — выполняем (fail-open)

            elif cond_type == "min_interval_hours":
                # Минимальный интервал с последнего выполнения
                min_hours = condition.get("hours", 1)
                if automation.last_run_at:
                    last_run = datetime.fromisoformat(automation.last_run_at.replace("Z", "+00:00"))
                    hours_since = (now - last_run).total_seconds() / 3600
                    if hours_since < min_hours:
                        logger.info(f"[Conditions] min_interval check failed: {hours_since:.1f}h < {min_hours}h")
                        return False

            else:
                logger.warning(f"[Conditions] Unknown condition type: {cond_type}")

        return True

    async def execute_automation(self, automation: ScheduledAutomation) -> Dict[str, Any]:
        """Выполнить автоматизацию"""
        start_time = datetime.now(timezone.utc)
        result = {"success": False, "message": "", "data": None}

        try:
            # Проверяем условия (conditions) перед выполнением
            if automation.conditions:
                conditions_met = await self.check_conditions(automation)
                if not conditions_met:
                    logger.info(f"⏭️ Skipping automation '{automation.name}': conditions not met")
                    # Обновляем last_run_at, но НЕ run_count (не считаем как выполнение)
                    await self.update_automation(automation.id, {
                        "last_run_at": start_time.isoformat(),
                        "last_run_status": "skipped",
                        "last_run_result": {"skipped": True, "reason": "conditions not met"},
                    })
                    return {"success": True, "message": "Skipped: conditions not met", "skipped": True}

            logger.info(f"🚀 Executing automation: {automation.name} ({automation.action_type})")

            # Устанавливаем контекст пользователя для automation_tools_async
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)
            from automation_tools_async import set_user_context
            set_user_context(user_id=automation.user_id)
            logger.info(f"📤 Set user context: user_id={automation.user_id}")

            # P4: write-back проходит через контракт Ontology Action.
            # WARN-режим: только наблюдаемость (форма payload), НИКОГДА
            # не блокирует исполнение — careful rollout. Обёрнуто, чтобы
            # SDK не мог уронить автоматизацию.
            try:
                from backend.core.ontology.actions import (
                    ACTION_WRITEBACK,
                    Action,
                    apply_actions,
                )
                from backend.core.ontology.sdk import EnforcementMode

                _wb = Action(
                    kind=ACTION_WRITEBACK,
                    target=str(automation.action_type),
                    payload=automation.action_data
                    if isinstance(automation.action_data, dict) else {},
                )

                async def _noop_exec(*, kind, target, payload):
                    return True  # реальное исполнение — ниже по диспетчеру

                _rep = await apply_actions(
                    [_wb], executor=_noop_exec, mode=EnforcementMode.WARN,
                )
                if _rep.rejected:
                    logger.warning(
                        f"⚠️ Ontology Action check: write-back "
                        f"'{automation.action_type}' shape issues "
                        f"(rejected={_rep.rejected})"
                    )
            except Exception as _exc:
                logger.debug(f"ontology action precheck skipped: {_exc}")

            # Выполняем действие в зависимости от типа
            if automation.action_type == ActionType.SEND_TELEGRAM.value:
                result = await self._execute_telegram(automation.action_data)
            elif automation.action_type == ActionType.CREATE_CALENDAR_EVENT.value:
                result = await self._execute_calendar(automation.action_data)
            elif automation.action_type == ActionType.CREATE_NOTION_PAGE.value:
                result = await self._execute_notion(automation.action_data)
            elif automation.action_type == ActionType.SEND_EMAIL.value:
                result = await self._execute_email(automation.action_data)
            elif automation.action_type == ActionType.CREATE_YOUGILE_TASK.value:
                result = await self._execute_yougile(automation.action_data)
            elif automation.action_type == ActionType.CUSTOM_AGENT_TASK.value:
                result = await self._execute_custom_agent(automation.action_data, automation.user_id)
            elif automation.action_type == ActionType.LLM_PROCESS.value:
                result = await self._execute_llm_process(automation.action_data, automation.user_id, automation.id)
            elif automation.action_type == ActionType.LLM_EXTRACT_AND_SEND.value:
                result = await self._execute_llm_extract_and_send(automation.action_data, automation.user_id, automation.id)
            elif automation.action_type == ActionType.RUN_ANALYSIS.value:
                result = await self._execute_run_analysis(automation.action_data, automation.user_id)
            elif automation.action_type == ActionType.RUN_MEETING_PIPELINE.value:
                result = await self._execute_run_meeting_pipeline(automation.action_data, automation.user_id, automation.id)
            elif automation.action_type == ActionType.SCHEDULED_RESEARCH.value:
                result = await self._execute_scheduled_research(automation.action_data, automation.user_id, automation.id)
            elif automation.action_type == ActionType.CHAT_DIGEST.value:
                result = await self._execute_chat_digest(automation.action_data, automation.user_id, automation.id)
            # New integration actions (via IntegrationRegistry)
            elif automation.action_type == ActionType.CRM_WRITE.value:
                result = await self._execute_crm_write(automation.action_data, automation.user_id)
            elif automation.action_type in (
                ActionType.SEND_SLACK.value,
                ActionType.CREATE_JIRA_ISSUE.value,
                ActionType.CREATE_TRELLO_CARD.value,
                ActionType.SEND_GMAIL.value,
                ActionType.CREATE_GITHUB_ISSUE.value,
                ActionType.CREATE_LINEAR_ISSUE.value,
                ActionType.INTEGRATION_TOOL.value,
            ):
                result = await self._execute_integration_tool(automation.action_type, automation.action_data, automation.user_id)
            elif automation.action_type in [
                ActionType.MEETING_SUMMARY_TELEGRAM.value,
                ActionType.MEETING_TASKS_YOUGILE.value,
                ActionType.MEETING_REPORT_EMAIL.value,
                ActionType.MEETING_NOTES_NOTION.value,
                ActionType.CLIENT_FOLLOWUP.value,
                ActionType.HR_CANDIDATE_REPORT.value,
                ActionType.MEETING_DOCUMENT.value,
                ActionType.COMMITMENTS_TRACKER.value,
                ActionType.CLIENT_RISK_ALERT.value,
                ActionType.SALES_CALL_SCORECARD.value,
                ActionType.VOICE_OF_CUSTOMER.value,
                ActionType.ONE_ON_ONE_PULSE.value,
                ActionType.CALL_QA_REVIEW.value,
                ActionType.EXEC_WEEKLY_BRIEF.value,
                ActionType.DAILY_MEETINGS_DIGEST.value,
                ActionType.MORNING_BRIEFING.value,
                ActionType.WEEKLY_TASKS_REPORT.value,
                ActionType.WEEKLY_MEETINGS_SUMMARY.value,
                ActionType.SYNC_OVERDUE_TASKS.value,
                ActionType.DEADLINE_REMINDER.value,
            ]:
                result = await self._execute_meeting_workflow(automation.action_type, automation.action_data, automation.user_id)
            else:
                result = {"success": False, "message": f"Unknown action type: {automation.action_type}"}

            # Обновляем статус
            end_time = datetime.now(timezone.utc)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            # Сериализуем result для хранения в JSONB
            try:
                serialized_result = json.loads(json.dumps(result, default=str))
            except Exception:
                serialized_result = {"success": result.get("success", False), "message": str(result.get("message", ""))}

            updates = {
                "last_run_at": end_time.isoformat(),
                "last_run_status": "success" if result.get("success") else "failed",
                "last_run_result": serialized_result,
                "run_count": automation.run_count + 1,
            }

            # Track LLM usage if present in result - используем глобальный UsageTracker
            llm_usage = result.get("llm_usage", {})
            if llm_usage:
                try:
                    from backend.core.llm.usage_tracker import track_usage
                    track_usage(
                        provider="openai",
                        model=llm_usage.get("model", "gpt-4o-mini"),
                        input_tokens=llm_usage.get("input_tokens", 0),
                        output_tokens=llm_usage.get("output_tokens", 0),
                        agent_mode="automation",
                        request_type=automation.action_type,
                        user_id=automation.user_id,
                        session_id=automation.id,  # используем automation_id как session_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to track LLM usage: {e}")

            if not result.get("success"):
                updates["last_error"] = result.get("message", "Unknown error")

            # Анти-«зомби»: recurring-автоматизация при ошибке оставалась
            # active и ретраилась КАЖДЫЙ 10-сек тик вечно (без backoff).
            # Считаем ошибки ПОДРЯД; после порога — автопауза + last_error,
            # пользователь чинит и включает заново.
            _MAX_STREAK = int(os.getenv("TESSENT_AUTOMATION_MAX_FAILURES", "5"))
            if result.get("success"):
                updates["failure_streak"] = 0
            else:
                _streak = getattr(automation, "failure_streak", 0) + 1
                updates["failure_streak"] = _streak
                if automation.schedule_type != "once" and _streak >= _MAX_STREAK:
                    updates["status"] = "paused"
                    updates["last_error"] = (
                        f"Автопауза после {_streak} ошибок подряд: "
                        + str(result.get("message", ""))[:300])
                    logger.warning(
                        f"⏸ Automation {automation.name} auto-paused after "
                        f"{_streak} consecutive failures")

            # Для одноразовых задач - помечаем как completed
            if automation.schedule_type == "once":
                updates["status"] = "completed" if result.get("success") else "failed"

            # Проверяем max_runs для повторяющихся
            if automation.max_runs and (automation.run_count + 1) >= automation.max_runs:
                updates["status"] = "completed"

            await self.update_automation(automation.id, updates)

            # Логируем выполнение
            await self._log_execution(
                automation_id=automation.id,
                status="success" if result.get("success") else "failed",
                result=result,
                error_message=result.get("message") if not result.get("success") else None,
                started_at=start_time,
                completed_at=end_time,
                duration_ms=duration_ms
            )

            logger.info(f"✅ Automation {automation.name} completed: {result.get('success')}")

            # === Chain: запуск следующего шага в цепочке ===
            if result.get("success") and automation.chain_next:
                try:
                    next_automation = await self.get_automation(automation.chain_next)
                    if next_automation and next_automation.status == "active":
                        # Передаём результат текущего шага как _chain_input
                        if automation.chain_pass_result:
                            next_automation.action_data = dict(next_automation.action_data)
                            next_automation.action_data["_chain_input"] = result.get("data", {})
                            next_automation.action_data["_chain_parent_result"] = result

                        logger.info(f"🔗 Chain: triggering next step '{next_automation.name}' (id={automation.chain_next})")
                        asyncio.create_task(self.execute_automation(next_automation))
                    else:
                        logger.warning(f"🔗 Chain: next automation '{automation.chain_next}' not found or not active")
                except Exception as chain_err:
                    logger.error(f"🔗 Chain error: {chain_err}")

        except Exception as e:
            logger.error(f"❌ Automation {automation.name} failed: {e}")
            result = {"success": False, "message": str(e)}

            _streak = getattr(automation, "failure_streak", 0) + 1
            _MAX_STREAK = int(os.getenv("TESSENT_AUTOMATION_MAX_FAILURES", "5"))
            _status = automation.status
            if automation.schedule_type == "once":
                _status = "failed"
            elif _streak >= _MAX_STREAK:
                _status = "paused"  # анти-«зомби»: не ретраим вечно
                logger.warning(
                    f"⏸ Automation {automation.name} auto-paused after "
                    f"{_streak} consecutive failures (exception path)")
            await self.update_automation(automation.id, {
                "last_run_at": datetime.now(timezone.utc).isoformat(),
                "last_run_status": "failed",
                "last_error": str(e),
                "run_count": automation.run_count + 1,
                "failure_streak": _streak,
                "status": _status,
            })

        return result

    async def _log_execution(
        self,
        automation_id: str,
        status: str,
        result: Dict,
        error_message: Optional[str],
        started_at: datetime,
        completed_at: datetime,
        duration_ms: int
    ):
        """Записать лог выполнения"""
        try:
            log_data = {
                "id": str(uuid.uuid4()),
                "automation_id": automation_id,
                "status": status,
                "result": result,
                "error_message": error_message,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_ms": duration_ms,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            await self._request("POST", "/rest/v1/automation_execution_log", json_data=log_data)
        except Exception as e:
            logger.warning(f"Failed to log execution: {e}")

    async def purge_old_logs(self, days: int = 90) -> Dict[str, int]:
        """Удалить логи выполнений старше N дней (ретеншн). Логи
        automation_execution_log / agent_automation_logs копились без чистки.
        Вызывается ежедневно из планировщика; PostgREST-фильтр по created_at."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
        purged = {"automation_execution_log": 0, "agent_automation_logs": 0}
        for table in ("automation_execution_log", "agent_automation_logs"):
            try:
                # Prefer: count — PostgREST вернёт число затронутых в заголовке;
                # тут просто DELETE по created_at < cutoff (best-effort).
                await self._request(
                    "DELETE", f"/rest/v1/{table}",
                    params={"created_at": f"lt.{cutoff}"})
                purged[table] = 1  # успех (точное число не критично)
            except Exception as e:
                logger.debug("purge %s skipped: %s", table, e)
        logger.info("🧹 Ретеншн логов: удалены записи старше %d дн.", days)
        return purged

    # ==================== Action Executors ====================

    async def _execute_telegram(self, data: Dict) -> Dict:
        """Отправить сообщение в Telegram (поддержка множественных получателей)"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            from automation_tools_async import send_telegram_message

            message = data.get("message", "")

            # Поддержка множественных получателей
            chat_ids = data.get("chat_ids", [])
            if not chat_ids:
                # Fallback на одиночный chat_id
                single_id = data.get("chat_id", "")
                if single_id:
                    chat_ids = [single_id]

            if not chat_ids:
                return {"success": False, "message": "No recipients specified"}

            # Отправляем всем получателям
            results = []
            success_count = 0
            for chat_id in chat_ids:
                try:
                    result = await send_telegram_message(message=message, chat_id=chat_id)
                    result_data = json.loads(result)
                    results.append({"chat_id": chat_id, "success": result_data.get("success", False)})
                    if result_data.get("success"):
                        success_count += 1
                except Exception as e:
                    results.append({"chat_id": chat_id, "success": False, "error": str(e)})

            return {
                "success": success_count > 0,
                "message": f"Telegram message sent to {success_count}/{len(chat_ids)} recipients",
                "data": {
                    "recipients": len(chat_ids),
                    "success_count": success_count,
                    "results": results
                }
            }
        except Exception as e:
            return {"success": False, "message": f"Telegram error: {e}"}

    async def _execute_calendar(self, data: Dict) -> Dict:
        """Создать событие в Google Calendar"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            from automation_tools_async import create_calendar_event
            result = await create_calendar_event(
                title=data.get("title", ""),
                start_time=data.get("start_time", ""),
                duration_minutes=data.get("duration_minutes", 60),
                description=data.get("description", ""),
                attendees=data.get("attendees", [])
            )
            result_data = json.loads(result)
            return {"success": result_data.get("success", False), "message": "Calendar event created", "data": result_data}
        except Exception as e:
            return {"success": False, "message": f"Calendar error: {e}"}

    async def _execute_notion(self, data: Dict) -> Dict:
        """Создать страницу в Notion"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            from automation_tools_async import create_notion_page
            result = await create_notion_page(
                title=data.get("title", ""),
                content=data.get("content", ""),
                parent_page_id=data.get("parent_page_id", "")
            )
            result_data = json.loads(result)
            return {"success": "error" not in result_data, "message": "Notion page created", "data": result_data}
        except Exception as e:
            return {"success": False, "message": f"Notion error: {e}"}

    async def _execute_email(self, data: Dict) -> Dict:
        """Отправить email через Gmail (поддержка множественных получателей)"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            from automation_tools_async import send_email

            subject = data.get("subject", "")
            body = data.get("body", "")

            # Поддержка множественных получателей
            to_emails = data.get("to_emails", [])
            if not to_emails:
                # Fallback на одиночный to
                single_email = data.get("to", "")
                if single_email:
                    to_emails = [single_email]

            if not to_emails:
                return {"success": False, "message": "No recipients specified"}

            # Отправляем всем получателям
            results = []
            success_count = 0
            for email in to_emails:
                try:
                    result = await send_email(to=email, subject=subject, body=body)
                    result_data = json.loads(result)
                    results.append({"email": email, "success": result_data.get("success", False)})
                    if result_data.get("success"):
                        success_count += 1
                except Exception as e:
                    results.append({"email": email, "success": False, "error": str(e)})

            return {
                "success": success_count > 0,
                "message": f"Email sent to {success_count}/{len(to_emails)} recipients",
                "data": {
                    "recipients": len(to_emails),
                    "success_count": success_count,
                    "results": results
                }
            }
        except Exception as e:
            return {"success": False, "message": f"Email error: {e}"}

    async def _execute_yougile(self, data: Dict) -> Dict:
        """Создать задачу в YouGile"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            from automation_tools_async import create_task
            result = await create_task(
                title=data.get("title", ""),
                description=data.get("description", ""),
                deadline=data.get("deadline"),
                assignee=data.get("assignee")
            )
            result_data = json.loads(result)
            return {"success": "error" not in result_data, "message": "YouGile task created", "data": result_data}
        except Exception as e:
            return {"success": False, "message": f"YouGile error: {e}"}

    async def _execute_custom_agent(self, data: Dict, user_id: str) -> Dict:
        """Выполнить произвольную команду через агентов"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            from autogen_async import (
                create_automation_agents,
                create_automation_graph_dict,
                set_current_user_context,
                start_new_automation_chat,
            )

            message = data.get("message", "")
            if not message:
                return {"success": False, "message": "No message provided for custom agent task"}

            set_current_user_context(user_id=user_id)

            agents = create_automation_agents()
            graph = create_automation_graph_dict(agents)

            session_id = str(uuid.uuid4())
            main_group, _main_manager, _result, _user_proxy = await start_new_automation_chat(
                session_id=session_id,
                agents=agents,
                graph_dict=graph,
                message=message,
                openwebui_mode=True
            )

            # Извлекаем ответ
            response_text = None
            for msg in getattr(main_group, "messages", []):
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    name = msg.get("name", "")
                    if name != "User" and content and "TERMINATE" not in content:
                        response_text = content

            return {
                "success": True,
                "message": "Custom agent task completed",
                "data": {"response": response_text or "Task completed"}
            }
        except Exception as e:
            return {"success": False, "message": f"Custom agent error: {e}"}

    async def _execute_crm_write(self, data: Dict, user_id: str) -> Dict:
        """Запись в CRM из автоматизации — ровно тот же путь, что узел доски
        crm_write: deny-by-default (ENABLE_CRM_WRITEBACK), провайдеры
        amocrm/bitrix24/hubspot/pipedrive, операции create (сделка/лид) и
        note (комментарий к записи). Учётные данные — из env, как у доски."""
        try:
            from backend.core.ontology.crm_writeback import (
                get_writer,
                writeback_enabled,
            )
            if not writeback_enabled():
                return {"success": False,
                        "message": "Запись в CRM выключена (включите ENABLE_CRM_WRITEBACK на сервере)"}
            provider = str(data.get("provider") or "").strip().lower()
            op = str(data.get("op") or "create").strip().lower()
            writer = get_writer(provider)
            if not writer:
                return {"success": False,
                        "message": f"CRM-провайдер не поддержан для записи: {provider or '—'}"}
            if (env_err := writer.env_check()):
                return {"success": False, "message": f"CRM {provider}: {env_err}"}
            fields = {
                "name": str(data.get("name") or data.get("title") or "").strip(),
                "value": data.get("value"),
                "entity_id": str(data.get("entity_id") or "").strip(),
                "text": str(data.get("note_text") or data.get("text") or "").strip(),
            }
            res = await writer.write(op, fields)
            rid = (res or {}).get("id")
            url = (res or {}).get("url") or ""
            label = "Комментарий добавлен" if op == "note" else "Запись создана"
            return {"success": True,
                    "message": f"{label} в CRM ({provider}). ID: {rid}." + (f" {url}" if url else ""),
                    "crm_id": rid, "crm_url": url}
        except Exception as e:
            logger.error(f"crm_write automation failed: {e}")
            return {"success": False, "message": f"запись в CRM: {e}"}

    async def _execute_integration_tool(self, action_type: str, data: Dict, user_id: str) -> Dict:
        """
        Выполнить действие через IntegrationRegistry.

        Поддерживает:
        - send_slack: отправка в Slack
        - create_jira_issue: создание задачи в Jira
        - create_trello_card: создание карточки в Trello
        - send_gmail: отправка email
        - create_github_issue: создание issue в GitHub
        - integration_tool: универсальный — tool_name из action_data
        """
        try:
            from backend.integrations.registry import IntegrationRegistry

            registry = IntegrationRegistry()
            await registry.load_for_user(user_id)

            # Маппинг action_type → tool_name + kwargs
            tool_mapping = {
                ActionType.SEND_SLACK.value: ("slack_send_message", {
                    "channel": data.get("channel", ""),
                    "text": data.get("text", data.get("message", "")),
                }),
                ActionType.CREATE_JIRA_ISSUE.value: ("jira_create_issue", {
                    "project": data.get("project", ""),
                    "summary": data.get("summary", data.get("title", "")),
                    "description": data.get("description", ""),
                    "issue_type": data.get("issue_type", "Task"),
                    "priority": data.get("priority", ""),
                }),
                ActionType.CREATE_TRELLO_CARD.value: ("trello_create_card", {
                    "list_id": data.get("list_id", ""),
                    "name": data.get("name", data.get("title", "")),
                    "description": data.get("description", ""),
                    "due": data.get("due", ""),
                }),
                ActionType.SEND_GMAIL.value: ("gmail_send", {
                    "to": data.get("to", ""),
                    "subject": data.get("subject", ""),
                    "body": data.get("body", data.get("message", "")),
                    "cc": data.get("cc", ""),
                }),
                ActionType.CREATE_GITHUB_ISSUE.value: ("github_create_issue", {
                    "repo": data.get("repo", ""),
                    "title": data.get("title", ""),
                    "body": data.get("body", data.get("description", "")),
                    "labels": data.get("labels", ""),
                }),
                ActionType.CREATE_LINEAR_ISSUE.value: ("linear_create_issue", {
                    "title": data.get("title", data.get("name", "")),
                    "description": data.get("description", ""),
                    "team_id": data.get("team_id", ""),
                }),
            }

            if action_type == ActionType.INTEGRATION_TOOL.value:
                # Универсальный режим: tool_name и args берём из action_data
                tool_name = data.get("tool_name", "")
                tool_args = data.get("tool_args", {})
            elif action_type in tool_mapping:
                tool_name, tool_args = tool_mapping[action_type]
            else:
                return {"success": False, "message": f"Unknown integration action: {action_type}"}

            if not tool_name:
                return {"success": False, "message": "No tool_name specified in action_data"}

            # Выполняем
            result_json = await registry.execute_tool(tool_name, **tool_args)
            import json as _json
            result = _json.loads(result_json)

            success = result.get("success", not result.get("error"))
            return {
                "success": success,
                "message": f"Integration tool '{tool_name}' executed" if success else result.get("error", "Failed"),
                "data": result,
            }

        except Exception as e:
            logger.error(f"Integration tool execution error ({action_type}): {e}")
            return {"success": False, "message": f"Integration tool error: {e!s}"}

    async def _execute_meeting_workflow(self, workflow_type: str, data: Dict, user_id: str) -> Dict:
        """Выполнить автоматизацию для работы со встречами"""
        try:
            from backend.core.automations.meeting_workflows import get_workflow_executor

            executor = get_workflow_executor()
            result = await executor.execute_workflow(
                workflow_type=workflow_type,
                parameters=data,
                user_id=user_id
            )

            return result
        except Exception as e:
            logger.error(f"Meeting workflow error: {e}")
            return {"success": False, "message": f"Meeting workflow error: {e}"}

    async def _execute_llm_process(self, data: Dict, user_id: str, automation_id: str) -> Dict:
        """
        Обработать данные через LLM.

        action_data должен содержать:
        - prompt: Промпт для LLM (что нужно сделать с данными)
        - source_type: Тип источника данных (meeting, text, url)
        - source_id: ID источника (meeting_id, etc.) или текст напрямую
        - output_type: Куда отправить результат (telegram, email, notion, yougile)
        - output_target: ID цели (chat_id, email, page_id, column_id)
        - model_tier: Уровень модели (standard/premium) - опционально
        """
        try:
            from backend.core.llm.gemini_client import GeminiClient

            prompt = data.get("prompt", "")
            source_type = data.get("source_type", "text")
            source_id = data.get("source_id", "")
            source_text = data.get("source_text", "")
            output_type = data.get("output_type", "telegram")
            output_target = data.get("output_target", "")
            model_tier = data.get("model_tier", "standard")

            # Выбираем модель по tier
            TIER_MODELS = {
                "standard": "gemini-flash-lite-latest",
                "premium": "gemini-flash-latest"  # Новейшая модель Gemini 3 Flash
            }
            model = TIER_MODELS.get(model_tier, TIER_MODELS["standard"])

            if not prompt:
                return {"success": False, "message": "prompt is required"}

            # Получаем исходные данные
            input_text = ""
            if source_type == "meeting" and source_id:
                input_text = await self._get_meeting_text(source_id, user_id)
            elif source_type == "text":
                input_text = source_text or source_id
            else:
                input_text = source_text or source_id

            if not input_text:
                return {"success": False, "message": "No input data found"}

            # Вызываем Gemini LLM с контекстом для трекинга
            from backend.core.llm.usage_tracker import UsageContext

            gemini = GeminiClient(model=model)

            full_prompt = f"{prompt}\n\n--- Данные ---\n{input_text}"
            system_prompt = """Ты эксперт по анализу данных и текстов.

ВАЖНО:
- Работай ТОЛЬКО с данными из блока «Данные» ниже. Ничего не выдумывай: не
  добавляй фактов, цифр, имён и выводов, которых там нет.
- Отвечай развёрнуто, но без «воды» и без домыслов. Не хватает данных для
  вывода — честно скажи, чего не хватает.
- Цитируй важные части исходного текста дословно.
- Структурируй ответ, эмодзи — для разделения секций."""

            # Устанавливаем контекст для трекинга
            async with UsageContext(
                agent_mode="automation",
                request_type="llm_process",
                user_id=user_id,
                session_id=automation_id
            ):
                llm_result = await gemini.generate(
                    prompt=full_prompt,
                    system_prompt=system_prompt,
                    max_tokens=8192,
                    temperature=0.3,
                    use_cache=False
                )

            # Примерные токены для статистики (реальные уже затрекались в generate)
            input_tokens = gemini.estimate_tokens(full_prompt)
            output_tokens = gemini.estimate_tokens(llm_result)
            tokens_used = input_tokens + output_tokens
            cost_usd = (input_tokens / 1_000_000) * 0.075 + (output_tokens / 1_000_000) * 0.30

            # Поддержка множественных получателей
            output_targets = data.get("output_targets", [])
            if not output_targets and output_target:
                output_targets = [output_target]

            if not output_targets:
                return {"success": False, "message": "No recipients specified"}

            # Отправляем результат всем получателям
            results = []
            success_count = 0
            for target in output_targets:
                send_result = await self._send_llm_result(
                    output_type=output_type,
                    output_target=target,
                    content=llm_result,
                    user_id=user_id
                )
                results.append({"target": target, "success": send_result.get("success", False)})
                if send_result.get("success"):
                    success_count += 1

            return {
                "success": success_count > 0,
                "message": f"LLM processed and sent to {success_count}/{len(output_targets)} recipients",
                "data": {
                    "tokens_used": tokens_used,
                    "recipients": len(output_targets),
                    "success_count": success_count,
                    "cost_usd": cost_usd,
                    "model": model,
                    "output_length": len(llm_result),
                },
                "llm_usage": {
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "tokens_used": tokens_used,
                    "cost_usd": cost_usd
                }
            }

        except Exception as e:
            logger.error(f"LLM process error: {e}")
            return {"success": False, "message": f"LLM process error: {e}"}

    async def _execute_run_analysis(self, data: Dict, user_id: str) -> Dict:
        """Запустить методику разбора (вкладка «Анализ») по документам.

        action_data:
        - playbook_id: ID методики (обязателен)
        - document_ids: список ID документов (или document_id) — что разбирать
        - client_context: доп. контекст (опц.)

        Событийный триггер (meeting_ended / document_uploaded) прокидывает
        meeting_id/document_id в action_data через EventBus — можно строить
        «новый документ появился → запусти разбор».
        """
        try:
            playbook_id = (data.get("playbook_id") or "").strip()
            if not playbook_id:
                return {"success": False, "message": "playbook_id required"}
            doc_ids = data.get("document_ids") or []
            if not doc_ids and data.get("document_id"):
                doc_ids = [data["document_id"]]
            # событие могло положить meeting_id — как источник документа
            if not doc_ids and data.get("meeting_id"):
                doc_ids = [data["meeting_id"]]
            if not doc_ids:
                return {"success": False, "message": "document_ids required"}

            from backend.core.analysis.run_engine import start_analysis_run
            run_id = await start_analysis_run(
                user_id=user_id, playbook_id=playbook_id,
                document_ids=list(doc_ids),
                client_context=str(data.get("client_context") or ""),
            )
            if not run_id:
                return {"success": False,
                        "message": "Не удалось запустить разбор (методика не найдена или нет документов)"}
            return {"success": True,
                    "message": f"Разбор запущен (run {run_id}) — результат появится в истории «Анализ»",
                    "data": {"run_id": run_id, "documents": len(doc_ids)}}
        except Exception as e:
            return {"success": False, "message": f"Analysis run error: {e}"}

    async def _execute_run_meeting_pipeline(self, data: Dict, user_id: str,
                                            automation_id: str = "") -> Dict:
        """Vibe-tasking: после встречи прогнать её задачи через мозг-конвейер.

        Каждая выбранная задача проходит: извлечение → анализ (сложность/
        зависимости) → генерация ГОТОВОГО ТЗ на контексте компании
        (DataDrivenTaskSystem) → доставка/гейт/исполнение. Это НЕ вайп-кодинг
        продукта — это «вайп-таскинг»: подготовить простую задачу
        (презентация/лендинг/анализ таблицы/медиаплан) до готового брифа,
        который человек подтверждает и (опц.) отдаёт исполнителю.

        action_data:
        - meeting_id (обязателен; event-триггер meeting_ended инъектит сам)
        - max_tasks (1..20, default 5), task_titles[] (ручной выбор задач),
          assignee_filter (только задачи исполнителя)
        - dispatch_mode: "deliver" | "gate" | "auto" | "web" (default "deliver"):
            deliver — доставить готовое ТЗ в канал (L1+L3), ничего не исполнять;
            gate    — ТЗ в гейт подтверждения (pending handoff); после confirm —
                      CLI-исполнитель по ПОДПИСКЕ (claude -p / codex exec /
                      cursor-agent, авторизация логином, без API-ключа);
            auto    — авто-исполнить через executor (L4 напрямую) — только при
                      auto_execute-разрешении, высокий риск;
            web     — web-only трек: готовый бриф + ссылка-запуск в браузерный
                      инструмент (Lovable/v0/Bolt/Replit/Claude-web/ChatGPT),
                      человек выполняет и фиксирует результат (web-result).
        - channel/chat_id/to_email/slack_channel/recipient_group_id — доставка
          (как в остальных meeting-workflow), см. _send_to_channel;
        - handoff_agent: claude | cursor | codex (для gate/auto), default claude;
        - web_target: lovable | v0 | bolt | replit | claude | chatgpt (для web;
          пусто → выбор по типу задачи);
        - executor_backend: override исполнителя (для auto).
        - create_tracker_task (bool) + tracker (yougile/trello/jira) +
          tracker_column_id (колонка/список/project_key) — L2: создать задачу в
          трекере с приложенным ТЗ; tracker_mark_done + jira_done_status/
          trello_done_list_id — пометить «выполнено» там, где API позволяет.

        L4-идемпотентность: для dispatch_mode=auto ставится атомарный
        Redis-замок на (user_id, automation_id, meeting) — событие
        meeting_ended может прилететь дважды (два эмиттера / ретрай webhook);
        без замка исполнитель запустился бы дважды (двойная трата).
        """
        try:
            meeting_id = str(data.get("meeting_id") or data.get("id") or "").strip()
            if not meeting_id:
                return {"success": False, "message": "meeting_id required"}

            meeting_text = await self._get_meeting_text(meeting_id, user_id)
            if not meeting_text or not meeting_text.strip():
                return {"success": False,
                        "message": "Встреча не найдена или без транскрипта"}

            dispatch_mode = str(data.get("dispatch_mode") or "deliver").lower()
            if dispatch_mode not in ("deliver", "gate", "auto", "web"):
                dispatch_mode = "deliver"
            handoff_agent = str(data.get("handoff_agent") or "claude").lower()
            if handoff_agent not in ("claude", "cursor", "codex"):
                handoff_agent = "claude"
            web_target = str(data.get("web_target") or "").strip().lower()

            from backend.core.automations.meeting_pipeline import (
                PipelineConfig,
                run_meeting_pipeline,
            )
            cfg = PipelineConfig()
            try:
                if data.get("max_tasks") is not None:
                    cfg.max_tasks = max(1, min(int(data["max_tasks"]), 20))
            except (TypeError, ValueError):
                pass
            ids = data.get("task_ids")
            if isinstance(ids, list):
                cfg.task_ids = [str(i).strip() for i in ids if str(i).strip()]
            titles = data.get("task_titles")
            if isinstance(titles, list):
                cfg.task_titles = [str(t).strip() for t in titles if str(t).strip()]
            cfg.assignee_filter = str(data.get("assignee_filter") or "").strip()
            cfg.auto_execute = (dispatch_mode == "auto")

            # Ручной выбор по id → грузим ИМЕННО эти задачи из meeting_tasks
            # (тот же источник, откуда пользователь выбирал галочками), чтобы
            # НЕ пере-извлекать и не сопоставлять нечётко. Пусто/сбой → обычный
            # путь (пере-извлечение + фильтр), поведение не меняется.
            preselected_tasks = None
            if cfg.task_ids:
                preselected_tasks = await self._load_meeting_tasks_by_id(
                    meeting_id, user_id, cfg.task_ids)
            eb = data.get("executor_backend")
            if isinstance(eb, str) and eb.strip():
                cfg.executor_backend = eb.strip()

            delivered_count = {"n": 0}
            gated_ids: List[str] = []
            web_ids: List[str] = []

            async def _on_tz(*, task_description: str, task_type: str,
                             tz_markdown: str, record) -> None:
                # 1) распределение по режиму — до доставки, чтобы вложить id/URL.
                web_launch = None
                if dispatch_mode == "gate":
                    # CLI-исполнитель по подписке (claude/codex/cursor login):
                    # ТЗ в pending-хэндофф, запуск — после подтверждения человеком.
                    try:
                        from backend.core.tasks.task_analysis import coding_handoff
                        rec = await coding_handoff(
                            user_id,
                            {"title": task_description[:120], "description": ""},
                            agent=handoff_agent,
                            spec_text=tz_markdown,
                        )
                        record.handoff_id = str(rec.get("id") or "")
                        record.dispatch_status = "gated"
                        gated_ids.append(record.handoff_id)
                    except Exception as exc:
                        record.dispatch_status = "error"
                        logger.warning("run_meeting_pipeline gate failed: %s", exc)
                elif dispatch_mode == "web":
                    # Web-only трек (Lovable/v0/Bolt/Replit/Claude-web/ChatGPT):
                    # готовый бриф + ссылка-запуск, человек выполняет в браузере.
                    try:
                        from backend.core.executors.web_targets import build_launch
                        from backend.core.tasks.task_analysis import web_handoff
                        web_launch = build_launch(
                            web_target, task_title=task_description[:120],
                            tz_markdown=tz_markdown, task_type=task_type)
                        wh = await web_handoff(
                            user_id,
                            {"title": task_description[:120],
                             "tracker": (data.get("tracker")
                                         if data.get("create_tracker_task") else None),
                             "tracker_task_id": record.tracker_task_id or None},
                            tool=web_launch["tool"], launch_url=web_launch["url"],
                            spec_text=tz_markdown, prefilled=web_launch["prefilled"],
                            note=web_launch["note"])
                        record.handoff_id = str(wh.get("id") or "")
                        record.dispatch_status = "web_dispatched"
                        web_ids.append(record.handoff_id)
                    except Exception as exc:
                        record.dispatch_status = "error"
                        logger.warning("run_meeting_pipeline web dispatch failed: %s", exc)
                elif dispatch_mode == "auto":
                    record.dispatch_status = "dispatched"
                else:
                    record.dispatch_status = "delivered"

                # 2) доставка в канал (если настроен получатель)
                has_target = any(data.get(k) for k in (
                    "chat_id", "to_email", "to", "slack_channel",
                    "recipient_group_id"))
                if not has_target:
                    return
                try:
                    from backend.core.automations.meeting_workflows import (
                        get_workflow_executor,
                    )
                    if dispatch_mode == "web" and web_launch:
                        pf = ("(промпт предзаполнен) " if web_launch["prefilled"]
                              else "(вставь бриф ниже) ")
                        body = (
                            f"🌐 ТЗ готово → открой в {web_launch['label']}:\n"
                            f"{web_launch['url']}\n\n{pf}"
                            f"После выполнения зафиксируй результат "
                            f"(handoff {record.handoff_id}).\n\n"
                            f"— — —\n{tz_markdown[:2800]}"
                        )
                    elif dispatch_mode == "gate" and record.handoff_id:
                        body = (
                            f"🧩 Готово ТЗ по задаче: {task_description[:200]}\n\n"
                            f"Тип: {task_type or 'general'}\n"
                            f"Чтобы отдать исполнителю — подтвердите гейт "
                            f"(handoff {record.handoff_id}).\n\n"
                            f"— — —\n{tz_markdown[:2800]}"
                        )
                    else:
                        body = (
                            f"🧩 Готово ТЗ по задаче: {task_description[:200]}\n"
                            f"Тип: {task_type or 'general'}\n\n{tz_markdown[:3500]}"
                        )
                    res = await get_workflow_executor()._send_to_channel(
                        user_id=user_id, params=data, text=body,
                        subject=f"ТЗ: {task_description[:80]}")
                    record.delivered = bool(res.get("success"))
                except Exception as exc:
                    logger.warning("run_meeting_pipeline deliver failed: %s", exc)

            async def _create_tracker(*, task_description: str, tz_markdown: str,
                                      record) -> None:
                # L2: создать задачу в трекере с приложенным ТЗ (+ опц. done).
                # Независимо от dispatch_mode — работает рядом с доставкой/гейтом.
                if not (data.get("create_tracker_task") and data.get("tracker")):
                    return
                try:
                    from backend.core.tasks.task_actions import create_and_prepare_task
                    tsys = str(data["tracker"]).lower()
                    r = await create_and_prepare_task(
                        user_id, tsys,
                        title=task_description[:120], tz_text=tz_markdown,
                        column_id=str(data.get("tracker_column_id") or ""),
                        mark_done=bool(data.get("tracker_mark_done")),
                        transition_name=str(data.get("jira_done_status") or "Done"),
                        target_column_id=str(data.get("trello_done_list_id")
                                             or data.get("tracker_done_column_id") or ""))
                    if r.get("status") in ("success", "partial"):
                        record.tracker_task_id = str(r.get("task_id") or "")
                        record.tracker_system = tsys
                        if not record.dispatch_status:
                            record.dispatch_status = "tracker_created"
                        if r.get("warnings"):
                            logger.warning("tracker create partial (%s): %s",
                                           tsys, "; ".join(r["warnings"]))
                    else:
                        logger.warning("tracker create not ok: %s", r.get("message"))
                except Exception as exc:
                    logger.warning("run_meeting_pipeline tracker create failed: %s", exc)

            async def _on_tz_full(*, task_description: str, task_type: str,
                                  tz_markdown: str, record) -> None:
                await _on_tz(task_description=task_description, task_type=task_type,
                             tz_markdown=tz_markdown, record=record)
                await _create_tracker(task_description=task_description,
                                      tz_markdown=tz_markdown, record=record)

            try:
                from backend.core.observability import audit_log
                _audit = audit_log.emit
            except Exception:
                _audit = None

            # L4-идемпотентность: auto тратит деньги (executor-петля). Ключ —
            # СТАБИЛЬНЫЙ meeting_id (одинаков у обоих эмиттеров meeting_ended и
            # у ретраёв webhook); content_hash обёрнутого текста был бы хрупок
            # (title/транскрипт меняются между чтениями). Атомарный SET NX с
            # КОРОТКИМ окном (covers дубли/ретраи); после успешного диспатча
            # окно продлевается до 6ч, а если исполнение не стартовало (ранний
            # сбой без трат) — замок снимается, чтобы легитимный ретрай прошёл.
            # Fail-open: Redis недоступен → продолжаем.
            _guard_key = (f"mpipe:auto:{user_id}:{automation_id}:{meeting_id}"
                          if dispatch_mode == "auto" else "")
            _lock_claimed = False
            if _guard_key:
                try:
                    from backend.db.redis_client import get_redis
                    r = await get_redis()
                    if getattr(r, "client", None):
                        claimed = await r.client.set(
                            _guard_key, "1", nx=True, ex=30 * 60)
                        if not claimed:
                            return {"success": True, "skipped": True,
                                    "message": "pipeline уже запущен для этой встречи (идемпотентность)"}
                        _lock_claimed = True
                except Exception as _dexc:
                    logger.debug("auto-dispatch dedup skipped (fail-open): %s", _dexc)

            result = await run_meeting_pipeline(
                transcript=meeting_text,
                meeting_id=meeting_id,
                user_id=user_id,
                config=cfg,
                preselected_tasks=preselected_tasks,
                on_tz=_on_tz_full,
                audit=_audit,
            )

            # Управление замком: держим 6ч только если реально что-то запустили
            # (иначе ранний сбой заблокировал бы ретрай на всё окно).
            if _lock_claimed:
                try:
                    from backend.db.redis_client import get_redis
                    r = await get_redis()
                    if getattr(r, "client", None):
                        if any(rec.executed for rec in result.records):
                            await r.client.set(_guard_key, "1", ex=6 * 3600)
                        else:
                            await r.client.delete(_guard_key)
                except Exception as _lexc:
                    logger.debug("auto-dispatch lock finalize skipped: %s", _lexc)

            summary = result.to_dict()
            n_tz = sum(1 for r in result.records if r.tz_generated)
            n_tracker = 0
            for r in summary.get("records", []):
                if r.get("delivered"):
                    delivered_count["n"] += 1
                if r.get("tracker_task_id"):
                    n_tracker += 1
            msg = (f"Конвейер: {n_tz} ТЗ из {result.tasks_extracted} задач; "
                   f"режим={dispatch_mode}")
            if dispatch_mode == "gate" and gated_ids:
                msg += f"; в гейте подтверждения: {len(gated_ids)}"
            if dispatch_mode == "web" and web_ids:
                msg += f"; в web-очереди ({web_target or 'auto'}): {len(web_ids)}"
            if delivered_count["n"]:
                msg += f"; доставлено: {delivered_count['n']}"
            if n_tracker:
                msg += f"; создано в трекере: {n_tracker}"
            return {"success": result.status in ("ok",) or n_tz > 0,
                    "message": msg,
                    "data": {**summary, "dispatch_mode": dispatch_mode,
                             "gated_handoff_ids": gated_ids,
                             "web_handoff_ids": web_ids}}
        except Exception as e:
            logger.error(f"run_meeting_pipeline error: {e}", exc_info=True)
            return {"success": False, "message": f"Pipeline error: {e}"}

    # === Классические research/digest автоматизации (не по встречам) =====

    async def _llm_digest(self, *, corpus: str, lens: str, model_tier: str,
                          user_id: str, automation_id: str, request_type: str,
                          system_extra: str = "") -> tuple[str, Dict]:
        """Единый LLM-проход «разбери корпус под углом lens». Возвращает
        (текст, llm_usage). Используют оба handler'а (research/chat_digest)."""
        from backend.core.llm.router import ModelTier, get_llm_router
        from backend.core.llm.usage_tracker import cost_scope
        tier = ModelTier.PREMIUM if str(model_tier) == "premium" else ModelTier.STANDARD
        sys_prompt = (
            "Ты — аналитик компании. Разбери материалы СТРОГО под заданным "
            "углом и выдай сжатый, полезный результат на русском: ключевые "
            "сигналы, что важно именно нам, что делать. Без воды, только "
            "суть и конкретика. Сигналы, цифры и выводы бери ТОЛЬКО из "
            "материалов — ничего не выдумывай. Если данных мало — честно скажи "
            "об этом, а не заполняй правдоподобным.\n\n"
            f"Угол разбора (что нужно пользователю):\n{lens or 'Сделай полезную выжимку.'}"
        )
        if system_extra:
            sys_prompt += "\n\n" + system_extra
        router = get_llm_router()
        text = ""
        async with cost_scope(surface="digest", agent_mode="automation",
                              request_type=request_type, user_id=user_id,
                              session_id=automation_id):
            text = await router.generate(
                prompt=corpus, system_prompt=sys_prompt,
                model_tier=tier, temperature=0.3, max_tokens=4096)
        # Оценка токенов для учёта (router сам трекает внутри, но вернём сводку).
        usage = {"model": tier.value, "request_type": request_type,
                 "input_chars": len(corpus), "output_chars": len(text or "")}
        return (text or ""), usage

    async def _deliver_digest(self, *, data: Dict, user_id: str, title: str,
                              body: str) -> Dict:
        """Доставка отчёта: если задан channel/chat_id/… → _send_to_channel
        (Telegram/Email/Slack + группы), иначе output_type/output_target.

        Явно заданный `channel` (напр. пресет «channel=telegram» без chat_id)
        → идём через _send_to_channel: Telegram с пустым получателем уходит в
        чат по умолчанию из интеграций (нужно для one-click пресетов)."""
        has_channel = bool(data.get("channel")) or any(data.get(k) for k in (
            "chat_id", "to_email", "to", "slack_channel", "recipient_group_id"))
        if has_channel:
            from backend.core.automations.meeting_workflows import get_workflow_executor
            return await get_workflow_executor()._send_to_channel(
                user_id=user_id, params=data, text=body, subject=title)
        # fallback: классический output_type/output_target(s)
        out_type = str(data.get("output_type") or "telegram")
        targets = data.get("output_targets") or (
            [data["output_target"]] if data.get("output_target") else [])
        if not targets:
            return {"success": False, "message": "не указан получатель (chat_id/output_target)"}
        ok = 0
        for t in targets:
            r = await self._send_llm_result(out_type, str(t), body, user_id)
            if isinstance(r, dict) and (r.get("success") or not r.get("error")):
                ok += 1
        return {"success": ok > 0, "message": f"→ {ok}/{len(targets)}"}

    async def _execute_scheduled_research(self, data: Dict, user_id: str,
                                          automation_id: str = "") -> Dict:
        """Классическая автоматизация: собрать сайты/новости/публичные
        TG-каналы, разобрать под заданным углом (lens), прислать отчёт.

        action_data:
        - sources: {search_queries[], urls[], telegram_channels[]}
        - lens: под каким углом разбирать («инсайты рынка, полезные нам»)
        - model_tier: standard|premium; max_urls (default 8)
        - доставка: channel/chat_id/to_email/slack_channel/recipient_group_id
          ИЛИ output_type/output_targets.
        """
        try:
            sources = data.get("sources") or {}
            if not isinstance(sources, dict):
                sources = {}
            queries = [str(q).strip() for q in (sources.get("search_queries") or []) if str(q).strip()]
            urls = [str(u).strip() for u in (sources.get("urls") or []) if str(u).strip()]
            channels = [str(c).strip() for c in (sources.get("telegram_channels") or []) if str(c).strip()]
            lens = str(data.get("lens") or "").strip()
            if not (queries or urls or channels):
                return {"success": False, "message": "нужен хотя бы один источник (sources.search_queries/urls/telegram_channels)"}

            try:
                max_urls = max(1, min(int(data.get("max_urls", 8)), 20))
            except (TypeError, ValueError):
                max_urls = 8

            parts: List[str] = []
            fetched = {"urls": 0, "queries": 0, "channels": 0}

            # 1) веб-поиск
            if queries:
                from backend.core.search.web_search import format_results_md, web_search
                for q in queries[:8]:
                    res = await web_search(q, max_results=5)
                    parts.append(format_results_md(q, res))
                    fetched["queries"] += 1

            # 2) URL-фетч (SSRF-safe)
            if urls:
                import asyncio as _asyncio
                from backend.core.documents.url_ingest import fetch_url_text
                got = await _asyncio.gather(
                    *[fetch_url_text(u) for u in urls[:max_urls]],
                    return_exceptions=True)
                for u, r in zip(urls[:max_urls], got):
                    if isinstance(r, dict) and r.get("ok"):
                        parts.append(f"### URL: {u}\n{(r.get('title') or '').strip()}\n"
                                     f"{(r.get('text') or '')[:6000]}\n")
                        fetched["urls"] += 1
                    else:
                        err = r.get("error") if isinstance(r, dict) else str(r)
                        parts.append(f"### URL: {u}\n(не удалось: {err})\n")

            # 3) публичные TG-каналы (t.me/s/ scrape, без API)
            if channels:
                try:
                    import sys as _sys
                    if MEETFLOW_PATH not in _sys.path:
                        _sys.path.insert(0, MEETFLOW_PATH)
                    from telegram_tools import get_telegram_channel_posts
                    for ch in channels[:5]:
                        raw = await get_telegram_channel_posts(ch, limit=10)
                        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
                        if d.get("success"):
                            posts = d.get("posts") or []
                            txt = "\n".join(f"- {str(p.get('text', ''))[:400]}" for p in posts[:10])
                            parts.append(f"### Telegram-канал {ch}\n{txt}\n")
                            fetched["channels"] += 1
                        else:
                            parts.append(f"### Telegram-канал {ch}\n(не прочитан: {d.get('error')})\n")
                except Exception as tg_exc:
                    logger.warning("research: telegram channels failed: %s", tg_exc)

            corpus = "\n\n".join(parts)[:120000]  # cap на вход LLM
            if not corpus.strip():
                return {"success": False, "message": "источники не дали материала"}

            text, usage = await self._llm_digest(
                corpus=corpus, lens=lens,
                model_tier=str(data.get("model_tier", "standard")),
                user_id=user_id, automation_id=automation_id,
                request_type="scheduled_research")
            if not text.strip():
                return {"success": False, "message": "LLM не вернул результат"}

            title = str(data.get("title") or "🔬 Исследование/дайджест")
            deliver = await self._deliver_digest(
                data=data, user_id=user_id, title=title, body=text)
            return {"success": deliver.get("success", False),
                    "message": (f"Собрано (поиск={fetched['queries']}, url={fetched['urls']}, "
                                f"каналы={fetched['channels']}); доставка: {deliver.get('message')}"),
                    "data": {"fetched": fetched}, "llm_usage": usage}
        except Exception as e:
            logger.error(f"scheduled_research error: {e}", exc_info=True)
            return {"success": False, "message": f"Research error: {e}"}

    async def _execute_chat_digest(self, data: Dict, user_id: str,
                                   automation_id: str = "") -> Dict:
        """Классическая автоматизация: дайджест командного чата за период.

        action_data:
        - source: slack | telegram_group
        - channel: id канала Slack (или chat_id TG)
        - window_days (default 7), mode: retrospective|daily_analytics
        - lens: под каким углом (иначе — пресет по mode)
        - доставка: как в scheduled_research.

        Slack требует xoxb-токен со scope channels:history + бот в канале.
        Telegram-группа — ТОЛЬКО через userbot-сессию (не настроено → честный
        отказ), Bot API историю группы читать не умеет.
        """
        try:
            source = str(data.get("source") or "slack").lower()
            channel = str(data.get("channel") or data.get("slack_channel") or "").strip()
            try:
                window_days = max(1, min(int(data.get("window_days", 7)), 60))
            except (TypeError, ValueError):
                window_days = 7
            mode = str(data.get("mode") or "retrospective").lower()
            lens = str(data.get("lens") or "").strip()
            if not lens:
                lens = (
                    "Ежедневная аналитика: что вчера/сегодня обсуждали, какие "
                    "проблемы возникли, какие решения приняли, на чём заострить "
                    "внимание сейчас, что держать в голове и решать."
                    if mode == "daily_analytics" else
                    "Ретроспектива за период: о чём команда говорила, ключевые "
                    "темы, проблемы, принятые решения, что важного произошло и "
                    "на что обратить внимание.")

            if not channel:
                return {"success": False, "message": "нужен channel (id канала Slack или chat_id TG-группы)"}

            corpus = ""
            if source == "slack":
                from backend.integrations.registry import IntegrationRegistry
                reg = IntegrationRegistry()
                await reg.load_for_user(user_id)
                raw = await reg.execute_tool(
                    "slack_read_messages", channel=channel, days=window_days)
                d = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if not d.get("success"):
                    err = d.get("error", "unknown")
                    hint = (" — пригласите бота в канал (invite) и дайте scope "
                            "channels:history/groups:history"
                            if err in ("not_in_channel", "channel_not_found",
                                       "missing_scope") else "")
                    return {"success": False,
                            "message": f"Slack не прочитан: {err}{hint}"}
                msgs = d.get("messages") or []
                if not msgs:
                    return {"success": True, "message": f"за {window_days} дн. сообщений нет"}
                # хронологический порядок (history отдаёт свежие первыми)
                lines = [f"[{m.get('user', '')}] {m.get('text', '')}"
                         for m in reversed(msgs) if m.get("text")]
                corpus = ("\n".join(lines))[:120000]
            elif source == "telegram_group":
                # Bot API не умеет читать историю группы → userbot (Telethon +
                # аккаунт-участник группы). Не настроено → честный отказ.
                from backend.core.messengers.telegram_userbot import (
                    is_configured,
                    read_group_history,
                )
                if not is_configured():
                    return {"success": False,
                            "message": ("Чтение истории Telegram-группы требует "
                                        "userbot-сессии (TELEGRAM_SESSION_STRING + "
                                        "TELEGRAM_API_ID/HASH + аккаунт в группе) — "
                                        "не настроено. Bot API историю группы читать "
                                        "не умеет. Доступно сейчас: публичные каналы "
                                        "в «Исследование» или Slack здесь.")}
                res = await read_group_history(channel, days=window_days)
                if not res.get("ok"):
                    return {"success": False,
                            "message": f"Telegram-группа не прочитана: {res.get('reason')}"}
                gmsgs = res.get("messages") or []
                if not gmsgs:
                    return {"success": True, "message": f"за {window_days} дн. сообщений нет"}
                corpus = ("\n".join(f"[{m.get('sender', '')}] {m.get('text', '')}"
                                    for m in gmsgs if m.get("text")))[:120000]
            else:
                return {"success": False, "message": f"source должен быть slack|telegram_group (дано {source})"}

            if not corpus.strip():
                return {"success": False, "message": "нет материала для разбора"}

            text, usage = await self._llm_digest(
                corpus=corpus, lens=lens,
                model_tier=str(data.get("model_tier", "standard")),
                user_id=user_id, automation_id=automation_id,
                request_type="chat_digest")
            if not text.strip():
                return {"success": False, "message": "LLM не вернул результат"}

            title = str(data.get("title") or f"💬 Дайджест чата ({window_days} дн.)")
            deliver = await self._deliver_digest(
                data=data, user_id=user_id, title=title, body=text)
            return {"success": deliver.get("success", False),
                    "message": f"Дайджест {source} за {window_days} дн.; доставка: {deliver.get('message')}",
                    "data": {"source": source, "window_days": window_days},
                    "llm_usage": usage}
        except Exception as e:
            logger.error(f"chat_digest error: {e}", exc_info=True)
            return {"success": False, "message": f"Digest error: {e}"}

    async def _execute_llm_extract_and_send(self, data: Dict, user_id: str, automation_id: str) -> Dict:
        """
        Извлечь данные из текста через LLM и отправить.

        action_data должен содержать:
        - meeting_id: ID встречи для извлечения
        - extract_type: Что извлечь (tasks, decisions, summary, custom)
        - custom_prompt: Кастомный промпт (для extract_type=custom)
        - output_type: Куда отправить (telegram, email, notion, yougile)
        - output_target: ID цели
        - model_tier: Уровень модели (standard/premium) - опционально
        """
        try:
            from backend.core.llm.gemini_client import GeminiClient

            meeting_id = data.get("meeting_id", "")
            extract_type = data.get("extract_type", "summary")
            custom_prompt = data.get("custom_prompt", "")
            output_type = data.get("output_type", "telegram")
            output_target = data.get("output_target", "")
            model_tier = data.get("model_tier", "standard")

            # Выбираем модель по tier
            TIER_MODELS = {
                "standard": "gemini-flash-lite-latest",
                "premium": "gemini-flash-latest"  # Новейшая модель Gemini 3 Flash
            }
            model = TIER_MODELS.get(model_tier, TIER_MODELS["standard"])

            if not meeting_id:
                return {"success": False, "message": "meeting_id is required"}

            # Получаем текст встречи
            meeting_text = await self._get_meeting_text(meeting_id, user_id)
            if not meeting_text:
                return {"success": False, "message": "Meeting not found or has no transcript"}

            # Формируем промпт в зависимости от типа извлечения
            prompts = {
                "tasks": """Извлеки ВСЕ задачи из этой встречи. Для каждой задачи укажи:
- Название задачи (подробно)
- Ответственный (если упомянут)
- Дедлайн (если упомянут)
- Контекст и детали задачи

Формат: структурированный список с маркерами. Будь максимально подробным.""",

                "decisions": """Извлеки ВСЕ принятые решения из этой встречи. Для каждого решения укажи:
- Суть решения
- Кто принял / предложил
- Обоснование (если есть)
- Связанные действия

Формат: нумерованный список с подробным описанием каждого решения.""",

                "summary": """Сделай ПОДРОБНОЕ саммари этой встречи:

1. **Основные темы обсуждения** - что обсуждали, какие вопросы поднимались
2. **Ключевые моменты** - важные высказывания, идеи, предложения участников
3. **Принятые решения** - что решили, к чему пришли
4. **Следующие шаги** - что планируется делать дальше
5. **Открытые вопросы** - что осталось нерешённым

Пиши развёрнуто, не упускай важные детали. Цитируй ключевые высказывания участников.""",

                "action_items": """Извлеки ВСЕ action items (следующие шаги) из встречи:

Для каждого action item укажи:
- Что нужно сделать (подробно)
- Кто ответственный
- Срок выполнения (если упомянут)
- Приоритет (если понятен из контекста)
- Связанные задачи или зависимости

Будь максимально подробным, не упускай ничего важного.""",

                "custom": custom_prompt
            }

            prompt = prompts.get(extract_type, prompts["summary"])
            if not prompt:
                return {"success": False, "message": "Invalid extract_type or empty custom_prompt"}

            # Вызываем Gemini LLM с контекстом для трекинга
            from backend.core.llm.usage_tracker import UsageContext

            gemini = GeminiClient(model=model)
            system_prompt = """Ты эксперт по анализу деловых встреч. Твоя задача — извлекать информацию максимально полно, но СТРОГО из транскрипта.

ВАЖНО:
- Извлекай ТОЛЬКО то, что реально сказано во встрече. Не выдумывай задачи,
  решения, сроки, ответственных, цифры и договорённости, которых нет в тексте.
- Отвечай развёрнуто, но без домыслов. Нет данных по пункту — не пиши его.
- Цитируй важные высказывания дословно; ответственных/сроки указывай, только
  если они прозвучали.
- Структурируй ответ, эмодзи — для разделения секций."""

            full_prompt = f"{prompt}\n\n--- Транскрипция встречи ---\n{meeting_text}"

            # Устанавливаем контекст для трекинга
            async with UsageContext(
                agent_mode="automation",
                request_type="llm_extract_and_send",
                user_id=user_id,
                session_id=automation_id
            ):
                extracted = await gemini.generate(
                    prompt=full_prompt,
                    system_prompt=system_prompt,
                    max_tokens=8192,
                    temperature=0.3,
                    use_cache=False
                )

            # Примерные токены для статистики (реальные уже затрекались в generate)
            input_tokens = gemini.estimate_tokens(full_prompt)
            output_tokens = gemini.estimate_tokens(extracted)
            tokens_used = input_tokens + output_tokens
            cost_usd = (input_tokens / 1_000_000) * 0.075 + (output_tokens / 1_000_000) * 0.30

            # Поддержка множественных получателей
            output_targets = data.get("output_targets", [])
            if not output_targets and output_target:
                output_targets = [output_target]

            if not output_targets:
                return {"success": False, "message": "No recipients specified"}

            # Отправляем результат всем получателям
            results = []
            success_count = 0
            for target in output_targets:
                send_result = await self._send_llm_result(
                    output_type=output_type,
                    output_target=target,
                    content=extracted,
                    user_id=user_id
                )
                results.append({"target": target, "success": send_result.get("success", False)})
                if send_result.get("success"):
                    success_count += 1

            return {
                "success": success_count > 0,
                "message": f"Extracted {extract_type} and sent to {success_count}/{len(output_targets)} recipients",
                "data": {
                    "extract_type": extract_type,
                    "tokens_used": tokens_used,
                    "cost_usd": cost_usd,
                    "model": model,
                    "extracted_length": len(extracted),
                    "recipients": len(output_targets),
                    "success_count": success_count,
                },
                "llm_usage": {
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "tokens_used": tokens_used,
                    "cost_usd": cost_usd
                }
            }

        except Exception as e:
            logger.error(f"LLM extract error: {e}")
            return {"success": False, "message": f"LLM extract error: {e}"}

    async def _get_meeting_text(self, meeting_id: str, user_id: str) -> str:
        """Получить текст встречи (транскрипцию)"""
        try:
            params = {
                "id": f"eq.{meeting_id}",
                "select": "id,title,transcription_text,created_at"
            }
            result = await self._request("GET", "/rest/v1/meetings", params=params)
            if result and len(result) > 0:
                meeting = result[0]
                title = meeting.get("title", "Встреча")
                date = meeting.get("created_at", "")[:10]
                transcript = meeting.get("transcription_text", "")
                return f"Встреча: {title}\nДата: {date}\n\n{transcript}"
            return ""
        except Exception as e:
            logger.error(f"Failed to get meeting text: {e}")
            return ""

    async def _load_meeting_tasks_by_id(
        self, meeting_id: str, user_id: str, task_ids: List[str],
    ) -> Optional[List[Dict[str, Any]]]:
        """Загрузить ВЫБРАННЫЕ задачи встречи по task_id из meeting_tasks.

        Тот же источник, что и UI-выбор галочками (GET /automations/meeting-
        tasks). Изоляция по владельцу через meetings.user_id. Возврат — список
        task-dict'ов (shape для meeting_pipeline: id/title/description/assignee)
        ТОЛЬКО по выбранным id. None при сбое/нет доступа → caller откатывается
        на обычный путь (пере-извлечение), поведение не ломается.
        """
        try:
            wanted = [str(i).strip() for i in (task_ids or []) if str(i).strip()]
            if not meeting_id or not user_id or not wanted:
                return None
            # Владелец встречи — иначе не отдаём (сегрегация арендаторов).
            owner = await self._request(
                "GET", "/rest/v1/meetings",
                params={"id": f"eq.{meeting_id}", "user_id": f"eq.{user_id}",
                        "select": "id"})
            if not owner:
                return None
            in_list = ",".join(w.replace(",", "") for w in wanted)
            rows = await self._request(
                "GET", "/rest/v1/meeting_tasks",
                params={"meeting_id": f"eq.{meeting_id}",
                        "task_id": f"in.({in_list})",
                        "select": "task_id,title,description,assignee,due_date,"
                                  "status,priority",
                        "limit": "100"}) or []
            out: List[Dict[str, Any]] = []
            for r in rows:
                tid = str(r.get("task_id") or "").strip()
                if not tid:
                    continue
                out.append({
                    "id": tid,
                    "task_id": tid,
                    "title": r.get("title") or "Задача",
                    "description": r.get("description") or "",
                    "assignee": r.get("assignee") or "",
                })
            return out or None
        except Exception as e:
            logger.warning("load meeting tasks by id failed: %s", e)
            return None

    async def _update_llm_usage(self, automation_id: str, tokens: int, cost_usd: float):
        """Обновить статистику использования LLM"""
        try:
            # Получаем текущую автоматизацию
            automation = await self.get_automation(automation_id)
            if automation:
                updates = {
                    "last_run_tokens": tokens,
                    "last_run_cost_usd": cost_usd,
                    "total_tokens_used": automation.total_tokens_used + tokens,
                    "total_cost_usd": automation.total_cost_usd + cost_usd
                }
                await self.update_automation(automation_id, updates)

                # Также записываем в общую таблицу usage
                usage_data = {
                    "id": str(uuid.uuid4()),
                    "user_id": automation.user_id,
                    "source": "automation",
                    "source_id": automation_id,
                    "model": "gpt-4o-mini",
                    "tokens_input": tokens // 2,  # Примерное разделение
                    "tokens_output": tokens // 2,
                    "cost_usd": cost_usd,
                    "created_at": datetime.now(timezone.utc).isoformat()
                }
                try:
                    await self._request("POST", "/rest/v1/llm_usage", json_data=usage_data)
                except Exception:
                    pass  # Таблица может не существовать
        except Exception as e:
            logger.warning(f"Failed to update LLM usage: {e}")

    async def _send_llm_result(
        self,
        output_type: str,
        output_target: str,
        content: str,
        user_id: str
    ) -> Dict:
        """Отправить результат LLM в указанное место"""
        try:
            if MEETFLOW_PATH not in sys.path:
                sys.path.insert(0, MEETFLOW_PATH)

            # Устанавливаем контекст пользователя для automation_tools_async
            from automation_tools_async import set_user_context
            set_user_context(user_id=user_id)
            logger.info(f"📤 Set user context for sending: user_id={user_id}")

            if output_type == "telegram":
                from automation_tools_async import send_telegram_message
                logger.info(f"📤 Sending to Telegram: chat_id={output_target}, content_len={len(content)}")
                result = await send_telegram_message(content, output_target)
                logger.info(f"📤 Telegram result: {result}")
                return json.loads(result)

            elif output_type == "email":
                from automation_tools_async import send_email
                result = await send_email(
                    to=output_target,
                    subject="Результат автоматизации",
                    body=content
                )
                return json.loads(result)

            elif output_type == "notion":
                from automation_tools_async import create_notion_page
                result = await create_notion_page(
                    title="Результат автоматизации",
                    content=content,
                    parent_page_id=output_target
                )
                return {"success": "error" not in json.loads(result)}

            elif output_type == "yougile":
                from automation_tools_async import create_task
                result = await create_task(
                    column_id=output_target,
                    title="Задача из автоматизации",
                    description=content
                )
                return {"success": "error" not in json.loads(result)}

            else:
                return {"success": False, "message": f"Unknown output type: {output_type}"}

        except Exception as e:
            logger.error(f"Failed to send LLM result: {e}")
            return {"success": False, "message": str(e)}


    # ==================== Legacy Tasks (from MeetFlow agents) ====================

    async def list_legacy_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Получить legacy задачи из таблицы scheduled_tasks (MeetFlow agents)"""
        params = {
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
        }

        if user_id:
            params["user_id"] = f"eq.{user_id}"
        if status:
            params["status"] = f"eq.{status}"

        try:
            result = await self._request("GET", "/rest/v1/scheduled_tasks", params=params)
            return result or []
        except Exception as e:
            logger.warning(f"Failed to get legacy tasks: {e}")
            return []

    async def get_all_tasks_combined(
        self,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Получить все задачи из обеих таблиц"""
        # Получаем из новой таблицы
        automations = await self.list_automations(user_id=user_id, limit=limit)

        # Получаем legacy задачи
        legacy_tasks = await self.list_legacy_tasks(user_id=user_id, limit=limit)

        # Конвертируем legacy в формат автоматизаций
        legacy_converted = []
        for task in legacy_tasks:
            legacy_converted.append({
                "id": task.get("id"),
                "user_id": task.get("user_id", ""),
                "name": f"[Legacy] {task.get('action_type', 'task')}",
                "description": "Legacy task from MeetFlow agents",
                "action_type": task.get("action_type", ""),
                "action_data": task.get("action_data", {}),
                "schedule_type": "once",
                "execute_at": task.get("execute_at"),
                "status": task.get("status", "pending"),
                "last_run_at": None,
                "last_run_status": None,
                "last_error": task.get("error"),
                "run_count": 1 if task.get("status") == "completed" else 0,
                "created_at": task.get("created_at"),
                "is_legacy": True,
            })

        return {
            "automations": [a.__dict__ if hasattr(a, '__dict__') else a for a in automations],
            "legacy_tasks": legacy_converted,
            "total": len(automations) + len(legacy_converted),
        }


# Singleton instance
_automation_service: Optional[AutomationService] = None


def get_automation_service() -> AutomationService:
    """Получить singleton экземпляр сервиса"""
    global _automation_service
    if _automation_service is None:
        _automation_service = AutomationService()
    return _automation_service

