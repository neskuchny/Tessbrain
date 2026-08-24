# -*- coding: utf-8 -*-
"""Детекторы сигналов «Пульса исполнения» — чистые функции над реестром.

Каждый сигнал — ровно то, что руководитель спросил бы у живого менеджера:
«что просрочено? у чего нет срока? кто перегружен? что висит месяц?».
Никакого LLM: только факты реестра, тестируется без БД.
Каталог — docs/EXECUTION_PULSE.md §5.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional

STALE_DAYS = 30          # «висит»: нет движения столько дней
SOON_DAYS = 2            # «дедлайн на носу»
OVERLOAD_MIN = 4         # перегруз не раньше этого абсолютного порога


def _p(task: Dict[str, Any]) -> Dict[str, Any]:
    return task.get("_pulse") or {}


def _deadline(task: Dict[str, Any]) -> Optional[date]:
    raw = _p(task).get("deadline")
    try:
        return date.fromisoformat(raw) if raw else None
    except Exception:
        return None


def _days_since(iso: str, now: datetime) -> int:
    try:
        return max(0, int((now - datetime.fromisoformat(iso)).total_seconds()
                          // 86400))
    except Exception:
        return 0


def detect_signals(tasks: List[Dict[str, Any]], *,
                   today: Optional[date] = None,
                   now: Optional[datetime] = None,
                   trackers_configured: bool = False) -> Dict[str, Any]:
    """→ сигналы по категориям + счётчики. Задачи должны пройти build_ledger
    (несут _pulse). deferred уважается: не в просрочку, отдельной строкой.

    trackers_configured=True добавляет сверку «встреча↔задачник»: открытая
    задача, прозвучавшая на встрече (source=graph), но не сматченная ни с
    одной задачей трекера → сигнал not_in_tracker. То, что человек не завёл
    задачу в задачник, не значит, что её не ставили на встрече."""
    today = today or date.today()
    now = now or datetime.now(timezone.utc)

    out: Dict[str, List[Dict[str, Any]]] = {
        "overdue": [], "no_deadline": [], "no_owner": [],
        "deadline_soon": [], "stale": [], "blocked": [], "deferred": [],
        "not_in_tracker": [],
    }
    open_by_owner: Dict[str, int] = {}
    done_count = 0

    for t in tasks:
        bucket = _p(t).get("bucket") or "todo"
        if bucket == "done":
            done_count += 1
            continue
        if bucket == "deferred":
            out["deferred"].append(t)
            continue

        owner = str(t.get("assignee") or t.get("assignee_name") or "").strip()
        dl = _deadline(t)

        if not owner:
            out["no_owner"].append(t)
        else:
            open_by_owner[owner] = open_by_owner.get(owner, 0) + 1

        if bucket == "blocked":
            out["blocked"].append(t)

        if dl is None:
            out["no_deadline"].append(t)
        elif dl < today:
            t["_overdue_days"] = (today - dl).days
            out["overdue"].append(t)
        elif (dl - today).days <= SOON_DAYS:
            out["deadline_soon"].append(t)

        idle = _days_since(_p(t).get("last_activity_at") or "", now)
        if idle >= STALE_DAYS:
            t["_idle_days"] = idle
            out["stale"].append(t)

        # сверка встреча↔задачник: после dedup_tasks задача, сматченная с
        # трекерной, несёт tracker/tracker_task_id; graph-only без них =
        # прозвучала на встрече, в задачник не попала
        if (trackers_configured
                and not str(t.get("tracker") or "").strip()
                and all((s.get("source") or "") == "graph"
                        for s in (t.get("sources") or [{"source":
                                                        t.get("source")}]))):
            out["not_in_tracker"].append(t)

    # перегруз: открытых у человека ≥ max(OVERLOAD_MIN, 2×медианы по людям)
    overloaded: List[Dict[str, Any]] = []
    if open_by_owner:
        med = median(open_by_owner.values())
        threshold = max(OVERLOAD_MIN, int(2 * med))
        overloaded = [{"owner": o, "open": n}
                      for o, n in sorted(open_by_owner.items(),
                                         key=lambda kv: -kv[1])
                      if n >= threshold and n > med]

    out["overdue"].sort(key=lambda t: -t.get("_overdue_days", 0))
    out["stale"].sort(key=lambda t: -t.get("_idle_days", 0))

    open_total = sum(open_by_owner.values()) + len(out["no_owner"])
    return {
        "signals": out,
        "overloaded": overloaded,
        "counts": {k: len(v) for k, v in out.items()},
        "open_total": open_total,
        "done_total": done_count,
        "by_owner_open": open_by_owner,
    }
