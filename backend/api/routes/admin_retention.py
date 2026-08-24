# -*- coding: utf-8 -*-
"""Admin retention controls (W27).

Endpoint:
- ``POST /api/v1/admin/retention/run`` — ручной запуск retention cycle.

Доступно только Role.ADMIN. Используется для backfill / one-off cleanup,
без ожидания nightly_consolidation.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.auth.rbac import Role
from backend.core.observability import audit_log
from backend.core.retention.cleanup import run_retention_cycle

logger = logging.getLogger(__name__)


def _require_admin(authorization: Optional[str]) -> tuple[str, Role]:
    """Требует роль ADMIN. Раньше роль бралась из `decode_claims_guarded`,
    который в compat-режиме (без JWT-секрета или при enable_strict_chat_auth=off)
    возвращает claims БЕЗ проверки подписи — то есть подделанный `role: admin`
    проходил. Теперь роль признаётся только из подтверждённой подписи
    (backend.core.auth.admin_guard).
    """
    from backend.core.auth.admin_guard import require_admin
    return require_admin(authorization)


async def _build_postgres() -> Any:
    try:
        from backend.db.postgres import get_postgres
        return await get_postgres()
    except Exception:
        return None


async def _build_redis() -> Any:
    try:
        from backend.core.executors.store import _get_redis
        return await _get_redis()
    except Exception:
        return None


@post("/retention/run")
async def trigger_retention(
    data: Optional[dict[str, Any]] = None,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Запустить retention cleanup сейчас.

    Body (опционально):
        {
            "validation_retention_days": 90,    // override
            "executor_retention_days": 30,
            "dry_run": false
        }

    Возвращает CleanupResult.to_dict().
    """
    user_id, _role = _require_admin(authorization)
    payload = data or {}
    dry_run = bool(payload.get("dry_run", False))

    from backend.config import get_settings
    s = get_settings()
    if not getattr(s, "retention_enabled", False) and not payload.get("force"):
        raise HTTPException(
            status_code=409,
            detail="Retention disabled in settings. Pass force=true to override.",
        )

    val_days = int(payload.get(
        "validation_retention_days",
        getattr(s, "validation_retention_days", 90),
    ))
    exec_days = int(payload.get(
        "executor_retention_days",
        getattr(s, "executor_retention_days", 30),
    ))

    postgres = None if dry_run else await _build_postgres()
    redis = None if dry_run else await _build_redis()

    result = await run_retention_cycle(
        postgres=postgres,
        redis=redis,
        validation_retention_days=val_days,
        executor_retention_days=exec_days,
        enabled=True,
    )
    out = result.to_dict()
    out["dry_run"] = dry_run
    out["validation_retention_days"] = val_days
    out["executor_retention_days"] = exec_days

    await audit_log.emit(
        action="admin.retention.run",
        user_id=user_id,
        resource="retention",
        metadata={
            "validation_deleted": out["validation_deleted"],
            "executor_handles_purged": out["executor_handles_purged"],
            "errors": out.get("errors", []),
            "dry_run": dry_run,
        },
    )
    return out


router = Router(
    path="/admin",
    route_handlers=[trigger_retention],
    tags=["Admin"],
)
