# -*- coding: utf-8 -*-
"""Reader endpoint для audit_events (P2 #13).

Существующая инфраструктура (W9 audit_log.py + migration 040):
  - audit_events table уже tenant-scoped (tenant_id колонка + RLS)
  - audit_log.emit() пишет в неё с tenant_id из contextvars

Чего не хватало — публичного READER. Этот route даёт:
  GET /admin/audit-log     — события текущего tenant'а с фильтрами

Auth: ADMIN+ обязателен. Super-admin (OWNER) может через ?tenant_id=X
читать чужой tenant (для cross-org compliance audit).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.auth.rbac import Role

logger = logging.getLogger(__name__)


def _require_admin(authorization: Optional[str]) -> tuple[str, Role]:
    """Возвращает (user_id, role); raise если role < ADMIN.

    ДЫРА, которая тут была: `jwt.decode(token, verify_signature=False)` — и
    роль читалась из НЕПРОВЕРЕННОГО токена. Собранный вручную
    `{"sub": "x", "role": "owner"}` давал OWNER, а OWNER открывает
    `?tenant_id=` — то есть журнал аудита любого чужого тенанта. Теперь
    роль берётся только из подписи (backend.core.auth.admin_guard).
    """
    from backend.core.auth.admin_guard import require_admin
    return require_admin(authorization)


def _resolve_tenant(
    requested_tenant: Optional[str],
    role: Role,
) -> Optional[str]:
    """Определить какой tenant читаем.

    - Если caller указал ?tenant_id=X И его роль ≥ OWNER → возвращаем X
    - Если caller указал ?tenant_id=X но role < OWNER → 403
    - Иначе берём текущий tenant из contextvars (то что middleware
      положил из JWT claim'а)
    """
    if requested_tenant:
        if role < Role.OWNER:
            raise HTTPException(
                status_code=403,
                detail="reading audit-log of another tenant requires Role.OWNER",
            )
        return requested_tenant.strip()

    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


@get("/audit-log", status_code=200)
async def list_audit_events(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    tenant_id: Optional[str] = Parameter(query="tenant_id", default=None),
    action: Optional[str] = Parameter(query="action", default=None),
    user_id: Optional[str] = Parameter(query="user_id", default=None),
    since: Optional[str] = Parameter(query="since", default=None),
    until: Optional[str] = Parameter(query="until", default=None),
    limit: int = Parameter(query="limit", default=100),
    offset: int = Parameter(query="offset", default=0),
) -> dict[str, Any]:
    """Список audit-events текущего tenant'а.

    Query params:
        tenant_id (optional): требует Role.OWNER, читает чужой tenant
        action (optional): фильтр по `action` (поддерживается LIKE через `*`,
                           т.е. `?action=gdpr.*` → gdpr.export, gdpr.delete, ...)
        user_id (optional): фильтр по субъекту действия
        since/until (optional): ISO 8601 timestamps
        limit (default 100, max 1000)
        offset (default 0)

    Response:
        {
            "tenant_id": "...",
            "count": int,
            "limit": int, "offset": int,
            "events": [
                {id, created_at, action, user_id, actor_role, resource,
                 ip, user_agent, request_id, payload_hash, metadata},
                ...
            ]
        }
    """
    _, role = _require_admin(authorization)
    target_tenant = _resolve_tenant(tenant_id, role)
    if not target_tenant:
        raise HTTPException(
            status_code=400,
            detail="no tenant in context; pass ?tenant_id=X or set X-Tenant-Id header",
        )

    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}")

    conditions = ["tenant_id = :tenant_id"]
    params: dict[str, Any] = {"tenant_id": target_tenant, "limit": limit, "offset": offset}

    if action:
        a = action.strip()
        if "*" in a:
            conditions.append("action LIKE :action_pat")
            params["action_pat"] = a.replace("*", "%")
        else:
            conditions.append("action = :action")
            params["action"] = a

    if user_id:
        conditions.append("user_id = :user_id")
        params["user_id"] = user_id

    if since:
        conditions.append("created_at >= :since")
        params["since"] = since
    if until:
        conditions.append("created_at < :until")
        params["until"] = until

    where = " AND ".join(conditions)
    query = text(f"""
        SELECT id, created_at, action, user_id, actor_role, resource,
               ip, user_agent, request_id, payload_hash, metadata
        FROM public.audit_events
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    events: list[dict[str, Any]] = []
    try:
        # apply_tenant=False — мы фильтруем явно по target_tenant,
        # не полагаемся на RLS context (он может быть из JWT caller'а,
        # а нам нужен запрошенный tenant_id).
        async with pg.session(apply_tenant=False) as session:
            result = await session.execute(query, params)
            rows = result.fetchall() if hasattr(result, "fetchall") else await result.fetchall()
            for r in rows:
                # SQLAlchemy Row: tuple-like + .keys()
                events.append({
                    "id": r[0],
                    "created_at": r[1].isoformat() if r[1] else None,
                    "action": r[2],
                    "user_id": r[3],
                    "actor_role": r[4],
                    "resource": r[5],
                    "ip": str(r[6]) if r[6] else None,
                    "user_agent": r[7],
                    "request_id": r[8],
                    "payload_hash": r[9],
                    "metadata": r[10] or {},
                })
    except Exception as exc:
        logger.warning("audit-log query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"query failed: {str(exc)[:200]}")

    return {
        "tenant_id": target_tenant,
        "count": len(events),
        "limit": limit,
        "offset": offset,
        "events": events,
    }


router = Router(
    path="/admin",
    route_handlers=[list_audit_events],
    tags=["Admin", "Audit"],
)
