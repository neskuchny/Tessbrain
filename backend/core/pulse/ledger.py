# -*- coding: utf-8 -*-
"""Task Ledger — единый реестр задач со статус-историей (Пульс, фаза 1).

Задачи живут в трёх несинхронизированных местах (граф, meeting_tasks,
внешние трекеры) и до сих пор склеивались конкатенацией без дедупа — одна
задача могла посчитаться дважды, а истории изменений не было ни у одной.

Ledger: (1) собирает всё, (2) дедуплицирует между источниками тем же
алгоритмом, что проверенный _reconcile_completed_task (скоуп исполнителя +
SequenceMatcher по названию), (3) ведёт per-task состояние между прогонами —
статус-историю, last_activity_at, факт просрочки — и (4) впервые ЭМИТИТ
события TASK_COMPLETED / TASK_OVERDUE в EventBus, на которые доски и
автоматизации давно подписаны, но которых никто не производил.

Только чтение источников; состояние — data/pulse/<user_id>.json.
См. docs/EXECUTION_PULSE.md §3.1.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.tasks.status_norm import normalize_status

logger = logging.getLogger(__name__)

_TITLE_SIM = 0.72          # порог схожести названий (как в reconcile: ~0.7)
_STATE_KEEP_DAYS = 180     # записи, не виденные полгода, вычищаются


def _state_path(user_id: str) -> Path:
    base = Path(os.environ.get("PULSE_STATE_DIR", "").strip() or "data/pulse")
    return base / f"{user_id}.json"


def _load_state(user_id: str) -> Dict[str, Any]:
    try:
        p = _state_path(user_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("pulse state unreadable — начинаем заново", exc_info=True)
    return {}


def _save_state(user_id: str, state: Dict[str, Any]) -> None:
    p = _state_path(user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _norm(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def task_key(task: Dict[str, Any]) -> str:
    """Стабильный ключ задачи в реестре: внешний id, иначе владелец+название."""
    tracker_id = str(task.get("tracker_task_id") or "").strip()
    if tracker_id:
        return f"{task.get('tracker') or 'ext'}:{tracker_id}"
    tid = str(task.get("id") or task.get("task_id") or "").strip()
    if tid:
        return f"id:{tid}"
    return f"t:{_norm(task.get('assignee'))}:{_norm(task.get('title'))[:80]}"


def _parse_deadline(raw: Any) -> Optional[date]:
    s = str(raw or "").strip()
    if not s:
        return None
    for cut in (10, len(s)):
        try:
            return date.fromisoformat(s[:cut])
        except Exception:
            continue
    return None


def dedup_tasks(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Слить дубли одной задачи из разных источников.

    Тот же принцип, что в graph_builder._reconcile_completed_task: похожее
    название (SequenceMatcher ≥ порога) в скоупе одного исполнителя. Каноном
    становится более «богатая» запись (есть дедлайн/трекер), источники
    складываются в sources[]."""
    canon: List[Dict[str, Any]] = []
    for t in tasks:
        title = _norm(t.get("title"))
        assignee = _norm(t.get("assignee") or t.get("assignee_name"))
        merged = False
        if title:
            for c in canon:
                c_assignee = _norm(c.get("assignee") or c.get("assignee_name"))
                if assignee and c_assignee and assignee != c_assignee:
                    continue
                if SequenceMatcher(None, title,
                                   _norm(c.get("title"))).ratio() < _TITLE_SIM:
                    continue
                # merge: не терять более информативные поля
                for k in ("deadline", "due_date", "assignee", "tracker",
                          "tracker_task_id", "description", "priority"):
                    if not str(c.get(k) or "").strip() and str(t.get(k) or "").strip():
                        c[k] = t[k]
                # закрытый статус побеждает (источник, который знает больше)
                if normalize_status(t.get("status")) == "done":
                    c["status"] = t.get("status")
                c.setdefault("sources", []).append(
                    {"source": t.get("source") or "", "id": t.get("id") or ""})
                merged = True
                break
        if not merged:
            t = dict(t)
            t["sources"] = [{"source": t.get("source") or "",
                             "id": t.get("id") or ""}]
            canon.append(t)
    return canon


