"""LLM token quotas — жёсткое ограничение расхода на пользователя/тенант.

Принцип:
- Считаем daily-токены через существующий UsageTracker (SQLite).
- Limit берём из settings: LLM_USER_DAILY_TOKEN_LIMIT, LLM_TENANT_DAILY_TOKEN_LIMIT.
- 0 == безлимит (для дев-режима / админ-учёток).
- При превышении кидаем `QuotaExceeded`, который Litestar-handler в app.py
  превращает в 429 с типизированной ошибкой.

Безопасность:
- Если SQLite/трекер недоступен — пропускаем (fail-open). Лимит вторичен по
  отношению к работающему сервису; observability покажет аномалию.

Производительность:
- На каждый LLM-вызов идёт один SQL count. Это терпимо при <50 RPS, но при
  росте — закешируем daily-totals в Redis с TTL=60s. См. TODO ниже.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class QuotaExceeded(Exception):
    """LLM-квота исчерпана. Ловится в API-слое и превращается в 429."""

    scope: str          # "user" | "tenant"
    subject_id: str     # user_id или tenant_id
    limit: int          # лимит токенов в сутки
    used: int           # сколько уже потрачено
    reset_at: str       # ISO-8601, когда сбросится (utc-полночь)

    def __post_init__(self) -> None:
        super().__init__(
            f"LLM {self.scope} quota exceeded: {self.used}/{self.limit} "
            f"tokens, resets at {self.reset_at}"
        )


class QuotaStoreUnavailable(Exception):
    """Строгий режим: usage-store недоступен, а значит расход НЕ поддаётся
    учёту — блокируем вызов (fail-closed), чтобы битый учёт не превратился
    в бесконтрольную трату при масштабе. По умолчанию НЕ используется —
    поведение остаётся fail-open."""


def _quota_strict() -> bool:
    import os as _os
    return _os.getenv("TESSENT_QUOTA_STRICT", "").lower() in (
        "on", "true", "1")


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _next_utc_midnight_iso() -> str:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


def _window_since(window: str, now: Optional[datetime] = None) -> Optional[str]:
    """Начало окна квоты (ISO-дата, включительно) для UTC-«сейчас».

    day → None (store сам берёт сегодня); week → понедельник текущей ISO-
    недели; month → первое число месяца (биллинговый период). Чистая
    функция — тестируется без БД."""
    if window == "day":
        return None
    now = now or datetime.now(timezone.utc)
    if window == "week":
        monday = now - timedelta(days=now.weekday())
        return monday.strftime("%Y-%m-%d")
    if window == "month":
        return now.replace(day=1).strftime("%Y-%m-%d")
    return None


def _window_reset_iso(window: str, now: Optional[datetime] = None) -> str:
    """Когда окно сбросится (для сообщения 429)."""
    now = now or datetime.now(timezone.utc)
    if window == "week":
        nxt = (now + timedelta(days=7 - now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return nxt.isoformat()
    if window == "month":
        nxt = (now.replace(day=28) + timedelta(days=4)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        return nxt.isoformat()
    return _next_utc_midnight_iso()


# issue #110 пакет 3: scope=job — атрибуция на конкретную фоновую задачу
# (taskiq job_id). Job-cap не имеет UTC-suffix-фильтра по дню — он считается
# за весь жизненный цикл job (taskiq-таска коротка, дневное окно избыточно).
_SCOPE_COLUMN = {"user": "user_id", "tenant": "tenant_id", "job": "job_id"}


def _daily_metric(
    db_path: str,   # unused (kept for backward-compat call sites)
    *,
    scope: str,
    subject_id: str,
    metric: str,
    since_date: Optional[str] = None,   # None → сегодня; иначе окно с даты
) -> float:
    """Сумма метрики за UTC-сегодня (для scope=job — за весь срок).

    issue #110 пакет 4: реализация делегирована в UsageStore — позволяет
    свободно подменить backend (SQLite/Postgres). db_path игнорируется
    (берётся из активного tracker.store), сохранён в сигнатуре для
    backward-compat с прямыми вызовами из старого кода.
    """
    if metric not in ("total_tokens", "total_cost"):
        return 0.0
    try:
        from backend.core.llm.usage_tracker import get_usage_tracker
        store = get_usage_tracker().store
        try:
            return float(store.sum_metric_for_quota(
                scope=scope, subject_id=subject_id, metric=metric,
                since_date=since_date))
        except TypeError:
            # кастомный store без since_date (плагин) → дневная семантика
            return float(store.sum_metric_for_quota(
                scope=scope, subject_id=subject_id, metric=metric))
    except Exception as exc:
        if _quota_strict():
            # fail-closed: не можем измерить расход → не разрешаем трату
            logger.error("Quota: store read error, fail-CLOSED (strict): %s",
                         exc)
            raise QuotaStoreUnavailable(str(exc)) from exc
        logger.warning("Quota: store read error, fail-open: %s", exc)
        return 0.0


def _daily_tokens(db_path: str, *, scope: str, subject_id: str) -> int:
    """Backward-compat: вызывает _daily_metric с metric=total_tokens."""
    return int(_daily_metric(db_path, scope=scope, subject_id=subject_id,
                             metric="total_tokens"))


def _check_one(
    *,
    db_path: str,
    scope: str,
    subject_id: str,
    metric: str,           # "total_tokens" | "total_cost"
    limit: float,
    near_ratio: float,
    window: str = "day",   # day | week | month (биллинговые окна)
) -> float:
    """Один check лимита по ЗАПИСАННОМУ расходу. Бросает QuotaExceeded при
    used >= limit, логирует warning при near_ratio*limit <= used < limit.
    Возвращает записанный расход `used` (нужен обёртке _guard для резерва).

    limit <= 0 трактуется как «безлимит» — выходим без проверки.
    window задаёт окно подсчёта: day (деф.), week (с понедельника UTC),
    month (с 1 числа — биллинговый период тарифа).

    ВНИМАНИЕ: это только про уже ЗАПИСАННЫЙ расход. От гонки «сто параллельных
    вызовов проверились раньше, чем записались» эта функция не защищает — это
    делает атомарная резервация в _guard (quota_reservation.reserve).
    """
    if limit <= 0:
        return 0.0
    used = _daily_metric(db_path, scope=scope, subject_id=subject_id,
                         metric=metric, since_date=_window_since(window))
    if used >= limit:
        # «used» в QuotaExceeded — int по контракту dataclass; для $-метрик
        # сохраняем доли центов через округление до целых mUSD.
        used_int = int(used) if metric == "total_tokens" else int(used * 1000)
        limit_int = int(limit) if metric == "total_tokens" else int(limit * 1000)
        raise QuotaExceeded(
            scope=scope,
            subject_id=subject_id,
            limit=limit_int,
            used=used_int,
            reset_at=_window_reset_iso(window),
        )
    if used >= near_ratio * limit:
        unit = "tokens" if metric == "total_tokens" else "USD"
        logger.warning(
            "LLM quota near-limit: %s=%s used %.2f/%.2f %s (>= %.0f%%)",
            scope, subject_id, used, limit, unit, near_ratio * 100,
        )
    return used


async def _guard(
    *,
    db_path: str,
    scope: str,
    subject_id: str,
    metric: str,
    limit: float,
    near_ratio: float,
    window: str = "day",
) -> None:
    """Проверка записанного расхода + атомарная резервация под текущий вызов.

    Сначала _check_one (может бросить по записанному расходу). Если место
    ещё есть — резервируем оценку в Redis; если её нет из-за уже летящих
    параллельных вызовов, тоже бросаем QuotaExceeded. Это и закрывает гонку:
    залп больше не проскакивает, потому что каждый летящий вызов держит
    резерв, видимый остальным.

    Резервация fail-open: без Redis / при выключенном флаге ведёт себя как
    прежняя проверка (только записанный расход).
    """
    used = _check_one(
        db_path=db_path, scope=scope, subject_id=subject_id,
        metric=metric, limit=limit, near_ratio=near_ratio, window=window)
    if limit <= 0:
        return
    from backend.core.llm import quota_reservation
    admitted, inflight = await quota_reservation.reserve(
        scope=scope, subject_id=subject_id, metric=metric, window=window,
        limit=limit, used=used)
    if not admitted:
        # Место занято летящими вызовами. used+inflight — сколько «в воздухе»
        # плюс записано; для сообщения приводим к контракту dataclass (int).
        if metric == "total_tokens":
            used_int = int(used + inflight)
            limit_int = int(limit)
        else:
            used_int = int((used + inflight) * 1000)
            limit_int = int(limit * 1000)
        logger.warning(
            "LLM quota race-guard: %s=%s резерв отклонён "
            "(записано %.2f + в полёте %.2f >= %.2f)",
            scope, subject_id, used, inflight, limit)
        raise QuotaExceeded(
            scope=scope, subject_id=subject_id,
            limit=limit_int, used=used_int,
            reset_at=_window_reset_iso(window))


async def assert_within_quota(
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> None:
    """Проверить все 6 квот перед LLM-вызовом (user_tokens, user_cost,
    tenant_tokens, tenant_cost, job_cost) — бросает QuotaExceeded при
    первом превышении.

    issue #110 пакет 3: добавлены $-лимиты и per-job cap. job_id берётся
    из cost_scope() через usage_context, либо передаётся явно. Если ни
    одного scope не задано — система анонимна (cron), пропускаем.

    Warning на 80% (настраивается `llm_quota_near_limit_ratio`) — логи
    «near-limit» появляются за раньше до hard-cap'а, чтобы оператор успел
    среагировать.
    """
    try:
        from backend.config import settings
    except Exception as exc:
        logger.warning("Quota: cannot load settings: %s", exc)
        return
    if not settings.llm_quota_enabled:
        return

    # Поднимаем job_id из usage_context, если не передан явно. Это даёт
    # «бесплатный» per-job cap для всех вызовов внутри cost_scope(job_id=…).
    if job_id is None:
        try:
            from backend.core.llm.usage_tracker import get_usage_context
            ctx = get_usage_context() or {}
            job_id = ctx.get("job_id")
        except Exception:
            job_id = None

    from backend.core.llm.usage_tracker import get_usage_tracker
    tracker = get_usage_tracker()
    db_path = tracker.db_path
    near = float(getattr(settings, "llm_quota_near_limit_ratio", 0.80) or 0.80)

    # Order: user → tenant → job. Сначала персональные, потом общие.
    # Каждая проверка может бросить — это намеренно (fail-fast).
    if user_id:
        await _guard(
            db_path=db_path, scope="user", subject_id=user_id,
            metric="total_tokens",
            limit=float(settings.llm_user_daily_token_limit),
            near_ratio=near)
        await _guard(
            db_path=db_path, scope="user", subject_id=user_id,
            metric="total_cost",
            limit=float(getattr(settings, "llm_user_daily_cost_usd", 0.0) or 0.0),
            near_ratio=near)
    if tenant_id:
        await _guard(
            db_path=db_path, scope="tenant", subject_id=tenant_id,
            metric="total_tokens",
            limit=float(settings.llm_tenant_daily_token_limit),
            near_ratio=near)
        await _guard(
            db_path=db_path, scope="tenant", subject_id=tenant_id,
            metric="total_cost",
            limit=float(getattr(settings, "llm_tenant_daily_cost_usd", 0.0) or 0.0),
            near_ratio=near)
    # Недельные окна (биллинг SaaS: «дневные лимиты и недельные»).
    # 0 == выключено (деф.) — включаются тарифом/env.
    if user_id:
        await _guard(
            db_path=db_path, scope="user", subject_id=user_id,
            metric="total_tokens", window="week",
            limit=float(getattr(settings, "llm_user_weekly_token_limit", 0) or 0),
            near_ratio=near)
    if tenant_id:
        await _guard(
            db_path=db_path, scope="tenant", subject_id=tenant_id,
            metric="total_tokens", window="week",
            limit=float(getattr(settings, "llm_tenant_weekly_token_limit", 0) or 0),
            near_ratio=near)
        # Месячный $-кап ТАРИФА (tenant_limits.monthly_tokens_usd): включённый
        # пул тарифа «минималка + токены сверху». Раньше был advisory — теперь
        # энфорсится тем же 429-путём. Lookup из Postgres с TTL-кэшем 60с
        # (не ходим в БД на каждый LLM-вызов); best-effort: нет БД → нет капа.
        if bool(getattr(settings, "llm_enforce_plan_monthly_cap", True)):
            monthly_cap = await _tenant_monthly_cap_cached(tenant_id)
            if monthly_cap > 0:
                await _guard(
                    db_path=db_path, scope="tenant", subject_id=tenant_id,
                    metric="total_cost", window="month",
                    limit=monthly_cap, near_ratio=near)
    if job_id:
        # job-scope — ТОЛЬКО $-лимит, токенов нет (job — короткая задача,
        # лимит токенов её отрезал бы преждевременно по случайной модели).
        await _guard(
            db_path=db_path, scope="job", subject_id=job_id,
            metric="total_cost",
            limit=float(getattr(settings, "llm_per_job_cost_usd", 0.0) or 0.0),
            near_ratio=near)


# ── Месячный кап тарифа: TTL-кэш поверх tenant_limits ────────────────────
_PLAN_CAP_TTL_SEC = 60.0
_plan_cap_cache: dict = {}   # tenant_id → (monotonic_ts, cap_usd)


async def _tenant_monthly_cap_cached(tenant_id: str) -> float:
    """monthly_tokens_usd тарифа тенанта (0 = нет капа). TTL-кэш 60с."""
    import time
    now = time.monotonic()
    hit = _plan_cap_cache.get(tenant_id)
    if hit and (now - hit[0]) < _PLAN_CAP_TTL_SEC:
        return hit[1]
    cap = 0.0
    try:
        from backend.core.auth.tenant_limits import get_tenant_limits
        lim = await get_tenant_limits(tenant_id)
        raw = lim.get("monthly_tokens_usd")
        cap = float(raw) if raw is not None else 0.0
    except Exception as exc:
        logger.debug("plan cap lookup failed (no cap): %s", exc)
    _plan_cap_cache[tenant_id] = (now, cap)
    return cap


def _bucket_status(
    scope: str, subject_id: Optional[str], db_path: str,
    *, token_limit: int, cost_limit: float,
) -> dict:
    """Сформировать статус одного scope (user/tenant/job) с обоими лимитами.

    Новая структура (issue #110 пакет 3): каждый bucket несёт parallel
    `tokens` и `cost` под-секции, чтобы UI мог показать оба индикатора.
    `limit/used/remaining` верхнего уровня сохранены для backward-compat
    с прежним UI (показывают только токены)."""
    if not subject_id:
        return {"id": None}
    tok_used = int(_daily_metric(db_path, scope=scope, subject_id=subject_id,
                                 metric="total_tokens"))
    cost_used = float(_daily_metric(db_path, scope=scope, subject_id=subject_id,
                                    metric="total_cost"))
    return {
        "id": subject_id,
        # backward-compat: верхний уровень = токены (так UI ходил до пакета 3)
        "limit": token_limit,
        "used": tok_used,
        "remaining": max(0, token_limit - tok_used) if token_limit > 0 else None,
        # новые под-секции с явным разделением
        "tokens": {
            "limit": token_limit,
            "used": tok_used,
            "remaining": max(0, token_limit - tok_used) if token_limit > 0 else None,
        },
        "cost": {
            "limit_usd": cost_limit,
            "used_usd": round(cost_used, 6),
            "remaining_usd": round(max(0.0, cost_limit - cost_used), 6)
            if cost_limit > 0 else None,
            "used_formatted": f"${cost_used:.4f}",
        },
    }


async def get_quota_status(
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> dict:
    """Прочитать текущий статус квоты (для UI/админки).

    issue #110 пакет 3: к токен-лимитам добавлены $-под-секции
    `user.cost`, `tenant.cost`, `job.cost`. Старые поля
    `user.limit/used/remaining` сохранены — UI на них всё ещё ходит."""
    from backend.config import settings
    from backend.core.llm.usage_tracker import get_usage_tracker
    tracker = get_usage_tracker()
    db_path = tracker.db_path
    near_ratio = float(
        getattr(settings, "llm_quota_near_limit_ratio", 0.80) or 0.80)
    return {
        "enabled": settings.llm_quota_enabled,
        "reset_at": _next_utc_midnight_iso(),
        "near_limit_ratio": near_ratio,
        "user": _bucket_status(
            "user", user_id, db_path,
            token_limit=settings.llm_user_daily_token_limit,
            cost_limit=float(getattr(settings, "llm_user_daily_cost_usd", 0.0) or 0.0),
        ),
        "tenant": _bucket_status(
            "tenant", tenant_id, db_path,
            token_limit=settings.llm_tenant_daily_token_limit,
            cost_limit=float(getattr(settings, "llm_tenant_daily_cost_usd", 0.0) or 0.0),
        ),
        "job": _bucket_status(
            "job", job_id, db_path,
            token_limit=0,  # для job — только $-cap, токены не лимитируем
            cost_limit=float(getattr(settings, "llm_per_job_cost_usd", 0.0) or 0.0),
        ) if job_id else None,
    }
