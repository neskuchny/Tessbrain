# -*- coding: utf-8 -*-
"""
Service-JWT verification (аудит S1 — закрытие дыры в аутентификации).

ПРОБЛЕМА (из аудита): meetflow/боты подписывают service-JWT секретом
TESSENT_SERVICE_JWT_SECRET (tessent_service_auth.make_service_token), но мозг
его НЕ ПРОВЕРЯЛ — TenantResolverMiddleware декодировал с verify_signature=False,
а /chat и /compose брали user_id прямо из query/body. Итог: любой, кто дёрнет
endpoint с произвольным ?user_id=, получал данные этого юзера. Подпись была
декоративной.

ЭТОТ МОДУЛЬ: настоящая проверка подписи + audience + issuer + exp, и извлечение
ДОВЕРЕННОГО user_id из claim. Конфиг уже есть в settings (service_jwt_secret/
audience/issuer) — просто не использовался.

Использование (за флагом enable_service_auth — см. resolve_service_user_id):
- если запрос несёт валидный service-JWT → user_id берётся из claim (доверенно);
- невалидный токен / попытка действовать за ЧУЖОЙ user_id → отказ.

Чистый stdlib + PyJWT (уже зависимость).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def decode_hs256(token: str, secret: str) -> Optional[dict]:
    """Проверить подпись HS256 (HMAC-SHA256) и вернуть payload, или None.

    Service-токены симметричные (HS256) — это просто HMAC, тяжёлый
    `cryptography` (как у PyJWT для EC/RSA) не нужен. Формат JWT идентичен
    тому, что генерирует PyJWT jwt.encode(..., algorithm='HS256'), поэтому
    токены meetflow проверяются здесь без изменений на стороне отправителя."""
    if not token or not secret:
        return None
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        if header.get("alg") != "HS256":
            return None  # принимаем ТОЛЬКО HS256 (защита от alg=none/подмены)
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected = hmac.new(secret.encode("utf-8"), signing_input,
                            hashlib.sha256).digest()
        actual = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, actual):
            return None  # подпись неверна
        return json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        logger.debug("decode_hs256: %s", exc)
        return None


def extract_bearer(headers) -> Optional[str]:
    """Достать Bearer-токен из заголовков (dict / Litestar Headers)."""
    if headers is None:
        return None
    get = getattr(headers, "get", None)
    auth = ""
    if callable(get):
        auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


def verify_service_token(token: Optional[str]) -> Optional[dict]:
    """Проверить service-JWT (подпись + aud + iss + exp). Вернуть claims или None.

    None означает: токена нет / подпись неверна / просрочен / не тот
    audience/issuer / секрет не настроен. Caller трактует None как
    «сервисная аутентификация не пройдена»."""
    if not token:
        return None
    try:
        from backend.config import settings
        secret = getattr(settings, "service_jwt_secret", "") or ""
        if not secret:
            # секрет не задан — проверять нечем; в проде должен быть set'нут
            logger.debug("service_token: service_jwt_secret not configured")
            return None
        audience = getattr(settings, "service_jwt_audience", "tessent-brain")
        issuer = getattr(settings, "service_jwt_issuer", "meetflow")

        claims = decode_hs256(token, secret)
        if claims is None:
            return None  # подпись/формат неверны
        # обязательные claim'ы
        for req in ("exp", "iat", "aud", "iss"):
            if req not in claims:
                logger.debug("service_token: missing claim %s", req)
                return None
        # exp (с запасом 30с против рассинхрона часов)
        if int(claims["exp"]) < int(time.time()) - 30:
            logger.debug("service_token: expired")
            return None
        # audience может быть строкой или списком (JWT-спека)
        aud = claims["aud"]
        aud_ok = (aud == audience) or (isinstance(aud, list) and audience in aud)
        if not aud_ok:
            logger.debug("service_token: bad audience %r", aud)
            return None
        if claims["iss"] != issuer:
            logger.debug("service_token: bad issuer %r", claims["iss"])
            return None
        return claims
    except Exception as exc:
        logger.debug("service_token: verification failed: %s", exc)
        return None


def resolve_service_user_id(headers, requested_user_id: str) -> tuple[str, str]:
    """Определить ДОВЕРЕННЫЙ user_id для сервисного вызова.

    Возвращает (user_id, source). Бросает PermissionError для отказов, которые
    вызывающий маппит в 401/403.

    Логика (только при enable_service_auth=true; иначе — паритет со старым
    поведением: возвращаем requested_user_id, source='unverified'):
    - валидный service-JWT с claim user_id:
        · если requested_user_id пуст или совпадает → (claim_user_id, 'service_jwt');
        · если requested_user_id отличается → PermissionError (нельзя за чужого);
    - есть Bearer, но токен невалиден → PermissionError (401);
    - нет Bearer вовсе → PermissionError (требуется сервисная аутентификация).
    """
    try:
        from backend.core.config.feature_flags import get_feature_flags
        enforce = bool(get_feature_flags().enable_service_auth)
    except Exception:
        enforce = False

    if not enforce:
        return requested_user_id, "unverified"

    token = extract_bearer(headers)
    if token is None:
        raise PermissionError("service authentication required (no bearer token)")
    claims = verify_service_token(token)
    if claims is None:
        raise PermissionError("invalid service token")
    claim_uid = str(claims.get("user_id") or "").strip()
    if not claim_uid:
        raise PermissionError("service token has no user_id claim")
    req = (requested_user_id or "").strip()
    if req and req != claim_uid:
        raise PermissionError("service token not authorized for requested user_id")
    return claim_uid, "service_jwt"


def verify_user_token(token: Optional[str]) -> Optional[dict]:
    """Проверить ПОЛЬЗОВАТЕЛЬСКИЙ JWT (веб-UI, /auth/login) — подпись HS256 на
    jwt_secret_key (+ legacy) + exp. Claims: sub=user_id, email, name. Вернуть
    claims или None. Закрывает web-UI часть S1: user_id из проверенного sub,
    а не из произвольного query-param."""
    if not token:
        return None
    try:
        from backend.config import settings
        secrets: list[str] = []
        primary = getattr(settings, "jwt_secret_key", "") or ""
        if primary:
            secrets.append(primary)
        import os as _os
        # Те же кандидаты, что использует auth_middleware (Supabase/собственный
        # секрет): иначе в strict-режиме легитимный веб/Supabase-токен не
        # прошёл бы проверку и UI сломался бы. Дедуп — ниже.
        for env_name in ("JWT_SECRET", "SUPABASE_JWT_SECRET", "SECRET_KEY",
                         "JWT_SECRET_KEY", "LEGACY_JWT_SECRET"):
            val = _os.environ.get(env_name, "")
            if val and val not in secrets:
                secrets.append(val)
        if not secrets:
            return None
        claims = None
        for sec in secrets:
            claims = decode_hs256(token, sec)
            if claims is not None:
                break
        if claims is None:
            return None
        # exp (если есть) — с запасом 30с
        if "exp" in claims and int(claims["exp"]) < int(time.time()) - 30:
            return None
        return claims
    except Exception as exc:
        logger.debug("verify_user_token: %s", exc)
        return None


def _jwt_secret_configured() -> bool:
    """Есть ли вообще ключ, которым можно проверить ПОДПИСЬ токена?

    Strict-режим имеет смысл ТОЛЬКО когда настроен секрет: иначе ни один
    реальный токен (Supabase/web) нельзя верифицировать, и strict отвергает
    ВСЁ подряд → каскад 403 на /chat, knowledge-sync, ontology, object360.
    Кандидаты — те же, что у verify_user_token/auth_middleware.

    Авто-отключение ниже — тихая деградация безопасности, поэтому в
    PRODUCTION до неё дело не доходит: Settings не поднимаются без секрета
    (config._require_verifiable_jwt_secret_in_production). Здесь остаётся
    мягкий путь для dev. Список env-имён обязан совпадать с тем, что
    проверяет валидатор — за этим следит
    tests/unit/test_jwt_secret_required.py.
    """
    try:
        from backend.config import settings
        val = (getattr(settings, "jwt_secret_key", "") or "").strip()
        # "your-jwt-secret-key" — дефолт-плейсхолдер из config.py, не секрет.
        if val and val != "your-jwt-secret-key":
            return True
    except Exception:
        pass
    import os as _os
    for env_name in ("JWT_SECRET", "SUPABASE_JWT_SECRET", "SECRET_KEY",
                     "JWT_SECRET_KEY", "LEGACY_JWT_SECRET"):
        if (_os.environ.get(env_name, "") or "").strip():
            return True
    return False


def _strict_chat_auth() -> bool:
    try:
        from backend.core.config.feature_flags import get_feature_flags
        if not bool(get_feature_flags().enable_strict_chat_auth):
            return False
        # Auto-disable: strict без секрета = footgun (нечем проверять подпись,
        # отвергаются все валидные токены). Лучше деградировать в compat-режим
        # с warning'ом, чем ронять весь UI в 403.
        if not _jwt_secret_configured():
            logger.warning(
                "enable_strict_chat_auth=true, но НЕ настроен JWT-секрет "
                "(JWT_SECRET/SUPABASE_JWT_SECRET/SECRET_KEY/jwt_secret_key) — "
                "strict-режим отключён, иначе все токены отвергались бы. "
                "Задайте SUPABASE_JWT_SECRET в .env для реальной защиты.")
            return False
        return True
    except Exception:
        return False


def trusted_user_id(headers, requested_user_id: str) -> tuple[str, str]:
    """Объединённый резолвер для /chat: доверяем user_id из ВАЛИДНОГО
    токена (сервисного ИЛИ пользовательского), блокируем спуфинг чужого
    ?user_id=. Без токена — requested как есть (обратная совместимость).

    Строгий режим (enable_strict_chat_auth, по умолчанию OFF): без валидного
    токена — отказ. Включать, когда фронтенд гарантированно шлёт Bearer
    (см. docs/ru/PRODUCTION_HARDENING.md «strict auth mode»).

    Возвращает (user_id, source): 'service_jwt' | 'user_jwt' | 'unverified'.
    """
    token = extract_bearer(headers)
    if token is None:
        if _strict_chat_auth():
            raise PermissionError("authentication required (no bearer token)")
        return requested_user_id, "unverified"
    # 1) сервисный токен (бот/meetflow)
    claims = verify_service_token(token)
    source = "service_jwt"
    claim_uid = ""
    if claims is not None:
        claim_uid = str(claims.get("user_id") or "").strip()
    else:
        # 2) пользовательский токен (веб-UI): user_id = sub
        uclaims = verify_user_token(token)
        if uclaims is not None:
            source = "user_jwt"
            claim_uid = str(uclaims.get("sub") or uclaims.get("user_id") or "").strip()
    if not claim_uid:
        if _strict_chat_auth():
            raise PermissionError("invalid token")
        # токен есть, но не наш / без user_id — не трогаем (passthrough)
        return requested_user_id, "unverified"
    req = (requested_user_id or "").strip()
    if req and req != claim_uid:
        raise PermissionError("token not authorized for requested user_id")
    return claim_uid, source


def decode_claims_guarded(token: Optional[str]) -> dict:
    """Verify-first извлечение claims из JWT (issue #107, T-1).

    Единый безопасный путь вместо разрозненных
    `jwt.decode(token, options={"verify_signature": False})` по роутам и
    middleware. Логика:
    - валидная ПОДПИСЬ (service-JWT ИЛИ пользовательский JWT) → проверенные
      claims (доверенно);
    - подпись НЕ подтверждена:
        · строгий режим (enable_strict_chat_auth=true) → {} — вызывающий
          трактует отсутствие sub/user_id как 401 (подделка отклонена);
        · иначе (по умолчанию) → декод БЕЗ проверки подписи (обратная
          совместимость с AI-Coach/Report-Automation, пока они не начнут
          слать подписанные токены), с предупреждением в лог.
    Пустой/битый токен → {}.

    Проверка подписи — чистый HMAC (decode_hs256), не зависит от тяжёлого
    `cryptography` у PyJWT.
    """
    if not token:
        return {}
    claims = verify_service_token(token)
    if claims is not None:
        return claims
    uclaims = verify_user_token(token)
    if uclaims is not None:
        return uclaims
    # подпись не подтверждена ни одним секретом
    if _strict_chat_auth():
        logger.warning(
            "decode_claims_guarded: token signature NOT verified — rejected "
            "(strict mode)")
        return {}
    # compat-режим: декодируем payload БЕЗ проверки подписи (чистый stdlib,
    # не зависим от PyJWT/cryptography). Это недоверенный путь — только пока
    # внешние клиенты не начнут слать подписанные токены.
    try:
        payload_b64 = token.split(".")[1]
        payload = json.loads(_b64url_decode(payload_b64))
        if not isinstance(payload, dict):
            return {}
        logger.warning(
            "decode_claims_guarded: token signature NOT verified (compat "
            "mode); enable enable_strict_chat_auth to enforce")
        return payload
    except Exception as exc:
        logger.debug("decode_claims_guarded: unverifiable token: %s", exc)
        return {}


def service_user_id_if_present(headers, requested_user_id: str) -> tuple[str, str]:
    """Мягкий вариант для смешанных endpoint'ов (/chat: и веб-UI, и сервисы).

    - Если есть ВАЛИДНЫЙ service-JWT → user_id из claim (доверенно); попытка
      действовать за чужого user_id → PermissionError (спуфинг блокируется
      даже без enable_service_auth, раз токен валиден).
    - Если токена нет / он не наш service-JWT (например, пользовательский
      Supabase-JWT) → возвращаем requested как есть (веб-UI не ломаем).

    Это закрывает «service-token + произвольный user_id», не требуя токен от
    обычных пользователей. Полная защита веб-UI (валидация Supabase-JWT и
    user_id из его claim) — отдельный пункт, см. docs/ru/PRODUCTION_HARDENING.md.
    """
    token = extract_bearer(headers)
    if token is None:
        return requested_user_id, "unverified"
    claims = verify_service_token(token)
    if claims is None:
        # не наш service-JWT (возможно пользовательский) — не трогаем
        return requested_user_id, "unverified"
    claim_uid = str(claims.get("user_id") or "").strip()
    if not claim_uid:
        return requested_user_id, "service_jwt_no_uid"
    req = (requested_user_id or "").strip()
    if req and req != claim_uid:
        raise PermissionError("service token not authorized for requested user_id")
    return claim_uid, "service_jwt"
