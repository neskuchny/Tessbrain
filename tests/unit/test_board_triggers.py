# -*- coding: utf-8 -*-
"""Тесты триггеров процесс-досок по расписанию (чистая логика)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.core.board.triggers import is_due, schedule_interval

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def test_schedule_interval_present() -> None:
    g = {"nodes": [{"type": "trigger", "data": {"trigger_type": "schedule", "interval_min": 30}}]}
    assert schedule_interval(g) == 30


def test_schedule_interval_min_floor() -> None:
    g = {"nodes": [{"type": "trigger", "data": {"trigger_type": "schedule", "interval_min": 1}}]}
    assert schedule_interval(g) == 5   # пол — 5 мин


def test_schedule_interval_none_for_manual() -> None:
    g = {"nodes": [{"type": "trigger", "data": {"trigger_type": "manual"}}]}
    assert schedule_interval(g) is None
    assert schedule_interval({"nodes": []}) is None


def test_due_when_no_last() -> None:
    assert is_due(30, None, _NOW) is True


def test_due_when_interval_elapsed() -> None:
    last = (_NOW - timedelta(minutes=31)).isoformat()
    assert is_due(30, last, _NOW) is True


def test_not_due_when_within_interval() -> None:
    last = (_NOW - timedelta(minutes=10)).isoformat()
    assert is_due(30, last, _NOW) is False


def test_due_on_bad_last_iso() -> None:
    assert is_due(30, "не дата", _NOW) is True
