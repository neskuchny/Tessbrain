# -*- coding: utf-8 -*-
"""Свежесть факта — метка для тех мест, где человек факт читает.

Проблема, которую это закрывает: `decay_manager` честно считает, что факт
устарел (STALE), и по политике его архивирует — но пока это не произошло,
в интерфейсе решение девятимесячной давности выглядит ровно так же
уверенно, как вчерашнее. Человек принимает решение по данным, не зная,
что они старые.

Почему отдельный модуль, а не вызов `decay_manager`: тот работает по
загруженному графу и entity_id — для метки на карточке в списке это
слишком тяжело (граф на каждый факт не поднимешь). Здесь чистая функция
над обычным dict с датой, без ввода-вывода.

Пороги при этом НЕ дублируются: берутся из `DecayManager.DEFAULT_CONFIGS`,
то есть у метки в интерфейсе и у механизма архивации один источник правды.
Разойтись они не могут — тест это фиксирует.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Поля даты в порядке доверия: когда факт последний раз подтверждали
# важнее, чем когда его завели.
_DATE_FIELDS = ("updated_at", "last_seen_at", "created_at", "date",
                "_created_at")

# Человеческие подписи. Намеренно без запугивания: «не подтверждалось»
# точнее, чем «устарело» — факт мог остаться верным, просто его давно
# никто не трогал, и это разные вещи.
_LABELS = {
    "fresh": "свежие данные",
    "recent": "недавние данные",
    "aging": "давно не подтверждалось",
    "stale": "давно не подтверждалось",
    "ancient": "очень старые данные",
    "unknown": "дата неизвестна",
}

# Начиная с какого уровня метку стоит показывать. Свежее и недавнее не
# помечаем: шум на каждой карточке обесценивает предупреждение там, где
# оно действительно нужно.
_WARN_FROM = ("aging", "stale", "ancient")


def _thresholds(entity_type: str) -> Dict[str, int]:
    """Пороги в днях для типа сущности — из конфигурации decay."""
    try:
        from backend.core.sleep.decay_manager import DecayConfig, DecayManager
        cfg = DecayManager.DEFAULT_CONFIGS.get(
            entity_type or "", DecayConfig(entity_type=entity_type or ""))
    except Exception:
        logger.debug("freshness: конфиг decay недоступен", exc_info=True)
        class _Fallback:            # те же значения, что дефолт DecayConfig
            fresh_days, recent_days, aging_days, stale_days = 7, 30, 90, 365
        cfg = _Fallback()
    return {
        "fresh": int(cfg.fresh_days),
        "recent": int(cfg.recent_days),
        "aging": int(cfg.aging_days),
        "stale": int(cfg.stale_days),
    }


def age_days(data: Dict[str, Any], *, now: Optional[datetime] = None
             ) -> Optional[int]:
    """Возраст факта в днях, либо None если даты нет.

    None — это честное «не знаем», а не «очень старый»: пустое не должно
    выглядеть как измеренное."""
    now = now or datetime.now(timezone.utc)
    for field in _DATE_FIELDS:
        raw = (data or {}).get(field)
        if not raw:
            continue
        try:
            if isinstance(raw, datetime):
                dt = raw
            else:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, (now - dt).days)
        except (ValueError, TypeError):
            continue
    return None


def freshness_of(data: Dict[str, Any], *, entity_type: str = "",
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Метка свежести факта для интерфейса.

    Возвращает: level (fresh/recent/aging/stale/ancient/unknown),
    age_days (или None), label — человеческая подпись, show — стоит ли
    вообще показывать метку."""
    days = age_days(data, now=now)
    if days is None:
        return {"level": "unknown", "age_days": None,
                "label": _LABELS["unknown"], "show": False}

    th = _thresholds(entity_type)
    if days <= th["fresh"]:
        level = "fresh"
    elif days <= th["recent"]:
        level = "recent"
    elif days <= th["aging"]:
        level = "aging"
    elif days <= th["stale"]:
        level = "stale"
    else:
        level = "ancient"

    return {
        "level": level,
        "age_days": days,
        "label": _LABELS[level],
        "show": level in _WARN_FROM,
    }


def annotate(items, *, entity_type: str = "", key: str = "freshness",
             now: Optional[datetime] = None):
    """Проставить метку каждому элементу списка. Возвращает тот же список.

    Never-raise: метка — украшение поверх данных, и её отсутствие не повод
    не отдать сами данные."""
    try:
        for it in items or []:
            if isinstance(it, dict):
                it[key] = freshness_of(it, entity_type=entity_type, now=now)
    except Exception:
        logger.debug("freshness: разметка не удалась", exc_info=True)
    return items
