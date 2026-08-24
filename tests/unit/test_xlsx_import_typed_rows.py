# -*- coding: utf-8 -*-
"""Шаги 3–4 Foundry-плана: типизированное хранение ячеек (числа числами
при персисте) и импорт .xlsx (stdlib, round-trip через xlsx_export)."""
from __future__ import annotations

from backend.core.ontology.dataset_registry import (
    DatasetRegistry,
    deep_profile,
    typed_rows,
)
from backend.core.ontology.xlsx_export import rows_to_xlsx
from backend.core.ontology.xlsx_import import parse_xlsx


# ── типизация ячеек при записи ──────────────────────────────────────────

def test_typed_rows_converts_numbers_keeps_rest():
    columns = ["Менеджер", "Выручка", "Доля", "Дата"]
    rows = [{"Менеджер": "Аня", "Выручка": "1 200,50", "Доля": "12%",
             "Дата": "2026-06-01"},
            {"Менеджер": "Петя", "Выручка": "не сдал", "Доля": "8%",
             "Дата": "2026-06-02"}]
    profile = deep_profile(columns, rows)
    out = typed_rows(rows, profile)
    assert out[0]["Выручка"] == 1200.5          # число стало числом
    assert out[1]["Выручка"] == "не сдал"       # мусор честно остался строкой
    assert out[0]["Доля"] == "12%"              # percent несёт формат
    assert out[0]["Дата"] == "2026-06-01"       # даты не трогаем
    assert out[0]["Менеджер"] == "Аня"


def test_registry_persists_typed_numbers(tmp_path):
    reg = DatasetRegistry(str(tmp_path / "idx.json"))
    rec = reg.register(
        title="Продажи", columns=["Менеджер", "Выручка"],
        rows=[{"Менеджер": "Аня", "Выручка": "100"},
              {"Менеджер": "Петя", "Выручка": "200.5"}])
    stored = reg.load_rows(rec["dataset_id"])
    assert stored[0]["Выручка"] == 100.0
    assert isinstance(stored[1]["Выручка"], float)


# ── xlsx: round-trip экспорт → импорт ───────────────────────────────────

def test_xlsx_roundtrip():
    columns = ["Менеджер", "Выручка", "Комментарий"]
    rows = [
        {"Менеджер": "Аня", "Выручка": 1200.5, "Комментарий": "ок"},
        {"Менеджер": "Петя", "Выручка": 300, "Комментарий": "ждём оплату"},
    ]
    blob = rows_to_xlsx(columns, rows, sheet_name="Данные")
    cols2, rows2 = parse_xlsx(blob)
    assert cols2 == columns
    assert len(rows2) == 2
    assert rows2[0]["Менеджер"] == "Аня"
    assert rows2[0]["Выручка"] == 1200.5        # число пережило round-trip
    assert rows2[1]["Выручка"] == 300
    assert rows2[1]["Комментарий"] == "ждём оплату"


def test_xlsx_garbage_returns_empty():
    assert parse_xlsx(b"not a zip at all") == ([], [])
    assert parse_xlsx(b"") == ([], [])


def test_xlsx_register_end_to_end(tmp_path):
    """xlsx → DatasetRegistry: профиль видит числа, кириллица цела."""
    columns = ["Дата", "Выручка, млн"]
    rows = [{"Дата": "2026-05-01", "Выручка, млн": 1.5},
            {"Дата": "2026-06-01", "Выручка, млн": 2.5}]
    blob = rows_to_xlsx(columns, rows)
    cols2, rows2 = parse_xlsx(blob)
    reg = DatasetRegistry(str(tmp_path / "idx.json"))
    rec = reg.register(title="Из Excel", columns=cols2, rows=rows2)
    prof = {p["name"]: p for p in rec["profile"]["columns"]}
    assert prof["Выручка, млн"]["dtype"] == "number"
    assert prof["Выручка, млн"]["scale"] == 1_000_000   # млн из заголовка
    assert prof["Дата"]["dtype"] == "date"
