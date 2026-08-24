"""Distributed (cross-worker) cache helpers for the reactive engine.

Operational hardening (Phase M). До этого load- и calibration-кеши жили
ТОЛЬКО в in-process dict'ах. В реальном деплое крутятся несколько процессов
(API-сервер + один/несколько taskiq-воркеров), и каждый держал свою копию:

- инвалидация в одном процессе (после meeting_ended / recompute) не видна
  остальным → они продолжали отдавать stale load/calibration до истечения
  локального TTL (до 5–10 мин);
- решения delivery-gate расходились между процессами для одного юзера.

Здесь — тонкий слой поверх существующего RedisClient (backend/db/redis_client),
дающий двухуровневый кеш: L1 (in-process dict, у вызывающего модуля) + L2
(Redis, shared). Эти helper'ы покрывают только L2.

Все операции FAIL-OPEN: если Redis недоступен/не инициализирован, helper'ы
тихо деградируют до no-op / None, и вызывающий код продолжает работать на L1.
Это сохраняет never-raise дисциплину остального reactive-движка.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Namespace, чтобы reactive-ключи не пересекались с llm:prompt_cache,
# idem:* и прочими потребителями того же Redis.
KEY_PREFIX = "reactive:"

# Strong refs на fire-and-forget invalidation-tasks (см. schedule_invalidation).
_bg_tasks: set = set()

# ── Circuit breaker (F4-M) ───────────────────────────────────────
# Когда Redis недоступен, каждый L2-доступ платит connection-timeout (gate
# зовётся per-artifact в batch — это умножается). После сбоя «открываем»
# breaker на _BREAKER_COOLDOWN секунд: в это время L2-операции мгновенно
# no-op (работаем на L1), не дёргая мёртвый Redis. По истечении — пробуем снова.
_BREAKER_COOLDOWN = 30.0
_breaker_open_until: float = 0.0


def _breaker_open() -> bool:
    import time as _t
    return _t.monotonic() < _breaker_open_until


def _trip_breaker() -> None:
    import time as _t
    global _breaker_open_until
    _breaker_open_until = _t.monotonic() + _BREAKER_COOLDOWN


async def cache_get_json(key: str) -> Optional[Any]:
    """Прочитать JSON-значение из Redis. None при miss/ошибке/Redis-down."""
    if _breaker_open():
        return None
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        return await redis.get(KEY_PREFIX + key)
    except Exception as exc:
        _trip_breaker()
        logger.debug("dist_cache get %s failed (breaker tripped): %s", key, exc)
        return None


async def cache_set_json(key: str, value: Any, ttl_seconds: int) -> None:
    """Записать JSON-значение с TTL. Best-effort (no-op при Redis-down)."""
    if _breaker_open():
        return
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        await redis.set(KEY_PREFIX + key, value, expire=int(ttl_seconds))
    except Exception as exc:
        _trip_breaker()
        logger.debug("dist_cache set %s failed (breaker tripped): %s", key, exc)


async def cache_delete(key: str) -> None:
    """Удалить один ключ. Best-effort."""
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        await redis.delete(KEY_PREFIX + key)
    except Exception as exc:
        logger.debug("dist_cache delete %s failed: %s", key, exc)


async def cache_delete_prefix(prefix: str) -> int:
    """SCAN + DEL всех ключей по префиксу. Возвращает число удалённых.

    Нужно для инвалидации всех calibration-профилей юзера одним вызовом
    (reactive:calib:{user_id}:* покрывает '*' + per-signal-type). Использует
    неблокирующий scan_iter, чтобы не делать KEYS на проде. fail-open → 0.
    """
    deleted = 0
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        client = redis.client
        if client is None:
            return 0
        pattern = KEY_PREFIX + prefix + "*"
        async for k in client.scan_iter(match=pattern, count=200):
            await client.delete(k)
            deleted += 1
    except Exception as exc:
        logger.debug("dist_cache delete_prefix %s failed: %s", prefix, exc)
    return deleted


def schedule_invalidation(coro_factory) -> bool:
    """Запланировать async-инвалидацию из СИНХРОННОГО контекста, fire-and-forget.

    invalidate_load_cache / invalidate_calibration_cache имеют sync-сигнатуру
    (их зовут из разных мест, в т.ч. возможно синхронных). L1 они чистят сразу,
    а Redis-удаление откладывают сюда: если есть running loop — планируем task,
    иначе тихо пропускаем (L2 само истечёт по TTL). Возвращает True если task
    реально запланирован.
    """
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    try:
        # Audit-фикс (F3-M): держим strong ref на task. asyncio хранит лишь
        # weak reference — без _bg_tasks fire-and-forget invalidation мог быть
        # собран GC до выполнения (silent stale L2 cross-worker).
        task = loop.create_task(coro_factory())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return True
    except Exception as exc:
        logger.debug("schedule_invalidation failed: %s", exc)
        return False


__all__ = [
    "KEY_PREFIX",
    "cache_get_json",
    "cache_set_json",
    "cache_delete",
    "cache_delete_prefix",
    "schedule_invalidation",
]
