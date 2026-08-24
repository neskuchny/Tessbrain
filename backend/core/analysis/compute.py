# -*- coding: utf-8 -*-
"""COMPUTE фаза — детерминированные расчёты над извлечёнными цифрами (Фаза C).

Для каждого quantitative dimension с `computation`:
  1. собрать числовые значения inputs из extracted_facts
  2. агрегировать per-input (default sum; настраивается benchmark.aggregate)
  3. выполнить safe-calc computation над агрегатами
  4. сравнить с benchmark → benchmark_status (ok/warning/red_flag)
  5. записать в run.computed_metrics[key]

Числа извлекаются из facts: value может быть числом или строкой с числом
("150 млн") → парсим первое число. Единицы (млн/тыс/млрд/%) применяются
как множители если указаны в unit.

Результат пишется в computed_metrics[key] = {value, formula, inputs_used,
benchmark_status, aggregate}. analyze-фаза добавит туда analysis/severity.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from backend.core.analysis.models import AnalysisPlaybook, AnalysisRun
from backend.core.analysis.safe_calc import (
    SafeCalcError,
    evaluate_program,
)

logger = logging.getLogger(__name__)

# Множители единиц измерения (рус + eng).
_UNIT_MULTIPLIERS = {
    "тыс": 1_000, "тысяч": 1_000, "k": 1_000,
    "млн": 1_000_000, "миллион": 1_000_000, "m": 1_000_000, "mln": 1_000_000,
    "млрд": 1_000_000_000, "миллиард": 1_000_000_000, "b": 1_000_000_000, "bln": 1_000_000_000,
}

_NUM_RE = re.compile(r"-?\d[\d\s]*[.,]?\d*")


def _parse_number(value: Any, unit: Optional[str] = None) -> Optional[float]:
    """Извлечь число из value (число или строка типа '150 млн'). Применить unit."""
    num: Optional[float] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
    elif isinstance(value, str):
        m = _NUM_RE.search(value)
        if m:
            raw = m.group(0).replace(" ", "").replace(",", ".")
            try:
                num = float(raw)
            except ValueError:
                num = None
        # Единица может быть в самой строке.
        if unit is None:
            low = value.lower()
            for u in _UNIT_MULTIPLIERS:
                if u in low:
                    unit = u
                    break
    if num is None:
        return None
    # Применяем множитель единицы.
    if unit:
        u = str(unit).lower().strip()
        for key, mult in _UNIT_MULTIPLIERS.items():
            if u.startswith(key):
                num *= mult
                break
    return num


def _aggregate(numbers: list[float], method: str = "sum") -> Optional[float]:
    if not numbers:
        return None
    if method == "avg" or method == "mean":
        return sum(numbers) / len(numbers)
    if method == "max":
        return max(numbers)
    if method == "min":
        return min(numbers)
    if method == "first":
        return numbers[0]
    if method == "last":
        return numbers[-1]
    return sum(numbers)  # default


def _collect_input(extracted_facts: dict[str, Any], input_key: str,
                   aggregate: str) -> Optional[float]:
    """Собрать агрегированное число по input_key из extracted_facts."""
    facts = extracted_facts.get(input_key)
    if not isinstance(facts, list):
        return None
    nums = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        n = _parse_number(f.get("value"), f.get("unit"))
        if n is not None:
            nums.append(n)
    return _aggregate(nums, aggregate)


def _benchmark_status(value: float, benchmark: dict[str, Any]) -> str:
    """Интерпретировать значение vs benchmark.

    benchmark: {direction: higher_worse|higher_better, red_flag, warning}.
    direction default higher_worse (типично для долга/рисков).
    """
    if not benchmark:
        return ""
    direction = benchmark.get("direction", "higher_worse")
    red = benchmark.get("red_flag")
    warn = benchmark.get("warning")

    def _cmp(threshold: Any) -> bool:
        if threshold is None:
            return False
        try:
            t = float(threshold)
        except (TypeError, ValueError):
            return False
        return value >= t if direction == "higher_worse" else value <= t

    if _cmp(red):
        return "red_flag"
    if _cmp(warn):
        return "warning"
    return "ok"


async def run_compute_phase(
    run: AnalysisRun, playbook: AnalysisPlaybook,
) -> dict[str, Any]:
    """Выполнить расчёты по всем quantitative dimensions. Пишет в
    run.computed_metrics. never-raises (плохой computation → пропуск).
    """
    metrics: dict[str, Any] = dict(run.computed_metrics)  # сохраняем прежние

    for d in playbook.dimensions:
        if d.kind != "quantitative":
            continue

        # Опция: произвольный код в изоляции (ANALYSIS_CODE_EXEC=on) — для
        # расчётов, которые не выразить safe-calc формулой (цикл/группировка).
        dcode = getattr(d, "code", None)
        if dcode:
            try:
                from backend.core.analysis.code_runner import is_enabled, run_code
                if is_enabled():
                    res = run_code(dcode, {"facts": run.extracted_facts})
                    if res.get("ok"):
                        metrics[d.key] = {"value": res.get("result"),
                                          "formula": "code", "benchmark_status": ""}
                        run.add_step("computing", "code executed", {"dimension": d.key})
                        continue
                    run.add_step("computing", "code failed (skip)", {
                        "dimension": d.key, "error": str(res.get("error"))[:160]})
                    continue
                else:
                    run.add_step("computing", "code skipped (ANALYSIS_CODE_EXEC off)", {
                        "dimension": d.key})
            except Exception as e:
                run.add_step("computing", "code runner error (skip)", {
                    "dimension": d.key, "error": str(e)[:160]})
                continue

        if not d.computation:
            continue
        aggregate = str(d.benchmark.get("aggregate", "sum")) if d.benchmark else "sum"

        # Namespace: каждый input → агрегированное число. input может быть
        # ключом другого dimension или собственным ключом.
        ns: dict[str, float] = {}
        missing = []
        for inp in (d.inputs or [d.key]):
            v = _collect_input(run.extracted_facts, inp, aggregate)
            if v is None:
                missing.append(inp)
            else:
                ns[inp] = v

        if missing and not ns:
            run.add_step("computing", "skip (no input data)", {
                "dimension": d.key, "missing": missing,
            })
            continue

        try:
            # evaluate_program поддерживает и одну формулу, и многошаговые
            # расчёты с промежуточными переменными (gross=...; margin=...).
            value = evaluate_program(d.computation, ns)
        except SafeCalcError as e:
            run.add_step("computing", "invalid computation (skip)", {
                "dimension": d.key, "error": str(e)[:160],
            })
            continue

        if value is None:
            run.add_step("computing", "computation returned None", {
                "dimension": d.key, "inputs_used": list(ns.keys()), "missing": missing,
            })
            continue

        status = _benchmark_status(value, d.benchmark or {})
        metrics[d.key] = {
            "value": round(value, 4),
            "formula": d.computation,
            "inputs_used": ns,
            "benchmark_status": status,
            "aggregate": aggregate,
        }
        run.add_step("computing", "metric computed", {
            "dimension": d.key, "value": round(value, 4), "status": status,
        })

    run.computed_metrics = metrics
    run.add_step("computing", "compute complete", {
        "metrics_count": len([m for m in metrics.values() if "value" in m]),
    })
    return metrics
