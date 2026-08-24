"""Unit-тесты для core.observability.tracing (W13).

Проверяем graceful no-op поведение когда:
- OTEL_EXPORTER_OTLP_ENDPOINT не задан → setup_tracing → False;
- opentelemetry-sdk не установлен → setup_tracing → False (warn);
- get_tracer возвращает _NoopTracer когда OTel недоступен.

Реальный init-тест требует поднятого OTLP collector — это integration scope.
"""
from __future__ import annotations

import contextlib

import pytest
from backend.core.observability import tracing


def test_setup_skips_when_endpoint_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # reset _initialized чтобы не было side-эффектов от других тестов.
    monkeypatch.setattr(tracing, "_initialized", False)
    assert tracing.setup_tracing() is False


def test_setup_skips_on_blank_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
    monkeypatch.setattr(tracing, "_initialized", False)
    assert tracing.setup_tracing() is False


def test_setup_returns_true_if_already_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent: повторный вызов не делает дубль-init."""
    monkeypatch.setattr(tracing, "_initialized", True)
    assert tracing.setup_tracing() is True


def test_get_tracer_returns_object() -> None:
    """get_tracer всегда что-то возвращает — real OTel tracer или _NoopTracer."""
    t = tracing.get_tracer("backend.tests")
    assert t is not None


def test_noop_tracer_context_manager() -> None:
    """_NoopTracer.start_as_current_span может работать as context manager."""
    nt = tracing._NoopTracer()
    cm = nt.start_as_current_span("test_span")
    # Проверяем что это нечто context-manager-shaped.
    assert hasattr(cm, "__enter__") or isinstance(cm, contextlib.AbstractContextManager)
    with cm:
        pass  # должен not raise


def test_try_instrument_helpers_swallow_import_errors() -> None:
    """Каждый _try_instrument_X должен gracefully no-op без пакета."""
    tracing._try_instrument_asgi()
    tracing._try_instrument_httpx()
    tracing._try_instrument_asyncpg()
    tracing._try_instrument_redis()
