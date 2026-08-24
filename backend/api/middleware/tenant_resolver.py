"""ASGI middleware: извлечь tenant_id и положить в contextvar.

Источники (по приоритету):
1. JWT-claim `tenant_id` или `org_id` в Authorization header.
   Декодируется БЕЗ верификации подписи — middleware считает, что подпись
   проверяется вышестоящим auth-слоем (Supabase / api gateway). Это нужно
   только для извлечения метаданных, не для авторизации.
2. Заголовок `X-Tenant-Id`. Полезен для:
   - внутренних сервисов, ходящих с сервисным токеном;
   - интеграционных тестов;
   - локальной разработки без JWT.

Если ни одно не задано — tenant_id = None. Дальше сервисы решают сами:
- Storage-слой (Qdrant/Neo4j) при `MULTITENANT_STRICT=true` отклоняет такие
  запросы; иначе работает single-tenant.
- Rate-limit и LLM-квоты просто не применяют tenant-scope.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _decode_jwt_claims_unsafe(token: str) -> dict:
    """Извлечь claims для tenant_id (verify-first, issue #107 T-1).

    В strict-режиме невалидная подпись → {} → tenant_id не подменяется из
    недоверенного токена (fail-closed к дефолтному контексту). В compat
    (по умолчанию) поведение прежнее."""
    try:
        from backend.core.auth.service_token import decode_claims_guarded
        return decode_claims_guarded(token)
    except Exception as exc:
        logger.debug("tenant_resolver: failed to decode JWT: %s", exc)
        return {}


def _extract_from_jwt(headers: dict[str, str]) -> Optional[str]:
    auth = headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1]
    claims = _decode_jwt_claims_unsafe(token)
    # Известные места для tenant_id в Supabase / собственных JWT.
    candidates = [
        claims.get("tenant_id"),
        claims.get("org_id"),
        (claims.get("app_metadata") or {}).get("tenant_id") if isinstance(claims.get("app_metadata"), dict) else None,
        (claims.get("app_metadata") or {}).get("org_id") if isinstance(claims.get("app_metadata"), dict) else None,
    ]
    for c in candidates:
        if c:
            return str(c)
    return None


def _extract_from_header(headers: dict[str, str]) -> Optional[str]:
    return headers.get("x-tenant-id") or headers.get("x-org-id")


class TenantResolverMiddleware:
    """ASGI middleware. Должен идти ДО RateLimitMiddleware.

    Использует contextvars, поэтому корректно работает в async/threadpool,
    не требует pin'ить значение к request.scope.
    """

    def __init__(self, app, *, strict: bool = False) -> None:
        self.app = app
        self.strict = strict

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        jwt_tenant = _extract_from_jwt(headers)
        header_tenant = _extract_from_header(headers)

        if self.strict:
            # STRICT: JWT-claim доверенный (decode_claims_guarded проверяет
            # подпись; при плохой — {}). Голый X-Tenant-Id доверяем ТОЛЬКО
            # если он подкреплён валидным сервис-токеном — иначе внешний
            # запрос, дошедший до бэка напрямую, мог бы спуфнуть чужой
            # tenant заголовком. Внутренние сервисы и так ходят с сервис-
            # токеном, легитимный поток не ломается.
            tenant_id = jwt_tenant
            if tenant_id is None and header_tenant is not None:
                try:
                    from backend.core.auth.service_token import (
                        extract_bearer, verify_service_token)
                    if verify_service_token(extract_bearer(headers)):
                        tenant_id = header_tenant
                    else:
                        logger.warning(
                            "tenant_resolver strict: X-Tenant-Id без валидного "
                            "сервис-токена отклонён (анти-спуфинг)")
                except Exception:
                    logger.debug("service-token check failed", exc_info=True)
        else:
            # COMPAT: прежнее поведение (JWT или заголовок, безусловно)
            tenant_id = jwt_tenant or header_tenant

        if tenant_id is None and self.strict and not _is_exempt(scope.get("path", "")):
            await _send_400(send, "tenant_id is required (verified JWT claim or X-Tenant-Id with service token)")
            return

        if tenant_id is not None:
            from backend.core.observability.tenant_context import (
                reset_current_tenant,
                set_current_tenant,
            )
            token = set_current_tenant(tenant_id)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_current_tenant(token)
        else:
            await self.app(scope, receive, send)


_EXEMPT_PATHS = frozenset({
    "/healthz", "/livez", "/readyz", "/", "/ping",
})


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_PATHS or path.startswith("/schema/") or path.startswith("/api/v1/auth/")


async def _send_400(send, message: str) -> None:
    body = b'{"error":{"code":"TENANT_REQUIRED","message":"' + message.encode("utf-8") + b'"}}'
    await send({
        "type": "http.response.start",
        "status": 400,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
