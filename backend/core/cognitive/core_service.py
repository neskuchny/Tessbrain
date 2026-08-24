# -*- coding: utf-8 -*-
"""Cognitive Core — read-only агрегатор «Единая память».

Собирает для одного пользователя «когнитивное ядро»: фрагменты знания (узлы
графа), их состояние консолидации, связи и индекс синхронизации ядра. ВСЁ на
реальных данных — ни одного мока:

- Фрагменты = узлы personal∪org-графа (`merged_graph_view_for_user`,
  read-only, tenant-safe). Тип = метка узла (`_label`), имя/владелец/дата — из
  свойств узла.
- Состояние (В ТЕНИ / НА ОРБИТЕ / В ЯДРЕ) выводится из РЕАЛЬНОГО сигнала графа —
  связности (степень узла) + свежести: изолированный узел ещё «в тени», хорошо
  связанный вплетён «в ядро». Это derived-from-real-data (как перцентиль), а не
  выдуманный статус: степень считается по фактическим рёбрам графа.
- Связанные фрагменты = соседи по графу (`get_node_relationships`).
- Поиск = семантический `federated_vector_search` (personal + org, RBAC).
- СИНХРОНИЗАЦИЯ ЯДРА % = индекс S(t) CogniLayer (`org_sync_report`).

Deny-by-default: за флагом ENABLE_COGNITIVE_CORE. Выключено → `enabled=false`,
эндпоинты отдают пусто, ни одного тяжёлого чтения.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Состояния консолидации фрагмента (визуальный язык интерфейса).
STATE_SHADOW = "shadow"   # В ТЕНИ — изолирован, ещё не вплетён в ядро
STATE_ORBIT = "orbit"     # НА ОРБИТЕ — частично связан
STATE_CORE = "core"       # В ЯДРЕ — плотно вплетён в ядро знаний

# Порог «в ядре» по степени узла (число связей). Ниже — орбита, ноль — тень.
# Это интерпретация реального сигнала связности, а не хранимый статус.
_CORE_DEGREE = 4

# Метка графа → человеческое имя (RU). Fallback — сама метка.
_TYPE_RU: Dict[str, str] = {
    "Person": "Сотрудник", "Team": "Команда", "Department": "Отдел",
    "Role": "Роль", "Project": "Проект", "Product": "Продукт",
    "Meeting": "Встреча", "Task": "Задача", "Decision": "Решение",
    "Goal": "Цель", "KPI": "KPI", "Regulation": "Регламент",
    "Policy": "Политика", "Template": "Шаблон", "Topic": "Тема",
    "Technology": "Технология", "Knowledge": "Знание", "Principle": "Принцип",
    "Milestone": "Веха", "Risk": "Риск", "Commitment": "Обязательство",
    "Contradiction": "Противоречие", "Pattern": "Паттерн", "Question": "Вопрос",
    "Chunk": "Фрагмент", "Snapshot": "Снепшот", "Report": "Отчёт",
    "Scenario": "Сценарий", "Prediction": "Прогноз", "ConflictRisk": "Риск-конфликт",
    "Entity": "Сущность", "Episodic": "Эпизод",
}

# Служебные метки — не показываем как «фрагменты знания».
_HIDDEN_LABELS = {"Chunk", "Episodic"}


def cognitive_core_enabled() -> bool:
    """Глобальный гейт. ВКЛЮЧЕНО по умолчанию (opt-out): вид read-only и
    тенант-безопасен, поэтому работает сразу. Явно выключить —
    ENABLE_COGNITIVE_CORE=false|0|no|off."""
    return str(os.environ.get("ENABLE_COGNITIVE_CORE", "")).strip().lower() \
        not in ("0", "false", "no", "off")


def _state_from_degree(deg: int) -> str:
    if deg <= 0:
        return STATE_SHADOW
    if deg < _CORE_DEGREE:
        return STATE_ORBIT
    return STATE_CORE


def _node_name(node: Dict[str, Any]) -> str:
    for k in ("name", "title", "goal_statement", "statement", "text", "label"):
        v = node.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return str(node.get("id") or "—")


def _fragment(node: Dict[str, Any], degree_map: Dict[Any, int]) -> Dict[str, Any]:
    """Узел графа → нормализованный фрагмент для интерфейса."""
    nid = node.get("id")
    label = str(node.get("_label") or node.get("entity_type") or "Entity")
    deg = int(degree_map.get(nid, 0))
    return {
        "id": nid,
        "type": label,
        "type_ru": _TYPE_RU.get(label, label),
        "title": _node_name(node)[:160],
        "owner": node.get("user_id") or node.get("tenant_id") or "",
        "updated_at": node.get("updated_at") or node.get("created_at") or "",
        "state": _state_from_degree(deg),
        "degree": deg,
    }


def _degree_map(gb: Any) -> Dict[Any, int]:
    """Степень каждого узла из nx_graph (реальные рёбра). Пусто для Neo4j-mode."""
    try:
        g = getattr(gb, "nx_graph", None)
        if g is not None:
            return {n: int(d) for n, d in g.degree()}
    except Exception:
        logger.debug("cognitive-core: degree map unavailable", exc_info=True)
    return {}


def _all_edges(gb: Any, *, cap: int = 8000) -> List[tuple]:
    """Уникальные неориентированные рёбра графа [(u, v)] (реальные связи).
    Ограничено cap. Пусто для Neo4j-mode (nx_graph недоступен)."""
    out: List[tuple] = []
    try:
        g = getattr(gb, "nx_graph", None)
        if g is None:
            return out
        seen: set = set()
        for u, v in g.edges():
            if u == v:
                continue
            key = (u, v) if str(u) <= str(v) else (v, u)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            if len(out) >= cap:
                break
    except Exception:
        logger.debug("cognitive-core: edge list unavailable", exc_info=True)
    return out


async def _neo4j_degree_edges(gb: Any, ids: List[str], *, cap: int = 8000
                              ) -> tuple:
    """Степень и рёбра среди узлов `ids` из Neo4j (одним запросом). Оба конца
    ребра — уже авторизованные узлы (из tenant-фильтрованного списка), поэтому
    межтенантных утечек нет. Возвращает (degree_map, edges)."""
    degree: Dict[str, int] = {}
    edges: List[tuple] = []
    ids = [i for i in ids if i]
    if not ids or not getattr(gb, "driver", None):
        return degree, edges
    try:
        q = ("MATCH (a)-[r]-(b) WHERE a.id IN $ids AND b.id IN $ids "
             "RETURN a.id AS s, b.id AS t")
        async with gb.driver.session() as session:
            res = await session.run(q, {"ids": ids})
            rows = await res.data()
        seen: set = set()
        for row in rows:
            s, t = row.get("s"), row.get("t")
            if not s or not t or s == t:
                continue
            key = (s, t) if str(s) <= str(t) else (t, s)
            if key in seen:
                continue
            seen.add(key)
            edges.append(key)
            degree[s] = degree.get(s, 0) + 1
            degree[t] = degree.get(t, 0) + 1
            if len(edges) >= cap:
                break
    except Exception as e:
        logger.debug("cognitive-core: neo4j degree/edges unavailable: %s", e)
    return degree, edges


async def _sync_percent(user_id: str) -> Optional[int]:
    """Индекс синхронизации ядра S(t) 0..100 (или None). Read-only, best-effort."""
    try:
        from backend.core.think.org_sync import org_sync_report
        report = await org_sync_report(user_id, judge=False, notify=False)
        s = (report or {}).get("index", {}).get("s")
        return int(s) if s is not None else None
    except Exception as e:
        logger.debug("cognitive-core: sync index unavailable: %s", e)
        return None


def _sync_trend(user_id: str, *, limit: int = 30) -> List[Dict[str, Any]]:
    """История индекса синхронизации ядра [{ts, s}] (реальная, из index_history).
    Точки с s=None пропускаем — тренд рисуем только по фактическим значениям."""
    try:
        from backend.core.think.org_sync import index_history
        hist = index_history(user_id, limit=max(2, min(120, limit))) or []
        return [{"ts": h.get("ts"), "s": int(h["s"])}
                for h in hist if h.get("s") is not None]
    except Exception as e:
        logger.debug("cognitive-core: sync trend unavailable: %s", e)
        return []


async def core_overview(user_id: str, *, limit: int = 240,
                        with_sync: bool = True) -> Dict[str, Any]:
    """Обзор ядра: счётчики по типам/состояниям + топ-фрагменты + sync%.

    Всё из реального графа пользователя (personal∪org, RBAC). Never-raise:
    при сбое отдаёт пустой, но валидный ответ."""
    if not cognitive_core_enabled():
        return {"enabled": False}
    out: Dict[str, Any] = {
        "enabled": True, "total": 0, "by_type": {}, "by_state": {},
        "fragments": [], "edges": [], "sync_percent": None, "sync_trend": [],
    }
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        # Auto-detect бэкенда (как рабочий /entities/graph): в проде это Neo4j,
        # в dev — NetworkX. Раньше форс use_networkx=True читал ПУСТОЙ локальный
        # JSON при Neo4j-деплое → 0 фрагментов.
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            nodes = await gb.get_all_nodes_async(
                label=None, limit=max(limit * 4, 1000), tenant_id=user_id)
            # Степень и рёбра считаем ДО close. NetworkX — из nx_graph; Neo4j —
            # одним Cypher-запросом среди этих же узлов.
            if getattr(gb, "nx_graph", None) is not None:
                degree_map = _degree_map(gb)
                all_edges = _all_edges(gb)
            else:
                ids = [n.get("id") for n in nodes if n.get("id")]
                degree_map, all_edges = await _neo4j_degree_edges(gb, ids)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
        frags: List[Dict[str, Any]] = []
        by_type: Dict[str, int] = {}
        by_state: Dict[str, int] = {STATE_SHADOW: 0, STATE_ORBIT: 0, STATE_CORE: 0}
        for n in nodes:
            label = str(n.get("_label") or n.get("entity_type") or "Entity")
            if label in _HIDDEN_LABELS:
                continue
            fr = _fragment(n, degree_map)
            frags.append(fr)
            by_type[fr["type_ru"]] = by_type.get(fr["type_ru"], 0) + 1
            by_state[fr["state"]] = by_state.get(fr["state"], 0) + 1
        # Свежие и хорошо связанные — выше.
        frags.sort(key=lambda f: (f.get("degree", 0), str(f.get("updated_at") or "")),
                   reverse=True)
        shown = frags[:limit]
        out["total"] = len(frags)
        out["by_type"] = by_type
        out["by_state"] = by_state
        out["fragments"] = shown
        # Рёбра ТОЛЬКО среди показанных фрагментов (реальные связи графа).
        shown_ids = {f["id"] for f in shown}
        out["edges"] = [{"source": u, "target": v} for (u, v) in all_edges
                        if u in shown_ids and v in shown_ids]
    except Exception as e:
        logger.warning("cognitive-core overview failed for %s: %s", user_id, e)
        out["error"] = str(e)
    if with_sync:
        out["sync_percent"] = await _sync_percent(user_id)
        out["sync_trend"] = _sync_trend(user_id)
    return out


async def core_search(user_id: str, query: str, *, limit: int = 24) -> Dict[str, Any]:
    """Семантический поиск по фрагментам знания (personal + org, RBAC)."""
    if not cognitive_core_enabled():
        return {"enabled": False, "results": []}
    q = (query or "").strip()
    if not q:
        return {"enabled": True, "results": [], "query": q}
    results: List[Dict[str, Any]] = []
    try:
        from backend.core.store.vector_view import federated_vector_search
        hits = await federated_vector_search(user_id, q, limit=max(1, min(50, limit)))
        for h in hits or []:
            payload = h if isinstance(h, dict) else {}
            label = str(payload.get("type") or payload.get("_label") or "Entity")
            results.append({
                "id": payload.get("original_id") or payload.get("document_id") or payload.get("id"),
                "type": label,
                "type_ru": _TYPE_RU.get(label, label),
                "title": (str(payload.get("text") or payload.get("title") or "").strip()[:160] or "—"),
                "score": round(float(payload.get("score") or 0.0), 3),
            })
    except Exception as e:
        logger.warning("cognitive-core search failed for %s: %s", user_id, e)
        return {"enabled": True, "results": [], "query": q, "error": str(e)}
    return {"enabled": True, "results": results, "query": q}


async def core_fragment(user_id: str, node_id: str) -> Dict[str, Any]:
    """Один фрагмент + связанные (соседи графа). Read-only, tenant-safe."""
    if not cognitive_core_enabled():
        return {"enabled": False}
    nid = (node_id or "").strip()
    if not nid:
        return {"enabled": True, "fragment": None, "related": []}
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(user_id, use_networkx=None)
        try:
            node = await gb.get_node_by_id(nid)
            if not node:
                return {"enabled": True, "fragment": None, "related": []}
            degree_map = _degree_map(gb)  # пусто для Neo4j — степень ниже из связей
            fragment = _fragment(node, degree_map)
            rels = await gb.get_node_relationships(nid)
            related: List[Dict[str, Any]] = []
            seen: set = set()
            for direction in ("outgoing", "incoming"):
                for r in (rels or {}).get(direction, []):
                    other_id = r.get("target") if direction == "outgoing" else r.get("source")
                    if not other_id or other_id in seen:
                        continue
                    seen.add(other_id)
                    other = await gb.get_node_by_id(other_id)
                    if not other:
                        continue
                    fr = _fragment(other, degree_map)
                    fr["rel"] = r.get("type") or ""
                    fr["direction"] = direction
                    related.append(fr)
            # Neo4j: nx_graph нет → степень/состояние берём из числа связей.
            if not degree_map:
                deg = len(seen)
                fragment["degree"] = deg
                fragment["state"] = _state_from_degree(deg)
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
        return {"enabled": True, "fragment": fragment, "related": related[:40]}
    except Exception as e:
        logger.warning("cognitive-core fragment failed for %s/%s: %s", user_id, nid, e)
        return {"enabled": True, "fragment": None, "related": [], "error": str(e)}
