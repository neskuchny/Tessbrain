"""Per-user calibration (Phase E) — self-tuning порогов из feedback.

Идея: фиксированный порог (Phase C) одинаков для всех. Но founder
который реагирует на каждый brief и dev который игнорит 9 из 10 —
разные. Phase E учит per-user threshold_delta из истории реакций.

Поток:
1. feedback.record_feedback пишет реакции в reactive_feedback
2. recompute_calibration(user_id) агрегирует за 30 дней →
   engagement_score ∈ [-2; +2] → threshold_delta → пишет в
   reactive_calibration
3. delivery_gate.should_deliver_now читает get_calibration(user_id)
   и применяет: effective_threshold = base + delta (clamp [0; 95])

Логика знака:
- engagement_score > 0 (юзер реагирует) → хотим слать БОЛЬШЕ →
  threshold НИЖЕ → delta отрицательный
- engagement_score < 0 (юзер игнорит) → слать МЕНЬШЕ →
  threshold ВЫШЕ → delta положительный

Cold-start protection: delta применяется только при
sample_count >= MIN_SAMPLES, иначе 0 (доверяем дефолтам).

Все методы best-effort.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Минимум feedback-событий для применения калибровки (cold-start guard).
MIN_SAMPLES = 5

# Phase G: минимум событий в load-bucket'е для contextual delta. Ниже
# глобального MIN_SAMPLES, т.к. bucket — подмножество (иначе bucket'ы
# почти никогда не «прогреваются»). При недоборе → fallback на global delta.
MIN_BUCKET_SAMPLES = 3

# Phase K: минимум событий С компонентами load для weight-калибровки.
# Веса меняют ШКАЛУ load'а, не сдвиг — изменение более чувствительное,
# чем threshold_delta. Поэтому консервативнее.
MIN_WEIGHT_SAMPLES = 10

# Компоненты load (ключи в payload feedback.details.load_components)
_LOAD_COMPONENT_KEYS = ("calendar", "activity", "sentiment", "off_hours")

# Масштаб: engagement_score [-2;+2] → delta. score=+2 → -20 (агрессивнее),
# score=-2 → +30 (осторожнее; консервативная сторона сильнее, т.к. спамить
# хуже чем недослать).
_DELTA_SCALE_POSITIVE = 10.0   # для score > 0: delta = -score * 10  → [-20; 0]
_DELTA_SCALE_NEGATIVE = 15.0   # для score < 0: delta = -score * 15  → [0; +30]

_DELTA_MIN = -20.0
_DELTA_MAX = 30.0


def _load_bucket(load_value: Optional[float]) -> Optional[str]:
    """load_at_delivery → bucket. Границы совпадают с LoadLevel (0.30/0.55)."""
    if load_value is None:
        return None
    if load_value < 0.30:
        return "low"
    if load_value < 0.55:
        return "medium"
    return "high"  # high + critical


def _load_level_to_bucket(load_level: Any) -> str:
    """LoadLevel (enum или str) → bucket-имя для lookup в context."""
    v = load_level.value if hasattr(load_level, "value") else str(load_level)
    if v == "low":
        return "low"
    if v == "medium":
        return "medium"
    return "high"  # high, critical


@dataclass
class CalibrationProfile:
    """Калибровочный профиль (user_id, signal_type).

    signal_type='*' — user-wide aggregate (Phase E семантика).
    Конкретный тип (Phase F) — например 'coffee_strategic_brief'.
    """

    user_id: str
    signal_type: str = "*"
    threshold_delta: float = 0.0
    sample_count: int = 0
    engagement_score: float = 0.0
    weight_calendar: Optional[float] = None
    weight_activity: Optional[float] = None
    weight_sentiment: Optional[float] = None
    weight_off_hours: Optional[float] = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def is_applied(self) -> bool:
        """True если калибровка реально влияет (прошла cold-start)."""
        return self.sample_count >= MIN_SAMPLES and self.threshold_delta != 0.0

    @property
    def effective_delta(self) -> float:
        """Delta с учётом cold-start guard."""
        return self.threshold_delta if self.sample_count >= MIN_SAMPLES else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "signal_type": self.signal_type,
            "threshold_delta": round(self.threshold_delta, 2),
            "effective_delta": round(self.effective_delta, 2),
            "sample_count": self.sample_count,
            "engagement_score": round(self.engagement_score, 3),
            "is_applied": self.is_applied,
            "min_samples": MIN_SAMPLES,
            "weights": {
                "calendar": self.weight_calendar,
                "activity": self.weight_activity,
                "sentiment": self.weight_sentiment,
                "off_hours": self.weight_off_hours,
            },
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalibrationProfile":
        """Гидрация из dict (для L2 Redis-кеша). Зеркалит to_dict().

        Примечание: to_dict эмитит производные поля (effective_delta,
        is_applied, min_samples) — они не конструкторные, игнорируем их.
        """
        w = d.get("weights") or {}

        def _f(x: Any) -> Optional[float]:
            return None if x is None else float(x)

        return cls(
            user_id=str(d.get("user_id", "")),
            signal_type=str(d.get("signal_type", "*")),
            threshold_delta=float(d.get("threshold_delta", 0.0)),
            sample_count=int(d.get("sample_count", 0)),
            engagement_score=float(d.get("engagement_score", 0.0)),
            weight_calendar=_f(w.get("calendar")),
            weight_activity=_f(w.get("activity")),
            weight_sentiment=_f(w.get("sentiment")),
            weight_off_hours=_f(w.get("off_hours")),
            details=d.get("details") or {},
        )


# ─── L1 in-process + L2 Redis cache (Phase M cross-worker) ────
# Ключ L1 — (user_id, signal_type). До Phase M был только L1, и recompute
# в taskiq-воркере не сбрасывал кеш в API-процессе → гейт там до 10 мин
# применял старую калибровку. L2 fail-open: работаем на L1 если Redis down.
_calib_cache: dict[tuple[str, str], tuple[float, CalibrationProfile]] = {}
_CALIB_TTL = 600.0  # 10 мин — калибровка меняется редко


def _calib_key(user_id: str, signal_type: str) -> str:
    return f"calib:{user_id}:{signal_type}"


async def _calib_l2_get(user_id: str, signal_type: str) -> Optional[CalibrationProfile]:
    try:
        from backend.core.reactive.dist_cache import cache_get_json
        blob = await cache_get_json(_calib_key(user_id, signal_type))
        if isinstance(blob, dict):
            return CalibrationProfile.from_dict(blob)
    except Exception as exc:
        logger.debug("calib L2 get failed: %s", exc)
    return None


async def _calib_l2_set(
    user_id: str, signal_type: str, profile: CalibrationProfile,
) -> None:
    try:
        from backend.core.reactive.dist_cache import cache_set_json
        await cache_set_json(
            _calib_key(user_id, signal_type), profile.to_dict(), int(_CALIB_TTL),
        )
    except Exception as exc:
        logger.debug("calib L2 set failed: %s", exc)


def invalidate_calibration_cache(user_id: Optional[str] = None) -> None:
    """Сброс кеша (L1 сразу + L2 Redis best-effort).

    user_id=None → весь; иначе все signal_type этого юзера. Зовётся из
    recompute_calibration после upsert. Sync-сигнатура сохранена: L1 чистим
    немедленно, Redis-удаление по префиксу — fire-and-forget (cross-worker
    инвалидация через общий Redis).
    """
    if user_id is None:
        _calib_cache.clear()
    else:
        for key in [k for k in _calib_cache if k[0] == user_id]:
            _calib_cache.pop(key, None)
    try:
        from backend.core.reactive.dist_cache import (
            cache_delete_prefix,
            schedule_invalidation,
        )
        if user_id is None:
            schedule_invalidation(lambda: cache_delete_prefix("calib:"))
        else:
            # calib:{user_id}: покрывает '*' и все per-signal-type профили
            schedule_invalidation(
                lambda: cache_delete_prefix(f"calib:{user_id}:")
            )
    except Exception as exc:
        logger.debug("calib invalidate L2 schedule failed: %s", exc)


def _compute_delta(engagement_score: float) -> float:
    """engagement_score → threshold_delta (см. модульный docstring)."""
    if engagement_score > 0:
        delta = -engagement_score * _DELTA_SCALE_POSITIVE
    else:
        delta = -engagement_score * _DELTA_SCALE_NEGATIVE
    return max(_DELTA_MIN, min(_DELTA_MAX, delta))


def _learn_weights(events: list) -> Optional[dict[str, float]]:
    """Phase K: per-user веса компонентов load из feedback.

    Алгоритм «separation»: для каждого компонента c считаем
    `mean(c | ignored) - mean(c | engaged)`. Положительный знак значит
    «когда c высокий, юзер чаще игнорит» → компонент хороший предиктор
    → вес ВЫШЕ. Отрицательный знак или ноль → false alarm → вес 0.

    После max(0, separation) для каждого нормализуем чтобы сумма = 1.0
    (если все нули — return None, gate использует global константы).

    Cold-start guard: события без `details.load_components` пропускаем;
    нужно >= MIN_WEIGHT_SAMPLES «годных» событий.

    Returns dict{calendar, activity, sentiment, off_hours} или None.
    """
    from backend.core.reactive.feedback import FEEDBACK_WEIGHTS

    # Собираем только события с известными компонентами И ненулевой реакцией
    engaged_vals: dict[str, list[float]] = {k: [] for k in _LOAD_COMPONENT_KEYS}
    ignored_vals: dict[str, list[float]] = {k: [] for k in _LOAD_COMPONENT_KEYS}
    usable = 0
    for e in events:
        details = e.get("details") or {}
        comps = details.get("load_components") if isinstance(details, dict) else None
        if not isinstance(comps, dict):
            continue
        try:
            vals = {k: float(comps.get(k, 0.0)) for k in _LOAD_COMPONENT_KEYS}
        except (TypeError, ValueError):
            continue
        w = FEEDBACK_WEIGHTS.get(e.get("kind"), 0.0)
        if w >= 1.0:        # engaged or acted
            target = engaged_vals
        elif w <= -1.0:     # ignored or dismissed
            target = ignored_vals
        else:
            continue        # snoozed — нейтрально, пропускаем
        for k, v in vals.items():
            target[k].append(v)
        usable += 1

    if usable < MIN_WEIGHT_SAMPLES:
        return None
    # Нужно по чему-то «отделять»: если одна из групп пустая — не учим
    if not any(engaged_vals.values()) or not any(ignored_vals.values()):
        return None

    raw: dict[str, float] = {}
    for k in _LOAD_COMPONENT_KEYS:
        eng_list = engaged_vals[k]
        ign_list = ignored_vals[k]
        mean_eng = sum(eng_list) / len(eng_list) if eng_list else 0.0
        mean_ign = sum(ign_list) / len(ign_list) if ign_list else 0.0
        sep = mean_ign - mean_eng    # положительная separation = хороший предиктор
        raw[k] = max(0.0, sep)

    total = sum(raw.values())
    if total <= 0.0:
        return None  # ни один компонент не разделяет — используем globals
    return {k: raw[k] / total for k in _LOAD_COMPONENT_KEYS}


async def _fetch_one(user_id: str, signal_type: str) -> Optional[CalibrationProfile]:
    """Прочитать конкретную строку (user_id, signal_type). None если нет."""
    from sqlalchemy import text
    from backend.db.postgres import get_postgres
    pg = await get_postgres()
    async with pg.session(apply_tenant=False) as session:
        r = await session.execute(
            text("""
                SELECT threshold_delta, sample_count, engagement_score,
                       weight_calendar, weight_activity,
                       weight_sentiment, weight_off_hours, details
                FROM public.reactive_calibration
                WHERE user_id = CAST(:uid AS UUID)
                  AND signal_type = :stype
                LIMIT 1
            """),
            {"uid": user_id, "stype": signal_type},
        )
        row = r.first()
    if not row:
        return None
    details = row[7]
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except Exception:
            details = {}
    return CalibrationProfile(
        user_id=user_id,
        signal_type=signal_type,
        threshold_delta=float(row[0] or 0.0),
        sample_count=int(row[1] or 0),
        engagement_score=float(row[2] or 0.0),
        weight_calendar=row[3],
        weight_activity=row[4],
        weight_sentiment=row[5],
        weight_off_hours=row[6],
        details=details or {},
    )


async def get_calibration(
    user_id: str,
    signal_type: str = "*",
    *,
    use_cache: bool = True,
) -> CalibrationProfile:
    """Прочитать профиль для (user_id, signal_type) с fallback на '*'.

    Phase F: сначала пробуем профиль конкретного signal_type. Если его
    нет или он не прошёл cold-start (sample_count < MIN_SAMPLES) — падаем
    на user-wide '*' aggregate. Если и тот холодный — нейтральный профиль.

    Never-raise: при любом сбое возвращает нейтральный (delta=0) профиль.
    """
    cache_key = (user_id, signal_type)
    if use_cache:
        # L1 (in-process)
        entry = _calib_cache.get(cache_key)
        if entry is not None and (time.monotonic() - entry[0]) <= _CALIB_TTL:
            return entry[1]
        # L2 (Redis, cross-worker) — прогреваем L1 при попадании
        l2 = await _calib_l2_get(user_id, signal_type)
        if l2 is not None:
            _calib_cache[cache_key] = (time.monotonic(), l2)
            return l2

    profile = CalibrationProfile(user_id=user_id, signal_type=signal_type)
    # Audit-фикс (F1-M): кешируем ТОЛЬКО профиль, реально прочитанный из БД.
    # Раньше при DB-miss/exception нейтральный (delta=0) профиль писался в L2
    # на 600s и отравлял ВСЕ воркеры cross-worker (а при транзиентной ошибке
    # БД — ещё и затирал хороший профиль до истечения TTL). from_db=False →
    # не трогаем кеш, следующий вызов попробует БД снова.
    from_db = False
    try:
        # 1. Конкретный тип
        specific = None
        if signal_type != "*":
            specific = await _fetch_one(user_id, signal_type)
        # 2. Fallback на '*' если specific нет/холодный
        if specific is not None and specific.sample_count >= MIN_SAMPLES:
            profile = specific
            from_db = True
        else:
            agg = await _fetch_one(user_id, "*")
            if agg is not None:
                # Помечаем что применили fallback (для transparency)
                if signal_type != "*" and specific is not None:
                    agg.details = {**agg.details, "fallback_from": signal_type,
                                   "specific_samples": specific.sample_count}
                elif signal_type != "*":
                    agg.details = {**agg.details, "fallback_from": signal_type}
                profile = agg
                from_db = True
            elif specific is not None:
                profile = specific
                from_db = True
    except Exception as exc:
        logger.debug("get_calibration failed for %s/%s: %s", user_id, signal_type, exc)

    if use_cache and from_db:
        _calib_cache[cache_key] = (time.monotonic(), profile)
        await _calib_l2_set(user_id, signal_type, profile)
    return profile


def _profile_from_events(
    *, user_id: str, signal_type: str, events: list, since_days: int,
) -> CalibrationProfile:
    """Построить CalibrationProfile из списка feedback-событий.

    Phase G: дополнительно считает context — engagement/delta по
    load-bucket'ам (low/medium/high из load_at_delivery) и по каналам.
    Bucket-данные кладутся в details["context"] и используются gate'ом
    для contextual delta под текущий load level.
    """
    from backend.core.reactive.feedback import FEEDBACK_WEIGHTS

    n = len(events)
    if n == 0:
        return CalibrationProfile(
            user_id=user_id, signal_type=signal_type,
            details={"reason": "no feedback"},
        )
    total = sum(FEEDBACK_WEIGHTS.get(e["kind"], 0.0) for e in events)
    engagement = total / n
    dist: dict[str, int] = {}
    # Phase G: контекстные группировки
    bucket_weights: dict[str, list[float]] = {}
    channel_weights: dict[str, list[float]] = {}
    for e in events:
        w = FEEDBACK_WEIGHTS.get(e["kind"], 0.0)
        dist[e["kind"]] = dist.get(e["kind"], 0) + 1
        b = _load_bucket(e.get("load_at_delivery"))
        if b:
            bucket_weights.setdefault(b, []).append(w)
        ch = e.get("channel")
        if ch:
            channel_weights.setdefault(ch, []).append(w)

    load_buckets: dict[str, Any] = {}
    for b, ws in bucket_weights.items():
        eng_b = sum(ws) / len(ws)
        load_buckets[b] = {
            "n": len(ws),
            "engagement": round(eng_b, 3),
            "delta": round(_compute_delta(eng_b), 1),
        }
    channels: dict[str, Any] = {}
    for ch, ws in channel_weights.items():
        channels[ch] = {
            "n": len(ws),
            "engagement": round(sum(ws) / len(ws), 3),
        }

    # Phase K: weight-калибровка только для '*' aggregate. load — user-level
    # величина; учить её веса на подмножестве (per-type) даст оверфит и
    # шумовые веса. Per-type профили оставляем без весов (None → globals).
    w_cal = w_act = w_sent = w_off = None
    if signal_type == "*":
        learned = _learn_weights(events)
        if learned is not None:
            w_cal = learned["calendar"]
            w_act = learned["activity"]
            w_sent = learned["sentiment"]
            w_off = learned["off_hours"]

    return CalibrationProfile(
        user_id=user_id,
        signal_type=signal_type,
        threshold_delta=_compute_delta(engagement),
        sample_count=n,
        engagement_score=engagement,
        weight_calendar=w_cal,
        weight_activity=w_act,
        weight_sentiment=w_sent,
        weight_off_hours=w_off,
        details={
            "distribution": dist,
            "window_days": since_days,
            "context": {"load_buckets": load_buckets, "channels": channels},
        },
    )


async def recompute_calibration(
    *,
    user_id: str,
    tenant_id: Optional[str] = None,
    since_days: int = 30,
) -> CalibrationProfile:
    """Пересчитать профили из истории feedback и сохранить. Never-raise.

    Phase F: считает '*' aggregate ПЛЮС отдельный профиль на каждый
    signal_type у которого набралось >= MIN_SAMPLES событий. Per-type
    профили с малым n не персистятся (get_calibration упадёт на '*').

    Returns user-wide '*' профиль (для обратной совместимости API).
    """
    from backend.core.reactive.feedback import list_feedback

    agg_profile = CalibrationProfile(user_id=user_id, signal_type="*")
    try:
        events = await list_feedback(user_id=user_id, since_days=since_days, limit=2000)

        # 1. User-wide '*' aggregate
        agg_profile = _profile_from_events(
            user_id=user_id, signal_type="*", events=events, since_days=since_days,
        )
        await _persist_calibration(agg_profile, tenant_id=tenant_id or user_id)

        # 2. Per-signal-type — только типы с достаточной статистикой
        by_type: dict[str, list] = {}
        for e in events:
            st = e.get("signal_type") or "generic"
            by_type.setdefault(st, []).append(e)
        type_summary: dict[str, Any] = {}
        for st, st_events in by_type.items():
            if st == "*":
                continue
            if len(st_events) < MIN_SAMPLES:
                continue  # недостаточно — пусть fallback на '*'
            st_profile = _profile_from_events(
                user_id=user_id, signal_type=st,
                events=st_events, since_days=since_days,
            )
            await _persist_calibration(st_profile, tenant_id=tenant_id or user_id)
            type_summary[st] = {
                "delta": round(st_profile.threshold_delta, 1),
                "samples": st_profile.sample_count,
            }
        if type_summary:
            agg_profile.details = {**agg_profile.details, "per_type": type_summary}
    except Exception as exc:
        logger.warning("recompute_calibration failed for %s: %s", user_id, exc)

    invalidate_calibration_cache(user_id)
    return agg_profile


async def recompute_all_calibrations(*, since_days: int = 30) -> dict[str, Any]:
    """Пересчитать для всех юзеров с feedback за окно. Для cron-таски."""
    stats = {"users": 0, "applied": 0}
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(
                text("""
                    SELECT DISTINCT user_id, tenant_id
                    FROM public.reactive_feedback
                    WHERE created_at > now() - make_interval(days => :days)
                """),
                {"days": int(since_days)},
            )
            targets = [(str(r[0]), str(r[1])) for r in rows]
        for uid, tid in targets:
            prof = await recompute_calibration(
                user_id=uid, tenant_id=tid, since_days=since_days,
            )
            stats["users"] += 1
            if prof.is_applied:
                stats["applied"] += 1
    except Exception as exc:
        logger.warning("recompute_all_calibrations failed: %s", exc)
    return stats


async def _persist_calibration(profile: CalibrationProfile, *, tenant_id: str) -> None:
    """Upsert профиля в reactive_calibration. Best-effort.

    Phase K: weight_* колонки тоже апсертятся. None → NULL (cold-start,
    estimate_load использует global константы).
    """
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    INSERT INTO public.reactive_calibration
                        (user_id, tenant_id, signal_type, threshold_delta,
                         sample_count, engagement_score,
                         weight_calendar, weight_activity,
                         weight_sentiment, weight_off_hours,
                         updated_at, details)
                    VALUES
                        (CAST(:uid AS UUID), CAST(:tid AS UUID), :stype,
                         :delta, :n, :eng,
                         :w_cal, :w_act, :w_sent, :w_off,
                         now(), CAST(:details AS JSONB))
                    ON CONFLICT (user_id, signal_type) DO UPDATE SET
                        threshold_delta = EXCLUDED.threshold_delta,
                        sample_count = EXCLUDED.sample_count,
                        engagement_score = EXCLUDED.engagement_score,
                        weight_calendar = EXCLUDED.weight_calendar,
                        weight_activity = EXCLUDED.weight_activity,
                        weight_sentiment = EXCLUDED.weight_sentiment,
                        weight_off_hours = EXCLUDED.weight_off_hours,
                        updated_at = now(),
                        details = EXCLUDED.details
                """),
                {
                    "uid": profile.user_id,
                    "tid": tenant_id,
                    "stype": profile.signal_type,
                    "delta": float(profile.threshold_delta),
                    "n": int(profile.sample_count),
                    "eng": float(profile.engagement_score),
                    "w_cal": profile.weight_calendar,
                    "w_act": profile.weight_activity,
                    "w_sent": profile.weight_sentiment,
                    "w_off": profile.weight_off_hours,
                    "details": json.dumps(profile.details, ensure_ascii=False, default=str),
                },
            )
    except Exception as exc:
        logger.debug("persist_calibration failed: %s", exc)


