# -*- coding: utf-8 -*-
"""Единственное место, где решается «этот запрос — от администратора».

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ. Четыре админ-роута (audit-log, retention,
llm-profiles, billing-admin) заводили себе по собственному `_require_admin`,
и они разъехались. Худший вариант — admin_audit: он звал
`jwt.decode(token, options={"verify_signature": False})` и брал оттуда роль.
Подпись при этом не проверялась вообще. То есть кто угодно мог собрать
руками токен

    {"sub": "любой", "role": "owner"}

подписать чем угодно — и прочитать журнал аудита ЛЮБОГО тенанта
(`?tenant_id=...` открывается как раз ролью OWNER). Ни пароля, ни ключа не
нужно: три base64-строки через точку.

ПРАВИЛО, которое здесь закреплено:

    роль берётся ТОЛЬКО из токена с подтверждённой подписью.

Личность (sub) в compat-режиме по-прежнему принимается неподписанной —
это осознанная обратная совместимость для внешних клиентов, которые ещё не
научились подписывать (см. service_token.decode_claims_guarded). Но роль —
никогда: неподписанный токен получает Role.NONE и упирается в 403.

Правило уже действовало в orgs.py; этот модуль просто делает его общим,
чтобы следующий админ-роут не начал с копипасты дырявой версии.

Побочный эффект честный: если в окружении вообще НЕ задан JWT-секрет,
проверить подпись нечем, и админские ручки отвечают 403 всем. Для админских
операций отказ — правильная сторона отказа, и сообщение прямо говорит, что
делать.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar.exceptions import HTTPException

from backend.core.auth.rbac import (
    PermissionDenied,
    Role,
    extract_role_from_claims,
    set_current_role,
)

logger = logging.getLogger(__name__)

_NO_SECRET_HINT = (
    "роль не подтверждена: токен без проверяемой подписи. "
    "Задайте JWT-секрет (SUPABASE_JWT_SECRET / JWT_SECRET) и выдавайте "
    "админам подписанные токены."
)


def caller_identity(authorization: Optional[str]) -> tuple[str, Role]:
    """(user_id, role) для запроса. Роль — только из проверенной подписи.

    Бросает HTTPException(401), если Bearer нет или в токене нет sub.
    Роль неподписанного токена — Role.NONE (не 401: личность может быть
    валидной, прав просто нет).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]

    from backend.core.auth.service_token import (
        decode_claims_guarded,
        verify_service_token,
        verify_user_token,
    )

    verified = verify_service_token(token) or verify_user_token(token)
    if verified is not None:
        claims: dict[str, Any] = verified
        role = extract_role_from_claims(claims)
    else:
        try:
            # strict → {} (подделка отброшена), compat → payload без проверки
            claims = decode_claims_guarded(token) or {}
        except Exception as exc:
            raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
        # Ключевая строка всего модуля: роль неподписанного токена не признаём.
        role = Role.NONE

    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    set_current_role(role)
    return str(user_id), role


def require_role(
    authorization: Optional[str],
    minimum: Role = Role.ADMIN,
) -> tuple[str, Role]:
    """Требует роль не ниже `minimum`. Возвращает (user_id, role).

    Отказ — PermissionDenied (роут маппит его в 403).
    """
    user_id, role = caller_identity(authorization)
    if role < minimum:
        if role is Role.NONE:
            logger.warning(
                "admin_guard: отказ — роль не подтверждена подписью [%s…]",
                str(user_id)[:8],
            )
        raise PermissionDenied(minimum, role)
    return user_id, role


def require_admin(authorization: Optional[str]) -> tuple[str, Role]:
    """Частый случай: нужна роль ADMIN или выше."""
    return require_role(authorization, Role.ADMIN)


__all__ = ["_NO_SECRET_HINT", "caller_identity", "require_admin", "require_role"]
