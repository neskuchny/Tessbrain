# -*- coding: utf-8 -*-
"""Единый объект «Метрика» — мост между цифрами из встреч и из таблиц.

Проверяем: парсинг живых цифр («4,2 млн ₽» → 4200000.0 RUB), слияние по
имени, идемпотентные питатели (meeting/dataset), сверку «встреча vs
данные» с расхождением и происхождение каждой точки.
"""
from __future__ import annotations

import pytest

import backend.core.ontology.metric_registry as mr
from backend.core.ontology.metric_registry import (
    MetricRegistry,
    ingest_dataset_metrics,
    ingest_meeting_kpis,
    parse_value_with_unit,
)


# ── понимание цифры из речи ─────────────────────────────────────────────

def test_parse_millions_rub():
    p = parse_value_with_unit("4,2 млн ₽")
    assert p["value"] == pytest.approx(4_200_000.0)
    assert p["unit"] == "RUB"


def test_parse_plain_number_and_thousands():
    assert parse_value_with_unit("1 250")["value"] == 1250.0
    assert parse_value_with_unit("300 тыс руб")["value"] == 300_000.0
    assert parse_value_with_unit(42)["value"] == 42.0


def test_parse_no_number_empty():
    assert parse_value_with_unit("вырастет сильно") == {}
    assert parse_value_with_unit("") == {}


# ── реестр ──────────────────────────────────────────────────────────────

@pytest.fixture()
def reg(tmp_path):
    return MetricRegistry(str(tmp_path / "metrics.db"))


def test_upsert_merges_by_normalized_name(reg):
    a = reg.upsert_metric("Выручка", unit="RUB")
    b = reg.upsert_metric("  выручка ")
    assert a["metric_id"] == b["metric_id"]
    assert b["unit"] == "RUB"          # unit не потерялся


def test_points_and_series_with_lineage(reg):
    reg.add_point("Выручка", 4_200_000, source_type="meeting",
                  source_id="m1", detail={"raw": "4,2 млн"})
    reg.add_point("Выручка", 4_000_000, source_type="dataset",
                  source_id="d1", period="2026-06")
    s = reg.series("Выручка")
    assert len(s) == 2
    assert {p["source_type"] for p in s} == {"meeting", "dataset"}
    assert all(p["source_id"] for p in s)     # происхождение у каждой точки


def test_replace_source_points_idempotent(reg):
    for _ in range(3):   # refresh датасета трижды — дублей нет
        reg.replace_source_points("dataset", "d1", [
            {"name": "Выручка", "value": 100, "period": "2026-05"},
            {"name": "Выручка", "value": 200, "period": "2026-06"},
        ])
    assert len(reg.series("Выручка")) == 2


def test_summary_delta_meeting_vs_dataset(reg):
    reg.add_point("Выручка", 5_000_000, source_type="meeting",
                  source_id="m1", period="2026-06", at="2026-06-20")
    reg.add_point("Выручка", 4_200_000, source_type="dataset",
                  source_id="d1", period="2026-06", at="2026-07-01")
    s = reg.summary("Выручка")
    assert s["delta"]["pct"] == pytest.approx(-16.0)
    assert s["delta"]["period"] == "2026-06"   # сверка по общему периоду
    assert "РАСХОЖДЕНИЕ" in s["note"]


def test_summary_single_source_no_delta(reg):
    reg.add_point("NPS", 72, source_type="meeting", source_id="m1")
    s = reg.summary("NPS")
    assert s["delta"] is None and s["points_total"] == 1


# ── питатель: встречи ───────────────────────────────────────────────────

def test_ingest_meeting_kpis(tmp_path, monkeypatch):
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    n = ingest_meeting_kpis(
        "u1", "meet1",
        kpis=[{"name": "Выручка", "value": "4,2 млн ₽", "trend": "рост"},
              {"name": "Пустой", "value": "не знаю"}],   # без числа — скип
        enhanced_kpis=[{"metric_name": "NPS",
                        "values": {"current_value": "72",
                                   "target_value": "80"}}],
        at_iso="2026-06-15")
    assert n == 3           # Выручка-факт + NPS-факт + NPS-план (target)
    assert reg.series("NPS", kind="plan")[0]["value"] == 80.0
    v = reg.series("Выручка")[0]
    assert v["value"] == pytest.approx(4_200_000.0)
    assert v["source_type"] == "meeting" and v["source_id"] == "meet1"
    # повторный синк той же встречи не плодит дубли
    ingest_meeting_kpis("u1", "meet1",
                        [{"name": "Выручка", "value": "4,2 млн ₽"}], [],
                        at_iso="2026-06-15")
    assert len(reg.series("Выручка")) == 1


