# -*- coding: utf-8 -*-
"""Атомарная резервация квоты — закрывает гонку «проверил → потратил → записал».

ПРОБЛЕМА (пункт «состояние гонки» из разбора уязвимостей). Проверка квоты
(`quota._check_one`) читает УЖЕ ЗАПИСАННЫЙ расход, а запись расхода
происходит после ответа модели. Между этими двумя моментами — окно. Сто
запросов, пришедших в одну миллисекунду, читают одно и то же «ещё ничего не
потрачено», проходят все, и только потом каждый записывает свою трату. Лимит
пробивается на величину залпа. Ровно как промокод, применённый сто раз
одновременно.

РЕШЕНИЕ. Разорвать окно: в момент проверки атомарно ЗАРЕЗЕРВИРОВАТЬ квоту,
чтобы параллельный запрос видел резерв ещё не завершившихся вызовов. Резерв
живёт от проверки до записи расхода и снимается, когда трата уже попала в
основной учёт.

Точной цены звонка заранее нет — токены известны только ПОСЛЕ ответа.
Поэтому резервируется ОЦЕНКА (LLM_QUOTA_RESERVE_TOKENS / _USD). Это не учёт,
а сторож на время полёта: основной счёт по-прежнему ведёт usage-store, а
резерв лишь не даёт залпу проскочить, когда бюджет почти исчерпан. Оценка
мала относительно суточного лимита (тысячи токенов против миллионов), так
что срабатывает только у самой границы — там, где и нужно.

АТОМАРНОСТЬ. Вся операция «выкинуть протухшие резервы → сложить живые →
сравнить с остатком бюджета → при успехе добавить свой» выполняется одним
Lua-скриптом на стороне Redis. Redis исполняет скрипт целиком, без
чередования с другими клиентами — поэтому два параллельных запроса не могут
оба прочитать бюджет до того, как любой из них зарезервирует.

САМОЗАЛЕЧИВАНИЕ. Резерв держит TTL (LLM_QUOTA_INFLIGHT_TTL_SEC). Явно он
снимается после записи расхода (usage_tracker.track → schedule_release), но
если процесс упал между ответом и записью — резерв всё равно уйдёт сам,
протухнув по времени, и следующая же проверка выкинет его. Худшее, что
бывает при утечке резерва, — квота считается чуть строже несколько минут
(консервативная сторона), а не наоборот.

БЕЗОПАСНОСТЬ ПО УМОЛЧАНИЮ. Redis недоступен, флаг выключен, любая ошибка —
пропускаем (fail-open), как и весь остальной модуль квот. То есть без Redis
поведение ровно такое же, как было. Выключатель: LLM_QUOTA_ATOMIC=off.
"""
from __future__ import annotations

import contextvars
import logging
import os
import time
import uuid
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Резервы текущего запроса — снимаются, когда его трата попала в учёт.
# ContextVar, а не глобал: у каждого запроса свой список, ничего не течёт
# между параллельными вызовами.
_reservations: contextvars.ContextVar[Optional[List[Tuple[str, str]]]] = (
    contextvars.ContextVar("llm_quota_reservations", default=None))

# Атомарно: выкинуть протухшее → сложить живое → сравнить → при успехе добавить.
# KEYS[1] — ключ ZSET резервов. ARGV: now_ms, expiry_ms, estimate, budget, member.
# Возврат: {admitted(0|1), inflight_units}.
_LUA_RESERVE = """
local now = tonumber(ARGV[1])
local expiry = tonumber(ARGV[2])
local est = tonumber(ARGV[3])
local budget = tonumber(ARGV[4])
local member = ARGV[5]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local live = redis.call('ZRANGE', KEYS[1], 0, -1)
local inflight = 0
for i=1,#live do
  local a = tonumber(string.match(live[i], ':(%-?%d+)$'))
  if a then inflight = inflight + a end
end
if inflight + est > budget then
  return {0, inflight}
end
redis.call('ZADD', KEYS[1], expiry, member)
redis.call('PEXPIRE', KEYS[1], expiry - now + 5000)
return {1, inflight}
"""


def _enabled() -> bool:
    """Выключатель. По умолчанию включено; LLM_QUOTA_ATOMIC=off глушит."""
    raw = (os.getenv("LLM_QUOTA_ATOMIC", "") or "").strip().lower()
    if raw in ("off", "0", "false", "no"):
        return False
    return True


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _ttl_sec() -> int:
    # Резерв живёт не дольше одного вызова модели (секунды). 120с — с запасом
    # на самые медленные ответы; больше и не нужно, иначе утечка тянулась бы
    # дольше положенного.
    return _int_env("LLM_QUOTA_INFLIGHT_TTL_SEC", 120)


