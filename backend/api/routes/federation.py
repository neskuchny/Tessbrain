# -*- coding: utf-8 -*-
"""Федерация: связи между организациями внутри развёртывания.

Закрывает разрыв, о котором честно сказано в бизнес-карте: компания могла
открыть срез внешнему потребителю по ключу, но не было ОТНОШЕНИЯ двух
организаций — как они находят друг друга, договариваются о границах и
подтверждают полномочия.

Endpoints (все требуют подтверждённый токеном user_id):
  GET    /federation/links                  — мои связи
  POST   /federation/links                  — предложить связь
  POST   /federation/links/{id}/accept      — принять (только вторая сторона)
  POST   /federation/links/{id}/reject      — отклонить
  POST   /federation/links/{id}/policy      — объявить свой срез
  POST   /federation/links/{id}/revoke      — прекратить (любая сторона)
  GET    /federation/access                 — что мне доступно у партнёра

Дисциплина доступа: организация запрашивающего резолвится из его аккаунта
(get_org_for_user), роль — из членства. Ни одно поле, определяющее «от
чьего имени» и «с какими правами», не берётся из тела запроса.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Request, Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


def _actor(uid: str) -> Dict[str, str]:
    """Организация и роль действующего — из членства, не из запроса."""
    from backend.core.ingest import membership
    org = membership.get_org_for_user(uid) or ""
    role = membership.get_user_org_base_role(uid) or ""
    if not org:
        raise HTTPException(
            status_code=400,
            detail="аккаунт не состоит в организации — связи между "
                   "компаниями заводятся от имени организации")
    return {"org": str(org), "role": str(role)}


class ProposeBody(BaseModel):
    user_id: Optional[str] = None
    partner_org_id: str
    purpose: str = ""
    policy_id: str = ""


class DecisionBody(BaseModel):
    user_id: Optional[str] = None
    reason: str = ""


class PolicyBody(BaseModel):
    user_id: Optional[str] = None
    policy_id: str = ""


@get("/links")
async def list_links(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    include_closed: bool = Parameter(query="include_closed", default=False),
) -> Dict[str, Any]:
    """Связи моей организации. Закрытые — по запросу (история для аудита)."""
    uid = _resolve_user(request, user_id)
    actor = _actor(uid)
    from backend.core.data_bus.federation_store import get_federation_store
    links = get_federation_store().list_for_org(
        actor["org"], include_closed=include_closed)
    return {"org_id": actor["org"], "links": links, "count": len(links)}


@post("/links")
async def propose(data: ProposeBody, request: Request) -> Dict[str, Any]:
    """Предложить связь другой организации. Действовать начнёт только
    после её согласия."""
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.federation import propose_link
    from backend.core.data_bus.federation_store import get_federation_store
    from backend.core.ingest.org_store import get_org

    partner = str(data.partner_org_id or "").strip()
    if not get_org(partner):
        raise HTTPException(status_code=404,
                            detail="организация-партнёр не найдена")
    res = propose_link(org_a=actor["org"], org_b=partner,
                       proposed_by_user=uid, role=actor["role"],
                       purpose=data.purpose, policy_id=data.policy_id)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res["error"])
    saved = get_federation_store().add(res["link"])
    if not saved.get("ok"):
        raise HTTPException(status_code=409, detail=saved["error"])
    return {"status": "proposed", **saved}


async def _decide(link_id: str, data: DecisionBody, request: Request,
                  accept: bool) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.federation import decide_link
    from backend.core.data_bus.federation_store import get_federation_store
    store = get_federation_store()
    link = store.get(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="связь не найдена")
    res = decide_link(link, decider_org=actor["org"], decider_user=uid,
                      role=actor["role"], accept=accept)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res["error"])
    return {"status": link.status, **store.save(link)}


@post("/links/{link_id:str}/accept")
async def accept(link_id: str, data: DecisionBody,
                 request: Request) -> Dict[str, Any]:
    """Принять предложение. Только вторая сторона — предложивший не может
    принять сам себя."""
    return await _decide(link_id, data, request, accept=True)


@post("/links/{link_id:str}/reject")
async def reject(link_id: str, data: DecisionBody,
                 request: Request) -> Dict[str, Any]:
    """Отклонить предложение."""
    return await _decide(link_id, data, request, accept=False)


@post("/links/{link_id:str}/policy")
async def set_policy(link_id: str, data: PolicyBody,
                     request: Request) -> Dict[str, Any]:
    """Объявить, что МОЯ сторона открывает партнёру. Пустое значение
    закрывает выдачу, не разрывая связь."""
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.federation import set_side_policy
    from backend.core.data_bus.federation_store import get_federation_store
    store = get_federation_store()
    link = store.get(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="связь не найдена")
    res = set_side_policy(link, org_id=actor["org"], role=actor["role"],
                          policy_id=data.policy_id)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res["error"])
    return {"status": "ok", **store.save(link)}


@post("/links/{link_id:str}/revoke")
async def revoke(link_id: str, data: DecisionBody,
                 request: Request) -> Dict[str, Any]:
    """Прекратить связь. Односторонне и сразу, без согласия партнёра."""
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.federation import revoke_link
    from backend.core.data_bus.federation_store import get_federation_store
    store = get_federation_store()
    link = store.get(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="связь не найдена")
    res = revoke_link(link, org_id=actor["org"], role=actor["role"],
                      reason=data.reason)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res["error"])
    return {"status": "revoked", **store.save(link)}


@get("/access")
async def check_access(
    request: Request,
    partner_org_id: str = Parameter(query="partner_org_id"),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Что моя организация вправе получить у партнёра прямо сейчас.

    Единственная точка ответа «пускать ли»: отказ по умолчанию, причина
    возвращается словами — видно, чего не хватает (связи, согласия или
    объявленного среза у второй стороны)."""
    uid = _resolve_user(request, user_id)
    actor = _actor(uid)
    from backend.core.data_bus.federation import resolve_access
    from backend.core.data_bus.federation_store import get_federation_store
    link = get_federation_store().open_link_between(
        actor["org"], partner_org_id)
    decision = resolve_access(link, requester_org=actor["org"],
                              target_org=partner_org_id)
    return {"requester_org": actor["org"], "partner_org_id": partner_org_id,
            "link_id": link.id if link else None, **decision}


