# -*- coding: utf-8 -*-
"""Объём и наклон памяти + цена за правильный ответ.

Два разрыва из разбора инженерии памяти:
(1) объём памяти не измерялся вовсе — ни числа, ни наклона, а политика
забывания без измерения невозможна; (2) точность публиковалась без цены.

Контракты:
  1. замер считает узлы/связи по типам и байты из реального формата графа;
  2. пустой/битый файл → нули, не исключение;
  3. снимки копятся с капом, наклон считается против снимка нужной давности;
  4. мало снимков → отчёт честно говорит «истории нет», не рисует нули;
  5. лидеры роста — только реально выросшие типы;
  6. цена за правильный ответ считается из сохранённых прогонов, «ноль
     правильных» → честная бесконечность, а не деление на ноль;
  7. харнесс пишет usage в результат прогона; снимок вплетён в ночную
     консолидацию; документ называет цену рядом с точностью.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stub_tenant(tmp):
    """Стаб tenant_paths + tenant_io на временную папку."""
    tp = types.ModuleType("backend.core.store.tenant_paths")
    tp._DATA_ROOT = tmp

    def _p(name):
        def f(user_id):
            return os.path.join(tmp, f"{name}_{user_id}.json")
        return f
    tp.graph_path_for_user = _p("graph")
    tp.supersede_store_path_for_user = _p("supersede")
    tp.event_log_path_for_user = _p("events")
    tp.cross_source_mentions_path_for_user = _p("mentions")
    tp.vector_index_path_for_user = _p("vectors")
    sys.modules["backend.core.store.tenant_paths"] = tp

    tio = types.ModuleType("backend.core.store.tenant_io")

    class _L:
        def __init__(self, *_a, **_k): ...

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _aw(path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    tio.file_lock, tio.atomic_write_json = _L, _aw
    sys.modules["backend.core.store.tenant_io"] = tio


def _load_fp(tmp):
    _stub_tenant(tmp)
    for pkg in ("backend", "backend.core", "backend.core.store"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = m
    name = "backend.core.store.memory_footprint"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend/core/store/memory_footprint.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_graph(tmp, uid, nodes, edges):
    with open(os.path.join(tmp, f"graph_{uid}.json"), "w",
              encoding="utf-8") as f:
        json.dump({"nodes": nodes, "edges": edges}, f, ensure_ascii=False)


def _src(relpath):
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        return f.read()


# ── 1-2. Замер ──────────────────────────────────────────────────────────

def test_measure_counts_real_graph_format():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _load_fp(tmp)
        _write_graph(tmp, "u1",
                     [{"id": "1", "label": "Person"},
                      {"id": "2", "label": "Person"},
                      {"id": "3", "label": "Task"}],
                     [{"source": "1", "target": "3", "key": 0,
                       "data": {"type": "ASSIGNED_TO"}}])
        with open(os.path.join(tmp, "supersede_u1.json"), "w") as f:
            json.dump({"records": [{"a": 1}, {"a": 2}]}, f)
        s = fp.measure_user("u1")
        assert s["nodes_total"] == 3 and s["edges_total"] == 1
        assert s["nodes_by_label"]["Person"] == 2
        assert s["edges_by_type"]["ASSIGNED_TO"] == 1
        assert s["supersede_records"] == 2
        assert s["total_bytes"] > 0
    print("✅ замер считает узлы, связи, версии и байты из реального формата")


def test_missing_or_broken_graph_gives_zeros_not_crash():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _load_fp(tmp)
        s = fp.measure_user("нет-такого")
        assert s["nodes_total"] == 0 and s["total_bytes"] == 0
        with open(os.path.join(tmp, "graph_u2.json"), "w") as f:
            f.write("{битый json")
        s2 = fp.measure_user("u2")
        assert s2["nodes_total"] == 0
    print("✅ пустой или битый файл — нули, не падение")


# ── 3-5. История и наклон ───────────────────────────────────────────────

def test_history_caps_and_growth_uses_dated_baseline():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _load_fp(tmp)
        now = datetime.now(timezone.utc)
        hist = []
        # 10 дней назад: 100 узлов; вчера: 140; сегодня: 150
        for days, nodes in ((10, 100), (1, 140), (0, 150)):
            hist.append({"at": (now - timedelta(days=days)).isoformat(),
                         "nodes_total": nodes, "edges_total": nodes * 2,
                         "supersede_records": 5, "total_bytes": nodes * 10,
                         "nodes_by_label": {"Task": nodes // 2,
                                            "Person": 10}})
        with open(fp._history_path("u1"), "w", encoding="utf-8") as f:
            json.dump({"snapshots": hist}, f)
        rep = fp.growth_report("u1")
        g7 = rep["growth"].get("7d")
        assert g7 and g7["nodes_total"]["was"] == 100, (
            "рост за 7 дней обязан считаться против снимка ~7+ дней давности"
        )
        assert g7["nodes_total"]["delta"] == 50
        # лидер роста — Task (+25), Person не рос
        labels = [x["label"] for x in rep["fastest_growing"]]
        assert "Task" in labels and "Person" not in labels
    print("✅ наклон против снимка нужной давности; лидеры — только росшие")


def test_no_history_is_honest_not_zero():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _load_fp(tmp)
        _write_graph(tmp, "u1", [{"id": "1", "label": "Person"}], [])
        rep = fp.growth_report("u1")
        assert rep["growth"] == {} and "истории ещё нет" in rep["note"], (
            "«нет данных» не должно читаться как «не растёт»"
        )
    print("✅ мало снимков — честное «истории нет», не нули")


def test_snapshot_cap():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _load_fp(tmp)
        _write_graph(tmp, "u1", [], [])
        fp.MAX_SNAPSHOTS = 5
        for _ in range(8):
            fp.record_snapshot("u1")
        assert len(fp.load_history("u1")) == 5, (
            "история снимков не должна сама стать проблемой объёма"
        )
    print("✅ история снимков ограничена капом")


# ── 6. Цена за правильный ответ ─────────────────────────────────────────

def test_cost_per_correct_from_real_run_shape():
    spec = importlib.util.spec_from_file_location(
        "cpc", os.path.join(ROOT, "scripts/cost_per_correct.py"))
    cpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cpc)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "benchmark_x.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"model": "m", "n": 10, "summary": {
                "good": {"accuracy": 0.8},
                "none": {"accuracy": 0.0},
            }}, f)
        lines = "\n".join(cpc.analyze(p))
        assert "2.5" in lines, "0.8 на 10 вопросах → 20/8 = 2.5 вызова"
        assert "∞" in lines, "ноль правильных — честная бесконечность"
    print("✅ цена за правильный ответ: считается, ноль — бесконечность")


# ── 7. Проводка ─────────────────────────────────────────────────────────

def test_harness_records_usage():
    src = _src("backend/core/eval/benchmark_longmemeval.py")
    assert "_count_usage" in src and '"usage": dict(USAGE)' in src, (
        "харнесс обязан писать токены — иначе следующий прогон опять без цены"
    )
    assert "promptTokenCount" in src, "gemini-формат usage тоже учитывается"
    print("✅ харнесс пишет usage в результат прогона")


def test_nightly_takes_snapshot_and_metrics_exist():
    nc = _src("backend/core/corrections/nightly_consolidation.py")
    assert "record_snapshot" in nc, "снимок обязан быть в ночном цикле"
    idx_decay = nc.index("_apply_temporal_decay(user_id)")
    assert nc.index("record_snapshot") > idx_decay, (
        "меряем ПОСЛЕ затухания — то, что реально осталось"
    )
    m = _src("backend/core/observability/metrics.py")
    for g in ("memory_nodes_gauge", "memory_edges_gauge",
              "memory_bytes_gauge"):
        assert g in m, f"gauge {g} обязан существовать"
    print("✅ снимок в ночном цикле после затухания; gauge заведены")


def test_doc_pairs_accuracy_with_cost():
    doc = _src("docs/ru/PRODUCT_CAPABILITIES.md")
    assert "Цена за правильный ответ" in doc
    assert "точность не называется без цены" in doc
    assert "не записывали токены" in doc, (
        "оговорка про вызовы-вместо-денег обязана быть в документе"
    )
    assert "Объём памяти измеряется" in doc
    print("✅ документ: цена рядом с точностью, наклон памяти назван")




# ── 8. Совместимый путь харнесса (замер в закрытом контуре) ─────────────

def test_harness_supports_openai_compatible_endpoint():
    """Без этого пути вопрос «сколько качества теряет закрытый контур»
    не измерить: харнесс говорил только с OpenAI и Gemini, а локальная
    модель (vLLM/Ollama) отвечает по OpenAI-совместимому адресу."""
    src = _src("backend/core/eval/benchmark_longmemeval.py")
    assert "def _llm_compat" in src
    fn = src[src.index("def _llm_compat"):src.index("def _llm(")]
    assert "LME_COMPAT_BASE_URL" in fn and "chat/completions" in fn
    assert "_count_usage" in fn, (
        "совместимый путь обязан считать токены — иначе цена снова "
        "останется без денег"
    )
    # приоритет: заданный адрес побеждает автоопределение по имени модели
    disp = src[src.index("def _llm(") : src.index("_ANSWER_SYSTEM")]
    assert disp.index("LME_COMPAT_BASE_URL") < disp.index("startswith(\"gemini\")"), (
        "явно заданный совместимый адрес сильнее эвристики по имени"
    )
    print("✅ харнесс умеет OpenAI-совместимый адрес (vLLM/Ollama/провайдеры)")




# ── 9. Защита от завышения: вырожденные ответы ──────────────────────────

def test_degenerate_answers_detected():
    """Ловушка живого прогона: модель выдала леса промпта вместо ответа,
    судья засчитал их верными, рука «без памяти» получила 0.40 при
    fallbacks=0. Молчаливое завышение опаснее падения — падение видно."""
    import sys as _s
    _s.path.insert(0, ROOT)
    from backend.core.eval.benchmark_longmemeval import _looks_degenerate as dg
    # леса промпта в разных формах
    assert dg('*   Question: "How much did I earn?"\n * Constraint 1: ...')
    assert dg('User Question: что купили?\nConstraint: коротко')
    assert dg('')
    assert dg('(llm_error: timeout)')
    # ответ, который просто повторяет вопрос
    assert dg('Сколько мы потратили на подрядчиков?',
              'Сколько мы потратили на подрядчиков?')
    # настоящие ответы НЕ помечаются
    for good in ('$420', 'NOT MENTIONED.',
                 'Вы работаете 50 часов в неделю в пик сезона.',
                 'Aerial yoga and kundalini yoga.'):
        assert not dg(good, 'How many hours do you work?'), good
    print("✅ вырожденный ответ отличается от настоящего")


def test_invalid_run_excluded_from_price():
    """Недействительный прогон не должен давать «дешёвый правильный ответ»."""
    spec = importlib.util.spec_from_file_location(
        "cpc2", os.path.join(ROOT, "scripts/cost_per_correct.py"))
    cpc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cpc)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "benchmark_bad.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"model": "m", "n": 10, "INVALID": True,
                       "invalid_reason": "леса промпта вместо ответов",
                       "summary": {"none": {"accuracy": 0.9}}}, f)
        out = "\n".join(cpc.analyze(p))
        assert "НЕДЕЙСТВИТЕЛЕН" in out and "0.9" not in out, (
            "числа недействительного прогона не должны попасть в отчёт"
        )
        # и отдельная рука, помеченная невалидной, пропускается
        p2 = os.path.join(tmp, "benchmark_mixed.json")
        with open(p2, "w", encoding="utf-8") as f:
            json.dump({"model": "m", "n": 10, "summary": {
                "good": {"accuracy": 0.5, "valid": True},
                "bad": {"accuracy": 0.99, "valid": False, "degenerate": 9}}}, f)
        out2 = "\n".join(cpc.analyze(p2))
        assert "рука невалидна" in out2 and "0.990" not in out2
    print("✅ невалидные прогон и рука не участвуют в цене")


def test_harness_marks_arm_validity():
    src = _src("backend/core/eval/benchmark_longmemeval.py")
    assert '"degenerate": degen[a]' in src and '"valid":' in src, (
        "сводка обязана нести признак валидности руки, а не только точность"
    )
    assert "ВЫРОЖДЕННЫХ ОТВЕТОВ" in src, "вывод обязан кричать об этом"
    print("✅ харнесс помечает руку невалидной прямо в сводке")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты объёма памяти и цены прошли.")
