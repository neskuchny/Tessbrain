# -*- coding: utf-8 -*-
"""Метка свежести факта.

Главное, что здесь фиксируется: пороги метки и пороги механизма архивации
берутся из одного места. Если они разойдутся, интерфейс начнёт называть
свежим то, что decay уже считает устаревшим, — и человек будет принимать
решения по данным, которые система про себя считает мусором.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.core.store.freshness import age_days, annotate, freshness_of

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


# ── возраст ─────────────────────────────────────────────────────────


def test_age_from_iso_string():
    assert age_days({"created_at": _ago(10)}, now=NOW) == 10


def test_age_handles_z_suffix():
    raw = (NOW - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    assert age_days({"created_at": raw}, now=NOW) == 5


def test_age_accepts_datetime_object():
    assert age_days({"updated_at": NOW - timedelta(days=3)}, now=NOW) == 3


def test_naive_datetime_treated_as_utc():
    naive = (NOW - timedelta(days=4)).replace(tzinfo=None).isoformat()
    assert age_days({"created_at": naive}, now=NOW) == 4


def test_updated_at_wins_over_created_at():
    """Когда факт подтверждали важнее, чем когда завели."""
    data = {"created_at": _ago(300), "updated_at": _ago(2)}
    assert age_days(data, now=NOW) == 2


def test_missing_date_is_none_not_ancient():
    """Пустое не должно выглядеть как измеренное."""
    assert age_days({"title": "без даты"}, now=NOW) is None


def test_broken_date_falls_through_to_next_field():
    data = {"updated_at": "не дата", "created_at": _ago(9)}
    assert age_days(data, now=NOW) == 9


def test_future_date_is_not_negative():
    assert age_days({"created_at": _ago(-5)}, now=NOW) == 0


# ── уровни ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("days,level,show", [
    (0, "fresh", False),
    (7, "fresh", False),
    (8, "recent", False),
    (30, "recent", False),
    (31, "aging", True),
    (90, "aging", True),
    (91, "stale", True),
    (365, "stale", True),
    (400, "ancient", True),
])
def test_levels_and_visibility(days, level, show):
    f = freshness_of({"created_at": _ago(days)}, now=NOW)
    assert f["level"] == level
    assert f["show"] is show, "свежее не помечаем — иначе метка обесценится"
    assert f["age_days"] == days


def test_unknown_date_is_not_shown():
    f = freshness_of({}, now=NOW)
    assert f["level"] == "unknown"
    assert f["age_days"] is None
    assert f["show"] is False, "«дата неизвестна» не повод пугать пользователя"


def test_label_says_not_confirmed_not_wrong():
    """Формулировка: факт мог остаться верным, просто давно не трогали."""
    f = freshness_of({"created_at": _ago(120)}, now=NOW)
    assert "подтверждалось" in f["label"]
    assert "неверн" not in f["label"] and "ошиб" not in f["label"]


# ── единый источник порогов ─────────────────────────────────────────


def test_thresholds_come_from_decay_manager():
    """Пороги метки и пороги архивации — из одного места.

    Разойдись они, интерфейс называл бы свежим то, что decay считает
    устаревшим. Тест сравнивает поведение напрямую с конфигом decay."""
    from backend.core.sleep.decay_manager import DecayManager

    for entity_type, cfg in DecayManager.DEFAULT_CONFIGS.items():
        on_edge = freshness_of({"created_at": _ago(cfg.fresh_days)},
                               entity_type=entity_type, now=NOW)
        just_after = freshness_of({"created_at": _ago(cfg.fresh_days + 1)},
                                  entity_type=entity_type, now=NOW)
        assert on_edge["level"] == "fresh", entity_type
        assert just_after["level"] == "recent", entity_type

        stale_edge = freshness_of({"created_at": _ago(cfg.aging_days + 1)},
                                  entity_type=entity_type, now=NOW)
        assert stale_edge["level"] == "stale", entity_type


def test_entity_type_changes_thresholds():
    """У Task окно короче, чем у Person — метка это учитывает."""
    from backend.core.sleep.decay_manager import DecayManager
    task_cfg = DecayManager.DEFAULT_CONFIGS["Task"]
    person_cfg = DecayManager.DEFAULT_CONFIGS["Person"]
    assert task_cfg.fresh_days < person_cfg.fresh_days, "предпосылка теста"

    days = person_cfg.fresh_days
    assert freshness_of({"created_at": _ago(days)},
                        entity_type="Person", now=NOW)["level"] == "fresh"
    assert freshness_of({"created_at": _ago(days)},
                        entity_type="Task", now=NOW)["level"] != "fresh"


def test_unknown_entity_type_uses_defaults():
    f = freshness_of({"created_at": _ago(10)}, entity_type="Небывалый",
                     now=NOW)
    assert f["level"] == "recent"


# ── разметка списка ─────────────────────────────────────────────────


def test_annotate_adds_marks_in_place():
    items = [{"created_at": _ago(2)}, {"created_at": _ago(200)}]
    out = annotate(items, now=NOW)
    assert out is items, "возвращаем тот же список"
    assert items[0]["freshness"]["show"] is False
    assert items[1]["freshness"]["show"] is True


def test_annotate_survives_garbage():
    """Метка — украшение поверх данных; её сбой не должен ронять выдачу."""
    items = [{"created_at": _ago(1)}, "не словарь", None]
    annotate(items, now=NOW)
    assert items[0]["freshness"]["level"] == "fresh"


def test_annotate_empty_and_none():
    assert annotate([], now=NOW) == []
    assert annotate(None, now=NOW) is None
