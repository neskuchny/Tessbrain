# -*- coding: utf-8 -*-
"""
Event ingestion glue — подключение EventLog к РЕАЛЬНОЙ обработке встреч
(MEMORY_DESIGN_PRINCIPLES §6 №4).

Записывает СТРУКТУРНЫЕ результаты встречи (decisions/tasks — уже извлечённые,
не lossy) как доменные события: decision_made / task_created. Это даёт реальный
СЧЁТ во времени («сколько решений в Q3», «сколько задач назначено на X»), который
frequency_tracker (упоминания) и vector (поиск) не дают.

Принципы (как у mention_ingestion):
- Non-lossy: события из уже-структурных decisions/tasks, без нового LLM-извлечения.
- Идемпотентность по явному event_id = "{kind}::{meeting_id}::{decision/task_id}"
  → повторная обработка той же встречи НЕ задваивает.
- Best-effort: ошибка → {"error": ...}, вызывающего не роняем.
- store инъектируем (тесты); по умолчанию per-user JSON по tenant-пути.
- at = дата встречи (meeting_metadata.date), запас — now.
"""
from __future__ import annotations

from typing import Optional


def _open_user_store(user_id: str):
    from backend.core.memory.event_log import EventLog
    from backend.core.store.tenant_paths import event_log_path_for_user
    return EventLog(persist_path=event_log_path_for_user(user_id))


def _meeting_date(meeting_metadata: Optional[dict]) -> Optional[str]:
    if isinstance(meeting_metadata, dict):
        d = meeting_metadata.get("date")
        if d:
            return str(d)
    return None


def build_meeting_events(results: dict, *, meeting_id: str, user_id: str,
                         at: Optional[str] = None) -> list:
    """Собрать список событий из результатов встречи (decisions + tasks).
    Чистая функция (без I/O) — легко тестируется. Возвращает list[dict-kwargs]
    для EventLog.record."""
    events: list = []
    for d in (results.get("decisions") or []):
        if not isinstance(d, dict):
            continue
        did = d.get("decision_id") or ""
        if not did:
            continue
        events.append({
            "actor": user_id, "kind": "decision_made", "at": at,
            "source": meeting_id,
            "event_id": f"decision_made::{meeting_id}::{did}",
            "payload": {"decision_id": did,
                        "summary": (d.get("decision_summary") or "")[:200],
                        "category": d.get("decision_category", "")},
        })
    for t in (results.get("tasks") or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("task_id") or ""
        if not tid:
            continue
        assignee = t.get("assignee")
        assignee_name = assignee.get("name", "") if isinstance(assignee, dict) else (assignee or "")
        deadline = t.get("deadline")
        deadline_date = deadline.get("date", "") if isinstance(deadline, dict) else (deadline or "")
        events.append({
            "actor": user_id, "kind": "task_created", "at": at,
            "source": meeting_id,
            "event_id": f"task_created::{meeting_id}::{tid}",
            "payload": {"task_id": tid, "title": (t.get("title") or "")[:200],
                        "assignee": assignee_name, "deadline": deadline_date,
                        "priority": t.get("priority", "")},
        })
    return events


def ingest_meeting_events(results: dict, *, meeting_id: str, user_id: str,
                          meeting_metadata: Optional[dict] = None, store=None) -> dict:
    """Записать события встречи (решения/задачи) в EventLog. Best-effort.

    Возвращает {"ingested": bool, "added": int, "existed": int} или
    {"ingested": False, "error": ...}.
    """
    if not (user_id and meeting_id) or not isinstance(results, dict):
        return {"ingested": False, "error": "user_id, meeting_id, results обязательны"}
    at = _meeting_date(meeting_metadata)
    events = build_meeting_events(results, meeting_id=meeting_id, user_id=user_id, at=at)
    if not events:
        return {"ingested": False, "error": "нет решений/задач для событий"}
    try:
        st = store if store is not None else _open_user_store(user_id)
        res = st.record_many(events)
        return {"ingested": True, "added": res["added"], "existed": res["existed"]}
    except Exception as e:
        return {"ingested": False, "error": f"{type(e).__name__}: {e}"}
