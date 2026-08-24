"""Reactive engine (Phase C) — Tessbrain's readiness-aware delivery layer.

Идея:
Tessbrain НЕ молотит уведомлениями как обычный бот. Перед каждой важной
доставкой (Coffee artifact, action recommendation, learning nudge) мы
спрашиваем у Reactive: «Сейчас удачный момент?» Если нет — паркуем
сигнал в очередь и доставим позже.

Подсистемы:
- cognitive_load.py  — оценщик когнитивной нагрузки в [0.0; 1.0]
- signal_queue.py    — приоритетная очередь сигналов с drain'ом
- cascade.py         — каскадные инвалидации при отмене задач/встреч
- delivery_gate.py   — high-level should_deliver_now() для других модулей
- drain_runner.py    — единая доставка parked-сигналов (Phase D)
- feedback.py        — фиксация реакций юзера на доставленное (Phase E)
- calibration.py     — self-tuning per-user порогов из feedback (Phase E)

Все компоненты best-effort: если БД/recipient недоступны, never-raise
и degrade gracefully — система продолжает работать в "always-deliver"
режиме.

Подробная архитектура: docs/ru/READINESS_ARCHITECTURE.md.
"""
from backend.core.reactive.calibration import (
    CalibrationProfile,
    contextual_delta,
    get_calibration,
    get_channel_engagement,
    list_calibrations,
    recompute_all_calibrations,
    recompute_calibration,
)
from backend.core.reactive.cascade import (
    CascadeEvent,
    apply_cascade,
    record_cascade,
)
from backend.core.reactive.cognitive_load import (
    CognitiveLoadScore,
    LoadLevel,
    apply_user_weights,
    estimate_load,
    invalidate_load_cache,
)
from backend.core.reactive.delivery_gate import (
    GateDecision,
    GateRequest,
    bulk_gate,
    should_deliver_now,
)
from backend.core.reactive.drain_runner import run_drain
from backend.core.reactive.feedback import (
    FeedbackKind,
    list_feedback,
    record_feedback,
)
from backend.core.reactive.recommendations import (
    submit_night_insights,
    submit_recommendation,
)
from backend.core.reactive.recompute_trigger import (
    maybe_trigger_recompute,
    reset_trigger_state,
)
from backend.core.reactive.resolver import resolve_dependencies
from backend.core.reactive.signal_queue import (
    Signal,
    drain_signals,
    enqueue_signal,
    list_signals,
    mark_signal_delivered,
)

__all__ = [
    "CalibrationProfile",
    "CascadeEvent",
    "CognitiveLoadScore",
    "FeedbackKind",
    "GateDecision",
    "GateRequest",
    "LoadLevel",
    "Signal",
    "apply_cascade",
    "apply_user_weights",
    "bulk_gate",
    "contextual_delta",
    "drain_signals",
    "enqueue_signal",
    "estimate_load",
    "get_calibration",
    "get_channel_engagement",
    "invalidate_load_cache",
    "list_calibrations",
    "list_feedback",
    "list_signals",
    "mark_signal_delivered",
    "maybe_trigger_recompute",
    "record_cascade",
    "record_feedback",
    "recompute_all_calibrations",
    "recompute_calibration",
    "reset_trigger_state",
    "resolve_dependencies",
    "run_drain",
    "should_deliver_now",
    "submit_night_insights",
    "submit_recommendation",
]
