# -*- coding: utf-8 -*-
"""
Task grounding — вшивание сигналов скелета (заземление + desync + blind-spots)
в генерацию ТЗ, с фокусом на РЕЛЕВАНТНОСТЬ.

Несущая идея (из обсуждения): при куче встреч чистый LLM не охватит всё и не
найдёт нужное. Скелет охватывает систематически — НО охват ВСЕГО бесполезен без
охвата НУЖНОГО. Поэтому сигналы подмешиваются в ТЗ только если КАСАЮТСЯ сущностей
задачи (точное пересечение по сущностям), а не свалкой.

Что даёт (под дисциплиной §4 — заземлять в факты, не выдумывать):
1. check_task_grounding — сущности задачи, которых в компании НЕТ («создать ролик
   для продукта X», а продукта X у нас не существует → флаг, не молча генерим ТЗ).
2. select_relevant_signals — ТОЛЬКО те desync/blind-spots, что касаются сущностей
   задачи (точная релевантность, не «что-то»).
3. grounding_notes — короткие предупреждения для генерации ТЗ.

ЧЕСТНАЯ ГРАНИЦА (ответ на «охватим нужное или что-то»): релевантность точна
(пересечение сущностей детерминированно — мы берём ИМЕННО связанное, не шум). НО
RECALL ограничен ДЕТЕКЦИЕЙ: мы покрываем нужное СРЕДИ того, что обнаружили
(blind-spot нужен ≥3 встреч, desync нужны размеченные цели). Точность высокая,
полнота — потолок детекции. Чистый код (stdlib) → тестируется везде.
"""
from __future__ import annotations

import re
from typing import Optional


def _norm(s) -> str:
    return str(s).strip().lower()


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[^\W\d_]+", _norm(text), re.UNICODE) if len(w) > 3}


def _entity_token_match(a: str, b: str) -> bool:
    """Консервативный alias-матч: один — ТОКЕН-подмножество другого («polis» ↔
    «polis insurance»), но разные токены не сливаются («sales» ≠ «salesforce»)."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return a == b
    return ta <= tb or tb <= ta


def check_task_grounding(entities: list, known: Optional[set]) -> dict:
    """Сущности, на которые ссылается задача → существуют ли в компании.
    known — нормализованные имена известных сущностей (из графа/prior).
    Матч alias-aware (токен-граничный): «Polis Insurance» заземлится на «polis».
    Пусто known → нечего проверять (не over-reject)."""
    known = known or set()
    grounded, ungrounded = [], []
    for e in (entities or []):
        if not e or not str(e).strip():
            continue
        if not known:
            grounded.append(e)
        elif any(_entity_token_match(_norm(e), k) for k in known):
            grounded.append(e)
        else:
            ungrounded.append(e)
    return {"grounded": grounded, "ungrounded": ungrounded}


def _signal_touches(entities_norm: set, *texts) -> bool:
    """Сигнал релевантен задаче, если ИМЯ сущности задачи встречается в его тексте
    (точное пересечение по сущности — не общий смысл)."""
    if not entities_norm:
        return False
    blob = " ".join(_norm(t) for t in texts if t)
    return any(e in blob for e in entities_norm)


def select_relevant_signals(task_entities: list, desyncs: Optional[list] = None,
                            blind_spots: Optional[list] = None) -> dict:
    """ТОЛЬКО сигналы, КАСАЮЩИЕСЯ сущностей задачи (точная релевантность).
    desyncs: list[dict] (department_a/b, goal_a/b); blind_spots: list[dict]
    (pattern, evidence)."""
    ents = {_norm(e) for e in (task_entities or []) if e and str(e).strip()}
    rel_desync = [
        d for d in (desyncs or [])
        if _signal_touches(ents, d.get("department_a"), d.get("department_b"),
                           d.get("goal_a"), d.get("goal_b"))
    ]
    rel_blind = [
        b for b in (blind_spots or [])
        if _signal_touches(ents, b.get("pattern"), b.get("detail"))
    ]
    return {"desyncs": rel_desync, "blind_spots": rel_blind}


def grounding_notes(task_entities: list, known: Optional[set],
                    desyncs: Optional[list] = None, blind_spots: Optional[list] = None) -> list:
    """Короткие предупреждения для генерации ТЗ: несуществующие сущности +
    РЕЛЕВАНТНЫЕ рассинхроны/слепые зоны. Пусто → нечего добавлять."""
    notes: list = []
    g = check_task_grounding(task_entities, known)
    for e in g["ungrounded"]:
        notes.append(f"⚠️ «{e}» не найдено в компании — это новое? Уточнить до реализации "
                     "(другой отдел может не смочь это продать/использовать).")
    rel = select_relevant_signals(task_entities, desyncs, blind_spots)
    for d in rel["desyncs"][:3]:
        notes.append(f"⚠️ Возможен рассинхрон с отделом: {d.get('department_a')}↔{d.get('department_b')} "
                     f"({d.get('conflict_type', 'конфликт')}) — учесть при планировании.")
    for b in rel["blind_spots"][:3]:
        notes.append(f"⚠️ По этой теме повторяется: {b.get('pattern')} — заложить в риски.")
    return notes
