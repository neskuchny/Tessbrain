# -*- coding: utf-8 -*-
"""Контекст отчёта строится из ЗНАНИЙ и не режется.

Инцидент: отчёт «уровня McKinsey» собирался из сырых транскриптов 25 последних
встреч, каждый обрезанный до 5000 символов. 25 встреч не описывают компанию —
их сотни; а обрубленный транскрипт выбрасывает всё, что мозг из встречи уже
извлёк (решения, цели, риски, KPI). Здесь проверяем новый сборщик:
composition из трёх источников, отсутствие обрезки, честный пустой ответ и
map-reduce вместо ножа при переполнении бюджета.
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import backend.core.reports.report_context as rc  # noqa: E402


def _patch(monkeypatch, *, snap="", facts=("", {}), summaries=("", 0, 0)):
    async def _snap(uid):
        return snap

    async def _facts(uid, days, keywords=None):
        return facts

    async def _sums(uid, **kw):
        return summaries

    async def _dom(uid, domains):
        return ""

    monkeypatch.setattr(rc, "_company_snapshot_text", _snap)
    monkeypatch.setattr(rc, "_graph_facts_text", _facts)
    monkeypatch.setattr(rc, "_meeting_summaries", _sums)
    monkeypatch.setattr(rc, "_domain_snapshots_text", _dom)


def test_composes_three_knowledge_sources(monkeypatch):
    _patch(monkeypatch,
           snap="## ПРОФИЛЬ КОМПАНИИ\nНазвание: Т-Сенд",
           facts=("## ЧТО ПРОИЗОШЛО\n  • [2026-05-01] Решили запустить MVP",
                  {"Decision": 1, "Risk": 2}),
           summaries=("## ХРОНОЛОГИЯ\n--- Встреча (2026-05-02) ---\nитоги", 40, 31))

    out = asyncio.run(rc.build_company_context("u-1", days_back=90))
    text, stats = out["text"], out["stats"]

    assert "ПРОФИЛЬ КОМПАНИИ" in text and "ЧТО ПРОИЗОШЛО" in text \
        and "ХРОНОЛОГИЯ" in text, "потерян один из источников знаний"
    assert stats["source"] == "knowledge", "отчёт снова читает сырые транскрипты"
    assert stats["has_snapshot"] is True
    assert stats["graph_facts_total"] == 3
    # Охват честный: 40 встреч в периоде, у 31 есть саммари — не «25 последних».
    assert stats["meetings_in_period"] == 40
    assert stats["meetings_with_summary"] == 31
    assert stats["summaries_condensed"] is False


def test_no_data_is_honest_empty(monkeypatch):
    """Ни снапшота, ни графа, ни саммари → пустой текст, а не выдумка."""
    _patch(monkeypatch)
    out = asyncio.run(rc.build_company_context("u-1"))
    assert out["text"] == ""
    assert out["stats"]["has_snapshot"] is False
    assert out["stats"]["graph_facts_total"] == 0


def test_partial_sources_still_build_context(monkeypatch):
    """Отказ снапшота не должен обнулять отчёт — остальное идёт как есть."""
    _patch(monkeypatch, summaries=("## ХРОНОЛОГИЯ\nсаммари", 5, 5))
    out = asyncio.run(rc.build_company_context("u-1"))
    assert "ХРОНОЛОГИЯ" in out["text"]
    assert out["stats"]["has_snapshot"] is False


def test_overflow_is_condensed_not_truncated(monkeypatch):
    """Переполнение бюджета → map-reduce, а НЕ text[:N].

    Ключевая проверка: факт из самого конца материала обязан дожить до
    результата. Обрезка убила бы его молча."""
    tail = "ФАКТ-ХВОСТ: выручка выросла на 12%"
    big = ("## ХРОНОЛОГИЯ\n" + ("строка саммари встречи\n" * 30_000) + tail)
    assert len(big) > rc._SUMMARY_BUDGET_CHARS

    seen = []

    async def _fake_condense(uid, text, *, what):
        seen.append(len(text))
        # честный map-reduce сохраняет и начало, и хвост
        return "СВЁРНУТО\n" + tail

    _patch(monkeypatch, summaries=(big, 300, 300))
    monkeypatch.setattr(rc, "_condense", _fake_condense)

    out = asyncio.run(rc.build_company_context("u-1"))
    assert seen and seen[0] == len(big), "в сжатие ушёл не весь материал"
    assert tail in out["text"], "хвост материала потерян — это и есть обрезка"
    assert out["stats"]["summaries_condensed"] is True


def test_condense_failure_returns_source_intact(monkeypatch):
    """Если LLM недоступна — отдаём материал целиком, но не режем."""
    async def _boom(*a, **k):
        raise RuntimeError("LLM недоступна")

    monkeypatch.setattr(
        "backend.core.llm.workload_policy.generate_for_workload", _boom,
        raising=False)
    src = "строка\n" * 100
    got = asyncio.run(rc._condense("u-1", src, what="тест"))
    assert got == src


def test_snapshot_has_no_slices():
    """format_company_snapshot не должен терять элементы длинных списков."""
    snap = {"name": "X",
            "active_projects": [{"name": f"Проект {i}"} for i in range(25)],
            "current_challenges": [f"Вызов {i}" for i in range(15)]}
    out = rc.format_company_snapshot(snap)
    for i in range(25):
        assert f"Проект {i}" in out
    for i in range(15):
        assert f"Вызов {i}" in out
    assert "(25)" in out and "(15)" in out, "счётчики блоков должны быть честными"
