# -*- coding: utf-8 -*-
"""W42: per-tenant LLM provider profiles — admin routes.

Endpoints:
    GET    /api/v1/admin/llm-profiles              — список (без api_key)
    POST   /api/v1/admin/llm-profiles              — создать
    GET    /api/v1/admin/llm-profiles/{id}         — детали
    PATCH  /api/v1/admin/llm-profiles/{id}         — обновить (partial)
    DELETE /api/v1/admin/llm-profiles/{id}         — удалить
    POST   /api/v1/admin/llm-profiles/{id}/set-default — switch active
    POST   /api/v1/admin/llm-profiles/{id}/test    — health-check probe
    GET    /api/v1/admin/llm-profiles/active       — текущий default

Доступ: Role.ADMIN. API key возвращается только при create response (один раз),
после этого только `has_api_key: bool` без plaintext.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.llm.profiles import (
    SUPPORTED_PROVIDERS,
    LLMProfileService,
    ProfileConflict,
    ProfileNotFound,
    ProfileValidationError,
    probe_profile,
)
from backend.core.observability import audit_log

logger = logging.getLogger(__name__)


def _require_admin(authorization: Optional[str]) -> str:
    """Требует роль ADMIN. Раньше роль бралась из `decode_claims_guarded`,
    который в compat-режиме (без JWT-секрета или при enable_strict_chat_auth=off)
    возвращает claims БЕЗ проверки подписи — то есть подделанный `role: admin`
    проходил. Теперь роль признаётся только из подтверждённой подписи
    (backend.core.auth.admin_guard).
    """
    from backend.core.auth.admin_guard import require_admin
    return require_admin(authorization)[0]


async def _build_service() -> LLMProfileService:
    postgres = None
    try:
        from backend.db.postgres import get_postgres
        postgres = await get_postgres()
    except Exception:
        postgres = None
    return LLMProfileService(postgres=postgres)


@get("/llm-profiles")
async def list_profiles(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    _require_admin(authorization)
    svc = await _build_service()
    profiles = await svc.list(limit=limit, offset=offset)
    return {
        "items": [p.to_dict() for p in profiles],
        "supported_providers": sorted(SUPPORTED_PROVIDERS),
        "limit": limit,
        "offset": offset,
    }


@get("/llm-profiles/active")
async def get_active_profile(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    svc = await _build_service()
    profile = await svc.get_default()
    if profile is None:
        return {"active": None}
    return {"active": profile.to_dict()}


@get("/llm-profiles/{profile_id:str}")
async def get_profile(
    profile_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    _require_admin(authorization)
    svc = await _build_service()
    profile = await svc.get(profile_id=profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile.to_dict()


@post("/llm-profiles")
async def create_profile(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Body:
        {
          "name": "RunPod Qwen3 30B",
          "provider": "runpod",
          "model": "Qwen/Qwen3-30B-A3B",
          "base_url": "https://abc-8000.proxy.runpod.net/v1",
          "api_key": "rp_xxx",
          "is_default": false,
          "is_fallback": false,
          "config": {"temperature": 0.5, "max_tokens": 2000}
        }
    """
    user_id = _require_admin(authorization)
    svc = await _build_service()
    try:
        profile = await svc.create(
            name=str(data.get("name") or "").strip(),
            provider=str(data.get("provider") or "").strip(),
            model=str(data.get("model") or "").strip(),
            base_url=(data.get("base_url") or None),
            api_key=(data.get("api_key") or None),
            is_default=bool(data.get("is_default", False)),
            is_fallback=bool(data.get("is_fallback", False)),
            config=data.get("config") or {},
            created_by=user_id,
        )
    except ProfileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ProfileConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.warning("create_profile failed: %s", exc)
        raise HTTPException(status_code=500, detail="failed to create profile")

    await audit_log.emit(
        action="llm.profile.create",
        user_id=user_id,
        resource=f"llm_profile:{profile.id}",
        metadata={
            "name": profile.name, "provider": profile.provider,
            "model": profile.model, "is_default": profile.is_default,
        },
    )
    return profile.to_dict()


