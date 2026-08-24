# -*- coding: utf-8 -*-
"""
Automations module - управление отложенными и повторяющимися задачами.

Поддерживает 3 типа триггеров:
- Расписание (cron/interval/once) — AutomationService + AutomationScheduler
- События (EventBus) — event_bus.py
- Условия (conditions) — проверяются перед выполнением
"""
from .automation_service import (
    ActionType,
    AutomationService,
    AutomationStatus,
    ScheduledAutomation,
    ScheduleType,
    get_automation_service,
)
from .event_bus import Event, EventBus, EventSubscription, EventType, get_event_bus
from .scheduler import AutomationScheduler, get_scheduler

__all__ = [
    "ActionType",
    "AutomationScheduler",
    "AutomationService",
    "AutomationStatus",
    "Event",
    "EventBus",
    "EventSubscription",
    "EventType",
    "ScheduleType",
    "ScheduledAutomation",
    "get_automation_service",
    "get_event_bus",
    "get_scheduler",
]


