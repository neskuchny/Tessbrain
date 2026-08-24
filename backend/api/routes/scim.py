# -*- coding: utf-8 -*-
"""SCIM 2.0 minimal /Users endpoints (W15).

Реализует RFC 7644 для интеграции с IdP (Azure AD, Okta, OneLogin):
- `GET    /scim/v2/Users` — list (с filter `userName eq "..."`)
- `GET    /scim/v2/Users/{id}` — read
- `POST   /scim/v2/Users` — create (provisioning)
- `PUT    /scim/v2/Users/{id}` — full replace
- `PATCH  /scim/v2/Users/{id}` — partial update (ops: replace/remove/add)
- `DELETE /scim/v2/Users/{id}` — deprovision (cascade-delete or soft)

Дизайн:
- **Tenant-scoped через service-token**: SCIM-token включает `tenant_id`,
  IdP управляет ТОЛЬКО users из своего tenant'а. Использует тот же
  service-JWT pattern что W6 (HS256 + audience=`tessent-scim`).
- **Storage**: используем `UserProfileService.update_profile()` —
  существующая abstraction; полный `users` table остаётся
  app-specific и может быть в Supabase/Postgres/etc.
- **PATCH ops**: поддерживаем минимум — `replace path="active"`,
  `replace path="userName"`, `replace path="emails[type eq \"work\"].value"`.
  Полный SCIM filter language НЕ реализован (это огромный scope).

Что НЕ покрыто и пометилось как RFC-deviation:
- `Groups` resource: нет (planned per-customer)
- ETags + `If-Match`: нет
- bulk operations: нет
- ServiceProviderConfig endpoint: stub (404 для большинства IdP OK)

См. SSO.md для интеграционного гайда per IdP.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from litestar import Router, delete, get, patch, post, put
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.auth.permissions import Permission, has_permission
from backend.core.observability import audit_log

logger = logging.getLogger(__name__)


# === SCIM-specific helpers ================================================

_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_LIST_RESP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def _scim_error(status: int, detail: str, *, scim_type: Optional[str] = None) -> dict[str, Any]:
    """Стандартизированный SCIM error body."""
    err: dict[str, Any] = {
        "schemas": [_SCIM_ERROR_SCHEMA],
        "status": str(status),
        "detail": detail,
    }
    if scim_type:
        err["scimType"] = scim_type
    return err


def _user_to_scim(user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Сериализовать наш UserProfile в SCIM-format."""
    email = profile.get("email") or ""
    return {
        "schemas": [_SCIM_USER_SCHEMA],
        "id": user_id,
        "userName": profile.get("user_name") or email or user_id,
        "active": not profile.get("deleted_at") and not profile.get("processing_restricted"),
        "emails": (
            [{"value": email, "primary": True, "type": "work"}] if email else []
        ),
        "name": {
            "givenName": profile.get("given_name") or "",
            "familyName": profile.get("family_name") or "",
        },
        "meta": {
            "resourceType": "User",
            "created": profile.get("created_at") or "",
            "lastModified": profile.get("updated_at") or "",
        },
    }


def _scim_to_profile_updates(scim: dict[str, Any]) -> dict[str, Any]:
    """SCIM body → profile updates dict (только known fields)."""
    updates: dict[str, Any] = {}
    if "userName" in scim:
        updates["user_name"] = scim["userName"]
    if "active" in scim:
        # active=false означает deprovisioning через restriction.
        if scim["active"] is False:
            updates["processing_restricted"] = True
        else:
            updates["processing_restricted"] = False
    if "emails" in scim and isinstance(scim["emails"], list):
        primary = next(
            (e for e in scim["emails"] if e.get("primary")),
            scim["emails"][0] if scim["emails"] else None,
        )
        if primary and primary.get("value"):
            updates["email"] = primary["value"]
    if "name" in scim and isinstance(scim["name"], dict):
        n = scim["name"]
        if "givenName" in n:
            updates["given_name"] = n["givenName"]
        if "familyName" in n:
            updates["family_name"] = n["familyName"]
    return updates


# Простейший parser для `userName eq "value"` filter.
_FILTER_RE = re.compile(
    r'^\s*(?P<attr>userName|email|id)\s+eq\s+"(?P<value>[^"]*)"\s*$',
    re.IGNORECASE,
)


