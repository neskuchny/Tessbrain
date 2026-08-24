"""Signal queue (Phase C) — отложенные доставки.

Сигнал — что-то, что должно быть показано юзеру (artifact / nudge /
reminder / cascade-notice). Если момент неподходящий — паркуем в БД,
drain'им когда load падает.

API:
- enqueue_signal(...)  — поставить в очередь (Coffee orchestrator вызывает
  через delivery_gate, а не напрямую)
- list_signals(...)    — UI-вьюха «что Tessbrain ждёт момента»
- drain_signals(...)   — Cron/worker: пройтись по queued, попытаться
  отдать тем у кого сейчас low/medium load
- mark_signal_delivered(...) — после фактической доставки
- expire_signals()     — отмечает expired по valid_until

Все методы best-effort.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Один сигнал в очереди."""

    id: str
    user_id: str
    signal_type: str
    title: str
    body_markdown: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 50
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    status: str = "queued"
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    # Для retry-backoff в drain (F3-N): сколько раз доставка падала и когда
    # последний раз трогали строку. Заполняются list_signals.
    delivery_attempts: int = 0
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "signal_type": self.signal_type,
            "title": self.title,
            "body_markdown": self.body_markdown,
            "payload": self.payload,
            "priority": self.priority,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "status": self.status,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "delivery_attempts": self.delivery_attempts,
        }


async def enqueue_signal(
    *,
    user_id: str,
    signal_type: str,
    title: str,
    body_markdown: str = "",
    payload: Optional[dict[str, Any]] = None,
    priority: int = 50,
    tenant_id: Optional[str] = None,
    valid_until_minutes: Optional[int] = None,
    source_kind: Optional[str] = None,
    source_id: Optional[str] = None,
) -> Optional[str]:
    """Поставить сигнал в очередь. Возвращает signal_id (UUID) или None."""
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        sid = str(uuid.uuid4())
        tid = tenant_id or user_id
        # asyncpg биндит параметры ДО SQL-каста: TIMESTAMPTZ-колонке нужен
        # datetime, а не ISO-строка. Прод-падение: сигнал наблюдателя не
        # вставал в очередь вовсе («invalid input for query argument $11 …
        # expected a datetime … got 'str'») — лента/TG его не получали.
        valid_until = None
        if valid_until_minutes:
            from datetime import timedelta
            valid_until = (datetime.now(timezone.utc)
                           + timedelta(minutes=int(valid_until_minutes)))
        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    INSERT INTO public.reactive_signals
                        (id, user_id, tenant_id, signal_type,
                         source_kind, source_id,
                         title, body_markdown, payload,
                         priority, valid_until, status)
                    VALUES
                        (CAST(:sid AS UUID), CAST(:uid AS UUID),
                         CAST(:tid AS UUID), :stype,
                         :skind, :srcid,
                         :title, :body, CAST(:pl AS JSONB),
                         :pri, CAST(:vu AS TIMESTAMPTZ), 'queued')
                """),
                {
                    "sid": sid,
                    "uid": user_id,
                    "tid": tid,
                    "stype": signal_type,
                    "skind": source_kind,
                    "srcid": source_id,
                    "title": title[:500],
                    "body": (body_markdown or "")[:50000],
                    "pl": json.dumps(payload or {}, ensure_ascii=False, default=str),
                    "pri": max(0, min(100, int(priority))),
                    "vu": valid_until,
                },
            )
        return sid
    except Exception as exc:
        logger.warning("enqueue_signal failed: %s", exc)
        return None


async def list_signals(
    *,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    signal_type: Optional[str] = None,
    limit: int = 100,
) -> list[Signal]:
    """Вернуть signals по фильтрам, в порядке (priority DESC, valid_from ASC)."""
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        conditions: list[str] = []
        params: dict[str, Any] = {"lim": int(limit)}
        if user_id:
            conditions.append("user_id = CAST(:uid AS UUID)")
            params["uid"] = user_id
        if status:
            conditions.append("status = :st")
            params["st"] = status
        if signal_type:
            conditions.append("signal_type = :stype")
            params["stype"] = signal_type
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT id, user_id, signal_type, source_kind, source_id,
                   title, body_markdown, payload,
                   priority, valid_from, valid_until, status,
                   delivery_attempts, updated_at
            FROM public.reactive_signals
            {where}
            ORDER BY priority DESC, valid_from ASC
            LIMIT :lim
        """
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(text(sql), params)
            out: list[Signal] = []
            for r in rows:
                pl = r[7]
                if isinstance(pl, str):
                    try:
                        pl = json.loads(pl)
                    except Exception:
                        pl = {}
                out.append(Signal(
                    id=str(r[0]),
                    user_id=str(r[1]),
                    signal_type=r[2],
                    source_kind=r[3],
                    source_id=r[4],
                    title=r[5],
                    body_markdown=r[6] or "",
                    payload=pl or {},
                    priority=int(r[8] or 50),
                    valid_from=r[9].isoformat() if r[9] else None,
                    valid_until=r[10].isoformat() if r[10] else None,
                    status=r[11],
                    delivery_attempts=int(r[12] or 0),
                    updated_at=r[13].isoformat() if r[13] else None,
                ))
            return out
    except Exception as exc:
        logger.warning("list_signals failed: %s", exc)
        return []


