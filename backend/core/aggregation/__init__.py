"""k-anonymous cross-user aggregation (P2 §6 + п.7 360-review).

Один примитив для двух задач Mini Tess:
- manager_signal: эмит руководителю при ≥k разных контрибьюторов;
- review_360: 360-агрегат с подавлением метрик ниже k оценок.

Гарантия приватности: contributor_user_id используется ТОЛЬКО для подсчёта
distinct и НИКОГДА не попадает в выдаваемый/эмитируемый агрегат.
"""
from backend.core.aggregation.core import (
    DEFAULT_K,
    AggregateView,
    aggregate_view,
    contribute,
    distinct_count,
    release_emit,
    try_emit_once,
)
from backend.core.aggregation.manager_signal import contribute_manager_signal
from backend.core.aggregation.review360 import build_360_review, submit_360_feedback

__all__ = [
    "DEFAULT_K",
    "AggregateView",
    "aggregate_view",
    "contribute",
    "distinct_count",
    "release_emit",
    "try_emit_once",
    "contribute_manager_signal",
    "build_360_review",
    "submit_360_feedback",
]