# ── питатель: датасеты ──────────────────────────────────────────────────

def test_ingest_dataset_metrics_monthly(tmp_path, monkeypatch):
    from backend.core.ontology.dataset_registry import deep_profile
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    columns = ["Дата", "Выручка, млн ₽"]
    rows = [
        {"Дата": "2026-05-10", "Выручка, млн ₽": "1.5"},
        {"Дата": "2026-05-20", "Выручка, млн ₽": "2.5"},
        {"Дата": "2026-06-05", "Выручка, млн ₽": "3"},
    ]
    rec = {
        "dataset_id": "d1", "title": "Финансы",
        "profile": deep_profile(columns, rows),
        "ontology": {"grounding": {
            "Выручка, млн ₽": {"known": True, "kpi": "Выручка"}}},
        "refreshed_at": "2026-07-01T00:00:00Z",
    }
    n = ingest_dataset_metrics("u1", rec, rows)
    assert n == 2                                   # 2026-05 и 2026-06
    pts = {p["period"]: p["value"] for p in reg.series("Выручка")}
    # масштаб «млн» применён: числа сопоставимы со встречами
    assert pts["2026-05"] == pytest.approx(4_000_000.0)
    assert pts["2026-06"] == pytest.approx(3_000_000.0)


def test_ingest_dataset_skips_ungrounded(tmp_path, monkeypatch):
    from backend.core.ontology.dataset_registry import deep_profile
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    columns = ["Что-то"]
    rows = [{"Что-то": "5"}]
    rec = {"dataset_id": "d2", "title": "X",
           "profile": deep_profile(columns, rows),
           "ontology": {"grounding": {"Что-то": {"known": False}}}}
    assert ingest_dataset_metrics("u1", rec, rows) == 0


# ── план/факт как first-class ───────────────────────────────────────────

def test_plan_point_and_completion(reg):
    reg.add_point("Выручка", 5_000_000, source_type="meeting",
                  source_id="m1", period="2026-06", kind="plan",
                  at="2026-06-01")
    reg.add_point("Выручка", 4_200_000, source_type="dataset",
                  source_id="d1", period="2026-06", at="2026-07-01")
    s = reg.summary("Выручка")
    assert s["plan"]["value"] == 5_000_000
    assert s["plan"]["completion_pct"] == pytest.approx(84.0)
    assert "выполнение 84" in s["plan"]["note"]
    assert s["plan"]["fact_source"] == "dataset"   # факт — приоритетно данные
    # план НЕ участвует в сверке «встреча vs данные» (нет meeting-факта)
    assert s["delta"] is None


def test_plan_from_enhanced_kpi_target(tmp_path, monkeypatch):
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    n = ingest_meeting_kpis(
        "u1", "meet1", kpis=[],
        enhanced_kpis=[{"metric_name": "Выручка", "time_period": "2026-06",
                        "values": {"current_value": "4,2 млн ₽",
                                   "target_value": "5 млн ₽"}}],
        at_iso="2026-06-15")
    assert n == 2                                   # факт + план
    plans = reg.series("Выручка", kind="plan")
    assert len(plans) == 1
    assert plans[0]["value"] == pytest.approx(5_000_000.0)
    s = reg.summary("Выручка")
    assert s["plan"]["completion_pct"] == pytest.approx(84.0)


