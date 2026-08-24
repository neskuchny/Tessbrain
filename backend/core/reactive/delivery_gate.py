"""Delivery gate (Phase C) — high-level «слать сейчас или парковать».

Используется Coffee orchestrator и любыми другими доставщиками перед
финальной отправкой:

    gate = await should_deliver_now(
        user_id=..., priority=70, signal_type="coffee_artifact",
    )
    if gate.deliver_now:
        ... отправляем по каналу ...
    else:
        await enqueue_signal(..., priority=70, ...)

Решение:
- Если load_score → LOW: всегда deliver_now=True
- MEDIUM: priority ≥ 50
- HIGH: priority ≥ 70
- CRITICAL: priority ≥ 90

Settings.reactive_engine_enabled = False → всегда deliver_now=True
(backward-compat для legacy флоу).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.core.reactive.cognitive_load import (
    CognitiveLoadScore,
    LoadLevel,
    estimate_load,
)
from backend.core.reactive.signal_queue import (
    _priority_threshold_for_level,
)

logger = logging.getLogger(__name__)


@dataclass
class GateDecision:
    deliver_now: bool
    reason: str
    threshold: int
    load: Optional[CognitiveLoadScore] = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deliver_now": self.deliver_now,
            "reason": self.reason,
            "threshold": self.threshold,
            "load": self.load.to_dict() if self.load else None,
            "details": self.details,
        }


def _is_engine_enabled(user_id: Optional[str] = None) -> bool:
    """Включён ли движок для юзера (Phase O canary rollout).

    Делегирует в rollout.is_reactive_enabled_for_user (master + percentage +
    canary allowlist). При сбое импорта — fallback на старую master-only логику.
    """
    try:
        from backend.core.reactive.rollout import is_reactive_enabled_for_user
        return is_reactive_enabled_for_user(user_id)
    except Exception:
        try:
            from backend.config import get_settings
            return bool(getattr(get_settings(), "reactive_engine_enabled", False))
        except Exception:
            return False


async def should_deliver_now(
    *,
    user_id: str,
    priority: int = 50,
    signal_type: str = "generic",
    persona: Optional[Any] = None,
    tenant_id: Optional[str] = None,
    force: bool = False,
) -> GateDecision:
    """Главное решение: слать сейчас или парковать.

    Args:
        priority: 0..100, см. _priority_threshold_for_level
        force: True → bypass всех проверок (для critical/manual)
    """
    if force:
        return GateDecision(deliver_now=True, reason="force", threshold=0)

    if not _is_engine_enabled(user_id):
        # Движок выключен для этого юзера (master off ИЛИ вне канареечного
        # раската) — legacy «всегда слать».
        return GateDecision(
            deliver_now=True, reason="engine_disabled", threshold=0,
        )

    # Critical priority всегда проходит
    if priority >= 90:
        return GateDecision(
            deliver_now=True, reason="critical_priority", threshold=90,
        )

    try:
        load = await estimate_load(
            user_id=user_id, tenant_id=tenant_id, persona=persona,
            reason=f"gate:{signal_type}", persist=True,
        )
    except Exception as exc:
        logger.debug("estimate_load failed: %s", exc)
        # Безопасный fallback — отдаём
        return GateDecision(
            deliver_now=True, reason="load_estimate_failed", threshold=0,
        )

    # Phase E/F/G/K: per-(user, signal_type) calibration с contextual delta
    # и per-user weights. effective_threshold = base + delta, base зависит
    # от load.load_level (Phase K пересчитывает level с per-user весами).
    threshold = 0
    base_threshold = _priority_threshold_for_level(load.load_level)
    calib = None
    applied_delta = 0.0
    delta_source = "none"
    weights_source = "global"
    try:
        from backend.core.reactive.calibration import (
            contextual_delta,
            get_calibration,
        )
        from backend.core.reactive.cognitive_load import apply_user_weights
        # Phase F: per-signal-type калибровка (fallback на '*' внутри).
        calib = await get_calibration(user_id, signal_type)

        # Phase K: per-user веса живут на '*' aggregate (load — user-level).
        # Если попали в per-type профиль — дополнительно подтягиваем '*' для весов.
        weights_profile = calib
        if calib.signal_type != "*" or all(
            getattr(calib, f"weight_{k}") is None
            for k in ("calendar", "activity", "sentiment", "off_hours")
        ):
            try:
                weights_profile = await get_calibration(user_id, "*")
            except Exception:
                weights_profile = calib

        weights = None
        if (
            weights_profile.weight_calendar is not None
            and weights_profile.weight_activity is not None
            and weights_profile.weight_sentiment is not None
            and weights_profile.weight_off_hours is not None
        ):
            weights = {
                "calendar": weights_profile.weight_calendar,
                "activity": weights_profile.weight_activity,
                "sentiment": weights_profile.weight_sentiment,
                "off_hours": weights_profile.weight_off_hours,
            }
        if weights:
            load = apply_user_weights(load, weights)
            base_threshold = _priority_threshold_for_level(load.load_level)
            weights_source = "user"

        # Phase G: bucket-delta под (возможно пересчитанный) load level.
        applied_delta, delta_source = contextual_delta(calib, load.load_level)
        threshold = int(max(0, min(95, round(base_threshold + applied_delta))))
    except Exception as exc:
        logger.debug("calibration unavailable: %s", exc)
        threshold = base_threshold

    deliver = priority >= threshold
    details: dict[str, Any] = {
        "priority": priority,
        "signal_type": signal_type,
        "weights_source": weights_source,  # Phase K: 'user' если применили per-user веса
    }
    reason = (
        f"load={load.load_level.value} threshold={threshold} priority={priority}"
    )
    if calib is not None and applied_delta != 0.0:
        details["calibration"] = {
            "base_threshold": base_threshold,
            "applied_delta": round(applied_delta, 1),
            "delta_source": delta_source,
            "engagement_score": round(calib.engagement_score, 2),
            "sample_count": calib.sample_count,
            "matched_signal_type": calib.signal_type,
        }
        reason += (
            f" (calibrated Δ{applied_delta:+.0f} via {delta_source} "
            f"from base {base_threshold})"
        )
    if weights_source == "user":
        reason += " [user weights]"

    return GateDecision(
        deliver_now=deliver,
        reason=reason,
        threshold=threshold,
        load=load,
        details=details,
    )


@dataclass
class GateRequest:
    """Phase L: единица запроса в bulk_gate."""

    user_id: str
    priority: int = 50
    signal_type: str = "generic"
    persona: Optional[Any] = None
    tenant_id: Optional[str] = None
    force: bool = False


async def bulk_gate(requests: list[GateRequest]) -> list[GateDecision]:
    """Phase L: batch-вариант should_deliver_now.

    Параллелит дорогие prefetch'и (load estimate с calendar API +
    calibration fetch) ПО ЮЗЕРАМ. Внутри одного юзера запросы идут
    последовательно — это намеренно: первый прогревает TTL-cache
    (load 5 мин, calibration 10 мин), последующие хитают cache,
    дополнительная параллельность бы только спорила за блокировку cache-init.

    Для пустого списка / одного элемента — fast path через
    `should_deliver_now`. Для смешанных батчей — `asyncio.gather` по юзерам.

    Возвращает GateDecision'ы в ОРИГИНАЛЬНОМ порядке.
    """
    n = len(requests)
    if n == 0:
        return []
    if n == 1:
        r = requests[0]
        return [await should_deliver_now(
            user_id=r.user_id, priority=r.priority, signal_type=r.signal_type,
            persona=r.persona, tenant_id=r.tenant_id, force=r.force,
        )]

    import asyncio as _asyncio

    # Группируем индексы по user_id чтобы внутри юзера прогревать cache
    by_user: dict[str, list[int]] = {}
    for i, r in enumerate(requests):
        by_user.setdefault(r.user_id, []).append(i)

    async def _user_batch(user_id: str, indices: list[int]) -> list[tuple[int, GateDecision]]:
        """Последовательно прогоняем запросы одного юзера — TTL-cache warms once."""
        out: list[tuple[int, GateDecision]] = []
        for idx in indices:
            req = requests[idx]
            try:
                decision = await should_deliver_now(
                    user_id=req.user_id, priority=req.priority,
                    signal_type=req.signal_type, persona=req.persona,
                    tenant_id=req.tenant_id, force=req.force,
                )
            except Exception as exc:
                logger.debug("bulk_gate item failed: %s", exc)
                decision = GateDecision(
                    deliver_now=True, reason="gate_item_failed", threshold=0,
                )
            out.append((idx, decision))
        return out

    user_batches = await _asyncio.gather(
        *[_user_batch(uid, lst) for uid, lst in by_user.items()],
        return_exceptions=False,
    )

    # Реассембл в оригинальный порядок
    decisions: list[Optional[GateDecision]] = [None] * n
    for batch in user_batches:
        for idx, d in batch:
            decisions[idx] = d
    # any None — конвертируем в безопасный allow (не должно случаться,
    # но на всякий случай)
    return [
        d if d is not None
        else GateDecision(deliver_now=True, reason="gate_missing", threshold=0)
        for d in decisions
    ]


__all__ = ["GateDecision", "GateRequest", "bulk_gate", "should_deliver_now"]