def _estimate_units(metric: str) -> int:
    """Оценка одного вызова в целых единицах метрики.

    Токены — как есть. Стоимость — в микро-долларах (USD × 1e6), чтобы Lua
    считал целыми и не терял копейки на дробях.
    """
    if metric == "total_tokens":
        return _int_env("LLM_QUOTA_RESERVE_TOKENS", 4000)
    usd = _float_env("LLM_QUOTA_RESERVE_USD", 0.02)
    return round(usd * 1_000_000)


def _to_units(metric: str, native: float) -> int:
    return int(native) if metric == "total_tokens" else round(native * 1_000_000)


def _from_units(metric: str, units: int) -> float:
    return float(units) if metric == "total_tokens" else units / 1_000_000.0


async def _redis_client():
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        if not await redis.health_check():
            return None
        return redis.client
    except Exception as exc:
        logger.debug("quota_reservation: Redis недоступен: %s", exc)
        return None


def _key(scope: str, subject_id: str, metric: str, window: str) -> str:
    return f"quota:inflight:{scope}:{subject_id}:{metric}:{window}"


async def reserve(
    *,
    scope: str,
    subject_id: str,
    metric: str,
    window: str,
    limit: float,
    used: float,
) -> Tuple[bool, float]:
    """Зарезервировать оценку под текущий вызов. Возврат (admitted, inflight).

    admitted=False → допустить нельзя: записанный расход плюс уже летящие
    резервы не оставляют места под ещё один вызов. inflight — сколько сейчас
    зарезервировано другими (в натуральной единице метрики), для сообщения.

    Fail-open во всех сомнительных случаях: выключено, нет Redis, кривой
    ввод, любая ошибка — возвращаем (True, 0), поведение как без резервации.
    """
    if not _enabled() or limit <= 0:
        return True, 0.0
    budget_native = limit - used
    if budget_native <= 0:
        # Записанный расход уже упёрся в лимит — это ловит основная проверка
        # (_check_one бросает раньше). Сюда попадать не должны; на всякий
        # случай не даём делать вид, что место есть.
        return False, 0.0

    est_units = _estimate_units(metric)
    budget_units = _to_units(metric, budget_native)
    if est_units <= 0:
        return True, 0.0

    client = await _redis_client()
    if client is None:
        return True, 0.0

    now_ms = int(time.time() * 1000)
    expiry_ms = now_ms + _ttl_sec() * 1000
    member = f"{uuid.uuid4().hex}:{est_units}"
    key = _key(scope, subject_id, metric, window)
    try:
        res: Any = await client.eval(
            _LUA_RESERVE, 1, key,
            now_ms, expiry_ms, est_units, budget_units, member)
        admitted = bool(res[0])
        inflight_units = int(res[1])
    except Exception as exc:
        logger.debug("quota_reservation: eval failed, fail-open: %s", exc)
        return True, 0.0

    if admitted:
        lst = _reservations.get()
        if lst is None:
            lst = []
            _reservations.set(lst)
        lst.append((key, member))
    return admitted, _from_units(metric, inflight_units)


async def release_current() -> None:
    """Снять резервы текущего запроса. Идемпотентно, never-raise."""
    lst = _reservations.get()
    if not lst:
        return
    _reservations.set(None)
    client = await _redis_client()
    if client is None:
        return  # TTL/prune уберут сами
    for key, member in lst:
        try:
            await client.zrem(key, member)
        except Exception:
            logger.debug("quota_reservation: zrem failed (снимет TTL)", exc_info=True)


def schedule_release() -> None:
    """Освободить резерв из синхронного кода (usage_tracker.track).

    track() синхронный, а Redis асинхронный. Планируем снятие на текущий
    event loop и не ждём — трата уже записана в основной учёт, дальше резерв
    не нужен. Нет петли (чистый sync-контекст) или сбой — резерв уйдёт по
    TTL. Никогда не роняем track из-за телеметрии квоты.
    """
    if _reservations.get() is None:
        return
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # нет петли — снимет TTL
    try:
        loop.create_task(release_current())
    except Exception:
        logger.debug("quota_reservation: не удалось запланировать release", exc_info=True)


__all__ = ["release_current", "reserve", "schedule_release"]
