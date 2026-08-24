"""Тесты для Analysis Engine Фаза C — safe_calc + COMPUTE + ANALYZE."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.analysis import analyze as analyze_mod
from backend.core.analysis import compute as compute_mod
from backend.core.analysis.models import AnalysisDimension, AnalysisPlaybook, AnalysisRun
from backend.core.analysis.safe_calc import SafeCalcError, evaluate


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# safe_calc — security (критично) + correctness
# =============================================================================


def test_safe_calc_arithmetic():
    assert evaluate("debt / equity", {"debt": 150, "equity": 100}) == 1.5
    assert evaluate("(rev - cost) / rev", {"rev": 1000, "cost": 600}) == 0.4
    assert evaluate("a + b * c", {"a": 1, "b": 2, "c": 3}) == 7.0
    assert evaluate("2 ** 10", {}) == 1024.0


def test_safe_calc_functions():
    assert evaluate("avg(a, b, c)", {"a": 10, "b": 20, "c": 30}) == 20.0
    assert evaluate("max(a, b)", {"a": 5, "b": 9}) == 9.0
    assert evaluate("abs(x)", {"x": -7}) == 7.0
    assert evaluate("round(x, 1)", {"x": 3.14159}) == 3.1


def test_safe_calc_div_zero_returns_none():
    assert evaluate("x / y", {"x": 1, "y": 0}) is None


def test_safe_calc_missing_var_returns_none():
    assert evaluate("a + b", {"a": 1}) is None


def test_safe_calc_empty_expression():
    assert evaluate("", {}) is None
    assert evaluate("   ", {}) is None


@pytest.mark.parametrize("attack", [
    '__import__("os").system("ls")',
    'open("/etc/passwd").read()',
    'globals()',
    '(1).__class__.__bases__',
    'eval("1+1")',
    'exec("x=1")',
    '[x for x in range(10)]',
    '{"a": 1}',
    'lambda: 1',
    'x.attr',
    'a if b else c',
    'print("hi")',
])
def test_safe_calc_blocks_attacks(attack):
    """SECURITY: любая не-арифметическая конструкция → SafeCalcError."""
    with pytest.raises(SafeCalcError):
        evaluate(attack, {"a": 1, "b": 2, "c": 3, "x": 1})


def test_safe_calc_too_long():
    with pytest.raises(SafeCalcError):
        evaluate("1+" * 300 + "1", {})


# =============================================================================
# COMPUTE — number parsing + aggregation + benchmark
# =============================================================================


def test_parse_number_plain():
    assert compute_mod._parse_number(150) == 150.0
    assert compute_mod._parse_number(150.5) == 150.5


def test_parse_number_from_string_with_unit():
    assert compute_mod._parse_number("150 млн") == 150_000_000.0
    assert compute_mod._parse_number("2,5 млрд") == 2_500_000_000.0
    assert compute_mod._parse_number("100", "тыс") == 100_000.0


def test_parse_number_garbage():
    assert compute_mod._parse_number("no number here") is None
    assert compute_mod._parse_number(None) is None


def test_aggregate_methods():
    assert compute_mod._aggregate([1, 2, 3], "sum") == 6
    assert compute_mod._aggregate([1, 2, 3], "avg") == 2
    assert compute_mod._aggregate([1, 5], "max") == 5
    assert compute_mod._aggregate([1, 5], "min") == 1
    assert compute_mod._aggregate([], "sum") is None


def test_benchmark_status_higher_worse():
    bm = {"direction": "higher_worse", "red_flag": 2.0, "warning": 1.5}
    assert compute_mod._benchmark_status(2.5, bm) == "red_flag"
    assert compute_mod._benchmark_status(1.6, bm) == "warning"
    assert compute_mod._benchmark_status(1.0, bm) == "ok"


def test_benchmark_status_higher_better():
    bm = {"direction": "higher_better", "red_flag": 0.05, "warning": 0.10}
    # margin 3% → ниже red_flag threshold → red_flag
    assert compute_mod._benchmark_status(0.03, bm) == "red_flag"
    assert compute_mod._benchmark_status(0.08, bm) == "warning"
    assert compute_mod._benchmark_status(0.20, bm) == "ok"


def _pb_quant():
    return AnalysisPlaybook(
        id="p", name="DD",
        dimensions=[
            AnalysisDimension(
                key="debt_load", label="Долговая нагрузка", kind="quantitative",
                computation="debt / equity", inputs=["debt", "equity"],
                benchmark={"direction": "higher_worse", "red_flag": 2.0, "warning": 1.5},
            ),
        ],
    )


def test_compute_phase_happy():
    pb = _pb_quant()
    run = AnalysisRun(id="r", playbook_id="p")
    run.extracted_facts = {
        "debt": [{"value": "120 млн"}],
        "equity": [{"value": "100 млн"}],
    }
    metrics = _run(compute_mod.run_compute_phase(run, pb))
    assert metrics["debt_load"]["value"] == 1.2  # < warning(1.5) → ok
    assert metrics["debt_load"]["benchmark_status"] == "ok"
    assert metrics["debt_load"]["formula"] == "debt / equity"


def test_compute_phase_red_flag():
    pb = _pb_quant()
    run = AnalysisRun(id="r", playbook_id="p")
    run.extracted_facts = {
        "debt": [{"value": 300}],
        "equity": [{"value": 100}],
    }
    metrics = _run(compute_mod.run_compute_phase(run, pb))
    assert metrics["debt_load"]["value"] == 3.0
    assert metrics["debt_load"]["benchmark_status"] == "red_flag"


def test_compute_phase_missing_input_skips():
    pb = _pb_quant()
    run = AnalysisRun(id="r", playbook_id="p")
    run.extracted_facts = {"debt": [{"value": 150}]}  # нет equity
    metrics = _run(compute_mod.run_compute_phase(run, pb))
    # equity отсутствует → debt/equity не посчитать (KeyError → None) → skip
    assert "debt_load" not in metrics or "value" not in metrics.get("debt_load", {})


def test_compute_phase_aggregates_multiple_facts():
    pb = AnalysisPlaybook(
        id="p", name="x",
        dimensions=[AnalysisDimension(
            key="total_debt", label="Долг", kind="quantitative",
            computation="debt", inputs=["debt"],
            benchmark={"aggregate": "sum"},
        )],
    )
    run = AnalysisRun(id="r", playbook_id="p")
    run.extracted_facts = {"debt": [{"value": 100}, {"value": 50}, {"value": 25}]}
    metrics = _run(compute_mod.run_compute_phase(run, pb))
    assert metrics["total_debt"]["value"] == 175.0


def test_compute_phase_ignores_qualitative():
    pb = AnalysisPlaybook(
        id="p", name="x",
        dimensions=[AnalysisDimension(key="legal", label="Юр", kind="qualitative")],
    )
    run = AnalysisRun(id="r", playbook_id="p")
    metrics = _run(compute_mod.run_compute_phase(run, pb))
    assert "legal" not in metrics


# =============================================================================
# ANALYZE — LLM-вывод (замокан)
# =============================================================================


class _FakeLLM:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def generate_json(self, prompt, system_prompt=None):
        self.calls.append(prompt)
        return self._response


class _FakeDeps:
    def __init__(self, llm):
        self.llm = llm


def test_analyze_phase_adds_verdict_and_severity():
    pb = AnalysisPlaybook(
        id="p", name="DD",
        dimensions=[
            AnalysisDimension(key="debt_load", label="Долг", kind="quantitative"),
            AnalysisDimension(key="legal", label="Юр-риски", kind="qualitative"),
        ],
    )
    run = AnalysisRun(id="r", playbook_id="p")
    run.computed_metrics = {"debt_load": {"value": 3.0, "benchmark_status": "red_flag"}}
    llm = _FakeLLM({
        "debt_load": {"verdict": "Долг втрое выше капитала — высокий риск.",
                      "severity": "red_flag"},
        "legal": {"verdict": "Один активный иск, несущественный.", "severity": "info"},
    })
    metrics = _run(analyze_mod.run_analyze_phase(run, pb, _FakeDeps(llm)))
    assert metrics["debt_load"]["analysis"].startswith("Долг втрое")
    assert metrics["debt_load"]["severity"] == "red_flag"
    assert metrics["debt_load"]["value"] == 3.0  # прежнее поле сохранено
    assert metrics["legal"]["severity"] == "info"


def test_analyze_phase_company_context_in_prompt():
    pb = AnalysisPlaybook(
        id="p", name="DD",
        dimensions=[AnalysisDimension(key="x", label="X")],
        company_context={"custom": "Мы — PE-фонд, толерантность к долгу низкая"},
    )
    run = AnalysisRun(id="r", playbook_id="p", client_context="Сделка $5M")
    llm = _FakeLLM({"x": {"verdict": "ok", "severity": "ok"}})
    _run(analyze_mod.run_analyze_phase(run, pb, _FakeDeps(llm)))
    prompt = llm.calls[0]
    assert "PE-фонд" in prompt
    assert "Сделка $5M" in prompt


def test_analyze_phase_llm_failure_graceful():
    pb = AnalysisPlaybook(id="p", name="x", dimensions=[AnalysisDimension(key="a", label="A")])
    run = AnalysisRun(id="r", playbook_id="p")

    class _BadLLM:
        async def generate_json(self, prompt, system_prompt=None):
            raise RuntimeError("down")

    metrics = _run(analyze_mod.run_analyze_phase(run, pb, _FakeDeps(_BadLLM())))
    # не упали, метрики не обогащены
    assert metrics == run.computed_metrics
    assert any("llm failed" in s["action"] for s in run.steps_log)


def test_analyze_coerce_invalid_severity():
    raw = {"a": {"verdict": "x", "severity": "INVALID"}, "b": "plain string verdict"}
    out = analyze_mod._coerce_analysis(raw)
    assert out["a"]["severity"] == "info"  # invalid → info
    assert out["b"]["severity"] == "info"
    assert out["b"]["verdict"] == "plain string verdict"


# =============================================================================
# safe_calc — multi-step programs (промежуточные переменные)
# =============================================================================


def test_evaluate_program_multistep():
    from backend.core.analysis.safe_calc import evaluate_program
    code = "gross = rev - cost\nmargin = gross / rev\nmargin"
    assert evaluate_program(code, {"rev": 1000, "cost": 600}) == 0.4


def test_evaluate_program_single_formula_still_works():
    from backend.core.analysis.safe_calc import evaluate_program
    assert evaluate_program("debt / equity", {"debt": 150, "equity": 100}) == 1.5


def test_evaluate_program_div_zero():
    from backend.core.analysis.safe_calc import evaluate_program
    assert evaluate_program("x = a / b\nx", {"a": 1, "b": 0}) is None


def test_evaluate_program_chained_vars():
    from backend.core.analysis.safe_calc import evaluate_program
    # runway = cash / (burn / months)
    code = "monthly = burn / months\nrunway = cash / monthly\nrunway"
    assert evaluate_program(code, {"cash": 1200, "burn": 600, "months": 6}) == 12.0


@pytest.mark.parametrize("attack", [
    "import os",
    "x = __import__('os')\nx",
    "def f(): pass",
    "for i in range(10): pass",
    "while True: pass",
    "class C: pass",
    "x = open('/etc/passwd')\nx",
])
def test_evaluate_program_blocks_attacks(attack):
    from backend.core.analysis.safe_calc import SafeCalcError, evaluate_program
    with pytest.raises(SafeCalcError):
        evaluate_program(attack, {})


def test_evaluate_program_too_many_statements():
    from backend.core.analysis.safe_calc import SafeCalcError, evaluate_program
    code = "\n".join(f"x{i} = {i}" for i in range(40)) + "\nx1"
    with pytest.raises(SafeCalcError):
        evaluate_program(code, {})


def test_compute_phase_with_multistep_formula():
    """COMPUTE использует evaluate_program → многошаговые формулы работают."""
    pb = AnalysisPlaybook(
        id="p", name="fin",
        dimensions=[AnalysisDimension(
            key="margin", label="Маржа", kind="quantitative",
            computation="gross = rev - cost\nmargin = gross / rev\nmargin",
            inputs=["rev", "cost"],
            benchmark={"direction": "higher_better", "warning": 0.10, "red_flag": 0.05},
        )],
    )
    run = AnalysisRun(id="r", playbook_id="p")
    run.extracted_facts = {
        "rev": [{"value": "1000 млн"}],
        "cost": [{"value": "600 млн"}],
    }
    metrics = _run(compute_mod.run_compute_phase(run, pb))
    assert metrics["margin"]["value"] == 0.4
    assert metrics["margin"]["benchmark_status"] == "ok"
