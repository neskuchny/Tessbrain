# -*- coding: utf-8 -*-
"""Mark → «Исследования»: вывод продукта на рынок по полевым данным.

POST /mark/research        — запустить (бриф) → ран исполняется фоном
GET  /mark/research        — список ранов пользователя
GET  /mark/research/{id}   — статус/стадии/отчёт

Auth: Bearer (trusted_user_id), как analysis.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _caller(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id({"authorization": authorization}, "")
        if src != "unverified" and uid:
            return str(uid)
    except PermissionError:
        raise HTTPException(status_code=401, detail="token not authorized")
    except Exception:
        pass
    # compat-режим без JWT-секрета — как в остальном приложении
    try:
        import jwt
        payload = jwt.decode(authorization[7:],
                             options={"verify_signature": False})
        sub = payload.get("sub") or payload.get("user_id")
        if sub:
            return str(sub)
    except Exception:
        pass
    raise HTTPException(status_code=401, detail="Not authenticated")


@post("/mark/research", status_code=201)
async def start_research_route(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Запуск исследования аудитории.

    Body: {product, market?, extra?, tg_channels?: [], vk_groups?: [],
    urls?: []}. Ран уходит в фон (сбор корпуса — минуты), статус — GET.
    """
    user_id = _caller(authorization)
    product = str(data.get("product") or "").strip()
    if len(product) < 3:
        raise HTTPException(status_code=400,
                            detail="product: опишите продукт (мин. 3 символа)")
    from backend.core.marketing.research_engine import (
        execute_research, start_research)
    run = start_research(
        user_id, product=product,
        market=str(data.get("market") or ""),
        extra=str(data.get("extra") or ""),
        tg_channels=data.get("tg_channels") or [],
        vk_groups=data.get("vk_groups") or [],
        urls=data.get("urls") or [])

    async def _bg() -> None:
        try:
            await execute_research(user_id, run["id"])
        except Exception:
            logger.exception("research background task crashed")

    asyncio.create_task(_bg())
    return {"success": True, "run": run}


@get("/mark/research", status_code=200)
async def list_research_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    from backend.core.marketing.research_engine import list_runs
    runs = list_runs(user_id)
    # список — без тяжёлых полей
    slim = [{k: r.get(k) for k in ("id", "product", "market", "status",
                                   "stage", "created_at", "corpus_count",
                                   "document_id")}
            for r in reversed(runs)]
    return {"runs": slim}


@get("/mark/research/{run_id:str}", status_code=200)
async def get_research_route(
    run_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    from backend.core.marketing.research_engine import get_run
    run = get_run(user_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@post("/mark/research/{run_id:str}/personas", status_code=200)
async def build_personas_route(
    run_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Собрать персоны из корпуса завершённого исследования (фаза 2).
    Боли персон проходят проверку цитат кодом (field/hypothesis)."""
    user_id = _caller(authorization)
    from backend.core.marketing.persona_engine import build_personas
    try:
        return await build_personas(user_id, run_id=run_id)
    except Exception as e:
        logger.exception("build personas failed")
        return {"success": False, "error": f"сборка персон не отработала: {e}"}


@get("/mark/personas", status_code=200)
async def list_personas_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    from backend.core.marketing.persona_engine import list_personas
    return {"personas": list_personas(user_id)}


@post("/mark/personas/delete", status_code=200)
async def delete_persona_route(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    pid = str(data.get("persona_id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="persona_id обязателен")
    from backend.core.marketing.persona_engine import delete_persona
    return {"success": delete_persona(user_id, pid)}


@post("/mark/personas/simulate", status_code=200)
async def simulate_route(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Панель реакций: {persona_ids: [], message} → реакции персон на их
    языке + скептик-адверсарий. Симуляция = гипотезы, не измерение."""
    user_id = _caller(authorization)
    persona_ids = data.get("persona_ids") or []
    if not isinstance(persona_ids, list) or not persona_ids:
        raise HTTPException(status_code=400, detail="persona_ids обязателен")
    from backend.core.marketing.persona_engine import simulate_reactions
    try:
        return await simulate_reactions(
            user_id, persona_ids=[str(p) for p in persona_ids],
            message=str(data.get("message") or ""))
    except Exception as e:
        logger.exception("simulate failed")
        return {"success": False, "error": f"панель не отработала: {e}"}


@get("/mark/personas/simulations", status_code=200)
async def list_simulations_route(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """История панелей реакций (полные результаты, новые первыми)."""
    user_id = _caller(authorization)
    from backend.core.marketing.persona_engine import list_simulations
    return {"simulations": list(reversed(list_simulations(user_id)))}


@post("/mark/personas/to-chat", status_code=200)
async def personas_to_chat_route(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Персоны + панели + отчёт → KB-документ «Аудитория и персоны: X»
    (upsert): дальше его отмечают в «Контексте чата» → Документы и
    обсуждают с Brain («какой посыл им понравится?»)."""
    user_id = _caller(authorization)
    from backend.core.marketing.chat_context import export_audience_to_chat
    try:
        return await export_audience_to_chat(
            user_id, run_id=(str(data.get("run_id")) if data.get("run_id")
                             else None))
    except Exception as e:
        logger.exception("personas to chat failed")
        return {"success": False, "error": f"экспорт в чат не отработал: {e}"}


@post("/mark/research/{run_id:str}/sizing", status_code=200)
async def sizing_route(
    run_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Размер сегментов по Wordstat (фаза 3): показы/мес по запросам
    сегментов. Нужен YANDEX_DIRECT_TOKEN — без него честный отказ."""
    user_id = _caller(authorization)
    from backend.core.marketing.sizing import estimate_segments
    try:
        return await estimate_segments(user_id, run_id)
    except Exception as e:
        logger.exception("sizing failed")
        return {"success": False, "error": f"оценка размера не отработала: {e}"}


@post("/mark/research/{run_id:str}/facts", status_code=200)
async def facts_route(
    run_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Факт-гейт (фаза 3): {facts: [{label, impressions, clicks, leads?,
    spend?}]} — CTR/CR/CPA считает код, порядок сверяется с панелью
    персон, дрейф пишется в документ исследования."""
    user_id = _caller(authorization)
    facts = data.get("facts") or []
    if not isinstance(facts, list) or not facts:
        raise HTTPException(status_code=400, detail="facts: непустой список")
    from backend.core.marketing.fact_gate import apply_campaign_facts
    try:
        return await apply_campaign_facts(user_id, run_id, facts)
    except Exception as e:
        logger.exception("fact gate failed")
        return {"success": False, "error": f"факт-гейт не отработал: {e}"}


router = Router(path="", route_handlers=[
    start_research_route, list_research_route, get_research_route,
    build_personas_route, list_personas_route, delete_persona_route,
    simulate_route, sizing_route, facts_route,
    list_simulations_route, personas_to_chat_route,
], tags=["Mark Research"])
