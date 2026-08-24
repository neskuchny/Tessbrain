# -*- coding: utf-8 -*-
"""Версии сущностей датируются датой ВСТРЕЧИ, а не моментом индексации.

Зачем эти тесты. Раньше `create_version` штамповал `datetime.now()`, а рядом
лежащий `add_timeline_event` брал реальную дату встречи. Пока встречи
обрабатывались сразу после проведения, расхождение было незаметным. Но при
загрузке архива (или пересинке) вся история сущности схлопывалась в один
день — и запрос «как было на 15 марта» честно возвращал пустоту, хотя данные
за март в системе были.

Здесь проверяется:
  1. занос задним числом виден по своей дате, а не по дате загрузки;
  2. срез «на дату» отвечает по хронологии, даже если порядок ЗАПИСИ обратный;
  3. дата без времени означает конец дня (иначе решения этого же дня выпадают);
  4. без occurred_at поведение прежнее — время записи;
  5. мусор в occurred_at не создаёт версию с недостижимой датой.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from backend.core.temporal.temporal_tracker import (  # noqa: E402
    TemporalTracker,
    _as_of_bound,
    _normalize_ts,
)


def _tracker() -> TemporalTracker:
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    t = TemporalTracker(db_path=db, user_id="u-test")
    asyncio.run(t.initialize())
    return t


def test_backdated_version_keeps_meeting_date():
    """Версия, заведённая задним числом, живёт на дате встречи."""
    t = _tracker()
    asyncio.run(t.create_version(
        entity_id="project_acme", entity_type="project",
        data={"status": "planning"},
        occurred_at="2026-03-10T10:00:00+00:00",
    ))

    # На 1 марта проекта ещё не существовало
    before = asyncio.run(t.get_version_at_date("project_acme", "2026-03-01"))
    assert before is None

    # На 15 марта — уже есть, и это именно мартовская версия
    after = asyncio.run(t.get_version_at_date("project_acme", "2026-03-15"))
    assert after is not None
    assert after["data"]["status"] == "planning"
    assert after["timestamp"].startswith("2026-03-10")
    print("✅ занос задним числом виден по дате встречи, а не загрузки")


def test_out_of_order_ingest_resolves_by_chronology():
    """Порядок ЗАПИСИ обратный хронологии — срез всё равно верный.

    Ровно случай «догрузили архив старых встреч после свежих».
    """
    t = _tracker()
    # Сначала записали ИЮНЬСКУЮ встречу
    asyncio.run(t.create_version(
        entity_id="project_acme", entity_type="project",
        data={"status": "in_progress"},
        occurred_at="2026-06-01T10:00:00+00:00",
    ))
    # Потом догрузили МАЙСКУЮ (version=2, но хронологически раньше)
    asyncio.run(t.create_version(
        entity_id="project_acme", entity_type="project",
        data={"status": "planning"},
        occurred_at="2026-05-01T10:00:00+00:00",
    ))

    may = asyncio.run(t.get_version_at_date("project_acme", "2026-05-15"))
    assert may is not None
    assert may["data"]["status"] == "planning", (
        "срез на май обязан вернуть майское состояние, а не последнее записанное"
    )

    june = asyncio.run(t.get_version_at_date("project_acme", "2026-06-15"))
    assert june is not None
    assert june["data"]["status"] == "in_progress"
    print("✅ обратный порядок записи не ломает срез на дату")


def test_date_without_time_means_end_of_day():
    """«Как было 15 марта» включает то, что решили в этот день."""
    t = _tracker()
    asyncio.run(t.create_version(
        entity_id="decision_x", entity_type="decision",
        data={"summary": "запускаем в марте"},
        occurred_at="2026-03-15T14:30:00+00:00",
    ))

    same_day = asyncio.run(t.get_version_at_date("decision_x", "2026-03-15"))
    assert same_day is not None, (
        "решение, принятое днём 15 марта, должно попадать в срез «на 15 марта»"
    )
    assert same_day["data"]["summary"] == "запускаем в марте"
    print("✅ дата без времени = конец дня")


def test_without_occurred_at_falls_back_to_write_time():
    """Обратная совместимость: без occurred_at — прежнее поведение."""
    t = _tracker()
    v = asyncio.run(t.create_version(
        entity_id="task_y", entity_type="task", data={"status": "open"},
    ))
    stamped = datetime.fromisoformat(v["timestamp"])
    assert abs(datetime.now(timezone.utc) - stamped) < timedelta(minutes=5)
    print("✅ без occurred_at — время записи, как раньше")


def test_garbage_occurred_at_does_not_hide_version():
    """Неразобранная дата не должна прятать версию в недостижимом времени."""
    t = _tracker()
    asyncio.run(t.create_version(
        entity_id="task_z", entity_type="task", data={"status": "open"},
        occurred_at="позавчера после обеда",
    ))
    found = asyncio.run(t.get_version_at_date(
        "task_z", datetime.now(timezone.utc).isoformat()))
    assert found is not None, "версия с мусорной датой обязана остаться находимой"
    print("✅ мусор в occurred_at → откат на время записи, версия не теряется")


def test_diff_against_chronological_predecessor():
    """change_log сравнивает с тем, что было ДО этой даты, а не с последним
    записанным — иначе задним числом появляется несуществующий «откат»."""
    t = _tracker()
    asyncio.run(t.create_version(
        entity_id="person_a", entity_type="person",
        data={"role": "CTO"}, occurred_at="2026-06-01T10:00:00+00:00"))
    v = asyncio.run(t.create_version(
        entity_id="person_a", entity_type="person",
        data={"role": "разработчик"}, occurred_at="2026-01-01T10:00:00+00:00"))
    # Январская версия — самая первая по времени, сравнивать ей не с чем
    assert v["changes"], "первая по хронологии версия фиксирует свои поля"
    changed_fields = {c["field"] for c in v["changes"]}
    assert "role" in changed_fields
    old_values = {c["field"]: c["old_value"] for c in v["changes"]}
    assert old_values["role"] is None, (
        "у январской версии не должно быть «предыдущей роли CTO» из июня"
    )
    print("✅ дифф считается против хронологического предшественника")


def test_normalize_and_bound_helpers():
    """Хелперы: naive → UTC, мусор → None, дата → конец дня."""
    assert _normalize_ts(None) is None
    assert _normalize_ts("") is None
    assert _normalize_ts("не дата") is None
    assert _normalize_ts("2026-03-15T10:00:00").endswith("+00:00")
    assert _normalize_ts(datetime(2026, 3, 15, 10, 0)).startswith("2026-03-15T10:00")
    assert _as_of_bound("2026-03-15").startswith("2026-03-15T23:59:59")
    # Полная метка времени не превращается в конец дня
    assert _as_of_bound("2026-03-15T08:00:00").startswith("2026-03-15T08:00")
    print("✅ хелперы нормализации дат")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты occurred_at прошли.")
