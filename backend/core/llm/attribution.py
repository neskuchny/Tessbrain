# -*- coding: utf-8 -*-
"""LLM Cost Attribution (issue #110).

Хелперы для агрегатных отчётов «по сущности»: сколько стоила обработка
конкретного документа/встречи/job. Используется в `backend/api/routes/usage.py`
эндпоинтами /by-document, /by-meeting, /by-job, /attribution.

Изолировано от роутов, чтобы тесты могли вызывать напрямую без подъёма
Litestar / зависимостей агентов (autogen, ag2 и т.п.).

SQL — параметризованный; идентификаторы колонок берутся из жёсткого
allowlist `_COL_ALLOWLIST` (защита от инъекции).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# allowlist значений group_by — никакого пользовательского ввода в SQL.
# Дублирует _ATTR_COL_ALLOWLIST в usage_stores.py намеренно — двойная защита
# на уровне API + на уровне store (defense-in-depth).
_COL_ALLOWLIST = {"meeting_id", "document_id", "job_id", "surface", "model"}


def _get_store():
    """Активный UsageStore — из синглтона UsageTracker. Лениво, чтобы
    импорт модуля не таскал store-init в момент старта."""
    from backend.core.llm.usage_tracker import get_usage_tracker
    return get_usage_tracker().store


def aggregate(column: str, value: str, tenant_id: Optional[str]) -> Dict[str, Any]:
    """Агрегат по одной сущности (документ/встреча/job).

    issue #110 пакет 4: store-вариант. SQL выполняется в UsageStore
    (SQLite или Postgres), здесь — только форматирование для UI.
    """
    if column not in _COL_ALLOWLIST:
        return {"error": f"unknown column: {column}"}
    raw = _get_store().aggregate_by_entity(column, value, tenant_id)
    if "error" in raw:
        return raw
    agg = raw.get("agg", {})

    def _fmt_rows(rows, key_col):
        out = {}
        for r in rows:
            key = r.get(key_col) or "unknown"
            cost = r.get("cost") or 0.0
            out[key] = {
                "requests": r.get("requests", 0),
                "tokens": r.get("tokens", 0),
                "cost": round(cost, 6),
                "cost_formatted": f"${cost:.4f}",
            }
        return out

    return {
        column: value,
        "tenant_id": tenant_id,
        "total": {
            "requests": agg.get("requests", 0),
            "input_tokens": agg.get("input_tokens", 0),
            "output_tokens": agg.get("output_tokens", 0),
            "cached_tokens": agg.get("cached_tokens", 0),
            "total_tokens": agg.get("total_tokens", 0),
            "cost": round(agg.get("total_cost", 0.0), 6),
            "cost_formatted": f"${agg.get('total_cost', 0.0):.4f}",
            "errors": agg.get("errors", 0),
            "avg_latency_ms": round(agg.get("avg_latency_ms", 0) or 0, 0),
            "first_at": agg.get("first_at"),
            "last_at": agg.get("last_at"),
        },
        "by_model": _fmt_rows(raw.get("by_model_rows", []), "model"),
        "by_surface": _fmt_rows(raw.get("by_surface_rows", []), "surface"),
        "recent": raw.get("recent", []),
    }


def top(
    group_by: str,
    period: str,
    surface: Optional[str],
    tenant_id: Optional[str],
    limit: int,
) -> Dict[str, Any]:
    """Топ-N сущностей по стоимости за период. issue #110 пакет 4:
    SQL делегирован в UsageStore.top_by_entity, здесь — форматирование."""
    if group_by not in _COL_ALLOWLIST:
        return {"error": f"unknown group_by: {group_by}"}
    rows = _get_store().top_by_entity(
        group_by, period, surface, tenant_id, int(limit))
    return {
        "group_by": group_by, "period": period, "surface": surface,
        "tenant_id": tenant_id,
        "items": [{
            "key": r.get("key"),
            "requests": r.get("requests", 0),
            "tokens": r.get("tokens", 0),
            "cost": round(r.get("cost") or 0.0, 6),
            "cost_formatted": f"${(r.get('cost') or 0.0):.4f}",
            "last_at": r.get("last_at"),
        } for r in rows],
    }
