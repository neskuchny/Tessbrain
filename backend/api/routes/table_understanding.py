# -*- coding: utf-8 -*-
"""
Столп C — эндпоинт понимания таблиц (TableUnderstandingAgent).

POST /api/v1/table/understand — дают колонки+строки (+ снапшот компании) →
система говорит, ЧТО это за таблица, потому что знает компанию. Скелет
заземляет детерминированно; LLM интерпретирует под скелетом; если данные не
вяжутся с компанией — честно «незнакомая таблица».

За флагом table_understanding_enabled (default False). Авто-фетч снапшота
компании из графа — follow-up; пока снапшот передаётся в запросе (frontend знает
контекст компании), либо его нет → честный «unknown».
"""
from __future__ import annotations

from typing import Any, Optional

from litestar import Router, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field


class TableUnderstandRequest(BaseModel):
    columns: list[str]
    rows: list[Any] = Field(default_factory=list)         # list[dict] или list[list]
    snapshot: Optional[dict] = None                       # структурный снапшот компании
    snapshot_text: str = ""                               # текст снапшота для LLM-контекста
    user_id: Optional[str] = None                         # для авто-фетча снапшота
    auto_fetch: bool = True                               # тянуть снапшот, если не передан


async def _autofetch_prior_from_user_graph(user_id: str) -> Optional[dict]:
    """Best-effort per-user prior НАПРЯМУЮ из графа юзера (источник истины,
    read-only, БЕЗ LLM/генерации). Узлы Person/Product/Department/Project/Team/
    KPI → имена. Любой сбой → None. Строго guarded: не ломает эндпоинт."""
    try:
        from backend.core.store.graph_builder import GraphBuilder
        from backend.core.store.tenant_paths import graph_path_for_user
        from backend.core.think.table_understanding_agent import prior_from_graph_nodes

        gb = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))
        await gb.connect()
        nxg = getattr(gb, "nx_graph", None)
        nodes = [data for _id, data in nxg.nodes(data=True)] if nxg is not None else []
        prior = prior_from_graph_nodes(nodes)
        try:
            await gb.close(save=False)
        except Exception:
            pass
        return prior if any(prior.values()) else None
    except Exception:
        return None


async def _autofetch_global_snapshot() -> tuple:
    """Фолбэк: глобальный простой снапшот (read-only, БЕЗ LLM). (dict|None, text)."""
    try:
        from backend.core.sleep.company_snapshot_agent import get_company_snapshot_agent
        agent = get_company_snapshot_agent()
        snap = getattr(agent, "_current_snapshot", None)
        text = agent.get_snapshot_for_prompt() if hasattr(agent, "get_snapshot_for_prompt") else ""
        return (snap if isinstance(snap, dict) else None), (text or "")
    except Exception:
        return None, ""


@post("/understand")
async def understand_table(data: TableUnderstandRequest) -> dict:
    """Понять таблицу с заземлением на модель компании."""
    from backend.config import settings as _settings
    if not getattr(_settings, "table_understanding_enabled", False):
        raise HTTPException(status_code=403, detail="table_understanding disabled")

    from backend.core.think.table_understanding_agent import TableUnderstandingAgent

    snapshot = data.snapshot
    snapshot_text = data.snapshot_text
    prior = None
    # Авто-фетч, если снапшот не передан: «система сама знает компанию».
    # Приоритет — per-user ГРАФ (источник истины); фолбэк — глобальный снапшот.
    if snapshot is None and data.auto_fetch:
        if data.user_id:
            prior = await _autofetch_prior_from_user_graph(data.user_id)
        if prior is None:
            snapshot, fetched_text = await _autofetch_global_snapshot()
            snapshot_text = snapshot_text or fetched_text

    agent = TableUnderstandingAgent()  # ленивый LLM-роутер внутри
    result = await agent.understand(
        columns=data.columns,
        rows=data.rows,
        snapshot=snapshot,
        prior=prior,
        snapshot_text=snapshot_text,
    )
    return result.to_dict()


router = Router(path="/table", route_handlers=[understand_table], tags=["Table Understanding"])
