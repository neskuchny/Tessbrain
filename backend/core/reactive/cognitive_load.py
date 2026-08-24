"""Cognitive load estimator (Phase C).

Считает score ∈ [0.0; 1.0] показывающий, насколько user'у сейчас
не до уведомлений.

Компоненты (взвешенная сумма):
- component_calendar (40%):     плотность ближайших встреч (Google Calendar)
- component_recent_activity (25%): активность в messengers за последние 30 мин
- component_sentiment (20%):     последние NLP-сигналы (frustration/urgency)
- component_off_hours (15%):     вне рабочего окна Persona.behavioral.peak_hours

Уровни:
- 0.00-0.30  → low       — спокойно, можно слать всё
- 0.30-0.55  → medium    — слать только priority ≥ 50
- 0.55-0.80  → high      — только priority ≥ 70
- 0.80-1.00  → critical  — только priority ≥ 90

Все источники best-effort: если Google Calendar недоступен —
component=0 (т.е. считаем «нет данных = свободен»). Это намеренно
консервативно: лучше отдать сигнал чем потерять.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LoadLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class CognitiveLoadScore:
    """Результат estimate_load."""

    user_id: str
    load_score: float            # 0.0 .. 1.0
    load_level: LoadLevel
    component_calendar: float = 0.0
    component_sentiment: float = 0.0
    component_recent_activity: float = 0.0
    component_off_hours: float = 0.0
    reason: str = "pre_delivery_check"
    sampled_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "load_score": round(self.load_score, 3),
            "load_level": self.load_level.value,
            "component_calendar": round(self.component_calendar, 3),
            "component_sentiment": round(self.component_sentiment, 3),
            "component_recent_activity": round(self.component_recent_activity, 3),
            "component_off_hours": round(self.component_off_hours, 3),
            "reason": self.reason,
            "sampled_at": self.sampled_at,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CognitiveLoadScore":
        """Гидрация из dict (для L2 Redis-кеша). Толерантна к пропускам."""
        return cls(
            user_id=str(d.get("user_id", "")),
            load_score=float(d.get("load_score", 0.0)),
            load_level=LoadLevel(d.get("load_level", "low")),
            component_calendar=float(d.get("component_calendar", 0.0)),
            component_sentiment=float(d.get("component_sentiment", 0.0)),
            component_recent_activity=float(d.get("component_recent_activity", 0.0)),
            component_off_hours=float(d.get("component_off_hours", 0.0)),
            reason=str(d.get("reason", "pre_delivery_check")),
            sampled_at=str(d.get("sampled_at")
                           or datetime.now(timezone.utc).isoformat()),
            details=d.get("details") or {},
        )


# Weights для итоговой формулы
_W_CALENDAR = 0.40
_W_ACTIVITY = 0.25
_W_SENTIMENT = 0.20
_W_OFF_HOURS = 0.15


def apply_user_weights(
    score: "CognitiveLoadScore",
    weights: Optional[dict[str, float]],
) -> "CognitiveLoadScore":
    """Phase K: пересчитать score из тех же компонентов с per-user весами.

    `weights` ожидается dict с ключами calendar/activity/sentiment/off_hours.
    Если weights=None или сумма ≈ 0 — возвращаем score как есть (используем
    global константы). Иначе нормализуем веса (защита от плохо подобранных)
    и пересчитываем итоговый score + level. БЕЗ DB-запросов — переиспользуем
    components уже сохранённые на CognitiveLoadScore.
    """
    if not weights:
        return score
    try:
        total = sum(max(0.0, float(weights.get(k, 0.0)))
                    for k in ("calendar", "activity", "sentiment", "off_hours"))
        if total <= 0:
            return score
        wc = max(0.0, float(weights.get("calendar", 0.0))) / total
        wa = max(0.0, float(weights.get("activity", 0.0))) / total
        ws = max(0.0, float(weights.get("sentiment", 0.0))) / total
        wo = max(0.0, float(weights.get("off_hours", 0.0))) / total
    except (TypeError, ValueError):
        return score

    new_score = (
        wc * score.component_calendar
        + wa * score.component_recent_activity
        + ws * score.component_sentiment
        + wo * score.component_off_hours
    )
    new_score = max(0.0, min(1.0, new_score))
    return CognitiveLoadScore(
        user_id=score.user_id,
        load_score=new_score,
        load_level=_level_for_score(new_score),
        component_calendar=score.component_calendar,
        component_sentiment=score.component_sentiment,
        component_recent_activity=score.component_recent_activity,
        component_off_hours=score.component_off_hours,
        reason=f"{score.reason}:weighted",
        sampled_at=score.sampled_at,
        details={
            **score.details,
            "applied_weights": {
                "calendar": round(wc, 3),
                "activity": round(wa, 3),
                "sentiment": round(ws, 3),
                "off_hours": round(wo, 3),
            },
        },
    )


def _level_for_score(score: float) -> LoadLevel:
    if score < 0.30:
        return LoadLevel.LOW
    if score < 0.55:
        return LoadLevel.MEDIUM
    if score < 0.80:
        return LoadLevel.HIGH
    return LoadLevel.CRITICAL


async def estimate_load(
    *,
    user_id: str,
    tenant_id: Optional[str] = None,
    persona: Optional[Any] = None,
    reason: str = "pre_delivery_check",
    persist: bool = True,
    use_cache: bool = True,
    cache_ttl_seconds: float = 300.0,
) -> CognitiveLoadScore:
    """Оценить когнитивную нагрузку. Never-raise.

    Args:
        user_id: UUID юзера
        tenant_id: для cognitive_load_samples persist
        persona: Persona если уже подгружена (избегаем повторного fetch)
        reason: контекст оценки для samples
        persist: писать ли в cognitive_load_samples (False для unit-tests)
        use_cache: использовать ли in-process TTL-cache (Phase D). При
            batch-delivery (10 артефактов одной встречи) это убирает
            ~40 повторных SQL/calendar запросов. invalidate_load_cache()
            сбрасывает кеш для юзера (например после meeting_ended).
        cache_ttl_seconds: TTL записи в кеше (default 5 мин)
    """
    # Phase D: in-process TTL cache. Гейт зовётся per-artifact, а load
    # за 5 минут не меняется существенно → переиспользуем.
    if use_cache:
        cached = await _cache_get(user_id, cache_ttl_seconds)
        if cached is not None:
            # Возвращаем копию с актуальным reason, без повторного persist
            return CognitiveLoadScore(
                user_id=cached.user_id,
                load_score=cached.load_score,
                load_level=cached.load_level,
                component_calendar=cached.component_calendar,
                component_sentiment=cached.component_sentiment,
                component_recent_activity=cached.component_recent_activity,
                component_off_hours=cached.component_off_hours,
                reason=f"{reason}:cached",
                sampled_at=cached.sampled_at,
                details={**cached.details, "cache_hit": True},
            )

    components = {
        "calendar": 0.0,
        "activity": 0.0,
        "sentiment": 0.0,
        "off_hours": 0.0,
    }
    details: dict[str, Any] = {}

    # Persona подгружаем если не передали
    if persona is None:
        try:
            from backend.core.persona.store import get_persona_store
            persona = await get_persona_store().get(user_id)
        except Exception:
            persona = None

    # 1. Calendar density
    try:
        components["calendar"], cal_details = await _calendar_density(user_id)
        details["calendar"] = cal_details
    except Exception as exc:
        logger.debug("calendar density failed: %s", exc)

    # 2. Recent messenger activity
    try:
        components["activity"], act_details = await _recent_activity_pressure(user_id)
        details["activity"] = act_details
    except Exception as exc:
        logger.debug("recent activity failed: %s", exc)

    # 3. Sentiment из последних NLP-сигналов
    try:
        components["sentiment"], sent_details = await _sentiment_pressure(user_id)
        details["sentiment"] = sent_details
    except Exception as exc:
        logger.debug("sentiment failed: %s", exc)

    # 4. Off-hours — вне peak_hours Persona
    components["off_hours"] = _off_hours_component(persona)

    score = (
        _W_CALENDAR * components["calendar"]
        + _W_ACTIVITY * components["activity"]
        + _W_SENTIMENT * components["sentiment"]
        + _W_OFF_HOURS * components["off_hours"]
    )
    score = max(0.0, min(1.0, score))

    result = CognitiveLoadScore(
        user_id=user_id,
        load_score=score,
        load_level=_level_for_score(score),
        component_calendar=components["calendar"],
        component_sentiment=components["sentiment"],
        component_recent_activity=components["activity"],
        component_off_hours=components["off_hours"],
        reason=reason,
        details=details,
    )

    if persist:
        await _persist_sample(result, tenant_id=tenant_id or user_id)

    if use_cache:
        await _cache_set(user_id, result, cache_ttl_seconds)

    return result


# ─── TTL cache (Phase D + Phase M cross-worker) ───────────────
# Двухуровневый кеш: L1 in-process dict (быстрый reuse в пределах воркера,
# его прогревает bulk_gate при batch-доставке) + L2 Redis (shared между
# API-процессом и taskiq-воркерами). Phase M: раньше был только L1, и в
# multi-worker деплое инвалидация в одном процессе не доходила до других.
# L2 fail-open: если Redis недоступен, работаем как раньше — только на L1.

_load_cache: dict[str, tuple[float, CognitiveLoadScore]] = {}


def _load_key(user_id: str) -> str:
    return f"load:{user_id}"


async def _cache_get(user_id: str, ttl_seconds: float) -> Optional[CognitiveLoadScore]:
    import time as _time
    # L1
    entry = _load_cache.get(user_id)
    if entry is not None:
        ts, score = entry
        if (_time.monotonic() - ts) <= ttl_seconds:
            return score
        _load_cache.pop(user_id, None)
    # L2 Redis (cross-worker)
    try:
        from backend.core.reactive.dist_cache import cache_get_json
        blob = await cache_get_json(_load_key(user_id))
        if isinstance(blob, dict):
            score = CognitiveLoadScore.from_dict(blob)
            # Прогреваем L1, чтобы следующий per-artifact вызов был мгновенным
            _load_cache[user_id] = (_time.monotonic(), score)
            return score
    except Exception as exc:
        logger.debug("load L2 get failed: %s", exc)
    return None


async def _cache_set(
    user_id: str, score: CognitiveLoadScore, ttl_seconds: float = 300.0,
) -> None:
    import time as _time
    _load_cache[user_id] = (_time.monotonic(), score)
    # Лёгкий cap чтобы L1 не рос неограниченно
    if len(_load_cache) > 5000:
        # Выкидываем самые старые ~10%
        items = sorted(_load_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in items[:500]:
            _load_cache.pop(k, None)
    # L2 Redis с тем же TTL что и у L1-записи
    try:
        from backend.core.reactive.dist_cache import cache_set_json
        await cache_set_json(_load_key(user_id), score.to_dict(), int(ttl_seconds))
    except Exception as exc:
        logger.debug("load L2 set failed: %s", exc)


def invalidate_load_cache(user_id: Optional[str] = None) -> None:
    """Сбросить кеш (L1 сразу + L2 Redis best-effort). user_id=None → весь кеш.

    Зовётся после события которое меняет load (meeting_ended,
    user открыл Telegram) — чтобы следующий estimate_load был свежим.
    Сигнатура остаётся синхронной: L1 чистим немедленно, Redis-удаление
    откладываем в fire-and-forget task (если есть running loop). Cross-worker
    инвалидация гарантируется тем, что L2 удаляется во всех процессах через
    общий Redis; при отсутствии loop'а L2 истечёт сам по короткому TTL.
    """
    if user_id is None:
        _load_cache.clear()
    else:
        _load_cache.pop(user_id, None)
    try:
        from backend.core.reactive.dist_cache import (
            cache_delete,
            cache_delete_prefix,
            schedule_invalidation,
        )
        if user_id is None:
            schedule_invalidation(lambda: cache_delete_prefix("load:"))
        else:
            schedule_invalidation(lambda: cache_delete(_load_key(user_id)))
    except Exception as exc:
        logger.debug("load invalidate L2 schedule failed: %s", exc)


# ─── Компоненты ──────────────────────────────────────────────


async def _calendar_density(user_id: str) -> tuple[float, dict[str, Any]]:
    """Сколько busy-минут в ближайшие 2 часа. >= 90 мин → 1.0."""
    try:
        from backend.integrations.registry import IntegrationRegistry
        reg = IntegrationRegistry()
        await reg.load_for_user(user_id)
        cal = reg.get("google_calendar") or reg.get("calendar")
        if cal is None:
            return 0.0, {"reason": "no calendar integration"}
        # Многие calendar-integrations имеют list_events / get_events
        for fn_name in ("get_busy_minutes_ahead", "list_events_window"):
            fn = getattr(cal, fn_name, None)
            if callable(fn):
                try:
                    if fn_name == "get_busy_minutes_ahead":
                        minutes = await fn(window_minutes=120)
                    else:
                        now = datetime.now(timezone.utc)
                        events = await fn(
                            start=now,
                            end=now + timedelta(minutes=120),
                        )
                        minutes = sum(
                            int(e.get("duration_minutes", 0))
                            for e in (events or [])
                            if isinstance(e, dict)
                        )
                    score = min(1.0, float(minutes) / 90.0)
                    return score, {"busy_minutes": int(minutes)}
                except Exception as exc:
                    logger.debug("calendar fn %s failed: %s", fn_name, exc)
        return 0.0, {"reason": "no compatible method"}
    except Exception as exc:
        return 0.0, {"error": str(exc)[:200]}


async def _recent_activity_pressure(user_id: str) -> tuple[float, dict[str, Any]]:
    """Сколько входящих сообщений за последние 30 мин. >= 20 → 1.0."""
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()
        from backend.db.postgres_tenant import set_persona_tenant
        async with pg.session(apply_tenant=False) as session:
            # Phase A.6: persona_observations под RLS — выставляем tenant=user_id.
            await set_persona_tenant(session, user_id)
            r = await session.execute(
                text("""
                    SELECT COUNT(*)
                    FROM public.persona_observations
                    WHERE user_id = CAST(:uid AS UUID)
                      AND extracted_at > now() - interval '30 minutes'
                """),
                {"uid": user_id},
            )
            cnt = int((r.first() or [0])[0] or 0)
        score = min(1.0, cnt / 20.0)
        return score, {"observations_30min": cnt}
    except Exception as exc:
        return 0.0, {"error": str(exc)[:200]}


async def _sentiment_pressure(user_id: str) -> tuple[float, dict[str, Any]]:
    """Доля наблюдений за последний час с frustration/urgency.

    Берём persona_observations.payload->>'sentiment' и считаем долю
    значений из {"frustrated","urgent","anxious"}.
    """
    try:
        from backend.db.postgres import get_postgres
        from backend.db.postgres_tenant import set_persona_tenant
        from sqlalchemy import text
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            await set_persona_tenant(session, user_id)  # Phase A.6 persona-RLS
            r = await session.execute(
                text("""
                    SELECT
                      COUNT(*) FILTER (
                        WHERE payload->>'sentiment' IN ('frustrated','urgent','anxious')
                      ) AS hot,
                      COUNT(*) AS total
                    FROM public.persona_observations
                    WHERE user_id = CAST(:uid AS UUID)
                      AND extracted_at > now() - interval '60 minutes'
                """),
                {"uid": user_id},
            )
            row = r.first()
            hot = int((row[0] if row else 0) or 0)
            total = int((row[1] if row else 0) or 0)
        if total == 0:
            return 0.0, {"reason": "no observations"}
        ratio = hot / total
        # Усиливаем: 30% frustrated → score 1.0
        score = min(1.0, ratio * 3.3)
        return score, {"frustrated_ratio": round(ratio, 2), "total": total}
    except Exception as exc:
        return 0.0, {"error": str(exc)[:200]}


def _off_hours_component(persona: Optional[Any]) -> float:
    """1.0 если сейчас не peak_hour юзера, 0.0 если в peak_hour."""
    if persona is None:
        # Без Persona — fallback на universal worktime 09:00-19:00 локально UTC
        hour = datetime.now(timezone.utc).hour
        return 0.0 if 9 <= hour < 19 else 1.0
    try:
        peak = list(persona.behavioral.peak_hours or [])
    except Exception:
        peak = []
    if not peak:
        hour = datetime.now(timezone.utc).hour
        return 0.0 if 9 <= hour < 19 else 1.0
    hour = datetime.now(timezone.utc).hour
    return 0.0 if hour in peak else 1.0


# ─── Persist ──────────────────────────────────────────────────


async def _persist_sample(score: CognitiveLoadScore, *, tenant_id: str) -> None:
    """Сохранить sample в cognitive_load_samples. Best-effort."""
    try:
        import json
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    INSERT INTO public.cognitive_load_samples
                        (user_id, tenant_id, load_score, load_level,
                         component_calendar, component_sentiment,
                         component_recent_activity, component_off_hours,
                         reason, details)
                    VALUES
                        (CAST(:uid AS UUID), CAST(:tid AS UUID),
                         :score, :level,
                         :c1, :c2, :c3, :c4,
                         :reason, CAST(:details AS JSONB))
                """),
                {
                    "uid": score.user_id,
                    "tid": tenant_id,
                    "score": float(score.load_score),
                    "level": score.load_level.value,
                    "c1": float(score.component_calendar),
                    "c2": float(score.component_sentiment),
                    "c3": float(score.component_recent_activity),
                    "c4": float(score.component_off_hours),
                    "reason": score.reason,
                    "details": json.dumps(score.details, ensure_ascii=False, default=str),
                },
            )
    except Exception as exc:
        logger.debug("persist cognitive_load_sample failed: %s", exc)


__all__ = [
    "CognitiveLoadScore",
    "LoadLevel",
    "apply_user_weights",
    "estimate_load",
    "invalidate_load_cache",
]
