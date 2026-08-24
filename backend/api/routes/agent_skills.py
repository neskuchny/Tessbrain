# -*- coding: utf-8 -*-
"""Навыки агента: личные + орг-слой с одобрением админа (перенос из QM).

GET  /agent-skills          — {personal, org, pending, can_approve}
POST /agent-skills/propose  — предложить СВОЙ навык в организацию
POST /agent-skills/approve  — одобрить предложение (только founder/admin)
POST /agent-skills/reject   — отклонить предложение (только founder/admin)
GET  /agent-skills/view     — текст SKILL.md (scope=personal|org)

Прозрачность: pending видят ВСЕ члены организации; право одобрять/
отклонять — только MANAGEMENT_ROLES (через can_manage_org, которая
резолвит кастомные роли в base и учитывает дерево организаций).
Auth: Bearer (trusted_user_id), как chat_sources.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)

_NO_ORG_MSG = "вы не состоите в организации"
_NOT_ADMIN_MSG = ("только администратор организации (founder/admin) "
                  "может это делать")


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


def _store():
    from backend.core.hermes.skill_store import SkillStore
    return SkillStore()


def _org_of(user_id: str) -> Optional[str]:
    """org_id пользователя или None (best-effort — без membership
    работаем в персональном режиме, а не падаем)."""
    try:
        from backend.core.ingest.membership import get_org_for_user
        return get_org_for_user(user_id)
    except Exception:
        logger.debug("agent_skills: org resolve failed", exc_info=True)
        return None


def _is_admin(user_id: str, org_id: str) -> bool:
    """founder/admin (MANAGEMENT_ROLES) — через can_manage_org: она
    резолвит кастомные роли (resolve_base_role) и дерево организаций."""
    try:
        from backend.core.ingest.membership import can_manage_org
        return can_manage_org(user_id, org_id)
    except Exception:
        logger.debug("agent_skills: admin check failed", exc_info=True)
        return False  # fail-closed: сомнение = не админ


@get("/")
async def agent_skills_list(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    store = _store()
    personal = [m.to_dict() for m in store.list_personal(user_id)]
    org_id = _org_of(user_id)
    if not org_id:
        return {"status": "success", "personal": personal, "org": [],
                "pending": [], "can_approve": False, "org_id": None}
    org: list[dict[str, Any]] = []
    for m in store.list_org(org_id):
        doc = store.view_org(org_id, m.name)
        org.append({
            **m.to_dict(),
            "shared_by": doc.shared_by if doc else "",
            "approved_by": doc.approved_by if doc else "",
            "approved_at": doc.approved_at if doc else "",
        })
    pending = [
        {**d.meta().to_dict(), "shared_by": d.shared_by}
        for d in store.list_pending(org_id)
    ]
    return {
        "status": "success",
        "personal": personal,
        "org": org,
        "pending": pending,  # прозрачность: видят все члены орги
        "can_approve": _is_admin(user_id, org_id),
        "org_id": org_id,
    }


@post("/propose")
async def agent_skills_propose(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    name = str(data.get("name") or "").strip()
    if not name:
        return {"status": "error", "message": "не указано имя навыка"}
    org_id = _org_of(user_id)
    if not org_id:
        return {"status": "error", "message": _NO_ORG_MSG}
    store = _store()
    if store.propose_to_org(user_id, org_id, name):
        return {"status": "success",
                "message": "навык предложен организации, ждёт одобрения "
                           "администратора"}
    # честно различаем причины отказа: навык есть, но копирование не
    # прошло → в _pending лежит чужое предложение с этим именем
    if any(m.name == name for m in store.list_personal(user_id)):
        return {"status": "error",
                "message": "предложение с этим именем уже подано другим "
                           "участником"}
    return {"status": "error",
            "message": "у вас нет личного навыка с таким именем"}


@post("/approve")
async def agent_skills_approve(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_of(user_id)
    if not org_id:
        return {"status": "error", "message": _NO_ORG_MSG}
    if not _is_admin(user_id, org_id):
        return {"status": "error", "message": _NOT_ADMIN_MSG}
    ok = _store().approve(org_id, str(data.get("category") or "general"),
                          str(data.get("name") or ""), user_id)
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "предложение не найдено"})}


@post("/reject")
async def agent_skills_reject(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    org_id = _org_of(user_id)
    if not org_id:
        return {"status": "error", "message": _NO_ORG_MSG}
    if not _is_admin(user_id, org_id):
        return {"status": "error", "message": _NOT_ADMIN_MSG}
    ok = _store().reject(org_id, str(data.get("category") or "general"),
                         str(data.get("name") or ""))
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "предложение не найдено"})}


@get("/view")
async def agent_skills_view(
    name: str = Parameter(query="name"),
    # python-имя не «scope» — это зарезервированный kwarg Litestar
    skill_scope: str = Parameter(query="scope", default="personal",
                                 required=False),
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    user_id = _caller(authorization)
    from backend.core.hermes.skill_store import serialize
    store = _store()
    scope = skill_scope
    if scope == "org":
        org_id = _org_of(user_id)
        if not org_id:
            return {"status": "error", "message": _NO_ORG_MSG}
        doc = store.view_org(org_id, name)
    else:
        doc = store.view(user_id, name)
    if doc is None:
        return {"status": "error", "message": "навык не найден"}
    return {"status": "success", "name": doc.name, "scope": scope,
            "content": serialize(doc)}


router = Router(path="/agent-skills", route_handlers=[
    agent_skills_list, agent_skills_propose, agent_skills_approve,
    agent_skills_reject, agent_skills_view,
], tags=["Agent Skills"])
