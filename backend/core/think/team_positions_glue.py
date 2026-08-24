# -*- coding: utf-8 -*-
"""
Team-positions glue — позиции команд из РЕАЛЬНЫХ структурных данных
(docs/CAPABILITY_READINESS.md, C-glue для team_misalignment).

ВХОД (как ДАННЫЕ, инъекция — не I/O в этом модуле, тестируется без инфры):
- решения встреч (EventLog kind='decision_made' c summary в payload), либо
  decisions из results встречи как есть;
- задачи (kind='task_created' с title); опционально.
- person_to_team: dict {имя/id person → team_name} — резолвится из графа
  (Person→Team MEMBER_OF) или Persona/Snapshot. Сборка карты — отдельным
  glue (graph→map), здесь принимаем готовой.

ВЫХОД: list[TeamPosition] для team_misalignment.detect_team_misalignment.

Принципы:
- СКЕЛЕТ: декомпозиция в коде; LLM не участвует (он только в детекторе).
- ЧЕСТНОСТЬ: события без владельца/команды просто пропускаются (счётчик
  unresolved в результате) — НЕ присваиваем «по умолчанию», не выдумываем.
- Идемпотентность по source-полю позиции (meeting_id::event_id).
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Optional, Tuple

# Импортируем модель позиции из детектора — единая точка правды
from backend.core.think.team_misalignment import TeamPosition


def _normalize_person(p) -> str:
    if not p:
        return ""
    if isinstance(p, dict):
        return str(p.get("name") or p.get("id") or "").strip()
    return str(p).strip()


def _resolve_team(person_key: str, person_to_team: Mapping[str, str]) -> Optional[str]:
    """Карта person→team: точный матч → case-insensitive → None."""
    if not person_key:
        return None
    if person_key in person_to_team:
        return person_to_team[person_key]
    low = person_key.casefold()
    for k, v in person_to_team.items():
        if k.casefold() == low:
            return v
    return None


def positions_from_decisions(
    decisions: Iterable[dict],
    person_to_team: Mapping[str, str],
    *,
    meeting_id: str = "",
    meeting_date: str = "",
) -> Tuple[List[TeamPosition], dict]:
    """Решения встречи → позиции команд.

    decisions — список dict'ов как в results встречи: {decision_id, decision_summary,
    decision_owner (или owner), meeting_id?, decision_category?}. owner может быть
    str или {"name": ...}. Возвращает (позиции, {resolved, unresolved}).
    """
    positions: List[TeamPosition] = []
    resolved = 0
    unresolved = 0
    for d in decisions or []:
        if not isinstance(d, dict):
            continue
        statement = (d.get("decision_summary") or d.get("summary") or
                     d.get("title") or "").strip()
        if not statement:
            continue
        owner = _normalize_person(d.get("decision_owner") or d.get("owner")
                                  or d.get("author") or d.get("actor"))
        team = _resolve_team(owner, person_to_team)
        if not team:
            unresolved += 1
            continue
        mid = d.get("meeting_id") or meeting_id or ""
        did = d.get("decision_id") or ""
        source = f"{mid}::{did}" if (mid and did) else (mid or did or "")
        at = d.get("decided_at") or d.get("at") or meeting_date or ""
        positions.append(TeamPosition(team=team, statement=statement,
                                      at=at, source=source))
        resolved += 1
    return positions, {"resolved": resolved, "unresolved": unresolved}


def positions_from_event_log(
    events: Iterable[dict],
    person_to_team: Mapping[str, str],
    *,
    kinds: Tuple[str, ...] = ("decision_made",),
) -> Tuple[List[TeamPosition], dict]:
    """Доменные события (EventLog) → позиции. Безопасно работает с decision_made
    и task_created. По умолчанию — только решения (статусы команд)."""
    positions: List[TeamPosition] = []
    resolved = 0
    unresolved = 0
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("kind") not in kinds:
            continue
        payload = ev.get("payload") or {}
        statement = (payload.get("summary") or payload.get("title") or "").strip()
        if not statement:
            continue
        owner = _normalize_person(payload.get("decision_owner")
                                  or payload.get("owner")
                                  or payload.get("assignee")
                                  or ev.get("actor"))
        team = _resolve_team(owner, person_to_team)
        if not team:
            unresolved += 1
            continue
        positions.append(TeamPosition(
            team=team, statement=statement,
            at=str(ev.get("at") or ""),
            source=str(ev.get("source") or ev.get("event_id") or "")))
        resolved += 1
    return positions, {"resolved": resolved, "unresolved": unresolved}
