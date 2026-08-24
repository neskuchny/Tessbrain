# -*- coding: utf-8 -*-
"""Порядок событий во времени — чистый stdlib, без зависимостей.

Вынесено из knowledge_sync отдельным модулем по двум причинам: логика
относится к темпоральному слою, а не к индексации встреч, и её нужно уметь
проверять тестами без numpy/networkx/qdrant, которые тянет knowledge_sync.

Используется цепочкой решений: прежде чем объявить прошлое решение
предыдущим звеном (Decision_new -SUPERSEDES-> Decision_old), надо убедиться,
что оно действительно раньше. Раньше проверки не было — только «кандидат из
другой встречи», — и при досинхронизации архива стрелка могла указать в
будущее.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Дата из строки любого из форматов, которые реально встречаются в графе:
# ISO с Z и без, слэши, «2024/01/15 (Mon) 10:00».
_DATE_RX = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")

CANDIDATE_IS_OLDER = "candidate_is_older"
CANDIDATE_IS_NEWER = "candidate_is_newer"
UNKNOWN = "unknown"


def date_key(value: Any) -> Optional[tuple]:
    """Сравнимый ключ даты (год, месяц, день) или None, если даты нет.

    Сравниваем по дню, а не по метке времени: дата встречи приходит из разных
    источников с разной точностью, и время в них не сопоставимо между собой.
    Для порядка решений точности до дня достаточно.
    """
    if not value:
        return None
    m = _DATE_RX.search(str(value))
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    except (TypeError, ValueError):
        return None
    if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return (y, mo, d)


def decision_order(current_date: Any, candidate_date: Any) -> str:
    """Кто раньше: CANDIDATE_IS_OLDER | CANDIDATE_IS_NEWER | UNKNOWN.

    UNKNOWN — если хотя бы у одной стороны нет разбираемой даты (старые
    векторы писались без meeting_date). Тогда вызывающий ставит связь, но
    помечает её непроверенной: терять цепочки на исторических данных хуже,
    чем признать, что порядок не подтверждён.

    Кандидат того же дня считается более ранним: внутри дня порядок нам
    неизвестен, но это всё ещё «раньше или одновременно», а не будущее.
    """
    cur, cand = date_key(current_date), date_key(candidate_date)
    if cur is None or cand is None:
        return UNKNOWN
    return CANDIDATE_IS_OLDER if cand <= cur else CANDIDATE_IS_NEWER
