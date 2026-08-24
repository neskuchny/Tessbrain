# -*- coding: utf-8 -*-
"""Admin endpoints для управления organizations и membership.

Wave 2.2 — organizations + membership CRUD.
Wave 3.1 — invite-based onboarding (POST /orgs/{id}/invites + accept flow).

Endpoints (mounted под /api/v1):
    POST   /orgs                                — создать org (system admin only)
    GET    /orgs/{org_id}                       — метадата org (member или admin)
    GET    /orgs/{org_id}/members               — список members (member или admin)
    POST   /orgs/{org_id}/members               — добавить (org founder/admin или sys-admin)
    DELETE /orgs/{org_id}/members/{user_id}     — удалить (org admin или self)
    PATCH  /orgs/{org_id}/members/{user_id}     — сменить роль (org admin или sys-admin)

Wave 3.1 — Onboarding via invite:
    POST   /orgs/{org_id}/invites               — создать invite (org admin) → raw-token
    GET    /orgs/{org_id}/invites               — список pending (org admin)
    DELETE /orgs/{org_id}/invites/{invite_id}   — revoke (org admin)
    GET    /invites/{token}                     — landing-info (public с токеном)
    POST   /invites/{token}/accept              — принять и стать member (auth)

Auth-model:
    - System-level Role.ADMIN (из JWT claim `role`) — может всё в любой org.
    - Org-level founder/admin (membership.json `role`) — может управлять
      своей org.
    - Member любой роли — может читать свою org.
    - Self-removal: любой member может удалить себя.
    - Accept invite: любой authenticated user.

Audit-log пишется для всех mutating операций.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, delete, get, patch, post, put
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.auth.rbac import (
    PermissionDenied,
    Role,
    extract_role_from_claims,
    set_current_role,
)
from backend.core.ingest import invite_store, membership, org_store
from backend.core.observability import audit_log

logger = logging.getLogger(__name__)


# === Auth helpers ==========================================================


def _extract_caller(authorization: Optional[str]) -> tuple[str, Role]:
    """Достать (user_id, system_role) из Authorization header.

    Verify-first: подпись проверяется (service/user-JWT). Никакого глобального
    middleware, валидирующего подпись, НЕТ — раньше здесь был decode с
    verify_signature=False, и подделанный токен {"sub": x, "role": "admin"}
    давал system-admin на все /orgs и /holdings. Теперь системная роль
    берётся ТОЛЬКО из claims с подтверждённой подписью; в compat-режиме
    (enable_strict_chat_auth=off) неподписанный sub ещё принимается для
    обратной совместимости, но роль такого токена всегда NONE — эскалация
    через подделку role-claim закрыта в любом режиме."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    from backend.core.auth.service_token import (
        decode_claims_guarded, verify_service_token, verify_user_token,
    )
    verified = verify_service_token(token) or verify_user_token(token)
    if verified is not None:
        claims = verified
        role = extract_role_from_claims(claims)
    else:
        try:
            claims = decode_claims_guarded(token)  # strict → {}, compat → unverified
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
        role = Role.NONE  # роль из неподписанного токена не признаём никогда
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    set_current_role(role)
    return str(user_id), role


def _require_system_admin(authorization: Optional[str]) -> str:
    """Требует system-level Role.ADMIN. Возвращает user_id."""
    user_id, role = _extract_caller(authorization)
    if role < Role.ADMIN:
        raise PermissionDenied(Role.ADMIN, role)
    return user_id


def _require_org_admin_or_system(
    authorization: Optional[str], org_id: str,
) -> str:
    """Caller должен быть либо system-admin, либо founder/admin указанной org."""
    caller_id, sys_role = _extract_caller(authorization)
    if sys_role >= Role.ADMIN:
        return caller_id
    if membership.can_manage_org(caller_id, org_id):
        return caller_id
    raise HTTPException(
        status_code=403,
        detail=f"caller is not an admin of org {org_id}",
    )


def _require_org_member_or_system(
    authorization: Optional[str], org_id: str,
) -> str:
    """Caller должен быть либо system-admin, либо членом org (любая роль)."""
    caller_id, sys_role = _extract_caller(authorization)
    if sys_role >= Role.ADMIN:
        return caller_id
    if membership.is_org_member(caller_id, org_id):
        return caller_id
    raise HTTPException(
        status_code=403,
        detail=f"caller is not a member of org {org_id}",
    )


