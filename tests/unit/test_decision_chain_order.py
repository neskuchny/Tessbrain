# -*- coding: utf-8 -*-
"""Цепочка решений не должна смотреть в будущее.

Ребро Decision_new -SUPERSEDES-> Decision_old строится по семантическому
сходству: «то же самое решали раньше». Раньше единственной проверкой было
«кандидат из другой встречи» — при досинхронизации архива стрелка могла
указать на более позднее решение, и эволюция договорённостей читалась задом
наперёд.

Здесь проверяется хелпер порядка: он решает, ставить связь или пропустить
кандидата.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from backend.core.temporal.ordering import (  # noqa: E402
    date_key as _date_key,
    decision_order as _decision_order,
)


def test_older_candidate_is_linkable():
    assert _decision_order("2026-06-01", "2026-03-15") == "candidate_is_older"
    print("✅ более раннее решение — валидное предыдущее звено")


def test_newer_candidate_is_rejected():
    """Главный случай: кандидат позже — связывать нельзя."""
    assert _decision_order("2026-03-15", "2026-06-01") == "candidate_is_newer"
    print("✅ более позднее решение отвергается (стрелка не смотрит в будущее)")


def test_same_day_counts_as_older():
    """Внутри дня порядок неизвестен, но это не будущее."""
    assert _decision_order("2026-06-01", "2026-06-01") == "candidate_is_older"
    print("✅ решение того же дня считается предыдущим, а не будущим")


def test_missing_date_is_unknown_not_guess():
    """Старые векторы писались без meeting_date — честный 'unknown'."""
    assert _decision_order("2026-06-01", None) == "unknown"
    assert _decision_order("2026-06-01", "") == "unknown"
    assert _decision_order(None, "2026-06-01") == "unknown"
    assert _decision_order("не дата", "2026-06-01") == "unknown"
    print("✅ без даты — 'unknown', связь помечается непроверенной")


def test_date_formats_from_real_graph():
    """Форматы, которые реально приходят из источников встреч."""
    assert _date_key("2026-03-15") == (2026, 3, 15)
    assert _date_key("2026-03-15T10:30:00Z") == (2026, 3, 15)
    assert _date_key("2026-03-15T10:30:00+03:00") == (2026, 3, 15)
    assert _date_key("2026/03/15") == (2026, 3, 15)
    assert _date_key("2026/3/5 (Thu) 14:00") == (2026, 3, 5)
    print("✅ разбор форматов дат из графа")


def test_garbage_dates_rejected():
    assert _date_key(None) is None
    assert _date_key("") is None
    assert _date_key("вчера") is None
    assert _date_key("1799-01-01") is None     # вне разумного диапазона
    assert _date_key("2026-13-01") is None     # 13-й месяц
    assert _date_key("2026-02-45") is None     # 45-е число
    print("✅ мусорные даты отбрасываются, а не превращаются в ложный порядок")


def test_ordering_is_asymmetric():
    """Свойство, ради которого всё делалось: A→B и B→A не могут быть оба
    валидными звеньями цепочки."""
    early, late = "2026-01-10", "2026-09-20"
    assert _decision_order(late, early) == "candidate_is_older"
    assert _decision_order(early, late) == "candidate_is_newer"
    print("✅ порядок асимметричен — двусторонняя цепочка невозможна")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты порядка решений прошли.")
