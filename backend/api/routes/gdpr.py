# -*- coding: utf-8 -*-
"""GDPR endpoints (W9 enterprise-pack).

Реализует **минимальный набор** прав субъекта данных по GDPR:
- Art. 15 — Right of access (`GET /api/v1/gdpr/me/export`),
- Art. 17 — Right to erasure (`DELETE /api/v1/gdpr/me`).

Дизайн:
- Tenant-scoped: запрос обязан нести валидный `Authorization: Bearer <jwt>`,
  tenant + user_id берутся из claims.
- Каждое действие пишет audit-event (`gdpr.export` / `gdpr.delete`).
- Cascade-delete делает best-effort по известным таблицам user-owned данных
  (`user_profiles_v2` гарантированно; остальные — через registry plugin).
- Export возвращает JSON-документ, не файл — клиент сам сохраняет.

ВАЖНО: для production клиента рекомендуется async-job через taskiq +
email-notification, иначе delete крупного юзера может занять минуты.
Здесь — sync MVP; см. TODO в коде.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.auth.rbac import Role, current_role
from backend.core.observability import audit_log

logger = logging.getLogger(__name__)


def _decode_jwt_unsafe(token: str) -> dict[str, Any]:
    """Извлечь claims (verify-first). Возвращает sub/email.

    Прежний комментарий «подпись уже проверена выше» был неверен —
    вышестоящий tenant_resolver тоже декодировал без проверки. Теперь
    единый guarded-путь (issue #107 T-1).
    """
    # issue #107 T-1: verify-first (strict → {} на плохой подписи → 401;
    # compat по умолчанию → decode без проверки, как раньше).
    from backend.core.auth.service_token import decode_claims_guarded
    return decode_claims_guarded(token)


def _extract_user(authorization: Optional[str]) -> tuple[str, Optional[str]]:
    """Вернуть (user_id, email) из Bearer-токена или 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        claims = _decode_jwt_unsafe(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id, claims.get("email")


async def _collect_user_profile(user_id: str) -> Optional[dict[str, Any]]:
    """Собрать профиль из user_profiles_v2 (best-effort)."""
    try:
        from backend.memory.user_profiles import UserProfileService
        svc = UserProfileService()
        await svc.initialize()
        profile = await svc.get_profile(user_id)
        if profile is None:
            return None
        return profile.to_dict() if hasattr(profile, "to_dict") else dict(profile)
    except Exception as exc:
        logger.debug("gdpr.export: profile fetch skipped (%s)", exc)
        return None


async def _collect_user_queries(user_id: str) -> list[dict[str, Any]]:
    try:
        from backend.memory.user_profiles import UserProfileService
        svc = UserProfileService()
        await svc.initialize()
        return await svc.get_recent_queries(user_id, limit=10_000) or []
    except Exception as exc:
        logger.debug("gdpr.export: queries fetch skipped (%s)", exc)
        return []


@get("/me/export")
async def export_my_data(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """GDPR Art. 15 — Right of access.

    Возвращает JSON-документ со всеми персональными данными пользователя
    в текущем tenant'е. Документ structured: `{user_id, email, profile,
    queries, exported_at, scope}`.
    """
    user_id, email = _extract_user(authorization)

    profile = await _collect_user_profile(user_id)
    queries = await _collect_user_queries(user_id)

    from datetime import datetime, timezone
    payload: dict[str, Any] = {
        "user_id": user_id,
        "email": email,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "scope": "tenant_scoped",
        "profile": profile,
        "queries": queries,
        # TODO(W10): добавлять данные из других сервисов через registry
        # plugin'ов (graph, snapshots, agent_memory). Сейчас — минимум,
        # достаточный для прохождения GDPR-аудита.
    }

    await audit_log.emit(
        action="gdpr.export",
        user_id=user_id,
        resource=f"user:{user_id}",
        payload=None,
        metadata={"queries_count": len(queries), "profile_present": profile is not None},
    )
    return payload


@delete("/me", status_code=200)
async def delete_my_data(
    dry_run: bool = Parameter(query="dry_run", default=False),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """GDPR Art. 17 — Right to erasure ('right to be forgotten').

    Каскадно удаляет user-owned данные в текущем tenant'е. Действие
    необратимо; перед запуском backend ДОЛЖЕН попросить подтверждение
    на UI-уровне (frontend ответственность).

    Защита: требует роль не ниже OWNER ИЛИ self-delete (sub == user_id).
    Здесь это всегда self-delete (мы достаём user_id из JWT того же
    запроса), но ADMIN-инициированный delete конкретного юзера должен
    быть отдельным endpoint'ом — здесь его нет намеренно.
    """
    user_id, email = _extract_user(authorization)

    # Безопасность: даже self-delete требует, чтобы middleware установила роль.
    # Если RBAC не сконфигурирован (Role.NONE) — отказ. Это страхует от
    # сценария «токен украли, без RBAC можно удалить весь профиль».
    if current_role() < Role.VIEWER:
        raise HTTPException(
            status_code=403,
            detail="GDPR delete requires authenticated session with role>=VIEWER",
        )

    deleted: dict[str, Any] = {"profile": False, "queries": 0}

    # 1. Локальный профиль (SQLite UserProfileService). При dry_run не трогаем.
    if not dry_run:
        try:
            from backend.memory.user_profiles import UserProfileService
            svc = UserProfileService()
            await svc.initialize()
            if hasattr(svc, "delete_profile"):
                await svc.delete_profile(user_id)
                deleted["profile"] = True
            else:
                # Fallback: затереть профиль пустым словарём.
                await svc.update_profile(user_id, {"deleted_at": "true"})
                deleted["profile"] = "soft"
        except Exception as exc:
            logger.warning("gdpr.delete: profile cleanup failed: %s", exc)

    # 2. P1 #9 — полный каскад: Postgres + filesystem + membership + invites
    # + org-graphs tombstone. Постгрес-erasure теперь wrapped внутри
    # erase_user_full (org_cascade orchestrator).
    cascade: dict[str, Any] = {}
    try:
        from backend.core.gdpr.org_cascade import erase_user_full
        cascade = await erase_user_full(user_id, dry_run=dry_run)
    except Exception as exc:
        logger.warning("gdpr.delete: full cascade failed: %s", exc)
        cascade = {"error": str(exc)}

    # Аудит пишем только при реальном удалении (dry_run — превью, не действие).
    if not dry_run:
        pg_cascade = cascade.get("postgres") or {}
        await audit_log.emit(
            action="gdpr.delete",
            user_id=user_id,
            resource=f"user:{user_id}",
            payload=None,
            metadata={
                "deleted": deleted,
                "cascade_rows": pg_cascade.get("total_rows"),
                "cascade_tables": list((pg_cascade.get("deleted") or {}).keys()),
                # P1 #9 — расширенный аудит для compliance-проверки.
                "filesystem_deleted": (cascade.get("filesystem") or {}).get("deleted", {}),
                "membership_removed": (cascade.get("membership") or {}).get("removed"),
                "invites_revoked": (cascade.get("invites") or {}).get("revoked"),
                "org_nodes_tombstoned": (cascade.get("org_graphs") or {}).get("nodes_tombstoned"),
                "email_was": email,
            },
        )

    from datetime import datetime, timezone
    return {
        "status": "preview" if dry_run else "deleted",
        "user_id": user_id,
        "dry_run": dry_run,
        "deleted": deleted,
        "cascade": cascade,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


@post("/me/restrict")
async def restrict_my_processing(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """GDPR Art. 18 — Right to restriction of processing.

    Помечает профиль флагом `processing_restricted=true`. Дальше
    application-слой обязан:
    - НЕ запускать ML/LLM-personalize для этого user_id,
    - НЕ включать его data в snapshot/synthesis процессы,
    - НЕ собирать новые сигналы (audit-log читать ОК — мы должны).

    Реальная инфорсация остаётся за хэндлерами features (через чтение
    флага из profile при каждом запросе). Этот endpoint — точка
    управления, не сам инфорсер.
    """
    user_id, _ = _extract_user(authorization)
    if current_role() < Role.VIEWER:
        raise HTTPException(status_code=403, detail="restriction requires authenticated session")

    try:
        from backend.memory.user_profiles import UserProfileService
        svc = UserProfileService()
        await svc.initialize()
        await svc.update_profile(user_id, {
            "processing_restricted": True,
            "processing_restricted_at": _now_iso(),
        })
    except Exception as exc:
        logger.error("gdpr.restrict: profile update failed: %s", exc)
        raise HTTPException(status_code=500, detail="restriction failed to persist")

    await audit_log.emit(
        action="gdpr.restrict",
        user_id=user_id,
        resource=f"user:{user_id}",
        metadata={"basis": "Art. 18 GDPR"},
    )
    return {
        "status": "restricted",
        "user_id": user_id,
        "applied_at": _now_iso(),
    }


@post("/me/unrestrict")
async def unrestrict_my_processing(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Снять restriction — возобновить processing. Только сам субъект."""
    user_id, _ = _extract_user(authorization)
    if current_role() < Role.VIEWER:
        raise HTTPException(status_code=403, detail="unrestriction requires authenticated session")

    try:
        from backend.memory.user_profiles import UserProfileService
        svc = UserProfileService()
        await svc.initialize()
        await svc.update_profile(user_id, {"processing_restricted": False})
    except Exception as exc:
        logger.error("gdpr.unrestrict: profile update failed: %s", exc)
        raise HTTPException(status_code=500, detail="unrestriction failed to persist")

    await audit_log.emit(
        action="gdpr.unrestrict",
        user_id=user_id,
        resource=f"user:{user_id}",
    )
    return {"status": "active", "user_id": user_id}


@post("/me/objection")
async def object_to_processing(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """GDPR Art. 21 — Right to object.

    Юзер возражает против processing для специфических целей
    (personalization / profiling). Опций:
    - opted_out_personalize: отключает adaptive responses (W7)
    - opted_out_profiling: отключает curator (W7) и chat_pref_extractor
    - opted_out_telemetry: отключает usage_tracker / metrics, кроме
      обязательного audit_log (его удержание — legal requirement).
    """
    user_id, _ = _extract_user(authorization)
    if current_role() < Role.VIEWER:
        raise HTTPException(status_code=403, detail="objection requires authenticated session")

    objections = {
        "opted_out_personalize": True,
        "opted_out_profiling": True,
        "opted_out_telemetry": True,
        "objected_at": _now_iso(),
    }
    try:
        from backend.memory.user_profiles import UserProfileService
        svc = UserProfileService()
        await svc.initialize()
        await svc.update_profile(user_id, objections)
    except Exception as exc:
        logger.error("gdpr.objection: profile update failed: %s", exc)
        raise HTTPException(status_code=500, detail="objection failed to persist")

    await audit_log.emit(
        action="gdpr.objection",
        user_id=user_id,
        resource=f"user:{user_id}",
        metadata={"objections": list(objections.keys()), "basis": "Art. 21 GDPR"},
    )
    return {
        "status": "objection_recorded",
        "user_id": user_id,
        "applied": objections,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


router = Router(
    path="/gdpr",
    route_handlers=[
        export_my_data,
        delete_my_data,
        restrict_my_processing,
        unrestrict_my_processing,
        object_to_processing,
    ],
    tags=["GDPR"],
)
