"""Unit-тесты для api.middleware.tenant_resolver (W4 phase 7a).

Проверяет извлечение tenant_id из headers/JWT и поведение strict-режима.
Без живого Litestar — гоняем middleware напрямую как ASGI-callable.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from backend.api.middleware.tenant_resolver import (
    TenantResolverMiddleware,
    _extract_from_header,
    _extract_from_jwt,
    _is_exempt,
)
from backend.core.observability.tenant_context import get_current_tenant


def _make_jwt_unsigned(payload: dict) -> str:
    """JWT без подписи (header.payload.empty) — то, что декодит resolver."""
    import base64

    def b64(v: bytes) -> str:
        return base64.urlsafe_b64encode(v).rstrip(b"=").decode("ascii")

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    return f"{header}.{body}."


# === header extractor ===

def test_extract_from_header_x_tenant_id() -> None:
    headers = {"x-tenant-id": "tenant-A"}
    assert _extract_from_header(headers) == "tenant-A"


def test_extract_from_header_x_org_id_fallback() -> None:
    headers = {"x-org-id": "org-XYZ"}
    assert _extract_from_header(headers) == "org-XYZ"


def test_extract_from_header_prefers_tenant_over_org() -> None:
    headers = {"x-tenant-id": "tenant-A", "x-org-id": "org-XYZ"}
    assert _extract_from_header(headers) == "tenant-A"


def test_extract_from_header_returns_none_when_absent() -> None:
    assert _extract_from_header({}) is None


# === JWT extractor ===
#
# Эти тесты проверяют ВЫБОР claim-поля (tenant_id → org_id → app_metadata),
# а не политику подписи. issue #107 включил strict-режим по умолчанию
# (enable_strict_chat_auth=True), который отклоняет неподписанные токены ДО
# извлечения claims. Чтобы тестировать именно логику выбора поля, гоняем их
# в compat-режиме (strict OFF). Политику strict проверяет
# test_middleware_strict_rejects_without_tenant ниже.

@pytest.fixture
def _compat_auth(monkeypatch):
    import backend.core.auth.service_token as st
    monkeypatch.setattr(st, "_strict_chat_auth", lambda: False)
    yield


def test_jwt_extractor_picks_tenant_id_claim(_compat_auth) -> None:
    token = _make_jwt_unsigned({"sub": "u1", "tenant_id": "tA"})
    headers = {"authorization": f"Bearer {token}"}
    assert _extract_from_jwt(headers) == "tA"


def test_jwt_extractor_falls_back_to_org_id(_compat_auth) -> None:
    token = _make_jwt_unsigned({"sub": "u1", "org_id": "o-42"})
    headers = {"authorization": f"Bearer {token}"}
    assert _extract_from_jwt(headers) == "o-42"


def test_jwt_extractor_reads_supabase_app_metadata(_compat_auth) -> None:
    token = _make_jwt_unsigned({"sub": "u1", "app_metadata": {"tenant_id": "tB"}})
    headers = {"authorization": f"Bearer {token}"}
    assert _extract_from_jwt(headers) == "tB"


def test_jwt_extractor_returns_none_without_bearer() -> None:
    assert _extract_from_jwt({"authorization": "Basic abc"}) is None
    assert _extract_from_jwt({}) is None


def test_jwt_extractor_returns_none_for_garbage_token() -> None:
    headers = {"authorization": "Bearer not-a-valid-jwt"}
    assert _extract_from_jwt(headers) is None


# === exempt-paths ===

def test_exempt_paths() -> None:
    assert _is_exempt("/healthz") is True
    assert _is_exempt("/livez") is True
    assert _is_exempt("/readyz") is True
    assert _is_exempt("/schema/openapi.json") is True
    assert _is_exempt("/api/v1/auth/login") is True
    # Всё остальное — не exempt.
    assert _is_exempt("/api/v1/projects") is False
    assert _is_exempt("/api/v1/sima/blocks") is False


# === middleware end-to-end ===

async def _run_middleware(scope, mw: TenantResolverMiddleware) -> tuple[list[dict], str | None]:
    captured_tenant: list[str | None] = [None]
    sent: list[dict] = []

    async def app(scope, receive, send):
        captured_tenant[0] = get_current_tenant()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw.app = app

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent, captured_tenant[0]


def test_middleware_sets_tenant_from_header() -> None:
    mw = TenantResolverMiddleware(app=None, strict=False)
    scope = {
        "type": "http",
        "path": "/api/v1/foo",
        "headers": [(b"x-tenant-id", b"tenant-X")],
    }
    sent, tenant = asyncio.run(_run_middleware(scope, mw))
    assert tenant == "tenant-X"
    assert sent[0]["status"] == 200
    # После выхода из middleware contextvar сброшен.
    assert get_current_tenant() is None


def test_middleware_passes_through_when_no_tenant_and_not_strict() -> None:
    mw = TenantResolverMiddleware(app=None, strict=False)
    scope = {"type": "http", "path": "/api/v1/foo", "headers": []}
    sent, tenant = asyncio.run(_run_middleware(scope, mw))
    assert tenant is None
    assert sent[0]["status"] == 200


def test_middleware_strict_rejects_without_tenant() -> None:
    mw = TenantResolverMiddleware(app=None, strict=True)
    scope = {"type": "http", "path": "/api/v1/foo", "headers": []}
    sent, _ = asyncio.run(_run_middleware(scope, mw))
    assert sent[0]["status"] == 400
    body = sent[1]["body"].decode("utf-8")
    assert "TENANT_REQUIRED" in body


def test_middleware_strict_allows_exempt_paths_without_tenant() -> None:
    mw = TenantResolverMiddleware(app=None, strict=True)
    scope = {"type": "http", "path": "/healthz", "headers": []}
    sent, _ = asyncio.run(_run_middleware(scope, mw))
    assert sent[0]["status"] == 200


def test_middleware_skips_websocket_scope() -> None:
    """non-http scope (websocket, lifespan) пропускаются как есть."""
    mw = TenantResolverMiddleware(app=None, strict=True)
    sent: list[dict] = []

    async def app(scope, receive, send):
        sent.append({"called": True})

    mw.app = app
    asyncio.run(mw({"type": "lifespan"}, lambda: None, lambda x: None))
    assert sent == [{"called": True}]
