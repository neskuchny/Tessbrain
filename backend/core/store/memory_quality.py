# -*- coding: utf-8 -*-
"""Memory Quality агрегат для wow-screen виджета (issue #112 T-9).

Считает распределение attribution_confidence по Decision/Task узлам
тенанта — основа для honest 'мы говорим когда не знаем' UX:
  «47 задач извлечено · 38 высокая · 6 средняя · 3 требуют проверки»

Источники данных:
  - Neo4j Decision/Task узлы (T-7 поля: attribution_confidence,
    needs_human_review)
  - NetworkX вариант для dev/local

Подход — простой scan через scan_quality_for_tenant(tenant_id) с in-memory
агрегацией. Кэш не нужен — operation редкая (раз в 30s через polling в
HomeTab, либо когда фронт получает signal 'attribution_warning').

Foundry-style: показываем не «100% accuracy» (вранье), а честное
распределение. Это и есть enterprise-trust argument из обсуждения issue
#112 Пакета 4.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _empty_summary(tenant_id: Optional[str]) -> Dict[str, Any]:
    """Возвращается когда тенанта пустой или данных нет."""
    return {
        "tenant_id": tenant_id,
        "tasks": {"total": 0, "high": 0, "medium": 0, "low": 0,
                  "needs_review": 0},
        "decisions": {"total": 0, "high": 0, "medium": 0, "low": 0,
                      "needs_review": 0},
        "overall": {
            "total": 0,
            "honest_attribution_pct": None,  # null когда нет данных
            "needs_review": 0,
        },
    }


def _accumulate(
    bucket: Dict[str, int],
    confidence: str,
    needs_review: bool,
) -> None:
    """In-place: bump соответствующий counter в bucket."""
    bucket["total"] += 1
    if confidence in ("high", "medium", "low"):
        bucket[confidence] += 1
    if needs_review:
        bucket["needs_review"] += 1


def _compute_overall(tasks: Dict[str, int], decisions: Dict[str, int]) -> Dict[str, Any]:
    """Объединить два bucket'а в общий summary."""
    total = tasks["total"] + decisions["total"]
    if total == 0:
        return {"total": 0, "honest_attribution_pct": None, "needs_review": 0}
    high = tasks["high"] + decisions["high"]
    # «honest_attribution_pct» = доля high-confidence к общему.
    # НЕ показываем 'accuracy' (мы не знаем ground truth), показываем
    # **что система сама о себе говорит** — это и есть честность.
    return {
        "total": total,
        "honest_attribution_pct": round(100.0 * high / total, 1),
        "needs_review": tasks["needs_review"] + decisions["needs_review"],
    }


def scan_quality_for_tenant_networkx(
    nx_graph, tenant_id: Optional[str],
) -> Dict[str, Any]:
    """NetworkX вариант — для dev/local (когда use_networkx=True)."""
    summary = _empty_summary(tenant_id)
    for _, data in nx_graph.nodes(data=True):
        label = data.get("_label")
        if label not in ("Decision", "Task"):
            continue
        if tenant_id and data.get("tenant_id") not in (tenant_id, "", None):
            # Узлы без tenant_id попадают если tenant_id фильтр None —
            # но если запросили конкретного тенанта, исключаем чужие.
            continue
        confidence_field = (
            "attribution_confidence" if label == "Decision"
            else "assignment_confidence"
        )
        confidence = (data.get(confidence_field) or "low").lower()
        needs_review = bool(data.get("needs_human_review", False))
        bucket = summary["tasks"] if label == "Task" else summary["decisions"]
        _accumulate(bucket, confidence, needs_review)

    summary["overall"] = _compute_overall(summary["tasks"], summary["decisions"])
    return summary