async def mark_signal_delivered(
    *,
    signal_id: str,
    delivered_via: str,
    error_message: Optional[str] = None,
) -> Optional[str]:
    """Зафиксировать факт доставки сигнала. Audit-фикс: возвращает резулт-
    статус (delivered/dropped/queued/...) через RETURNING, чтобы drain мог
    различать failed (retry будет) и dropped (permanent loss) в stats.
    None при сбое БД (best-effort, как раньше).
    """
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        if error_message:
            sql = """
                UPDATE public.reactive_signals
                SET delivery_attempts = delivery_attempts + 1,
                    last_error = :err,
                    updated_at = now(),
                    status = CASE WHEN (delivery_attempts + 1) >= 3 THEN 'dropped' ELSE status END
                WHERE id = CAST(:sid AS UUID)
                RETURNING status
            """
            params = {"sid": signal_id, "err": error_message[:1000]}
        else:
            sql = """
                UPDATE public.reactive_signals
                SET status = 'delivered',
                    delivered_at = now(),
                    delivered_via = :via,
                    delivery_attempts = delivery_attempts + 1,
                    updated_at = now()
                WHERE id = CAST(:sid AS UUID)
                RETURNING status
            """
            params = {"sid": signal_id, "via": delivered_via}
        async with pg.session(apply_tenant=False) as session:
            r = await session.execute(text(sql), params)
            row = r.first()
        return str(row[0]) if row else None
    except Exception as exc:
        logger.warning("mark_signal_delivered failed: %s", exc)
        return None


async def expire_signals() -> int:
    """Помечает expired все queued сигналы с valid_until < now()."""
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            r = await session.execute(text("""
                UPDATE public.reactive_signals
                SET status = 'expired', updated_at = now()
                WHERE status = 'queued'
                  AND valid_until IS NOT NULL
                  AND valid_until < now()
            """))
            return r.rowcount or 0
    except Exception as exc:
        logger.warning("expire_signals failed: %s", exc)
        return 0


# ─── Drain ────────────────────────────────────────────────────


# Базовый retry-backoff (сек): фактический cooldown = _RETRY_BACKOFF_BASE *
# 2^(attempts-1), кап 1ч. attempts=1 → 60с, 2 → 120с (до 'dropped' на 3-й).
_RETRY_BACKOFF_BASE = 60.0
_RETRY_BACKOFF_CAP = 3600.0


