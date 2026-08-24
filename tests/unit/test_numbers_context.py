# -*- coding: utf-8 -*-
"""numbers_block — общий поставщик цифр для ТЗ/таскера, Mark, глубокого
поиска и автоматизаций: единые метрики (план/факт) + заземлённые колонки
датасетов, детерминированно и без LLM."""
from __future__ import annotations

import pytest

import backend.core.ontology.dataset_registry as dreg_mod
import backend.core.ontology.dataset_service as dsvc
import backend.core.ontology.metric_registry as mreg_mod
from backend.core.ontology.dataset_registry import DatasetRegistry
from backend.core.ontology.metric_registry import MetricRegistry
from backend.core.ontology.numbers_context import numbers_block


@pytest.fixture()
def env(tmp_path, monkeypatch):
    metrics = MetricRegistry(str(tmp_path / "m.db"))
    datasets = DatasetRegistry(str(tmp_path / "idx.json"))
    monkeypatch.setattr(dreg_mod, "ontology_enabled", lambda: True)
    monkeypatch.setattr(mreg_mod, "metrics_for_user", lambda uid: metrics)
    monkeypatch.setattr(dsvc, "registry_for_user", lambda uid: datasets)
    return metrics, datasets


def test_disabled_flag_returns_empty(monkeypatch):
    monkeypatch.setattr(dreg_mod, "ontology_enabled", lambda: False)
    out = numbers_block("u1", "выручка")
    assert out["numbers"] == [] and out["text"] == ""


def test_metrics_with_plan_and_divergence(env):
    metrics, _ = env
    metrics.add_point("Выручка", 5_000_000, source_type="meeting",
                      source_id="m1", period="2026-06", at="2026-06-20",
                      unit="RUB")
    metrics.add_point("Выручка", 4_200_000, source_type="dataset",
                      source_id="d1", period="2026-06", at="2026-07-01")
    metrics.add_point("Выручка", 6_000_000, source_type="meeting",
                      source_id="m1", period="2026-07", kind="plan",
                      at="2026-06-20")
    out = numbers_block("u1", "какая выручка")
    names = {n["metric"] for n in out["numbers"]}
    assert names == {"Выручка"}
    # обе последние точки (встреча и данные) + verified у данных
    verified = [n for n in out["numbers"] if n["is_verified"]]
    assert len(verified) == 1
    assert "🎯" in out["text"] and "⚖️" in out["text"]   # план и сверка
    assert "metric:Выручка" in out["sources"]


def test_dataset_grounded_columns_stats(env):
    _, datasets = env
    rec = datasets.register(
        title="Продажи", columns=["Менеджер", "Выручка"],
        rows=[{"Менеджер": "Аня", "Выручка": "100"},
              {"Менеджер": "Петя", "Выручка": "200"}])
    datasets.set_column_override(rec["dataset_id"], "Выручка",
                                 {"kpi": "Выручка", "unit": "RUB",
                                  "unit_symbol": "₽"})
    out = numbers_block("u1", "выручка по продажам")
    ds_nums = [n for n in out["numbers"] if "таблица" in n["source"]]
    assert len(ds_nums) == 1
    assert ds_nums[0]["value"] == "300" and ds_nums[0]["is_verified"]
    assert "сумма 300" in out["text"]


def test_ungrounded_columns_excluded(env):
    _, datasets = env
    datasets.register(
        title="Случайное", columns=["Что-то"],
        rows=[{"Что-то": "5"}, {"Что-то": "6"}])
    out = numbers_block("u1", "что-то")
    assert all("таблица" not in n["source"] for n in out["numbers"])


def test_query_filter_prefers_matches(env):
    metrics, _ = env
    metrics.add_point("Выручка", 100, source_type="meeting", source_id="m1")
    metrics.add_point("Совсем другое", 7, source_type="meeting",
                      source_id="m1")
    out = numbers_block("u1", "посчитай выручку за месяц")
    assert {n["metric"] for n in out["numbers"]} == {"Выручка"}


def test_tasker_numbers_format(env):
    """Формат записей совместим с extracted_numbers таскера."""
    metrics, _ = env
    metrics.add_point("NPS", 72, source_type="dataset", source_id="d1")
    out = numbers_block("u1", "nps")
    n = out["numbers"][0]
    assert set(n) >= {"metric", "value", "unit", "source", "is_verified"}
