# -*- coding: utf-8 -*-
"""
Company Hypotheses — Gate-0 ядро (ТЗ_AGI/TESSENT_HYPOTHESES_SPEC §1-3,8).

КОНТЕКСТ И ГРАНИЦА. Полный «дифференциальный диагноз компании» (конкурирующие
гипотезы + ev_info + predict-before-act + Neo4j + Qwen-оркестрация) — ДОРОГАЯ
стройка. Док ставит жёсткий Gate-0: не строить её, пока не ИЗМЕРЕНО на реальных
транскриптах, что скелет даёт вывод, которого нет у чистого LLM.

Поэтому здесь — только ИЗМЕРИМОЕ ЯДРО-КАК-КОД (то, что в §8 шаг 1 называется
первым гейтом: «режет ли admission-gate пустые нарративы?»):
  - CompanyHypothesis (структура §1);
  - admit_hypothesis — шлюз допуска §2 (анти-нарративный: гипотеза принимается,
    только если делает проверяемое РАЗЛИЧАЮЩЕЕ предсказание);
  - most_distinguishing_dimension — ev_info §3 (куда направить внимание).

Всё — чистые функции, БЕЗ LLM/БД/Neo4j. НЕ подключено в query() — это и есть
дисциплина Gate-0: сперва измерить ядро на примерах/транскриптах, и только если
оно режет нарративы и находит различающее измерение лучше одного LLM — строить
остальное. (См. INVENTORY_HONEST §5: ev_info помогает на динамике, не на фактах.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Расплывчатые значения целиком (нарратив, не инвариант).
_VAGUE = {
    "", "?", "—", "-", "n/a", "na", "tbd", "возможно", "может быть", "что-то",
    "неясно", "не ясно", "зависит", "разное", "по-разному", "unclear", "maybe",
    "depends", "various", "it depends", "хз", "непонятно",
}
# Расплывчатые ТОКЕНЫ: значение, состоящее ТОЛЬКО из них, непроверяемо
# («всё сложно», «как-то так»).
_VAGUE_TOKENS = {
    "всё", "все", "сложно", "сложна", "трудно", "непросто", "так", "как-то",
    "что-то", "наверное", "возможно", "зависит", "разное", "по", "разному",
    "unclear", "maybe", "depends", "various", "somehow", "stuff",
}


def _norm(value) -> str:
    return str(value).strip().lower() if value is not None else ""


def is_checkable(value) -> bool:
    """Детерминированный ПОЛ проверяемости предсказания: режет пустое/короткое/
    явно-расплывчатое и значения из одних расплывчатых токенов («всё сложно»).

    ГРАНИЦА (честно, §4): это floor, не полный шлюз. Фразу-нарратив с одним
    конкретным словом («всё из-за коммуникации») floor НЕ ловит — в спеке §2
    это делает LLM-проверка `is_checkable_against_graph` (фальсифицируемо ли
    против графа событий). Здесь — дешёвый пред-фильтр перед дорогой LLM-частью.
    """
    s = _norm(value)
    if not s or s in _VAGUE:  # пусто/явно-расплывчато (однобуквенное «B» — ок)
        return False
    words = re.findall(r"[^\W\d_]+", s, re.UNICODE)
    if words and all(w in _VAGUE_TOKENS for w in words):
        return False
    return True


@dataclass
class CompanyHypothesis:
    """Проверяемое объяснение паттерна с РАЗЛИЧАЮЩИМИ предсказаниями (§1)."""
    id: str
    claim: str
    mechanism: str = ""
    predicted_observables: dict = field(default_factory=dict)  # {измерение: значение}
    evidence_for: list = field(default_factory=list)
    evidence_against: list = field(default_factory=list)
    belief: float = 0.5
    status: str = "pending"  # pending|admitted|rejected_narrative|merged|ungrounded_entity
    entities: list = field(default_factory=list)  # сущности компании, на которые ссылается гипотеза


def _is_duplicate(a: CompanyHypothesis, b: CompanyHypothesis) -> bool:
    """Дубль: делят ≥1 измерение и СОВПАДАЮТ по всем общим (ничем не различаются)."""
    shared = set(a.predicted_observables) & set(b.predicted_observables)
    if not shared:
        return False
    return all(_norm(a.predicted_observables[d]) == _norm(b.predicted_observables[d]) for d in shared)


def admit_hypothesis(h: CompanyHypothesis, others: list) -> bool:
    """Шлюз допуска §2 (главный анти-нарративный механизм). Принимаем ТОЛЬКО:
      1) если есть хоть одно ПРОВЕРЯЕМОЕ предсказание (иначе rejected_narrative);
      2) если гипотеза РАЗЛИЧАЕТСЯ с уже принятыми (иначе merged — дубль).
    Мутирует h.status; возвращает True при допуске."""
    if not any(is_checkable(v) for v in h.predicted_observables.values()):
        h.status = "rejected_narrative"
        return False
    if any(_is_duplicate(h, o) for o in (others or [])):
        h.status = "merged"
        return False
    h.status = "admitted"
    return True


def _disagreement(values: list) -> int:
    """Разброс по измерению = число РАЗЛИЧНЫХ конкретных значений (энтропия-lite)."""
    distinct = {_norm(v) for v in values if is_checkable(v)}
    return len(distinct)


def most_distinguishing_dimension(hyps: list) -> Optional[str]:
    """ev_info §3: измерение, где гипотезы РАСХОДЯТСЯ сильнее всего → туда направить
    следующий анализ/внимание. None, если нет различающих измерений."""
    if not hyps:
        return None
    dims: set = set()
    for h in hyps:
        dims |= set(h.predicted_observables.keys())
    best = None
    best_d = 1  # нужен реальный разброс (≥2 различных значения)
    for dim in sorted(dims):
        vals = [h.predicted_observables.get(dim) for h in hyps if dim in h.predicted_observables]
        d = _disagreement(vals)
        if d > best_d:
            best_d = d
            best = dim
    return best


def ungrounded_entities(hyp: CompanyHypothesis, known: set) -> list:
    """Claim-guard для гипотез: сущности, на которые ССЫЛАЕТСЯ гипотеза, но
    которых в компании НЕТ. Это ровно «прорекламировали то, чего у нас не
    существует» — причина/план, опирающийся на несуществующее, отвергается.
    known — нормализованные имена всех известных сущностей (из графа/prior).
    Пусто known → нечего проверять (не over-reject)."""
    if not known:
        return []
    return [e for e in (hyp.entities or []) if e and _norm(e) not in known]


def ground_cause_analysis(hypotheses: list, known: Optional[set] = None) -> dict:
    """Заземлённый LLM-анализ причины (desync/blind-spot) ПОД дисциплиной скелета:
    admission-gate (§2: режет нарративы без проверяемых различающих предсказаний)
    + claim-guard (отвергает гипотезы, ссылающиеся на несуществующие сущности).

    Возвращает:
      admitted   — гипотезы, прошедшие ОБА фильтра (с проставленным status);
      rejected   — нарративы/дубли (admission-gate);
      ungrounded — гипотезы с несуществующими сущностями (claim-guard);
      distinguishing_dimension — ev_info по принятым (куда смотреть для подтверждения).
    """
    known = known or set()
    admitted: list = []
    rejected: list = []
    ungrounded: list = []
    for h in (hypotheses or []):
        if not admit_hypothesis(h, admitted):       # narrative / merged
            rejected.append(h)
            continue
        bad = ungrounded_entities(h, known)
        if bad:                                      # ссылается на несуществующее
            h.status = "ungrounded_entity"
            ungrounded.append({"hypothesis": h, "missing": bad})
            continue
        admitted.append(h)
    return {
        "admitted": admitted,
        "rejected": rejected,
        "ungrounded": ungrounded,
        "distinguishing_dimension": most_distinguishing_dimension(admitted),
    }
