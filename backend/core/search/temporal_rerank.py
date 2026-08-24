# -*- coding: utf-8 -*-
"""
Temporal re-rank для гибридного поиска (MEMORY_DESIGN_PRINCIPLES.md §6, пункт №2).

Зачем: темпоральные поля УЖЕ лежат в графе и vector-payload (`date`, `deadline`,
`indexed_at`, `created_at`) — но в ретриве не используются (диагноз Explore). Это
поднимает temporal-вопросы («когда решили X», «последнее решение по Y», recency).

Реализация:
- Чисто детерминированный slight-boost к RRF-score по близости даты документа к
  reference-дате (по умолчанию now; можно передать `as_of`). Чем свежее — тем выше.
- НЕ меняет сигнатуру search(), не делает доп. запросов в граф. Работает на полях
  metadata, которые уже приходят с vector-результатами.
- Без LLM, без heavy-deps — math из stdlib.
- За флагом enable_temporal_retrieval_boost; OFF → байт-в-байт прежнее поведение.

Дизайн-гарантия (гардрейл MEMORY_DESIGN_PRINCIPLES.md §1):
- Buster ограничен (max +20% к RRF) → НЕ доминирует над релевантностью.
- На документах БЕЗ темпоральных полей → буст = 0 (никакой регрессии).
- Не удаляет результаты, не меняет состав топ-k до буста — только пере-сортирует.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

# Половина-жизни в днях: 90 дней — год назад весит ~25%, неделя назад ~95%.
DEFAULT_HALF_LIFE_DAYS = 90.0
# Максимальный boost: свежий документ может получить score * (1+MAX_BOOST).
DEFAULT_MAX_BOOST = 0.20


_DATE_KEYS = ("date", "meeting_date", "decided_at", "deadline", "created_at", "indexed_at")
# Регекс для гибкого парсинга — ловит ISO (2024-01-15T10:00:00Z) и слэш-формат
# (2024/01/15 или 2024/01/15 (Mon) 10:00) — это то, что использует наш граф.
_DATE_RX = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")


def _parse_date(value) -> Optional[datetime]:
    """Распарсить дату из строки/datetime/None в aware datetime (UTC). None если нечего."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    m = _DATE_RX.search(str(value))
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
            return None
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


def extract_doc_date(doc_data: dict) -> Optional[datetime]:
    """Найти дату документа из metadata. Перебирает известные ключи в порядке
    приоритета (date > meeting_date > decided_at > deadline > created_at > indexed_at).
    Возвращает aware datetime или None если поля нет/невалидное."""
    if not isinstance(doc_data, dict):
        return None
    meta = doc_data.get("metadata") if isinstance(doc_data.get("metadata"), dict) else doc_data
    for key in _DATE_KEYS:
        dt = _parse_date(meta.get(key) if isinstance(meta, dict) else None)
        if dt is not None:
            return dt
    # запасной — на верхнем уровне doc_data (если metadata пуст)
    for key in _DATE_KEYS:
        dt = _parse_date(doc_data.get(key))
        if dt is not None:
            return dt
    return None


def recency_factor(doc_date: Optional[datetime], *, as_of: Optional[datetime] = None,
                   half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
                   max_boost: float = DEFAULT_MAX_BOOST) -> float:
    """Множитель для score: 1.0 (нет даты / future) → 1+max_boost (сегодня).

    Экспоненциальный distance-decay с заданной half-life. Симметричен для будущих
    дат (deadline через неделю → такой же буст как событие неделю назад) — это
    нужно для «ближайший дедлайн» и «недавнее решение».
    """
    if doc_date is None:
        return 1.0
    as_of = as_of or datetime.now(timezone.utc)
    days_diff = abs((as_of - doc_date).total_seconds()) / 86400.0
    # экспоненциальный distance-decay: при diff=0 → 1.0, при half_life → 0.5
    decay = math.exp(-days_diff / max(half_life_days, 1.0))
    return 1.0 + max_boost * decay


def _range_mode_enabled() -> bool:
    """Диапазонный режим. Выключатель TESSENT_TEMPORAL_RANGE=off.

    По умолчанию включён ПОД родительским флагом enable_temporal_retrieval_boost
    (который сам по умолчанию выключен — дефолтное поведение продукта не
    меняется). Логика: когда в вопросе явно назван календарный период,
    буст свежести не просто бесполезен — он работает против ответа
    (на «что решили в марте» поднимает свежий август). Диапазонный режим
    в этом случае — исправление, а не ставка.
    """
    import os
    return (os.getenv("TESSENT_TEMPORAL_RANGE", "") or "").strip().lower() not in (
        "off", "0", "false", "no")


def range_factor(doc_date: Optional[datetime], query_range, *,
                 max_boost: float = DEFAULT_MAX_BOOST) -> float:
    """Множитель диапазонного режима: документ ВНУТРИ спрошенного периода →
    1+max_boost, вне периода или без даты → 1.0.

    Никого не наказываем (вне периода — не 0.8, а ровно 1.0): гардрейл тот
    же, что у буста свежести — темпоральный сигнал не должен доминировать
    над релевантностью, он только подталкивает попавших в период.
    """
    if doc_date is None or query_range is None:
        return 1.0
    from backend.core.temporal.interval_algebra import active_at
    return 1.0 + max_boost if active_at(query_range, doc_date) else 1.0


def apply_temporal_rerank(fused_results: list, *, query: Optional[str] = None,
                          as_of: Optional[datetime] = None,
                          half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
                          max_boost: float = DEFAULT_MAX_BOOST) -> list:
    """Применить temporal-boost к списку FusedResult-like объектов и пересортировать.

    Каждый элемент должен иметь атрибуты `rrf_score` и `data` (dict с metadata) —
    это в точности форма FusedResult из rrf_fusion.py. НЕ мутирует исходные
    объекты; возвращает новый список (сортирован по boosted-score).

    Два режима, взаимоисключающие:
    - в вопросе явно назван календарный период («в марте 2025», «Q1 2025»,
      «в прошлом году») → ДИАПАЗОННЫЙ: бустятся документы внутри периода,
      свежесть не участвует вовсе (она бы тянула в сторону «сейчас»);
    - периода нет (query=None или не распознан) → прежний буст СВЕЖЕСТИ,
      байт-в-байт как до появления диапазонного режима.
    """
    if not fused_results:
        return []

    query_range = None
    if query and _range_mode_enabled():
        try:
            from backend.core.search.temporal_range import detect_range
            query_range = detect_range(query, as_of=as_of)
        except Exception:
            query_range = None  # детектор не должен уметь ронять поиск

    class _BoostedView:
        """Тонкая обёртка: внешне = FusedResult, но rrf_score = boosted-значение.
        Используем именно обёртку, чтобы НЕ мутировать оригинальные объекты."""
        __slots__ = ("_orig", "rrf_score")

        def __init__(self, orig, new_score):
            self._orig = orig
            self.rrf_score = new_score

        def __getattr__(self, name):
            # все остальные атрибуты (doc_id, data, sources, ...) — из оригинала
            return getattr(self._orig, name)

    boosted = []
    for fr in fused_results:
        data = getattr(fr, "data", None) or {}
        date = extract_doc_date(data)
        if query_range is not None:
            factor = range_factor(date, query_range, max_boost=max_boost)
        else:
            factor = recency_factor(date, as_of=as_of, half_life_days=half_life_days,
                                    max_boost=max_boost)
        boosted.append(_BoostedView(fr, fr.rrf_score * factor))

    boosted.sort(key=lambda x: x.rrf_score, reverse=True)
    return boosted