def _ensure_org_exists(org_id: str) -> dict[str, Any]:
    meta = org_store.get_org(org_id)
    if not meta:
        raise HTTPException(status_code=404, detail=f"org {org_id} not found")
    return meta


# === Endpoints =============================================================


@post("/orgs", status_code=201)
async def create_org(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Создать новую организацию.

    P3 мульти-аккаунта — self-serve: любой аутентифицированный пользователь
    БЕЗ организации может создать компанию для себя (становится founder;
    org_id генерируется). System-admin по-прежнему может bootstrap'ить оргу
    для другого пользователя (founder_user_id + свой org_id).

    Body: `{"name": "Acme Inc", "org_id"?: "<uuid>", "founder_user_id"?: "<uuid>",
    "parent_org_id"?: "<uuid>"}`.

    parent_org_id (enterprise): создать ДОЧЕРНЮЮ организацию (филиал/
    корпорацию в дереве). Право — founder/admin родительской организации
    (или system-admin); создатель становится founder дочки secondary-
    членством, активная организация не меняется.
    """
    caller_id, sys_role = _extract_caller(authorization)
    is_system_admin = sys_role >= Role.ADMIN
    if not caller_id:
        raise HTTPException(status_code=401, detail="auth required")

    org_id = (data.get("org_id") or "").strip()
    name = (data.get("name") or "").strip()
    founder_id = (data.get("founder_user_id") or "").strip() or caller_id
    parent_org_id = (data.get("parent_org_id") or "").strip() or None
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    if parent_org_id and not is_system_admin:
        if not membership.can_manage_org(caller_id, parent_org_id):
            raise HTTPException(
                status_code=403,
                detail="создавать дочерние организации может только "
                       "founder/admin родительской")

    if not is_system_admin:
        # self-serve: только для себя
        if founder_id != caller_id:
            raise HTTPException(
                status_code=403,
                detail="founder_user_id может задавать только system-admin")
        if membership.get_org_for_user(caller_id) and not parent_org_id:
            raise HTTPException(
                status_code=409,
                detail="вы уже состоите в организации; создать можно "
                       "дочернюю (parent_org_id) в дереве своей компании")
        org_id = ""  # id всегда генерируем — не даём занять произвольный
    if not org_id:
        import uuid as _uuid
        org_id = str(_uuid.uuid4())

    try:
        org_meta = org_store.create_org(org_id, name=name, created_by=caller_id,
                                        parent_org_id=parent_org_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    try:
        # Дочка при существующем членстве → secondary founder (активная org
        # создателя не меняется; переключение — POST /my-org/switch).
        as_secondary = bool(membership.get_org_for_user(founder_id))
        member = membership.add_member(
            founder_id, org_id, role=membership.ROLE_FOUNDER,
            secondary=as_secondary,
        )
    except ValueError as e:
        # Если founder уже в другой org — откатить создание org вручную не
        # можем (нет транзакций), но логируем для оператора.
        logger.error("Org %s created but founder %s could not be added: %s",
                     org_id, founder_id, e)
        raise HTTPException(status_code=409, detail=str(e))

    await audit_log.emit(
        action="orgs.create",
        user_id=caller_id,
        resource=f"org:{org_id}",
        metadata={"name": name, "founder": founder_id,
                  "parent_org_id": parent_org_id},
    )
    return {"org": org_meta, "founder": member}


@get("/orgs/{org_id:str}", status_code=200)
async def get_org_info(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Получить метадату org + member count. Member или system-admin."""
    meta = _ensure_org_exists(org_id)
    _require_org_member_or_system(authorization, org_id)
    members = membership.list_members(org_id)
    return {**meta, "member_count": len(members)}


@get("/orgs/{org_id:str}/members", status_code=200)
async def list_org_members(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Список всех members org."""
    _ensure_org_exists(org_id)
    _require_org_member_or_system(authorization, org_id)
    members = membership.list_members(org_id)
    return {"org_id": org_id, "members": members, "count": len(members)}


@post("/orgs/{org_id:str}/members", status_code=201)
async def add_org_member(
    org_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Добавить user в org. Body: `{"user_id", "role"?, "access_level"?,
    "secondary"?}`.

    role: встроенная (founder/admin/manager/employee/contractor) или
    кастомная роль этой организации (default employee).
    access_level: int 0..3 (default 2 = INTERNAL).
    secondary: true — второе членство для user'а, уже состоящего в другой
    организации (директор портфеля); его активная организация не меняется.
    """
    _ensure_org_exists(org_id)
    caller_id = _require_org_admin_or_system(authorization, org_id)
    new_user_id = (data.get("user_id") or "").strip()
    if not new_user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    role = data.get("role") or membership.ROLE_EMPLOYEE
    access_level = data.get("access_level", membership.DEFAULT_ACCESS_LEVEL)
    secondary = bool(data.get("secondary"))

    try:
        member = membership.add_member(
            new_user_id, org_id, role=role, access_level=access_level,
            secondary=secondary,
        )
    except ValueError as e:
        # 409: либо unknown role, либо user уже в другой org.
        raise HTTPException(status_code=409, detail=str(e))

    await audit_log.emit(
        action="orgs.members.add",
        user_id=caller_id,
        resource=f"org:{org_id}",
        metadata={"added_user_id": new_user_id, "role": member.get("role"),
                  "secondary": secondary},
    )
    return member


@delete("/orgs/{org_id:str}/members/{user_id:str}", status_code=200)
async def remove_org_member(
    org_id: str,
    user_id: str,
    freeze_twin: bool = True,
    reassign_reports_to: Optional[str] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Удалить user из org. Доступно org-admin'у или самому user (self-leave).

    Кроме членства закрывает хвосты ухода: прямые подчинённые переходят к
    руководителю ушедшего (или к `reassign_reports_to`), слепок замораживается,
    выданные человеком согласия отзываются. `freeze_twin=false` оставляет
    слепок доступным — осознанный выбор организации, а не умолчание.

    Защита: нельзя удалить последнего founder — org остался бы без admin'а.
    """
    _ensure_org_exists(org_id)
    caller_id, sys_role = _extract_caller(authorization)
    is_self = caller_id == user_id
    is_admin = sys_role >= Role.ADMIN or membership.can_manage_org(caller_id, org_id)
    if not (is_self or is_admin):
        raise HTTPException(
            status_code=403,
            detail="only org admin or the user themselves can remove membership",
        )

    # Защита: последний founder не может уйти, пока кто-то другой не станет
    # founder/admin. Иначе org остаётся без owner'а — невозможно управлять.
    target_role = membership.get_user_org_role(user_id)
    if target_role == membership.ROLE_FOUNDER:
        all_members = membership.list_members(org_id)
        other_admins = [
            m for m in all_members
            if m["user_id"] != user_id
            and m.get("role") in membership.MANAGEMENT_ROLES
        ]
        if not other_admins:
            raise HTTPException(
                status_code=409,
                detail="cannot remove the last founder/admin; promote another member first",
            )

    # Уход — это не только вычеркнуть из списка. Подчинённые не должны
    # повиснуть на несуществующем руководителе, слепок не должен продолжать
    # говорить от лица ушедшего, а выданные им согласия — действовать.
    # freeze_twin=false оставляет слепок доступным: сценарий «человек ушёл,
    # знания остались» законен, но включаться должен осознанно.
    from backend.core.ingest.offboarding import offboard
    result = await offboard(user_id, org_id, actor_id=caller_id,
                            reassign_reports_to=reassign_reports_to,
                            freeze=freeze_twin)
    if not result.get("membership_removed"):
        raise HTTPException(
            status_code=404,
            detail=f"user {user_id} is not a member of org {org_id}",
        )

    await audit_log.emit(
        action="orgs.members.remove",
        user_id=caller_id,
        resource=f"org:{org_id}",
        metadata={"removed_user_id": user_id, "self": is_self,
                  "reassigned": len(result.get("reassigned") or []),
                  "orphaned": len(result.get("orphaned") or []),
                  "twin_frozen": result.get("twin_frozen")},
    )
    return {
        "removed": True, "user_id": user_id, "org_id": org_id,
        # Отдаём наружу, что именно произошло: «осиротевшие» подчинённые —
        # это решение для администратора, а не деталь реализации.
        "reassigned": result.get("reassigned") or [],
        "orphaned": result.get("orphaned") or [],
        "twin_frozen": bool(result.get("twin_frozen")),
        "consents_revoked": int(result.get("consents_revoked") or 0),
    }


@patch("/orgs/{org_id:str}/members/{user_id:str}", status_code=200)
async def update_org_member(
    org_id: str,
    user_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Сменить роль и/или отдел user'а в org.
    Body: `{"role": "manager"}` и/или `{"department": "Маркетинг"}`
    (department: null/"" — сбросить). Отдел влияет на видимость: контент
    отдела другие отделы читают только с уровня руководителя."""
    _ensure_org_exists(org_id)
    caller_id = _require_org_admin_or_system(authorization, org_id)
    new_role = (data.get("role") or "").strip().lower()
    has_department = "department" in (data or {})
    if not new_role and not has_department:
        raise HTTPException(status_code=400, detail="role or department required")

    # Только отдел (без смены роли) — отдельная короткая ветка.
    if not new_role:
        ok = membership.set_member_department(user_id, org_id, data.get("department"))
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"user {user_id} is not a member of org {org_id}")
        await audit_log.emit(
            action="orgs.members.update_department",
            user_id=caller_id,
            resource=f"org:{org_id}",
            metadata={"user_id": user_id, "department": data.get("department")},
        )
        return {"user_id": user_id, "org_id": org_id,
                "department": (data.get("department") or "").strip() or None}

    # Защита: нельзя демоутить последнего founder/admin.
    current_role = membership.get_user_org_role(user_id)
    if current_role in membership.MANAGEMENT_ROLES \
       and new_role not in membership.MANAGEMENT_ROLES:
        all_members = membership.list_members(org_id)
        other_admins = [
            m for m in all_members
            if m["user_id"] != user_id
            and m.get("role") in membership.MANAGEMENT_ROLES
        ]
        if not other_admins:
            raise HTTPException(
                status_code=409,
                detail="cannot demote the last founder/admin; promote another member first",
            )

    try:
        updated = membership.update_member_role(user_id, org_id, new_role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"user {user_id} is not a member of org {org_id}",
        )
    if has_department:
        membership.set_member_department(user_id, org_id, data.get("department"))

    await audit_log.emit(
        action="orgs.members.update_role",
        user_id=caller_id,
        resource=f"org:{org_id}",
        metadata={"user_id": user_id, "new_role": new_role, "old_role": current_role,
                  **({"department": data.get("department")} if has_department else {})},
    )
    return {"user_id": user_id, "org_id": org_id, "role": new_role,
            **({"department": (data.get("department") or "").strip() or None}
               if has_department else {})}


# =============================================================================
# Invite-based onboarding (Wave 3.1)
# =============================================================================


@get("/my-org", status_code=200)
async def my_org(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Моя организация: org, моя роль/отдел и список коллег (для пикера
    «Поделиться» и т.п.). Solo-пользователь → {org: null}."""
    caller_id, _role = _extract_caller(authorization)
    if not caller_id:
        raise HTTPException(status_code=401, detail="auth required")
    org_id = membership.get_org_for_user(caller_id)
    if not org_id:
        return {"org": None, "members": []}
    org = _ensure_org_exists(org_id)
    members = membership.list_members(org_id)
    return {
        "org": {"id": org_id, "name": org.get("name")},
        "me": {"user_id": caller_id,
               "role": membership.get_user_org_role(caller_id),
               "department": membership.get_user_org_department(caller_id)},
        "members": members,
    }


@get("/my-orgs", status_code=200)
async def my_orgs(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Все членства вызывающего (enterprise: директор портфеля состоит в
    нескольких организациях). Активная — primary=true, первой."""
    caller_id, _role = _extract_caller(authorization)
    if not caller_id:
        raise HTTPException(status_code=401, detail="auth required")
    out = []
    for m in membership.get_orgs_for_user(caller_id):
        meta = org_store.get_org(m["org_id"]) or {}
        out.append({**m, "name": meta.get("name") or m["org_id"],
                    "parent_org_id": meta.get("parent_org_id")})
    return {"orgs": out}


@post("/my-org/switch", status_code=200)
async def switch_my_org(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Переключить АКТИВНУЮ организацию. Body: {"org_id"}. Членство в ней
    должно уже существовать (secondary). Роль/отдел/сшивка орг-скоупные —
    переезжают вместе с членством."""
    caller_id, _role = _extract_caller(authorization)
    if not caller_id:
        raise HTTPException(status_code=401, detail="auth required")
    org_id = (data.get("org_id") or "").strip()
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id required")
    if not membership.switch_primary_org(caller_id, org_id):
        raise HTTPException(
            status_code=404,
            detail="у вас нет членства в этой организации")
    await audit_log.emit(action="orgs.switch", user_id=caller_id,
                         resource=f"org:{org_id}")
    return {"ok": True, "active_org_id": org_id,
            "role": membership.get_user_org_role(caller_id)}


@get("/orgs/{org_id:str}/tree", status_code=200)
async def org_tree_route(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Дерево организаций от org_id вниз (холдинг → корпорации → филиалы).
    Доступ: член org (любое членство) или управляющий предком."""
    _ensure_org_exists(org_id)
    caller_id, sys_role = _extract_caller(authorization)
    allowed = (sys_role >= Role.ADMIN
               or membership.is_org_member_any(caller_id, org_id)
               or membership.can_manage_org(caller_id, org_id))
    if not allowed:
        raise HTTPException(status_code=403, detail="not a member of this org tree")
    tree = org_store.org_tree(org_id)
    return {"tree": tree, "ancestors": org_store.get_ancestors(org_id)}


@get("/orgs/{org_id:str}/roles", status_code=200)
async def get_org_roles(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Ролевая модель организации: встроенные роли + кастомные (enterprise)."""
    _ensure_org_exists(org_id)
    _require_org_member_or_system(authorization, org_id)
    from backend.core.access.levels import ROLE_MAX_ACCESS_LEVEL
    builtin = [{"role": r, "max_access_level": int(lvl), "builtin": True}
               for r, lvl in ROLE_MAX_ACCESS_LEVEL.items()]
    custom = [{"role": name, **spec, "builtin": False}
              for name, spec in org_store.get_custom_roles(org_id).items()]
    return {"org_id": org_id, "builtin": builtin, "custom": custom}


@put("/orgs/{org_id:str}/roles", status_code=200)
async def put_org_roles(
    org_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Заменить реестр кастомных ролей организации (org-admin).

    Body: {"roles": {"cpo": {"base": "manager", "title": "CPO"},
                      "аудитор": {"base": "employee", "max_access_level": 1}}}
    base ∈ admin/manager/employee/contractor (founder делегировать нельзя);
    max_access_level может только сузить клиренс base. Проверки прав идут
    по base — кастомная роль не расширяет права никогда."""
    _ensure_org_exists(org_id)
    caller_id = _require_org_admin_or_system(authorization, org_id)
    roles = data.get("roles")
    if not isinstance(roles, dict):
        raise HTTPException(status_code=400, detail="roles (object) required")
    try:
        ok = org_store.set_custom_roles(org_id, roles)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="org not found")
    await audit_log.emit(action="orgs.roles.update", user_id=caller_id,
                         resource=f"org:{org_id}",
                         metadata={"roles": sorted(roles.keys())})
    return {"ok": True, "custom": org_store.get_custom_roles(org_id)}


@get("/orgs/{org_id:str}/brain-stats", status_code=200)
async def org_brain_stats_route(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """«Мозг компании целиком»: сколько узлов в орг-мозге и что видно ИМЕННО
    вам (уровни + отделы). Генеральный (founder) видит всё; сотрудник —
    честное «всего N, вам видно M». Данные — из орг-графа (промоушен встреч
    и документов участников)."""
    _ensure_org_exists(org_id)
    caller_id = _require_org_member_or_system(authorization, org_id)

    import json as _json

    from backend.core.access.levels import (
        department_for_user,
        max_access_level_for_user,
    )
    from backend.core.access.org_stats import org_brain_stats
    from backend.core.store.tenant_paths import org_graph_path

    nodes: list[dict[str, Any]] = []
    try:
        with open(org_graph_path(org_id), "r", encoding="utf-8") as f:
            data = _json.load(f)
        raw = data.get("nodes", [])
        # формат файла: [{id, ...attrs}] — attrs уже плоские
        nodes = [n for n in raw if isinstance(n, dict)]
    except FileNotFoundError:
        pass  # орг-мозг ещё пуст — честные нули
    except Exception as e:
        logger.warning("brain-stats: org graph read failed: %s", e)

    stats = org_brain_stats(
        nodes, user_id=caller_id,
        max_level=max_access_level_for_user(caller_id),
        viewer_department=department_for_user(caller_id))
    return {"org_id": org_id, "viewer": {
        "user_id": caller_id,
        "role": membership.get_user_org_role(caller_id),
        "department": membership.get_user_org_department(caller_id),
    }, **stats}


@post("/orgs/{org_id:str}/invites", status_code=201)
async def create_org_invite(
    org_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Создать invite. Body: `{"role"?, "access_level"?, "invited_email"?, "expires_in_days"?}`.

    Ответ содержит `token` в plain виде — это ЕДИНСТВЕННЫЙ раз, когда
    он будет показан. Founder/admin шарит ссылку с этим токеном по
    защищённому каналу (email/Slack DM). В storage хранится только
    SHA256-хэш.
    """
    _ensure_org_exists(org_id)
    caller_id = _require_org_admin_or_system(authorization, org_id)

    role = data.get("role") or membership.ROLE_EMPLOYEE
    # Раннее validate, чтобы не создавать invite с unknown role.
    if role not in membership.ALL_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown role '{role}', allowed: {sorted(membership.ALL_ROLES)}",
        )

    access_level = data.get("access_level", membership.DEFAULT_ACCESS_LEVEL)
    invited_email = data.get("invited_email")
    expires_in = None
    days = data.get("expires_in_days")
    if days is not None:
        try:
            from datetime import timedelta
            expires_in = timedelta(days=int(days))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="expires_in_days must be int")

    invite = invite_store.create_invite(
        org_id, invited_by=caller_id,
        role=role, access_level=access_level,
        invited_email=invited_email, expires_in=expires_in,
    )
    await audit_log.emit(
        action="orgs.invites.create",
        user_id=caller_id,
        resource=f"org:{org_id}",
        metadata={
            "invite_id": invite["invite_id"],
            "role": invite["role"],
            "invited_email": invite.get("invited_email"),
        },
    )
    return invite


@get("/orgs/{org_id:str}/invites", status_code=200)
async def list_org_invites(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    include_used: bool = Parameter(query="include_used", default=False),
) -> dict[str, Any]:
    """Список pending invites (по умолчанию) или всех (include_used=true)."""
    _ensure_org_exists(org_id)
    _require_org_admin_or_system(authorization, org_id)
    invites = invite_store.list_invites_for_org(org_id, include_used=include_used)
    return {"org_id": org_id, "invites": invites, "count": len(invites)}


@delete("/orgs/{org_id:str}/invites/{invite_id:str}", status_code=200)
async def revoke_org_invite(
    org_id: str,
    invite_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Revoke pending invite. Already-used/expired/revoked → 410."""
    _ensure_org_exists(org_id)
    caller_id = _require_org_admin_or_system(authorization, org_id)
    revoked = invite_store.revoke_invite(invite_id, org_id, revoked_by=caller_id)
    if not revoked:
        raise HTTPException(
            status_code=410,
            detail="invite not found or already revoked/used/expired",
        )
    await audit_log.emit(
        action="orgs.invites.revoke",
        user_id=caller_id,
        resource=f"org:{org_id}",
        metadata={"invite_id": invite_id},
    )
    return {"revoked": True, "invite_id": invite_id, "org_id": org_id}


@get("/invites/{token:str}", status_code=200)
async def get_invite_info(token: str) -> dict[str, Any]:
    """Landing-info по токену: какая org, какая роль, кто пригласил.

    Public (требуется только знать токен) — нужно для UI, чтобы показать
    «Acme Inc приглашает вас как employee. Войдите чтобы принять.»
    до того как user залогинится.

    Token приходит в URL — не в Authorization header.
    """
    invite = invite_store.get_invite_by_token(token)
    if invite is None:
        raise HTTPException(status_code=404, detail="invite not found")

    # Подмешиваем org name для UX. Если org нет (была удалена) — пусть
    # инвайт показывается как expired, чтобы не leak'нуть имя orphaned.
    org_meta = org_store.get_org(invite["org_id"])
    if org_meta is None:
        invite["status"] = "expired"
        invite["org_name"] = None
    else:
        invite["org_name"] = org_meta.get("name")
    return invite


@post("/invites/{token:str}/accept", status_code=200)
async def accept_invite(
    token: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Принять invite: добавляем caller'а как member соответствующей org.

    Требует authenticated user. Если invite был создан с email — caller
    должен войти под этим email (email-pinning защищает от перехвата
    ссылки).

    Errors:
        404 — токен не найден или org удалена
        410 — invite expired / used / revoked
        403 — email-mismatch
        409 — user уже в другой org (cross-org block)
    """
    caller_id, _ = _extract_caller(authorization)

    # Email из JWT для email-pinning (если invite был с email).
    caller_email: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        try:
            from backend.core.auth.service_token import decode_claims_guarded
            claims = decode_claims_guarded(authorization.split(" ", 1)[1])
            em = claims.get("email")
            if isinstance(em, str):
                caller_email = em.strip().lower()
        except Exception:
            pass

    try:
        accept = invite_store.consume_invite(
            token, consuming_user_id=caller_id, consuming_user_email=caller_email,
        )
    except ValueError as ve:
        reason = str(ve)
        if reason == "token_not_found":
            raise HTTPException(status_code=404, detail="invite not found")
        if reason in ("expired", "used", "revoked"):
            raise HTTPException(status_code=410, detail=f"invite {reason}")
        if reason == "email_mismatch":
            raise HTTPException(
                status_code=403,
                detail="this invite was issued to a different email address",
            )
        raise HTTPException(status_code=400, detail=reason)

    # Проверим, что org всё ещё существует (могла быть удалена между
    # созданием invite и его accept). Если нет — accept уже помечен
    # used (rollback здесь сложен), но membership не создаём.
    if org_store.get_org(accept["org_id"]) is None:
        raise HTTPException(status_code=404, detail="org no longer exists")

    try:
        member = membership.add_member(
            caller_id, accept["org_id"],
            role=accept["role"], access_level=accept["access_level"],
        )
    except ValueError as e:
        # Чаще всего: user уже в другой org (cross-org block из Wave 2.2).
        raise HTTPException(status_code=409, detail=str(e))

    await audit_log.emit(
        action="orgs.invites.accept",
        user_id=caller_id,
        resource=f"org:{accept['org_id']}",
        metadata={"invite_id": accept["invite_id"], "role": accept["role"]},
    )
    return {
        "accepted": True,
        "invite_id": accept["invite_id"],
        "member": member,
    }


@get("/orgs/{org_id:str}/frozen-twins")
async def list_frozen_twins(
    org_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Слепки, замороженные при уходе людей. Видит администратор."""
    _ensure_org_exists(org_id)
    caller_id, sys_role = _extract_caller(authorization)
    if not (sys_role >= Role.ADMIN or membership.can_manage_org(caller_id, org_id)):
        raise HTTPException(status_code=403, detail="org admin required")
    from backend.core.ingest.offboarding import list_frozen
    return {"status": "success", "org_id": org_id, "frozen": list_frozen(org_id)}


@post("/orgs/{org_id:str}/frozen-twins/{person_id:str}/unfreeze",
      status_code=200)
async def unfreeze_twin_route(
    org_id: str,
    person_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Разморозить слепок ушедшего.

    Отдельное осознанное действие администратора: сценарий «человек ушёл,
    а знания остались» законен, но у него есть юридическая сторона
    (согласие, коммерческая тайна), и он не должен включаться сам собой.
    Действие журналируется."""
    _ensure_org_exists(org_id)
    caller_id, sys_role = _extract_caller(authorization)
    if not (sys_role >= Role.ADMIN or membership.can_manage_org(caller_id, org_id)):
        raise HTTPException(status_code=403, detail="org admin required")

    from backend.core.ingest.offboarding import unfreeze_twin
    if not unfreeze_twin(org_id, person_id):
        raise HTTPException(status_code=404, detail="слепок не заморожен")

    await audit_log.emit(
        action="orgs.twin.unfrozen", user_id=caller_id,
        resource=f"org:{org_id}", metadata={"person_id": person_id})
    return {"status": "success", "person_id": person_id, "frozen": False}


router = Router(
    path="",
    route_handlers=[
        my_org,          # /my-org — моя орга + коллеги (пикер «Поделиться»)
        my_orgs,         # /my-orgs — все членства (мульти-орг, enterprise)
        switch_my_org,   # /my-org/switch — переключить активную организацию
        org_tree_route,  # /orgs/{id}/tree — дерево холдинг→корпорация→филиал
        get_org_roles,   # /orgs/{id}/roles — ролевая модель (встроенные+кастомные)
        put_org_roles,   # PUT /orgs/{id}/roles — кастомные роли (админ)
        org_brain_stats_route,  # /orgs/{id}/brain-stats — «мозг целиком»
        create_org,
        get_org_info,
        list_org_members,
        add_org_member,
        remove_org_member,
        update_org_member,
        # Уход сотрудника: замороженные слепки
        list_frozen_twins,
        unfreeze_twin_route,
        # Wave 3.1 — invites
        create_org_invite,
        list_org_invites,
        revoke_org_invite,
        get_invite_info,
        accept_invite,
    ],
    tags=["Organizations"],
)