def test_kind_column_migration(tmp_path):
    """БД, созданная до появления kind, мигрирует на открытии."""
    import sqlite3
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE metrics (metric_id TEXT PRIMARY KEY, name TEXT,
                name_norm TEXT UNIQUE, unit TEXT, category TEXT,
                created_at TEXT);
            CREATE TABLE metric_points (id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric_id TEXT, value REAL, period TEXT, at TEXT,
                source_type TEXT, source_id TEXT, detail TEXT,
                created_at TEXT);
        """)
        c.execute("INSERT INTO metrics VALUES ('x','NPS','nps',NULL,NULL,'t')")
        c.execute("INSERT INTO metric_points(metric_id,value,at,source_type,"
                  "created_at) VALUES ('x',72,'2026-06-01','meeting','t')")
    reg2 = MetricRegistry(db)
    pts = reg2.series("NPS")
    assert pts[0]["kind"] == "fact"     # старые точки — факты по умолчанию


# ── проактивные алерты на расхождение ───────────────────────────────────

def _seed_divergent(reg, name, meet_val, data_val, period="2026-06"):
    reg.add_point(name, meet_val, source_type="meeting", source_id="m1",
                  period=period, at="2026-06-20")
    reg.add_point(name, data_val, source_type="dataset", source_id="d1",
                  period=period, at="2026-07-01")


def test_divergences_thresholded_and_sorted(reg):
    _seed_divergent(reg, "Выручка", 5_000_000, 4_200_000)   # 16%
    _seed_divergent(reg, "NPS", 72, 71)                     # ~1.4%
    _seed_divergent(reg, "Лиды", 100, 50)                   # 50%
    reg.add_point("Одинокая", 5, source_type="meeting", source_id="m2")
    div = reg.divergences(threshold_pct=10)
    names = [s["metric"]["name"] for s in div]
    assert names == ["Лиды", "Выручка"]     # NPS ниже порога, сортировка по |Δ|


def test_divergence_insights_shape(tmp_path, monkeypatch):
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    _seed_divergent(reg, "Выручка", 5_000_000, 4_200_000)
    ins = mr.divergence_insights("u1", threshold_pct=10)
    assert len(ins) == 1
    i = ins[0]
    assert i["insight_type"] == "warning"
    assert i["source"] == "metric_divergence"
    assert "16" in i["title"] and "Выручка" in i["title"]
    assert i["priority"] == "medium"        # 16% < 25 → medium
    _seed_divergent(reg, "Лиды", 100, 50)
    ins2 = mr.divergence_insights("u1", threshold_pct=10)
    lead = [x for x in ins2 if x["metric"] == "Лиды"][0]
    assert lead["priority"] == "high"       # 50% ≥ 25 → high


def test_divergence_insights_stable_id_idempotent(tmp_path, monkeypatch):
    from backend.core.sleep.insight_store import stable_insight_id
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    _seed_divergent(reg, "Выручка", 5_000_000, 4_200_000)
    a = stable_insight_id(mr.divergence_insights("u1")[0])
    b = stable_insight_id(mr.divergence_insights("u1")[0])
    assert a == b       # повторный ночной прогон не задваивает алерт


# ── сквозной wow-сценарий ───────────────────────────────────────────────

def test_end_to_end_meeting_vs_table(tmp_path, monkeypatch):
    """На встрече заявили 5 млн за июнь; таблица показывает 4.2 млн →
    единая метрика ловит расхождение 16%."""
    from backend.core.ontology.dataset_registry import deep_profile
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    ingest_meeting_kpis(
        "u1", "meet1",
        kpis=[], enhanced_kpis=[{
            "metric_name": "Выручка", "time_period": "2026-06",
            "values": {"current_value": "5 млн ₽"}}],
        at_iso="2026-06-25")
    columns = ["Месяц", "Выручка"]
    rows = [{"Месяц": "2026-06-30", "Выручка": "4200000"}]
    rec = {"dataset_id": "d1", "title": "Отчёт",
           "profile": deep_profile(columns, rows),
           "ontology": {"grounding": {
               "Выручка": {"known": True, "kpi": "Выручка"}}},
           "refreshed_at": "2026-07-01T00:00:00Z"}
    ingest_dataset_metrics("u1", rec, rows)
    s = reg.summary("Выручка")
    assert s["delta"] is not None
    assert s["delta"]["pct"] == pytest.approx(-16.0)
    assert "РАСХОЖДЕНИЕ" in (s["note"] or "")


def test_dashboard_for_user_series_and_plan(tmp_path, monkeypatch):
    """B3: dashboard_for_user — компактные ряды по осям с раздельными сериями
    fact_dataset/fact_meeting/plan + delta сверки + выполнение плана."""
    import backend.core.ontology.metric_registry as mr
    from backend.core.ontology.metric_registry import (
        MetricRegistry, dashboard_for_user,
    )
    reg = MetricRegistry(str(tmp_path / "m.db"))
    monkeypatch.setattr(mr, "metrics_for_user", lambda uid: reg)
    reg.replace_source_points("dataset", "d1", [
        {"name": "Выручка", "value": 3_500_000, "unit": "₽", "period": "2026-04"},
        {"name": "Выручка", "value": 3_800_000, "unit": "₽", "period": "2026-05"},
    ])
    reg.replace_source_points("meeting", "m1", [
        {"name": "Выручка", "value": 4_200_000, "unit": "₽", "period": "2026-05"},
        {"name": "Выручка", "value": 5_000_000, "unit": "₽", "period": "2026-05",
         "kind": "plan"},
    ])
    db = dashboard_for_user("u")
    assert len(db) == 1
    m = db[0]
    xs = {p["x"]: p for p in m["series"]}
    assert xs["2026-04"]["fact_dataset"] == 3_500_000
    assert xs["2026-05"]["fact_meeting"] == 4_200_000
    assert xs["2026-05"]["plan"] == 5_000_000
    assert m["delta"] and m["plan"]["completion_pct"] == 76.0
