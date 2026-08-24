# -*- coding: utf-8 -*-
"""Политика эмоций для визуальных отчётов (VISUAL_REPORTS §7.2).

Эмоции/личные наблюдения важны руководителю, но недопустимы в командном отчёте.
Модуль решает, ЧТО показать и КОМУ:
- audience="private" (лично руководителю) — сохраняем всё, включая «человеческий»
  слой (кто на грани, где напряжение);
- audience="public"  (команде) — вычищаем чувствительное: убираем личный блок
  «внимание», гасим пометки sensitive и смягчаем явно чувствительные заметки.

Разметку даёт экстракция (поле "sensitive": true на личных/негативных сигналах);
ключевые слова — запасной эвристический фильтр. Guardrail: в ПУБЛИЧНУЮ версию
чувствительное о конкретных людях не попадает никогда.
"""
from __future__ import annotations

import copy
from typing import Any, Dict

# Запасной эвристический детектор (если экстракция не проставила sensitive).
_SENSITIVE_MARKERS = (
    "выгора", "перегруз", "на грани", "увол", "уход", "конфликт", "ссор",
    "обид", "недовол", "разочаров", "устал", "вымотан", "молчал", "выключил",
    "токсич", "саботаж", "не тянет", "проседает", "жалоб",
)

# Нейтральная замена вырезанной чувствительной заметки в структурных элементах.
_SOFTENED = ""


def _looks_sensitive(text: Any) -> bool:
    t = str(text or "").lower()
    return any(m in t for m in _SENSITIVE_MARKERS)


def _is_sensitive_item(it: Dict[str, Any]) -> bool:
    if it.get("sensitive") is True:
        return True
    for k in ("note", "text", "line", "attention", "caption"):
        if _looks_sensitive(it.get(k)):
            return True
    return False


def apply_emotion_policy(data: Dict[str, Any], audience: str) -> Dict[str, Any]:
    """Применить политику эмоций к структуре визуального отчёта.

    audience: "public" (команда) → чистим чувствительное; иначе (private) → как есть.
    Никогда не raises; на нераспознанной структуре — безопасный проброс.
    """
    if not isinstance(data, dict):
        return data
    aud = str(audience or "public").strip().lower()
    if aud not in ("public", "team", "команда", "публично"):
        return data  # лично / по умолчанию для личного — всё сохраняем

    d = copy.deepcopy(data)
    # 1) Личный блок «внимание» (напр. «признаки перегрузки») — только руководителю.
    d.pop("attention", None)

    # 1b) Мульти-портрет: чувствительные поля вложены В КАЖДОГО character —
    # без рекурсии attention/сенситив-хайлайты утекали в публичный текст-дубль
    # (и хайлайты — в картинку отряда). Guardrail §6/§7.2: чистим каждого.
    chars = d.get("characters")
    if isinstance(chars, list):
        d["characters"] = [apply_emotion_policy(c, aud) if isinstance(c, dict)
                           else c for c in chars]

    # 2) Дискретные «контентные» списки: чувствительные пункты УБИРАЕМ целиком.
    for key in ("highlights", "skills", "panels", "items"):
        arr = d.get(key)
        if isinstance(arr, list):
            d[key] = [it for it in arr
                      if not (isinstance(it, dict) and _is_sensitive_item(it))]

    # 3) Структурные списки (органы/ветви): элемент СОХРАНЯЕМ (иначе исчезнет
    #    отдел/тема), но чувствительную заметку гасим, а «боль» смягчаем.
    for key in ("organs", "branches"):
        arr = d.get(key)
        if not isinstance(arr, list):
            continue
        for it in arr:
            if not isinstance(it, dict):
                continue
            if it.get("sensitive") is True or _looks_sensitive(it.get("note")):
                it["note"] = _SOFTENED
                it.pop("sensitive", None)
            # «боль» о конкретном отделе публично не выпячиваем → «напряжение».
            if str(it.get("state") or "").lower() in ("pain", "down"):
                it["state"] = "tension"
            # вложенные пункты ветви
            sub = it.get("items")
            if isinstance(sub, list):
                it["items"] = [s for s in sub
                               if not (isinstance(s, dict) and _is_sensitive_item(s))]
    return d


# Значения, означающие «отчёт для одного человека, а не для команды».
# Список приходится держать явным: значение приходит из карточки узла и из
# встроенных шаблонов, где его писали по-разному. Не узнали слово — считаем
# публичным: ошибка в эту сторону скрывает лишнее, ошибка в другую —
# показывает команде «на грани выгорания».
_PRIVATE_WORDS = frozenset({
    "private", "личное", "лично", "личный", "me", "self",
    # Руководительская версия — тоже адресная: её смысл в том, чтобы
    # показать то, что вычищается из командной. Встроенный шаблон
    # «Два отчёта: команде и себе» ставит именно "leader", и без этого
    # слова он молча отдавал руководителю ту же вычищенную версию,
    # то есть обещал полный отчёт и не давал его.
    "leader", "manager", "руководитель", "руководителю", "boss",
})


def audience_of(data_or_str: Any) -> str:
    """Нормализовать значение аудитории к 'public' | 'private'."""
    v = str(data_or_str or "public").strip().lower()
    return "private" if v in _PRIVATE_WORDS else "public"


__all__ = ["apply_emotion_policy", "audience_of"]
