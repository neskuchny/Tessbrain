# -*- coding: utf-8 -*-
"""Симуляция клиентов, сегментов и партнёров — API.

GET  /clients/sim/clients          — реальные клиенты из графа + сегменты
GET  /clients/sim/clients/{id}     — досье клиента (без LLM)
POST /clients/sim/market-groups    — построить гипотезные группы рынка
GET  /clients/sim/market-groups    — сохранённые группы
POST /clients/sim/market-groups/delete — удалить группу
POST /clients/sim/offer            — панель реакций на оффер
GET  /clients/sim/simulations      — история панелей
POST /clients/sim/chat             — диалог с клиентом/группой
GET  /clients/sim/partners         — кандидаты в партнёрскую симуляцию
POST /clients/sim/partner/chat     — переговоры с партнёром (слепок)
POST /clients/sim/partner/pack     — пакет: концепция/КП/условия/план

Auth: Bearer (trusted_user_id), как mark_research.
"""
from __future__ import annotations

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


@get("/clients/sim/clients")
async def sim_clients(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    out = await client_sim.list_clients(user_id)
    return {"status": "success", **out}


@get("/clients/sim/clients/{client_id:str}")
async def sim_client_dossier(
    client_id: str,
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return await client_sim.client_dossier(user_id, client_id)


@post("/clients/sim/market-groups")
async def sim_build_market_groups(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return await client_sim.build_market_groups(
        user_id, market=str(data.get("market") or ""),
        product=str(data.get("product") or ""))


@get("/clients/sim/market-groups")
async def sim_list_market_groups(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return {"status": "success",
            "groups": client_sim.list_market_groups(user_id)}


@post("/clients/sim/market-groups/delete")
async def sim_delete_market_group(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    ok = client_sim.delete_market_group(
        user_id, str(data.get("group_id") or ""))
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "группа не найдена"})}


@post("/clients/sim/offer")
async def sim_offer(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return await client_sim.simulate_offer(
        user_id, offer=str(data.get("offer") or ""),
        client_ids=[str(x) for x in (data.get("client_ids") or [])],
        group_ids=[str(x) for x in (data.get("group_ids") or [])])


@get("/clients/sim/simulations")
async def sim_history(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return {"status": "success",
            "simulations": client_sim.list_simulations(user_id)}


@post("/clients/sim/chat")
async def sim_chat(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return await client_sim.chat_with_client(
        user_id,
        client_id=str(data.get("client_id") or ""),
        group_id=str(data.get("group_id") or ""),
        message=str(data.get("message") or ""),
        history=data.get("history") or [])


@get("/clients/sim/partners")
async def sim_partners(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    out = await client_sim.list_partner_candidates(user_id)
    return {"status": "success", **out}


@post("/clients/sim/partner/chat")
async def sim_partner_chat(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    mode = str(data.get("mode") or "negotiation")
    return await client_sim.partner_chat(
        user_id,
        person_id=str(data.get("person_id") or ""),
        message=str(data.get("message") or ""),
        history=data.get("history") or [],
        mode=mode if mode in ("negotiation", "co_create") else "negotiation")


@post("/clients/sim/partner/pack")
async def sim_partner_pack(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.marketing import client_sim
    user_id = _caller(authorization)
    return await client_sim.partner_pack(
        user_id,
        person_id=str(data.get("person_id") or ""),
        focus=str(data.get("focus") or ""),
        history=data.get("history") or [])


router = Router(path="", route_handlers=[
    sim_clients, sim_client_dossier,
    sim_build_market_groups, sim_list_market_groups, sim_delete_market_group,
    sim_offer, sim_history, sim_chat,
    sim_partners, sim_partner_chat, sim_partner_pack,
], tags=["Client Simulation"])
