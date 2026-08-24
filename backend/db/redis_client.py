"""
TESSENT BRAIN - Redis Client
Асинхронный клиент для Redis (кеш, очереди, сессии)
"""
from typing import Any

import orjson
import redis.asyncio as aioredis
import structlog

from backend.config import settings

logger = structlog.get_logger(__name__)


class RedisClient:
    """Асинхронный клиент Redis"""

    def __init__(self):
        self.client: aioredis.Redis | None = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Инициализация подключения"""
        try:
            self.client = await aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            # Проверка подключения
            await self.client.ping()
            self._initialized = True
            logger.info("✅ Redis initialized", url=settings.redis_url)
            return True
        except Exception as e:
            logger.error("❌ Redis initialization failed", error=str(e))
            return False

    async def get(self, key: str) -> Any | None:
        """Получить значение по ключу"""
        if not self.client:
            return None
        value = await self.client.get(key)
        if value:
            try:
                return orjson.loads(value)
            except Exception:
                return value
        return None

    async def set(
        self,
        key: str,
        value: Any,
        expire: int | None = None
    ) -> bool:
        """Установить значение с опциональным TTL"""
        if not self.client:
            return False
        try:
            if isinstance(value, (dict, list)):
                value = orjson.dumps(value).decode()
            await self.client.set(key, value, ex=expire)
            return True
        except Exception as e:
            logger.error("Redis set error", key=key, error=str(e))
            return False

    async def delete(self, key: str) -> bool:
        """Удалить ключ"""
        if not self.client:
            return False
        await self.client.delete(key)
        return True

    async def exists(self, key: str) -> bool:
        """Проверить существование ключа"""
        if not self.client:
            return False
        return await self.client.exists(key) > 0

    async def health_check(self) -> bool:
        """Проверка здоровья подключения"""
        try:
            if self.client:
                await self.client.ping()
                return True
            return False
        except Exception:
            return False

    async def close(self):
        """Закрытие подключения"""
        if self.client:
            await self.client.close()
            logger.info("Redis connection closed")


# Singleton instance
_redis_client: RedisClient | None = None


async def get_redis() -> RedisClient:
    """Получить клиент Redis"""
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
        await _redis_client.initialize()
    return _redis_client



