# -*- coding: utf-8 -*-
"""UserIdEnforcerMiddleware — серверное лечение стейлного/чужого ?user_id=.

ПРОБЛЕМА (подтверждена в проде): браузер после смены аккаунта продолжает слать
СТАРЫЙ user_id при валидном токене НОВОГО пользователя. Точечные guard'ы
отказывали (403), но UI разваливался, а токен-less запросы проскальзывали по
легаси-пути. Гоняться за каждым компонентом — whack-a-mole.

РЕШЕНИЕ одним местом: если Bearer ВАЛИДЕН (service- или user-JWT) — user_id в
query ПРИНУДИТЕЛЬНО ПОДМЕНЯЕТСЯ на uid из токена ДО роутинга. Стейлный id
становится безвреден: пользователь всегда видит СВОИ данные, невозможно
запросить чужие. Токена нет — ничего не меняем (внутренние вызовы/легаси).

Только на чувствительных префиксах: admin/orgs/scim/gdpr используют user_id
как ФИЛЬТР под собственным RBAC — там подмена сломала бы админ-флоу."""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import parse_qsl, urlencode

logger = logging.getLogger(__name__)


def _strict_mode() -> bool:
    """STRICT_USER_AUTH=1 — безтокенный запрос с ?user_id= на чувствительных
    префиксах получает 401 вместо легаси-пропуска. Для SaaS-прода обязателен:
    без него истёкшая сессия браузера продолжает читать данные по стейлному
    user_id (подтверждённый сценарий утечки). По умолчанию выключен, чтобы не
    ломать локальный безавторизационный режим разработки."""
    return (os.getenv("STRICT_USER_AUTH", "") or "").strip().lower() in (
        "1", "true", "yes", "on")

# Префиксы per-user данных (совпадает с guarded-роутерами анти-IDOR свипа)
ENFORCED_PREFIXES = (
    "/api/v1/entities", "/api/v1/snapshots", "/api/v1/meetflow",
    "/api/v1/insights", "/api/v1/memory", "/api/v1/wisdom",
    "/api/v1/knowledge", "/api/v1/corrections", "/api/v1/data-bus",
    "/api/v1/lineage", "/api/v1/temporal", "/api/v1/research",
    "/api/v1/metrics", "/api/v1/chat", "/api/v1/task-analysis",
    "/api/v1/agents", "/api/v1/documents", "/api/v1/boards",
    "/api/v1/goals", "/api/v1/knowledge-sync", "/api/v1/analyze",
    "/api/v1/usage", "/api/v1/templates", "/api/v1/automations",
    "/api/v1/integrations", "/api/v1/sima",
)


def _token_uid(headers: list) -> str:
    """uid из ВАЛИДНОГО Bearer (service/user JWT) или ''. Never-raise."""
    try:
        authz = ""
        for k, v in headers or []:
            if k == b"authorization":
                authz = v.decode("latin-1")
                break
        if not authz.lower().startswith("bearer "):
            return ""
        token = authz[7:].strip()
        from backend.core.auth.service_token import (
            verify_service_token,
            verify_user_token,
        )
        claims = verify_service_token(token)
        if claims:
            return str(claims.get("user_id") or "").strip()
        uclaims = verify_user_token(token)
        if uclaims:
            return str(uclaims.get("sub") or uclaims.get("user_id") or "").strip()
    except Exception:
        logger.debug("user_id_enforcer: token check skipped", exc_info=True)
    return ""


class UserIdEnforcerMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "") or ""
        if not path.startswith(ENFORCED_PREFIXES):
            await self.app(scope, receive, send)
            return
        qs = (scope.get("query_string") or b"").decode("latin-1")
        if "user_id" not in qs:
            await self.app(scope, receive, send)
            return
        uid = _token_uid(scope.get("headers") or [])
        if not uid and _strict_mode():
            # Валидного токена нет (отсутствует/истёк/битый), а запрос просит
            # per-user данные — в строгом режиме отклоняем: пропуск означал бы
            # чтение по произвольному user_id без аутентификации.
            logger.warning(
                f"user_id enforcer: безтокенный запрос отклонён (strict) [{path}]")
            body = json.dumps({
                "status_code": 401,
                "detail": "authentication required (token missing or expired)",
            }).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        if uid:
            try:
                pairs = parse_qsl(qs, keep_blank_values=True)
                changed = False
                out = []
                for k, v in pairs:
                    if k == "user_id" and v != uid:
                        logger.warning(
                            f"user_id enforced: {v[:8]}…→{uid[:8]}… [{path}] "
                            "(стейлный id из клиента подменён на uid токена)")
                        out.append((k, uid))
                        changed = True
                    else:
                        out.append((k, v))
                if changed:
                    scope["query_string"] = urlencode(out).encode("latin-1")
            except Exception:
                logger.debug("user_id_enforcer: rewrite skipped", exc_info=True)
        await self.app(scope, receive, send)
