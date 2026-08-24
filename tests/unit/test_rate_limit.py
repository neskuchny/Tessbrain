"""Unit-тесты для api.middleware.rate_limit (W2 phase 4).

Без живого Redis проверяем: exempt-paths, fail-open, IP-extraction.
"""
from __future__ import annotations

import asyncio

from backend.api.middleware.rate_limit import (
    EXEMPT_PATHS,
    RateLimitMiddleware,
    _client_ip,
    _extract_tenant_id,
)


def _decode_headers(scope_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope_headers}


def test_exempt_paths_contain_health_probes() -> None:
    assert "/healthz" in EXEMPT_PATHS
    assert "/livez" in EXEMPT_PATHS
    assert "/readyz" in EXEMPT_PATHS


def test_extract_tenant_from_header() -> None:
    scope = {"headers": [(b"x-tenant-id", b"tenant-A")]}
    assert _extract_tenant_id(scope) == "tenant-A"


def test_extract_tenant_returns_none_when_absent() -> None:
    scope = {"headers": []}
    assert _extract_tenant_id(scope) is None


def test_client_ip_uses_xff_first() -> None:
    scope = {
        "headers": [(b"x-forwarded-for", b"203.0.113.1, 10.0.0.1")],
        "client": ("10.0.0.5", 4000),
    }
    assert _client_ip(scope) == "203.0.113.1"


def test_client_ip_falls_back_to_scope_client() -> None:
    scope = {"headers": [], "client": ("198.51.100.7", 1234)}
    assert _client_ip(scope) == "198.51.100.7"


def test_client_ip_returns_unknown_when_neither_present() -> None:
    scope = {"headers": []}
    assert _client_ip(scope) == "unknown"


# === middleware exempt-path bypass ===

async def _run(scope, mw: RateLimitMiddleware) -> list[dict]:
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw.app = app

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


def test_exempt_path_bypasses_rate_limit() -> None:
    """/healthz должен пройти без обращения к Redis (которого нет в тесте)."""
    mw = RateLimitMiddleware(
        app=None, enabled=True, user_per_minute=1, anon_per_minute=1, tenant_per_minute=1,
    )
    scope = {"type": "http", "path": "/healthz", "headers": [], "client": ("1.2.3.4", 0)}
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200


def test_disabled_middleware_passes_through() -> None:
    """enabled=False → middleware не делает никаких проверок."""
    mw = RateLimitMiddleware(
        app=None, enabled=False, user_per_minute=1, anon_per_minute=1, tenant_per_minute=1,
    )
    scope = {"type": "http", "path": "/api/v1/foo", "headers": [], "client": ("1.2.3.4", 0)}
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200


def test_websocket_scope_skipped() -> None:
    mw = RateLimitMiddleware(
        app=None, enabled=True, user_per_minute=1, anon_per_minute=1, tenant_per_minute=1,
    )
    scope = {"type": "websocket"}
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200


def test_anonymous_request_fails_open_without_redis() -> None:
    """Без Redis _RedisCounter возвращает (True, 0, 0) → запрос проходит.
    Это специально: лучше пропустить, чем уронить API из-за infra."""
    mw = RateLimitMiddleware(
        app=None, enabled=True, user_per_minute=1, anon_per_minute=1, tenant_per_minute=1,
    )
    scope = {"type": "http", "path": "/api/v1/foo", "headers": [], "client": ("1.2.3.4", 0)}
    sent = asyncio.run(_run(scope, mw))
    assert sent[0]["status"] == 200
