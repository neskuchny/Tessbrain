# -*- coding: utf-8 -*-
"""Детектор ВРЕМЕННОГО ДИАПАЗОНА в вопросе: «в марте 2025» → интервал.

ЗАЧЕМ. Буст свежести (temporal_rerank) отвечает на «что недавно», но на
вопрос «что решили В МАРТЕ» он поднимает свежие августовские документы —
то есть работает ПРОТИВ ответа. Чтобы включить диапазонный режим, нужно
сначала понять, что вопрос вообще про календарный период. Это и делает
детектор: чистые регулярки, без модели, тестируется офлайн.

Принцип — консервативность (как в enumerative_detect): срабатываем ТОЛЬКО
на явно названный календарный период (месяц, год, квартал, «в прошлом
месяце»). «Недавно», «когда-то», голое «когда» — НЕ диапазон: это зона
буста свежести, и отбирать её у него нельзя. Не уверены → None, и
поведение остаётся прежним байт-в-байт.

Языки: русский (язык продукта) и английский (язык бенчмарков) — как в
temporal_resolver.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from backend.core.temporal.interval_algebra import (
    Interval,
    month_interval,
    quarter_interval,
    year_interval,
)

# Месяцы: имя → номер. Русские — по основе (падежи: «марте», «мартом»).
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
    "декабр": 12,
}

# «в марте 2025», «за март 2025 года», "in March 2025", "March 2025"
_MONTH_YEAR_RX = re.compile(
    r"\b(?:в|за|in)?\s*"
    r"(январ[а-яё]*|феврал[а-яё]*|март[а-яё]*|апрел[а-яё]*|ма[ея]|июн[а-яё]*|"
    r"июл[а-яё]*|август[а-яё]*|сентябр[а-яё]*|октябр[а-яё]*|ноябр[а-яё]*|"
    r"декабр[а-яё]*|january|february|march|april|may|june|july|august|"
    r"september|october|november|december)"
    r"\s+(\d{4})\b", re.I)

# месяц БЕЗ года: «в марте», "in March" — год достраивается от as_of
_MONTH_ONLY_RX = re.compile(
    r"\b(?:в|за|in)\s+"
    r"(январ[её]|феврал[её]|марте|апреле|мае|июне|июле|августе|сентябре|"
    r"октябре|ноябре|декабре|january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\b", re.I)

# «в 2024 году», «за 2024 год», "in 2024" — год отдельным словом
_YEAR_RX = re.compile(r"\b(?:в|за|in)\s+(\d{4})(?:\s*(?:году|год|г\.))?\b", re.I)

# кварталы: «в 1 квартале 2025», «во втором квартале», "Q1 2025"
_QUARTER_RX = re.compile(
    r"\b(?:q([1-4])\s*(\d{4})?|(?:в|во|за)\s+"
    r"(?:([1-4])|перв|втор|трет|четв[её]рт)[а-яё]*\s+квартал[а-яё]*"
    r"(?:\s+(\d{4}))?)", re.I)
_QUARTER_WORDS = {"перв": 1, "втор": 2, "трет": 3, "четв": 4}

# относительные периоды (нужен as_of)
_REL_RX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bв прошлом месяце\b|\blast month\b", re.I), "prev_month"),
    (re.compile(r"\bв этом месяце\b|\bthis month\b", re.I), "this_month"),
    (re.compile(r"\bв прошлом году\b|\blast year\b", re.I), "prev_year"),
    (re.compile(r"\bв этом году\b|\bthis year\b", re.I), "this_year"),
    (re.compile(r"\bв прошлом квартале\b|\blast quarter\b", re.I), "prev_quarter"),
]


def _month_no(word: str) -> Optional[int]:
    w = (word or "").lower()
    if w in _MONTHS:
        return _MONTHS[w]
    # русские падежи: самая длинная подходящая основа («марта» → «март»,
    # не «ма»=май — longest-match, та же логика, что в temporal_resolver)
    best, best_len = None, 0
    for stem, no in _MONTHS.items():
        if len(stem) > 2 and w.startswith(stem) and len(stem) > best_len:
            best, best_len = no, len(stem)
    return best


def detect_range(query: Optional[str], *,
                 as_of: Optional[datetime] = None) -> Optional[Interval]:
    """Календарный период из вопроса, или None.

    Порядок — от специфичного к общему: месяц+год раньше голого года,
    иначе «в марте 2025» дал бы годовой интервал по «2025».
    """
    if not query:
        return None
    q = str(query)
    now = as_of or datetime.now(timezone.utc)

    m = _MONTH_YEAR_RX.search(q)
    if m:
        mon = _month_no(m.group(1))
        year = int(m.group(2))
        if mon and 1900 <= year <= 2100:
            return month_interval(year, mon)

    m = _QUARTER_RX.search(q)
    if m:
        if m.group(1):  # Q1 2025
            quarter, year_s = int(m.group(1)), m.group(2)
        else:
            quarter = int(m.group(3)) if m.group(3) else None
            if quarter is None:
                for stem, no in _QUARTER_WORDS.items():
                    if re.search(stem, m.group(0), re.I):
                        quarter = no
                        break
            year_s = m.group(4)
        if quarter:
            year = int(year_s) if year_s else now.year
            if 1900 <= year <= 2100:
                return quarter_interval(year, quarter)

    for rx, kind in _REL_RX:
        if rx.search(q):
            return _relative(kind, now)

    m = _MONTH_ONLY_RX.search(q)
    if m:
        mon = _month_no(m.group(1))
        if mon:
            # год от as_of: месяц ещё не наступил → берём прошлый год
            # («в марте», спрошено в августе 2026 → март 2026;
            #  «в ноябре», спрошено в августе 2026 → ноябрь 2025)
            year = now.year if mon <= now.month else now.year - 1
            return month_interval(year, mon)

    m = _YEAR_RX.search(q)
    if m:
        year = int(m.group(1))
        if 1990 <= year <= 2100:
            return year_interval(year)

    return None


def _relative(kind: str, now: datetime) -> Interval:
    if kind == "this_month":
        return month_interval(now.year, now.month)
    if kind == "prev_month":
        y, mo = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        return month_interval(y, mo)
    if kind == "this_year":
        return year_interval(now.year)
    if kind == "prev_year":
        return year_interval(now.year - 1)
    # prev_quarter
    q = (now.month - 1) // 3 + 1
    return (quarter_interval(now.year - 1, 4) if q == 1
            else quarter_interval(now.year, q - 1))


__all__ = ["detect_range"]
