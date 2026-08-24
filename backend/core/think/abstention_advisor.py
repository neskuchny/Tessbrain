# -*- coding: utf-8 -*-
"""
Abstention advisor — ОСТОРОЖНЫЙ совет «нет в памяти»
(MEMORY_DESIGN_PRINCIPLES §8, роадмап §6 №7).

⚠️ ГЛАВНОЕ ОГРАНИЧЕНИЕ (требование): система построена так, что явного ответа
может НЕ быть, но она должна НАЧАТЬ ответ и СКОМПИЛИРОВАТЬ его из данных. Пример:
«кого нам нанять» — прямого ответа в базе нет, но система должна собрать сигналы
и ответить. Простое «нет в памяти» это УБИВАЕТ. Поэтому:

1. Это СОВЕТУЮЩИЙ примитив, НЕ гейт. Возвращает рекомендацию; решает caller.
2. ПО УМОЛЧАНИЮ НЕ абстенит — при сомнении → «answer/compile», не «нет».
3. Синтез/консалтинг-интенты ЖЁСТКО исключены из абстенции.
4. Абстенция уместна ТОЛЬКО в явном fast-lookup режиме И при пустом/нерелевантном
   ретриве (самый простой/быстрый чат).
5. НЕ для пути ТЗ/синтеза.

Дизайн-инвариант (тестируется): при любой неоднозначности предпочитаем
should_abstain=False (система пытается ответить/компилировать).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Интенты, которые КОМПИЛИРУЮТ ответ из частичных сигналов — НИКОГДА не абстенят.
# Это сердце ценности системы (консультант, а не справочник).
SYNTHESIS_INTENTS = {
    "synthesis", "consulting", "consult", "recommendation", "recommend",
    "hiring", "hire", "who_to_hire", "strategy", "planning", "plan",
    "analysis", "analyze", "diagnosis", "diagnose", "blind_spot", "desync",
    "advice", "decision_support", "tz", "task_spec",
}

# Интенты прямого поиска факта — МОГУТ абстенить, если ничего не найдено.
LOOKUP_INTENTS = {
    "fact_lookup", "lookup", "status", "assignment", "who_assigned",
    "date_lookup", "when", "definition", "find",
}

# Честный текст, если caller РЕШИТ абстенить (только fast-lookup).
DEFAULT_ABSTAIN_MESSAGE = (
    "По этому вопросу в памяти пока нет данных. Уточните запрос или добавьте "
    "встречу/документ — и я отвечу по фактам."
)


@dataclass(frozen=True)
class AbstentionAdvice:
    """Совет (не приказ). mode: 'compile' | 'answer' | 'abstain'."""
    should_abstain: bool
    mode: str
    reason: str
    message: Optional[str] = None   # текст, ТОЛЬКО если should_abstain=True


# Маркеры СИНТЕЗ-вопроса (консервативно: при любом совпадении → НЕ абстенить).
# Намеренно широкий список — лучше лишний раз скомпилировать, чем сказать «нет»
# там, где система могла бы собрать ответ из данных (требование пользователя §8).
import re as _re

_SYNTHESIS_MARKERS = _re.compile(
    r"(кого|кому|нанять|наним|нанимать|стратег|рекоменд|посовет|совет|"
    r"почему|зачем|как нам|как мне|что делать|что нам|стоит ли|надо ли|нужно ли|"
    r"улучшить|оптимизир|проблем|риск|анализ|сравни|предлож|иде[яи]|план|"
    r"планир|развит|вырас|масштаб|нужен|не хватает|где у нас|"
    r"что не так|подскажи|помоги|hire|strateg|recommend|why|"
    r"what should|advise|improve|optimi|analyz)",
    _re.IGNORECASE | _re.UNICODE,
)


def classify_question_intent(text: Optional[str]) -> str:
    """ОЧЕНЬ консервативный классификатор: 'synthesis' если есть хоть один
    синтез-маркер, иначе 'unknown'. НИКОГДА не возвращает 'lookup' (утверждать
    «это точно lookup» рискованно). Смысл: при сомнении → 'unknown' (advisor по
    умолчанию НЕ абстенит без явного fast_lookup); синтез-вопросы защищены."""
    if text and _SYNTHESIS_MARKERS.search(str(text)):
        return "synthesis"
    return "unknown"


def is_synthesis_intent(intent: Optional[str]) -> bool:
    return (intent or "").lower().strip() in SYNTHESIS_INTENTS


def is_lookup_intent(intent: Optional[str]) -> bool:
    return (intent or "").lower().strip() in LOOKUP_INTENTS


def advise(
    *,
    retrieval_count: int,
    max_score: Optional[float] = None,
    intent: Optional[str] = None,
    fast_lookup: bool = False,
    min_score_floor: float = 0.0,
) -> AbstentionAdvice:
    """Посоветовать: абстенить, отвечать или компилировать.

    Args:
        retrieval_count: сколько релевантных результатов вернул ретрив.
        max_score: лучший score (если есть) — для порога релевантности.
        intent: интент запроса (см. SYNTHESIS_INTENTS / LOOKUP_INTENTS).
        fast_lookup: caller явно в самом простом/быстром lookup-режиме.
        min_score_floor: порог, ниже которого ретрив считаем «нерелевантным».

    Returns:
        AbstentionAdvice. ВАЖНО: при любой неоднозначности → should_abstain=False.
    """
    intent_norm = (intent or "").lower().strip()

    # ПРАВИЛО 1 (жёсткое): синтез/консалтинг → НИКОГДА не абстенить.
    # «кого нанять» и подобное компилируется из данных, а не отвечается «нет».
    if is_synthesis_intent(intent_norm):
        return AbstentionAdvice(
            should_abstain=False, mode="compile",
            reason=f"synthesis-интент '{intent_norm}': компилировать из данных, не абстенить",
        )

    has_signal = retrieval_count > 0 and (max_score is None or max_score >= min_score_floor)

    # ПРАВИЛО 2: абстенция уместна ТОЛЬКО в явном fast-lookup ИЛИ lookup-интенте,
    # И только при отсутствии релевантного сигнала.
    is_lookup = fast_lookup or is_lookup_intent(intent_norm)
    if is_lookup and not has_signal:
        return AbstentionAdvice(
            should_abstain=True, mode="abstain",
            reason="fast-lookup и пустой/нерелевантный ретрив — честное «нет в памяти»",
            message=DEFAULT_ABSTAIN_MESSAGE,
        )

    # ПРАВИЛО 3 (дефолт, консервативный): пытаемся ответить/компилировать.
    # Сюда попадают: неизвестный интент; lookup с сигналом; всё спорное.
    if has_signal:
        return AbstentionAdvice(
            should_abstain=False, mode="answer",
            reason="есть релевантный ретрив — отвечать по нему",
        )
    return AbstentionAdvice(
        should_abstain=False, mode="compile",
        reason="неоднозначно/нет сигнала, но НЕ fast-lookup — пытаться компилировать, не абстенить",
    )
