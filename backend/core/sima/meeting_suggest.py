# -*- coding: utf-8 -*-
"""Автоподбор встреч под тему SIMA-проекта.

Закрывает ручную дыру контекста: встречи и документы в проект подключал
только человек, перебирая список глазами. Теперь система предлагает
кандидатов сама — по пересечению словаря проекта (название, описание,
блоки) со словарём встречи (название, сводка).

Скоринг детерминированный, без LLM — это предложение «посмотрите сюда»,
а не утверждение о релевантности. Правила честности:
  - нет пересечения — встреча не предлагается вовсе (пустой список лучше
    натянутых совпадений);
  - совпавшие слова возвращаются в ответе: видно, ПОЧЕМУ предложено;
  - стоп-слова и короткие слова не считаются совпадением.

Чистый stdlib, тестируется без БД.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

_WORD_RX = re.compile(r"[а-яёa-z0-9]{4,}", re.IGNORECASE)

# Слова, совпадение по которым ничего не значит в контексте проектов.
_STOP = frozenset({
    "чтобы", "который", "которая", "которые", "может", "можно", "нужно",
    "должен", "должна", "будет", "быть", "есть", "этот", "эта", "это",
    "проект", "проекта", "проекту", "система", "системы", "продукт",
    "продукта", "сервис", "сервиса", "встреча", "встречи", "обсуждение",
    "компания", "компании", "работа", "работы", "новый", "новая", "новое",
    "делать", "сделать", "также", "просто", "очень", "более", "менее",
    "when", "what", "with", "this", "that", "from", "have", "будем",
    "meeting", "project", "system", "product", "service",
})


def _words(text: Any) -> set:
    out = set()
    for w in _WORD_RX.findall(str(text or "").lower()):
        if w not in _STOP:
            out.add(w)
    return out


def project_vocabulary(project: Dict[str, Any]) -> set:
    """Словарь проекта: название, описание, блоки (название/описание/цель)."""
    parts = [project.get("name"), project.get("description")]
    for b in project.get("blocks") or []:
        if isinstance(b, dict):
            parts.extend([b.get("title"), b.get("name"),
                          b.get("description"), b.get("goal"),
                          b.get("problemSolved")])
    return _words(" ".join(str(p) for p in parts if p))


def score_meetings(
    project: Dict[str, Any],
    meetings: List[Dict[str, Any]],
    *,
    limit: int = 8,
    min_overlap: int = 2,
) -> List[Dict[str, Any]]:
    """Ранжировать встречи по пересечению словарей.

    min_overlap=2: одно случайное общее слово — не повод предлагать.
    Возвращает [{meeting, score, matched_words}], лучшие сверху.
    """
    vocab = project_vocabulary(project)
    if not vocab:
        return []
    scored = []
    for m in meetings or []:
        if not isinstance(m, dict):
            continue
        m_words = _words(" ".join(str(x) for x in (
            m.get("title"), m.get("name"), m.get("summary"),
            m.get("description")) if x))
        matched = vocab & m_words
        if len(matched) < min_overlap:
            continue
        # Нормировка на размер словаря встречи: длинная сводка не должна
        # выигрывать только за счёт объёма.
        score = len(matched) / (1 + len(m_words) ** 0.5)
        scored.append({
            "meeting": m,
            "score": round(score, 4),
            "matched_words": sorted(matched)[:10],
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]
