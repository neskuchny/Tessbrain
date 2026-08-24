"""Тесты для Analysis Engine Фаза B — run_engine (INGEST + EXTRACT + assemble).

LLM и document loader замокированы. Проверяем оркестрацию петли:
chunking, batching, map-reduce extract, steps_log, базовый отчёт,
обработку ошибок.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.core.analysis import run_engine
from backend.core.analysis.models import (
    AnalysisDimension,
    AnalysisPlaybook,
    AnalysisRun,
    RunStatus,
)
from backend.core.analysis.run_engine import RunDeps, run_pipeline


def _run(coro):
    return asyncio.run(coro)


# === Fakes =================================================================


def _fake_docs(docs: dict[str, dict]):
    async def _loader(doc_id: str) -> Optional[dict[str, Any]]:
        return docs.get(doc_id)
    return _loader


class _FakeLLM:
    """Возвращает заранее заданные facts per-call (по очереди), или константу."""
    def __init__(self, responses=None, constant=None):
        self._responses = list(responses or [])
        self._constant = constant
        self.calls: list[str] = []

    async def generate_json(self, prompt, system_prompt=None):
        self.calls.append(prompt)
        if self._responses:
            return self._responses.pop(0)
        return self._constant if self._constant is not None else {}


def _deps(loader, llm) -> RunDeps:
    saved: list = []

    async def _save(run):
        saved.append(run.status)
        return run

    d = RunDeps(document_loader=loader, llm=llm, save_run=_save)
    d._saved = saved  # type: ignore
    return d


def _playbook(dims=None) -> AnalysisPlaybook:
    return AnalysisPlaybook(
        id="apb_test", name="DD",
        dimensions=dims or [
            AnalysisDimension(key="debt", label="Долг", kind="quantitative",
                              extraction_hint="кредиты, займы"),
            AnalysisDimension(key="legal", label="Юр-риски", kind="qualitative",
                              extraction_hint="суды, претензии"),
        ],
    )


def _new_run(doc_ids) -> AnalysisRun:
    return AnalysisRun(id="arun_test", playbook_id="apb_test",
                       input_document_ids=doc_ids)


# === chunking / batching ===================================================


def test_batch_chunks_groups_within_limit():
    chunks = ["a" * 5000, "b" * 5000, "c" * 5000, "d" * 5000]
    batches = run_engine._batch_chunks(chunks, batch_chars=12000)
    # 4×5000=20000 при лимите 12000 → 2 батча (по 2 чанка)
    assert len(batches) == 2


def test_batch_chunks_single_when_small():
    batches = run_engine._batch_chunks(["short1", "short2"], batch_chars=12000)
    assert len(batches) == 1


def test_coerce_facts_normalizes():
    raw = {
        "debt": [{"value": 100, "quote": "кредит 100", "unit": "млн"}],
        "legal": [{"value": "суд с X", "quote": "иск"}],
        "junk": "not a list",
        "empty": [],
    }
    out = run_engine._coerce_facts(raw)
    assert "debt" in out and out["debt"][0]["value"] == 100
    assert "legal" in out
    assert "junk" not in out
    assert "empty" not in out


# === INGEST ================================================================


def test_ingest_loads_and_chunks():
    docs = {"d1": {"id": "d1", "title": "Report", "content": "x" * 3000}}
    run = _new_run(["d1"])
    deps = _deps(_fake_docs(docs), _FakeLLM(constant={}))
    chunks = _run(run_engine._phase_ingest(run, deps))
    assert len(chunks) > 0
    assert run.status == RunStatus.PARSING.value
    # provenance tag
    assert chunks[0].startswith("[doc: Report]")
    # steps logged
    assert any(s["action"] == "ingest complete" for s in run.steps_log)


def test_ingest_skips_missing_and_empty():
    docs = {
        "d1": {"id": "d1", "title": "T", "content": "real content here"},
        "d2": {"id": "d2", "title": "Empty", "content": "   "},
    }
    run = _new_run(["d1", "d2", "d3_missing"])
    deps = _deps(_fake_docs(docs), _FakeLLM(constant={}))
    chunks = _run(run_engine._phase_ingest(run, deps))
    # d1 loaded, d2 empty skip, d3 missing skip
    assert any("not found" in s["action"] for s in run.steps_log)
    assert any("empty document" in s["action"] for s in run.steps_log)
    assert len(chunks) > 0


# === EXTRACT (map-reduce) ==================================================


def test_extract_merges_facts_across_batches():
    pb = _playbook()
    run = _new_run(["d1"])
    # Два батча → два LLM-ответа, факты сливаются.
    llm = _FakeLLM(responses=[
        {"debt": [{"value": 100, "quote": "кредит", "unit": "млн"}], "legal": []},
        {"debt": [{"value": 50, "quote": "займ", "unit": "млн"}],
         "legal": [{"value": "иск X", "quote": "судится"}]},
    ])
    deps = _deps(_fake_docs({}), llm)
    # 2 батча по >12000 символов
    chunks = ["a" * 8000, "b" * 8000]
    facts = _run(run_engine._phase_extract(run, pb, chunks, deps))
    assert len(facts["debt"]) == 2  # из обоих батчей
    assert len(facts["legal"]) == 1
    assert run.status == RunStatus.EXTRACTING.value


def test_extract_handles_llm_failure_gracefully():
    pb = _playbook()
    run = _new_run(["d1"])

    class _BadLLM:
        async def generate_json(self, prompt, system_prompt=None):
            raise RuntimeError("llm down")

    deps = _deps(_fake_docs({}), _BadLLM())
    facts = _run(run_engine._phase_extract(run, pb, ["text"], deps))
    assert facts == {}  # ничего не извлекли, но не упали
    assert any("batch llm failed" in s["action"] for s in run.steps_log)


def test_extract_no_dimensions():
    pb = AnalysisPlaybook(id="x", name="empty", dimensions=[])
    run = _new_run(["d1"])
    deps = _deps(_fake_docs({}), _FakeLLM(constant={}))
    facts = _run(run_engine._phase_extract(run, pb, ["text"], deps))
    assert facts == {}


# === Full pipeline =========================================================


def test_run_pipeline_happy_path():
    docs = {"d1": {"id": "d1", "title": "AnnualReport",
                   "content": "Компания X. Кредиты 150 млн. Судебный иск от Y."}}
    pb = _playbook()
    run = _new_run(["d1"])
    llm = _FakeLLM(constant={
        "debt": [{"value": 150, "quote": "Кредиты 150 млн", "unit": "млн"}],
        "legal": [{"value": "иск от Y", "quote": "Судебный иск от Y"}],
    })
    deps = _deps(_fake_docs(docs), llm)

    result = _run(run_pipeline(run, pb, deps))
    assert result.status == RunStatus.DONE.value
    assert result.final_report is not None
    md = result.final_report["markdown"]
    assert "Аналитический разбор: DD" in md
    assert "Долг" in md and "150" in md
    assert "Юр-риски" in md
    # extracted_facts заполнены
    assert "debt" in result.extracted_facts
    # steps_log покрывает все фазы
    phases = {s["phase"] for s in result.steps_log}
    assert "parsing" in phases
    assert "extracting" in phases
    assert "assembling" in phases


def test_run_pipeline_no_content_fails():
    docs = {"d1": {"id": "d1", "title": "T", "content": ""}}
    pb = _playbook()
    run = _new_run(["d1"])
    deps = _deps(_fake_docs(docs), _FakeLLM(constant={}))
    result = _run(run_pipeline(run, pb, deps))
    assert result.status == RunStatus.FAILED.value
    assert "no content" in result.error


def test_run_pipeline_includes_client_context_in_report():
    docs = {"d1": {"id": "d1", "title": "T", "content": "data data data"}}
    pb = _playbook()
    run = _new_run(["d1"])
    run.client_context = "Сделка на $5M, проверяем риски"
    deps = _deps(_fake_docs(docs), _FakeLLM(constant={}))
    result = _run(run_pipeline(run, pb, deps))
    assert "Сделка на $5M" in result.final_report["markdown"]


def test_run_pipeline_dimension_without_facts_shows_placeholder():
    docs = {"d1": {"id": "d1", "title": "T", "content": "nothing relevant"}}
    pb = _playbook()
    run = _new_run(["d1"])
    deps = _deps(_fake_docs(docs), _FakeLLM(constant={}))  # пустые facts
    result = _run(run_pipeline(run, pb, deps))
    md = result.final_report["markdown"]
    assert "данные не найдены" in md


def test_run_pipeline_saves_after_each_phase():
    docs = {"d1": {"id": "d1", "title": "T", "content": "content here"}}
    pb = _playbook()
    run = _new_run(["d1"])
    deps = _deps(_fake_docs(docs), _FakeLLM(constant={}))
    _run(run_pipeline(run, pb, deps))
    # save_run вызывался многократно (после ingest, extract, assemble done)
    assert len(deps._saved) >= 3
    assert deps._saved[-1] == RunStatus.DONE.value


# === Background dispatch ====================================================


def test_dispatch_run_background_asyncio_fallback(monkeypatch):
    """Без taskiq → asyncio.create_task fallback, возвращает 'asyncio'."""
    import asyncio as _asyncio

    from backend.core.analysis import run_engine as re

    called = {"run_id": None}

    async def _fake_dispatch(run_id, **kw):
        called["run_id"] = run_id

    monkeypatch.setattr(re, "dispatch_run", _fake_dispatch)
    # taskiq import упадёт (нет broker в тестах) → fallback на asyncio.

    async def _go():
        channel = await re.dispatch_run_background("arun_bg")
        # Дать фоновому task завершиться.
        await _asyncio.sleep(0.01)
        return channel

    channel = _run(_go())
    assert channel == "asyncio"
    assert called["run_id"] == "arun_bg"


def test_dispatch_background_tracks_task_reference(monkeypatch):
    """REGRESSION: фоновый task держится в _BACKGROUND_TASKS (не GC'ится)."""
    import asyncio as _asyncio

    from backend.core.analysis import run_engine as re

    started = _asyncio.Event() if False else {"done": False}

    async def _slow_dispatch(run_id, **kw):
        await _asyncio.sleep(0.005)
        started["done"] = True

    monkeypatch.setattr(re, "dispatch_run", _slow_dispatch)

    async def _go():
        await re.dispatch_run_background("arun_x")
        # Сразу после старта task должен быть в наборе.
        assert len(re._BACKGROUND_TASKS) >= 1
        await _asyncio.sleep(0.02)
        return started["done"]

    done = _run(_go())
    assert done is True
