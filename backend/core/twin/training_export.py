# -*- coding: utf-8 -*-
"""Датасет для дообучения ИИ-копии сотрудника.

Задача: «создать нашему директору его копию, дообученную ИИ». Этот модуль
строит ОБУЧАЮЩИЙ ДАТАСЕТ (JSONL, формат messages: system/user/assistant),
пригодный для fine-tune любого провайдера, который такой формат принимает.
Само дообучение происходит вне системы — это выгрузка данных, а не тренировка.

Из чего строятся примеры (только реальные данные, никаких синтетических
диалогов):
  • решения человека  → «какое решение ты принял по …?» → решение;
  • мнения            → «что ты думаешь про …?» → мнение (с тональностью);
  • идеи              → «какие идеи у тебя были про …?» → идея;
  • system везде один — карточка слепка (роль, стиль, правила честности).

Честность: если данных мало, датасет будет маленьким — stats покажут
сколько примеров и из чего; мы не раздуваем его выдумкой. Для приличного
fine-tune обычно нужны сотни примеров — об этом прямо говорит поле note.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _topic(text: str, limit: int = 60) -> str:
    """Короткая тема из текста — для формулировки вопроса."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def _example(system: str, user: str, assistant: str) -> Dict[str, Any]:
    return {"messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]}


def build_examples(snap: Any, *, system_card: str) -> List[Dict[str, Any]]:
    """Примеры из решений/мнений/идей слепка. Чистая функция — тестируема."""
    out: List[Dict[str, Any]] = []

    for r in getattr(snap, "decisions", None) or []:
        if not isinstance(r, dict):
            continue
        t = str(r.get("summary") or r.get("text") or "").strip()
        if not t:
            continue
        cat = str(r.get("category") or "").strip()
        q = f"Какое решение ты принял по теме «{_topic(t)}» и почему?"
        a = t + (f"\n\n(Тип решения: {cat})" if cat else "")
        out.append(_example(system_card, q, a))

    for r in getattr(snap, "opinions", None) or []:
        if not isinstance(r, dict):
            continue
        t = str(r.get("summary") or r.get("text") or "").strip()
        if not t:
            continue
        sent = str(r.get("sentiment") or "").strip()
        q = f"Что ты думаешь про «{_topic(t)}»?"
        a = t + (f"\n\n(Отношение: {sent})" if sent else "")
        out.append(_example(system_card, q, a))

    for r in getattr(snap, "ideas", None) or []:
        t = (str(r.get("summary") or r.get("text") or "").strip()
             if isinstance(r, dict) else str(r or "").strip())
        if not t:
            continue
        out.append(_example(
            system_card, f"Какие идеи у тебя были про «{_topic(t)}»?", t))

    # Пример честного отказа — чтобы копия УЧИЛАСЬ не выдумывать.
    out.append(_example(
        system_card,
        "Какой у нас бюджет на следующий год?",
        "В моём слепке этого нет — я не буду угадывать. Спроси у "
        "финансового директора или посмотри актуальные документы."))
    return out


async def build_training_dataset(user_id: str, person_id: str) -> Dict[str, Any]:
    """Собрать датасет дообучения ИИ-копии сотрудника."""
    from backend.core.twin.profile import load_twin, twin_system_prompt

    snap, profile, voice = await load_twin(user_id, person_id)
    if snap is None:
        return {"status": "no_data",
                "message": (f"Слепка для «{person_id}» нет — датасет строить "
                            "не из чего.")}

    name = snap.name
    role = str(getattr(snap, "role", "") or "").strip()
    # System-карточка для примеров — короче полного профиля (полный профиль
    # в каждом примере раздул бы датасет), но с теми же правилами честности.
    card_bits = [f"Ты — {name}" + (f", {role}" if role else "") +
                 ". Отвечай в его манере, от первого лица."]
    if voice:
        card_bits.append(voice)
    card_bits.append("Правило: отвечай только тем, что реально знаешь из "
                     "своего опыта; чего не знаешь — честно говори «в моём "
                     "слепке этого нет».")
    system_card = "\n".join(card_bits)

    examples = build_examples(snap, system_card=system_card)
    jsonl = "\n".join(json.dumps(e, ensure_ascii=False) for e in examples)

    n = len(examples)
    note = ("Датасет готов к fine-tune (формат messages/JSONL). "
            f"Примеров: {n}. ")
    if n < 50:
        note += ("Этого мало для качественного дообучения (обычно нужны "
                 "сотни): продолжайте синхронизировать встречи — слепок и "
                 "датасет растут автоматически.")
    else:
        note += "Объём разумный для первого прогона дообучения."

    return {
        "status": "success",
        "person": name,
        "format": "jsonl-messages",
        "examples_count": n,
        "sources": {
            "decisions": len(getattr(snap, "decisions", None) or []),
            "opinions": len(getattr(snap, "opinions", None) or []),
            "ideas": len(getattr(snap, "ideas", None) or []),
        },
        "system_prompt_full": twin_system_prompt(name, profile, voice),
        "jsonl": jsonl,
        "note": note,
    }
