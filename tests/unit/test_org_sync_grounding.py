# -*- coding: utf-8 -*-
"""Сверка синхронизации: заземление конфликтов и регулярность прогона.

Два дефекта, найденные при разборе раздела «Руководителю»:

1. В четырёхуровневом отчёте поле `grounded` не выставлялось НИКОГДА:
   `org_sync_report` звал голый `find_desyncs()` без списка известных KPI
   компании. Значит «high» в отчёте означал лишь противоположные
   слова-направления в формулировках целей («увеличить» против
   «сократить»), а не подтверждённый фактами графа конфликт. Руководитель
   читает «high» как «система нашла реальное расхождение» — разница
   принципиальная, и docstring модуля обещал именно заземление.

2. Отчёт считался только когда руководитель открывал дашборд. Обещание
   «расхождение ловится в ту неделю, когда возникло» держалось на том,
   что кто-то зайдёт. Теперь сверка стоит в расписании, и уведомления
   сторонам уходят сами.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _load_detector():
    name = "backend.core.think.cross_department_desync"
    if name in sys.modules:
        return sys.modules[name]
    for pkg in ("backend", "backend.core", "backend.core.think"):
        if pkg not in sys.modules:
            mod = types.ModuleType(pkg)
            mod.__path__ = [os.path.join(ROOT, *pkg.split("."))]
            sys.modules[pkg] = mod
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "backend", "core", "think",
                           "cross_department_desync.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_d = _load_detector()

_GOALS = {
    "Продажи": ["Увеличить нагрузку на поддержку за счёт роста продаж"],
    "Операции": ["Сократить нагрузку на поддержку вдвое"],
}


# ── 1. Заземление ───────────────────────────────────────────────────────

def test_conflict_detected_without_kpis_is_not_grounded():
    """Без известных KPI конфликт остаётся гипотезой по словам."""
    out = _d.CrossDepartmentDesyncDetector().detect(_GOALS)
    assert out, "противоположные направления по общей метрике должны ловиться"
    assert all(not c.grounded for c in out), (
        "без списка KPI компании конфликт не может считаться заземлённым"
    )
    print("✅ без KPI конфликт найден, но помечен как незаземлённый")


def test_conflict_with_known_kpi_is_grounded():
    """Метрика конфликта — известный показатель компании → заземлено."""
    shared = set()
    for c in _d.CrossDepartmentDesyncDetector().detect(_GOALS):
        shared |= set(c.shared or [])
    assert shared, "детектор должен возвращать общие сигнатуры метрик"
    out = _d.CrossDepartmentDesyncDetector(known_kpis=shared).detect(_GOALS)
    assert any(c.grounded for c in out), (
        "конфликт по известному KPI обязан апгрейдиться до заземлённого"
    )
    print("✅ конфликт по известному KPI помечается заземлённым")


def test_report_passes_known_kpis():
    """Главный дефект: отчёт звал детектор без KPI."""
    src = _src("backend/core/think/org_sync.py")
    start = src.index("# L2: отдел ↔ отдел")
    block = src[start:start + 2200]
    assert "CrossDepartmentDesyncDetector" in block, (
        "отчёт снова считает конфликты без заземления по KPI"
    )
    assert "known_kpis" in block
    assert "prior_from_graph_nodes" in block, "KPI берутся из графа"
    # Падение заземления не должно ронять отчёт целиком
    assert "cross_list = find_desyncs(dept_texts)" in block, (
        "нужен фолбэк: недоступный граф не отменяет сверку"
    )
    print("✅ отчёт заземляет конфликты по KPI графа, с фолбэком")


# ── 2. Регулярность ─────────────────────────────────────────────────────

def test_sync_job_registered_in_scheduler():
    src = _src("backend/core/automations/scheduler.py")
    assert 'id="org_sync_weekly"' in src, (
        "сверка синхронизации обязана стоять в расписании, "
        "а не считаться только при открытии дашборда"
    )
    assert "ORG_SYNC_CRON" in src, "расписание должно настраиваться"
    assert "async def _org_sync_job" in src
    print("✅ сверка синхронизации стоит в расписании")


def test_sync_job_respects_flag_and_notifies():
    src = _src("backend/core/automations/scheduler.py")
    start = src.index("async def _org_sync_job")
    body = src[start:src.index("async def _execution_pulse_job", start)]
    assert "cross_dept_desync_enabled" in body, "тот же гейт, что у эндпоинтов"
    assert "notify=True" in body, "уведомления сторонам обязаны уходить"
    assert "judge=False" in body, (
        "судья платный — регулярный прогон должен быть детерминированным"
    )
    print("✅ джоба уважает флаг, уведомляет и не тратит модель")


# ── 3. Честность индекса сохранена ──────────────────────────────────────

def test_empty_input_gives_no_index():
    """Пустота не превращается в идеальную синхронность."""
    src = _src("backend/core/think/org_sync.py")
    start = src.index("def sync_index(")
    body = src[start:start + 2000]
    assert "None" in body, "компонент без данных обязан быть None"
    assert "note" in src, "правило должно объясняться текстом в ответе"
    print("✅ при нехватке данных индекс не выдумывается")


if __name__ == "__main__":
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("\nВсе тесты сверки синхронизации прошли.")
