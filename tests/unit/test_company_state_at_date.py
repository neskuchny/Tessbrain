# -*- coding: utf-8 -*-
"""Срез КОМПАНИИ на дату, а не одного объекта.

Данные для этого лежали в temporal-сторе с самого начала — версии
person/decision/task/project пишутся на каждой встрече. Не хватало сборки:
сложить «последнюю версию каждой сущности на дату» в один срез. Здесь
проверяется, что сборка честная:

  1. срез отражает состояние на дату, а не текущее;
  2. сущность, заведённая позже даты среза, в него не попадает;
  3. разница между срезами делит объекты на появившиеся/изменившиеся/исчезнувшие;
  4. фильтр по типам работает;
  5. чужой tenant в срез не протекает;
  6. обрезка по limit сообщается флагом, а не молча.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from backend.core.temporal.compare import (  # noqa: E402
    compare_company_states,
    diff_company_states,
    get_company_state_at_date,
)
from backend.core.temporal.temporal_tracker import (  # noqa: E402
    TemporalTracker,
    _trackers,
)


def _seed(user_id: str, rows: list) -> TemporalTracker:
    """rows: (entity_id, entity_type, data, occurred_at)."""
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    t = TemporalTracker(db_path=db, user_id=user_id)
    asyncio.run(t.initialize())
    for eid, etype, data, when in rows:
        asyncio.run(t.create_version(
            entity_id=eid, entity_type=etype, data=data, occurred_at=when))
    _trackers[user_id] = t
    return t


def test_state_reflects_the_date_not_today():
    """На 15 марта проект в planning, хотя сейчас он уже in_progress."""
    _seed("co-1", [
        ("project_acme", "project", {"name": "Acme", "status": "planning"},
         "2026-03-01T10:00:00+00:00"),
        ("project_acme", "project", {"name": "Acme", "status": "in_progress"},
         "2026-06-01T10:00:00+00:00"),
    ])
    st = asyncio.run(get_company_state_at_date(user_id="co-1", date="2026-03-15"))
    assert st["total"] == 1
    proj = st["entities"]["project"][0]
    assert proj["snapshot"]["status"] == "planning", (
        "срез на март обязан показать мартовское состояние"
    )

    now = asyncio.run(get_company_state_at_date(user_id="co-1", date="2026-08-01"))
    assert now["entities"]["project"][0]["snapshot"]["status"] == "in_progress"
    print("✅ срез отражает состояние на дату, а не текущее")


def test_entity_created_later_is_absent():
    """Человек, появившийся в мае, в мартовский срез не попадает."""
    _seed("co-2", [
        ("person_a", "person", {"name": "Аня", "role": "CTO"},
         "2026-02-01T10:00:00+00:00"),
        ("person_b", "person", {"name": "Боря", "role": "аналитик"},
         "2026-05-01T10:00:00+00:00"),
    ])
    march = asyncio.run(get_company_state_at_date(user_id="co-2", date="2026-03-15"))
    names = {p["name"] for p in march["entities"]["person"]}
    assert names == {"Аня"}, f"в марте Бори ещё не было, а получили {names}"
    assert march["total"] == 1
    # scanned считает всех известных сущностей — видно, что срез неполон не
    # из-за обрезки, а потому что объекта тогда не существовало
    assert march["scanned"] == 2

    may = asyncio.run(get_company_state_at_date(user_id="co-2", date="2026-05-15"))
    assert {p["name"] for p in may["entities"]["person"]} == {"Аня", "Боря"}
    print("✅ сущность, заведённая позже даты среза, в него не попадает")


def test_diff_splits_appeared_changed_disappeared():
    _seed("co-3", [
        ("person_a", "person", {"name": "Аня", "role": "аналитик"},
         "2026-02-01T10:00:00+00:00"),
        ("person_a", "person", {"name": "Аня", "role": "CTO"},
         "2026-05-01T10:00:00+00:00"),
        ("person_b", "person", {"name": "Боря", "role": "стажёр"},
         "2026-05-02T10:00:00+00:00"),
    ])
    before = asyncio.run(get_company_state_at_date(user_id="co-3", date="2026-03-01"))
    after = asyncio.run(get_company_state_at_date(user_id="co-3", date="2026-06-01"))
    d = diff_company_states(before, after)

    assert [x["name"] for x in d["appeared"]] == ["Боря"]
    assert d["disappeared"] == []
    assert len(d["changed"]) == 1
    assert d["changed"][0]["delta"]["changed"]["role"] == ["аналитик", "CTO"]
    assert "появилось 1" in d["summary"] and "изменилось 1" in d["summary"]
    print(f"✅ разница между срезами: {d['summary']}")


def test_entity_type_filter():
    _seed("co-4", [
        ("person_a", "person", {"name": "Аня"}, "2026-02-01T10:00:00+00:00"),
        ("task_1", "task", {"title": "Собрать отчёт"}, "2026-02-02T10:00:00+00:00"),
    ])
    only_people = asyncio.run(get_company_state_at_date(
        user_id="co-4", date="2026-03-01", entity_types=["person"]))
    assert set(only_people["entities"]) == {"person"}
    assert only_people["total"] == 1
    print("✅ фильтр по типам сущностей")


def test_foreign_tenant_does_not_leak():
    """Запись с чужим штампом tenant_id в срез не попадает."""
    _seed("co-5", [
        ("person_own", "person", {"name": "Свой"}, "2026-02-01T10:00:00+00:00"),
        ("person_alien", "person", {"name": "Чужой", "tenant_id": "other-org"},
         "2026-02-01T10:00:00+00:00"),
    ])
    st = asyncio.run(get_company_state_at_date(
        user_id="co-5", date="2026-03-01", allowed_tenants={"co-5"}))
    names = {p["name"] for p in st["entities"]["person"]}
    assert names == {"Свой"}, f"чужой tenant протёк в срез: {names}"
    print("✅ чужой tenant в срез не протекает")


def test_truncation_is_reported():
    """Обрезка по limit видна флагом, а не выглядит полным срезом."""
    rows = [(f"person_{i}", "person", {"name": f"Ч{i}"},
             "2026-02-01T10:00:00+00:00") for i in range(5)]
    _seed("co-6", rows)
    st = asyncio.run(get_company_state_at_date(
        user_id="co-6", date="2026-03-01", limit=3))
    assert st["truncated"] is True
    assert st["total"] <= 3
    full = asyncio.run(get_company_state_at_date(
        user_id="co-6", date="2026-03-01", limit=100))
    assert full["truncated"] is False
    assert full["total"] == 5
    print("✅ обрезка по limit сообщается честно")


def test_compare_company_states_multi_date():
    _seed("co-7", [
        ("project_x", "project", {"name": "X", "status": "planning"},
         "2026-01-10T10:00:00+00:00"),
        ("project_x", "project", {"name": "X", "status": "in_progress"},
         "2026-04-10T10:00:00+00:00"),
        ("project_y", "project", {"name": "Y", "status": "planning"},
         "2026-04-11T10:00:00+00:00"),
    ])
    res = asyncio.run(compare_company_states(
        user_id="co-7", dates=["2026-06-01", "2026-02-01"]))
    # Даты отсортированы по возрастанию, несмотря на порядок аргумента
    assert res["dates"] == ["2026-02-01", "2026-06-01"]
    assert len(res["states"]) == 2
    assert len(res["deltas"]) == 1
    assert "2 срез(ов) компании" in res["narrative"]
    print(f"✅ мульти-срез: {res['narrative']}")


def test_empty_inputs_are_honest_errors():
    assert "error" in asyncio.run(get_company_state_at_date(user_id="co-8", date=""))
    assert "error" in asyncio.run(compare_company_states(user_id="co-8", dates=[]))
    assert "error" in asyncio.run(compare_company_states(user_id="co-8", dates=["  "]))
    print("✅ пустые входы → честная ошибка, а не пустой срез")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты среза компании прошли.")
