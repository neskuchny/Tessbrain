"""Unit-тесты для core.observability.metrics (W13).

Импортируем модуль напрямую — `backend.core.observability.__init__`
лёгкий (только logging + tenant_context). Lazy-import prometheus_client
внутри metrics обеспечивает graceful fallback на _NoopMetric.
"""
from __future__ import annotations

from backend.core.observability import metrics


def test_metrics_objects_exist() -> None:
    """Все основные метрики declared (Counter/Histogram/Gauge или _NoopMetric)."""
    assert metrics.http_requests_total is not None
    assert metrics.llm_calls_total is not None
    assert metrics.llm_cost_usd_total is not None
    assert metrics.audit_events_total is not None
    assert metrics.dlp_redactions_total is not None
    assert metrics.nightly_job_duration_seconds is not None


def test_labels_chain_does_not_raise() -> None:
    """Helper API одинаково работает на real Counter и _NoopMetric."""
    m = metrics.http_requests_total.labels(
        method="GET", path_template="/foo", status="200",
    )
    m.inc()


def test_histogram_observe_does_not_raise() -> None:
    h = metrics.http_request_duration_seconds.labels(
        method="GET", path_template="/foo",
    )
    h.observe(0.123)


def test_gauge_set_does_not_raise() -> None:
    g = metrics.active_users_gauge.labels(tenant_id="t-1")
    g.set(42)


def test_render_metrics_returns_bytes_and_content_type() -> None:
    body, ctype = metrics.render_metrics()
    assert isinstance(body, (bytes, bytearray))
    assert isinstance(ctype, str)
    assert "text" in ctype.lower() or "openmetrics" in ctype.lower()


def test_noop_metric_is_chainable() -> None:
    """_NoopMetric.labels().inc() — не должен ломать API."""
    n = metrics._NoopMetric()
    n.labels(a=1).labels(b=2).inc()
    n.labels().observe(0.5)
    n.labels().set(100)
