# -*- coding: utf-8 -*-
"""Объём и наклон памяти: видно, что растёт, — можно решать, что отпустить.

Разрыв из разбора инженерии памяти (Stanford/Microsoft): долгоживущего
агента разоряет не размер хранилища, а НАКЛОН его роста — и ни одна из
типовых систем не измеряет его по умолчанию. У нас было то же самое:
очистка трогала только служебное (результаты валидаций, заявки
исполнителей), а объём самой памяти — узлы, связи, версии — не измерялся
вовсе: ни числа, ни порога, ни сигнала «за месяц выросло вдвое».

Политика забывания без измерения невозможна в принципе: нечего решать.
Этот модуль — измерение. Он НЕ удаляет ничего сам: затухание связей
живёт в ночной консолидации со своими guardrails (структурные связи не
затухают), а решения об остальном — за человеком, которому теперь есть
на что смотреть.

Что считается (на пользователя/организацию):
  - граф: узлы по типам, связи по типам, размер файла;
  - версии фактов (supersede), события, упоминания, векторный индекс —
    размеры и количества из их JSON-хранилищ;
  - снимок пишется в историю (кап 400 ≈ год ежедневных) → наклон:
    рост за 7 и 30 дней, и кто растёт быстрее всех.

Чистые функции + файловая история, без LLM и без сети.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SNAPSHOTS = 400
TOP_LABELS = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _count_json_items(path: str, *keys: str) -> int:
    """Число элементов в JSON-хранилище по первому подходящему ключу."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return 0
    if isinstance(d, list):
        return len(d)
    if isinstance(d, dict):
        for k in keys:
            v = d.get(k)
            if isinstance(v, (list, dict)):
                return len(v)
    return 0


def measure_user(user_id: str) -> Dict[str, Any]:
    """Снимок объёма памяти одного пользователя/организации. Не raises."""
    from backend.core.store.tenant_paths import (
        cross_source_mentions_path_for_user,
        event_log_path_for_user,
        graph_path_for_user,
        supersede_store_path_for_user,
        vector_index_path_for_user,
    )
    snap: Dict[str, Any] = {
        "at": _now(),
        "nodes_total": 0, "edges_total": 0,
        "nodes_by_label": {}, "edges_by_type": {},
        "graph_bytes": 0, "supersede_records": 0, "supersede_bytes": 0,
        "events": 0, "mentions": 0, "vector_bytes": 0,
        "total_bytes": 0,
    }
    try:
        gp = graph_path_for_user(user_id)
    except Exception:
        return snap

    snap["graph_bytes"] = _size(gp)
    try:
        with open(gp, encoding="utf-8") as f:
            g = json.load(f)
        nodes = g.get("nodes") or []
        edges = g.get("edges") or []
        snap["nodes_total"] = len(nodes)
        snap["edges_total"] = len(edges)
        by_label: Dict[str, int] = {}
        for n in nodes:
            lbl = str(n.get("label") or n.get("type") or "?")
            by_label[lbl] = by_label.get(lbl, 0) + 1
        by_type: Dict[str, int] = {}
        for e in edges:
            et = str((e.get("data") or {}).get("type")
                     or (e.get("data") or {}).get("relation")
                     or e.get("key") or "?")
            by_type[et] = by_type.get(et, 0) + 1
        # только верхушка — иначе снимок сам станет проблемой объёма
        snap["nodes_by_label"] = dict(sorted(
            by_label.items(), key=lambda x: -x[1])[:TOP_LABELS])
        snap["edges_by_type"] = dict(sorted(
            by_type.items(), key=lambda x: -x[1])[:TOP_LABELS])
    except Exception:
        logger.debug("footprint: graph read failed", exc_info=True)

    try:
        sp = supersede_store_path_for_user(user_id)
        snap["supersede_bytes"] = _size(sp)
        snap["supersede_records"] = _count_json_items(
            sp, "records", "items", "facts")
        snap["events"] = _count_json_items(
            event_log_path_for_user(user_id), "events", "items")
        snap["mentions"] = _count_json_items(
            cross_source_mentions_path_for_user(user_id),
            "mentions", "items")
        snap["vector_bytes"] = _size(vector_index_path_for_user(user_id))
    except Exception:
        logger.debug("footprint: aux stores read failed", exc_info=True)

    snap["total_bytes"] = (snap["graph_bytes"] + snap["supersede_bytes"]
                           + snap["vector_bytes"])
    return snap


