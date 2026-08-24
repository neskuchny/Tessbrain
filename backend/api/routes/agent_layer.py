# -*- coding: utf-8 -*-
"""Слой внешних агентов: реестр + задачи с машинной приёмкой.

Две стороны с разной аутентификацией — и это принципиально:
  - ОРГАНИЗАЦИЯ (JWT сотрудника): регистрирует агентов, предлагает задачи,
    выносит финальное решение. Личность и роль — из членства.
  - АГЕНТ (X-API-Key своего канала шины): берёт задачи и сдаёт результат.
    Агент опознаётся по каналу, к которому привязан при регистрации, —
    отдельного «агентского логина» нет, и это осознанно: канал уже несёт
    границы доступа и отзывается одной кнопкой.

Endpoints:
  организация —
    GET  /agent-layer/agents             POST /agent-layer/agents
    POST /agent-layer/agents/{id}/status
    GET  /agent-layer/tasks              POST /agent-layer/tasks
    POST /agent-layer/tasks/{id}/close
  агент (X-API-Key) —
    GET  /agent-layer/inbox              POST /agent-layer/tasks/{id}/take
    POST /agent-layer/tasks/{id}/submit
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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
    from backend.core.ingest import membership
    org = membership.get_org_for_user(uid) or ""
    role = membership.get_user_org_base_role(uid) or ""
    if not org:
        raise HTTPException(status_code=400,
                            detail="аккаунт не состоит в организации")
    return {"org": str(org), "role": str(role)}


async def _agent_from_key(x_api_key: Optional[str]):
    """Опознать агента по ключу его канала. Возвращает (agent, store).

    Ключ → consumer шины → зарегистрированный на этом канале агент.
    Ключ валиден, но агента на нём нет → 403 с объяснением: доступ к
    данным не равен праву брать задачи.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key required")
    from backend.core.data_bus.agent_layer_store import AgentLayerStore
    from backend.core.data_bus.bus_service import get_data_bus_service
    consumer = await get_data_bus_service().resolve_consumer(x_api_key)
    if not consumer:
        raise HTTPException(status_code=401,
                            detail="ключ не распознан или истёк")
    store = AgentLayerStore(consumer.tenant_id)
    agent = store.find_agent_by_channel("consumer", consumer.id)
    if not agent:
        raise HTTPException(
            status_code=403,
            detail="на этом канале нет зарегистрированного агента: доступ "
                   "к данным не равен праву брать задачи — попросите "
                   "администратора организации зарегистрировать агента")
    return agent, store


# ── Организация: реестр ─────────────────────────────────────────────────

class RegisterAgentBody(BaseModel):
    user_id: Optional[str] = None
    name: str
    channel_kind: str = "consumer"
    channel_id: str
    capabilities: List[str] = []
    operator: str = ""


class AgentStatusBody(BaseModel):
    user_id: Optional[str] = None
    status: str


@get("/agents")
async def list_agents(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    include_retired: bool = Parameter(query="include_retired", default=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    actor = _actor(uid)
    from backend.core.data_bus.agent_layer_store import AgentLayerStore
    agents = AgentLayerStore(actor["org"]).list_agents(include_retired)
    return {"agents": agents, "count": len(agents)}


@post("/agents")
async def register_agent_route(data: RegisterAgentBody,
                               request: Request) -> Dict[str, Any]:
    """Зарегистрировать внешнего агента на существующем канале доступа."""
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.agent_layer import register_agent
    from backend.core.data_bus.agent_layer_store import AgentLayerStore

    # Канал должен существовать и принадлежать этой организации.
    if data.channel_kind == "consumer":
        from backend.core.data_bus.bus_service import get_data_bus_service
        c = await get_data_bus_service().storage.get_consumer(data.channel_id)
        if not c or str(c.tenant_id) != actor["org"]:
            raise HTTPException(status_code=404,
                                detail="канал (потребитель шины) не найден "
                                       "в вашей организации")
    elif data.channel_kind == "federation":
        from backend.core.data_bus.federation_store import (
            get_federation_store,
        )
        link = get_federation_store().get(data.channel_id)
        if not link or not link.involves(actor["org"]):
            raise HTTPException(status_code=404,
                                detail="федеративная связь не найдена "
                                       "у вашей организации")
    res = register_agent(
        org_id=actor["org"], name=data.name,
        channel_kind=data.channel_kind, channel_id=data.channel_id,
        role=actor["role"], registered_by=uid,
        capabilities=data.capabilities, operator=data.operator)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res["error"])
    saved = AgentLayerStore(actor["org"]).save_agent(res["agent"])
    return {"status": "registered", **saved}


@post("/agents/{agent_id:str}/status")
async def agent_status(agent_id: str, data: AgentStatusBody,
                       request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.agent_layer import set_agent_status
    from backend.core.data_bus.agent_layer_store import AgentLayerStore
    store = AgentLayerStore(actor["org"])
    agent = store.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="агент не найден")
    res = set_agent_status(agent, role=actor["role"], status=data.status)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res["error"])
    return {"status": "ok", **store.save_agent(agent)}


