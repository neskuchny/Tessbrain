"""Unit-тесты для dependency_closure (P2 — что ещё надо сделать).

Всё инъектируемо: graph_search / llm_generate — фейки, без графа/LLM.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

# dependency_closure.py — чистый модуль (stdlib-only), но `backend.core.think`
# __init__ тащит каскад тяжёлых deps (autogen/mem0/...). Загружаем модуль
# напрямую через importlib, подсунув lightweight stub-пакеты
# (паттерн из test_tz_generator_template_hook).
_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "core" / "think" / "task_specification"
    / "dependency_closure.py"
)


def _load_module():
    for pkg in ("backend", "backend.core", "backend.core.think",
                "backend.core.think.task_specification"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m

    spec = importlib.util.spec_from_file_location(
        "backend.core.think.task_specification.dependency_closure",
        _MODULE_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
DependencyClosure = _mod.DependencyClosure
compute_dependency_closure = _mod.compute_dependency_closure


def _run(coro):
    return asyncio.run(coro)


# === Режим analysis_only (нет графа, нет LLM) ============================

def test_analysis_only_seeds_from_dependencies() -> None:
    analysis = {
        "analysis": {
            "dependencies": {
                "technical": ["нужен API ключ"],
                "business": ["согласование с юристами"],
                "resource": [],
            },
            "recommended_approach": {"critical_path": ["настроить CI"]},
        }
    }
    cl = _run(compute_dependency_closure(
        task_description="сделать интеграцию",
        llm_analysis=analysis,
    ))
    assert cl.mode == "analysis_only"
    assert cl.graph_grounded is False
    assert "нужен API ключ" in cl.missing
    assert "согласование с юристами" in cl.missing
    assert "настроить CI" in cl.must_precede
    assert cl.summary  # детерминированный summary заполнен
    assert not cl.is_empty


def test_empty_analysis_gives_clean_summary() -> None:
    cl = _run(compute_dependency_closure(
        task_description="простая задача",
        llm_analysis={"analysis": {}},
    ))
    assert cl.is_empty
    assert "можно брать в работу" in cl.summary
    assert cl.to_extra_context() != ""  # summary всё равно даёт блок


# === Режим graph_only (граф есть, LLM нет) ==============================

def test_graph_only_grounds_from_graph() -> None:
    async def fake_graph(query, depth=2):
        return [
            {"title": "Задача X не закрыта"},
            {"name": "Ждём решение по бюджету"},
            "raw string hit",
            {"irrelevant": 1},  # без текстового поля → отбрасывается
        ]

    cl = _run(compute_dependency_closure(
        task_description="запустить фичу",
        entities={"projects": ["Alpha"]},
        llm_analysis={"analysis": {"dependencies": {"technical": ["t1"]}}},
        graph_search=fake_graph,
    ))
    assert cl.graph_grounded is True
    assert cl.mode == "graph_only"
    # graph-хиты + seeded technical попадают в missing
    joined = " ".join(cl.missing)
    assert "Задача X не закрыта" in joined
    assert "Ждём решение по бюджету" in joined
    assert "raw string hit" in joined
    assert "t1" in joined


def test_graph_search_failure_is_caught() -> None:
    async def boom(query, depth=2):
        raise RuntimeError("neo4j down")

    cl = _run(compute_dependency_closure(
        task_description="x",
        llm_analysis={"analysis": {"dependencies": {"resource": ["r"]}}},
        graph_search=boom,
    ))
    # не падает, деградирует до analysis_only
    assert cl.graph_grounded is False
    assert "r" in cl.missing


# === Режим full (граф + LLM) ============================================

def test_full_mode_llm_synth_wins() -> None:
    async def fake_graph(query, depth=2):
        return [{"title": "соседняя работа"}]

    async def fake_llm(prompt, system):
        return {
            "blocked_by": ["нет доступа к проду"],
            "must_precede": ["миграция БД"],
            "missing": ["спека API"],
            "follow_ups": ["написать доки"],
            "summary": "Чтобы закрыть задачу, нужно получить доступ и мигрировать БД.",
        }

    cl = _run(compute_dependency_closure(
        task_description="выкатить релиз",
        llm_analysis={"analysis": {"dependencies": {"technical": ["seed"]}}},
        graph_search=fake_graph,
        llm_generate=fake_llm,
    ))
    assert cl.mode == "full"
    assert cl.blocked_by == ["нет доступа к проду"]
    assert cl.must_precede == ["миграция БД"]
    assert cl.missing == ["спека API"]
    assert cl.follow_ups == ["написать доки"]
    assert cl.summary.startswith("Чтобы закрыть задачу")
    assert cl.graph_grounded is True


def test_llm_only_mode_no_graph() -> None:
    async def fake_llm(prompt, system):
        return {"blocked_by": ["b"], "summary": "s"}

    cl = _run(compute_dependency_closure(
        task_description="x",
        llm_analysis={"analysis": {}},
        llm_generate=fake_llm,
    ))
    assert cl.mode == "llm_only"
    assert cl.blocked_by == ["b"]


def test_llm_error_falls_back_to_deterministic() -> None:
    async def bad_llm(prompt, system):
        return {"error": "rate limited"}

    cl = _run(compute_dependency_closure(
        task_description="x",
        llm_analysis={"analysis": {"dependencies": {"business": ["bizdep"]}}},
        llm_generate=bad_llm,
    ))
    assert "bizdep" in cl.missing
    assert cl.summary  # детерминированный


def test_llm_exception_caught() -> None:
    async def boom_llm(prompt, system):
        raise RuntimeError("llm crash")

    cl = _run(compute_dependency_closure(
        task_description="x",
        llm_analysis={"analysis": {"dependencies": {"technical": ["td"]}}},
        llm_generate=boom_llm,
    ))
    assert "td" in cl.missing  # деградация без падения


# === to_extra_context / to_dict =========================================

def test_to_extra_context_markdown_shape() -> None:
    cl = DependencyClosure(
        blocked_by=["B1"], must_precede=["M1"], missing=["X1"],
        follow_ups=["F1"], summary="нужно сделать A, B",
    )
    md = cl.to_extra_context()
    assert "## Предусловия и зависимости" in md
    assert "нужно сделать A, B" in md
    assert "Блокируется: B1" in md
    assert "Должно быть сделано до: M1" in md
    assert "Не хватает: X1" in md
    assert "После — доделать: F1" in md


def test_to_dict_roundtrip_fields() -> None:
    cl = DependencyClosure(missing=["a"], summary="s", graph_grounded=True)
    d = cl.to_dict()
    assert d["missing"] == ["a"]
    assert d["summary"] == "s"
    assert d["graph_grounded"] is True
    assert d["is_empty"] is False
    assert "mode" in d


def test_dedup_and_limits() -> None:
    async def fake_llm(prompt, system):
        return {
            "missing": ["dup", "Dup", "dup ", "uniq", *[f"x{i}" for i in range(20)]],
            "summary": "s",
        }

    cl = _run(compute_dependency_closure(
        task_description="t",
        llm_analysis={"analysis": {}},
        llm_generate=fake_llm,
    ))
    # дедуп без регистра + лимит 10
    assert cl.missing.count("dup") <= 1
    assert len(cl.missing) <= 10


# === Никогда не raises на мусоре =========================================

def test_garbage_inputs_no_raise() -> None:
    cl = _run(compute_dependency_closure(
        task_description="",
        entities=None,
        llm_analysis=None,
    ))
    assert isinstance(cl, DependencyClosure)
    assert cl.summary  # всегда есть summary