# ── История снимков и наклон ────────────────────────────────────────────

def _history_path(user_id: str) -> str:
    try:
        from backend.core.store.tenant_paths import _DATA_ROOT
        root = str(_DATA_ROOT)
    except Exception:
        root = os.getenv("TESSENT_DATA_DIR", "data")
    safe = "".join(c for c in str(user_id) if c.isalnum() or c in "-_")
    d = os.path.join(root, "memory_footprint")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe or 'default'}.json")


def record_snapshot(user_id: str,
                    snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Замерить и дописать снимок в историю. Возвращает снимок."""
    snap = snap or measure_user(user_id)
    path = _history_path(user_id)
    try:
        from backend.core.store.tenant_io import atomic_write_json, file_lock
        with file_lock(path):
            history: List[dict] = []
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        history = list(json.load(f).get("snapshots") or [])
                except Exception:
                    history = []
            history.append(snap)
            atomic_write_json(path, {"snapshots": history[-MAX_SNAPSHOTS:]})
    except Exception:
        logger.warning("footprint: history write failed", exc_info=True)
    _export_gauges(user_id, snap)
    return snap


def load_history(user_id: str) -> List[dict]:
    try:
        with open(_history_path(user_id), encoding="utf-8") as f:
            return list(json.load(f).get("snapshots") or [])
    except Exception:
        return []


def _closest_before(history: List[dict], days: float) -> Optional[dict]:
    """Снимок, ближайший к «N дней назад» (но старше N-0.5 дня)."""
    if not history:
        return None
    now = datetime.now(timezone.utc)
    best, best_gap = None, None
    for s in history:
        try:
            at = datetime.fromisoformat(str(s.get("at")))
        except ValueError:
            continue
        age = (now - at).total_seconds() / 86400.0
        if age < days - 0.5:
            continue
        gap = abs(age - days)
        if best_gap is None or gap < best_gap:
            best, best_gap = s, gap
    return best


def growth_report(user_id: str) -> Dict[str, Any]:
    """Наклон: текущий объём, рост за 7 и 30 дней, лидеры роста.

    Честно про пустоту: пока снимков меньше двух, наклона нет — отчёт
    так и говорит, а не рисует нули, которые читаются как «не растёт».
    """
    history = load_history(user_id)
    current = history[-1] if history else measure_user(user_id)
    report: Dict[str, Any] = {"current": current, "growth": {},
                              "fastest_growing": [],
                              "note": ""}
    if len(history) < 2:
        report["note"] = ("истории ещё нет — наклон появится после "
                          "нескольких ночных снимков; «нет данных» ≠ "
                          "«не растёт»")
        return report

    for label, days in (("7d", 7.0), ("30d", 30.0)):
        base = _closest_before(history[:-1], days)
        if not base:
            continue
        entry: Dict[str, Any] = {}
        for k in ("nodes_total", "edges_total", "supersede_records",
                  "total_bytes"):
            was, now_v = int(base.get(k) or 0), int(current.get(k) or 0)
            entry[k] = {"was": was, "now": now_v, "delta": now_v - was}
        report["growth"][label] = entry

    # лидеры роста по типам узлов — за самое длинное доступное окно
    base = _closest_before(history[:-1], 30.0) or history[0]
    was_l = base.get("nodes_by_label") or {}
    now_l = current.get("nodes_by_label") or {}
    deltas = [(lbl, now_l.get(lbl, 0) - was_l.get(lbl, 0))
              for lbl in set(was_l) | set(now_l)]
    report["fastest_growing"] = [
        {"label": lbl, "delta": d}
        for lbl, d in sorted(deltas, key=lambda x: -x[1])[:5] if d > 0
    ]
    return report


def _export_gauges(user_id: str, snap: Dict[str, Any]) -> None:
    """Выставить Prometheus-gauge — объём виден в мониторинге, не в файле."""
    try:
        from backend.core.observability import metrics as m
        m.memory_nodes_gauge.labels(tenant_id=str(user_id)).set(
            snap.get("nodes_total") or 0)
        m.memory_edges_gauge.labels(tenant_id=str(user_id)).set(
            snap.get("edges_total") or 0)
        m.memory_bytes_gauge.labels(tenant_id=str(user_id)).set(
            snap.get("total_bytes") or 0)
    except Exception:
        logger.debug("footprint: gauges export skipped", exc_info=True)


__all__ = ["growth_report", "load_history", "measure_user",
           "record_snapshot"]
