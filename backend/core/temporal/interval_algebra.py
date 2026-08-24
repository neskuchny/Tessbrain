# -*- coding: utf-8 -*-
"""Интервальная алгебра Аллена — детерминированные отношения между периодами.

ЗАЧЕМ. Наш временной слой до сих пор был ТОЧЕЧНЫМ: resolver превращает
«вчера» в дату, rerank бустит по близости к одному моменту. А целый класс
вопросов — про ДИАПАЗОН: «что решили в марте», «что происходило во время
проекта X», «пересекались ли эти работы». Буст свежести таким вопросам
активно вредит: на вопрос про март он поднимает августовские документы.
Темпоральные вопросы — наше измеренное слабое место (LongMemEval 0.29–0.33),
и точечность — одна из причин.

Алгебра Аллена даёт язык для диапазонов: 13 взаимоисключающих отношений
между двумя интервалами (до/после, встык, перекрытие, внутри, совпадение).
Ноль обращений к модели — чистая арифметика дат, каждое отношение
проверяется тестом.

ПРОИСХОЖДЕНИЕ. Адаптировано из проекта semantica
(https://github.com/semantica-agi/semantica,
semantica/kg/temporal_reasoning.py), MIT License,
Copyright (c) 2026 Hawksight AI. По условиям MIT уведомление сохранено.
Отличия от оригинала:
- без их temporal_model: свой минимальный Interval, открытый конец = None;
- EQUALS проверяется РАНЬШЕ MEETS/MET_BY — у оригинала вырожденный
  интервал-точка, равный другой точке, классифицировался как MEETS
  (для «настоящих» интервалов start < end порядок проверок неважен,
  разница только на точках);
- парсинг дат — через наш же _parse_date из temporal_rerank (одни и те же
  форматы дат во всём поисковом слое, а не два разных парсера).

Инварианты (закреплены тестами):
- отношения взаимоисключающие: ровно одно на пару интервалов;
- converse-симметрия: relation(a,b) == converse(relation(b,a));
- merge идемпотентен; coverage ∈ [0, 1]; gaps не пересекаются с покрытием.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, List, Optional

# Далёкое будущее вместо «бесконечности» для открытого конца. Не datetime.max:
# с ним любая арифметика (+timedelta) мгновенно даёт OverflowError.
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Interval:
    """Период времени. end=None означает «ещё длится» (открытый конец)."""

    start: datetime
    end: Optional[datetime] = None
    label: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _aware(self.start))
        if self.end is not None:
            object.__setattr__(self, "end", _aware(self.end))
            if self.start > self.end:
                raise ValueError("интервал требует start <= end")

    @property
    def end_value(self) -> datetime:
        """Конец для сравнения: открытый интервал тянется в далёкое будущее."""
        return self.end if self.end is not None else _FAR_FUTURE


def _aware(dt: datetime) -> datetime:
    """Наивные datetime считаем UTC — сравнение naive/aware иначе бросает."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class IntervalRelation(Enum):
    """13 отношений Аллена. Имена — канонические из литературы."""

    BEFORE = "before"                  # a целиком раньше b
    AFTER = "after"                    # a целиком позже b
    MEETS = "meets"                    # a кончается ровно там, где b начинается
    MET_BY = "met_by"
    OVERLAPS = "overlaps"              # a начался раньше и кончился внутри b
    OVERLAPPED_BY = "overlapped_by"
    STARTS = "starts"                  # общее начало, a короче
    STARTED_BY = "started_by"
    DURING = "during"                  # a целиком внутри b
    CONTAINS = "contains"
    FINISHES = "finishes"              # общий конец, a короче
    FINISHED_BY = "finished_by"
    EQUALS = "equals"


# Обратное отношение: relation(b, a) для известного relation(a, b).
_CONVERSE = {
    IntervalRelation.BEFORE: IntervalRelation.AFTER,
    IntervalRelation.AFTER: IntervalRelation.BEFORE,
    IntervalRelation.MEETS: IntervalRelation.MET_BY,
    IntervalRelation.MET_BY: IntervalRelation.MEETS,
    IntervalRelation.OVERLAPS: IntervalRelation.OVERLAPPED_BY,
    IntervalRelation.OVERLAPPED_BY: IntervalRelation.OVERLAPS,
    IntervalRelation.STARTS: IntervalRelation.STARTED_BY,
    IntervalRelation.STARTED_BY: IntervalRelation.STARTS,
    IntervalRelation.DURING: IntervalRelation.CONTAINS,
    IntervalRelation.CONTAINS: IntervalRelation.DURING,
    IntervalRelation.FINISHES: IntervalRelation.FINISHED_BY,
    IntervalRelation.FINISHED_BY: IntervalRelation.FINISHES,
    IntervalRelation.EQUALS: IntervalRelation.EQUALS,
}


def converse(rel: IntervalRelation) -> IntervalRelation:
    return _CONVERSE[rel]