async def _emit_events(user_id: str, events: List[Dict[str, Any]]) -> None:
    """Первый в системе продюсер TASK_COMPLETED/TASK_OVERDUE. Best-effort."""
    if not events:
        return
    try:
        from backend.core.automations.event_bus import EventType, get_event_bus
        bus = get_event_bus()
        for ev in events:
            etype = (EventType.TASK_COMPLETED if ev["type"] == "task_completed"
                     else EventType.TASK_OVERDUE)
            await bus.emit(etype, {"user_id": user_id, **ev})
    except Exception:
        logger.debug("pulse: события не отправлены", exc_info=True)


async def build_ledger(user_id: str, *, emit_events: bool = True
                       ) -> Dict[str, Any]:
    """Собрать реестр: задачи всех источников + состояние + события переходов.

    Возврат: {"tasks": [...с полем _pulse...], "events": [...], "stats": {...}}.
    """
    from backend.core.tasks.task_analysis import (
        collect_tasks,
        collect_tasks_from_trackers,
    )
    raw: List[Dict[str, Any]] = []
    try:
        raw.extend(await collect_tasks(user_id) or [])
    except Exception:
        logger.warning("pulse: граф-задачи недоступны", exc_info=True)
    try:
        raw.extend(await collect_tasks_from_trackers(user_id) or [])
    except Exception:
        logger.warning("pulse: трекеры недоступны", exc_info=True)

    tasks = dedup_tasks(raw)
    state = _load_state(user_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    today = date.today()
    events: List[Dict[str, Any]] = []
    seen_keys = set()

    for t in tasks:
        key = task_key(t)
        seen_keys.add(key)
        bucket = normalize_status(t.get("status"))
        deadline = _parse_deadline(t.get("deadline") or t.get("due_date"))
        rec = state.get(key)
        if rec is None:
            rec = {"first_seen": now_iso, "status": bucket,
                   "status_history": [{"status": bucket, "at": now_iso}],
                   "last_activity_at": now_iso}
        else:
            if bucket != rec.get("status"):
                rec.setdefault("status_history", []).append(
                    {"status": bucket, "at": now_iso})
                rec["status"] = bucket
                rec["last_activity_at"] = now_iso
                if bucket == "done":
                    events.append({"type": "task_completed", "key": key,
                                   "title": t.get("title"),
                                   "assignee": t.get("assignee")})
            old_dl = rec.get("deadline")
            new_dl = deadline.isoformat() if deadline else None
            if new_dl != old_dl and (new_dl or old_dl):
                rec["last_activity_at"] = now_iso
        rec["deadline"] = deadline.isoformat() if deadline else None
        rec["last_seen"] = now_iso

        # просрочка — событие ОДИН раз на задачу (не каждый прогон)
        if (bucket not in ("done", "deferred") and deadline
                and deadline < today and not rec.get("overdue_flagged")):
            rec["overdue_flagged"] = True
            events.append({"type": "task_overdue", "key": key,
                           "title": t.get("title"),
                           "assignee": t.get("assignee"),
                           "deadline": deadline.isoformat()})
        if bucket == "done":
            rec.pop("overdue_flagged", None)

        state[key] = rec
        t["_pulse"] = {"key": key, "bucket": bucket,
                       "deadline": rec["deadline"],
                       "first_seen": rec["first_seen"],
                       "last_activity_at": rec["last_activity_at"],
                       "status_history": rec.get("status_history", [])}

    # чистка давно исчезнувших записей
    cutoff = datetime.now(timezone.utc).timestamp() - _STATE_KEEP_DAYS * 86400
    for key in list(state.keys()):
        if key in seen_keys:
            continue
        try:
            last = datetime.fromisoformat(state[key].get("last_seen") or
                                          state[key].get("first_seen"))
            if last.timestamp() < cutoff:
                del state[key]
        except Exception:
            del state[key]

    _save_state(user_id, state)
    if emit_events:
        await _emit_events(user_id, events)

    return {"tasks": tasks, "events": events,
            "stats": {"raw": len(raw), "deduped": len(tasks),
                      "merged_away": len(raw) - len(tasks),
                      "transitions": len(events)}}
