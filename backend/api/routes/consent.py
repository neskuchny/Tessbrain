# -*- coding: utf-8 -*-
"""Согласия на предоставление своего профиля — личный кабинет человека.

Только сам человек, только по проверенному токену: работодатель или админ не
может дать согласие за сотрудника. Пока обмен профилями выключен
(ENABLE_PROFILE_EXCHANGE, по умолчанию OFF), согласия можно выдавать и
отзывать «впрок» — наружу всё равно ничего не уходит; это видно в /status.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from litestar import Request, Router, delete, get, post
from litestar.exceptions import HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _verified_user(request: Request) -> str:
    """Согласие — строго личное: нужен ПОДПИСАННЫЙ токен, ?user_id= мало."""
    from backend.core.auth.service_token import trusted_user_id
    try:
        uid, source = trusted_user_id(request.headers, "")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if source not in ("user_jwt", "service_jwt") or not uid:
        raise HTTPException(
            status_code=401,
            detail="нужен вход в аккаунт: согласие может дать только сам человек")
    return uid


class GrantConsentRequest(BaseModel):
    grantee: str                       # id организации/потребителя или "*"
    scope: Optional[List[str]] = None  # разделы слепка; None = весь
    days: int = 90
    note: Optional[str] = None


@get("/status")
async def consent_status(request: Request) -> Dict[str, Any]:
    from backend.core.consent import exchange_enabled, list_for
    uid = _verified_user(request)
    from backend.core.consent.consent_store import CONSENT_TEXT_VERSION, SCOPES
    return {
        "exchange_enabled": exchange_enabled(),
        "consent_text_version": CONSENT_TEXT_VERSION,
        "available_scopes": list(SCOPES),
        "active_consents": len([c for c in list_for(uid) if c["active"]]),
        "note": ("Обмен профилями сейчас выключен: согласия можно готовить, "
                 "но наружу ничего не уходит."
                 if not exchange_enabled() else
                 "Обмен профилями включён: активные согласия действуют."),
    }


@get("/me")
async def my_consents(request: Request) -> Dict[str, Any]:
    from backend.core.consent import list_for
    uid = _verified_user(request)
    return {"consents": list_for(uid)}


@post("/me")
async def grant_consent(request: Request, data: GrantConsentRequest) -> Dict[str, Any]:
    from backend.core.consent import grant
    uid = _verified_user(request)
    try:
        rec = grant(uid, grantee=data.grantee, scope=data.scope,
                    days=data.days, note=data.note or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "consent": {**rec, "active": True}}


@delete("/me/{consent_id:str}", status_code=200)
async def revoke_consent(request: Request, consent_id: str) -> Dict[str, Any]:
    from backend.core.consent import revoke
    uid = _verified_user(request)
    if not revoke(uid, consent_id):
        raise HTTPException(status_code=404, detail="согласие не найдено")
    return {"status": "success", "revoked": consent_id}


router = Router(path="/consent",
                route_handlers=[consent_status, my_consents, grant_consent,
                                revoke_consent],
                tags=["Consent"])
