# -*- coding: utf-8 -*-
"""
Answer-mode router — выбор режима ответа чата: factual vs analysis
(MEMORY_DESIGN_PRINCIPLES §8; обоснование — docs/ru/BENCHMARK_ANALYSIS_MODE.md).

ЗАЧЕМ. Замер показал: на ДЕШЁВОЙ модели multi-session/temporal вопросы и
синтез решаются ГОРАЗДО лучше, если дать хронологический таймлайн + analysis-
промпт (компилируй, рассуждай о порядке/изменениях), а не строгий «NOT
MENTIONED». При этом для прямых фактических lookup'ов строгий режим уместнее
(точно, без воды). Этот роутер выбирает режим по вопросу.

Два режима:
- factual:  «когда дедлайн X», «кто назначен на Y» → строгий промпт, top-k ретрив.
- analysis: «когда пошло не так», «вся история проекта», «как идёт проект сейчас»,
            «кого нанять», multi-session/temporal → analysis-промпт, ХРОНО-таймлайн.

Гарантия (баланс из LoCoMo adversarial): analysis-промпт компилирует, НО при
полностью отсутствующих данных говорит «CANNOT DETERMINE» — не выдумывает.

Чистый stdlib примитив (тестируем изолированно). Подключение в chat — отдельный
аккуратный шаг (см. docs/ru/BENCHMARK_ANALYSIS_MODE.md §продуктовый план).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Промпты — ДОСЛОВНО валидированные в benchmark_analysis_mode.py / benchmark_locomo.py.
FACTUAL_PROMPT = (
    "You are an assistant with memory. Answer the user's question using ONLY the "
    "provided memory. Be precise and concise. If the memory does not contain the "
    "answer, say exactly: NOT MENTIONED."
)

ANALYSIS_PROMPT = (
    "You are an analyst with full timestamped memory of the company/user history. "
    "Memory is given in CHRONOLOGICAL order with dates. ANALYZE, don't just look up: "
    "combine information ACROSS different sessions/meetings/dates, reason about ORDER "
    "and CHANGE over time (how it was before vs now, where the inflection happened). "
    "Give your BEST supported answer derived from the data — compile it even if there "
    "is no single direct statement. \n"
    "HONESTY FRAMING (important): clearly separate what is EXPLICITLY stated in the "
    "memory from what you INFER. State facts plainly; mark inferences with 'likely' / "
    "'based on the discussions' / 'this suggests'. If you have nothing to work with at "
    "all, say 'CANNOT DETERMINE' — do not invent. \n"
    "TEMPORAL RESOLUTION (measured +0.20 on LoCoMo temporal): each memory item has a "
    "DATE. Relative expressions ('yesterday', 'last week', 'last month', 'last summer', "
    "'next month') are relative to THAT item's date — APPLY THE OFFSET (yesterday = date "
    "minus 1 day; last week ~ 7 days before; last month/summer/year = the previous one) "
    "to get the ABSOLUTE date; do NOT output the item's own date unchanged. For 'now / "
    "current / latest' questions rely on the MOST RECENT dated items, not old ones. \n"
    "End with one concise final answer."
)

# Калиброванный analysis (gated): то же РАССУЖДЕНИЕ, но СИЛЬНАЯ дисциплина опоры —
# чтобы не выдумывать на неотвечаемых вопросах (LoCoMo adversarial: ANALYSIS дал
# 0.4, нужно вернуть к ~1.0, сохранив multi-hop/temporal выигрыш).
# Правит over-compile: запрещает ВЫВОДИТЬ невысказанные мнения/ценности/чувства.
ANALYSIS_GATED_PROMPT = (
    "You are an analyst with full timestamped memory (CHRONOLOGICAL, with dates). "
    "ANALYZE: combine info ACROSS sessions/meetings/dates and reason about ORDER and "
    "CHANGE over time. \n"
    "EVIDENCE RULE (critical): answer ONLY if the memory contains DIRECT supporting "
    "evidence (an explicit statement, or facts you can combine by clear multi-hop "
    "reasoning). If the specific thing asked (a value, feeling, opinion, fact, number) "
    "is NOT explicitly present — even if RELATED topics are discussed — say exactly "
    "'CANNOT DETERMINE'. Do NOT infer unstated beliefs, preferences, or motivations. "
    "Do NOT guess. \n"
    "When you DO answer, briefly cite which sessions/dates support it. End with one "
    "concise final answer."
)


# Маркеры, требующие АНАЛИЗА (временна́я динамика / история / multi-hop / синтез).
# Широкий список — лучше дать анализ, чем строго отказать (требование §8).
_ANALYSIS_MARKERS = re.compile(
    r"(когда (пошло|начал|случил|произош|стал)|"           # «когда пошло не так»
    r"что (пошло|случил|произош|измен|стал)|"
    r"как (мы |вы |)(дошл|пришл|менял|измен|развив|идёт|идет|работа|обстоят)|"
    r"истори[яю]|динамик|за (последн|период|квартал|год|месяц|время|всё время|все время)|"
    r"со времен|с начала|хронолог|таймлайн|раскол|перелом|тренд|"
    r"сколько раз|как часто|когда именно|в какой момент|"
    r"проанализир|анализ|сравни|почему|зачем|кого (нам |)(над|нужно|нанять)|"
    r"чего не хватает|где (у нас |мы )|что не так|вся (истор|инфо|картин)|"
    r"сейчас\b.*проект|проект.*сейчас|команд.*сейчас|сейчас.*команд|"
    # advice/recommendation — нужен СИНТЕЗ предпочтений/истории (измерено:
    # single-session-preference 0.0 strict → 0.667 analysis). Эти запросы в
    # LongMemEval сформулированы как «any suggestions/tips?», «what should I…».
    r"посоветуй|порекоменд|есть (идеи|совет|рекоменд)|как мне\b|помоги (с |мне |спланир|выбрать)|"
    r"что мне (стоит|лучше|приготов|подать|сделать|выбрать)|"
    r"any (suggestions?|tips?|ideas?|advice|recommendations?)|"
    r"do you have (any )?(ideas?|tips?|suggestions?|advice)|can you (suggest|recommend)|"
    r"what should i|how (can|do) i|help me (with|plan|decide|choose)|recommend|suggest some|"
    r"timeline|history|over time|when did|what changed|how (did|have) (we|things)|"
    r"analyz|trend|evolution|why )",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class AnswerMode:
    """Решение роутера. mode: 'factual' | 'analysis'."""
    mode: str
    system_prompt: str
    use_timeline: bool       # собирать ли хронологический таймлайн (vs top-k)
    reason: str


def needs_analysis(question: Optional[str]) -> bool:
    """Есть ли в вопросе маркеры анализа/динамики/синтеза."""
    if not question:
        return False
    if _ANALYSIS_MARKERS.search(str(question)):
        return True
    # синтез-интенты из abstention_advisor (кого нанять, стратегия, ...)
    try:
        from backend.core.think.abstention_advisor import classify_question_intent
        if classify_question_intent(question) == "synthesis":
            return True
    except Exception:
        pass
    return False


def route(question: Optional[str]) -> AnswerMode:
    """Выбрать режим ответа по вопросу.

    analysis — для временно́й динамики / истории / multi-hop / синтеза
               (таймлайн + компиляция, баланс с CANNOT DETERMINE).
    factual  — для прямых фактических lookup'ов (строгий, top-k).
    """
    if needs_analysis(question):
        return AnswerMode(
            mode="analysis", system_prompt=ANALYSIS_PROMPT, use_timeline=True,
            reason="вопрос про динамику/историю/синтез — компилируем по таймлайну",
        )
    return AnswerMode(
        mode="factual", system_prompt=FACTUAL_PROMPT, use_timeline=False,
        reason="прямой фактический lookup — строгий ответ по top-k",
    )
