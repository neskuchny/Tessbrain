"""Unit-тесты P10: extractor-адаптеры + eval-гейт раннер.

Чистая логика через importlib со stub-пакетами (обходим тяжёлый
core.store.__init__). LLM инъектируем — без сети/ключей.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, _ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap():
    for pkg in ("backend", "backend.core", "backend.core.store",
                "backend.core.ontology", "backend.core.eval"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    _load("backend.core.store.schema", "backend/core/store/schema.py")
    _load("backend.core.ontology.sdk", "backend/core/ontology/sdk.py")
    _load("backend.core.eval.harness", "backend/core/eval/harness.py")
    ex = _load("backend.core.eval.extractors",
               "backend/core/eval/extractors.py")
    rn = _load("backend.core.eval.runner", "backend/core/eval/runner.py")
    return ex, rn


_ex, _rn = _bootstrap()
_h = sys.modules["backend.core.eval.harness"]
GoldenCase = _h.GoldenCase

build_extraction_prompt = _ex.build_extraction_prompt
parse_extraction = _ex.parse_extraction
llm_extract_all = _ex.llm_extract_all
make_input_extractor = _ex.make_input_extractor

run_gate = _rn.run_gate
run_llm_gate = _rn.run_llm_gate
decide_exit = _rn.decide_exit
format_summary = _rn.format_summary
EXIT_OK = _rn.EXIT_OK
EXIT_GATE_FAILED = _rn.EXIT_GATE_FAILED
EXIT_ERROR = _rn.EXIT_ERROR


def _run(c):
    return asyncio.run(c)


def _case():
    return GoldenCase(
        id="c1", input="Иван взял задачу",
        expected_objects=[{"label": "Person", "key": {"name": "Иван"}}],
        expected_actions=[{"kind": "create_node", "target": "Person"}],
    )


# === build / parse =====================================================

def test_prompt_is_deterministic_and_contains_body() -> None:
    p1 = build_extraction_prompt("текст A")
    p2 = build_extraction_prompt("текст A")
    assert p1 == p2 and "текст A" in p1


def test_prompt_handles_non_str_input() -> None:
    p = build_extraction_prompt({"x": 1})
    assert '"x": 1' in p or "'x': 1" in p


def test_parse_plain_dict() -> None:
    r = parse_extraction({"objects": [{"label": "Person"}], "actions": []})
    assert r["objects"] and r["actions"] == []


def test_parse_json_string() -> None:
    r = parse_extraction('{"objects":[],"actions":[{"kind":"x"}]}')
    assert r["actions"][0]["kind"] == "x"


def test_parse_fenced_json() -> None:
    r = parse_extraction('```json\n{"objects":[{"label":"P"}],"actions":[]}\n```')
    assert r["objects"][0]["label"] == "P"


def test_parse_garbage_is_empty_not_raise() -> None:
    assert parse_extraction("not json") == {"objects": [], "actions": []}
    assert parse_extraction(42) == {"objects": [], "actions": []}
    assert parse_extraction('{"objects":"bad"}')["objects"] == []


# === llm_extract_all (инъектируемый async) =============================

def test_llm_extract_all_keys_by_input() -> None:
    async def llm(prompt):
        return {"objects": [{"label": "Person", "props": {"name": "Иван"}}],
                "actions": [{"kind": "create_node", "target": "Person"}]}

    cases = [_case()]
    res = _run(llm_extract_all(cases, llm))
    extractor = make_input_extractor(res)
    got = extractor(cases[0].input)
    assert got["objects"][0]["props"]["name"] == "Иван"


def test_llm_failure_isolated_to_empty() -> None:
    async def boom(prompt):
        raise RuntimeError("LLM 500")

    cases = [_case()]
    res = _run(llm_extract_all(cases, boom))
    extractor = make_input_extractor(res)
    assert extractor(cases[0].input) == {"objects": [], "actions": []}


# === run_gate / decide_exit ===========================================

def test_gate_pass_exit_0() -> None:
    cases = [_case()]

    def perfect(_inp):
        return {"objects": [{"label": "Person", "props": {"name": "Иван"}}],
                "actions": [{"kind": "create_node", "target": "Person"}]}

    report, code = run_gate(cases, extractor=perfect, check_ontology=True)
    assert code == EXIT_OK
    assert report.objects["recall"] == 1.0


def test_gate_fail_exit_1() -> None:
    cases = [_case()]

    def empty(_inp):
        return {"objects": [], "actions": []}

    report, code = run_gate(cases, extractor=empty, check_ontology=True)
    assert code == EXIT_GATE_FAILED


def test_gate_threshold_override() -> None:
    cases = [_case()]

    def half(_inp):
        return {"objects": [{"label": "Person", "props": {"name": "Иван"}}],
                "actions": []}  # recall actions = 0

    # дефолт → fail; ослабим recall → pass
    _r, c1 = run_gate(cases, extractor=half, check_ontology=False)
    assert c1 == EXIT_GATE_FAILED
    _r2, c2 = run_gate(cases, extractor=half, check_ontology=False,
                       thresholds={"min_recall": 0.0})
    assert c2 == EXIT_OK


def test_run_gate_never_raises_on_bad_extractor() -> None:
    def boom(_inp):
        raise RuntimeError("boom")

    # harness изолирует ошибку extractor → errored_cases → gate fail,
    # но НЕ исключение наружу
    report, code = run_gate([_case()], extractor=boom)
    assert code in (EXIT_GATE_FAILED, EXIT_ERROR)


# === run_llm_gate (async) =============================================

def test_run_llm_gate_pass() -> None:
    async def llm(prompt):
        return {"objects": [{"label": "Person", "props": {"name": "Иван"}}],
                "actions": [{"kind": "create_node", "target": "Person"}]}

    report, code = _run(run_llm_gate([_case()], llm_json_call=llm))
    assert code == EXIT_OK


def test_run_llm_gate_fail_on_bad_model() -> None:
    async def llm(prompt):
        return "garbage not json"

    report, code = _run(run_llm_gate([_case()], llm_json_call=llm))
    assert code == EXIT_GATE_FAILED


# === decide_exit / summary ============================================

def test_decide_exit_on_broken_report_is_error() -> None:
    class Bad:
        def passed(self, **_k):
            raise RuntimeError("x")

    assert decide_exit(Bad(), {}) == EXIT_ERROR


def test_format_summary_contains_status() -> None:
    cases = [_case()]
    report, code = run_gate(
        cases,
        extractor=lambda _i: {"objects": [], "actions": []},
        check_ontology=False,
    )
    s = format_summary(report, code)
    assert "Eval gate" in s and "exit=" in s
