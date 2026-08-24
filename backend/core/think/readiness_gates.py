# -*- coding: utf-8 -*-
"""
Readiness gates / Q0 — REUSE-IN-CODE из claude_skills/ai_business_audit (Раздел III
«Матрица решений», Раздел V «Фреймворк готовности»).

Зачем (дисциплинирует ТЗ-рекомендации): LLM любит советовать «внедрить новый
инструмент». Эти гейты — скелет ПОВЕРХ рекомендаций: (1) Q0 — если рекомендация
про активацию уже КУПЛЕННОГО (самые дешёвые победы всегда там) — пометить
«активировать, не строить»; (2) фильтр «Лучше руками?» — если процесс редкий /
требует живого суждения / нет владельца / часто меняется — НЕ автоматизировать.
Это ровно то, что отличает консультанта от продавца софта.

Дисциплина честности (claim-guard, как в signal_library): из ТЕКСТА рекомендации
сигналы ловятся консервативно (характерные маркеры, не общие слова). Не уверены —
рекомендация проходит как есть, без ярлыка. Чистый stdlib → тесты везде, без LLM.

Источник дословно: III.3.1 (5 квадрантов Q0–Q4), III.3.2 (фильтр «Лучше руками?»,
5 вопросов), IId (Q0 — активация уже купленного).
"""
from __future__ import annotations

import re

# --- Квадранты (III.3.1) -----------------------------------------------------
# Q0 — активировать уже купленное; Q1 — быстрая победа; Q2 — стратегическая
# инвестиция; Q3 — под вопросом; Q4 — не делать.
_QUADRANTS = {
    "Q0": ("АКТИВИРОВАТЬ", "инструмент уже куплен — включить и настроить (первый приоритет)"),
    "Q1": ("СДЕЛАТЬ ПЕРВЫМ", "высокая ценность + низкая сложность — максимальный ROI на усилие"),
    "Q2": ("СТРАТЕГИЧЕСКАЯ ИНВЕСТИЦИЯ", "высокая ценность + высокая сложность — нужен владелец и горизонт 3–6 мес"),
    "Q3": ("ПОД ВОПРОСОМ", "низкая ценность + низкая сложность — только если «по пути»"),
    "Q4": ("НЕ ДЕЛАТЬ", "низкая ценность + высокая сложность — прямо сказать «не стоит»"),
}


def quadrant(value_high: bool, complexity_high: bool, already_owned: bool = False) -> dict:
    """Квадрант рекомендации. already_owned перебивает всё → Q0 (IId)."""
    if already_owned:
        key = "Q0"
    elif value_high and not complexity_high:
        key = "Q1"
    elif value_high and complexity_high:
        key = "Q2"
    elif not value_high and not complexity_high:
        key = "Q3"
    else:
        key = "Q4"
    label, action = _QUADRANTS[key]
    return {"quadrant": key, "label": label, "action": action}


# --- Q0: активация уже купленного (IId / III.3.1) ----------------------------
# Характерные маркеры «оно уже есть, просто включить».
_Q0_MARKERS = [
    r"уже\s+(куплен|оплач|есть|внедрён|внедрен|настро)",
    r"не\s+используе(тся|м)\b", r"лежит\s+(мёртв|без дела)",
    r"включить\s+(функци|модул|интеграц)", r"активир",
    r"функци[яю]\s+уже", r"настроить\s+уже",
]
_Q0_RX = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in _Q0_MARKERS]

# --- Фильтр «Лучше руками?» (III.3.2), 5 сигналов ----------------------------
# Каждый — отдельный «руками»-сигнал. ≥2 в одной рекомендации → предупредить,
# ≥3 → сильно («скорее руками, не автоматизировать»).
_MANUAL_SIGNALS = {
    "редкость": r"раз\s+в\s+(месяц|квартал|год)|ежеквартальн|редко\b|нечаст",
    "живое_суждение": r"суждени|интуици|креатив|переговор|индивидуальн\w*\s+подход|на\s+глаз",
    "нет_владельца": r"нет\s+(владельца|ответственн|хозяина)|некому\s+(вести|поддерж)",
    "меняется": r"част[оа]\s+меня|постоянно\s+меня|нестабильн\w*\s+процесс|меняется\s+каждые",
    "малый_объём": r"\d+\s*минут\s+в\s+неделю|пар[уаы]\s+раз|единичн",
}
_MANUAL_RX = {k: re.compile(v, re.IGNORECASE | re.UNICODE) for k, v in _MANUAL_SIGNALS.items()}


def q0_signal(text: str, known_tools=None) -> bool:
    """Похоже ли, что рекомендация — про активацию уже имеющегося (Q0).
    Маркер активации ИЛИ упоминание уже купленного инструмента из known_tools."""
    if not text:
        return False
    t = str(text)
    if any(rx.search(t) for rx in _Q0_RX):
        return True
    for tool in (known_tools or []):
        if tool and len(str(tool)) >= 3 and re.search(re.escape(str(tool)), t, re.IGNORECASE):
            # упомянут уже купленный инструмент + глагол «использовать полнее»
            if re.search(r"использова|задейств|подключ|интегр|включ", t, re.IGNORECASE):
                return True
    return False