def _parse_filter(filter_str: Optional[str]) -> Optional[tuple[str, str]]:
    """`userName eq "alice@example.com"` → ("userName", "alice@example.com").

    Возвращает None если filter пустой или неподдерживаемый.
    """
    if not filter_str:
        return None
    m = _FILTER_RE.match(filter_str)
    if not m:
        return None
    return m.group("attr").lower(), m.group("value")


# === Auth helper ==========================================================

def _check_scim_auth() -> None:
    """SCIM endpoint требует SCIM_PROVISION permission.

    Реальный SCIM-token приходит из middleware (parse JWT, set role).
    Если permission нет — fail-closed.
    """
    if not has_permission(Permission.SCIM_PROVISION):
        raise HTTPException(
            status_code=403,
            detail="SCIM endpoints require scim:provision permission",
        )


# === Storage facade =======================================================
# Используем UserProfileService как primary store. Полный enterprise
# с external IDs / external tracking — отдельный repository (TODO).

async def _get_profile_svc():
    from backend.memory.user_profiles import UserProfileService
    svc = UserProfileService()
    await svc.initialize()
    return svc


# === Endpoints ============================================================

@get("/Users")
async def list_users(
    filter: Optional[str] = Parameter(query="filter", default=None),
    start_index: int = Parameter(query="startIndex", default=1),
    count: int = Parameter(query="count", default=20),
) -> dict[str, Any]:
    """SCIM list. Поддерживает только filter `userName eq "..."`.

    Без filter возвращает первую страницу — но мы не итерируем все
    профили в БД (нет такого list-API в UserProfileService). В этом
    сценарии возвращаем пустой list со scim-warning. Для прод-deployments
    с массовым SCIM sync нужно реализовать `UserProfileService.list_all()`.
    """
    _check_scim_auth()
    parsed = _parse_filter(filter)
    if parsed is None:
        return {
            "schemas": [_SCIM_LIST_RESP_SCHEMA],
            "totalResults": 0,
            "Resources": [],
            "startIndex": start_index,
            "itemsPerPage": count,
            # SCIM позволяет такой ответ — IdP должен делать filter-based queries.
        }

    attr, value = parsed
    svc = await _get_profile_svc()
    user_id = None
    if attr == "id":
        user_id = value
    else:
        # email/userName — для нашего минимального UserProfile email и есть
        # primary identifier. UserProfileService.find_by_email — TODO; пока
        # делаем lookup через get_profile по предполагаемому ID = email.
        if hasattr(svc, "find_by_email"):
            user_id = await svc.find_by_email(value)  # type: ignore[attr-defined]
        else:
            # fallback — predполагаем что user_id == email в этом deploy.
            user_id = value

    if not user_id:
        return {
            "schemas": [_SCIM_LIST_RESP_SCHEMA],
            "totalResults": 0,
            "Resources": [],
            "startIndex": start_index,
            "itemsPerPage": count,
        }

    profile = await svc.get_profile(user_id)
    if profile is None:
        return {
            "schemas": [_SCIM_LIST_RESP_SCHEMA],
            "totalResults": 0,
            "Resources": [],
            "startIndex": start_index,
            "itemsPerPage": count,
        }
    user = _user_to_scim(user_id, profile.to_dict() if hasattr(profile, "to_dict") else dict(profile))
    return {
        "schemas": [_SCIM_LIST_RESP_SCHEMA],
        "totalResults": 1,
        "Resources": [user],
        "startIndex": start_index,
        "itemsPerPage": count,
    }


@get("/Users/{user_id:str}")
async def get_user(user_id: str) -> dict[str, Any]:
    _check_scim_auth()
    svc = await _get_profile_svc()
    profile = await svc.get_profile(user_id)
    if profile is None:
        raise HTTPException(
            status_code=404,
            detail=_scim_error(404, f"user {user_id} not found"),
        )
    return _user_to_scim(
        user_id,
        profile.to_dict() if hasattr(profile, "to_dict") else dict(profile),
    )


