# -*- coding: utf-8 -*-
"""Медиаплан: каскадную арифметику считает КОД (Decimal), LLM только
извлекает ставки из материалов. Проверяем каскады CPM→CTR→CR, CPC, CPA,
сплит по долям, честные notes и e2e с фейковым LLM."""
from __future__ import annotations

import asyncio

import pytest

import backend.core.documents.mediaplan_engine as me
from backend.core.documents.mediaplan_engine import (
    build_mediaplan,
    compute_mediaplan,
)


def test_cascade_cpm_ctr_cr():
    r = compute_mediaplan([{
        "name": "Яндекс.Директ", "budget": 100_000,
        "cpm": 300, "ctr_pct": 1.2, "cr_pct": 5,
    }])
    row = r["rows"][0]
    # 100000/300*1000 = 333333 показов; ×1.2% = 4000 кликов; ×5% = 200 лидов
    assert row["Показы"] == "333333"
    assert row["Клики"] == "4000"
    assert row["Лиды"] == "200"
    assert row["CPA, ₽"] == "500.00"       # 100000/200 — посчитал код
    assert r["notes"] == []


def test_cascade_cpc_and_cpa_paths():
    r = compute_mediaplan([
        {"name": "Директ (CPC)", "budget": 50_000, "cpc": 25, "cr_pct": 4},
        {"name": "Телеграм (CPA)", "budget": 30_000, "cpa": 600},
    ])
    a, b = r["rows"]
    assert a["Клики"] == "2000" and a["Лиды"] == "80"    # 50000/25; ×4%
    assert b["Лиды"] == "50"                             # 30000/600
    assert r["totals"]["Лиды"] == "130"
    assert r["totals"]["Бюджет, ₽"] == "80000.00"


def test_share_split_needs_total():
    r = compute_mediaplan([{"name": "VK", "share_pct": 40, "cpm": 200,
                            "ctr_pct": 1, "cr_pct": 3}],
                          total_budget=200_000)
    assert r["rows"][0]["Бюджет, ₽"] == "80000.00"       # 40% от 200k
    r2 = compute_mediaplan([{"name": "VK", "share_pct": 40}])
    assert r2["rows"] == []                              # доля без total
    assert any("нет общего бюджета" in n for n in r2["notes"])


def test_missing_rates_honest_notes():
    r = compute_mediaplan([{"name": "Новый канал", "budget": 10_000}])
    assert r["rows"][0]["Показы"] == "" and r["rows"][0]["Лиды"] == ""
    assert any("не хватает ставок" in n for n in r["notes"])


def test_split_mismatch_note():
    r = compute_mediaplan(
        [{"name": "A", "budget": 30_000, "cpa": 500}],
        total_budget=100_000)
    assert any("не сходится" in n for n in r["notes"])


class _FakeLLM:
    async def generate_json(self, prompt=None, **kw):
        # ключевые правила в промпте: бенчмарк — ориентир, не истина
        assert "НЕ выдумывай" in prompt
        assert "БЕНЧМАРКИ-ОРИЕНТИРЫ" in prompt
        assert "СКОРРЕКТИРОВАТЬ" in prompt
        return {
            "total_budget": 100_000,
            "channels": [
                {"name": "Яндекс.Директ", "share_pct": 60, "cpm": 300,
                 "ctr_pct": 1.2, "cr_pct": 5,
                 "rate_origin": "adjusted_benchmark",
                 "baseline": "CPM 250 (заготовка)",
                 "adjustment_reason": "ниша конкурентная, Москва — CPM выше",
                 "source": "заготовка «Ставки Директ»",
                 "why": "клиент ищет b2c-лиды"},
                {"name": "Telegram Ads", "share_pct": 30, "cpm": None,
                 "cpc": None, "rate_origin": "missing", "source": "",
                 "why": "аудитория клиента там"},
                {"name": "VK", "share_pct": 10, "cpm": 180, "ctr_pct": 0.9,
                 "cr_pct": 3, "rate_origin": "benchmark_asis",
                 "source": "заготовка", "why": "дешёвый охват"},
            ],
            "assumptions": ["бюджет 100к из встречи"],
            "rationale": "Директ даёт предсказуемый CPA по нашей практике.",
        }


