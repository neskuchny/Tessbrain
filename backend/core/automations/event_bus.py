# -*- coding: utf-8 -*-
"""
EventBus — система событийных автоматизаций.

Вместо cron/interval, автоматизации могут срабатывать на события:
- meeting_ended: встреча завершилась
- task_overdue: задача просрочена
- task_completed: задача выполнена
- new_meeting_scheduled: назначена новая встреча
- kpi_changed: KPI изменился
- document_updated: документ обновлён
- user_mentioned: пользователя упомянули
- daily_summary: ежедневный триггер для дайджестов (09:00)

Использование:
    bus = get_event_bus()

    # Зарегистрировать обработчик
    bus.subscribe("meeting_ended", my_handler)

    # Или через автоматизацию
    await bus.register_event_automation(
        user_id="...",
        event_type="meeting_ended",
        automation_id="...",
    )

    # Эмитировать событие
    await bus.emit("meeting_ended", {"meeting_id": "123", "title": "Standup"})
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


# === Поддерживаемые типы событий ===

class EventType:
    """Все поддерживаемые типы событий."""
    MEETING_ENDED = "meeting_ended"
    TASK_OVERDUE = "task_overdue"
    TASK_COMPLETED = "task_completed"
    NEW_MEETING_SCHEDULED = "new_meeting_scheduled"
    KPI_CHANGED = "kpi_changed"
    DOCUMENT_UPDATED = "document_updated"
    USER_MENTIONED = "user_mentioned"
    NEW_EMAIL = "new_email"          # входящее письмо (webhook mail-провайдера)
    NEW_LEAD = "new_lead"            # входящая заявка из CRM/формы (webhook)
    DAILY_SUMMARY = "daily_summary"
    WEEKLY_SUMMARY = "weekly_summary"
    AUTOMATION_FAILED = "automation_failed"
    INTEGRATION_WEBHOOK = "integration_webhook"
    CUSTOM = "custom"

    ALL_TYPES = [
        MEETING_ENDED, TASK_OVERDUE, TASK_COMPLETED,
        NEW_MEETING_SCHEDULED, KPI_CHANGED, DOCUMENT_UPDATED,
        USER_MENTIONED, DAILY_SUMMARY, WEEKLY_SUMMARY,
        AUTOMATION_FAILED, INTEGRATION_WEBHOOK, CUSTOM,
    ]


@dataclass
class Event:
    """Событие в системе."""
    id: str
    event_type: str
    payload: Dict[str, Any]
    user_id: str = ""
    timestamp: str = ""
    source: str = ""  # Кто эмитировал: "meetflow", "scheduler", "webhook", "manual"

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class EventSubscription:
    """Подписка на событие."""
    id: str
    event_type: str
    user_id: str
    # Может быть automation_id ИЛИ callback
    automation_id: Optional[str] = None
    callback_name: Optional[str] = None  # Имя зарегистрированной callback-функции
    # Фильтр: payload должен содержать эти ключи/значения
    filter_conditions: Optional[Dict[str, Any]] = None
    active: bool = True
    created_at: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


# Type alias for async event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """
    Центральная шина событий.

    Подходы:
    1. subscribe(event_type, handler) — подписка на Python callback
    2. register_event_automation(user_id, event_type, automation_id) — подписка автоматизации
    3. emit(event_type, payload) — эмитировать событие
    """

    def __init__(self):
        # Callback подписки: event_type → [handler, ...]
        self._handlers: Dict[str, List[EventHandler]] = {}
        # Подписки автоматизаций: event_type → [EventSubscription, ...]
        self._subscriptions: Dict[str, List[EventSubscription]] = {}
        # Лог последних событий (для отладки, ring buffer)
        self._event_log: List[Event] = []
        self._max_log_size = 200

    # ==================== Callback подписки ====================

    def subscribe(self, event_type: str, handler: EventHandler) -> str:
        """
        Подписать callback на событие.

        Args:
            event_type: Тип события (см. EventType)
            handler: Async функция, принимающая Event

        Returns:
            subscription_id (для unsubscribe)
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.info(f"[EventBus] Subscribed handler '{handler.__name__}' to '{event_type}'")
        return f"cb_{event_type}_{handler.__name__}"

    def unsubscribe(self, event_type: str, handler: EventHandler) -> bool:
        """Отписать callback."""
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
                return True
            except ValueError:
                return False
        return False

    # ==================== Automation подписки ====================

    def register_event_automation(
        self,
        user_id: str,
        event_type: str,
        automation_id: str,
        filter_conditions: Optional[Dict[str, Any]] = None,
    ) -> EventSubscription:
        """
        Подписать автоматизацию на событие.

        Когда событие произойдёт, AutomationService.execute_automation будет вызван.

        Args:
            user_id: ID пользователя
            event_type: Тип события
            automation_id: ID автоматизации для выполнения
            filter_conditions: Доп. условия на payload (опционально)

        Returns:
            EventSubscription
        """
        sub = EventSubscription(
            id=str(uuid.uuid4()),
            event_type=event_type,
            user_id=user_id,
            automation_id=automation_id,
            filter_conditions=filter_conditions,
        )

        if event_type not in self._subscriptions:
            self._subscriptions[event_type] = []
        self._subscriptions[event_type].append(sub)

        logger.info(
            f"[EventBus] Registered automation '{automation_id}' for event '{event_type}' "
            f"(user={user_id}, filter={filter_conditions})"
        )
        return sub

    def unregister_event_automation(self, subscription_id: str) -> bool:
        """Отменить подписку автоматизации."""
        for _event_type, subs in self._subscriptions.items():
            for i, sub in enumerate(subs):
                if sub.id == subscription_id:
                    subs.pop(i)
                    logger.info(f"[EventBus] Unregistered subscription '{subscription_id}'")
                    return True
        return False

    def get_user_subscriptions(self, user_id: str) -> List[EventSubscription]:
        """Получить все подписки пользователя."""
        result = []
        for _event_type, subs in self._subscriptions.items():
            for sub in subs:
                if sub.user_id == user_id and sub.active:
                    result.append(sub)
        return result

    # ==================== Эмиссия событий ====================

    async def emit(
        self,
        event_type: str,
        payload: Dict[str, Any],
        user_id: str = "",
        source: str = "",
    ) -> Event:
        """
        Эмитировать событие.

        1. Создаёт Event
        2. Вызывает все callback-и (sync)
        3. Находит подписанные автоматизации и запускает их

        Args:
            event_type: Тип события
            payload: Данные события
            user_id: ID пользователя (если специфичный)
            source: Источник

        Returns:
            Созданный Event
        """
        event = Event(
            id=str(uuid.uuid4()),
            event_type=event_type,
            payload=payload,
            user_id=user_id,
            source=source,
        )

        # Log
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size:]

        logger.info(f"[EventBus] Event emitted: {event_type} (source={source}, user={user_id})")

        # 1. Вызываем callback-и
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"[EventBus] Handler '{handler.__name__}' failed for '{event_type}': {e}")

        # 2. Находим и запускаем автоматизации
        subscriptions = self._subscriptions.get(event_type, [])
        for sub in subscriptions:
            if not sub.active:
                continue

            # Проверяем user_id (если указан в подписке — должен совпадать)
            if sub.user_id and user_id and sub.user_id != user_id:
                continue

            # Проверяем filter_conditions
            if sub.filter_conditions and not self._match_filter(payload, sub.filter_conditions):
                continue

            # Запускаем автоматизацию
            if sub.automation_id:
                asyncio.create_task(
                    self._trigger_automation(sub, event)
                )

        return event

    def _match_filter(self, payload: Dict[str, Any], conditions: Dict[str, Any]) -> bool:
        """Проверить, соответствует ли payload условиям фильтра."""
        for key, expected in conditions.items():
            actual = payload.get(key)
            if actual is None:
                return False

            if isinstance(expected, dict):
                # Поддержка операторов: {"$gt": 10}, {"$contains": "text"}
                for op, val in expected.items():
                    if op == "$gt" and not (actual > val):
                        return False
                    elif op == "$lt" and not (actual < val):
                        return False
                    elif op == "$gte" and not (actual >= val):
                        return False
                    elif op == "$lte" and not (actual <= val):
                        return False
                    elif op == "$contains" and val not in str(actual):
                        return False
                    elif op == "$in" and actual not in val:
                        return False
                    elif op == "$ne" and actual == val:
                        return False
            else:
                # Прямое сравнение
                if actual != expected:
                    return False

        return True

    async def _trigger_automation(self, subscription: EventSubscription, event: Event):
        """Запустить классическую или агентную автоматизацию по событию."""
        try:
            from backend.core.automations.agent_automation_service import (
                get_agent_automation_service,
            )
            from backend.core.automations.automation_service import get_automation_service

            # 1) Классическая автоматизация
            service = get_automation_service()
            classic_automation = await service.get_automation(subscription.automation_id)
            if classic_automation:
                if classic_automation.status != "active":
                    logger.info(
                        f"[EventBus] Skipping non-active automation '{classic_automation.name}' "
                        f"(status={classic_automation.status})"
                    )
                    return

                # Обогащаем action_data данными из события
                enriched_data = dict(classic_automation.action_data)
                enriched_data["_event"] = {
                    "id": event.id,
                    "type": event.event_type,
                    "payload": event.payload,
                    "source": event.source,
                    "timestamp": event.timestamp,
                }

                # Подставляем переменные из payload в action_data
                # Поддержка {{payload.key}} шаблонов
                enriched_data = self._resolve_templates(enriched_data, event.payload)

                # Meeting-workflows читают meeting_id из action_data напрямую —
                # прокидываем известные ключи события, если их там нет (иначе
                # событийный «Отчёт по встрече» падал бы «meeting_id required»)
                for _k in ("meeting_id", "project_id", "folder_id"):
                    if not enriched_data.get(_k) and event.payload.get(_k):
                        enriched_data[_k] = event.payload[_k]

                # Временно подменяем action_data
                original_data = classic_automation.action_data
                classic_automation.action_data = enriched_data

                logger.info(
                    f"[EventBus] Triggering classic automation '{classic_automation.name}' "
                    f"for event '{event.event_type}'"
                )

                result = await service.execute_automation(classic_automation)

                # Восстанавливаем
                classic_automation.action_data = original_data

                logger.info(
                    f"[EventBus] Classic automation '{classic_automation.name}' result: "
                    f"success={result.get('success')}"
                )
                return

            # 2) Агентная автоматизация
            agent_service = get_agent_automation_service()
            agent_automation = await agent_service.get(subscription.automation_id)
            if agent_automation:
                if agent_automation.status != "active":
                    logger.info(
                        f"[EventBus] Skipping non-active agent automation '{agent_automation.name}' "
                        f"(status={agent_automation.status})"
                    )
                    return

                # Даём агенту контекст события в instruction, без захардкоженных данных.
                event_context = {
                    "id": event.id,
                    "type": event.event_type,
                    "payload": event.payload,
                    "source": event.source,
                    "timestamp": event.timestamp,
                }
                original_instruction = agent_automation.instruction
                agent_automation.instruction = (
                    f"{original_instruction}\n\n"
                    f"Event context (JSON): {json.dumps(event_context, ensure_ascii=False)}"
                )

                logger.info(
                    f"[EventBus] Triggering agent automation '{agent_automation.name}' "
                    f"for event '{event.event_type}'"
                )
                try:
                    result = await agent_service.execute(agent_automation)
                finally:
                    agent_automation.instruction = original_instruction

                logger.info(
                    f"[EventBus] Agent automation '{agent_automation.name}' result: "
                    f"success={result.get('success')}"
                )
                return

            logger.warning(
                f"[EventBus] Automation '{subscription.automation_id}' not found "
                f"in classic or agent services for event '{event.event_type}'"
            )

        except Exception as e:
            logger.error(
                f"[EventBus] Failed to trigger automation "
                f"'{subscription.automation_id}': {e}"
            )

    def _resolve_templates(self, data: Any, payload: Dict[str, Any]) -> Any:
        """
        Заменяет {{payload.key}} шаблоны в action_data.

        Например:
            {"message": "Встреча '{{title}}' завершена"}
            + payload {"title": "Standup"}
            = {"message": "Встреча 'Standup' завершена"}
        """
        if isinstance(data, str):
            import re
            def replacer(match):
                key = match.group(1).strip()
                # Поддержка вложенных ключей через точку: payload.nested.key
                value = payload
                for part in key.split("."):
                    if isinstance(value, dict):
                        value = value.get(part, match.group(0))
                    else:
                        return match.group(0)
                return str(value)
            return re.sub(r"\{\{(\S+?)\}\}", replacer, data)
        elif isinstance(data, dict):
            return {k: self._resolve_templates(v, payload) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._resolve_templates(item, payload) for item in data]
        return data

    # ==================== Информация ====================

    def get_recent_events(self, limit: int = 20, event_type: Optional[str] = None) -> List[Event]:
        """Получить последние события."""
        events = self._event_log
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Статистика EventBus."""
        handler_counts = {k: len(v) for k, v in self._handlers.items()}
        subscription_counts = {k: len(v) for k, v in self._subscriptions.items()}
        return {
            "total_handlers": sum(handler_counts.values()),
            "total_subscriptions": sum(subscription_counts.values()),
            "handlers_by_type": handler_counts,
            "subscriptions_by_type": subscription_counts,
            "events_in_log": len(self._event_log),
        }


# === Singleton ===

_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Получить singleton EventBus."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
