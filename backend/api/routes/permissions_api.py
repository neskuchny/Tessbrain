# -*- coding: utf-8 -*-
"""Управление правами на ресурсы (docs/ru/ACCESS_CONTROL_DESIGN.md, S1).

- GET    /permissions/{type}/{id}            — гранты ресурса
- POST   /permissions/{type}/{id}            — выдать {grantee_type,
         grantee_id, permission_type, expires_at?}
- DELETE /permissions/{type}/{id}            — отозвать {grantee_type,
         grantee_id} (body)
- POST   /permissions/{type}/{id}/check      — проверить себя
         {required, owner_id?, parents?}

Управлять грантами может владелец ресурса или держатель admin-гранта
(их же модель); владелец определяется СЕРВЕРОМ (owner_id из body не
принимается). Флаг enable_resource_permissions (default ON).
"""
from __future__ import annotations

import logging
from typing import Optional

from litestar import Request, Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    """См. integrations_daily_pulse._resolve_user — общая логика: 403 на
    PermissionError, 500 (с логом) на непредвиденную ошибку auth-слоя,
    401 если uid пуст. Тихий fail-open в Exception-ветке убран — это
    критично для permissions API (grant/revoke прав)."""
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"_resolve_user: auth subsystem error: {e}",
                     exc_info=True)
        raise HTTPException(status_code=500, detail="auth subsystem error")
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


def _guard() -> None:
    from backend.core.auth.resource_permissions import permissions_enabled
    if not permissions_enabled():
        raise HTTPException(
            status_code=403,
            detail="права выключены (enable_resource_permissions=false)")


async def _resolve_owner(resource_type: str, resource_id: str,
                         caller_uid: str) -> Optional[str]:
    """Настоящий владелец ресурса, определённый СЕРВЕРОМ.

    Клиентский owner_id из body не используется: раньше вызывающий мог
    прислать owner_id=<свой uid> и пройти owner-shortcut в effective_level
    на ЧУЖОМ ресурсе — т.е. выдать себе гранты на что угодно. Неизвестный
    тип ресурса → None (owner-shortcut недоступен, работают только
    существующие admin-гранты)."""
    try:
        if resource_type == "project":
            from backend.db.sima_client import get_sima_db
            db = await get_sima_db()
            proj = await db.get_project(resource_id)
            if proj:
                owner = str(proj.get("userId") or proj.get("user_id") or "")
                return owner or None
        elif resource_type == "dataset":
            # реестр датасетов per-user: ресурс в СВОЁМ реестре ⇒ владелец
            from backend.core.ontology.dataset_service import registry_for_user
            if registry_for_user(caller_uid).get(resource_id):
                return caller_uid
    except Exception:
        logger.debug("owner resolve failed (%s/%s)", resource_type,
                     resource_id, exc_info=True)
    return None


async def _can_manage(user_id: str, resource_type: str,
                      resource_id: str) -> bool:
    """Управлять грантами: владелец (определён сервером) или admin-грант."""
    from backend.core.auth import resource_permissions as rp
    owner_id = await _resolve_owner(resource_type, resource_id, user_id)
    res = await rp.check(user_id, "admin", resource_type=resource_type,
                         resource_id=resource_id, owner_id=owner_id,
                         log_denied=False)
    return bool(res["allowed"])


