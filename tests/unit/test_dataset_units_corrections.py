# -*- coding: utf-8 -*-
"""Онтология данных: единицы/валюты/масштаб + корректировки пользователя.

«Будет ли мозг понимать, что это млн ₽, а не штуки?» — единица выводится
из заголовка/значений, деньги без единицы честно просят уточнения, а
правка пользователя (entity/unit/scale) сильнее авто-разбора и переживает
refresh данных.
"""
from __future__ import annotations

import pytest

from backend.core.ontology.dataset_registry import (
    DatasetRegistry,
    infer_unit,
    profile_column,
    unit_label,
)
from backend.core.ontology.query_engine import assess_result


# ── infer_unit / unit_label ─────────────────────────────────────────────

def test_infer_unit_from_header_scale_and_currency():
    u = infer_unit("Выручка, млн ₽", ["1.2", "3.4"])
    assert u["scale"] == 1_000_000 and u["scale_label"] == "млн"
    assert u["unit"] == "RUB" and u["unit_symbol"] == "₽"
    assert u["unit_source"] == "header"


def test_infer_unit_usd_and_thousands():
    u = infer_unit("Cost USD, тыс", ["10", "20"])
    assert u["unit"] == "USD" and u["scale"] == 1_000


def test_money_like_without_unit_flagged():
    u = infer_unit("Выручка", ["100", "200"])
    assert u.get("money_like") is True and "unit" not in u


def test_non_money_column_no_flags():
    assert infer_unit("Штук", ["1", "2"]) == {}


def test_unit_label_composition():
    assert unit_label({"scale_label": "млн", "unit_symbol": "₽"}) == "млн ₽"
    assert unit_label({"unit_symbol": "$"}) == "$"
    assert unit_label({"dtype": "percent"}) == "%"
    assert unit_label({}) == ""


def test_profile_column_carries_unit():
    prof = profile_column("Бюджет, млн ₽", ["1.5", "2", "3.1"])
    assert prof["dtype"] == "number"
    assert prof["scale"] == 1_000_000 and prof["unit"] == "RUB"


# ── корректировки в реестре ─────────────────────────────────────────────

@pytest.fixture()
def reg(tmp_path):
    return DatasetRegistry(str(tmp_path / "datasets_index.json"))


def _register(reg, title="Продажи", col="Выручка"):
    return reg.register(
        title=title, columns=["Менеджер", col],
        rows=[{"Менеджер": "Аня", col: "100"},
              {"Менеджер": "Петя", col: "200"}])


def test_override_grounds_column_and_sets_unit(reg):
    rec = _register(reg)
    rec = reg.set_column_override(rec["dataset_id"], "Выручка", {
        "kpi": "Выручка", "unit": "RUB", "unit_symbol": "₽",
        "scale": 1_000_000, "scale_label": "млн"})
    g = rec["ontology"]["grounding"]["Выручка"]
    assert g["known"] is True and g["corrected_by_user"] is True
    prof = [p for p in rec["profile"]["columns"] if p["name"] == "Выручка"][0]
    assert prof["unit"] == "RUB" and prof["scale"] == 1_000_000
    assert "money_like" not in prof   # единица подтверждена — просьба снята


def test_override_survives_refresh(reg):
    rec = _register(reg)
    reg.set_column_override(rec["dataset_id"], "Выручка",
                            {"unit": "RUB", "unit_symbol": "₽"})
    # refresh: новые строки → профиль пересобран с нуля
    rec = reg.update(rec["dataset_id"], {}, rows=[
        {"Менеджер": "Аня", "Выручка": "300"}])
    prof = [p for p in rec["profile"]["columns"] if p["name"] == "Выручка"][0]
    assert prof["unit"] == "RUB"      # правка наложилась заново


def test_override_unknown_column_rejected(reg):
    rec = _register(reg)
    assert reg.set_column_override(rec["dataset_id"], "Нет такой", {"unit": "RUB"}) is None


def test_override_dtype_updates_derived_lists(reg):
    rec = _register(reg)
    rec = reg.set_column_override(rec["dataset_id"], "Менеджер", {"dtype": "category"})
    assert "Менеджер" in rec["profile"]["category_columns"]


# ── сервис: алиасы человека («млн», «руб») ──────────────────────────────

def test_correct_dataset_column_aliases(tmp_path, monkeypatch):
    import backend.core.ontology.dataset_service as svc
    reg = DatasetRegistry(str(tmp_path / "idx.json"))
    rec = _register(reg)
    monkeypatch.setattr(svc, "registry_for_user", lambda uid: reg)
    out = svc.correct_dataset_column("u1", rec["dataset_id"], "Выручка",
                                     {"unit": "руб", "scale": "млн"})
    assert out["success"] is True
    ov = out["override"]
    assert ov["unit"] == "RUB" and ov["unit_symbol"] == "₽"
    assert ov["scale"] == 1_000_000 and ov["scale_label"] == "млн"


def test_correct_bad_scale_rejected(tmp_path, monkeypatch):
    import backend.core.ontology.dataset_service as svc
    reg = DatasetRegistry(str(tmp_path / "idx.json"))
    rec = _register(reg)
    monkeypatch.setattr(svc, "registry_for_user", lambda uid: reg)
    out = svc.correct_dataset_column("u1", rec["dataset_id"], "Выручка",
                                     {"scale": "сорок"})
    assert out["success"] is False


# ── оценка: единица в ответе + просьба уточнить деньги ─────────────────

def test_assess_result_money_like_note_and_unit(reg):
    rec = _register(reg)
    plan = {"op": "sum", "column": "Выручка", "filters": []}
    exec_out = {"result": 300.0, "rows_used": 2, "nulls_skipped": 0}
    a = assess_result(plan, exec_out, rec)
    assert any("валюта/масштаб" in n for n in a["notes"])   # money_like
    # после корректировки — единица в оценке, просьбы нет
    rec = reg.set_column_override(rec["dataset_id"], "Выручка", {
        "unit": "RUB", "unit_symbol": "₽", "scale": 1_000_000,
        "scale_label": "млн"})
    a2 = assess_result(plan, exec_out, rec)
    assert a2["unit"] == "млн ₽"
    assert not any("валюта/масштаб" in n for n in a2["notes"])


# ── кросс-сверка не сравнивает разные единицы ──────────────────────────

def test_context_checks_skip_unit_mismatch(reg):
    from backend.core.ontology.dataset_service import _context_checks
    a = _register(reg, title="Отчёт А")
    b = _register(reg, title="Отчёт Б")
    reg.set_column_override(a["dataset_id"], "Выручка",
                            {"unit": "RUB", "unit_symbol": "₽"})
    reg.set_column_override(b["dataset_id"], "Выручка",
                            {"unit": "USD", "unit_symbol": "$",
                             "scale": 1_000, "scale_label": "тыс"})
    a_rec = reg.get(a["dataset_id"])
    plan = {"op": "sum", "column": "Выручка", "filters": []}
    checks = _context_checks(reg, a_rec, plan,
                             {"result": 300.0, "rows_used": 2})
    kinds = {c["kind"] for c in checks}
    assert "unit_mismatch" in kinds
    assert "cross_dataset" not in kinds   # ложное «РАСХОЖДЕНИЕ» не рисуем
