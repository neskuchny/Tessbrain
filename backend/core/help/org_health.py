# -*- coding: utf-8 -*-
"""
Проактивное структурное озарение по графу компании (роадмап R-4).

Две функции, обе поверх того же графа знаний, что и signals_collector:

- build_organ_map (R-4.1, «Нервная система»): агрегирует сигналы по «органам»
  (проект / направление) с единым индикатором состояния red/amber/green/idle —
  чтобы на главном экране было видно, какой «орган» требует внимания, до того
  как проблема стала пожаром.

- find_structure_gaps (R-4.3, «Пробел»): структурный gap-анализ оргграфа —
  перегруженный исполнитель (bottleneck), задачи без владельца (orphan),
  недоукомплектованный проект (understaffed) → выдаёт ПРОФИЛЬ РОЛИ (что за
  человек нужен), а не имя. Имя — уже задача рекрутёра.

Чистые вычисления (`_compute_*`) принимают networkx-граф → юнит-тестируются
без БД. Обёртки (`build_*`/`find_*`) открывают граф пользователя best-effort,
никогда не raises; нет графа → пустой результат. LLM не используется (дёшево).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DONE = {"done", "completed", "closed", "resolved", "finished",
         "выполнено", "закрыт", "закрыта", "готово"}

# Пороги состояния «органа» (задачи).
_AMBER_OPEN = 5        # открытых задач ≥ → жёлтый (даже без просрочки)
# Пороги gap-анализа.
_OVERLOAD_ABS = 8      # открытых задач на одном исполнителе ≥ → bottleneck
_OVERLOAD_REL = 2.0    # или ≥ 2× медианы (и ≥ _OVERLOAD_MIN)
_OVERLOAD_MIN = 5
_UNDERSTAFF_OPEN = 5   # открытых задач в проекте ≥ при ≤1 человеке → understaffed
_ORPHAN_MIN = 3        # задач без владельца ≥ → gap-роль

_STOPWORDS = {
    "и", "в", "во", "не", "на", "с", "по", "для", "к", "от", "до", "за", "the",
    "a", "an", "to", "of", "for", "with", "как", "что", "это", "по", "из", "у",
    "task", "задача", "сделать", "надо", "нужно", "сделай", "новый", "новая",
}


def _norm_status(data: dict) -> str:
    return str(data.get("status") or data.get("state") or "").strip().lower()


def _is_open(data: dict) -> bool:
    return _norm_status(data) not in _DONE


def _is_overdue(data: dict, today: str) -> bool:
    due = str(data.get("deadline") or data.get("due_date") or "")[:10]
    return bool(due) and due < today


def _incident(g: Any, nid: Any):
    """Все инцидентные рёбра узла (out+in) как (other_id, type, other_data)."""
    for _u, v, d in g.out_edges(nid, data=True):
        yield v, str(d.get("_type") or d.get("type") or "").upper(), g.nodes.get(v, {})
    for u, _v, d in g.in_edges(nid, data=True):
        yield u, str(d.get("_type") or d.get("type") or "").upper(), g.nodes.get(u, {})


def _resolve_assignee(g: Any, nid: Any, data: dict) -> Optional[str]:
    """Исполнитель задачи: сначала атрибут, потом Person через ASSIGNED_TO."""
    for k in ("assignee", "assignee_name", "owner", "assigned_to"):
        v = str(data.get(k) or "").strip()
        if v:
            return v
    for other_id, etype, odata in _incident(g, nid):
        if etype == "ASSIGNED_TO" and str(odata.get("_label") or "") == "Person":
            name = str(odata.get("name") or odata.get("title") or other_id).strip()
            if name:
                return name
    return None


def _resolve_project(g: Any, nid: Any, data: dict) -> Optional[str]:
    """Проект/орган задачи: сначала атрибут, потом Project через ребро."""
    for k in ("project", "project_name", "project_id", "direction"):
        v = str(data.get(k) or "").strip()
        if v:
            return v
    for other_id, etype, odata in _incident(g, nid):
        if str(odata.get("_label") or "") == "Project":
            name = str(odata.get("name") or odata.get("title") or other_id).strip()
            if name:
                return name
    return None


def _keywords(titles: list[str], top: int = 4) -> list[str]:
    """Дешёвый разбор ключевых слов из заголовков задач (без LLM)."""
    words: Counter = Counter()
    for t in titles:
        for w in re.findall(r"[a-zA-Zа-яА-ЯёЁ]{4,}", str(t or "").lower()):
            if w not in _STOPWORDS:
                words[w] += 1
    return [w for w, _ in words.most_common(top)]


# ── R-4.1: карта органов ──────────────────────────────────────────────────

def _compute_organ_map(g: Any) -> dict[str, Any]:
    """Состояние по «органам» (группируем задачи по проекту/направлению)."""
    if g is None or g.number_of_nodes() == 0:
        return {"organs": [], "summary": {}}
    today = date.today().isoformat()

    # орган → {open, overdue, people:set, titles:[]}
    organs: dict[str, dict[str, Any]] = {}
    for nid, data in g.nodes(data=True):
        if str(data.get("_label") or data.get("label") or "") != "Task":
            continue
        organ = _resolve_project(g, nid, data) or "Общие задачи"
        o = organs.setdefault(organ, {"open": 0, "overdue": 0,
                                      "people": set(), "titles": []})
        if _is_open(data):
            o["open"] += 1
            if _is_overdue(data, today):
                o["overdue"] += 1
            who = _resolve_assignee(g, nid, data)
            if who:
                o["people"].add(who)
            o["titles"].append(data.get("title") or data.get("name") or "")

    # Проекты без задач — тоже органы (idle), чтобы карта была полной.
    for nid, data in g.nodes(data=True):
        if str(data.get("_label") or "") == "Project":
            name = str(data.get("name") or data.get("title") or nid).strip()
            organs.setdefault(name, {"open": 0, "overdue": 0,
                                     "people": set(), "titles": []})

    out: list[dict[str, Any]] = []
    for name, o in organs.items():
        open_, overdue = o["open"], o["overdue"]
        people = len(o["people"])
        if overdue >= 1:
            state, reason = "red", f"{overdue} просроченных из {open_} открытых"
        elif open_ >= _AMBER_OPEN or (open_ >= 1 and people == 0):
            state = "amber"
            reason = (f"{open_} открытых, без владельца" if people == 0
                      else f"{open_} открытых задач")
        elif open_ >= 1:
            state, reason = "green", f"{open_} открытых, в норме"
        else:
            state, reason = "idle", "нет активных задач"
        out.append({
            "name": name, "kind": "project", "state": state,
            "open_tasks": open_, "overdue_tasks": overdue,
            "people": people, "reason": reason,
        })

    order = {"red": 0, "amber": 1, "green": 2, "idle": 3}
    out.sort(key=lambda x: (order.get(x["state"], 9), -x["overdue_tasks"],
                            -x["open_tasks"]))
    summary: Counter = Counter(x["state"] for x in out)
    return {"organs": out, "summary": dict(summary)}


async def build_organ_map(user_id: str) -> dict[str, Any]:
    """R-4.1: карта органов по графу пользователя. {} если графа нет."""
    g = await _open_graph(user_id)
    if g is None:
        return {"organs": [], "summary": {}}
    try:
        return _compute_organ_map(g[1])
    finally:
        await _close_graph(g[0])


# ── R-4.3: структурные пробелы → профили ролей ────────────────────────────

def _compute_structure_gaps(g: Any) -> dict[str, Any]:
    """Перегруз / бесхозные задачи / недоукомплектованность → профили ролей."""
    if g is None or g.number_of_nodes() == 0:
        return {"gaps": [], "summary": {}}
    today = date.today().isoformat()

    per_person: dict[str, dict[str, Any]] = {}
    orphan_titles: list[str] = []
    per_project: dict[str, dict[str, Any]] = {}

    for nid, data in g.nodes(data=True):
        if str(data.get("_label") or data.get("label") or "") != "Task":
            continue
        if not _is_open(data):
            continue
        title = data.get("title") or data.get("name") or ""
        who = _resolve_assignee(g, nid, data)
        proj = _resolve_project(g, nid, data) or "Общие задачи"
        overdue = _is_overdue(data, today)

        pj = per_project.setdefault(proj, {"open": 0, "people": set(), "titles": []})
        pj["open"] += 1
        pj["titles"].append(title)
        if who:
            pj["people"].add(who)
            p = per_person.setdefault(who, {"open": 0, "overdue": 0, "titles": []})
            p["open"] += 1
            p["overdue"] += int(overdue)
            p["titles"].append(title)
        else:
            orphan_titles.append(title)

    gaps: list[dict[str, Any]] = []

    # 1) Перегруз (bottleneck).
    loads = sorted(v["open"] for v in per_person.values())
    median = loads[len(loads) // 2] if loads else 0
    for who, p in per_person.items():
        overloaded = p["open"] >= _OVERLOAD_ABS or (
            p["open"] >= _OVERLOAD_MIN and median and p["open"] >= _OVERLOAD_REL * median)
        if overloaded:
            gaps.append({
                "type": "overload",
                "where": who,
                "severity": "high" if p["overdue"] else "medium",
                "open_tasks": p["open"],
                "role_profile": {
                    "need": "второй исполнитель / разгрузка узкого места",
                    "skills_hint": _keywords(p["titles"]),
                    "reason": (f"на «{who}» висит {p['open']} открытых задач"
                               + (f" ({p['overdue']} просрочены)" if p["overdue"] else "")
                               + " — единая точка отказа"),
                },
            })

    # 2) Бесхозные задачи (orphan) → роль без владельца.
    if len(orphan_titles) >= _ORPHAN_MIN:
        gaps.append({
            "type": "orphan",
            "where": "без владельца",
            "severity": "medium",
            "open_tasks": len(orphan_titles),
            "role_profile": {
                "need": "владелец для бесхозного пула задач",
                "skills_hint": _keywords(orphan_titles),
                "reason": (f"{len(orphan_titles)} открытых задач ни на ком не "
                           "закреплены — нужна роль, которая их подхватит"),
            },
        })

    # 3) Недоукомплектованный проект (understaffed).
    for proj, pj in per_project.items():
        if pj["open"] >= _UNDERSTAFF_OPEN and len(pj["people"]) <= 1:
            gaps.append({
                "type": "understaffed",
                "where": proj,
                "severity": "medium",
                "open_tasks": pj["open"],
                "role_profile": {
                    "need": f"дополнительный человек в «{proj}»",
                    "skills_hint": _keywords(pj["titles"]),
                    "reason": (f"в «{proj}» {pj['open']} открытых задач на "
                               f"{len(pj['people'])} человек — не тянут"),
                },
            })

    sev = {"high": 0, "medium": 1, "low": 2}
    gaps.sort(key=lambda x: (sev.get(x["severity"], 9), -x["open_tasks"]))
    summary: Counter = Counter(x["type"] for x in gaps)
    return {"gaps": gaps, "summary": dict(summary)}


async def find_structure_gaps(user_id: str) -> dict[str, Any]:
    """R-4.3: gap-анализ оргграфа. {} если графа нет."""
    g = await _open_graph(user_id)
    if g is None:
        return {"gaps": [], "summary": {}}
    try:
        return _compute_structure_gaps(g[1])
    finally:
        await _close_graph(g[0])


# ── Общий best-effort доступ к графу (как в signals_collector) ────────────

async def _open_graph(user_id: str):
    """Открыть граф пользователя. Возвращает (gb, nx_graph) или None."""
    if not user_id:
        return None
    gb = None
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        gb = GraphBuilder(use_networkx=None,
                          graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        if not (gb.connected and gb.nx_graph):
            await _close_graph(gb)
            return None
        return gb, gb.nx_graph
    except Exception as e:
        logger.debug("org_health graph open failed: %s", e)
        if gb is not None:
            await _close_graph(gb)
        return None


async def _close_graph(gb: Any) -> None:
    if gb is None:
        return
    try:
        await gb.close(save=False)
    except Exception:
        pass


__all__ = [
    "build_organ_map",
    "find_structure_gaps",
    "_compute_organ_map",
    "_compute_structure_gaps",
]