def contextual_delta(
    profile: CalibrationProfile,
    load_level: Any = None,
) -> tuple[float, str]:
    """Phase G: вернуть (delta, source) с учётом текущего load level.

    Если профиль прошёл global cold-start И для bucket'а под текущий
    load_level набрано >= MIN_BUCKET_SAMPLES событий — берём bucket-delta
    (юзер реагирует ИНАЧЕ при этом уровне нагрузки). Иначе — global
    effective_delta.

    source: "bucket:<low|medium|high>" или "global".
    """
    base = profile.effective_delta  # cold-start guard на global уже внутри
    if load_level is None or profile.sample_count < MIN_SAMPLES:
        return base, "global"
    ctx = (profile.details or {}).get("context") or {}
    buckets = ctx.get("load_buckets") or {}
    bucket = _load_level_to_bucket(load_level)
    bdata = buckets.get(bucket)
    if isinstance(bdata, dict) and int(bdata.get("n", 0)) >= MIN_BUCKET_SAMPLES:
        try:
            return float(bdata.get("delta", base)), f"bucket:{bucket}"
        except (TypeError, ValueError):
            return base, "global"
    return base, "global"


def apply_calibration_to_threshold(
    base_threshold: int,
    profile: CalibrationProfile,
    load_level: Any = None,
) -> int:
    """base_threshold + delta, клиппится в [0; 95].

    Phase G: если передан load_level — используется contextual (bucket)
    delta; иначе global effective_delta (backward-compat с Phase E/F).

    95 (не 100) чтобы calibration не могла полностью заблокировать даже
    высокоприоритетные не-critical сигналы (priority 90+ и так bypass'ят gate).
    """
    delta, _src = contextual_delta(profile, load_level)
    return int(max(0, min(95, round(base_threshold + delta))))


