# -*- coding: utf-8 -*-
"""Карточка человека не сообщает того, чего не измеряла.

Найдено при сверке раздела «HR 2.0» с кодом. `to_text()` печатал строку
«Тренд: стабильно» всегда — через fallback `self.performance_trend or
'стабильно'`. При этом поле `performance_trend` не заполняется никаким
кодом: присваивания есть только при слиянии двух снапшотов.

Цена ошибки не косметическая. Этот текст уходит в контекст чата и в
профиль цифрового слепка (`twin/profile.py` берёт `to_text` целиком).
То есть модели сообщали как факт, что динамика сотрудника стабильна,
хотя её никто не считал — ровно тот случай, который вся остальная
система старательно предотвращает требованием источника под каждым
утверждением.

Отсутствие строки честнее выдуманной стабильности.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from backend.core.sleep.enhanced_snapshot import PersonSnapshot  # noqa: E402


def test_uncomputed_trend_is_not_rendered():
    """Главный случай: тренда нет — строки о тренде тоже нет."""
    text = PersonSnapshot(person_id="p1", name="Аня").to_text()
    assert "Тренд" not in text, (
        "карточка сообщает о динамике человека, которую никто не измерял"
    )
    assert "стабильно" not in text
    print("✅ непосчитанный тренд не печатается")


def test_computed_trend_is_rendered():
    """Если тренд когда-нибудь начнут считать — он должен отображаться."""
    text = PersonSnapshot(
        person_id="p2", name="Боря", performance_trend="improving").to_text()
    assert "Тренд: improving" in text
    print("✅ посчитанный тренд печатается")


def test_empty_string_counts_as_absent():
    """Пустая строка — это тоже «не измеряли»."""
    text = PersonSnapshot(
        person_id="p3", name="Вера", performance_trend="").to_text()
    assert "Тренд" not in text
    print("✅ пустое значение тренда не печатается")


def test_activity_metrics_still_rendered():
    """Соседние метрики действительно считаются — их убирать не надо."""
    text = PersonSnapshot(
        person_id="p4", name="Гриша", tasks_completed_week=3,
        tasks_in_progress=2, meetings_participated=7).to_text()
    assert "Задач выполнено (неделя): 3" in text
    assert "Задач в работе: 2" in text
    assert "Участие во встречах: 7" in text
    print("✅ реально считаемые метрики активности на месте")


def test_no_fabricated_default_in_source():
    """Защита от возврата fallback при будущих правках."""
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(HERE)),
        "backend/core/sleep/enhanced_snapshot.py"), encoding="utf-8").read()
    assert "self.performance_trend or 'стабильно'" not in src
    assert 'self.performance_trend or "стабильно"' not in src
    print("✅ выдуманного значения по умолчанию в исходнике нет")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты карточки человека прошли.")