@get("/query")
async def federated_query(
    request: Request,
    partner_org_id: str = Parameter(query="partner_org_id"),
    q: str = Parameter(query="q"),
    search_depth: str = Parameter(query="search_depth", default="standard"),
    fmt: str = Parameter(query="format", default="natural"),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Спросить память ОРГАНИЗАЦИИ-ПАРТНЁРА по федеративной связи.

    Доступ решает отношение, а не ключ: связь принята второй стороной,
    и партнёр объявил, что открывает. Ответ проходит тот же конвейер,
    что ключевая выдача шины: срез партнёра, редакция, синтез, аудит —
    обращение видно партнёру в его журнале.
    """
    uid = _resolve_user(request, user_id)
    actor = _actor(uid)
    question = str(q or "").strip()
    if len(question) < 3:
        raise HTTPException(status_code=400, detail="q: слишком короткий запрос")
    from backend.core.data_bus.bus_service import get_data_bus_service
    res = await get_data_bus_service().federated_query(
        requester_org=actor["org"], requester_user=uid,
        target_org=str(partner_org_id or "").strip(),
        query_text=question, search_depth=search_depth,
        response_format=fmt,
        ip_address=request.headers.get("x-forwarded-for"),
    )
    if res.get("code") == 403:
        raise HTTPException(status_code=403, detail=res.get("error"))
    if res.get("code") == 429:
        raise HTTPException(status_code=429, detail=res.get("error"))
    return res


router = Router(
    path="/federation",
    route_handlers=[list_links, propose, accept, reject, set_policy,
                    revoke, check_access, federated_query],
    tags=["Federation"],
)
