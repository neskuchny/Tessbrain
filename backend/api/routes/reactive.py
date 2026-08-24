"""Reactive engine REST endpoints (Phase C).

Endpoints:
- GET  /api/v1/reactive/load               — текущий cognitive_load юзера
- GET  /api/v1/reactive/signals            — список сигналов в очереди
- POST /api/v1/reactive/drain              — admin/cron: попытаться доставить
                                              отложенные сигналы (test endpoint;
                                              реальный drain — taskiq job)
- POST /api/v1/reactive/cascade            — записать cascade event (UI-action
                                              «эти briefs больше не актуальны»)

Все endpoints требуют JWT. drain — admin-only (Phase D enforcement).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.reactive import (
    apply_cascade,
    estimate_load,
    get_calibration,
    list_calibrations,
    list_signals,
    record_feedback,
    recompute_calibration,
    submit_recommendation,
)
from backend.core.reactive.rollout import rollout_status
from backend.core.reactive.stats import reactive_dashboard

logger = logging.getLogger(__name__)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required")
    try:
        uid = get_user_id_from_token(authorization)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    if not uid:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return uid


@get("/load", status_code=200)
async def get_load_endpoint(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Текущий cognitive_load score для авторизованного юзера."""
    uid = _extract_user(authorization)
    score = await estimate_load(
        user_id=uid, reason="manual_query", persist=False,
    )
    return score.to_dict()


