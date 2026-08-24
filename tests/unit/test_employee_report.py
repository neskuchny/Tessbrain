# -*- coding: utf-8 -*-
"""Отчёт по сотруднику: встречи + задачи + ВЫЧИСЛЕННАЯ просрочка.

Клиентский запрос «загрузка сотрудника». До фикса «overdue» существовал
только если статус буквально назывался «просрочено»; теперь дедлайн
сравнивается с сегодняшним днём."""
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import backend.core.reports.deadline_tracker as dt
import backend.core.reports.employee_report as er


@pytest.fixture(autouse=True)
def _isolate_deadline_history(monkeypatch, tmp_path):
    """build_employee_report пишет историю дедлайнов — изолируем от реального
    data/ (иначе тесты оставляют артефакты в репозитории)."""
    monkeypatch.setattr(dt, "_path", lambda uid: tmp_path / f"{uid}.json")
from backend.core.reports.employee_report import (
    _assigned_to,
    _parse_deadline,
    build_employee_report,
)

_UID = "11111111-1111-4111-8111-111111111111"


def test_parse_deadline_formats():
    assert _parse_deadline("2026-07-01") == date(2026, 7, 1)
    assert _parse_deadline("до 15.07.2026 включительно") == date(2026, 7, 15)
    assert _parse_deadline("сделать к пятнице") is None
    assert _parse_deadline("") is None


def test_assigned_to_bidirectional_substring():
    assert _assigned_to({"assignee": "Александр Скалабухов"}, "Александр")
    assert _assigned_to({"owner": "саша"}, "Саша Иванов") is True
    assert _assigned_to({"assignee": "Пётр"}, "Мария") is False
    assert _assigned_to({}, "Мария") is False


def test_build_report_overdue_computed(monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    async def _graph_tasks(uid):
        return [
            {"title": "Просроченная", "assignee": "Антон",
             "status": "in_progress", "deadline": yesterday, "source": "graph"},
            {"title": "Сделанная старая", "assignee": "Антон",
             "status": "done", "deadline": yesterday, "source": "graph"},
        ]

    async def _tracker_tasks(uid):
        return [
            {"title": "В срок", "assignee": "Антон Иванов",
             "status": "todo", "deadline": tomorrow, "tracker": "trello"},
            {"title": "Чужая", "assignee": "Мария",
             "status": "todo", "deadline": yesterday, "tracker": "trello"},
        ]

    monkeypatch.setattr("backend.core.tasks.task_analysis.collect_tasks",
                        _graph_tasks)
    monkeypatch.setattr(
        "backend.core.tasks.task_analysis.collect_tasks_from_trackers",
        _tracker_tasks)

    class _FakeGen:
        async def get_person_snapshot(self, person):
            return SimpleNamespace(
                name="Антон", role="Менеджер", department="Продажи",
                meetings_participated=7,
                recent_meetings=[{"title": "Рахмат 08.07",
                                  "date": "2026-07-08",
                                  "role_in_meeting": "участник"}],
                recent_achievements=["закрыл сделку"],
                current_challenges=[],
                collaboration_score=62.4, decisions_made=3)

    monkeypatch.setattr(
        "backend.core.sleep.enhanced_snapshot.get_enhanced_snapshot_generator",
        lambda **kw: _FakeGen())

    rep = asyncio.run(build_employee_report(_UID, "Антон", days=30))
    s = rep["stats"]
    # просрочка ВЫЧИСЛЕНА: только незакрытая с прошедшим дедлайном
    assert s["tasks_overdue"] == 1
    assert rep["overdue"][0]["title"] == "Просроченная"
    # done с прошедшим дедлайном — НЕ просрочка; чужая задача не попала
    assert s["tasks_done"] == 1 and s["tasks_todo"] == 1
    assert s["meetings_total"] == 7 and s["decisions"] == 3
    md = rep["markdown"]
    assert "Отчёт по сотруднику: Антон" in md
    assert "Просроченная" in md and "Чужая" not in md
    assert "Рахмат 08.07" in md
    assert "Ограничения данных" in md   # честность про непокрытое


def test_build_report_survives_missing_sources(monkeypatch):
    async def _boom(uid):
        raise RuntimeError("нет графа")
    monkeypatch.setattr("backend.core.tasks.task_analysis.collect_tasks", _boom)
    monkeypatch.setattr(
        "backend.core.tasks.task_analysis.collect_tasks_from_trackers", _boom)
    monkeypatch.setattr(
        "backend.core.sleep.enhanced_snapshot.get_enhanced_snapshot_generator",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("нет снапшота")))

    rep = asyncio.run(build_employee_report(_UID, "Кто-то", days=7))
    assert rep["stats"]["tasks_overdue"] == 0
    assert "Отчёт по сотруднику: Кто-то" in rep["markdown"]
