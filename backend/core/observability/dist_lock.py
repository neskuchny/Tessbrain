"""Redis-backed distributed lock — простая обвязка над SET NX EX.

Назначение: гарантировать, что джоба (например, nightly_consolidation)
запускается ровно в одном инстансе, даже если несколько API-подов крутят
APScheduler параллельно.

API:
    async with try_acquire("nightly_consolidation", ttl=3600) as got:
        if not got:
            return                    # кто-то другой уже взял лок
        await do_heavy_work()

Особенности:
- Лок берётся атомарно: SET key value NX EX ttl.
- Уникальный owner_token для безопасного освобождения (CAS-удаление).
- Если Redis недоступен — функция возвращает True (fail-open), чтобы
  локальный dev/single-replica деплой не зависел от Redis для своей работы.
  В multi-replica проде Redis обязателен; вылетит на старте здоровьем.
"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

logger = logging.getLogger(__name__)

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


@asynccontextmanager
async def try_acquire(
    name: str,
    *,
    ttl_seconds: int = 3600,
    namespace: str = "lock",
) -> AsyncIterator[bool]:
    """Попытаться взять распределённый лок. yield-ит True, если получилось.

    При выходе из блока лок отпускается через Lua-скрипт, который проверяет
    owner-token (защита от случайного удаления чужого лока, если предыдущий
    держатель просрочил TTL и кто-то другой уже взял лок).
    """
    key = f"{namespace}:{name}"
    token = secrets.token_hex(16)
    client = None
    try:
        from backend.db.redis_client import get_redis
        redis = await get_redis()
        if await redis.health_check():
            client = redis.client
    except Exception as exc:
        logger.warning(
            "DistLock(%s): Redis unavailable, falling back to local exclusive (fail-open). "
            "Multi-replica deployments must have Redis available. Cause: %s",
            name, exc,
        )
        yield True
        return

    if client is None:
        yield True
        return

    try:
        acquired = await client.set(key, token, nx=True, ex=ttl_seconds)
    except Exception as exc:
        logger.warning("DistLock(%s): SET error, fail-open: %s", name, exc)
        yield True
        return

    if not acquired:
        logger.info("DistLock(%s): held by another worker, skipping", name)
        yield False
        return

    logger.info("DistLock(%s): acquired (token=%s, ttl=%ss)", name, token[:8], ttl_seconds)
    try:
        yield True
    finally:
        try:
            await client.eval(_RELEASE_LUA, 1, key, token)
        except Exception as exc:
            logger.warning("DistLock(%s): release error: %s", name, exc)