async def list_calibrations(user_id: str) -> list[CalibrationProfile]:
    """Все профили юзера (все signal_type). Для transparency UI. Never-raise."""
    out: list[CalibrationProfile] = []
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(
                text("""
                    SELECT signal_type, threshold_delta, sample_count,
                           engagement_score, details
                    FROM public.reactive_calibration
                    WHERE user_id = CAST(:uid AS UUID)
                    ORDER BY (signal_type = '*') DESC, sample_count DESC
                """),
                {"uid": user_id},
            )
            for r in rows:
                details = r[4]
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except Exception:
                        details = {}
                out.append(CalibrationProfile(
                    user_id=user_id,
                    signal_type=r[0],
                    threshold_delta=float(r[1] or 0.0),
                    sample_count=int(r[2] or 0),
                    engagement_score=float(r[3] or 0.0),
                    details=details or {},
                ))
    except Exception as exc:
        logger.debug("list_calibrations failed for %s: %s", user_id, exc)
    return out


async def get_channel_engagement(
    user_id: str,
    signal_type: str = "*",
    *,
    min_n: int = MIN_BUCKET_SAMPLES,
) -> dict[str, float]:
    """Phase H: engagement по каналам для (user, signal_type).

    Читает details.context.channels из калиброванного профиля (с тем же
    fallback на '*' что и get_calibration). Возвращает {channel: engagement}
    только для каналов с >= min_n наблюдений (cold-start guard). Пустой
    dict если данных нет — delivery тогда работает как раньше.

    Never-raise.
    """
    try:
        profile = await get_calibration(user_id, signal_type)
        channels = ((profile.details or {}).get("context") or {}).get("channels") or {}
        out: dict[str, float] = {}
        for ch, data in channels.items():
            if isinstance(data, dict) and int(data.get("n", 0)) >= min_n:
                try:
                    out[ch] = float(data.get("engagement", 0.0))
                except (TypeError, ValueError):
                    continue
        return out
    except Exception as exc:
        logger.debug("get_channel_engagement failed for %s: %s", user_id, exc)
        return {}


__all__ = [
    "CalibrationProfile",
    "MIN_BUCKET_SAMPLES",
    "MIN_SAMPLES",
    "apply_calibration_to_threshold",
    "contextual_delta",
    "get_calibration",
    "get_channel_engagement",
    "invalidate_calibration_cache",
    "list_calibrations",
    "recompute_all_calibrations",
    "recompute_calibration",
]