def relation(a: Interval, b: Interval) -> IntervalRelation:
    """Единственное отношение Аллена между a и b.

    Порядок проверок важен только для вырожденных интервалов-точек
    (start == end): EQUALS раньше MEETS, чтобы совпадающие точки давали
    EQUALS, а не MEETS (см. шапку — отличие от оригинала).
    """
    a_end, b_end = a.end_value, b.end_value

    if a.start == b.start and a_end == b_end:
        return IntervalRelation.EQUALS
    if a_end < b.start:
        return IntervalRelation.BEFORE
    if a.start > b_end:
        return IntervalRelation.AFTER
    if a_end == b.start:
        return IntervalRelation.MEETS
    if a.start == b_end:
        return IntervalRelation.MET_BY
    if a.start == b.start:
        return IntervalRelation.STARTS if a_end < b_end else IntervalRelation.STARTED_BY
    if a_end == b_end:
        return IntervalRelation.FINISHES if a.start > b.start else IntervalRelation.FINISHED_BY
    if a.start < b.start < a_end < b_end:
        return IntervalRelation.OVERLAPS
    if b.start < a.start < b_end < a_end:
        return IntervalRelation.OVERLAPPED_BY
    if a.start > b.start and a_end < b_end:
        return IntervalRelation.DURING
    return IntervalRelation.CONTAINS


def overlaps(a: Interval, b: Interval) -> bool:
    """Есть ли у периодов ОБЩЕЕ время. Стык (MEETS) — не перекрытие:
    «проект кончился в момент старта другого» общего времени не имеет."""
    return relation(a, b) not in (
        IntervalRelation.BEFORE, IntervalRelation.AFTER,
        IntervalRelation.MEETS, IntervalRelation.MET_BY,
    )


def contains(outer: Interval, inner: Interval) -> bool:
    """inner целиком внутри outer (границы включительно)."""
    return outer.start <= inner.start and outer.end_value >= inner.end_value


def active_at(interval: Interval, moment: datetime) -> bool:
    """Действовал ли интервал в момент. Полуоткрытая семантика [start, end):
    в секунду окончания период уже НЕ активен — иначе момент на стыке двух
    периодов был бы «активен» в обоих."""
    m = _aware(moment)
    if interval.start > m:
        return False
    return interval.end is None or m < interval.end


def merge_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    """Слить пересекающиеся и стыкующиеся интервалы в непересекающиеся.

    Метка результата — от первого интервала цепочки. Пустой вход → [].
    """
    ordered = sorted(intervals, key=lambda i: i.start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for cur in ordered[1:]:
        last = merged[-1]
        if cur.start <= last.end_value:  # пересечение или стык
            if cur.end_value > last.end_value:
                new_end = None if (cur.end is None or last.end is None) else cur.end
                merged[-1] = Interval(last.start, new_end, last.label)
            elif last.end is None:
                pass  # открытый уже поглощает всё правее
        else:
            merged.append(cur)
    return merged


def gaps(intervals: Iterable[Interval], domain: Interval) -> List[Interval]:
    """Непокрытые участки домена. Для «в каких периодах у нас нет данных»."""
    if domain.end is None:
        raise ValueError("gaps() требует закрытый домен: иначе разрыв бесконечен")
    merged = merge_intervals(
        _clip(i, domain) for i in intervals if overlaps(i, domain))
    out: List[Interval] = []
    cursor = domain.start
    for iv in merged:
        if cursor < iv.start:
            out.append(Interval(cursor, iv.start, "gap"))
        cursor = max(cursor, iv.end_value)
    if cursor < domain.end:
        out.append(Interval(cursor, domain.end, "gap"))
    return out


def coverage(intervals: Iterable[Interval], domain: Interval) -> float:
    """Доля домена, покрытая интервалами: 0.0..1.0."""
    if domain.end is None:
        raise ValueError("coverage() требует закрытый домен")
    total = (domain.end - domain.start).total_seconds()
    if total <= 0:
        return 0.0
    covered = sum(
        (min(iv.end_value, domain.end) - iv.start).total_seconds()
        for iv in merge_intervals(
            _clip(i, domain) for i in intervals if overlaps(i, domain)))
    return max(0.0, min(1.0, covered / total))


def _clip(iv: Interval, domain: Interval) -> Interval:
    start = max(iv.start, domain.start)
    end = min(iv.end_value, domain.end_value)
    if end >= _FAR_FUTURE:
        return Interval(start, None, iv.label)
    return Interval(start, end, iv.label)


def month_interval(year: int, month: int) -> Interval:
    """Календарный месяц как интервал [1-е число, 1-е след. месяца)."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
           else datetime(year, month + 1, 1, tzinfo=timezone.utc))
    return Interval(start, end)


def year_interval(year: int) -> Interval:
    return Interval(datetime(year, 1, 1, tzinfo=timezone.utc),
                    datetime(year + 1, 1, 1, tzinfo=timezone.utc))


def quarter_interval(year: int, quarter: int) -> Interval:
    if not 1 <= quarter <= 4:
        raise ValueError("квартал 1..4")
    m = (quarter - 1) * 3 + 1
    start = datetime(year, m, 1, tzinfo=timezone.utc)
    end = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if quarter == 4
           else datetime(year, m + 3, 1, tzinfo=timezone.utc))
    return Interval(start, end)


def day_interval(d: datetime) -> Interval:
    """Календарные сутки, содержащие момент d."""
    d = _aware(d)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return Interval(start, start + timedelta(days=1))


__all__ = [
    "Interval", "IntervalRelation", "active_at", "contains", "converse",
    "coverage", "day_interval", "gaps", "merge_intervals", "month_interval",
    "overlaps", "quarter_interval", "relation", "year_interval",
]
