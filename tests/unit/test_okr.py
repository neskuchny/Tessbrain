# -*- coding: utf-8 -*-
"""OKR-каркас: ключевые результаты, оценка 0..1, квартал, грейды, чекины.

Достроено по решению владельца продукта. До этого цель была плоской
записью с одним progress 0..100, прогресс считался по закрытым задачам, а
поле metric было мёртвой строкой. Контракты под проверкой:

  1. KR без данных → None, а не 0: «не мерили» ≠ «ноль прогресса»;
  2. метричный KR с target == start некорректен и оценки не имеет;
  3. committed-цель требует 100%, aspirational — 70% (шкала метода);
  4. оценка цели выводится из KR и не принимается с входа руками;
  5. чекин обновляет KR и фиксирует снимок оценок;
  6. закрытие квартала грейдит по правилам и закрывает цели.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, relpath: str, pkgs):
    if name in sys.modules:
        return sys.modules[name]
    for pkg in pkgs:
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_okr = _load("backend.core.goals.okr", "backend/core/goals/okr.py",
             ("backend", "backend.core", "backend.core.goals"))


# ── Скоринг KR ──────────────────────────────────────────────────────────

def test_metric_kr_scores_by_data():
    kr = _okr.normalize_kr({"title": "Выручка", "kind": "metric",
                            "start": 2, "target": 4, "current": 3})
    assert _okr.score_kr(kr) == 0.5
    kr["current"] = 5
    assert _okr.score_kr(kr) == 1.0, "перевыполнение зажимается в 1.0"
    kr["current"] = 1
    assert _okr.score_kr(kr) == 0.0, "движение назад — 0, не отрицательное"
    print("✅ метричный KR: (current−start)/(target−start), зажат в 0..1")


def test_unmeasured_kr_is_none_not_zero():
    kr = _okr.normalize_kr({"title": "Отток", "kind": "metric",
                            "start": 10, "target": 5})
    assert _okr.score_kr(kr) is None, "«не мерили» не равно «ноль прогресса»"
    print("✅ KR без current → None, а не 0")


def test_degenerate_metric_kr_has_no_score():
    kr = _okr.normalize_kr({"title": "Х", "kind": "metric",
                            "start": 5, "target": 5, "current": 5})
    assert _okr.score_kr(kr) is None, (
        "цель «из 5 в 5» не измеряет ничего — оценки быть не должно"
    )
    print("✅ target == start → некорректный KR без оценки")


def test_binary_and_milestone_kr():
    assert _okr.score_kr(_okr.normalize_kr(
        {"title": "Запустить", "kind": "binary", "done": True})) == 1.0
    assert _okr.score_kr(_okr.normalize_kr(
        {"title": "Запустить", "kind": "binary"})) == 0.0
    m = _okr.normalize_kr({"title": "Этап", "kind": "milestone",
                           "fraction": 0.6})
    assert _okr.score_kr(m) == 0.6
    print("✅ binary и milestone считаются")


def test_goal_score_averages_only_measured():
    krs = [
        _okr.normalize_kr({"title": "A", "kind": "binary", "done": True}),
        _okr.normalize_kr({"title": "B", "kind": "metric",
                           "start": 0, "target": 10, "current": 5}),
        _okr.normalize_kr({"title": "C", "kind": "metric",
                           "start": 0, "target": 10}),  # не мерили
    ]
    assert _okr.score_goal(krs) == 0.75, "среднее по ОЦЕНЁННЫМ: (1+0.5)/2"
    assert _okr.score_goal([]) is None
    print("✅ оценка цели — среднее по оценённым KR")


# ── Грейды ──────────────────────────────────────────────────────────────

def test_committed_requires_full():
    assert _okr.grade_goal(1.0, "committed")["grade"] == "done"
    g = _okr.grade_goal(0.9, "committed")
    assert g["grade"] == "missed", "обязательная цель на 90% — не выполнена"
    print("✅ committed требует 100%")


def test_aspirational_ok_at_70():
    assert _okr.grade_goal(0.7, "aspirational")["grade"] == "done"
    assert _okr.grade_goal(0.5, "aspirational")["grade"] == "progress"
    assert _okr.grade_goal(0.2, "aspirational")["grade"] == "missed"
    print("✅ aspirational: 0.7 — успех (шкала метода)")


def test_unmeasured_goal_grade_is_honest():
    g = _okr.grade_goal(None, "committed")
    assert g["grade"] == "unmeasured"
    print("✅ цель без оценённых KR грейдится как «не измерено»")


# ── Кварталы ────────────────────────────────────────────────────────────

def test_quarter_bounds():
    b = _okr.quarter_bounds("2026-Q3")
    assert b == {"start": "2026-07-01", "end": "2026-09-30"}
    assert _okr.quarter_bounds("2026-Q1")["end"] == "2026-03-31"
    assert _okr.quarter_bounds("мусор") is None
    assert _okr.quarter_bounds("2026-Q5") is None
    print("✅ границы квартала считаются, мусор отвергается")


# ── Хранилище: чекины и закрытие цикла ──────────────────────────────────

def _stub_tenant_io():
    """file_lock/atomic_write_json без пакета store: его __init__ тянет
    numpy, которого нет в песочнице. Логика хранилища от этого не зависит."""
    import contextlib
    if "backend.core.store" not in sys.modules:
        pkg = types.ModuleType("backend.core.store")
        pkg.__path__ = [os.path.join(ROOT, "backend", "core", "store")]
        sys.modules["backend.core.store"] = pkg
    tio = types.ModuleType("backend.core.store.tenant_io")

    @contextlib.contextmanager
    def file_lock(path):
        yield

    def atomic_write_json(path, data):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    tio.file_lock = file_lock
    tio.atomic_write_json = atomic_write_json
    sys.modules["backend.core.store.tenant_io"] = tio


def _store():
    _stub_tenant_io()
    gt = _load("backend.core.goals.goal_tracker",
               "backend/core/goals/goal_tracker.py",
               ("backend", "backend.core", "backend.core.goals"))
    path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    os.unlink(path)
    return gt.GoalStore(path)


def test_goal_with_krs_gets_score_and_cycle():
    s = _store()
    g = s.add_goal(title="Рост выручки", level="company",
                   key_results=[{"title": "Выручка", "kind": "metric",
                                 "start": 2, "target": 4, "current": 2}],
                   commitment="aspirational")
    assert g["score"] == 0.0
    assert g["cycle"], "цель с KR получает текущий квартал по умолчанию"
    assert g["commitment"] == "aspirational"
    print("✅ цель с KR: оценка и квартал проставлены")


def test_score_is_derived_not_accepted():
    """Оценку нельзя нарисовать руками — она выводится из KR."""
    s = _store()
    g = s.add_goal(title="Х", level="company",
                   key_results=[{"title": "A", "kind": "binary"}])
    s.update_goal(g["id"], {"score": 0.9})
    got = s.get_goal(g["id"])
    assert got["score"] == 0.0, "score с входа должен игнорироваться"
    print("✅ оценка выводится из KR, руками не принимается")


def test_checkin_updates_kr_and_records():
    s = _store()
    g = s.add_goal(title="Рост", level="company",
                   key_results=[{"title": "Выручка", "kind": "metric",
                                 "start": 0, "target": 10, "current": 0}])
    res = s.add_checkin(g["id"], author="u-1", note="идём по плану",
                        confidence=0.8,
                        kr_updates=[{"title": "Выручка", "current": 5}])
    assert res["goal"]["score"] == 0.5
    assert res["goal"]["progress"] == 50, "progress следует за score"
    ch = res["checkin"]
    assert ch["author"] == "u-1" and ch["confidence"] == 0.8
    assert ch["goal_score"] == 0.5
    assert len(res["goal"]["checkins"]) == 1
    print("✅ чекин обновляет KR и фиксирует снимок оценок")


def test_close_cycle_grades_and_closes():
    s = _store()
    a = s.add_goal(title="Обязательная", level="company", cycle="2026-Q3",
                   commitment="committed",
                   key_results=[{"title": "A", "kind": "binary", "done": True}])
    b = s.add_goal(title="Амбициозная", level="company", cycle="2026-Q3",
                   commitment="aspirational",
                   key_results=[{"title": "B", "kind": "metric",
                                 "start": 0, "target": 10, "current": 8}])
    c = s.add_goal(title="Другой квартал", level="company", cycle="2026-Q4",
                   key_results=[{"title": "C", "kind": "binary"}])
    out = s.close_cycle("2026-Q3")
    assert len(out["closed"]) == 2
    grades = {x["title"]: x["grade"] for x in out["closed"]}
    assert grades == {"Обязательная": "done", "Амбициозная": "done"}
    assert out["avg_score"] == 0.9
    assert s.get_goal(a["id"])["status"] == "closed"
    assert s.get_goal(c["id"])["status"] == "active", "чужой квартал не тронут"
    print("✅ закрытие квартала: грейды по правилам, чужой цикл не тронут")


def test_goal_without_krs_keeps_old_behavior():
    """Обратная совместимость: старые цели живут как раньше."""
    s = _store()
    g = s.add_goal(title="Старая цель", level="person")
    assert g["score"] is None
    assert g["key_results"] == []
    assert g["cycle"] == "", "без KR квартал не навязывается"
    s.update_goal(g["id"], {"progress": 40})
    assert s.get_goal(g["id"])["progress"] == 40, (
        "ручной прогресс без KR остаётся ручным"
    )
    print("✅ цели без KR работают по-старому")


def test_scheduler_and_routes_wired():
    src = open(os.path.join(ROOT, "backend/core/automations/scheduler.py"),
               encoding="utf-8").read()
    assert 'id="goals_weekly_snapshot"' in src, (
        "недельный снимок целей обязан стоять в расписании"
    )
    routes = open(os.path.join(ROOT, "backend/api/routes/goals.py"),
                  encoding="utf-8").read()
    for h in ("goal_checkin", "current_cycle", "close_cycle_endpoint"):
        assert h in routes.split("router = Router(")[1], f"{h} не зарегистрирован"
    print("✅ снимок в расписании, чекины и циклы в API")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты OKR прошли.")
