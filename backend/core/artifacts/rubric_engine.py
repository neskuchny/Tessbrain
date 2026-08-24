# -*- coding: utf-8 -*-
"""
Rubric Engine — оценка результата против контекста КОМПАНИИ
(docs/CAPABILITY_READINESS.md, универсальная операция №5).

ЗАЧЕМ. «Задача выполнена не так, потому что у нас такие принципы», «агентство
сделало сайт — насколько он подходит НАШЕЙ компании» — один класс: оценка
артефакта по рубрике, построенной из контекста компании. Существующий
validator (backend/core/validator) судит «соответствие ТЗ» generic-судьёй;
здесь добавляется недостающее — РУБРИКА ИЗ КОНТЕКСТА и решение в КОДЕ.

РАЗДЕЛЕНИЕ (скелет+LLM):
- СКЕЛЕТ (код): критерии из схемы артефакта — детерминированно; агрегация
  вердиктов, взвешенный счёт и решение (approve/review/reject) — в коде;
  непарсящийся ответ судьи → честный 'unknown' и решение 'review' (не
  выдуманный вердикт).
- LLM (инъекция): (а) опционально ИЗВЛЕЧЬ критерии из текста контекста
  компании (досье/снапшот/wisdom) — критерии видимы и редактируемы ДО оценки;
  (б) судья: per-criterion вердикт С ЦИТАТОЙ-доказательством.

Чистый stdlib + инъекция LLM (мок в тестах).
"""
from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Criterion:
    """Один проверяемый критерий. source — откуда взялся (прослеживаемость)."""
    key: str
    statement: str              # что должно быть верно для «хорошо»
    source: str = "custom"      # 'schema' | 'company_context' | 'request' | 'custom'
    weight: float = 1.0


@dataclass(frozen=True)
class Rubric:
    name: str
    criteria: Tuple[Criterion, ...]

    def to_text(self) -> str:
        return "\n".join(f"{i}. [{c.key}] {c.statement} (вес {c.weight}, из: {c.source})"
                         for i, c in enumerate(self.criteria, 1))


@dataclass(frozen=True)
class CriterionVerdict:
    key: str
    verdict: str                # 'pass' | 'partial' | 'fail' | 'unknown'
    evidence: str = ""          # цитата из артефакта
    comment: str = ""


@dataclass(frozen=True)
class Evaluation:
    rubric_name: str
    verdicts: Tuple[CriterionVerdict, ...]
    score: float                # взвешенный, в КОДЕ
    decision: str               # 'approve' | 'review' | 'reject'
    generated_at: str = ""

    def failed(self) -> List[CriterionVerdict]:
        return [v for v in self.verdicts if v.verdict == "fail"]

    def to_text(self) -> str:
        lines = [f"ОЦЕНКА «{self.rubric_name}»: {self.decision.upper()} "
                 f"(счёт {self.score:.2f})"]
        for v in self.verdicts:
            mark = {"pass": "✅", "partial": "◐", "fail": "❌", "unknown": "❔"}[v.verdict]
            lines.append(f"{mark} [{v.key}] {v.comment or v.verdict}"
                         + (f" — «{v.evidence}»" if v.evidence else ""))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Построение рубрики
# ---------------------------------------------------------------------------

def criteria_from_schema(schema) -> List[Criterion]:
    """Детерминированно: каждая секция схемы артефакта → критерий приёмки."""
    out = []
    for s in schema.sections:
        statement = f"Секция «{s.title}» присутствует и выполняет требование: {s.instruction or s.title}"
        out.append(Criterion(key=f"schema:{s.key}", statement=statement, source="schema"))
    return out


_EXTRACT_SYSTEM = (
    "Ты извлекаешь КРИТЕРИИ ПРИЁМКИ из контекста компании (принципы, позиционирование, "
    "требования, договорённости). Верни ТОЛЬКО JSON-массив объектов "
    '{"key": str, "statement": str} — каждый критерий проверяем по тексту результата. '
    "Только то, что РЕАЛЬНО следует из контекста; не выдумывай. Максимум {k} критериев."
)


def criteria_from_context(context_text: str, llm: Callable[[str, str], str],
                          *, max_criteria: int = 8) -> List[Criterion]:
    """LLM-извлечение критериев из текста контекста компании (досье/снапшот/wisdom).
    Критерии возвращаются КАК ДАННЫЕ — видимы/редактируемы до оценки. Best-effort:
    непарсящийся ответ → пустой список (не выдуманные критерии)."""
    if not (context_text or "").strip():
        return []
    raw = llm(_EXTRACT_SYSTEM.replace("{k}", str(max_criteria)),
              f"КОНТЕКСТ КОМПАНИИ:\n{context_text}\n\nJSON-массив критериев:")
    items = _parse_json_array(raw)
    out = []
    for i, it in enumerate(items[:max_criteria]):
        st = str(it.get("statement", "")).strip()
        if st:
            out.append(Criterion(key=str(it.get("key") or f"ctx{i+1}"),
                                 statement=st, source="company_context"))
    return out


