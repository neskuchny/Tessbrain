"""Unit-тесты для api.middleware.idempotency (W6 prod-hardening).

Без живого Redis проверяем:
- Не-кэшируемые методы (GET) проходят как есть.
- Запросы без Idempotency-Key проходят как есть.
- Невалидный ключ → 400.
- Fail-open при недоступности Redis.
"""
from __future__ import annotations

import asyncio
import json

from backend.api.middleware.idempotency import (
    _KEY_RE,
    IdempotencyMiddleware,
    _make_redis_key,
)


def test_key_regex_accepts_uuid_and_slug() -> None:
    assert _KEY_RE.match("00000000-0000-0000-0000-000000000000")
    assert _KEY_RE.match("meeting-abc-123")
    assert _KEY_RE.match("a" * 8)        # минимум 8 символов


def test_key_regex_rejects_short_and_unsafe() -> None:
    assert not _KEY_RE.match("abc")              # < 8 символов
    assert not _KEY_RE.match("a" * 129)          # > 128 символов
    assert not _KEY_RE.match("key with spaces")
    assert not _KEY_RE.match("key/slash")
    assert not _KEY_RE.match("key'inject")
    assert not _KEY_RE.match("key.dot")          # точки запрещены


def test_redis_key_includes_tenant() -> None:
    assert _make_redis_key("tenant-A", "abc12345") == "idem:tenant-A:abc12345"
    assert _make_redis_key(None, "abc12345") == "idem:anon:abc12345"


# === middleware behavior ===

async def _run(scope, mw: IdempotencyMiddleware, body: bytes = b"") -> list[dict]:
    sent: list[dict] = []
    received: list[dict] = []

    async def app(scope, receive, send):
        # Прочитываем тело и пускаем 200 OK.
        while True:
            msg = await receive()
            received.append(msg)
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body",
                    "body": json.dumps({"ok": True}).encode(),
                    "more_body": False})

    mw.app = app

    body_consumed = {"done": False}

    async def receive():
        if body_consumed["done"]:
            return {"type": "http.disconnect"}
        body_consumed["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


def test_get_request_passes_through() -> None:
    """GET не кэшируется даже с Idempotency-Key — GET и так должен быть идемпотентным."""
    mw = IdempotencyMiddleware(app=None)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/foo",
        "headers": [(b"idempotency-key", b"abc12345")],
    }
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200


def test_post_without_idempotency_key_passes_through() -> None:
    mw = IdempotencyMiddleware(app=None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/foo",
        "headers": [],
    }
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200


def test_post_with_invalid_key_rejected_with_400() -> None:
    mw = IdempotencyMiddleware(app=None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/foo",
        "headers": [(b"idempotency-key", b"too short")],  # < 8 + space
    }
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 400
    body = sent[1]["body"].decode("utf-8")
    assert "INVALID_IDEMPOTENCY_KEY" in body


def test_post_with_valid_key_fails_open_without_redis() -> None:
    """Без Redis запрос проходит к handler'у без кэширования."""
    mw = IdempotencyMiddleware(app=None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/foo",
        "headers": [(b"idempotency-key", b"meeting-abc-123-456")],
    }
    sent = asyncio.run(_run(scope, mw, body=b'{"x":1}'))
    assert sent[0]["status"] == 200


def test_websocket_scope_skipped() -> None:
    mw = IdempotencyMiddleware(app=None)
    scope = {"type": "websocket"}
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200
