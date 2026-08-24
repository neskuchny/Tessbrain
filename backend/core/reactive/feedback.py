"""Delivery feedback (Phase E) — фиксация реакции юзера на доставленное.

Замыкает контур обучения: Reactive engine доставил сигнал → юзер
как-то отреагировал → мы записываем это → calibration.py пересчитывает
per-user пороги.

API:
- record_feedback(...)  — записать реакцию (UI/Telegram callback зовут это)
- list_feedback(...)    — выборка для calibration + debug

FeedbackKind упорядочены по «силе» реакции; calibration.py маппит их
в числовой engagement weight.

Все методы best-effort (never-raise).
"""
from __future__ import annotations

import enum
import json
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FeedbackKind(str, enum.Enum):
    """Реакция юзера на доставленный сигнал, от сильного + к сильному -."""

    ACTED = "acted"          # взял в работу — сильный позитив
    ENGAGED = "engaged"      # открыл/прочитал — позитив
    SNOOZED = "snoozed"      # отложил — нейтрально/мягкий минус
    IGNORED = "ignored"      # не открыл в окне — минус
    DISMISSED = "dismissed"  # явно закрыл — сильный минус


# Числовой вес реакции для engagement_score ∈ [-2; +2]
FEEDBACK_WEIGHTS: dict[str, float] = {
    FeedbackKind.ACTED.value: 2.0,
    FeedbackKind.ENGAGED.value: 1.0,
    FeedbackKind.SNOOZED.value: -0.25,
    FeedbackKind.IGNORED.value: -1.0,
    FeedbackKind.DISMISSED.value: -2.0,
}


def _normalize_kind(kind: str) -> Optional[str]:
    k = (kind or "").strip().lower()
    return k if k in FEEDBACK_WEIGHTS else None


def _valid_uuid(value: Any) -> Optional[str]:
    """Вернуть строку если value — валидный UUID, иначе None.

    Audit-фикс: caller'ы (Telegram callback, UI) могут передать опаковый
    signal_id типа 'msg-123' или 'tg:42'. Без этой проверки CAST(:x AS UUID)
    рушит весь INSERT и feedback теряется.
    """
    if value is None or value == "":
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError):
        return None


async def record_feedback(
    *,
    user_id: str,
    kind: str,
    signal_id: Optional[str] = None,
    artifact_id: Optional[str] = None,
    signal_type: str = "generic",
    load_at_delivery: Optional[float] = None,
    latency_seconds: Optional[int] = None,
    channel: Optional[str] = None,
    tenant_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Записать реакцию. Возвращает feedback_id или None.

    Если kind неизвестен — None (не пишем мусор).
    """
    norm = _normalize_kind(kind)
    if not norm:
        logger.debug("record_feedback: unknown kind %r", kind)
        return None
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        fid = str(uuid.uuid4())
        tid = tenant_id or user_id

        # Audit-фикс: signal_id/artifact_id могут прийти из Telegram-callback'а
        # или legacy-кода в любом виде. CAST(:x AS UUID) на не-UUID строке
        # рушит весь INSERT (DataError swallowed → feedback теряется). Чистим
        # до валидных UUID-строк, иначе → NULL (колонка nullable).
        # Сохраняем оригинал в details для отладки если он не None.
        merged_details = dict(details or {})
        sid_clean = _valid_uuid(signal_id)
        if signal_id and not sid_clean:
            merged_details.setdefault("raw_signal_id", str(signal_id)[:200])
        aid_clean = _valid_uuid(artifact_id)
        if artifact_id and not aid_clean:
            merged_details.setdefault("raw_artifact_id", str(artifact_id)[:200])

        # Phase K: подтянуть компоненты load на момент доставки в details
        # для weight-калибровки. Best-effort, не блокирует запись.
        try:
            comps = await _fetch_recent_load_components(user_id)
            if comps:
                merged_details.setdefault("load_components", comps)
        except Exception as exc:
            logger.debug("fetch load components skipped: %s", exc)

        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    INSERT INTO public.reactive_feedback
                        (id, user_id, tenant_id, signal_id, artifact_id,
                         signal_type, kind, load_at_delivery, latency_seconds,
                         channel, details)
                    VALUES
                        (CAST(:fid AS UUID), CAST(:uid AS UUID),
                         CAST(:tid AS UUID),
                         CAST(:sid AS UUID), CAST(:aid AS UUID),
                         :stype, :kind, :load, :lat, :ch,
                         CAST(:details AS JSONB))
                """),
                {
                    "fid": fid,
                    "uid": user_id,
                    "tid": tid,
                    "sid": sid_clean,
                    "aid": aid_clean,
                    "stype": signal_type,
                    "kind": norm,
                    "load": load_at_delivery,
                    "lat": latency_seconds,
                    "ch": channel,
                    "details": json.dumps(merged_details, ensure_ascii=False, default=str),
                },
            )
        # Phase I: near-real-time recompute trigger. Best-effort, fire-and-forget.
        try:
            from backend.core.reactive.recompute_trigger import maybe_trigger_recompute
            maybe_trigger_recompute(user_id, tenant_id=tid)
        except Exception as exc:
            logger.debug("recompute trigger skipped: %s", exc)
        return fid
    except Exception as exc:
        logger.warning("record_feedback failed: %s", exc)
        return None


