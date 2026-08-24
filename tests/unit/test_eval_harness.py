"""Unit-тесты P6: env-gated extraction mode + eval-harness.

Чистая логика через importlib со stub-пакетами (обходим тяжёлый
core.store.__init__). LLM/граф/сеть не нужны — extractor инъектируем.
"""
from __future__ import annotations

import importlib.util
import json
import os
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
    sdk = _load("backend.core.ontology.sdk", "backend/core/ontology/sdk.py")
    harness = _load("backend.core.eval.harness", "backend/core/eval/harness.py")
    schema = sys.modules["backend.core.store.schema"]
    return sdk, harness, schema


_sdk, _h, _schema = _bootstrap()

EnforcementMode = _sdk.EnforcementMode
extraction_mode_from_env = _sdk.extraction_mode_from_env
SchemaValidator = _schema.SchemaValidator
GoldenCase = _h.GoldenCase
run_eval = _h.run_eval
load_golden = _h.load_golden


# === Часть A: env-gated extraction mode =================================

def test_extraction_mode_default_warn(monkeypatch) -> None:
    monkeypatch.delenv("ONTOLOGY_EXTRACTION_MODE", raising=False)
    assert extraction_mode_from_env() == EnforcementMode.WARN


def test_extraction_mode_strict_and_off(monkeypatch) -> None:
    monkeypatch.setenv("ONTOLOGY_EXTRACTION_MODE", "strict")
    assert extraction_mode_from_env() == EnforcementMode.STRICT
    monkeypatch.setenv("ONTOLOGY_EXTRACTION_MODE", "off")
    assert extraction_mode_from_env() == EnforcementMode.OFF
    monkeypatch.setenv("ONTOLOGY_EXTRACTION_MODE", "junk")
    assert extraction_mode_from_env() == EnforcementMode.WARN


def test_validator_default_non_strict_non_breaking(monkeypatch) -> None:
    monkeypatch.delenv("ONTOLOGY_EXTRACTION_MODE", raising=False)
    v = SchemaValidator(strict_mode=False)
    # неизвестный тип в WARN → True (поведение до P6 не изменилось)
    assert v.validate_node("Dragon", {"x": 1}) is True


def test_validator_global_strict_via_env(monkeypatch) -> None:
    monkeypatch.setenv("ONTOLOGY_EXTRACTION_MODE", "strict")
    v = SchemaValidator(strict_mode=False)  # НЕ явный strict
    # env поднял глобальный STRICT → невалидное теперь отклоняется
    assert v.validate_node("Dragon", {"x": 1}) is False
    assert v.validate_node("Person", {"name": "ok"}) is True


def test_explicit_strict_always_strict(monkeypatch) -> None:
    monkeypatch.setenv("ONTOLOGY_EXTRACTION_MODE", "off")
    v = SchemaValidator(strict_mode=True)
    assert v.validate_node("Dragon", {}) is False  # явный strict > env


# === Часть B: eval-harness =============================================

def _good_extractor(_inp):
    return {
        "objects": [
            {"label": "Person", "props": {"name": "Иван"}},
            {"label": "Task", "props": {"title": "Подготовить ТЗ"}},
        ],
        "actions": [
            {"kind": "create_node", "target": "Person"},
            {"kind": "create_node", "target": "Task"},
        ],
    }


def _case():
    return GoldenCase(
        id="c1",
        input="...",
        expected_objects=[
            {"label": "Person", "key": {"name": "Иван"}},
            {"label": "Task", "key": {"title": "Подготовить ТЗ"}},
        ],
        expected_actions=[
            {"kind": "create_node", "target": "Person"},
            {"kind": "create_node", "target": "Task"},
        ],
    )


def test_perfect_extraction_scores_1() -> None:
    rep = run_eval([_case()], extractor=_good_extractor)
    o = rep.objects
    assert o["precision"] == 1.0 and o["recall"] == 1.0 and o["f1"] == 1.0
    assert rep.actions["recall"] == 1.0
    assert rep.violation_rate == 0.0
    assert rep.passed() is True


