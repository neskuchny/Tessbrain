# -*- coding: utf-8 -*-
"""REST для сшивки идентичности «это я» (CogniLayer Ф0).

- GET  /identity/me          — текущая подтверждённая сшивка аккаунта
- GET  /identity/candidates  — кандидаты Person-сущностей для сшивки
- POST /identity/link        — подтвердить сшивку {person_entity_id}
- POST /identity/unlink      — расшить
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token

logger = logging.getLogger(__name__)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    try:
        user_id = get_user_id_from_token(authorization)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id


def _org_of(user_id: str) -> Optional[str]:
    try:
        from backend.core.ingest import membership
        return membership.get_org_for_user(user_id)
    except Exception:
        logger.debug("org resolve failed", exc_info=True)
        return None


@get("/me")
async def identity_me(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.identity.identity_service import resolve_person_for_account
    return {"user_id": uid, "person_entity_id": resolve_person_for_account(uid),
            "org_id": _org_of(uid)}


@get("/candidates")
async def identity_candidates(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    uid = _extract_user(authorization)
    org_id = _org_of(uid)
    if not org_id:
        return {"already_linked": None, "account": {}, "candidates": [],
                "note": "аккаунт не состоит в организации"}
    from backend.core.identity.identity_service import candidate_persons_for_account
    return await candidate_persons_for_account(uid, org_id)


@post("/link")
async def identity_link(
    data: dict,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    uid = _extract_user(authorization)
    org_id = _org_of(uid)
    if not org_id:
        return {"ok": False, "error": "аккаунт не состоит в организации"}
    person_entity_id = str((data or {}).get("person_entity_id") or "")
    if not person_entity_id:
        return {"ok": False, "error": "person_entity_id обязателен"}
    from backend.core.identity.identity_service import confirm_stitch
    return confirm_stitch(uid, org_id, person_entity_id)


@post("/unlink")
async def identity_unlink(
    data: dict,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    uid = _extract_user(authorization)
    org_id = _org_of(uid)
    if not org_id:
        return {"ok": False, "error": "аккаунт не состоит в организации"}
    from backend.core.identity.identity_service import unlink_stitch
    return unlink_stitch(uid, org_id)


@get("/person/{person_entity_id:str}/profile")
async def identity_person_profile(
    person_entity_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Когнитивный профиль по Person-сущности графа (CogniLayer Ф1).

    Два замка внутри сервиса: подтверждённая сшивка «это я» + запрашивающий
    в той же организации. Несшит / чужой орг → found=False (без деталей)."""
    uid = _extract_user(authorization)
    org_id = _org_of(uid)
    if not org_id:
        return {"found": False, "note": "аккаунт не состоит в организации"}
    from backend.core.identity.person_profile import profile_for_person
    prof = await profile_for_person(person_entity_id, org_id, requester_uid=uid)
    if prof is None:
        return {"found": False}
    # Аудит чтения: когнитивный профиль — чувствительные данные о человеке,
    # каждое чтение чужого профиля оставляет след (compliance).
    if prof.get("user_id") and prof["user_id"] != uid:
        from backend.core.observability import audit_log
        await audit_log.emit(action="identity.person_profile.read",
                             user_id=uid,
                             resource=f"person:{person_entity_id}",
                             metadata={"subject_user_id": prof["user_id"]})
    return {"found": True, **prof}


router = Router(
    path="/identity",
    route_handlers=[identity_me, identity_candidates, identity_link,
                    identity_unlink, identity_person_profile],
    tags=["Identity"],
)
