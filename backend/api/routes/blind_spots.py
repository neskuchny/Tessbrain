# -*- coding: utf-8 -*-
"""
Столп A — эндпоинт сканера слепых зон (BlindSpotScanner).

POST /api/v1/blind-spots/scan — дают размеченные эпизоды (паттерн+встреча) →
система возвращает заземлённые слепые зоны: что повторяется в ≥3 разных
встречах (никто по отдельности не видит), систематический observer-drift,
неразрешённые противоречия. Каждая — с evidence (факты графа), не «уверенность».
За флагом blind_spot_scan_enabled (default False).

Эпизоды/противоречия передаются в запросе (пайплайн их размечает); observer-
drift best-effort авто-фетчится через wisdom-движок по user_id.
"""
from __future__ import annotations

from typing import Optional

from litestar import Router, post
from litestar.params import Parameter
from litestar.exceptions import HTTPException
from pydantic import BaseModel, Field


class BlindSpotScanRequest(BaseModel):
    episodes: list = Field(default_factory=list)        # [{pattern, meeting_id}, ...]
    contradictions: list = Field(default_factory=list)  # [{summary, evidence:[...], severity}]
    drift: Optional[dict] = None                        # observer-drift report (или авто-фетч)
    user_id: Optional[str] = None
    tension_type: Optional[str] = None
    min_recurrence: int = 3
    auto_fetch_drift: bool = True


async def _autofetch_drift(user_id: str, tension_type: Optional[str]) -> Optional[dict]:
    """Best-effort observer-drift через wisdom-движок. Сбой → None."""
    try:
        from backend.core.wisdom.wisdom_engine import get_wisdom_engine
        engine = await get_wisdom_engine()
        return await engine.compute_observer_drift(user_id=user_id, tension_type=tension_type)
    except Exception:
        return None


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


@post("/scan")
async def scan_blind_spots(data: BlindSpotScanRequest,
                           authorization: Optional[str] = Parameter(header="Authorization", default=None)) -> dict:
    from backend.core.auth.user_guard import resolve_user_or_none
    _u = resolve_user_or_none(authorization, data.user_id, scope="blind_spots")
    if authorization and _u is None:
        return {"error": "user_id does not match token", "blind_spots": []}
    data.user_id = _u
    """Просканировать слепые зоны компании."""
    from backend.config import settings as _settings
    if not getattr(_settings, "blind_spot_scan_enabled", False):
        raise HTTPException(status_code=403, detail="blind_spot_scan disabled")

    from backend.core.think.blind_spot_scanner import BlindSpotScanner

    drift = data.drift
    if drift is None and data.user_id and data.auto_fetch_drift:
        drift = await _autofetch_drift(data.user_id, data.tension_type)

    scanner = BlindSpotScanner(min_recurrence=max(2, data.min_recurrence))
    # Если эпизоды/противоречия не переданы, но есть user_id — авто-извлекаем из
    # графа (Pattern/Contradiction-узлы). Иначе используем переданное.
    if not data.episodes and not data.contradictions and data.user_id:
        nodes = await _autofetch_graph_nodes(data.user_id)
        spots = scanner.scan_from_graph(nodes, drift=drift)
    else:
        spots = scanner.scan(episodes=data.episodes, drift=drift, contradictions=data.contradictions)
    out = [s.to_dict() for s in spots]
    # REUSE 17 принципов / 8 патологий — обогащаем ПОЧЕМУ (claim-guard).
    if getattr(_settings, "principle_mapping_enabled", False):
        from backend.core.think.systems_principles import enrich_insights
        out = enrich_insights(out)
    return {
        "count": len(spots),
        "by_kind": {
            k: sum(1 for s in spots if s.kind == k) for k in ("recurring", "drift", "contradiction")
        },
        "blind_spots": out,
    }


router = Router(path="/blind-spots", route_handlers=[scan_blind_spots], tags=["Blind Spots"])
