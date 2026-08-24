# -*- coding: utf-8 -*-
"""
Rate Limiter — Ограничение частоты запросов к шине данных.

Использует Redis если доступен, иначе fallback на in-memory.
"""
import logging
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Лимитер запросов с Redis и in-memory fallback.

    Проверяет лимиты:
    - per_hour: максимум запросов в час
    - per_day: максимум запросов в день
    """

    def __init__(self):
        """Инициализация."""
        self._redis = None
        self._memory_store: Dict[str, List[float]] = {}

    async def _get_redis(self):
        """Получить Redis клиент (ленивая инициализация)."""
        if self._redis is None:
            try:
                from backend.db.redis_client import get_redis
                redis = await get_redis()
                if await redis.health_check():
                    self._redis = redis
            except Exception as e:
                logger.debug(f"Redis not available for rate limiting: {e}")
                self._redis = False  # False = пробовали, не получилось
        return self._redis if self._redis and self._redis is not False else None

    async def check_and_increment(
        self,
        consumer_id: str,
        max_per_hour: int = 20,
        max_per_day: int = 100,
    ) -> Tuple[bool, str]:
        """
        Проверить rate limit и инкрементировать счётчик.

        Args:
            consumer_id: ID потребителя
            max_per_hour: Максимум запросов в час
            max_per_day: Максимум запросов в день

        Returns:
            (allowed: bool, reason: str)
            - allowed=True → запрос разрешён
            - allowed=False, reason="hourly limit" или "daily limit"
        """
        redis = await self._get_redis()

        if redis:
            return await self._check_redis(consumer_id, max_per_hour, max_per_day)
        else:
            return self._check_memory(consumer_id, max_per_hour, max_per_day)

    async def get_usage(self, consumer_id: str) -> Dict[str, int]:
        """Получить текущее использование."""
        redis = await self._get_redis()

        if redis:
            return await self._get_usage_redis(consumer_id)
        else:
            return self._get_usage_memory(consumer_id)

    # ──────────────── Redis implementation ────────────────

    async def _check_redis(
        self,
        consumer_id: str,
        max_per_hour: int,
        max_per_day: int,
    ) -> Tuple[bool, str]:
        """Проверка через Redis (sliding window)."""
        try:
            now = time.time()
            hour_key = f"databus:ratelimit:{consumer_id}:hour"
            day_key = f"databus:ratelimit:{consumer_id}:day"

            redis_client = self._redis.client

            # Hourly check (sliding window: удаляем записи старше 1 часа)
            await redis_client.zremrangebyscore(hour_key, 0, now - 3600)
            hour_count = await redis_client.zcard(hour_key)

            if hour_count >= max_per_hour:
                return False, f"Hourly limit exceeded ({hour_count}/{max_per_hour})"

            # Daily check
            await redis_client.zremrangebyscore(day_key, 0, now - 86400)
            day_count = await redis_client.zcard(day_key)

            if day_count >= max_per_day:
                return False, f"Daily limit exceeded ({day_count}/{max_per_day})"

            # Increment
            member = f"{now}"
            await redis_client.zadd(hour_key, {member: now})
            await redis_client.zadd(day_key, {member: now})
            await redis_client.expire(hour_key, 3600)
            await redis_client.expire(day_key, 86400)

            return True, "ok"

        except Exception as e:
            logger.warning(f"Redis rate limit error, falling back to memory: {e}")
            return self._check_memory(consumer_id, max_per_hour, max_per_day)

    async def _get_usage_redis(self, consumer_id: str) -> Dict[str, int]:
        """Получить использование из Redis."""
        try:
            now = time.time()
            redis_client = self._redis.client

            hour_key = f"databus:ratelimit:{consumer_id}:hour"
            day_key = f"databus:ratelimit:{consumer_id}:day"

            await redis_client.zremrangebyscore(hour_key, 0, now - 3600)
            await redis_client.zremrangebyscore(day_key, 0, now - 86400)

            hour_count = await redis_client.zcard(hour_key)
            day_count = await redis_client.zcard(day_key)

            return {"queries_this_hour": hour_count, "queries_today": day_count}
        except Exception:
            return self._get_usage_memory(consumer_id)

    # ──────────────── In-memory fallback ────────────────

    def _check_memory(
        self,
        consumer_id: str,
        max_per_hour: int,
        max_per_day: int,
    ) -> Tuple[bool, str]:
        """Проверка через in-memory хранилище."""
        now = time.time()

        if consumer_id not in self._memory_store:
            self._memory_store[consumer_id] = []

        # Очистка старых записей (старше 24 часов)
        self._memory_store[consumer_id] = [
            t for t in self._memory_store[consumer_id]
            if t > now - 86400
        ]

        timestamps = self._memory_store[consumer_id]

        # Hourly check
        hour_count = sum(1 for t in timestamps if t > now - 3600)
        if hour_count >= max_per_hour:
            return False, f"Hourly limit exceeded ({hour_count}/{max_per_hour})"

        # Daily check
        day_count = len(timestamps)
        if day_count >= max_per_day:
            return False, f"Daily limit exceeded ({day_count}/{max_per_day})"

        # Increment
        self._memory_store[consumer_id].append(now)

        return True, "ok"

    def _get_usage_memory(self, consumer_id: str) -> Dict[str, int]:
        """Получить использование из памяти."""
        now = time.time()
        timestamps = self._memory_store.get(consumer_id, [])

        hour_count = sum(1 for t in timestamps if t > now - 3600)
        day_count = sum(1 for t in timestamps if t > now - 86400)

        return {"queries_this_hour": hour_count, "queries_today": day_count}
