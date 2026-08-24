# -*- coding: utf-8 -*-
"""Лексический канал индекса опыта: второй путь к паттерну, не через вектор.

Причина (arXiv 2508.21038): одновекторный поиск ограничен размерностью —
число подмножеств, которые он способен вернуть, конечно, и на простых
перечислительных запросах лучшие эмбеддеры проваливаются. Индекс опыта
(BWE) был у нас ЕДИНСТВЕННЫМ местом, где поиск чисто одновекторный:
Qdrant, косинус, порог — и никакого второго канала. Значит предел из
статьи действовал на самом дорогом по цене ошибки слое — «мы уже
наступали на эти грабли, вот чем кончилось».

Решение то же, что в основном поиске: лексика РЯДОМ с вектором, не вместо.
Паттернов у пользователя немного (десятки-сотни — это кластеры, не
события), поэтому честное лексическое пересечение по словам считается
за миллисекунды без индекса.

Дисциплина осторожности:
- канал ДОПОЛНЯЕТ: кандидаты, которых вектор уже нашёл, не трогаются;
  лексические добавляются ПОСЛЕ векторных с пометкой источника;
- порог перекрытия строгий (≥2 значимых слов и ≥0.25 доли) — лучше
  не добавить, чем притащить шум в слой опыта;
- сбой канала → просто нет добавки (вектор уже отработал);
- выключатель WISDOM_LEXICAL_CHANNEL=off.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

MIN_SHARED_WORDS = 2
MIN_OVERLAP_RATIO = 0.25
MAX_EXTRA = 3   # добавляем максимум столько — канал дополняет, не заливает

# Слова без различительной силы для деловых ситуаций.
_STOP = frozenset("""
и в на с по за от до из у о об при для как что это мы вы они он она оно
не да ли же бы то а но или если когда чтобы наш ваш свой был была были
быть есть нет клиент компания вопрос ситуация решение решили нужно надо
""".split())


def tokenize(text: str) -> set:
    """Значимые слова: нижний регистр, ≥4 букв, без стоп-слов."""
    words = re.findall(r"[а-яёa-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) >= 4 and w not in _STOP}


def lexical_score(query_text: str, pattern_text: str) -> Dict[str, Any]:
    """Перекрытие значимых слов. Чистая функция.

    Возвращает {passes, shared, ratio}: passes только при ≥2 общих словах
    И доле ≥0.25 от слов запроса — оба порога сразу, чтобы длинный паттерн
    не проходил за счёт одного совпавшего слова.
    """
    q = tokenize(query_text)
    p = tokenize(pattern_text)
    if not q or not p:
        return {"passes": False, "shared": [], "ratio": 0.0}
    shared = q & p
    ratio = len(shared) / len(q)
    return {
        "passes": len(shared) >= MIN_SHARED_WORDS and ratio >= MIN_OVERLAP_RATIO,
        "shared": sorted(shared),
        "ratio": round(ratio, 3),
    }


def merge_lexical_candidates(
    vector_patterns: List[Dict[str, Any]],
    all_patterns: List[Dict[str, Any]],
    query_text: str,
    *,
    max_extra: int = MAX_EXTRA,
) -> List[Dict[str, Any]]:
    """Дополнить векторную выдачу лексическими кандидатами.

    `all_patterns` — [{pattern_uuid, pattern_name, text, confidence,
    outcome_distribution, total_cases}] — все паттерны пользователя.
    Векторные остаются как есть и ПЕРВЫМИ; лексические идут после, с
    channel="lexical" и score=0.0 — мы не выдумываем векторную близость,
    которой не меряли.
    """
    seen = {p.get("pattern_uuid") for p in vector_patterns}
    scored = []
    for p in all_patterns:
        uid = p.get("pattern_uuid")
        if not uid or uid in seen:
            continue
        verdict = lexical_score(query_text, str(p.get("text") or ""))
        if verdict["passes"]:
            scored.append((verdict["ratio"], p, verdict["shared"]))
    scored.sort(key=lambda x: -x[0])

    out = list(vector_patterns)
    for ratio, p, shared in scored[:max_extra]:
        out.append({
            "pattern_uuid": p["pattern_uuid"],
            "pattern_name": p.get("pattern_name", "unknown"),
            "score": 0.0,                       # векторной близости не меряли
            "channel": "lexical",
            "lexical_shared": shared[:6],
            "confidence": p.get("confidence", "low"),
            "outcome_distribution": p.get("outcome_distribution", {}),
            "total_cases": p.get("total_cases", 0),
        })
    return out


__all__ = ["lexical_score", "merge_lexical_candidates", "tokenize"]