def test_build_mediaplan_end_to_end(monkeypatch):
    async def _mt(uid, ids, cap_chars=0):
        return "=== ВСТРЕЧА === обсуждали бюджет 100 000 ₽"
    import backend.core.documents.fill_engine as fe
    monkeypatch.setattr(fe, "_meetings_text", _mt)
    monkeypatch.setattr(fe, "presets_text", lambda uid, ids: "CPM Директ 300₽")

    res = asyncio.run(build_mediaplan(
        "u1", client_query="ООО Ромашка", meeting_ids=["m1"],
        llm=_FakeLLM()))
    assert res["success"] is True
    t = res["table"]
    # 60% от 100к = 60к; СКОРРЕКТИРОВАННЫЙ CPM 300 (не бенчмарк 250!):
    # 60000/300*1000=200000 показов; ×1.2%=2400; ×5%=120
    d = t["rows"][0]
    assert d["Бюджет, ₽"] == "60000.00" and d["Лиды"] == "120"
    assert d["CPA, ₽"] == "500.00"
    # канал без ставок честно помечен
    assert "Telegram Ads" in res["rates_missing"]
    assert any("не хватает ставок" in n for n in t["notes"])
    assert "Директ" in res["rationale"]
    # происхождение ставок: корректировка бенчмарка видна с причиной
    m = res["channels_meta"][0]
    assert m["rate_origin"] == "adjusted_benchmark"
    assert "CPM 250" in m["baseline"]
    assert "конкурентная" in m["adjustment_reason"]
    # бенчмарк без корректировки → предупреждение менеджеру
    assert any("перепроверьте" in n and "VK" in n for n in t["notes"])


def test_build_mediaplan_no_channels(monkeypatch):
    import backend.core.documents.fill_engine as fe
    async def _mt(uid, ids, cap_chars=0):
        return ""
    monkeypatch.setattr(fe, "_meetings_text", _mt)
    monkeypatch.setattr(fe, "presets_text", lambda uid, ids: "")

    class _Empty:
        async def generate_json(self, **kw):
            return {"channels": []}
    res = asyncio.run(build_mediaplan("u1", client_query="x",
                                      meeting_ids=[], llm=_Empty()))
    assert res["success"] is False and "заготовку" in res["error"]


def test_formulas_table_live_formulas():
    from backend.core.documents.mediaplan_engine import formulas_table
    t = compute_mediaplan([
        {"name": "Директ", "budget": 100_000, "cpm": 300,
         "ctr_pct": 1.2, "cr_pct": 5},
        {"name": "Телеграм", "budget": 30_000, "cpa": 600},
    ])
    rows = formulas_table(t)
    d, tg, tot = rows
    # каскад — живыми формулами (менеджер правит бюджет → пересчёт)
    assert d["Показы"] == "=ROUND(B2/C2*1000,0)"
    assert d["Клики"] == "=ROUND(D2*F2/100,0)"
    assert d["Лиды"] == "=ROUND(G2*H2/100,0)"
    assert d["CPA, ₽"] == "=ROUND(B2/I2,2)"
    # у Телеграма нет CPM/CPC/CTR/CR — лиды остаются числом, CPA формулой
    assert tg["Показы"] == "" and tg["Лиды"] == 50.0
    assert tg["CPA, ₽"] == "=ROUND(B3/I3,2)"
    # ИТОГО — суммы диапазонов
    assert tot["Бюджет, ₽"] == "=SUM(B2:B3)"
    assert tot["Лиды"] == "=SUM(I2:I3)"
    assert tot["CPA, ₽"] == "=ROUND(B4/I4,2)"
    # бюджет и ставки — числа (не строки), Excel их суммирует
    assert d["Бюджет, ₽"] == 100000.0 and d["CPM, ₽"] == 300.0


def test_xlsx_formula_cell():
    from backend.core.ontology.xlsx_export import _cell, rows_to_xlsx
    assert _cell(2, 3, "=SUM(B2:B5)") == '<c r="D2"><f>SUM(B2:B5)</f></c>'
    blob = rows_to_xlsx(["A", "B"], [{"A": 1, "B": "=A2*2"}])
    import io
    import zipfile
    sheet = zipfile.ZipFile(io.BytesIO(blob)).read("xl/worksheets/sheet1.xml")
    assert b"<f>A2*2</f>" in sheet