def build_rubric(name: str, *, schema=None, context_criteria: Optional[List[Criterion]] = None,
                 extra: Optional[List[Criterion]] = None) -> Rubric:
    """Собрать рубрику из источников. Детерминированно (критерии уже-данные)."""
    criteria: List[Criterion] = []
    if schema is not None:
        criteria.extend(criteria_from_schema(schema))
    criteria.extend(context_criteria or [])
    criteria.extend(extra or [])
    if not criteria:
        raise ValueError("rubric requires at least one criterion")
    return Rubric(name=name, criteria=tuple(criteria))


# ---------------------------------------------------------------------------
# Оценка
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "Ты — строгий ревьюер результата по рубрике компании. Для КАЖДОГО критерия "
    "верни вердикт по ТЕКСТУ РЕЗУЛЬТАТА: 'pass' (выполнен), 'partial' (частично), "
    "'fail' (не выполнен / противоречит). ОБЯЗАТЕЛЬНО цитируй доказательство из "
    "результата (evidence) или укажи, чего не хватает (comment). Не выдумывай. "
    'Верни ТОЛЬКО JSON-массив объектов {"key": str, "verdict": str, '
    '"evidence": str, "comment": str} — по одному на критерий, в том же порядке.'
)

_SCORE = {"pass": 1.0, "partial": 0.5, "fail": 0.0, "unknown": 0.0}
APPROVE_THRESHOLD = 0.85   # как в backend/core/validator (AUTO_APPROVE ≥ 8.5/10)
REJECT_THRESHOLD = 0.5


def evaluate(artifact_text: str, rubric: Rubric, llm: Callable[[str, str], str],
             *, context: str = "") -> Evaluation:
    """Оценить результат по рубрике. Судья — LLM; счёт и решение — КОД."""
    ctx = f"\n\nКОНТЕКСТ (для понимания требований):\n{context}" if context else ""
    raw = llm(_JUDGE_SYSTEM,
              f"РУБРИКА:\n{rubric.to_text()}{ctx}\n\n"
              f"ТЕКСТ РЕЗУЛЬТАТА:\n{artifact_text}\n\nJSON-массив вердиктов:")
    items = _parse_json_array(raw)
    parsed = {str(it.get("key")): it for it in items}
    parsed_ci = {str(it.get("key", "")).strip().casefold(): it for it in items}
    # выравнивание по порядку — fallback, когда судья переименовал ключи,
    # но дал ровно по вердикту на критерий (видели вживую: регистр/нумерация)
    by_order = items if len(items) == len(rubric.criteria) else []
    verdicts = []
    for idx, c in enumerate(rubric.criteria):
        it = (parsed.get(c.key)
              or parsed_ci.get(c.key.strip().casefold())
              or (by_order[idx] if by_order else None))
        if it and str(it.get("verdict", "")).lower() in _SCORE:
            verdicts.append(CriterionVerdict(
                key=c.key, verdict=str(it["verdict"]).lower(),
                evidence=str(it.get("evidence", ""))[:300],
                comment=str(it.get("comment", ""))[:300]))
        else:
            # судья не дал валидного вердикта → ЧЕСТНЫЙ unknown, не выдумка
            verdicts.append(CriterionVerdict(key=c.key, verdict="unknown",
                                             comment="судья не дал валидного вердикта"))
    total_w = sum(c.weight for c in rubric.criteria) or 1.0
    score = round(sum(_SCORE[v.verdict] * c.weight
                      for v, c in zip(verdicts, rubric.criteria, strict=True)) / total_w, 4)
    has_unknown = any(v.verdict == "unknown" for v in verdicts)
    if score >= APPROVE_THRESHOLD and not has_unknown:
        decision = "approve"
    elif score < REJECT_THRESHOLD and not has_unknown:
        decision = "reject"
    else:
        decision = "review"   # неуверенность → человек, не автопилот
    return Evaluation(rubric_name=rubric.name, verdicts=tuple(verdicts),
                      score=score, decision=decision, generated_at=_now_iso())


def _parse_json_array(raw: str) -> List[dict]:
    """Достать JSON-массив из ответа LLM. Устойчиво к: ```-блокам, тексту со
    скобками ДО массива (эхо рубрики «[schema:...]»), обрезанному хвосту
    (восстановление по целым объектам). [] при полной неудаче — честный unknown
    дальше по конвейеру, не выдуманный вердикт."""
    if not raw:
        return []
    # 1) предпочесть fenced-блок, если есть
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    dec = json.JSONDecoder()
    # 2) пробовать raw_decode с каждой '[' — первая валидная побеждает
    for m in re.finditer(r"\[", raw):
        try:
            data, _ = dec.raw_decode(raw[m.start():])
        except Exception:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    # 3) обрезанный массив → собрать целые объекты по одному
    out = []
    i = 0
    while True:
        j = raw.find("{", i)
        if j < 0:
            break
        try:
            obj, end = dec.raw_decode(raw[j:])
            if isinstance(obj, dict):
                out.append(obj)
            i = j + max(end, 1)
        except Exception:
            i = j + 1
    return out