@get("/{resource_type:str}/{resource_id:str}")
async def list_grants_route(
        request: Request, resource_type: str, resource_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    _resolve_user(request, user_id)
    from backend.core.auth.resource_permissions import fetch_grants
    grants = await fetch_grants(resource_type, resource_id)
    for g in grants:
        if g.get("expires_at") is not None:
            g["expires_at"] = str(g["expires_at"])
    return {"grants": grants, "total": len(grants)}


@post("/{resource_type:str}/{resource_id:str}")
async def grant_route(
        request: Request, resource_type: str, resource_id: str,
        data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth import resource_permissions as rp
    if not await _can_manage(uid, resource_type, resource_id):
        return {"success": False,
                "error": "выдавать права может владелец или admin"}
    return await rp.grant(
        uid, resource_type=resource_type, resource_id=resource_id,
        grantee_type=data.get("grantee_type", "user"),
        grantee_id=str(data.get("grantee_id") or ""),
        permission_type=data.get("permission_type", "read"),
        expires_at=data.get("expires_at"))


@delete("/{resource_type:str}/{resource_id:str}", status_code=200)
async def revoke_route(
        request: Request, resource_type: str, resource_id: str,
        data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth import resource_permissions as rp
    if not await _can_manage(uid, resource_type, resource_id):
        return {"success": False,
                "error": "отзывать права может владелец или admin"}
    return await rp.revoke(
        uid, resource_type=resource_type, resource_id=resource_id,
        grantee_type=data.get("grantee_type", "user"),
        grantee_id=str(data.get("grantee_id") or ""))


@post("/{resource_type:str}/{resource_id:str}/check")
async def check_route(
        request: Request, resource_type: str, resource_id: str,
        data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Проверить свой доступ (для UI: показать/скрыть кнопки)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth import resource_permissions as rp
    res = await rp.check(
        uid, data.get("required", "read"),
        resource_type=resource_type, resource_id=resource_id,
        owner_id=data.get("owner_id"),
        parents=data.get("parents"), log_denied=False)
    return {"success": True, **res}


@post("/teams")
async def create_team_route(
        request: Request, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Создать команду (создатель — owner)."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth.teams import create_team
    return await create_team(uid, data.get("name", ""),
                             org_id=data.get("org_id"))


@get("/teams")
async def my_teams_route(
        request: Request,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth.teams import my_teams
    teams = await my_teams(uid)
    return {"teams": teams, "total": len(teams)}


@post("/teams/{team_id:str}/members")
async def team_member_route(
        request: Request, team_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Добавить/сменить роль: {user_id, role}. Удалить:
    {user_id, remove: true}. Только owner/admin команды; роль выше
    своей выдать нельзя."""
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth.teams import add_member, remove_member
    target = str(data.get("user_id") or "")
    if not target:
        return {"success": False, "error": "user_id обязателен"}
    if data.get("remove"):
        return await remove_member(uid, team_id, target)
    return await add_member(uid, team_id, target,
                            role=data.get("role", "member"))


# ─── S4: публичные ссылки на одиночные ресурсы ──────────────────────

@post("/shares/{resource_type:str}/{resource_id:str}")
async def public_share_create_route(
        request: Request, resource_type: str, resource_id: str,
        data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Создать публичную ссылку: {access_level?, expires_at?, max_views?,
    password?}. Право: владелец (определяется сервером) или admin-грант."""
    _guard()
    uid = _resolve_user(request, user_id)
    if not await _can_manage(uid, resource_type, resource_id):
        return {"success": False,
                "error": "создать ссылку может владелец или admin"}
    from backend.core.auth.public_shares import create_share
    return await create_share(
        uid, resource_type=resource_type, resource_id=resource_id,
        access_level=data.get("access_level", "read"),
        expires_at=data.get("expires_at"),
        max_views=data.get("max_views"),
        password=data.get("password"))


@delete("/shares/{share_id:str}", status_code=200)
async def public_share_revoke_route(
        request: Request, share_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    uid = _resolve_user(request, user_id)
    from backend.core.auth.public_shares import revoke_share
    return await revoke_share(uid, share_id)


@post("/shares/open/{token:str}")
async def public_share_open_route(
        request: Request, token: str, data: dict = Body(),
) -> dict:
    """Открыть по токену (БЕЗ авторизации, body: {password?})."""
    from backend.core.auth.public_shares import (
        consume_share,
        fetch_share,
        log_view,
    )
    rec = await fetch_share(token)
    res = consume_share(rec, password=(data or {}).get("password"))
    if not res["ok"]:
        return {"success": False, "error": res["error"]}
    s = res["share"]
    await log_view(str(s["id"]),
                   ip=request.headers.get("x-forwarded-for"),
                   user_agent=request.headers.get("user-agent"))
    return {"success": True,
            "resource_type": s["resource_type"],
            "resource_id": s["resource_id"],
            "access_level": s["access_level"]}


# ─── S6: численные лимиты тарифа ─────────────────────────────────────

@get("/limits/{tenant_id:str}")
async def limits_status_route(
        request: Request, tenant_id: str,
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    _guard()
    _resolve_user(request, user_id)
    from backend.core.auth.tenant_limits import limits_status
    return await limits_status(tenant_id)


@post("/limits/{tenant_id:str}")
async def limits_set_route(
        request: Request, tenant_id: str, data: dict = Body(),
        user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> dict:
    """Установить лимиты (для оператора инфры)."""
    _guard()
    _resolve_user(request, user_id)
    from backend.core.auth.tenant_limits import set_tenant_limits
    return await set_tenant_limits(
        tenant_id, plan=data.get("plan", "free"),
        monthly_tokens_usd=data.get("monthly_tokens_usd"),
        datasets_max=data.get("datasets_max"),
        projects_max=data.get("projects_max"),
        storage_mb=data.get("storage_mb"),
        seats=data.get("seats"),
        can_use_premium_tier=bool(data.get("can_use_premium_tier")))


permissions_router = Router(
    path="/permissions",
    route_handlers=[list_grants_route, grant_route, revoke_route,
                    check_route, create_team_route, my_teams_route,
                    team_member_route, public_share_create_route,
                    public_share_revoke_route, public_share_open_route,
                    limits_status_route, limits_set_route],
    tags=["Permissions"],
)
