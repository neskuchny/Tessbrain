# -*- coding: utf-8 -*-
"""Тесты «Пульса» (R-4.2): рецидивы + сезонность по дню недели."""
from __future__ import annotations

from datetime import date, timedelta

import networkx as nx

from backend.core.help.pulse import (
    _compute_pulse,
    _events_from_graph,
)


def _d(iso: str) -> str:
    return iso


# ── рецидивы ────────────────────────────────────────────────────────────────

def test_pulse_empty() -> None:
    assert _compute_pulse([]) == {"recurring": [], "seasonality": [], "summary": {}}


def test_pulse_recurrence_flagged() -> None:
    # «бюджет» всплывает 4 раза в 4 разные недели → рецидив
    events = [
        {"key": "бюджет", "ts": "2026-01-05"},
        {"key": "бюджет", "ts": "2026-01-12"},
        {"key": "бюджет", "ts": "2026-01-19"},
        {"key": "бюджет", "ts": "2026-01-26"},
    ]
    r = _compute_pulse(events)
    rec = r["recurring"]
    assert rec and rec[0]["key"] == "бюджет"
    assert rec[0]["count"] == 4 and rec[0]["weeks"] == 4
    assert rec[0]["regular"] is True          # ровно каждые 7 дней
    assert rec[0]["avg_gap_days"] == 7.0
    assert r["summary"]["recurring"] == 1


def test_pulse_below_threshold_not_recurring() -> None:
    events = [
        {"key": "релиз", "ts": "2026-01-05"},
        {"key": "релиз", "ts": "2026-01-12"},  # только 2 → ниже _RECUR_MIN(3)
    ]
    assert _compute_pulse(events)["recurring"] == []


def test_pulse_same_week_not_recurrence() -> None:
    # 3 раза, но всё в одной неделе → не рецидив (нужно ≥2 недель)
    events = [
        {"key": "инцидент", "ts": "2026-01-05"},
        {"key": "инцидент", "ts": "2026-01-06"},
        {"key": "инцидент", "ts": "2026-01-07"},
    ]
    assert _compute_pulse(events)["recurring"] == []


def test_pulse_irregular_not_marked_regular() -> None:
    events = [
        {"key": "аврал", "ts": "2026-01-01"},
        {"key": "аврал", "ts": "2026-01-03"},
        {"key": "аврал", "ts": "2026-02-20"},  # большой разрыв → нерегулярно
    ]
    rec = _compute_pulse(events)["recurring"]
    assert rec and rec[0]["regular"] is False


def test_pulse_invalid_dates_skipped() -> None:
    events = [{"key": "x", "ts": "не дата"}, {"key": "x", "ts": ""}]
    assert _compute_pulse(events)["recurring"] == []


# ── сезонность ──────────────────────────────────────────────────────────────

def test_pulse_weekday_seasonality() -> None:
    # 6 событий, 5 из них — понедельники → пик по понедельнику
    mondays = ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02"]
    events = [{"key": f"m{i}", "ts": d} for i, d in enumerate(mondays)]
    events.append({"key": "friday", "ts": "2026-01-09"})  # пятница
    r = _compute_pulse(events)
    seas = r["seasonality"]
    assert seas and seas[0]["weekday"] == "понедельник"
    assert seas[0]["count"] == 5 and seas[0]["share"] >= 0.45


def test_pulse_no_seasonality_when_spread() -> None:
    # по одному событию в каждый будний день → нет доминирующего дня
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    events = [{"key": f"m{i}", "ts": d} for i, d in enumerate(days)]
    assert _compute_pulse(events)["seasonality"] == []


# ── события из графа ────────────────────────────────────────────────────────

def test_events_from_graph_meetings_and_tasks() -> None:
    g = nx.MultiDiGraph()
    g.add_node("m1", _label="Meeting", title="Бюджет Q3", date="2026-01-05")
    g.add_node("t1", _label="Task", title="починить деплой", created_at="2026-01-06")
    g.add_node("p1", _label="Person", name="Аня")  # не событие
    events = _events_from_graph(g)
    assert len(events) == 2
    labels = {e["label"] for e in events}
    assert labels == {"Meeting", "Task"}
    # ключ = главное слово заголовка
    assert any(e["key"] in ("бюджет",) for e in events if e["label"] == "Meeting")


def test_events_from_graph_skips_nodes_without_date() -> None:
    g = nx.MultiDiGraph()
    g.add_node("m1", _label="Meeting", title="без даты")
    assert _events_from_graph(g) == []


def test_events_from_graph_end_to_end_recurrence() -> None:
    g = nx.MultiDiGraph()
    for i, d in enumerate(["2026-01-05", "2026-01-12", "2026-01-19"]):
        g.add_node(f"m{i}", _label="Meeting", title="Бюджет обсуждение", date=d)
    r = _compute_pulse(_events_from_graph(g))
    assert r["recurring"] and r["recurring"][0]["key"] == "бюджет"
