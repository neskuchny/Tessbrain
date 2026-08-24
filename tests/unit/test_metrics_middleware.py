"""Unit-тесты для backend.api.middleware.metrics_mw (W15)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from backend.api.middleware.metrics_mw import (
    _EXEMPT_PATHS,
    MetricsMiddleware,
    _path_template,
)


def _run(coro):
    return asyncio.run(coro)


# === path_template helper ================================================

def test_path_template_from_route_handler() -> None:
    """Если route_handler доступен — берём его paths[0]."""
    class Handler:
        paths = ["/api/v1/users/{id:str}"]
    scope = {"route_handler": Handler(), "path": "/api/v1/users/123"}
    assert _path_template(scope) == "/api/v1/users/{id:str}"


def test_path_template_fallback_to_path() -> None:
    scope = {"path": "/api/v1/foo"}
    assert _path_template(scope) == "/api/v1/foo"


def test_path_template_strips_query_string() -> None:
    scope = {"path": "/api/v1/foo?bar=1"}
    assert _path_template(scope) == "/api/v1/foo"


def test_path_template_default_root() -> None:
    """Если path пустой — '/'."""
    assert _path_template({"path": ""}) == "/"


# === MetricsMiddleware ===================================================

class _FakeApp:
    """ASGI app stub: записывает что middleware его вызвал и эмулирует
    response с указанным статусом."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.called = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.called = True
        # Эмулируем минимальный ASGI response.
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _drive(mw: MetricsMiddleware, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Прогнать middleware и собрать send-сообщения."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await mw(scope, receive, send)
    return sent


def test_passes_through_non_http() -> None:
    """ASGI websocket / lifespan — не наша зона."""
    app = _FakeApp()
    mw = MetricsMiddleware(app)
    scope = {"type": "lifespan"}
    _run(_drive(mw, scope))
    assert app.called is True


def test_increments_counter_on_http() -> None:
    app = _FakeApp(status=200)
    mw = MetricsMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/foo"}
    _run(_drive(mw, scope))
    # Не упало, app вызван.
    assert app.called is True


def test_skips_exempt_paths() -> None:
    """/livez и /metrics не должны instrumentировать сами себя."""
    app = _FakeApp(status=200)
    mw = MetricsMiddleware(app)
    for path in _EXEMPT_PATHS:
        scope = {"type": "http", "method": "GET", "path": path}
        _run(_drive(mw, scope))
        assert app.called is True
        app.called = False


def test_records_status_code_from_response() -> None:
    """Middleware должен подхватить статус из http.response.start."""
    app = _FakeApp(status=503)
    mw = MetricsMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/foo"}
    sent = _run(_drive(mw, scope))
    assert sent[0]["status"] == 503


def test_does_not_swallow_app_exceptions() -> None:
    """Если handler упал — middleware не должна спрятать exception."""
    class _BoomApp:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            raise RuntimeError("kaboom")

    mw = MetricsMiddleware(_BoomApp())
    scope = {"type": "http", "method": "POST", "path": "/api/v1/x"}

    async def receive() -> dict[str, Any]:
        return {}

    async def send(_: Any) -> None:
        pass

    with pytest.raises(RuntimeError):
        _run(mw(scope, receive, send))


def test_metrics_failure_does_not_break_request() -> None:
    """Даже если запись метрик упадёт — response должен пройти."""
    from unittest.mock import patch

    app = _FakeApp(status=200)
    mw = MetricsMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/api/v1/foo"}

    with patch("backend.api.middleware.metrics_mw.metrics") as m:
        m.http_requests_total.labels.side_effect = RuntimeError("metric backend down")
        # Не должен поднять exception.
        _run(_drive(mw, scope))
        assert app.called is True
