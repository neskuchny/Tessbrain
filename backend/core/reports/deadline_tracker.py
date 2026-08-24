# -*- coding: utf-8 -*-
"""Детектор переноса дедлайнов: задача «переносится и не делается».

Пункт 2 клиентского запроса «загрузка сотрудника». Ни граф, ни трекеры не
хранят ИСТОРИЮ дедлайна — только текущее значение, поэтому «перенёс срок
три раза» было невидимо. Здесь копится маленькая per-user история:
{task_key: [{d: дедлайн, seen: когда увидели}]} в data/deadline_history/.

Запись — при каждом сборе задач (отчёт по сотруднику, analyze_tasks):
дедлайн изменился с прошлого раза → новая точка. Перенос = у НЕзакрытой
задачи ≥2 разных дедлайнов и последний ПОЗЖЕ первого.

Честное ограничение: история копится с момента внедрения и только в
моменты сбора задач (отчёт/анализ/расписание) — переносы «между замерами»
одной точкой не различить. Never-raise: сбой стора не ломает отчёты.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_POINTS = 12          # точек истории на задачу (хватает и экономно)
_MAX_TASKS = 2000         # задач в сторе (страховка от распухания)


def _dir() -> Path:
    from backend.core.store.tenant_paths import _DATA_ROOT
    d = _DATA_ROOT / "deadline_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(user_id: str) -> Path:
    from backend.core.store.tenant_paths import _require_uuid
    return _dir() / f"{_require_uuid(user_id, 'user_id')}.json"


def task_key(task: Dict[str, Any]) -> str:
    """Стабильный ключ задачи: источник+id, иначе нормализованный заголовок
    (id у извлечённых из встреч задач не всегда стабилен между прогонами)."""
    src = str(task.get("source") or task.get("tracker") or "graph").lower()
    tid = str(task.get("id") or task.get("task_id") or "").strip()
    if tid:
        return f"{src}:{tid}"
    title = re.sub(r"\s+", " ", str(task.get("title") or "").strip().lower())
    return f"{src}:t:{title[:120]}"


def _load(user_id: str) -> Dict[str, List[Dict[str, str]]]:
    try:
        p = _path(user_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("deadline history read failed", exc_info=True)
    return {}


def _save(user_id: str, data: Dict[str, Any]) -> None:
    try:
        if len(data) > _MAX_TASKS:  # старейшие по последнему seen — на выход
            items = sorted(data.items(),
                           key=lambda kv: (kv[1] or [{}])[-1].get("seen", ""))
            data = dict(items[-_MAX_TASKS:])
        p = _path(user_id)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except Exception:
        logger.debug("deadline history write failed", exc_info=True)


def record_deadlines(user_id: str, tasks: List[Dict[str, Any]]) -> int:
    """Зафиксировать текущие дедлайны задач. Возвращает число НОВЫХ точек
    (изменившихся дедлайнов). Задачи без дедлайна пропускаются."""
    from backend.core.reports.employee_report import _parse_deadline

    data = _load(user_id)
    changed = 0
    now = datetime.now(timezone.utc).isoformat()
    for t in tasks or []:
        dl = _parse_deadline(t.get("deadline") or t.get("due_date"))
        if not dl:
            continue
        key = task_key(t)
        hist = data.setdefault(key, [])
        if hist and hist[-1].get("d") == dl.isoformat():
            continue                       # не изменился — не пишем
        hist.append({"d": dl.isoformat(), "seen": now})
        del hist[:-_MAX_POINTS]
        changed += 1
    if changed:
        _save(user_id, data)
    return changed


def postponed_tasks(user_id: str,
                    tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Из переданных (уже отфильтрованных по человеку) задач — те, чей
    дедлайн ПЕРЕНОСИЛСЯ: ≥2 разных значений в истории, последний позже
    первого, задача не закрыта."""
    from backend.core.tasks.task_analysis import _bucket

    data = _load(user_id)
    out: List[Dict[str, Any]] = []
    for t in tasks or []:
        if _bucket(str(t.get("status") or "")) == "done":
            continue
        hist = data.get(task_key(t)) or []
        dls = [h.get("d") for h in hist if h.get("d")]
        if len(set(dls)) < 2:
            continue
        if dls[-1] > dls[0]:               # ISO-строки сравнимы лексикографски
            out.append({**t, "_shifts": len(set(dls)) - 1,
                        "_from": dls[0], "_to": dls[-1]})
    out.sort(key=lambda x: -x["_shifts"])
    return out