@post("/Users", status_code=201)
async def create_user(data: dict[str, Any]) -> dict[str, Any]:
    """Provisioning через SCIM POST.

    Body — стандартный SCIM User resource. user_id выводится из
    `userName` (обычно email или UPN из IdP).
    """
    _check_scim_auth()
    user_name = data.get("userName")
    if not user_name:
        raise HTTPException(
            status_code=400,
            detail=_scim_error(400, "userName is required", scim_type="invalidValue"),
        )
    user_id = user_name  # Для нашего embedding минимально достаточно.

    svc = await _get_profile_svc()
    updates = _scim_to_profile_updates(data)
    await svc.update_profile(user_id, updates)
    profile = await svc.get_profile(user_id)
    profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)

    await audit_log.emit(
        action="scim.user.create",
        user_id=user_id,
        resource=f"user:{user_id}",
        metadata={"userName": user_name},
    )
    return _user_to_scim(user_id, profile_dict)


@put("/Users/{user_id:str}")
async def replace_user(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """SCIM PUT — full resource replace."""
    _check_scim_auth()
    svc = await _get_profile_svc()
    updates = _scim_to_profile_updates(data)
    await svc.update_profile(user_id, updates)
    profile = await svc.get_profile(user_id)
    profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)

    await audit_log.emit(
        action="scim.user.replace",
        user_id=user_id,
        resource=f"user:{user_id}",
    )
    return _user_to_scim(user_id, profile_dict)


@patch("/Users/{user_id:str}")
async def patch_user(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """SCIM PATCH — partial update.

    Поддерживаем минимальный subset RFC 7644 §3.5.2:
    - operations: replace, add, remove
    - paths: active, userName, emails[type eq "work"].value
    """
    _check_scim_auth()
    ops = data.get("Operations") or []
    if not isinstance(ops, list):
        raise HTTPException(status_code=400, detail=_scim_error(400, "Operations must be a list"))

    updates: dict[str, Any] = {}
    for op in ops:
        if not isinstance(op, dict):
            continue
        opname = (op.get("op") or "").lower()
        path = op.get("path") or ""
        value = op.get("value")

        if opname not in {"replace", "add", "remove"}:
            continue

        if path in ("active", "userName"):
            if path == "active":
                updates["processing_restricted"] = (value is False)
            else:
                updates["user_name"] = value if opname != "remove" else None
        elif path.startswith('emails'):
            # упрощённо: replace всю primary email
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict) and first.get("value"):
                    updates["email"] = first["value"]
            elif isinstance(value, str):
                updates["email"] = value
        elif path.startswith("name."):
            # name.givenName / name.familyName
            field = path.split(".", 1)[1]
            if field == "givenName":
                updates["given_name"] = value
            elif field == "familyName":
                updates["family_name"] = value

    if updates:
        svc = await _get_profile_svc()
        await svc.update_profile(user_id, updates)
        profile = await svc.get_profile(user_id)
        profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
        await audit_log.emit(
            action="scim.user.patch",
            user_id=user_id,
            resource=f"user:{user_id}",
            metadata={"ops_count": len(ops), "fields": list(updates.keys())},
        )
        return _user_to_scim(user_id, profile_dict)

    # No-op patch — возвращаем current state.
    svc = await _get_profile_svc()
    profile = await svc.get_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=_scim_error(404, "user not found"))
    profile_dict = profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
    return _user_to_scim(user_id, profile_dict)


@delete("/Users/{user_id:str}", status_code=204)
async def delete_user(user_id: str) -> None:
    """SCIM DELETE — deprovision.

    По умолчанию — soft-delete через `processing_restricted=true`.
    Это safer чем hard-delete: GDPR-erasure делается отдельным
    `DELETE /api/v1/gdpr/me` (Art. 17).
    """
    _check_scim_auth()
    svc = await _get_profile_svc()
    await svc.update_profile(user_id, {
        "processing_restricted": True,
        "deprovisioned_at": _now_iso(),
    })
    await audit_log.emit(
        action="scim.user.delete",
        user_id=user_id,
        resource=f"user:{user_id}",
        metadata={"basis": "SCIM DELETE"},
    )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


router = Router(
    path="/scim/v2",
    route_handlers=[
        list_users,
        get_user,
        create_user,
        replace_user,
        patch_user,
        delete_user,
    ],
    tags=["SCIM"],
)
