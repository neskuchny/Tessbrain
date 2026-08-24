"""Idempotency-Key middleware (W6 prod-hardening).

Если клиент шлёт `Idempotency-Key: <uuid>` на не-GET запрос:
- На первом приходе: пропускаем в handler, кешируем (status, headers, body)
  в Redis с TTL=1 час по ключу `idem:{tenant}:{key}`.
- На retry с тем же ключом и тем же fingerprint'ом тела: возвращаем
  кешированный response без вызова handler'а.
- Если retry приходит с тем же ключом, но ДРУГИМ телом → 409 CONFLICT
  (это сигнал баги в клиенте, не дублирование).

Зачем: meetflow при сетевом сбое ретрит ingest_meeting → без idempotency
получим 2 раза извлечение сущностей и удвоенный LLM-расход.

При недоступном Redis fail-open: кэш не работает, но запросы проходят
(дублирование возможно, но это деградация а не блок).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Только эти методы кэшируем — GET и так должен быть идемпотентным.
_CACHED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Идempotency-key должен быть UUID-like / slug. Защищаем от инъекции в Redis-key.
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")

_TTL_SECONDS = 60 * 60  # 1 час


def _decode_headers(scope_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope_headers}


def _make_redis_key(tenant: Optional[str], idem_key: str) -> str:
    return f"idem:{tenant or 'anon'}:{idem_key}"


async def _get_redis_client():
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        if not await redis.health_check():
            return None
        return redis.client
    except Exception as exc:
        logger.debug("idempotency: Redis unavailable: %s", exc)
        return None


async def _read_body(receive) -> tuple[bytes, list[dict]]:
    """Прочитать всё тело запроса, сохранив msg'и для повторной отдачи."""
    body = b""
    messages: list[dict] = []
    while True:
        msg = await receive()
        messages.append(msg)
        if msg["type"] != "http.request":
            break
        body += msg.get("body", b"") or b""
        if not msg.get("more_body"):
            break
    return body, messages


def _replay_receive_factory(messages: list[dict]):
    """Сделать новый receive(), отдающий ранее прочитанные сообщения."""
    iterator = iter(messages)

    async def receive():
        try:
            return next(iterator)
        except StopIteration:
            # Если handler затребует ещё — отдаём empty disconnect.
            return {"type": "http.disconnect"}

    return receive


class IdempotencyMiddleware:
    """ASGI middleware. Должен идти ПОСЛЕ tenant_resolver и ДО хендлеров."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method not in _CACHED_METHODS:
            await self.app(scope, receive, send)
            return

        headers = _decode_headers(scope.get("headers", []))
        idem_key = headers.get("idempotency-key")
        if not idem_key:
            # Клиент не запросил idempotency — стандартный путь без кэша.
            await self.app(scope, receive, send)
            return

        if not _KEY_RE.match(idem_key):
            # Защита от инъекции — отбрасываем не-slug ключи.
            await _send_json(send, 400, {
                "error": {
                    "code": "INVALID_IDEMPOTENCY_KEY",
                    "message": "Idempotency-Key must be 8-128 chars [A-Za-z0-9_-]",
                },
            })
            return

        client = await _get_redis_client()
        if client is None:
            # Fail-open: без Redis просто пропускаем без кэша.
            await self.app(scope, receive, send)
            return

        tenant = headers.get("x-tenant-id") or headers.get("x-org-id")
        cache_key = _make_redis_key(tenant, idem_key)

        # Читаем тело сейчас, чтобы посчитать fingerprint и потом replay'нуть в handler.
        body, replay_messages = await _read_body(receive)
        fingerprint = hashlib.sha256(
            (scope.get("path", "") + "|" + method + "|").encode() + body,
        ).hexdigest()

        # Проверяем кэш.
        try:
            cached_raw = await client.get(cache_key)
        except Exception as exc:
            logger.warning("idempotency: get error, fail-open: %s", exc)
            cached_raw = None

        if cached_raw:
            try:
                cached = json.loads(
                    cached_raw.decode("utf-8") if isinstance(cached_raw, bytes) else cached_raw,
                )
            except Exception:
                cached = None
            if cached:
                if cached.get("fingerprint") != fingerprint:
                    # Тот же ключ, другое тело — клиент-баг.
                    await _send_json(send, 409, {
                        "error": {
                            "code": "IDEMPOTENCY_CONFLICT",
                            "message": (
                                "Idempotency-Key reused with a different request body. "
                                "Use a fresh key for a new operation."
                            ),
                        },
                    })
                    return
                # Match — отдаём закешированный response.
                logger.info("idempotency hit: %s", cache_key)
                await _replay_response(send, cached)
                return

        # Кэш-мисс. Запускаем handler с replay'нутым receive и перехватываем send.
        captured: dict = {"start": None, "body_chunks": []}

        async def capturing_send(msg):
            if msg["type"] == "http.response.start":
                captured["start"] = msg
            elif msg["type"] == "http.response.body":
                captured["body_chunks"].append(msg.get("body", b"") or b"")
            await send(msg)

        await self.app(scope, _replay_receive_factory(replay_messages), capturing_send)

        # Сохраняем в Redis только успешные ответы (2xx), чтобы клиент мог
        # ретрить неудачи с тем же ключом и получить свежую попытку.
        start_msg = captured["start"]
        if not start_msg:
            return
        status = int(start_msg.get("status", 0))
        if not (200 <= status < 300):
            return

        try:
            response_body = b"".join(captured["body_chunks"])
            cache_payload = {
                "fingerprint": fingerprint,
                "status": status,
                "headers": [
                    [k.decode("latin-1"), v.decode("latin-1")]
                    for k, v in start_msg.get("headers", [])
                ],
                "body_b64": _b64encode(response_body),
            }
            await client.set(
                cache_key,
                json.dumps(cache_payload, ensure_ascii=False),
                ex=_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("idempotency: cache write error: %s", exc)


def _b64encode(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode("ascii")


def _b64decode(s: str) -> bytes:
    import base64
    return base64.b64decode(s.encode("ascii"))


async def _replay_response(send, cached: dict) -> None:
    headers = [
        (k.encode("latin-1"), v.encode("latin-1"))
        for k, v in cached.get("headers", [])
    ]
    # Метим повторное обслуживание явным заголовком — клиенту полезно.
    headers.append((b"x-idempotent-replay", b"true"))
    await send({
        "type": "http.response.start",
        "status": int(cached["status"]),
        "headers": headers,
    })
    await send({
        "type": "http.response.body",
        "body": _b64decode(cached.get("body_b64", "")),
        "more_body": False,
    })


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
