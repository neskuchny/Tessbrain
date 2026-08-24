"""Unit-тесты для record_* helper'ов в core.observability.metrics (W35).

Цель — убедиться что:
- helpers без падений работают и при наличии prometheus_client, и при
  его отсутствии (noop fallback);
- counters реально инкрементятся;
- histograms принимают наблюдения;
- render_metrics возвращает что-то sensible.
"""
from __future__ import annotations

from backend.core.observability import metrics as m

# === Smoke / noop fallback ============================================

def test_render_metrics_returns_bytes_and_content_type() -> None:
    body, ctype = m.render_metrics()
    assert isinstance(body, (bytes, bytearray))
    assert isinstance(ctype, str)
    assert ctype  # non-empty


def test_record_validator_run_does_not_raise() -> None:
    m.record_validator_run(decision="accept", duration_s=1.5)
    m.record_validator_run(decision="reject", duration_s=0.0)
    m.record_validator_run(decision="needs_review", duration_s=-0.5)  # clamp


def test_record_tz_generation_does_not_raise() -> None:
    m.record_tz_generation(task_type="landing", outcome="success", duration_s=12.0)
    m.record_tz_generation(task_type="api", outcome="failure", duration_s=2.5)


def test_record_executor_event_does_not_raise() -> None:
    m.record_executor_event(backend="openhands", event="started")
    m.record_executor_event(
        backend="openhands", event="completed", duration_s=120, outcome="success",
    )
    # no duration → пишется только counter
    m.record_executor_event(backend="noop", event="cancelled")


def test_record_messenger_event_does_not_raise() -> None:
    m.record_messenger_event(platform="telegram", event="inbound")
    m.record_messenger_event(platform="slack", event="onboarding")


def test_record_messenger_chat_does_not_raise() -> None:
    m.record_messenger_chat(platform="telegram", outcome="success", duration_s=2.5)
    m.record_messenger_chat(platform="telegram", outcome="failure", duration_s=0.1)


def test_record_share_event_does_not_raise() -> None:
    m.record_share_event("created")
    m.record_share_event("revoked")


def test_record_share_view_does_not_raise() -> None:
    m.record_share_view("landing_open")
    m.record_share_view("scope_violation")


def test_record_retention_cleanup_zero_is_noop() -> None:
    m.record_retention_cleanup("validation_results", 0)


def test_record_retention_cleanup_positive() -> None:
    m.record_retention_cleanup("validation_results", 42)


# === Counter increments visible in render output (если PG client есть) ===

def _has_prom_client() -> bool:
    try:
        import prometheus_client
        return True
    except ImportError:
        return False


def test_counter_increments_visible_in_render() -> None:
    if not _has_prom_client():
        # Без prometheus_client render возвращает "not installed" comment.
        body, _ = m.render_metrics()
        assert b"not installed" in body
        return
    m.record_validator_run(decision="accept", duration_s=1.0)
    m.record_validator_run(decision="accept", duration_s=2.0)
    m.record_share_event("created")
    body, _ = m.render_metrics()
    text = body.decode("utf-8")
    assert "tessent_validator_runs_total" in text
    assert "tessent_share_grants_total" in text


def test_render_includes_all_new_metric_names() -> None:
    if not _has_prom_client():
        return
    body, _ = m.render_metrics()
    text = body.decode("utf-8")
    expected = [
        "tessent_validator_runs_total",
        "tessent_validator_run_duration_seconds",
        "tessent_validator_quota_blocks_total",
        "tessent_tz_generations_total",
        "tessent_executor_tasks_total",
        "tessent_messenger_events_total",
        "tessent_messenger_chat_duration_seconds",
        "tessent_share_grants_total",
        "tessent_share_views_total",
        "tessent_retention_rows_deleted_total",
    ]
    for name in expected:
        assert name in text, f"missing {name} in /metrics output"
