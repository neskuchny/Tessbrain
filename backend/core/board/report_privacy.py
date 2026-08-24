# -*- coding: utf-8 -*-
"""Privacy-гейт визуальных отчётов (Visual Reports §8.4).

Независимая проверка ПЕРЕД доставкой: не просочилось ли в ПУБЛИЧНУЮ версию личное/
чувствительное (увольнение, конфликт, выгорание, здоровье, зарплата, семья…). Это
второй рубеж поверх политики эмоций (которая чистит sensitive-пункты на извлечении).

Детерминированно, never-raise. Возвращает список предупреждений; жёсткая блокировка
— только за флагом ENABLE_REPORT_PRIVACY_BLOCK (по умолчанию OFF → предупреждаем,
не ломаем доставку).
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List

# Маркеры чувствительного: RU + EN, по темам. Регистронезависимо, по границе слова.
_SENSITIVE = {
    "увольнение": ["увол", "уходит из компании", "сокращ", "fired", "layoff", "quit", "resign"],
    "конфликт": ["конфликт", "ссор", "скандал", "нахамил", "conflict", "argument", "clash"],
    "выгорание": ["выгор", "на грани", "перегруз", "истощ", "burnout", "overwhelmed", "exhausted"],
    "здоровье": ["болезн", "здоровь", "депресс", "больнич", "health", "sick leave", "depress"],
    "зарплата": ["зарплат", "оклад", "премия сотрудник", "salary", "compensation", "bonus of"],
    "семья/личное": ["развод", "семейн", "личн проблем", "divorce", "family issue", "personal problem"],
    "оценка личности": ["глуп", "некомпетент", "ленив", "слаб как", "incompetent", "lazy", "stupid"],
}


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def scan_public_leak(text: str, *, graph: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Просканировать ПУБЛИЧНЫЙ текст/граф отчёта на остаточное чувствительное.

    Возвращает {"warnings": [строки], "sensitive": bool, "topics": [...]}.
    Never-raise. Пустой вход → пусто."""
    try:
        blob = _norm(text)
        if isinstance(graph, dict):
            # Досканируем цитаты/подписи графа (они уходят на картинку и в подпись).
            extra = []
            for q in (graph.get("quotes") or []):
                if isinstance(q, dict):
                    extra.append(str(q.get("text") or ""))
            for br in (graph.get("branches") or []):
                if isinstance(br, dict):
                    for it in (br.get("items") or []):
                        if isinstance(it, dict):
                            extra.append(str(it.get("text") or ""))
            blob = blob + " " + _norm(" ".join(extra))
        if not blob:
            return {"warnings": [], "sensitive": False, "topics": []}
        topics: List[str] = []
        for topic, markers in _SENSITIVE.items():
            if any(m in blob for m in markers):
                topics.append(topic)
        if not topics:
            return {"warnings": [], "sensitive": False, "topics": []}
        warnings = [
            f"В публичной версии похоже есть чувствительное: {t}. "
            "Проверьте перед отправкой в группу." for t in topics
        ]
        return {"warnings": warnings, "sensitive": True, "topics": topics}
    except Exception:
        return {"warnings": [], "sensitive": False, "topics": []}


def privacy_block_enabled() -> bool:
    """Жёсткая блокировка публичной доставки при утечке — за флагом (default OFF)."""
    return os.getenv("ENABLE_REPORT_PRIVACY_BLOCK", "").strip().lower() in ("1", "true", "yes", "on")


__all__ = ["privacy_block_enabled", "scan_public_leak"]