def _backoff_elapsed(sig: "Signal") -> bool:
    """Прошёл ли cooldown с последней попытки доставки sig. None updated_at → да."""
    if not sig.updated_at:
        return True
    try:
        from datetime import datetime as _dt
        last = _dt.fromisoformat(sig.updated_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    except Exception:
        return True  # не смогли распарсить — не блокируем доставку
    cooldown = min(
        _RETRY_BACKOFF_CAP,
        _RETRY_BACKOFF_BASE * (2 ** max(0, sig.delivery_attempts - 1)),
    )
    return elapsed >= cooldown


async def drain_signals(
    *,
    deliver_fn: Callable[[Signal], Awaitable[tuple[bool, str, str]]],
    user_id: Optional[str] = None,
    max_signals: int = 50,
) -> dict[str, Any]:
    """Пройти по очереди и попытаться доставить сигналы у которых load OK.

    Args:
        deliver_fn: async (Signal) → (success, channel, error_message)
            Реальный delivery (обычно через core.coffee.delivery.deliver_artifact
            или Telegram bot).
        user_id: если задан — drain только этого юзера; иначе всех с очередью
        max_signals: лимит обработки за один вызов

    Returns:
        {"checked": N, "delivered": M, "deferred": K, "failed": F, "dropped": D, "expired": E}
    """
    from backend.core.reactive.cognitive_load import LoadLevel, estimate_load

    expired_count = await expire_signals()

    # Берём queued сигналы (если user_id указан — только его)
    signals = await list_signals(
        user_id=user_id, status="queued", limit=max_signals,
    )
    # Audit-фикс: failed/dropped больше не маскируются под "deferred".
    # "deferred" = load высокий, ждём низкой нагрузки (retry будет).
    # "failed"   = доставка попробовала и упала, но ещё в retry-окне.
    # "dropped"  = превысили retry-cap, permanently lost.
    stats = {
        "checked": len(signals),
        "delivered": 0,
        "deferred": 0,
        "failed": 0,
        "dropped": 0,
        "expired": int(expired_count or 0),
    }
    if not signals:
        return stats

    # Группируем по user_id (один load-check на юзера)
    by_user: dict[str, list[Signal]] = {}
    for s in signals:
        by_user.setdefault(s.user_id, []).append(s)

    for uid, user_signals in by_user.items():
        try:
            score = await estimate_load(
                user_id=uid, reason="scheduled_drain", persist=False,
            )
        except Exception:
            # Если не можем оценить load — считаем что low (отдаём)
            score = None

        # Threshold по уровню load
        threshold = _priority_threshold_for_level(
            score.load_level if score else LoadLevel.LOW
        )

        for sig in user_signals:
            if sig.priority < threshold:
                stats["deferred"] += 1
                continue
            # Audit-фикс (F3-N): retry-backoff. Без него 3 подряд drain-тика
            # сжигали весь retry-бюджет за секунды при транзиентном сбое канала
            # (бот-токен/ Gmail invalid_grant) → сигнал 'dropped' задолго до
            # истечения valid_until. Экспоненциальный backoff от updated_at:
            # уже падавший сигнал не трогаем, пока не прошло base*2^attempts.
            if sig.delivery_attempts > 0 and not _backoff_elapsed(sig):
                stats["deferred"] += 1
                continue
            try:
                ok, channel, err = await deliver_fn(sig)
            except Exception as exc:
                ok, channel, err = False, "", str(exc)
            if ok:
                await mark_signal_delivered(
                    signal_id=sig.id, delivered_via=channel,
                )
                stats["delivered"] += 1
            else:
                # Audit-фикс: учитываем фактический пост-UPDATE статус.
                # 'dropped' = превысили retry-cap, permanent loss — раньше
                # рапортовалось как 'deferred' (создавало впечатление "будет
                # ретрай"). Теперь dropped/failed считаются отдельно.
                result_status = await mark_signal_delivered(
                    signal_id=sig.id, delivered_via=channel,
                    error_message=err or "delivery failed",
                )
                if result_status == "dropped":
                    stats["dropped"] += 1
                else:
                    stats["failed"] += 1
    return stats


def _priority_threshold_for_level(level: Any) -> int:
    """Минимальный priority для доставки при данном load level."""
    from backend.core.reactive.cognitive_load import LoadLevel
    if level == LoadLevel.LOW:
        return 0
    if level == LoadLevel.MEDIUM:
        return 50
    if level == LoadLevel.HIGH:
        return 70
    return 90  # critical


__all__ = [
    "Signal",
    "drain_signals",
    "enqueue_signal",
    "expire_signals",
    "list_signals",
    "mark_signal_delivered",
]
