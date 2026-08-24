# -*- coding: utf-8 -*-
"""Тесты импорта методики из документа (Анализ → методология файлом)."""
from __future__ import annotations

import asyncio

import pytest

from backend.core.analysis.methodology_import import (
    _fallback_playbook,
    _playbook_from_llm_dict,
    playbook_from_methodology,
)


def _run(coro):
    return asyncio.run(coro)


# ── чистый билдер из LLM-dict ──────────────────────────────────────────────

def test_builder_basic_dimensions() -> None:
    d = {
        "name": "DD лайт",
        "analysis_type": "due_diligence",
        "dimensions": [
            {"key": "debt_load", "label": "Долговая нагрузка", "kind": "quantitative",
             "computation": "debt / equity", "inputs": ["debt", "equity"],
             "benchmark": {"red_flag": 2.0}},
            {"key": "legal", "label": "Юр-риски", "kind": "qualitative"},
        ],
    }
    pb = _playbook_from_llm_dict(d, name=None, created_by="u1", org_id="o1")
    assert pb.name == "DD лайт" and pb.analysis_type == "due_diligence"
    assert len(pb.dimensions) == 2
    assert pb.dimensions[0].kind == "quantitative"
    assert pb.dimensions[0].computation == "debt / equity"
    assert pb.created_by == "u1" and pb.org_id == "o1"
    assert pb.id.startswith("apb_")


def test_builder_dedups_keys() -> None:
    d = {"dimensions": [
        {"key": "risk", "label": "Риск А"},
        {"key": "risk", "label": "Риск Б"},
    ]}
    pb = _playbook_from_llm_dict(d, name=None, created_by="", org_id=None)
    keys = [dim.key for dim in pb.dimensions]
    assert len(keys) == len(set(keys)), keys


def test_builder_slugifies_and_defaults_kind() -> None:
    d = {"dimensions": [{"key": "Денежный Поток!!", "label": "Кэшфлоу",
                         "kind": "bogus"}]}
    pb = _playbook_from_llm_dict(d, name=None, created_by="", org_id=None)
    dim = pb.dimensions[0]
    assert dim.kind == "qualitative"          # неизвестный kind → qualitative
    assert dim.key and " " not in dim.key      # ключ слугифицирован
    assert dim.computation is None             # qualitative → без формулы


def test_builder_qualitative_drops_computation() -> None:
    d = {"dimensions": [{"key": "x", "label": "X", "kind": "qualitative",
                         "computation": "a / b"}]}
    pb = _playbook_from_llm_dict(d, name=None, created_by="", org_id=None)
    assert pb.dimensions[0].computation is None


def test_builder_name_override_wins() -> None:
    d = {"name": "из llm", "dimensions": [{"key": "x", "label": "X"}]}
    pb = _playbook_from_llm_dict(d, name="мой заголовок", created_by="", org_id=None)
    assert pb.name == "мой заголовок"


def test_builder_label_falls_back_to_key_but_skips_empty() -> None:
    # key-only → label=key (не теряем измерение); совсем пустое → пропуск.
    d = {"dimensions": [{"key": "x"}, {"label": "Есть", "key": "y"}, {}]}
    pb = _playbook_from_llm_dict(d, name=None, created_by="", org_id=None)
    assert [dim.label for dim in pb.dimensions] == ["x", "Есть"]


def test_builder_invalid_analysis_type_becomes_custom() -> None:
    d = {"analysis_type": "wild", "dimensions": [{"key": "x", "label": "X"}]}
    pb = _playbook_from_llm_dict(d, name=None, created_by="", org_id=None)
    assert pb.analysis_type == "custom"


# ── fallback ───────────────────────────────────────────────────────────────

def test_fallback_has_one_qualitative_dim() -> None:
    pb = _fallback_playbook("Смотри на выручку и риски", name=None,
                            created_by="u", org_id=None)
    assert len(pb.dimensions) == 1 and pb.dimensions[0].kind == "qualitative"
    assert "выручку" in pb.company_context.get("custom", "")


# ── обёртка playbook_from_methodology (LLM замокан) ────────────────────────

def test_wrapper_empty_text_returns_fallback() -> None:
    pb, warns = _run(playbook_from_methodology("", created_by="u"))
    assert pb.dimensions and any("пуст" in w for w in warns)


def test_wrapper_uses_llm_result(monkeypatch) -> None:
    async def fake_json(prompt, **kw):
        return {"name": "Из LLM", "analysis_type": "financial",
                "dimensions": [{"key": "rev", "label": "Выручка",
                                "kind": "quantitative", "computation": "a + b",
                                "inputs": ["a", "b"]}]}

    class _Router:
        generate_json = staticmethod(fake_json)

    import backend.core.llm.router as r
    monkeypatch.setattr(r, "get_llm_router", lambda: _Router())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    pb, warns = _run(playbook_from_methodology("методология...", created_by="u",
                                               org_id="o"))
    assert pb.name == "Из LLM" and pb.analysis_type == "financial"
    assert pb.dimensions[0].key == "rev"


def test_wrapper_llm_failure_falls_back(monkeypatch) -> None:
    async def boom(prompt, **kw):
        raise RuntimeError("llm down")

    class _Router:
        generate_json = staticmethod(boom)

    import backend.core.llm.router as r
    monkeypatch.setattr(r, "get_llm_router", lambda: _Router())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    pb, warns = _run(playbook_from_methodology("текст методики", created_by="u"))
    assert pb.dimensions  # черновик всё равно есть
    assert any("LLM" in w or "черновик" in w for w in warns)


def test_wrapper_junk_llm_result_falls_back(monkeypatch) -> None:
    async def junk(prompt, **kw):
        return {"no_dimensions": True}

    class _Router:
        generate_json = staticmethod(junk)

    import backend.core.llm.router as r
    monkeypatch.setattr(r, "get_llm_router", lambda: _Router())
    monkeypatch.setattr(r, "set_llm_context", lambda **k: None)

    pb, warns = _run(playbook_from_methodology("x" * 50, created_by="u"))
    assert pb.dimensions and any("черновик" in w for w in warns)
