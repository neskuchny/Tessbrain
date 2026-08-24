# -*- coding: utf-8 -*-
"""Детектор переноса дедлайнов: история → «задача переносится».

До фикса история дедлайнов нигде не хранилась — «перенёс срок три раза»
было невидимо (источники держат только текущее значение)."""
import backend.core.reports.deadline_tracker as dt

_UID = "11111111-1111-4111-8111-111111111111"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(dt, "_path", lambda uid: tmp_path / f"{uid}.json")


def test_task_key_stable():
    assert dt.task_key({"source": "trello", "id": "abc"}) == "trello:abc"
    k1 = dt.task_key({"title": "Сделать  КП   клиенту"})
    k2 = dt.task_key({"title": "сделать кп клиенту"})
    assert k1 == k2  # нормализация пробелов/регистра


def test_postponed_detected_after_shifts(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    t = {"source": "trello", "id": "1", "title": "Отчёт",
         "status": "in_progress"}

    # замер 1: дедлайн 10-е; повторный замер того же — точка не дублируется
    assert dt.record_deadlines(_UID, [{**t, "deadline": "2026-07-10"}]) == 1
    assert dt.record_deadlines(_UID, [{**t, "deadline": "2026-07-10"}]) == 0
    assert dt.postponed_tasks(_UID, [{**t, "deadline": "2026-07-10"}]) == []

    # замер 2 и 3: перенесли на 15-е, потом на 20-е
    dt.record_deadlines(_UID, [{**t, "deadline": "2026-07-15"}])
    dt.record_deadlines(_UID, [{**t, "deadline": "2026-07-20"}])
    out = dt.postponed_tasks(_UID, [{**t, "deadline": "2026-07-20"}])
    assert len(out) == 1
    assert out[0]["_shifts"] == 2
    assert out[0]["_from"] == "2026-07-10" and out[0]["_to"] == "2026-07-20"


def test_done_and_earlier_shift_not_postponed(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    done = {"source": "jira", "id": "d1", "title": "Сделана", "status": "done"}
    dt.record_deadlines(_UID, [{**done, "deadline": "2026-07-01"}])
    dt.record_deadlines(_UID, [{**done, "deadline": "2026-07-09"}])
    assert dt.postponed_tasks(_UID, [{**done, "deadline": "2026-07-09"}]) == []

    # сдвиг ВЛЕВО (сделали раньше) — не «переносится»
    early = {"source": "jira", "id": "e1", "title": "Ранняя", "status": "todo"}
    dt.record_deadlines(_UID, [{**early, "deadline": "2026-07-20"}])
    dt.record_deadlines(_UID, [{**early, "deadline": "2026-07-12"}])
    assert dt.postponed_tasks(_UID, [{**early, "deadline": "2026-07-12"}]) == []


def test_tasks_without_deadline_skipped(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert dt.record_deadlines(_UID, [
        {"source": "graph", "id": "x", "title": "Без срока", "status": "todo"},
        {"title": "Мусорный дедлайн", "deadline": "когда-нибудь"},
    ]) == 0