@get("/signals", status_code=200)
async def list_signals_endpoint(
    status: Optional[str] = Parameter(query="status", default=None),
    signal_type: Optional[str] = Parameter(query="signal_type", default=None),
    limit: int = Parameter(query="limit", default=100),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Сигналы текущего юзера, по фильтрам."""
    uid = _extract_user(authorization)
    items = await list_signals(
        user_id=uid, status=status, signal_type=signal_type,
        limit=max(1, min(int(limit or 100), 500)),
    )
    return {"user_id": uid, "count": len(items), "signals": [s.to_dict() for s in items]}


@post("/drain", status_code=200)
async def drain_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Manual drain trigger (debug/admin).

    Body:
        {"user_id": "..." (optional, default — текущий юзер),
         "max_signals": 50}

    Реальный drain делает taskiq job (см. backend/workers/reactive_drain.py).
    """
    uid = _extract_user(authorization)
    target_user = data.get("user_id") or uid
    max_n = int(data.get("max_signals") or 50)

    from backend.core.reactive.drain_runner import run_drain
    stats = await run_drain(user_id=target_user, max_signals=max_n)
    return {"user_id": target_user, **stats}


@post("/cascade", status_code=200)
async def cascade_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Инвалидировать связанные артефакты + сигналы.

    Body:
        {
          "trigger_kind": "task_cancelled" | "meeting_rescheduled" | ...,
          "trigger_ref": "<id>",
          "artifact_ids": ["...", "..."],     # optional
          "signal_ids": ["...", "..."],       # optional
          "auto_resolve": true,                # Phase J: автоматический обход графа
          "reason": "user cancelled the project"
        }

    Phase J: при `auto_resolve=true` для meeting_*/artifact_* триггеров
    сервер сам найдёт зависимости через `resolver.resolve_dependencies`
    и сольёт с явными списками. Для прочих kind'ов auto_resolve = no-op.
    """
    uid = _extract_user(authorization)
    trigger_kind = data.get("trigger_kind") or "manual"
    trigger_ref = data.get("trigger_ref") or "unknown"
    artifact_ids = data.get("artifact_ids") or []
    signal_ids = data.get("signal_ids") or []
    auto_resolve = bool(data.get("auto_resolve", False))
    reason = data.get("reason") or ""

    event = await apply_cascade(
        user_id=uid,
        trigger_kind=trigger_kind,
        trigger_ref=trigger_ref,
        affected_artifact_ids=[str(x) for x in artifact_ids if x],
        affected_signal_ids=[str(x) for x in signal_ids if x],
        auto_resolve=auto_resolve,
        reason=reason,
    )
    return {
        "success": event is not None,
        "event": event.to_dict() if event else None,
    }


@post("/feedback", status_code=200)
async def feedback_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Phase E: записать реакцию юзера на доставленный сигнал/артефакт.

    Body:
        {
          "kind": "acted" | "engaged" | "snoozed" | "ignored" | "dismissed",
          "signal_id": "..." (optional),
          "artifact_id": "..." (optional),
          "signal_type": "coffee_strategic_brief" (optional),
          "load_at_delivery": 0.6 (optional),
          "latency_seconds": 120 (optional),
          "channel": "telegram" (optional)
        }

    Telegram inline-buttons / UI dispatch это после показа артефакта.
    """
    uid = _extract_user(authorization)
    kind = data.get("kind")
    if not kind:
        raise HTTPException(status_code=400, detail="kind required")

    fid = await record_feedback(
        user_id=uid,
        kind=str(kind),
        signal_id=data.get("signal_id"),
        artifact_id=data.get("artifact_id"),
        signal_type=str(data.get("signal_type") or "generic"),
        load_at_delivery=data.get("load_at_delivery"),
        latency_seconds=data.get("latency_seconds"),
        channel=data.get("channel"),
        details=data.get("details") if isinstance(data.get("details"), dict) else None,
    )
    if fid is None:
        raise HTTPException(
            status_code=400,
            detail=f"feedback not recorded (unknown kind {kind!r}?)",
        )
    return {"success": True, "feedback_id": fid}


@get("/calibration", status_code=200)
async def get_calibration_endpoint(
    signal_type: Optional[str] = Parameter(query="signal_type", default=None),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Калибровочный профиль юзера (transparency).

    Phase E: user-wide '*' aggregate.
    Phase F: ?signal_type=coffee_tz_document — конкретный тип (с fallback
    на '*'). Без параметра — '*' aggregate + список всех per-type профилей.
    """
    uid = _extract_user(authorization)
    if signal_type:
        profile = await get_calibration(uid, signal_type)
        return profile.to_dict()
    # Без фильтра — aggregate + полная разбивка
    agg = await get_calibration(uid, "*")
    all_profiles = await list_calibrations(uid)
    return {
        **agg.to_dict(),
        "by_signal_type": [p.to_dict() for p in all_profiles],
    }


@post("/calibration/recompute", status_code=200)
async def recompute_calibration_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Phase E: пересчитать калибровку юзера on-demand (debug/admin).

    Реальный пересчёт — cron taskiq recompute_calibration_task.
    """
    uid = _extract_user(authorization)
    since_days = int(data.get("since_days") or 30)
    profile = await recompute_calibration(user_id=uid, since_days=since_days)
    return profile.to_dict()


@get("/stats", status_code=200)
async def stats_endpoint(
    window_hours: int = Parameter(query="window_hours", default=24),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Phase O: дашборд-снимок здоровья reactive-движка для текущего юзера.

    Агрегирует сигналы по статусам, feedback + engagement, распределение
    load-уровней, применённую калибровку и состояние раската за окно
    ?window_hours (default 24, max 720).
    """
    uid = _extract_user(authorization)
    return await reactive_dashboard(user_id=uid, window_hours=int(window_hours or 24))


@get("/rollout", status_code=200)
async def rollout_endpoint(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Phase O: статус канареечного раската + включён ли движок для этого юзера."""
    uid = _extract_user(authorization)
    return rollout_status(uid)


@post("/recommendations", status_code=200)
async def submit_recommendation_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Phase N/O: провести произвольную рекомендацию через learning-loop.

    Body:
        {
          "title": "...",
          "body_markdown": "...",
          "signal_type": "wisdom_decision" | "night_insight_risk" | ...,
          "priority": 50,                          # optional, 0..100
          "recommended_channels": ["telegram"],    # optional
          "force": false                           # optional, bypass gate
        }

    Гейт решает: доставить сейчас или запарковать. Возвращает
    {"status": "delivered"|"queued"|"failed", "signal_id", "channel", ...}.
    """
    uid = _extract_user(authorization)
    title = (data.get("title") or "").strip()
    signal_type = (data.get("signal_type") or "").strip()
    if not title or not signal_type:
        raise HTTPException(status_code=400, detail="title and signal_type required")
    channels = data.get("recommended_channels")
    return await submit_recommendation(
        user_id=uid,
        title=title,
        body_markdown=str(data.get("body_markdown") or ""),
        signal_type=signal_type,
        priority=int(data.get("priority") or 50),
        recommended_channels=[str(c) for c in channels] if isinstance(channels, list) else None,
        force=bool(data.get("force", False)),
    )


router = Router(
    path="/reactive",
    route_handlers=[
        get_load_endpoint,
        list_signals_endpoint,
        drain_endpoint,
        cascade_endpoint,
        feedback_endpoint,
        get_calibration_endpoint,
        recompute_calibration_endpoint,
        stats_endpoint,
        rollout_endpoint,
        submit_recommendation_endpoint,
    ],
    tags=["Reactive"],
)
