"""Diagnostic outermost middleware: логирует path+method+полную цепочку
исключений (`__cause__`/`__context__`) при сбоях.

Назначение — поймать причину `LitestarException("Exception caught after
response started")`. Litestar оборачивает оригинальное исключение в
LitestarException через `raise ... from e`, и в дефолтном trace остаётся
только обёртка. Этот middleware стоит САМЫМ ПЕРВЫМ во внешнем периметре
и пишет в лог реальный `e` с trace до того, как обёртка дойдёт до
granian'а.

Запускается дёшево (try/except вокруг await), без захвата тел и
сериализации. В production можно отключить через APP_ENV=production.
"""
from __future__ import annotations

import logging
import os
import traceback

logger = logging.getLogger(__name__)

_DEV_ONLY = os.getenv("APP_ENV", "development") != "production"


class ExceptionLoggerMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            # Лог только в dev — в проде это шумит и может утечь данные.
            if _DEV_ONLY:
                path = scope.get("path", "?")
                method = scope.get("method", "?")
                cause = exc.__cause__ or exc.__context__
                logger.error(
                    "ExceptionLogger %s %s: %s: %s",
                    method, path, type(exc).__name__, exc,
                )
                if cause is not None:
                    logger.error(
                        "  caused by %s: %s\n%s",
                        type(cause).__name__,
                        cause,
                        "".join(
                            traceback.format_exception(
                                type(cause), cause, cause.__traceback__,
                            )
                        ).rstrip(),
                    )
            raise