# ── Организация: задачи ─────────────────────────────────────────────────

class OfferTaskBody(BaseModel):
    user_id: Optional[str] = None
    agent_id: str
    title: str
    spec_text: str
    acceptance: List[Dict[str, Any]] = []


class CloseTaskBody(BaseModel):
    user_id: Optional[str] = None
    approve: bool


@get("/tasks")
async def list_tasks(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    status: Optional[str] = Parameter(query="status", default=None),
    agent_id: Optional[str] = Parameter(query="agent_id", default=None),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    actor = _actor(uid)
    from backend.core.data_bus.agent_layer_store import AgentLayerStore
    tasks = AgentLayerStore(actor["org"]).list_tasks(
        agent_id=agent_id, status=status)
    return {"tasks": tasks, "count": len(tasks)}


@post("/tasks")
async def offer_task_route(data: OfferTaskBody,
                           request: Request) -> Dict[str, Any]:
    """Предложить задачу агенту — с проверками приёмки, если есть чем
    проверять. Без проверок машинная приёмка честно скажет «не доказано»."""
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.agent_layer import offer_task
    from backend.core.data_bus.agent_layer_store import AgentLayerStore
    store = AgentLayerStore(actor["org"])
    agent = store.get_agent(data.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="агент не найден")
    res = offer_task(org_id=actor["org"], agent=agent, title=data.title,
                     spec_text=data.spec_text, created_by=uid,
                     acceptance=data.acceptance)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res["error"])
    return {"status": "offered", **store.save_task(res["task"])}


@post("/tasks/{task_id:str}/close")
async def close_task_route(task_id: str, data: CloseTaskBody,
                           request: Request) -> Dict[str, Any]:
    """Финальное решение человека: машина отобрала — человек закрывает."""
    uid = _resolve_user(request, data.user_id)
    actor = _actor(uid)
    from backend.core.data_bus.agent_layer import close_task
    from backend.core.data_bus.agent_layer_store import AgentLayerStore
    store = AgentLayerStore(actor["org"])
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="задача не найдена")
    res = close_task(task, closed_by=uid, approve=data.approve)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res["error"])
    return {"status": task.status, **store.save_task(task)}


# ── Агент (X-API-Key) ───────────────────────────────────────────────────

class SubmitBody(BaseModel):
    result_text: str


@get("/inbox")
async def agent_inbox(
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
) -> Dict[str, Any]:
    """Задачи, адресованные этому агенту (по его каналу)."""
    agent, store = await _agent_from_key(x_api_key)
    tasks = [t for t in store.list_tasks(agent_id=agent.id)
             if t.get("status") in ("offered", "returned", "in_progress")]
    return {"agent_id": agent.id, "agent_name": agent.name,
            "tasks": tasks, "count": len(tasks)}


@post("/tasks/{task_id:str}/take")
async def take_task_route(
    task_id: str,
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
) -> Dict[str, Any]:
    agent, store = await _agent_from_key(x_api_key)
    from backend.core.data_bus.agent_layer import take_task
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="задача не найдена")
    res = take_task(task, agent_id=agent.id)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res["error"])
    return {"status": task.status, **store.save_task(task)}


@post("/tasks/{task_id:str}/submit")
async def submit_task_route(
    task_id: str,
    data: SubmitBody,
    x_api_key: Optional[str] = Parameter(header="X-API-Key", default=None),
) -> Dict[str, Any]:
    """Сдать результат. Машинная приёмка выполняется сразу: провал
    возвращает задачу с конкретными замечаниями."""
    agent, store = await _agent_from_key(x_api_key)
    from backend.core.data_bus.agent_layer import submit_result
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="задача не найдена")
    res = submit_result(task, agent_id=agent.id,
                        result_text=data.result_text)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res["error"])
    saved = store.save_task(task)
    return {"status": task.status, "verdict": res.get("verdict"), **saved}


router = Router(
    path="/agent-layer",
    route_handlers=[list_agents, register_agent_route, agent_status,
                    list_tasks, offer_task_route, close_task_route,
                    agent_inbox, take_task_route, submit_task_route],
    tags=["Agent Layer"],
)