def manual_better_signals(text: str) -> list:
    """Какие «руками»-сигналы (из 5) присутствуют в тексте рекомендации."""
    if not text:
        return []
    t = str(text)
    return [k for k, rx in _MANUAL_RX.items() if rx.search(t)]


def assess_recommendation(text: str, known_tools=None) -> dict:
    """Оценить ОДНУ рекомендацию-строку гейтами готовности.

    Возвращает {text, q0, manual_signals, verdict, note}. Консервативно:
    verdict/note появляются только при уверенном сигнале, иначе verdict='ok' и
    рекомендация идёт как есть (claim-guard)."""
    q0 = q0_signal(text, known_tools)
    ms = manual_better_signals(text)
    if q0:
        verdict = "Q0"
        note = "⚡ Q0 — активировать уже имеющееся, не строить новое (самая дешёвая победа)"
    elif len(ms) >= 3:
        verdict = "manual"
        note = f"✋ Скорее РУКАМИ, не автоматизировать (сигналы: {', '.join(ms)})"
    elif len(ms) >= 2:
        verdict = "check_manual"
        note = f"⚠️ Проверь фильтр «лучше руками?» (сигналы: {', '.join(ms)})"
    else:
        verdict = "ok"
        note = ""
    return {"text": text, "q0": q0, "manual_signals": ms, "verdict": verdict, "note": note}


# --- СТРУКТУРНЫЙ путь (фикс после Gate-0) ------------------------------------
# Gate-0 вскрыл: сигналы «руками»/Q0 живут в ОПИСАНИИ ПРОЦЕССА, не в тексте
# рекомендации. Поэтому правильный вход — атрибуты процесса, а не строки. Тогда
# фильтр «Лучше руками?» (III.3.2, 5 вопросов) и квадрант работают ПО СУТИ.
_RARE_FREQ = {
    "monthly", "quarterly", "yearly", "rare", "ad-hoc",
    "ежемесячно", "ежеквартально", "раз в год", "раз в месяц", "раз в квартал", "редко",
}


def assess_process(
    frequency: str | None = None,
    needs_judgment: bool = False,
    has_owner: bool = True,
    volatile: bool = False,
    already_owned: bool = False,
    value_high: bool = True,
    complexity_high: bool = False,
) -> dict:
    """Оценить ПРОЦЕСС по структурным атрибутам (не по тексту) — честный фикс.

    Фильтр «Лучше руками?» (5 вопросов источника): редкость / живое суждение /
    нет владельца / изменчивость. ≥3 → руками; ≥2 → проверить. already_owned → Q0
    перебивает всё. Иначе — квадрант по ценности/сложности."""
    manual: list = []
    if frequency and str(frequency).strip().lower() in _RARE_FREQ:
        manual.append("редкость")          # вопрос 1: реже раза в неделю
    if needs_judgment:
        manual.append("живое_суждение")    # вопрос 2: требует суждения/интуиции
    if not has_owner:
        manual.append("нет_владельца")     # вопрос 4: нет ответственного
    if volatile:
        manual.append("меняется")          # вопрос 5: процесс меняется каждые 2–3 мес

    q = quadrant(value_high, complexity_high, already_owned)
    if already_owned:
        verdict, note = "Q0", "⚡ Q0 — активировать уже имеющееся, не строить новое"
    elif len(manual) >= 3:
        verdict = "manual"
        note = f"✋ Скорее РУКАМИ, не автоматизировать (сигналы: {', '.join(manual)})"
    elif len(manual) >= 2:
        verdict = "check_manual"
        note = f"⚠️ Под вопросом — проверить «лучше руками?» (сигналы: {', '.join(manual)})"
    else:
        verdict = q["quadrant"]
        note = f"{q['label']} — {q['action']}"
    return {
        "verdict": verdict, "note": note, "manual_signals": manual,
        "quadrant": q["quadrant"], "quadrant_label": q["label"],
    }


# Атрибуты, которые extractor (LLM/пайплайн) должен достать из описания процесса.
PROCESS_ATTRIBUTES = [
    "frequency", "needs_judgment", "has_owner", "volatile",
    "already_owned", "value_high", "complexity_high",
]


def annotate_recommendations(recommendations: list, known_tools=None) -> list:
    """Оценить список рекомендаций. Q0 поднимаются вверх (приоритет источника
    Q0→Q1→…), «руками» помечаются. Порядок внутри групп сохраняется (stable)."""
    assessed = [assess_recommendation(r, known_tools) for r in (recommendations or [])]
    # Q0 — первыми (стабильно), остальные в исходном порядке.
    order = {"Q0": 0}
    return sorted(assessed, key=lambda a: order.get(a["verdict"], 1))


def render_recommendation_line(assessment: dict) -> str:
    """Строка рекомендации с ярлыком гейта (для отчёта). Без сигнала — чистый текст."""
    base = f"- {assessment['text']}"
    return f"{base}\n  {assessment['note']}" if assessment.get("note") else base
