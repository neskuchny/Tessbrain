"""Тесты для Analysis Engine Фаза D — ASSEMBLE (template + few-shot)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.analysis import assemble as asm
from backend.core.analysis.models import AnalysisDimension, AnalysisPlaybook, AnalysisRun


def _run(coro):
    return asyncio.run(coro)


class _FakeLLM:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def generate_json(self, prompt, system_prompt=None):
        self.calls.append(prompt)
        return self._response


class _Deps:
    def __init__(self, llm):
        self.llm = llm


def _run_with_data(severities=None):
    pb = AnalysisPlaybook(
        id="p", name="DD-аудит",
        dimensions=[
            AnalysisDimension(key="debt", label="Долговая нагрузка", kind="quantitative",
                              rationale="риск дефолта"),
            AnalysisDimension(key="legal", label="Юр-риски", kind="qualitative"),
        ],
    )
    run = AnalysisRun(id="r", playbook_id="p", input_document_ids=["d1"])
    sev = severities or {"debt": "red_flag", "legal": "info"}
    run.computed_metrics = {
        "debt": {"value": 3.0, "benchmark_status": "red_flag", "formula": "debt/equity",
                 "analysis": "Долг втрое выше капитала", "severity": sev["debt"]},
        "legal": {"analysis": "Один иск", "severity": sev["legal"]},
    }
    run.extracted_facts = {
        "debt": [{"value": 300, "quote": "кредит 300"}],
        "legal": [{"value": "иск", "quote": "судится с X"}],
    }
    return pb, run


# === Severity summary + verdict ============================================


def test_severity_summary_overall_red_flag():
    pb, run = _run_with_data({"debt": "red_flag", "legal": "info"})
    s = asm._severity_summary(run, pb)
    assert s["overall"] == "red_flag"
    assert s["counts"]["red_flag"] == 1


def test_severity_summary_overall_ok():
    pb, run = _run_with_data({"debt": "ok", "legal": "ok"})
    s = asm._severity_summary(run, pb)
    assert s["overall"] == "ok"


def test_verdict_text():
    assert "ТРЕБУЕТ ВНИМАНИЯ" in asm._verdict_text("red_flag", {"red_flag": 2})
    assert "ОГОВОРКАМИ" in asm._verdict_text("warning", {"warning": 1})
    assert "НОРМЕ" in asm._verdict_text("ok", {})


# === Deterministic mode (нет template/reference) ===========================


def test_assemble_deterministic_no_template():
    pb, run = _run_with_data()
    # Нет llm-вызова т.к. нет template/reference.
    report = _run(asm.assemble_report(run, pb, _Deps(_FakeLLM({}))))
    assert report["generated_by"] == "deterministic"
    md = report["markdown"]
    assert "DD-аудит" in md
    assert "Долговая нагрузка" in md
    assert "🔴" in md  # red_flag icon
    assert "ТРЕБУЕТ ВНИМАНИЯ" in report["verdict"]
    assert "Долг втрое" in md  # analysis
    assert "кредит 300" in md  # quote


def test_assemble_deterministic_sections():
    pb, run = _run_with_data()
    report = _run(asm.assemble_report(run, pb, _Deps(_FakeLLM({}))))
    keys = {s["key"] for s in report["sections"]}
    assert keys == {"debt", "legal"}


# === LLM mode (есть template или reference) ================================


def test_assemble_llm_mode_with_template():
    pb, run = _run_with_data()
    pb.output_template = {"blocks": [
        {"name": "summary", "label": "Резюме", "description": "краткий итог"},
        {"name": "risks", "label": "Риски", "description": "выявленные риски"},
    ]}
    llm = _FakeLLM({"markdown": "# Отчёт\n## Резюме\nВсё ок\n## Риски\nДолг высокий",
                    "verdict": "С осторожностью"})
    report = _run(asm.assemble_report(run, pb, _Deps(llm)))
    assert report["generated_by"] == "llm"
    assert "Отчёт" in report["markdown"]
    assert report["verdict"] == "С осторожностью"
    # template-блоки в промпте
    assert "Резюме" in llm.calls[0]
    assert "Риски" in llm.calls[0]


def test_assemble_llm_mode_with_reference_fewshot():
    pb, run = _run_with_data()
    pb.reference_report = "# Идеальный отчёт\nСтруктура: вердикт, метрики, риски, рекомендация"
    llm = _FakeLLM({"markdown": "# Мой отчёт\n...", "verdict": "ok"})
    report = _run(asm.assemble_report(run, pb, _Deps(llm)))
    # reference попал в промпт как few-shot
    assert "Идеальный отчёт" in llm.calls[0]
    assert "ПРИМЕР ЖЕЛАЕМОГО ФОРМАТА" in llm.calls[0]
    assert report["generated_by"] == "llm"


def test_assemble_llm_failure_falls_back_to_deterministic():
    pb, run = _run_with_data()
    pb.reference_report = "example"

    class _BadLLM:
        async def generate_json(self, prompt, system_prompt=None):
            raise RuntimeError("llm down")

    report = _run(asm.assemble_report(run, pb, _Deps(_BadLLM())))
    assert report["generated_by"] == "deterministic"
    assert any("llm failed" in s["action"] for s in run.steps_log)


def test_assemble_llm_bad_output_falls_back():
    pb, run = _run_with_data()
    pb.reference_report = "example"
    # LLM вернул без markdown
    llm = _FakeLLM({"verdict": "x"})
    report = _run(asm.assemble_report(run, pb, _Deps(llm)))
    assert report["generated_by"] == "deterministic"


def test_assemble_client_context_in_llm_prompt():
    pb, run = _run_with_data()
    pb.reference_report = "ex"
    run.client_context = "Сделка $10M"
    llm = _FakeLLM({"markdown": "# r", "verdict": "v"})
    _run(asm.assemble_report(run, pb, _Deps(llm)))
    assert "Сделка $10M" in llm.calls[0]


# === Integration: full pipeline через run_engine с template =================


def test_full_pipeline_uses_assemble_phase():
    """run_pipeline с playbook-template → отчёт generated_by=llm."""
    from backend.core.analysis.run_engine import RunDeps, run_pipeline

    pb = AnalysisPlaybook(
        id="p", name="DD",
        dimensions=[AnalysisDimension(key="debt", label="Долг", kind="quantitative",
                                      computation="debt", inputs=["debt"])],
        output_template={"blocks": [{"name": "s", "label": "Сводка", "description": "итог"}]},
    )
    docs = {"d1": {"id": "d1", "title": "T", "content": "Кредиты 200 млн"}}

    # LLM: extract → потом analyze → потом assemble. Один fake отвечает
    # по-разному в зависимости от наличия ключевых слов промпта.
    class _SmartLLM:
        async def generate_json(self, prompt, system_prompt=None):
            if "ИТОГОВЫЙ ОТЧЁТ" in prompt:
                return {"markdown": "# Итоговый отчёт DD", "verdict": "готово"}
            if "вывод по каждому параметру" in prompt or ("verdict" in prompt and "severity" in prompt):
                return {"debt": {"verdict": "долг ок", "severity": "ok"}}
            # extract
            return {"debt": [{"value": 200, "quote": "Кредиты 200 млн", "unit": "млн"}]}

    async def _loader(doc_id):
        return docs.get(doc_id)

    saved = []

    async def _save(r):
        saved.append(r.status)

    deps = RunDeps(document_loader=_loader, llm=_SmartLLM(), save_run=_save)
    run = AnalysisRun(id="r", playbook_id="p", input_document_ids=["d1"])
    result = _run(run_pipeline(run, pb, deps))

    assert result.status == "done"
    assert result.final_report["generated_by"] == "llm"
    assert "Итоговый отчёт DD" in result.final_report["markdown"]
    # compute сработал: debt=200млн извлечён и посчитан
    assert "debt" in result.computed_metrics