@patch("/llm-profiles/{profile_id:str}", status_code=200)
async def update_profile(
    profile_id: str,
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Partial-update. Передаём ТОЛЬКО меняемые поля — отсутствующие не трогаем.

    api_key: непустая строка → заменить ключ; поле отсутствует → оставить как
    есть (маскированное поле UI не шлёт ключ при простом редактировании — так
    не затираем секрет случайно). Смена is_default=true атомарно снимает флаг
    с остальных.
    """
    user_id = _require_admin(authorization)
    svc = await _build_service()

    kwargs: dict[str, Any] = {}
    if "name" in data:
        kwargs["name"] = data.get("name")
    if "provider" in data:
        kwargs["provider"] = data.get("provider")
    if "model" in data:
        kwargs["model"] = data.get("model")
    if "base_url" in data:
        kwargs["base_url"] = data.get("base_url") or None
    if "is_default" in data:
        kwargs["is_default"] = bool(data.get("is_default"))
    if "is_fallback" in data:
        kwargs["is_fallback"] = bool(data.get("is_fallback"))
    if "config" in data:
        kwargs["config"] = data.get("config") or {}
    # api_key: обновляем только если передан непустой (иначе не затираем).
    if data.get("api_key"):
        kwargs["api_key"] = data.get("api_key")

    if not kwargs:
        raise HTTPException(status_code=400, detail="no updatable fields provided")

    try:
        profile = await svc.update(profile_id=profile_id, **kwargs)
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="profile not found")
    except ProfileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ProfileConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.warning("update_profile failed: %s", exc)
        raise HTTPException(status_code=500, detail="failed to update profile")

    # W42b: любое изменение активного профиля (default/provider/model/config/key)
    # может протухнуть кэш резолвера — сбрасываем безусловно, дёшево.
    try:
        from backend.core.llm.active_profile import invalidate_active_profile
        await invalidate_active_profile()
    except Exception as exc:
        logger.debug("invalidate_active_profile failed: %s", exc)

    await audit_log.emit(
        action="llm.profile.update",
        user_id=user_id,
        resource=f"llm_profile:{profile.id}",
        metadata={
            "fields": sorted(k for k in kwargs if k != "api_key"),
            "api_key_changed": "api_key" in kwargs,
            "is_default": profile.is_default,
        },
    )
    return profile.to_dict()


@post("/llm-profiles/{profile_id:str}/set-default", status_code=200)
async def set_default(
    profile_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _require_admin(authorization)
    svc = await _build_service()
    try:
        ok = await svc.set_default(profile_id=profile_id)
    except ProfileNotFound:
        raise HTTPException(status_code=404, detail="profile not found")
    if not ok:
        raise HTTPException(status_code=500, detail="failed to set default")

    # W42b: invalidate cached active profile в LLMRouter resolver.
    try:
        from backend.core.llm.active_profile import invalidate_active_profile
        await invalidate_active_profile()
    except Exception as exc:
        logger.debug("invalidate_active_profile failed: %s", exc)

    await audit_log.emit(
        action="llm.profile.set_default",
        user_id=user_id,
        resource=f"llm_profile:{profile_id}",
    )
    return {"ok": True, "profile_id": profile_id}


@delete("/llm-profiles/{profile_id:str}", status_code=200)
async def delete_profile(
    profile_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _require_admin(authorization)
    svc = await _build_service()
    profile = await svc.get(profile_id=profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    if profile.is_default:
        raise HTTPException(
            status_code=409,
            detail="cannot delete default profile — first set another as default",
        )
    ok = await svc.delete(profile_id=profile_id)
    # W42b: drop cached active profile если удалили его (но default удалять
    # запрещено — это просто defensive).
    try:
        from backend.core.llm.active_profile import invalidate_active_profile
        await invalidate_active_profile()
    except Exception as exc:
        logger.debug("invalidate_active_profile failed: %s", exc)
    await audit_log.emit(
        action="llm.profile.delete",
        user_id=user_id,
        resource=f"llm_profile:{profile_id}",
    )
    return {"ok": ok, "profile_id": profile_id}


@post("/llm-profiles/{profile_id:str}/test", status_code=200)
async def test_profile(
    profile_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Health-check: GET {base_url}/models — без отправки prompt'а.

    Не блокирует merge / set-default — это диагностика для UI.
    """
    user_id = _require_admin(authorization)
    svc = await _build_service()
    profile = await svc.get(profile_id=profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    api_key = await svc.get_decrypted_api_key(profile_id=profile_id)

    ok, error = await probe_profile(profile=profile, api_key=api_key)
    await svc.record_health_check(profile_id=profile_id, ok=ok)
    await audit_log.emit(
        action="llm.profile.test",
        user_id=user_id,
        resource=f"llm_profile:{profile_id}",
        metadata={"ok": ok, "error": error},
    )
    return {"ok": ok, "profile_id": profile_id, "error": error}


_LLM_PROFILE_HANDLERS = [
    list_profiles,
    get_active_profile,
    get_profile,
    create_profile,
    update_profile,
    set_default,
    delete_profile,
    test_profile,
]

router = Router(
    path="/admin",
    route_handlers=_LLM_PROFILE_HANDLERS,
    tags=["LLM Profiles"],
)

# issue #107 T-3: алиас без /admin для внешних клиентов (AI-Coach зовёт
# /api/v1/llm-profiles и получал 404). Фронт (LLMProfilesPanel.tsx) ходит на
# /admin/llm-profiles через `router` выше — НЕ трогаем. Те же хендлеры
# регистрируются и под /api/v1/llm-profiles* (Litestar допускает
# переиспользование хендлеров в роутерах с разными префиксами).
alias_router = Router(
    path="/",
    route_handlers=_LLM_PROFILE_HANDLERS,
    tags=["LLM Profiles"],
)