async def scan_quality_for_tenant_neo4j(
    driver, tenant_id: Optional[str],
) -> Dict[str, Any]:
    """Neo4j вариант — для прода. Считает агрегат через один запрос."""
    summary = _empty_summary(tenant_id)
    if not driver:
        return summary
    # Один запрос — два MATCH'а, COUNT по разрезам confidence.
    # tenant_id может быть None (тогда — глобальный счёт, для dev).
    query = """
    CALL {
      MATCH (t:Task)
      WHERE $tenant IS NULL OR t.tenant_id = $tenant OR t.tenant_id IS NULL
      RETURN
        'Task' AS kind,
        COALESCE(t.assignment_confidence, 'low') AS confidence,
        COALESCE(t.needs_human_review, false) AS needs_review
      UNION ALL
      MATCH (d:Decision)
      WHERE $tenant IS NULL OR d.tenant_id = $tenant OR d.tenant_id IS NULL
      RETURN
        'Decision' AS kind,
        COALESCE(d.attribution_confidence, 'low') AS confidence,
        COALESCE(d.needs_human_review, false) AS needs_review
    }
    RETURN kind, confidence, needs_review
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, {"tenant": tenant_id})
            async for row in result:
                bucket = (
                    summary["tasks"] if row["kind"] == "Task"
                    else summary["decisions"]
                )
                confidence = (row["confidence"] or "low").lower()
                _accumulate(bucket, confidence, bool(row["needs_review"]))
    except Exception as e:
        logger.warning("memory_quality scan_neo4j failed: %s", e)
        return summary

    summary["overall"] = _compute_overall(summary["tasks"], summary["decisions"])
    return summary


async def scan_quality_for_tenant(
    tenant_id: Optional[str],
) -> Dict[str, Any]:
    """Универсальная точка входа — выбирает backend по graph_builder
    singleton'у. Если graph_builder ещё не инициализирован — возвращает
    пустой summary (UI обработает loading-state)."""
    try:
        from backend.core.store.graph_builder import get_graph_builder
        gb = await get_graph_builder()  # async: без await возвращался coroutine,
        # getattr(coroutine, "driver") = None → скан памяти всегда пустой
    except Exception:
        return _empty_summary(tenant_id)

    if getattr(gb, "use_networkx", False):
        return scan_quality_for_tenant_networkx(gb.nx_graph, tenant_id)
    return await scan_quality_for_tenant_neo4j(getattr(gb, "driver", None), tenant_id)


async def list_needs_review(
    tenant_id: Optional[str],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Список Decision/Task с needs_human_review=True — для review queue
    UI (T-10). Сейчас отдаём базовую информацию, dipper UI добавит
    позже (контекст встречи, suggested correction)."""
    try:
        # get_graph_builder() — async (возвращает coroutine), без await
        # дальше `gb.driver` валилось `'coroutine' object has no attribute
        # 'driver'` → review queue был пустым.
        from backend.core.store.graph_builder import get_graph_builder
        gb = await get_graph_builder()
    except Exception:
        return []

    items: List[Dict[str, Any]] = []
    if getattr(gb, "use_networkx", False):
        for nid, data in gb.nx_graph.nodes(data=True):
            label = data.get("_label")
            if label not in ("Decision", "Task"):
                continue
            if not data.get("needs_human_review"):
                continue
            if tenant_id and data.get("tenant_id") not in (tenant_id, "", None):
                continue
            items.append({
                "id": nid,
                "kind": label.lower(),
                "title": data.get("title") or data.get("summary") or "",
                "confidence": (
                    data.get("assignment_confidence" if label == "Task"
                             else "attribution_confidence") or "low"
                ),
                "assignee_name": data.get("assignee_name") or "",
                "assignee_speaker": data.get("assignee_speaker") or "",
                "decision_maker_resolved": data.get("decision_maker_resolved") or "",
                "decision_maker_speaker": data.get("decision_maker_speaker") or "",
                "created_at": data.get("created_at") or data.get("_created_at"),
            })
            if len(items) >= limit:
                break
        return items

    # Neo4j: один запрос для обоих типов
    query = """
    CALL {
      MATCH (t:Task)
      WHERE t.needs_human_review = true
        AND ($tenant IS NULL OR t.tenant_id = $tenant OR t.tenant_id IS NULL)
      RETURN t.id AS id, 'task' AS kind, t.title AS title,
             t.assignment_confidence AS confidence,
             t.assignee_name AS assignee_name,
             t.assignee_speaker AS assignee_speaker,
             '' AS decision_maker_resolved,
             '' AS decision_maker_speaker,
             t.created_at AS created_at
      UNION ALL
      MATCH (d:Decision)
      WHERE d.needs_human_review = true
        AND ($tenant IS NULL OR d.tenant_id = $tenant OR d.tenant_id IS NULL)
      RETURN d.id AS id, 'decision' AS kind, d.summary AS title,
             d.attribution_confidence AS confidence,
             '' AS assignee_name,
             '' AS assignee_speaker,
             d.decision_maker_resolved AS decision_maker_resolved,
             d.decision_maker_speaker AS decision_maker_speaker,
             d.created_at AS created_at
    }
    RETURN id, kind, title, confidence, assignee_name, assignee_speaker,
           decision_maker_resolved, decision_maker_speaker, created_at
    ORDER BY created_at DESC
    LIMIT $limit
    """
    try:
        async with gb.driver.session() as session:
            result = await session.run(query, {"tenant": tenant_id, "limit": int(limit)})
            async for row in result:
                items.append({k: row[k] for k in row.keys()})
    except Exception as e:
        logger.warning("memory_quality list_needs_review failed: %s", e)
    return items
