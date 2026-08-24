"""Unit-тесты L2: create_and_prepare_task / attach_or_describe / _find_done_column.

Meetflow-модуль (_load_meetflow) подменяется фейком — без Supabase/ключей.
Проверяем ЧЕСТНОСТЬ: jira/trello done через transition/move, yougile ТЗ в
описание (нет comment-API), linear отклонён, done-колонка резолвится по
ключу `name`.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from backend.core.tasks import task_actions


def _run(coro):
    return asyncio.run(coro)


class FakeMF:
    def __init__(self):
        self.calls = []

    def set_user_context(self, **kw):
        pass

    async def create_task_universal(self, system, title, column_id="", description="",
                                    priority="medium", assignee="", due_date=""):
        self.calls.append(("create", system, title, column_id))
        if not column_id and system in ("yougile", "trello"):
            return json.dumps({"error": "column_id required"})
        return json.dumps({"success": True, "task_id": "T1"})

    async def add_task_comment(self, system, task_id, text):
        self.calls.append(("comment", system, task_id))
        if system == "yougile":
            return json.dumps({"error": "yougile does not support comments via API"})
        return json.dumps({"success": True})

    async def update_task_universal(self, system, task_id, title="", description="",
                                    priority="", assignee="", due_date=""):
        self.calls.append(("update", system, task_id, description))
        return json.dumps({"success": True})

    async def move_task_universal(self, system, task_id, target):
        self.calls.append(("move", system, task_id, target))
        return json.dumps({"success": True})

    async def list_task_columns(self, system):
        return json.dumps({"columns": [
            {"id": "c1", "name": "To Do"},
            {"id": "c2", "name": "Done"},
        ]})


@pytest.fixture
def fake(monkeypatch):
    mf = FakeMF()
    monkeypatch.setattr(task_actions, "_load_meetflow", lambda user_id: mf)
    return mf


def test_jira_create_attach_comment_and_mark_done(fake) -> None:
    r = _run(task_actions.create_and_prepare_task(
        "u1", "jira", title="Сделать КП", tz_text="# ТЗ",
        column_id="KP", mark_done=True))
    assert r["status"] == "success"
    assert r["task_id"] == "T1"
    assert any(c[0] == "comment" and c[1] == "jira" for c in fake.calls)
    assert ("move", "jira", "T1", "Done") in fake.calls


def test_yougile_attaches_to_description_not_comment(fake) -> None:
    r = _run(task_actions.create_and_prepare_task(
        "u1", "yougile", title="Презентация", tz_text="# ТЗ",
        column_id="col1", mark_done=False))
    assert r["status"] == "success"
    assert any(c[0] == "update" and c[1] == "yougile" for c in fake.calls)
    assert not any(c[0] == "comment" for c in fake.calls)


def test_linear_rejected(fake) -> None:
    r = _run(task_actions.create_and_prepare_task("u1", "linear", title="x", tz_text="y"))
    assert r["status"] == "error"
    assert "linear" in r["message"].lower()


def test_create_failure_propagates(fake) -> None:
    # yougile без column_id → адаптер вернёт 'column_id required'
    r = _run(task_actions.create_and_prepare_task(
        "u1", "yougile", title="x", tz_text="y", column_id=""))
    assert r["status"] == "error"


def test_attach_or_describe_routing(fake) -> None:
    _run(task_actions.attach_or_describe("u1", "yougile", "T1", text="ТЗ"))
    assert any(c[0] == "update" for c in fake.calls)
    fake.calls.clear()
    _run(task_actions.attach_or_describe("u1", "trello", "T1", text="ТЗ"))
    assert any(c[0] == "comment" for c in fake.calls)


def test_find_done_column_uses_name_key(fake) -> None:
    col = _run(task_actions._find_done_column(fake, "trello"))
    assert col == "c2"


def test_find_done_column_ambiguous_returns_none(monkeypatch, fake) -> None:
    # Две done-колонки (разные доски) → None, чтобы не двигать на чужую доску.
    async def two_done(system):
        return json.dumps({"columns": [
            {"id": "c1", "name": "Done"},
            {"id": "c2", "name": "Closed"},
        ]})
    monkeypatch.setattr(fake, "list_task_columns", two_done)
    assert _run(task_actions._find_done_column(fake, "yougile")) is None


def test_create_partial_when_mark_done_fails(monkeypatch, fake) -> None:
    # move возвращает ошибку → задача создана, но status=partial (не врём).
    async def move_err(system, task_id, target):
        return json.dumps({"error": "transition not found"})
    monkeypatch.setattr(fake, "move_task_universal", move_err)
    r = _run(task_actions.create_and_prepare_task(
        "u1", "jira", title="x", tz_text="y", column_id="KP", mark_done=True))
    assert r["status"] == "partial"
    assert r["task_id"] == "T1"           # задача всё же создана
    assert any("done" in w.lower() for w in r.get("warnings", []))
