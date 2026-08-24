"""Rate-limit ASGI middleware for tessent_brain API.

Применяет sliding-window лимиты на каждый запрос:
- Аутентифицированный (есть user_id из JWT) — лимит per-user.
- Неаутентифицированный — лимит per-IP, заметно строже (anti-abuse).
- Per-tenant лимит (если задан) применяется параллельно user-лимиту.

Если Redis недоступен — middleware пропускает запросы (fail-open) и
логирует warning. Это сознательный trade-off: лучше пропустить, чем уронить
работающий API из-за инфраструктурного сбоя.

Настраивается env-переменными (см. backend.config.Settings):
- RATE_LIMIT_ENABLED=true|false (default true)
- RATE_LIMIT_USER_PER_MINUTE=60
- RATE_LIMIT_ANON_PER_MINUTE=10
- RATE_LIMIT_TENANT_PER_MINUTE=1000

Эндпоинты, которые НЕ должны попадать под лимит (health, openapi schema),
перечислены в `EXEMPT_PATHS`.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

EXEMPT_PATHS: frozenset[str] = frozenset({
    "/healthz",
    "/livez",
    "/readyz",
    "/schema",
    "/schema/openapi.json",
    "/schema/openapi.yaml",
    "/schema/redoc",
    "/schema/swagger",
    "/schema/elements",
    "/",
})


class _RedisCounter:
    """Тонкая обёртка над Redis sliding-window: ZADD + ZREMRANGEBYSCORE."""

    def __init__(self) -> None:
        self._client = None
        self._tried_init = False

    async def _client_or_none(self):
        if self._tried_init:
            return self._client
        self._tried_init = True
        try:
            from backend.db.redis_client import get_redis
            redis = await get_redis()
            if await redis.health_check():
                self._client = redis.client
        except Exception as exc:
            logger.warning("Rate-limit: Redis unavailable, falling back to allow-all: %s", exc)
            self._client = None
        return self._client

    async def hit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int, int]:
        """Учесть запрос, вернуть (allowed, current_count, retry_after_seconds).

        retry_after_seconds == 0, когда allowed.
        """
        client = await self._client_or_none()
        if client is None:
            return True, 0, 0
        now = time.time()
        cutoff = now - window_seconds
        try:
            pipe = client.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            _, count = await pipe.execute()
        except Exception as exc:
            logger.warning("Rate-limit: Redis pipeline error, allow-all: %s", exc)
            return True, 0, 0
        if count >= limit:
            # Найдём самый старый таймстемп, чтобы посчитать честный Retry-After.
            try:
                oldest = await client.zrange(key, 0, 0, withscores=True)
                if oldest:
                    _, ts = oldest[0]
                    retry_after = max(1, int(window_seconds - (now - float(ts))))
                else:
                    retry_after = window_seconds
            except Exception:
                retry_after = window_seconds
            return False, int(count), retry_after
        try:
            await client.zadd(key, {f"{now}:{id(self)}": now})
            await client.expire(key, window_seconds + 5)
        except Exception as exc:
            logger.warning("Rate-limit: Redis write error: %s", exc)
        return True, int(count) + 1, 0


_counter = _RedisCounter()


def _extract_user_id(scope) -> Optional[str]:
    """Достать user_id ИЗ JWT для бакета rate-limit'а.

    Важно: для лимита нам нужен идентификатор-бакет, а НЕ доверенная
    identity. Если в Bearer-токене есть sub — это валидный идентификатор
    бакета, даже когда подпись не проверена (issue #107 strict-auth
    отклоняет неподписанные токены для chat/действий, но это про
    авторизацию, не про counting). Иначе залогиненный фронт с
    неподписанным dev-токеном попадает в anon-bucket (10/min) — HomeTab
    + ChatPanel-листы вылетают за лимит на первой же загрузке.

    Поэтому здесь используем compat-режим: декодируем sub без проверки
    подписи. Атакующему всё равно проще подделать sub — но даже тогда
    он остаётся в *своём* бакете, не сваливаясь на общий anon-IP.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    # 1) приоритет — проверенная подпись (service / user JWT)
    try:
        from backend.core.auth.service_token import verify_service_token, verify_user_token
        claims = verify_service_token(token) or verify_user_token(token)
        if claims:
            uid = claims.get("sub") or claims.get("user_id")
            if uid:
                return str(uid)
    except Exception:
        pass
    # 2) фолбэк — sub из payload без проверки (для счётчика этого хватает)
    try:
        import base64
        import json
        parts = token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            uid = payload.get("sub") or payload.get("user_id")
            if uid:
                return str(uid)
    except Exception:
        pass
    return None


def _extract_tenant_id(scope) -> Optional[str]:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    return headers.get("x-tenant-id")


