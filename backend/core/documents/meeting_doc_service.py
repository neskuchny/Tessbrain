# -*- coding: utf-8 -*-
"""
LLM-слой «Документов по встрече»: извлечение полей (Режим A) и сборка
содержимого (Режим B). Чистые функции — принимают llm_router и текст встречи,
роут прокидывает загрузку встречи/шаблона/реквизитов.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.core.documents import meeting_doc_engine as E

logger = logging.getLogger(__name__)


def _parse_json(content: Optional[str]) -> Any:
    if not content:
        return None
    t = content.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except Exception:
        return None


# ── Режим A: извлечь значения плейсхолдеров из встречи с уверенностью ────────

async def extract_fields_from_meeting(
    llm, *, placeholders: List[str], meeting_text: str,
    requisites: Optional[Dict[str, str]] = None,
    field_hints: Optional[Dict[str, str]] = None,
    extra_context: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Заполнить плейсхолдеры {{поле}} ЗНАЧЕНИЯМИ ИЗ ВСТРЕЧИ с пометкой
    уверенности high/low/missing и цитатой-подтверждением.

    Поля-реквизиты компании (company_/supplier_/…) НЕ спрашиваем у LLM —
    берём из requisites (детерминированно). Возвращает {key: {value,
    confidence, quote}}.
    """
    requisites = requisites or {}
    field_hints = field_hints or {}

    # Реквизиты — сразу из профиля (не из встречи).
    result: Dict[str, Dict[str, Any]] = {}
    ask_keys: List[str] = []
    for key in placeholders:
        if _is_company_slot(key) and key in requisites:
            result[key] = {"value": requisites[key], "confidence": E.CONF_HIGH,
                           "quote": "реквизиты компании"}
        elif key in requisites:  # явное совпадение с реквизитом
            result[key] = {"value": requisites[key], "confidence": E.CONF_HIGH,
                           "quote": "реквизиты компании"}
        else:
            ask_keys.append(key)

    if not ask_keys:
        return result
    if not (meeting_text or "").strip():
        for key in ask_keys:
            result[key] = {"value": "", "confidence": E.CONF_MISSING, "quote": ""}
        return result

    fields_block = "\n".join(
        f'  - "{k}"' + (f" — {field_hints[k]}" if field_hints.get(k) else "")
        for k in ask_keys
    )
    prompt = (
        "Ты заполняешь ДЕЛОВОЙ ДОКУМЕНТ данными из встречи. Для КАЖДОГО поля "
        "найди значение в тексте встречи. Честно оцени уверенность:\n"
        '  - "high" — нашёл прямо во встрече (приведи цитату);\n'
        '  - "low" — вывел/предположил, стоит проверить;\n'
        '  - "missing" — во встрече этого нет (value оставь пустым).\n'
        "НЕ ВЫДУМЫВАЙ значения — лучше missing, чем ложные данные.\n\n"
        f"ПОЛЯ:\n{fields_block}\n\n"
        + (f"ДОП. КОНТЕКСТ:\n{extra_context}\n\n" if extra_context.strip() else "")
        + "ТЕКСТ ВСТРЕЧИ:\n" + meeting_text[:14000] + "\n\n"
        'Верни СТРОГО JSON: { "<поле>": {"value": "<значение или пусто>", '
        '"confidence": "high|low|missing", "quote": "<короткая цитата или пусто>"} }'
    )
    try:
        raw = await llm.generate(
            prompt=prompt,
            system_prompt="Ты аккуратно заполняешь документы по встрече. Только JSON. Не выдумываешь.",
            temperature=0.0, max_tokens=2000,
        )
    except Exception as e:
        logger.warning("[MeetingDoc] extract failed: %s", e)
        raw = None
    data = _parse_json(raw)
    valid = {E.CONF_HIGH, E.CONF_LOW, E.CONF_MISSING}
    for key in ask_keys:
        f = (data or {}).get(key) if isinstance(data, dict) else None
        if isinstance(f, dict):
            conf = str(f.get("confidence", E.CONF_MISSING)).lower()
            if conf not in valid:
                conf = E.CONF_LOW if f.get("value") else E.CONF_MISSING
            val = f.get("value")
            if val is None or str(val).strip() == "":
                conf = E.CONF_MISSING
                val = ""
            result[key] = {"value": str(val), "confidence": conf,
                           "quote": str(f.get("quote", ""))[:300]}
        else:
            result[key] = {"value": "", "confidence": E.CONF_MISSING, "quote": ""}
    return result


def _is_company_slot(key: str) -> bool:
    k = (key or "").lower()
    return any(k.startswith(p) for p in E.COMPANY_SLOT_PREFIXES)


# ── Режим B: собрать содержимое документа по встрече и примеру стиля ─────────

async def compose_document(
    llm, *, doc_kind: str, meeting_text: str,
    style_example: str = "", extra_context: str = "",
    requisites: Optional[Dict[str, str]] = None,
    custom_prompt: str = "",
) -> str:
    """Собрать содержимое документа (markdown) ПО ВСТРЕЧЕ. Если задан
    style_example — повторить его структуру/тон, но факты взять из встречи
    (не копировать пример). Для КП система сама подбирает, что предложить.

    doc_kind: 'kp' | 'contract' | 'card' | 'free'. custom_prompt — свои
    указания к формату/тону.
    """
    kind_brief = {
        "kp": ("коммерческое предложение: задача клиента (из встречи) → что "
               "предлагаем и почему это решает задачу → состав работ/услуг → "
               "ориентир по стоимости (если обсуждалась) → следующий шаг"),
        "contract": ("проект договора (ЧЕРНОВИК, требует юриста): предмет, "
                     "стороны, обязанности, сроки, стоимость, порядок расчётов"),
        "card": ("карточка/сводка по итогам встречи: ключевые факты, участники, "
                 "договорённости, следующие шаги"),
        "free": "документ по итогам встречи",
    }.get(doc_kind, "документ по итогам встречи")

    parts = [
        "Ты готовишь ДЕЛОВОЙ ДОКУМЕНТ по итогам встречи. Тип: " + kind_brief + ".",
        "Пиши ТОЛЬКО на основе встречи и контекста ниже — не выдумывай факты и "
        "цифры. Где данных нет, пиши «уточнить» вместо выдуманного. Markdown, "
        "готовый к отправке, без markdown-звёздочек в теле (используй обычный текст).",
    ]
    if style_example.strip():
        parts.append(
            "ОБРАЗЕЦ СТИЛЯ (повтори структуру, тон и уровень детализации, но "
            "ФАКТЫ бери из встречи, а не из образца):\n" + style_example[:4000])
    if requisites:
        rq = "; ".join(f"{k}: {v}" for k, v in list(requisites.items())[:12] if v)
        if rq:
            parts.append("РЕКВИЗИТЫ КОМПАНИИ (используй при необходимости): " + rq)
    if custom_prompt.strip():
        parts.append("ПОЖЕЛАНИЯ К ФОРМАТУ: " + custom_prompt.strip())
    if extra_context.strip():
        parts.append("ДОП. КОНТЕКСТ:\n" + extra_context.strip())
    parts.append("ТЕКСТ ВСТРЕЧИ:\n" + (meeting_text or "")[:14000])
    prompt = "\n\n".join(parts)

    try:
        content = await llm.generate(
            prompt=prompt,
            system_prompt="Ты — деловой ассистент, готовишь документы по встречам. Точно, по делу, без выдумок.",
            temperature=0.4, max_tokens=2500,
        )
    except Exception as e:
        logger.warning("[MeetingDoc] compose failed: %s", e)
        return ""
    return (content or "").strip()
