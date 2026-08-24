"""Unit-тесты для data lineage (P7).

Чистая логика: модуль не зависит от тяжёлого core.store, грузим
напрямую через importlib. Часы инъектируем — детерминизм без времени.
"""
from __future__ import annotations

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
    for pkg in ("backend", "backend.core", "backend.core.lineage"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[pkg] = m
    return _load("backend.core.lineage.lineage",
                 "backend/core/lineage/lineage.py")


_l = _bootstrap()

stamp_props = _l.stamp_props
append_event = _l.append_event
get_lineage = _l.get_lineage
trace = _l.trace
lineage_enabled_from_env = _l.lineage_enabled_from_env
LINEAGE_KEY = _l.LINEAGE_KEY


class _Clock:
    """Детерминированные растущие отметки времени."""
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"t{self.n}"


# === env-gate ===========================================================

def test_lineage_default_on(monkeypatch) -> None:
    # 2026-08-13: включено по умолчанию (аудит бизнес-карты) — задним
    # числом происхождение не восстановить, каждый день выключенной
    # трассы — день фактов без ответа «откуда это известно».
    monkeypatch.delenv("DATA_LINEAGE_ENABLED", raising=False)
    assert lineage_enabled_from_env() is True


def test_lineage_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setenv("DATA_LINEAGE_ENABLED", "off")
    assert lineage_enabled_from_env() is False


def test_lineage_env_on_variants(monkeypatch) -> None:
    for v in ("1", "true", "on", "yes", "enabled", "TRUE"):
        monkeypatch.setenv("DATA_LINEAGE_ENABLED", v)
        assert lineage_enabled_from_env() is True
    monkeypatch.setenv("DATA_LINEAGE_ENABLED", "nope")
    assert lineage_enabled_from_env() is False


# === stamp_props: первый stamp ==========================================

def test_first_stamp_sets_record_and_mirrors() -> None:
    clk = _Clock()
    out = stamp_props({"name": "X"}, created_by="extractor",
                      pipeline_id="mtg_1", stage="extract", clock=clk)
    rec = out[LINEAGE_KEY]
    assert rec["created_by"] == "extractor"
    assert rec["pipeline_id"] == "mtg_1"
    assert rec["origin_stage"] == "extract"
    assert rec["created_at"] == "t1"
    assert len(rec["history"]) == 1
    # плоские зеркала
    assert out["created_by"] == "extractor"
    assert out["pipeline_id"] == "mtg_1"
    # исходные props не мутированы
    assert "name" in out


def test_stamp_returns_new_dict_not_mutating_input() -> None:
    src = {"a": 1}
    out = stamp_props(src, created_by="x", pipeline_id="p", stage="s")
    assert LINEAGE_KEY not in src
    assert LINEAGE_KEY in out


# === идемпотентность ====================================================

def test_second_stamp_keeps_origin_appends_history() -> None:
    clk = _Clock()
    a = stamp_props({}, created_by="extractor", pipeline_id="mtg_1",
                    stage="extract", clock=clk)
    b = stamp_props(a, created_by="consolidator", pipeline_id="mtg_2",
                    stage="consolidate", op="merge", clock=clk)
    rec = b[LINEAGE_KEY]
    # «кто создал» неизменно
    assert rec["created_by"] == "extractor"
    assert rec["pipeline_id"] == "mtg_1"
    assert rec["origin_stage"] == "extract"
    assert rec["created_at"] == "t1"
    # но история накапливается
    assert len(rec["history"]) == 2
    assert rec["history"][1]["actor"] == "consolidator"
    assert rec["history"][1]["op"] == "merge"
    assert rec["history"][1]["ts"] == "t2"


# === bounded история ====================================================

def test_history_is_bounded() -> None:
    clk = _Clock()
    p: dict = {}
    p = stamp_props(p, created_by="c", pipeline_id="pp", stage="s0",
                    clock=clk)
    for i in range(50):
        p = append_event(p, stage=f"s{i}", actor="worker",
                         op="transform", clock=clk)
    hist = trace(p)
    assert len(hist) <= 25  # _MAX_HISTORY
    # последнее событие сохранено (держим хвост)
    assert hist[-1]["stage"] == "s49"


# === append_event без предшествующего stamp =============================

def test_append_event_bootstraps_lineage() -> None:
    clk = _Clock()
    out = append_event({"k": "v"}, stage="enrich", actor="svc",
                       op="update", clock=clk)
    rec = get_lineage(out)
    assert rec.created_by == "svc"
    assert rec.history and rec.history[0]["op"] == "update"


# === robustness =========================================================

def test_get_lineage_on_garbage_is_safe() -> None:
    assert get_lineage(None).created_by == ""           # type: ignore[arg-type]
    assert get_lineage({"_lineage": "broken"}).history == []
    assert get_lineage({}).pipeline_id == ""


def test_stamp_non_dict_returns_input() -> None:
    assert stamp_props("nope", created_by="a", pipeline_id="b",  # type: ignore[arg-type]
                       stage="s") == "nope"


def test_trace_returns_ordered_history() -> None:
    clk = _Clock()
    p = stamp_props({}, created_by="c", pipeline_id="p", stage="s1",
                    clock=clk)
    p = append_event(p, stage="s2", actor="a2", op="o2", clock=clk)
    p = append_event(p, stage="s3", actor="a3", op="o3", clock=clk)
    ts = [e["ts"] for e in trace(p)]
    assert ts == ["t1", "t2", "t3"]


def test_record_to_dict_shape() -> None:
    p = stamp_props({}, created_by="c", pipeline_id="pid", stage="st")
    d = get_lineage(p).to_dict()
    assert set(d) == {"created_by", "pipeline_id", "origin_stage",
                      "created_at", "history"}
    assert d["pipeline_id"] == "pid"