def _is_private(ip: str) -> bool:
    """Адрес из локальной сети / loopback — то есть перед нами свой прокси."""
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local
    except Exception:
        return False


def _trusted_hops() -> int:
    """Сколько ПОСЛЕДНИХ записей X-Forwarded-For поставили наши прокси.

    -1 (по умолчанию) — определить самим по адресу соединения.
    0 — не верить заголовку вовсе. N — доверять N последним записям.
    """
    import os
    raw = (os.getenv("TRUSTED_PROXY_HOPS", "") or "").strip()
    if not raw:
        return -1
    try:
        return max(0, min(int(raw), 8))
    except ValueError:
        return -1


def _client_ip(scope) -> str:
    """Адрес клиента для бакета rate-limit'а.

    ЗДЕСЬ БЫЛА ДЫРА. Брали ПЕРВУЮ запись X-Forwarded-For — а её пишет сам
    клиент. Caddy (наш reverse proxy) заголовок не перезаписывает, а
    ДОПИСЫВАЕТ: пришло `X-Forwarded-For: 1.2.3.4` — уйдёт
    `1.2.3.4, <настоящий адрес>`. Значит достаточно было менять эту строку
    на каждом запросе, чтобы каждая попытка попадала в новый бакет. А
    анонимный лимит (10/мин на адрес) — единственное, что стояло между
    перебором паролей и формой входа. Перебор становился неограниченным.

    Теперь адрес берётся С КОНЦА: последние записи дописаны прокси, и
    подделать их клиент не может — он пишет только в начало. Сколько
    записей наши — берём из TRUSTED_PROXY_HOPS, а без настройки решаем по
    адресу соединения: если запрос пришёл из локальной сети, перед нами
    свой прокси (одна запись — его), если из интернета — мы открыты
    напрямую и заголовку верить нельзя вовсе.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    client = scope.get("client")
    peer = client[0] if client else ""

    hops = _trusted_hops()
    if hops < 0:
        hops = 1 if (peer and _is_private(peer)) else 0

    if hops:
        fwd = headers.get("x-forwarded-for")
        if fwd:
            parts = [p.strip() for p in fwd.split(",") if p.strip()]
            if parts:
                # len-hops: отступаем от конца на число «своих» записей.
                # Клиент может дописать сколько угодно записей слева — они
                # только сдвигают индекс, но не попадают в выбор.
                return parts[max(0, len(parts) - hops)]
    return peer or "unknown"


async def _send_429(send, retry_after: int, message: str) -> None:
    body = (
        b'{"detail":"' + message.encode("utf-8") + b'","status_code":429}'
    )
    await send({
        "type": "http.response.start",
        "status": 429,
        "headers": [
            (b"content-type", b"application/json"),
            (b"retry-after", str(retry_after).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})


class RateLimitMiddleware:
    """ASGI middleware for tessent_brain API."""

    def __init__(
        self,
        app,
        *,
        enabled: bool = True,
        user_per_minute: int = 60,
        anon_per_minute: int = 10,
        tenant_per_minute: int = 1000,
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.user_limit = user_per_minute
        self.anon_limit = anon_per_minute
        self.tenant_limit = tenant_per_minute
        self.window = 60

    async def __call__(self, scope, receive, send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if path in EXEMPT_PATHS or path.startswith("/schema/"):
            await self.app(scope, receive, send)
            return
        user_id = _extract_user_id(scope)
        tenant_id = _extract_tenant_id(scope)

        if user_id:
            allowed, count, retry_after = await _counter.hit(
                f"rl:user:{user_id}", self.user_limit, self.window,
            )
            if not allowed:
                logger.debug(
                    "rate_limit_block",
                    extra={"scope": "user", "user_id": user_id, "count": count, "path": path},
                )
                await _send_429(send, retry_after, "User rate limit exceeded")
                return
        else:
            ip = _client_ip(scope)
            allowed, count, retry_after = await _counter.hit(
                f"rl:ip:{ip}", self.anon_limit, self.window,
            )
            if not allowed:
                logger.debug(
                    "rate_limit_block",
                    extra={"scope": "ip", "ip": ip, "count": count, "path": path},
                )
                await _send_429(send, retry_after, "Anonymous rate limit exceeded")
                return

        if tenant_id:
            allowed, count, retry_after = await _counter.hit(
                f"rl:tenant:{tenant_id}", self.tenant_limit, self.window,
            )
            if not allowed:
                logger.debug(
                    "rate_limit_block",
                    extra={"scope": "tenant", "tenant_id": tenant_id, "count": count, "path": path},
                )
                await _send_429(send, retry_after, "Tenant rate limit exceeded")
                return

        await self.app(scope, receive, send)
