# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Auth Middleware
Утилиты для работы с JWT токенами
"""
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# JWT Configuration. Все ключи берутся ТОЛЬКО из окружения — никаких hardcoded
# дефолтов. Если переменные не выставлены, проверка подписи не пройдёт и токены
# с невалидной подписью будут отклоняться.
JWT_SECRET = (
    os.environ.get("JWT_SECRET")
    or os.environ.get("SUPABASE_JWT_SECRET")
    or os.environ.get("SECRET_KEY")
    # JWT_SECRET_KEY — имя из pydantic-настроек (settings.jwt_secret_key).
    # Раньше его тут не было → подпись/проверка читали РАЗНЫЕ env, и токен,
    # подписанный одним секретом, не верифицировался другим (каскад 403).
    or os.environ.get("JWT_SECRET_KEY")
    or ""
)
# Legacy секрет нужен только во время ротации — задаётся явно через env.
LEGACY_JWT_SECRET = os.environ.get("LEGACY_JWT_SECRET", "")
JWT_ALGORITHM = "HS256"

if not JWT_SECRET:
    logger.warning(
        "JWT_SECRET is not configured; signature verification will reject all tokens. "
        "Set JWT_SECRET / SUPABASE_JWT_SECRET / SECRET_KEY env var."
    )


def get_user_id_from_token(authorization: Optional[str]) -> Optional[str]:
    """
    Извлечь user_id из JWT токена в заголовке Authorization.

    Поддерживает токены Supabase (декодирование без верификации подписи)
    и собственные JWT токены (с верификацией).

    Args:
        authorization: Заголовок Authorization (Bearer token)

    Returns:
        user_id или None если токен невалидный
    """
    if not authorization or not authorization.startswith("Bearer "):
        logger.debug("No Bearer token in Authorization header")
        return None

    token = authorization.split(" ", 1)[1]

    # issue #107 T-1: единый verify-first путь. Раньше здесь декодировалось
    # БЕЗ проверки подписи (jwt.decode verify_signature=False) и сразу
    # возвращался sub — подделка проходила. Теперь:
    #   · валидная подпись (service/user-JWT) → доверенный user_id;
    #   · невалидная + strict (enable_strict_chat_auth) → None (→ 401);
    #   · невалидная + compat (по умолчанию) → sub без проверки (обратная
    #     совместимость), с warning. Включение strict закрывает дыру.
    from backend.core.auth.service_token import decode_claims_guarded
    claims = decode_claims_guarded(token)
    user_id = claims.get("sub") or claims.get("user_id")
    if user_id:
        return str(user_id)
    logger.warning("Failed to extract user_id from token")
    return None


def decode_jwt(token: str) -> dict[str, Any]:
    """
    Декодировать JWT токен.

    Args:
        token: JWT токен

    Returns:
        Payload токена

    Raises:
        jwt.InvalidTokenError: если токен невалидный
    """
    import jwt  # лениво: PyJWT тянет cryptography; hot-path (get_user_id_from_token)
    # его не использует — там чистый stdlib verify через service_token.
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidSignatureError:
        if LEGACY_JWT_SECRET and LEGACY_JWT_SECRET != JWT_SECRET:
            return jwt.decode(token, LEGACY_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        raise

