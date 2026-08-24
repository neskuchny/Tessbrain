"""Near-real-time recompute trigger (Phase I).

Замыкает контур обучения: сейчас feedback применяется только через
суточный cron `recompute_calibration_task`. Между записью feedback и
изменением калибровки — до 24 часов задержки.

Phase I считает per-user new-events-since-last-recompute. Когда счётчик
дотягивает до `RECOMPUTE_THRESHOLD` (или прошло `MAX_INTERVAL_SECONDS` от
последнего пересчёта), `record_feedback` запускает recompute в фоне через
`asyncio.create_task`. Запрос возвращается мгновенно — пользователь
не ждёт.

Защиты от шторма:
- `MIN_INTERVAL_SECONDS` гарантия паузы между recompute'ами одного юзера
  (даже если порог по событиям пробит, дёргать не чаще раза в N секунд)
- in-flight set: если recompute этого юзера уже бежит — новый не запускаем
- worker-local state — на multi-worker максимум один лишний recompute
  на воркера (приемлемо: финальный результат одинаков, конкурируют ON
  CONFLICT UPSERT'ы)

Best-effort: любая ошибка приводит к no-op, синхронный путь record_feedback
ничего не теряет.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Сколько новых feedback-событий триггерят recompute (мягкое окно)
RECOMPUTE_THRESHOLD = 5

# Минимум секунд между recompute'ами одного юзера (anti-thrash)
MIN_INTERVAL_SECONDS = 60.0

# Гарантированный recompute даже при малом потоке: 6 часов от последнего
MAX_INTERVAL_SECONDS = 6 * 3600.0


# Per-user: (count_since_last, monotonic_ts_last_recompute)
_counters: dict[str, tuple[int, float]] = {}

# user_id'ы для которых recompute сейчас бежит — чтобы не дублировать
_inflight: set[str] = set()

# Держим ссылки на background-таски (иначе GC может убить task'у)
_tasks: set[asyncio.Task] = set()


def reset_trigger_state(user_id: Optional[str] = None) -> None:
    """Сбросить счётчики (тесты + ручной reset)."""
    if user_id is None:
        _counters.clear()
        _inflight.clear()
    else:
        _counters.pop(user_id, None)
        _inflight.discard(user_id)


def _should_recompute(user_id: str) -> bool:
    """True если набралось достаточно событий ИЛИ прошло MAX_INTERVAL.

    Audit-фикс: всегда инкрементит счётчик, но НЕ обнуляет его. Обнуление
    переехало в _commit_trigger() и происходит только после того, как
    background-task реально запланирован — иначе при сбое scheduling
    (нет running loop) накопленные события терялись.
    """
    now = time.monotonic()
    count, last_ts = _counters.get(user_id, (0, 0.0))
    count += 1
    _counters[user_id] = (count, last_ts)

    elapsed_since_last = now - last_ts if last_ts > 0 else float("inf")

    # Если только что был recompute (< MIN_INTERVAL) — копим, но не дёргаем
    if elapsed_since_last < MIN_INTERVAL_SECONDS:
        return False

    return count >= RECOMPUTE_THRESHOLD or elapsed_since_last >= MAX_INTERVAL_SECONDS


def _commit_trigger(user_id: str) -> None:
    """Сбросить счётчик после успешного scheduling background-recompute."""
    _, last_ts = _counters.get(user_id, (0, 0.0))
    _counters[user_id] = (0, last_ts)


async def _run_recompute(user_id: str, tenant_id: Optional[str]) -> None:
    """Background-таска: один recompute. Never-raise."""
    try:
        from backend.core.reactive.calibration import recompute_calibration
        await recompute_calibration(
            user_id=user_id, tenant_id=tenant_id,
        )
        # Обновляем last_ts только при успехе — иначе следующий feedback снова
        # попробует (с уже сброшенным count)
        cur = _counters.get(user_id, (0, 0.0))
        _counters[user_id] = (cur[0], time.monotonic())
        logger.debug("[reactive.trigger] recomputed calibration for %s", user_id)
    except Exception as exc:
        logger.debug("[reactive.trigger] recompute failed for %s: %s", user_id, exc)
    finally:
        _inflight.discard(user_id)


def maybe_trigger_recompute(
    user_id: str,
    tenant_id: Optional[str] = None,
) -> bool:
    """Phase I: возможно запустить background recompute. Never-raise.

    Returns True если фоновая таска реально стартанула.
    Зовётся из `record_feedback` после успешного INSERT.
    """
    try:
        if not user_id:
            return False
        if user_id in _inflight:
            return False
        if not _should_recompute(user_id):
            return False
        # Нужен running loop — если record_feedback вызвали из синхронного
        # контекста (не должно случаться, но best-effort), просто пропустим
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        _inflight.add(user_id)
        task = loop.create_task(_run_recompute(user_id, tenant_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        # Audit-фикс: обнуляем счётчик ТОЛЬКО после успешного scheduling.
        # Раньше _should_recompute зануляло до проверки running-loop,
        # и при отсутствии loop'а накопленные события терялись.
        _commit_trigger(user_id)
        return True
    except Exception as exc:
        logger.debug("[reactive.trigger] maybe_trigger failed for %s: %s", user_id, exc)
        return False


__all__ = [
    "MAX_INTERVAL_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "RECOMPUTE_THRESHOLD",
    "maybe_trigger_recompute",
    "reset_trigger_state",
]
