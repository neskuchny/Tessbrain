# -*- coding: utf-8 -*-
"""Тесты структурного озарения (R-4.1 карта органов, R-4.3 gap-анализ).

Чистые _compute_* тестируем на руками собранном networkx-графе — без БД.
"""
from __future__ import annotations

from datetime import date, timedelta

import networkx as nx
import pytest

from backend.core.help.org_health import (
    _compute_organ_map,
    _compute_structure_gaps,
)

_TODAY = date.today().isoformat()
_PAST = (date.today() - timedelta(days=5)).isoformat()
_FUTURE = (date.today() + timedelta(days=5)).isoformat()


def _task(g, nid, *, status="todo", project=None, assignee=None, deadline=None,
          title="задача"):
    attrs = {"_label": "Task", "status": status, "title": title}
    if project:
        attrs["project"] = project
    if assignee:
        attrs["assignee"] = assignee
    if deadline:
        attrs["deadline"] = deadline
    g.add_node(nid, **attrs)


# ── R-4.1 organ map ───────────────────────────────────────────────────────

def test_organ_map_empty_graph() -> None:
    assert _compute_organ_map(nx.MultiDiGraph()) == {"organs": [], "summary": {}}


def test_organ_map_red_on_overdue() -> None:
    g = nx.MultiDiGraph()
    _task(g, "t1", project="Альфа", assignee="Аня", deadline=_PAST)
    _task(g, "t2", project="Альфа", assignee="Аня", deadline=_FUTURE)
    m = _compute_organ_map(g)
    alfa = next(o for o in m["organs"] if o["name"] == "Альфа")
    assert alfa["state"] == "red"
    assert alfa["overdue_tasks"] == 1 and alfa["open_tasks"] == 2
    assert m["summary"].get("red") == 1


def test_organ_map_amber_when_no_owner() -> None:
    g = nx.MultiDiGraph()
    _task(g, "t1", project="Бета", deadline=_FUTURE)  # открыта, без владельца
    m = _compute_organ_map(g)
    beta = next(o for o in m["organs"] if o["name"] == "Бета")
    assert beta["state"] == "amber" and beta["people"] == 0


def test_organ_map_green_healthy() -> None:
    g = nx.MultiDiGraph()
    _task(g, "t1", project="Гамма", assignee="Ким", deadline=_FUTURE)
    m = _compute_organ_map(g)
    gamma = next(o for o in m["organs"] if o["name"] == "Гамма")
    assert gamma["state"] == "green"


def test_organ_map_idle_project_without_tasks() -> None:
    g = nx.MultiDiGraph()
    g.add_node("p1", _label="Project", name="Дельта")
    m = _compute_organ_map(g)
    delta = next(o for o in m["organs"] if o["name"] == "Дельта")
    assert delta["state"] == "idle" and delta["open_tasks"] == 0


def test_organ_map_done_tasks_not_counted() -> None:
    g = nx.MultiDiGraph()
    _task(g, "t1", project="Эпсилон", assignee="Ли", status="выполнено",
          deadline=_PAST)
    m = _compute_organ_map(g)
    eps = next(o for o in m["organs"] if o["name"] == "Эпсилон")
    assert eps["state"] == "idle" and eps["open_tasks"] == 0


def test_organ_map_unassigned_bucket() -> None:
    g = nx.MultiDiGraph()
    _task(g, "t1", assignee="Аня", deadline=_FUTURE)  # без project
    m = _compute_organ_map(g)
    assert any(o["name"] == "Общие задачи" for o in m["organs"])


def test_organ_map_assignee_via_edge() -> None:
    """Исполнитель через ASSIGNED_TO-ребро, а не атрибут."""
    g = nx.MultiDiGraph()
    _task(g, "t1", project="Альфа", deadline=_FUTURE)
    g.add_node("p1", _label="Person", name="Оля")
    g.add_edge("t1", "p1", _type="ASSIGNED_TO")
    m = _compute_organ_map(g)
    alfa = next(o for o in m["organs"] if o["name"] == "Альфа")
    assert alfa["people"] == 1 and alfa["state"] == "green"


# ── R-4.3 structure gaps ──────────────────────────────────────────────────

def test_gaps_empty_graph() -> None:
    assert _compute_structure_gaps(nx.MultiDiGraph()) == {"gaps": [], "summary": {}}


def test_gaps_overload_bottleneck() -> None:
    g = nx.MultiDiGraph()
    for i in range(9):  # ≥ _OVERLOAD_ABS(8) на одном человеке
        _task(g, f"t{i}", project="Альфа", assignee="Аня", deadline=_FUTURE,
              title=f"деплой сервиса {i}")
    r = _compute_structure_gaps(g)
    overload = [x for x in r["gaps"] if x["type"] == "overload"]
    assert overload and overload[0]["where"] == "Аня"
    assert overload[0]["open_tasks"] == 9
    assert "skills_hint" in overload[0]["role_profile"]


def test_gaps_overload_high_severity_on_overdue() -> None:
    g = nx.MultiDiGraph()
    for i in range(8):
        _task(g, f"t{i}", assignee="Аня", deadline=_PAST)
    r = _compute_structure_gaps(g)
    overload = [x for x in r["gaps"] if x["type"] == "overload"][0]
    assert overload["severity"] == "high"


def test_gaps_orphan_tasks() -> None:
    g = nx.MultiDiGraph()
    for i in range(4):  # ≥ _ORPHAN_MIN(3), без исполнителя
        _task(g, f"t{i}", project="Бета", deadline=_FUTURE, title="лендинг рекламный")
    r = _compute_structure_gaps(g)
    orphan = [x for x in r["gaps"] if x["type"] == "orphan"]
    assert orphan and orphan[0]["open_tasks"] == 4


def test_gaps_understaffed_project() -> None:
    g = nx.MultiDiGraph()
    for i in range(6):  # ≥ _UNDERSTAFF_OPEN(5), один человек
        _task(g, f"t{i}", project="Гамма", assignee="Ким", deadline=_FUTURE)
    r = _compute_structure_gaps(g)
    under = [x for x in r["gaps"] if x["type"] == "understaffed"]
    assert under and under[0]["where"] == "Гамма"


def test_gaps_healthy_graph_no_gaps() -> None:
    g = nx.MultiDiGraph()
    _task(g, "t1", project="Альфа", assignee="Аня", deadline=_FUTURE)
    _task(g, "t2", project="Альфа", assignee="Боря", deadline=_FUTURE)
    r = _compute_structure_gaps(g)
    assert r["gaps"] == []


def test_gaps_role_profile_has_no_name_only_role() -> None:
    """R-4.3 инвариант: отдаём профиль роли, не «кого нанять» поимённо."""
    g = nx.MultiDiGraph()
    for i in range(4):
        _task(g, f"t{i}", project="Бета", deadline=_FUTURE, title="аналитика отчёт")
    r = _compute_structure_gaps(g)
    orphan = [x for x in r["gaps"] if x["type"] == "orphan"][0]
    rp = orphan["role_profile"]
    assert set(rp.keys()) == {"need", "skills_hint", "reason"}
    assert isinstance(rp["skills_hint"], list)
