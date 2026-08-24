"""OpenTelemetry tracing init (W13).

Опциональный helper для подключения distributed tracing. Если
`opentelemetry-sdk` не установлен — вся функциональность no-op.

Conf:
- OTEL_EXPORTER_OTLP_ENDPOINT (default: empty → не инициализируем)
- OTEL_SERVICE_NAME (default: "tessent_brain")
- OTEL_RESOURCE_ATTRIBUTES (стандартный env, передаётся в Resource)

Что инструментируется:
- LiteStar HTTP server spans (через opentelemetry-instrumentation-asgi)
- httpx client spans (исходящие LLM/storage вызовы)
- asyncpg query spans
- redis команды

Каждое из них требует соответствующий instrumentation-пакет;
функция `setup_tracing()` пытается импортировать каждый и
gracefully пропускает отсутствующие.

Использование:
    from backend.core.observability.tracing import setup_tracing
    setup_tracing()  # один раз при старте процесса
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_initialized = False


def setup_tracing(*, service_name: str = "tessent_brain") -> bool:
    """Инициализировать OTel SDK + OTLP exporter + auto-инструментацию.

    Returns:
        True если tracing включён, False если skip (no endpoint /
        пакет не установлен).
    """
    global _initialized
    if _initialized:
        return True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.debug("OTel disabled: OTEL_EXPORTER_OTLP_ENDPOINT not set")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTel disabled: install `opentelemetry-sdk opentelemetry-exporter-otlp` "
            "to enable tracing"
        )
        return False

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    _try_instrument_asgi()
    _try_instrument_httpx()
    _try_instrument_asyncpg()
    _try_instrument_redis()

    _initialized = True
    logger.info("OTel tracing enabled: endpoint=%s", endpoint)
    return True


def _try_instrument_asgi() -> None:
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
        # Применяется обёрткой ASGI app в backend/api/app.py.
        logger.debug("OTel ASGI instrumentation available")
    except ImportError:
        logger.debug("OTel ASGI: install opentelemetry-instrumentation-asgi to enable")


def _try_instrument_httpx() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        logger.debug("OTel httpx instrumented")
    except ImportError:
        logger.debug("OTel httpx: install opentelemetry-instrumentation-httpx to enable")


def _try_instrument_asyncpg() -> None:
    try:
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
        AsyncPGInstrumentor().instrument()
        logger.debug("OTel asyncpg instrumented")
    except ImportError:
        logger.debug("OTel asyncpg: install opentelemetry-instrumentation-asyncpg to enable")


def _try_instrument_redis() -> None:
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
        logger.debug("OTel redis instrumented")
    except ImportError:
        logger.debug("OTel redis: install opentelemetry-instrumentation-redis to enable")


def get_tracer(name: str) -> Any:
    """Вернуть OTel tracer или no-op заглушку."""
    try:
        from opentelemetry import trace
        return trace.get_tracer(name)
    except ImportError:
        return _NoopTracer()


class _NoopTracer:
    """Заглушка когда OTel не установлен."""

    def start_as_current_span(self, name: str, **kwargs: Any):
        from contextlib import nullcontext
        return nullcontext()


__all__ = ["get_tracer", "setup_tracing"]
