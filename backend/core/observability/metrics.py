"""Prometheus metrics (W13 production observability).

Экспортирует counters/histograms по основным операциям системы:
- HTTP requests (status, method, path) — Counter + Histogram(latency)
- LLM calls (provider, model, status) — Counter + Histogram(tokens)
- LLM cost — Counter (cumulative USD)
- Audit events — Counter (action, tenant_id)
- Cache hits — Counter (cache_name, hit/miss)
- Quota events — Counter (scope, decision)

Дизайн:
- Lazy import `prometheus_client` — модуль не падает на import если
  пакет не установлен (для dev/test окружений).
- Глобальный registry — стандартный, чтобы `/metrics` сам всё нашёл.
- Метки минимальные — high-cardinality labels (user_id, request_id) НЕ
  идут в Prometheus, они только в structlog. Tenant — допустимо
  потому что tenant'ов гораздо меньше юзеров.
- Безопасно вызывать helper'ы из любого места — они no-op если
  prometheus_client не установлен.

Использование:
    from backend.core.observability import metrics
    metrics.http_requests_total.labels(method="GET", path="/foo", status="200").inc()
    metrics.llm_calls_total.labels(provider="local", model="qwen", status="success").inc()
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _NoopMetric:
    """Заглушка для prometheus_client когда он не установлен.

    Поддерживает .labels(), .inc(), .observe(), .set() — все no-op.
    """

    def labels(self, **_: Any) -> "_NoopMetric":
        return self

    def inc(self, *_: Any, **__: Any) -> None:
        pass

    def observe(self, *_: Any, **__: Any) -> None:
        pass

    def set(self, *_: Any, **__: Any) -> None:
        pass


_NOOP = _NoopMetric()


def _make_counter(name: str, doc: str, labels: list[str]):
    try:
        from prometheus_client import REGISTRY, Counter
        try:
            return Counter(name, doc, labels)
        except ValueError:
            # Duplicated timeseries — модуль re-imported (бывает в тестах
            # с importlib.util). Возвращаем существующий collector.
            return _existing_collector(REGISTRY, name) or _NOOP
    except ImportError:
        return _NOOP


def _make_histogram(name: str, doc: str, labels: list[str], buckets: tuple = ()):
    try:
        from prometheus_client import REGISTRY, Histogram
        try:
            if buckets:
                return Histogram(name, doc, labels, buckets=buckets)
            return Histogram(name, doc, labels)
        except ValueError:
            return _existing_collector(REGISTRY, name) or _NOOP
    except ImportError:
        return _NOOP


def _make_gauge(name: str, doc: str, labels: list[str]):
    try:
        from prometheus_client import REGISTRY, Gauge
        try:
            return Gauge(name, doc, labels)
        except ValueError:
            return _existing_collector(REGISTRY, name) or _NOOP
    except ImportError:
        return _NOOP


def _existing_collector(registry, metric_name: str):
    """Найти ранее зарегистрированный collector с тем же именем.

    prometheus_client хранит mapping name → collector в `_names_to_collectors`.
    """
    names_map = getattr(registry, "_names_to_collectors", {})
    return names_map.get(metric_name) or names_map.get(metric_name + "_total")


# === HTTP =================================================================
http_requests_total = _make_counter(
    "tessent_http_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status"],
)

http_request_duration_seconds = _make_histogram(
    "tessent_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path_template"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# === LLM ==================================================================
llm_calls_total = _make_counter(
    "tessent_llm_calls_total",
    "Total LLM API calls",
    ["provider", "model", "status"],
)

llm_input_tokens_total = _make_counter(
    "tessent_llm_input_tokens_total",
    "Total LLM input tokens consumed",
    ["provider", "model", "tenant_id"],
)

llm_output_tokens_total = _make_counter(
    "tessent_llm_output_tokens_total",
    "Total LLM output tokens generated",
    ["provider", "model", "tenant_id"],
)

llm_cost_usd_total = _make_counter(
    "tessent_llm_cost_usd_total",
    "Cumulative LLM cost in USD",
    ["provider", "model", "tenant_id"],
)

llm_call_duration_seconds = _make_histogram(
    "tessent_llm_call_duration_seconds",
    "LLM call latency in seconds",
    ["provider", "model"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# === Cache ================================================================
cache_events_total = _make_counter(
    "tessent_cache_events_total",
    "Cache hit/miss events",
    ["cache", "result"],
)

# === Quota ================================================================
quota_decisions_total = _make_counter(
    "tessent_quota_decisions_total",
    "Quota allow/deny decisions",
    ["scope", "decision"],
)

# === Audit ================================================================
audit_events_total = _make_counter(
    "tessent_audit_events_total",
    "Audit events emitted",
    ["action", "tenant_id"],
)

# === DLP ==================================================================
dlp_redactions_total = _make_counter(
    "tessent_dlp_redactions_total",
    "DLP filter redactions",
    ["label"],
)

# === Background jobs ======================================================
nightly_job_duration_seconds = _make_histogram(
    "tessent_nightly_job_duration_seconds",
    "Nightly consolidation job duration",
    ["status"],
    buckets=(60, 300, 600, 1800, 3600, 7200, 14400),
)

active_users_gauge = _make_gauge(
    "tessent_active_users",
    "Users with activity in last 5 minutes",
    ["tenant_id"],
)


# === Validator (W21+W23) ==================================================
validator_runs_total = _make_counter(
    "tessent_validator_runs_total",
    "Validator pipeline runs by decision",
    ["decision"],
)

validator_run_duration_seconds = _make_histogram(
    "tessent_validator_run_duration_seconds",
    "Validator pipeline duration",
    ["decision"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

validator_quota_blocks_total = _make_counter(
    "tessent_validator_quota_blocks_total",
    "Validator runs blocked by quota",
    ["scope"],
)

# === TZ generation (W18+W25) =============================================
tz_generations_total = _make_counter(
    "tessent_tz_generations_total",
    "TZ generation runs by task_type and outcome",
    ["task_type", "outcome"],
)

tz_generation_duration_seconds = _make_histogram(
    "tessent_tz_generation_duration_seconds",
    "TZ generation latency",
    ["task_type"],
    buckets=(1, 5, 10, 30, 60, 120, 300),
)

# === Executor (W19+W20) ==================================================
executor_tasks_total = _make_counter(
    "tessent_executor_tasks_total",
    "Executor task lifecycle events",
    ["backend", "event"],     # event: started/completed/failed/cancelled
)

executor_task_duration_seconds = _make_histogram(
    "tessent_executor_task_duration_seconds",
    "Executor task duration",
    ["backend", "outcome"],
    buckets=(5, 30, 60, 300, 900, 1800, 3600),
)

# === Messengers (W30+W34) ================================================
messenger_events_total = _make_counter(
    "tessent_messenger_events_total",
    "Messenger inbound/outbound events",
    ["platform", "event"],    # event: inbound/onboarding/reply_sent/reply_failed
)

messenger_chat_duration_seconds = _make_histogram(
    "tessent_messenger_chat_duration_seconds",
    "Messenger chat handler latency",
    ["platform", "outcome"],
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Объём памяти компании (узлы/связи/байты). Обновляется ночным снимком
# memory_footprint: долгоживущего агента разоряет не размер, а наклон
# роста — без этих чисел политика забывания невозможна в принципе.
memory_nodes_gauge = _make_gauge(
    "tessent_memory_nodes",
    "Company memory graph nodes",
    ["tenant_id"],
)
memory_edges_gauge = _make_gauge(
    "tessent_memory_edges",
    "Company memory graph edges",
    ["tenant_id"],
)
memory_bytes_gauge = _make_gauge(
    "tessent_memory_bytes",
    "Company memory total bytes on disk (graph + versions + vectors)",
    ["tenant_id"],
)

messenger_links_active = _make_gauge(
    "tessent_messenger_links_active",
    "Currently active messenger links",
    ["platform"],
)

# === Sharing (W31) =======================================================
share_grants_total = _make_counter(
    "tessent_share_grants_total",
    "Share bundle lifecycle events",
    ["event"],                 # created/revoked/expired
)

share_views_total = _make_counter(
    "tessent_share_views_total",
    "Share landing/manifest/resource views",
    ["action"],                # landing_open/email_verified/email_mismatch/
                               # resource_view/scope_violation
)

# === Retention (W27) =====================================================
retention_rows_deleted_total = _make_counter(
    "tessent_retention_rows_deleted_total",
    "Rows removed by retention cleanup",
    ["target"],                # validation_results / executor_handles
)


# === Helper functions =====================================================
# Удобные wrapper'ы — caller'ам не надо импортировать сами collectors.

def record_validator_run(*, decision: str, duration_s: float) -> None:
    validator_runs_total.labels(decision=decision).inc()
    validator_run_duration_seconds.labels(decision=decision).observe(
        max(0.0, float(duration_s))
    )


def record_tz_generation(
    *,
    task_type: str,
    outcome: str,
    duration_s: float,
) -> None:
    tz_generations_total.labels(task_type=task_type, outcome=outcome).inc()
    tz_generation_duration_seconds.labels(task_type=task_type).observe(
        max(0.0, float(duration_s))
    )


def record_executor_event(
    *,
    backend: str,
    event: str,
    duration_s: float | None = None,
    outcome: str | None = None,
) -> None:
    executor_tasks_total.labels(backend=backend, event=event).inc()
    if duration_s is not None and outcome is not None:
        executor_task_duration_seconds.labels(
            backend=backend, outcome=outcome,
        ).observe(max(0.0, float(duration_s)))


def record_messenger_event(*, platform: str, event: str) -> None:
    messenger_events_total.labels(platform=platform, event=event).inc()


def record_messenger_chat(
    *,
    platform: str,
    outcome: str,
    duration_s: float,
) -> None:
    messenger_chat_duration_seconds.labels(
        platform=platform, outcome=outcome,
    ).observe(max(0.0, float(duration_s)))


def record_share_event(event: str) -> None:
    share_grants_total.labels(event=event).inc()


def record_share_view(action: str) -> None:
    share_views_total.labels(action=action).inc()


def record_retention_cleanup(target: str, rows: int) -> None:
    if rows > 0:
        retention_rows_deleted_total.labels(target=target).inc(int(rows))


# === HTTP endpoint helper =================================================

def render_metrics() -> tuple[bytes, str]:
    """Сериализовать все registered metrics в Prometheus text format.

    Возвращает (body, content_type). Используется
    `GET /api/v1/health/metrics`.
    """
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        return generate_latest(), CONTENT_TYPE_LATEST
    except ImportError:
        # Если пакета нет — отдаём пустой body, чтобы Prometheus scraper
        # не падал, а просто видел отсутствие метрик.
        return b"# prometheus_client not installed\n", "text/plain; charset=utf-8"


__all__ = [
    "active_users_gauge",
    "audit_events_total",
    "cache_events_total",
    "dlp_redactions_total",
    "executor_task_duration_seconds",
    "executor_tasks_total",
    "http_request_duration_seconds",
    "http_requests_total",
    "llm_call_duration_seconds",
    "llm_calls_total",
    "llm_cost_usd_total",
    "llm_input_tokens_total",
    "llm_output_tokens_total",
    "messenger_chat_duration_seconds",
    "messenger_events_total",
    "messenger_links_active",
    "nightly_job_duration_seconds",
    "quota_decisions_total",
    "record_executor_event",
    "record_messenger_chat",
    "record_messenger_event",
    "record_retention_cleanup",
    "record_share_event",
    "record_share_view",
    "record_tz_generation",
    "record_validator_run",
    "render_metrics",
    "retention_rows_deleted_total",
    "share_grants_total",
    "share_views_total",
    "tz_generation_duration_seconds",
    "tz_generations_total",
    "validator_quota_blocks_total",
    "validator_run_duration_seconds",
    "validator_runs_total",
]