async def list_feedback(
    *,
    user_id: str,
    since_days: int = 30,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Выборка feedback юзера за последние N дней (для calibration).

    Phase K: возвращает также `details` (содержит `load_components` для
    weight-калибровки).
    """
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(
                text("""
                    SELECT id, signal_type, kind, load_at_delivery,
                           latency_seconds, channel, created_at, details
                    FROM public.reactive_feedback
                    WHERE user_id = CAST(:uid AS UUID)
                      AND created_at > now() - make_interval(days => :days)
                    ORDER BY created_at DESC
                    LIMIT :lim
                """),
                {"uid": user_id, "days": int(since_days), "lim": int(limit)},
            )
            out: list[dict[str, Any]] = []
            for r in rows:
                d = r[7]
                if isinstance(d, str):
                    try:
                        d = json.loads(d)
                    except Exception:
                        d = {}
                out.append({
                    "id": str(r[0]),
                    "signal_type": r[1],
                    "kind": r[2],
                    "load_at_delivery": float(r[3]) if r[3] is not None else None,
                    "latency_seconds": int(r[4]) if r[4] is not None else None,
                    "channel": r[5],
                    "created_at": r[6].isoformat() if r[6] else None,
                    "details": d or {},
                })
            return out
    except Exception as exc:
        logger.warning("list_feedback failed: %s", exc)
        return []


_LOAD_COMPONENT_LOOKUP_MINUTES = 15


async def _fetch_recent_load_components(user_id: str) -> Optional[dict[str, float]]:
    """Phase K: подтянуть последний load-снимок юзера для weight-калибровки.

    Берём свежайшую запись из `cognitive_load_samples` за окно 15 мин.
    Best-effort: при сбое / отсутствии → None, feedback всё равно пишется.
    """
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            r = await session.execute(
                text("""
                    SELECT component_calendar, component_recent_activity,
                           component_sentiment, component_off_hours
                    FROM public.cognitive_load_samples
                    WHERE user_id = CAST(:uid AS UUID)
                      AND sampled_at > now() - make_interval(mins => :mins)
                    ORDER BY sampled_at DESC
                    LIMIT 1
                """),
                {"uid": user_id, "mins": _LOAD_COMPONENT_LOOKUP_MINUTES},
            )
            row = r.first()
        if not row:
            return None
        return {
            "calendar": float(row[0] or 0.0),
            "activity": float(row[1] or 0.0),
            "sentiment": float(row[2] or 0.0),
            "off_hours": float(row[3] or 0.0),
        }
    except Exception as exc:
        logger.debug("_fetch_recent_load_components failed: %s", exc)
        return None


__all__ = [
    "FEEDBACK_WEIGHTS",
    "FeedbackKind",
    "list_feedback",
    "record_feedback",
]
