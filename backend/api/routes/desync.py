# -*- coding: utf-8 -*-
"""
Столп B — эндпоинт рассинхрона между отделами (CrossDepartmentDesyncDetector).

POST /api/v1/desync/cross-department — дают цели по отделам → система находит
рассинхрон (одна метрика в противоположных направлениях; спор за ресурс).
hard-сигнал — когда метрика = известный KPI графа (best-effort авто-фетч),
иначе soft. За флагом cross_dept_desync_enabled (default False).

Цели отделов передаются в запросе (frontend/пайплайн знает); авто-извлечение
целей из снапшотов отделов — follow-up. known_kpis тянутся из per-user графа.
"""
from __future__ import annotations

from typing import Optional

from litestar import Router, post
from litestar.params import Parameter
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field


class DesyncRequest(BaseModel):
    dept_goals: dict = Field(default_factory=dict)   # {отдел: [цель, ...]}
    user_id: Optional[str] = None                    # для авто-фетча целей/KPI из графа
    auto_fetch_kpis: bool = True
    auto_fetch_goals: bool = True                    # тянуть цели отделов из графа


async def _autofetch_graph_nodes(user_id: str) -> list:
    """Best-effort: узлы per-user графа (read-only). Сбой → []."""
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user

        gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        nxg = getattr(gb, "nx_graph", None)
        nodes = [data for _id, data in nxg.nodes(data=True)] if nxg is not None else []
        try:
            await gb.close(save=False)
        except Exception:
            pass
        return nodes
    except Exception:
        return []


@post("/cross-department")
async def cross_department_desync(data: DesyncRequest,
                                  authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> dict:
    from backend.core.auth.user_guard import resolve_user_or_none
    _u = resolve_user_or_none(authorization, data.user_id, scope="desync")
    if authorization and _u is None:
        return {"error": "user_id does not match token"}
    data.user_id = _u
    """Найти рассинхрон между отделами по их целям."""
    from backend.config import settings as _settings
    if not getattr(_settings, "cross_dept_desync_enabled", False):
        raise HTTPException(status_code=403, detail="cross_dept_desync disabled")

    from backend.core.think.cross_department_desync import (
        CrossDepartmentDesyncDetector, dept_goals_from_graph_nodes,
    )

    dept_goals = data.dept_goals
    known_kpis: set = set()
    if data.user_id and (data.auto_fetch_kpis or (not dept_goals and data.auto_fetch_goals)):
        nodes = await _autofetch_graph_nodes(data.user_id)
        if data.auto_fetch_kpis:
            from backend.core.think.table_understanding_agent import prior_from_graph_nodes
            known_kpis = {k[:5] for k in prior_from_graph_nodes(nodes).get("kpi", set()) if len(k) >= 4}
        if not dept_goals and data.auto_fetch_goals:
            dept_goals = dept_goals_from_graph_nodes(nodes)

    detector = CrossDepartmentDesyncDetector(known_kpis=known_kpis)
    desyncs = detector.detect(dept_goals)
    out = [d.to_dict() for d in desyncs]
    # REUSE 17 принципов / 8 патологий — обогащаем ПОЧЕМУ (claim-guard).
    if getattr(_settings, "principle_mapping_enabled", False):
        from backend.core.think.systems_principles import enrich_insights
        out = enrich_insights(out)
    return {
        "count": len(desyncs),
        "hard": sum(1 for d in desyncs if d.grounded),
        "desyncs": out,
        "grounded_kpis": sorted(known_kpis),
    }


router = Router(path="/desync", route_handlers=[cross_department_desync], tags=["Cross-Department Desync"])
