"""Prometheus metrics middleware (W15 wire-up).

ASGI middleware, который инкрементит:
- `tessent_http_requests_total{method, path_template, status}`
- `tessent_http_request_duration_seconds{method, path_template}`

Дизайн:
- Path template вычисляется через `scope["route_handler"].path` если LiteStar
  установил его (после route resolution); иначе fallback на raw `scope["path"]`.
- Никаких high-cardinality labels: user_id / request_id остаются в structlog.
- Performance: один time.perf_counter() + один send-wrapper. ~1µs на request.

Как подключать (см. backend/api/app.py):
    middleware=[
        DefineMiddleware(MetricsMiddleware),
        ...
    ]
"""
from __future__ import annotations

import time
from typing import Any

from backend.core.observability import metrics

# Пути, которые не имеет смысла инструментировать (сами health/metrics
# создают «шум» в gauges, плюс /metrics scrape должен быть быстрым).
_EXEMPT_PATHS = frozenset({
    "/api/v1/livez",
    "/api/v1/readyz",
    "/api/v1/healthz",
    "/api/v1/health/metrics",
    "/api/v1/ping",
})


def _path_template(scope: dict[str, Any]) -> str:
    """Вернуть стабильный path template для метки.

    Litestar кладёт route_handler в scope после matching; если матчинга
    ещё не было (404), используем raw path с обрезкой query-string.
    """
    handler = scope.get("route_handler")
    if handler is not None:
        # У Litestar handler.paths — это SET[str] (не list!), поэтому
        # индексировать `paths[0]` нельзя — будет «'set' object is not
        # subscriptable», которое всплывает уже ПОСЛЕ старта ответа и
        # роняет каждый запрос. Берём детерминированно через sorted().
        paths = getattr(handler, "paths", None)
        if paths:
            if isinstance(paths, str):
                return paths
            try:
                return sorted(paths)[0]
            except (TypeError, IndexError):
                pass
        single = getattr(handler, "path", "")
        if single:
            return single
    return scope.get("path", "/").split("?", 1)[0] or "/"


class MetricsMiddleware:
    """ASGI middleware, эмитит HTTP-метрики после каждого request.

    Безопасно при отсутствии prometheus_client — `_NoopMetric` no-op.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if path in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        status_code: dict[str, int] = {"v": 0}
        start = time.perf_counter()

        async def _send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_code["v"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration = time.perf_counter() - start
            template = _path_template(scope)
            status = str(status_code["v"] or "0")
            try:
                metrics.http_requests_total.labels(
                    method=method, path_template=template, status=status,
                ).inc()
                metrics.http_request_duration_seconds.labels(
                    method=method, path_template=template,
                ).observe(duration)
            except Exception:
                # Никогда не ломаем response из-за метрик.
                pass


__all__ = ["MetricsMiddleware"]
