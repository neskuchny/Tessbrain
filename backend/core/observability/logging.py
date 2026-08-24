"""Structured logging configuration based on structlog + stdlib bridge.

Идея:
- В production пишем JSON-строки (можно собирать в Loki/ELK без парсинга).
- В dev пишем читабельный console-output с цветами.
- structlog оборачивает стандартный `logging` — поэтому существующий код,
  использующий `logging.getLogger(__name__)`, продолжает работать.
- Любой модуль может позвать `get_logger(__name__)` и получить structlog logger
  с тем же бэкендом.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_CONFIGURED = False


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
) -> None:
    """Сконфигурировать stdlib logging + structlog.

    Идемпотентно: повторные вызовы не создают дубликатов хендлеров.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)

    # Stdlib root: единый stdout-handler, формат подменяется ниже structlog'ом.
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    root.addHandler(handler)
    root.setLevel(log_level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if fmt == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    # structlog → stdlib: один формат для обоих миров.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    handler.setFormatter(formatter)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> Any:
    """Получить structlog-logger. Если конфигурация не вызвана — конфигурируем дефолтом."""
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)


def bind_request_context(**kwargs: Any) -> None:
    """Привязать произвольный контекст к текущему запросу (request_id, user_id, tenant_id…)."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
