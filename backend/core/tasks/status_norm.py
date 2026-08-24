# -*- coding: utf-8 -*-
"""Единая нормализация статусов задач.

До этого в коде жили ТРИ разных словаря (task_analysis, meeting_workflows,
org_health) с разными наборами синонимов — одна и та же задача считалась
закрытой в одном отчёте и открытой в другом. Здесь — один словарь-объединение;
потребители переводятся на него постепенно (task_analysis уже переведён).
"""
from __future__ import annotations

DONE = {
    "done", "completed", "closed", "resolved", "finished", "complete",
    "cancelled", "canceled",
    "выполнена", "выполнено", "готово", "сделано", "закрыта", "закрыт",
    "закрыто", "отменена", "отменено",
}
BLOCKED = {"blocked", "stuck", "on_hold", "заблокирована", "заблокировано",
           "ожидание", "on hold"}
IN_PROGRESS = {"in_progress", "in progress", "doing", "active", "wip",
               "started", "в работе", "делаю", "выполняется"}
DEFERRED = {"deferred", "postponed", "later", "snoozed", "отложена",
            "отложено", "позже"}


def normalize_status(status: str) -> str:
    """→ 'done' | 'blocked' | 'in_progress' | 'deferred' | 'todo'."""
    s = str(status or "").strip().lower()
    if s in DONE:
        return "done"
    if s in BLOCKED:
        return "blocked"
    if s in IN_PROGRESS:
        return "in_progress"
    if s in DEFERRED:
        return "deferred"
    return "todo"


def is_closed(status: str) -> bool:
    return normalize_status(status) == "done"
