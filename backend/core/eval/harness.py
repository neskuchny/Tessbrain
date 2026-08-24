# -*- coding: utf-8 -*-
"""Eval-harness качества цикла (P6).

ПРОБЕЛ: цикл «понял → сделал → проверил → передал» работал, но его
КАЧЕСТВО ничем не измерялось — не было ни golden-набора, ни метрик
precision/recall, ни замера онтологической чистоты extraction. Без
этого нельзя осознанно включить глобальный STRICT для extraction
(P6, часть A): не зная violation-rate, рискуешь рубить корректные
данные.

Этот модуль даёт **детерминированный, инъектируемый** замер:

  GoldenCase[input → expected_objects/actions]
        │  extractor(input)  (инъектируем: фейк в тесте, реальный
        │                     LLM/пайплайн в CI/прод-eval)
        ▼
  score: precision/recall/F1 по объектам и действиям
        + ontology violation-rate (через P4 SDK, STRICT) — прямой
          сигнал «можно ли включать глобальный STRICT»
        ▼
  EvalReport.passed(thresholds) — гейт для CI/решения о rollout

Дизайн (как P0–P5): чистый, без I/O, never-raises, всё инъектируемо
→ юнит-тест без LLM/графа/сети.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# P10: ontology.sdk импортируется ЛЕНИВО внутри run_eval (только когда
# check_ontology=True). Иначе `python -m backend.core.eval` тянул бы
# backend.core.store.__init__ (numpy/networkx) — ломая CLI/CI без
# тяжёлых зависимостей.

logger = logging.getLogger(__name__)

# extractor(case_input) -> {"objects": [{"label","props"}],
#                           "actions": [{"kind","target"}]}
ExtractorFn = Callable[[Any], dict]


@dataclass
class GoldenCase:
    """Один эталонный кейс.

    expected_objects: [{"label": str, "key": {props-подмножество}}]
        кейс считается покрытым, если есть actual-объект того же
        label, у которого все props из key совпадают.
    expected_actions: [{"kind": str, "target": str}]
    """
    id: str
    input: Any
    expected_objects: list[dict] = field(default_factory=list)
    expected_actions: list[dict] = field(default_factory=list)


@dataclass
class CaseScore:
    case_id: str
    obj_tp: int = 0
    obj_fp: int = 0
    obj_fn: int = 0
    act_tp: int = 0
    act_fp: int = 0
    act_fn: int = 0
    ontology_violations: int = 0
    objects_seen: int = 0
    error: str = ""

    def _pr(self, tp: int, fp: int, fn: int) -> tuple[float, float, float]:
        p = tp / (tp + fp) if (tp + fp) else 1.0
        r = tp / (tp + fn) if (tp + fn) else 1.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        return round(p, 4), round(r, 4), round(f1, 4)

    @property
    def object_prf(self) -> tuple[float, float, float]:
        return self._pr(self.obj_tp, self.obj_fp, self.obj_fn)

    @property
    def action_prf(self) -> tuple[float, float, float]:
        return self._pr(self.act_tp, self.act_fp, self.act_fn)


@dataclass
class EvalReport:
    cases: int = 0
    case_scores: list[CaseScore] = field(default_factory=list)

    def _agg(self, attr_tp: str, attr_fp: str, attr_fn: str) -> dict:
        tp = sum(getattr(c, attr_tp) for c in self.case_scores)
        fp = sum(getattr(c, attr_fp) for c in self.case_scores)
        fn = sum(getattr(c, attr_fn) for c in self.case_scores)
        p = tp / (tp + fp) if (tp + fp) else 1.0
        r = tp / (tp + fn) if (tp + fn) else 1.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        return {"tp": tp, "fp": fp, "fn": fn,
                "precision": round(p, 4), "recall": round(r, 4),
                "f1": round(f1, 4)}

    @property
    def objects(self) -> dict:
        return self._agg("obj_tp", "obj_fp", "obj_fn")

    @property
    def actions(self) -> dict:
        return self._agg("act_tp", "act_fp", "act_fn")

    @property
    def violation_rate(self) -> float:
        seen = sum(c.objects_seen for c in self.case_scores)
        viol = sum(c.ontology_violations for c in self.case_scores)
        return round(viol / seen, 4) if seen else 0.0

    @property
    def errored_cases(self) -> int:
        return sum(1 for c in self.case_scores if c.error)

    def passed(
        self,
        *,
        min_precision: float = 0.8,
        min_recall: float = 0.8,
        max_violation_rate: float = 0.05,
    ) -> bool:
        o = self.objects
        a = self.actions
        return (
            o["precision"] >= min_precision
            and o["recall"] >= min_recall
            and a["precision"] >= min_precision
            and a["recall"] >= min_recall
            and self.violation_rate <= max_violation_rate
            and self.errored_cases == 0
        )

    def to_dict(self) -> dict:
        return {
            "cases": self.cases,
            "errored_cases": self.errored_cases,
            "objects": self.objects,
            "actions": self.actions,
            "violation_rate": self.violation_rate,
            "per_case": [
                {
                    "case_id": c.case_id,
                    "object_prf": c.object_prf,
                    "action_prf": c.action_prf,
                    "ontology_violations": c.ontology_violations,
                    "error": c.error,
                }
                for c in self.case_scores
            ],
        }


def _key_matches(expected_key: dict, actual_props: dict) -> bool:
    """Все props из expected_key присутствуют и равны (subset-match)."""
    for k, v in (expected_key or {}).items():
        if actual_props.get(k) != v:
            return False
    return True


def _score_objects(expected: list[dict], actual: list[dict]) -> tuple[int, int, int]:
    """TP/FP/FN по объектам. Каждый actual матчится максимум раз."""
    used = [False] * len(actual)
    tp = 0
    for exp in expected:
        elabel = exp.get("label")
        ekey = exp.get("key", {})
        hit = False
        for i, act in enumerate(actual):
            if used[i]:
                continue
            if act.get("label") == elabel and _key_matches(ekey, act.get("props", {})):
                used[i] = True
                hit = True
                break
        tp += 1 if hit else 0
    fn = len(expected) - tp
    fp = sum(1 for u in used if not u)
    return tp, fp, fn


def _score_actions(expected: list[dict], actual: list[dict]) -> tuple[int, int, int]:
    used = [False] * len(actual)
    tp = 0
    for exp in expected:
        hit = False
        for i, act in enumerate(actual):
            if used[i]:
                continue
            if (act.get("kind") == exp.get("kind")
                    and act.get("target") == exp.get("target")):
                used[i] = True
                hit = True
                break
        tp += 1 if hit else 0
    fn = len(expected) - tp
    fp = sum(1 for u in used if not u)
    return tp, fp, fn


def run_eval(
    cases: list[GoldenCase],
    *,
    extractor: ExtractorFn,
    check_ontology: bool = True,
) -> EvalReport:
    """Прогнать golden-набор. Никогда не raises.

    extractor инъектируем: `(case.input) -> {"objects":[...],
    "actions":[...]}`. Падение extractor → кейс помечается error,
    метрики кейса нулевые (FN по всем expected) — отражается в
    `errored_cases` и роняет `passed()`.
    """
    report = EvalReport()
    if not isinstance(cases, list):
        return report

    for case in cases:
        cs = CaseScore(case_id=getattr(case, "id", "?"))
        try:
            out = extractor(case.input)
            if not isinstance(out, dict):
                out = {}
            actual_objs = out.get("objects") or []
            actual_acts = out.get("actions") or []
            if not isinstance(actual_objs, list):
                actual_objs = []
            if not isinstance(actual_acts, list):
                actual_acts = []

            tp, fp, fn = _score_objects(case.expected_objects, actual_objs)
            cs.obj_tp, cs.obj_fp, cs.obj_fn = tp, fp, fn
            atp, afp, afn = _score_actions(case.expected_actions, actual_acts)
            cs.act_tp, cs.act_fp, cs.act_fn = atp, afp, afn

            cs.objects_seen = len(actual_objs)
            if check_ontology:
                from backend.core.ontology.sdk import (
                    EnforcementMode,
                    validate_object,
                )

                for o in actual_objs:
                    r = validate_object(
                        o.get("label", ""), o.get("props", {}) or {},
                        mode=EnforcementMode.STRICT,
                    )
                    if not r.ok:
                        cs.ontology_violations += 1
        except Exception as exc:
            logger.warning("eval: case '%s' extractor failed: %s",
                            cs.case_id, exc)
            cs.error = str(exc) or "extractor failed"
            cs.obj_fn = len(getattr(case, "expected_objects", []) or [])
            cs.act_fn = len(getattr(case, "expected_actions", []) or [])

        report.case_scores.append(cs)

    report.cases = len(report.case_scores)
    return report


def load_golden(source: Any) -> list[GoldenCase]:
    """Загрузить golden-набор из list[dict] или JSON-строки. Не raises."""
    try:
        data = json.loads(source) if isinstance(source, str) else source
        if not isinstance(data, list):
            return []
        out: list[GoldenCase] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            out.append(GoldenCase(
                id=str(d.get("id", "?")),
                input=d.get("input"),
                expected_objects=d.get("expected_objects") or [],
                expected_actions=d.get("expected_actions") or [],
            ))
        return out
    except Exception as exc:
        logger.warning("eval: load_golden failed: %s", exc)
        return []


__all__ = [
    "GoldenCase",
    "CaseScore",
    "EvalReport",
    "run_eval",
    "load_golden",
    "ExtractorFn",
]