def test_missing_object_lowers_recall() -> None:
    def partial(_):
        return {"objects": [{"label": "Person", "props": {"name": "Иван"}}],
                "actions": []}

    rep = run_eval([_case()], extractor=partial)
    assert rep.objects["recall"] == 0.5   # 1 из 2 покрыт
    assert rep.actions["recall"] == 0.0
    assert rep.passed() is False


def test_extra_object_lowers_precision() -> None:
    def noisy(_):
        return {
            "objects": [
                {"label": "Person", "props": {"name": "Иван"}},
                {"label": "Task", "props": {"title": "Подготовить ТЗ"}},
                {"label": "Person", "props": {"name": "Лишний"}},
            ],
            "actions": [
                {"kind": "create_node", "target": "Person"},
                {"kind": "create_node", "target": "Task"},
            ],
        }

    rep = run_eval([_case()], extractor=noisy)
    o = rep.objects
    assert o["tp"] == 2 and o["fp"] == 1
    assert o["recall"] == 1.0
    assert o["precision"] < 1.0


def test_ontology_violation_rate_counts_bad_objects() -> None:
    def dirty(_):
        return {
            "objects": [
                {"label": "Person", "props": {"name": "ok"}},
                {"label": "Dragon", "props": {"x": 1}},        # unknown type
                {"label": "Project", "props": {"name": "no-id"}},  # missing req
            ],
            "actions": [],
        }

    # ожидаем все 3 (P/R=1) — изолируем violation_rate как единств. фактор
    case = GoldenCase(id="d", input="x",
                      expected_objects=[
                          {"label": "Person", "key": {"name": "ok"}},
                          {"label": "Dragon", "key": {"x": 1}},
                          {"label": "Project", "key": {"name": "no-id"}},
                      ],
                      expected_actions=[])
    rep = run_eval([case], extractor=dirty)
    assert rep.objects["precision"] == 1.0 and rep.objects["recall"] == 1.0
    # 2 из 3 объектов нарушают онтологию в STRICT
    assert rep.violation_rate == round(2 / 3, 4)
    assert rep.passed(max_violation_rate=0.05) is False
    assert rep.passed(max_violation_rate=0.7) is True


def test_extractor_failure_is_isolated_and_fails_gate() -> None:
    def boom(_):
        raise RuntimeError("LLM timeout")

    rep = run_eval([_case()], extractor=boom)
    assert rep.errored_cases == 1
    assert rep.case_scores[0].error
    # все expected → FN
    assert rep.objects["recall"] == 0.0
    assert rep.passed() is False


def test_run_eval_non_list_safe() -> None:
    rep = run_eval("nope", extractor=_good_extractor)  # type: ignore[arg-type]
    assert rep.cases == 0


def test_check_ontology_can_be_disabled() -> None:
    def dirty(_):
        return {"objects": [{"label": "Dragon", "props": {}}], "actions": []}

    rep = run_eval([GoldenCase(id="x", input="i")],
                   extractor=dirty, check_ontology=False)
    assert rep.violation_rate == 0.0


def test_load_golden_from_json_string_and_list() -> None:
    raw = json.dumps([
        {"id": "a", "input": "t",
         "expected_objects": [{"label": "Person", "key": {"name": "Z"}}],
         "expected_actions": []}
    ])
    cases = load_golden(raw)
    assert len(cases) == 1 and cases[0].id == "a"
    assert load_golden(123) == []          # мусор → []
    assert load_golden("{bad json") == []  # битый JSON → []


def test_shipped_golden_fixture_loads() -> None:
    p = _ROOT / "backend/core/eval/golden_extraction.json"
    cases = load_golden(p.read_text(encoding="utf-8"))
    assert len(cases) >= 3
    assert all(c.id and c.expected_objects for c in cases)


def test_report_to_dict_shape() -> None:
    rep = run_eval([_case()], extractor=_good_extractor)
    d = rep.to_dict()
    assert d["cases"] == 1
    assert "objects" in d and "violation_rate" in d
    assert d["per_case"][0]["case_id"] == "c1"
