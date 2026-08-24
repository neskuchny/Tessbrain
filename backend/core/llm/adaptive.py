# -*- coding: utf-8 -*-
"""
Адаптивная одновременность для LLM-задач (AIMD).

Зачем: при параллельном запуске множества LLM-вызовов (напр. 13 категорий
извлечения знаний) фиксированный Semaphore либо слишком осторожен (медленно),
либо пробивает rate-limit модели → 429/ResourceExhausted → задача падает и
данные теряются.

AdaptiveConcurrency регулирует лимит сам:
  • стартует с заданного значения;
  • при rate-limit ошибке МУЛЬТИПЛИКАТИВНО сжимает лимит (÷2, не ниже min) и
    повторяет ту же задачу с экспоненциальным backoff — «не выпадает в ошибку»;
  • при серии успехов АДДИТИВНО восстанавливает лимит (+1, не выше max).

Это классический AIMD (как TCP congestion control): быстро отступаем под
нагрузкой, осторожно наращиваем при стабильности.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Маркеры rate-limit / перегрузки провайдера (Gemini/OpenAI/общие).
_RATE_LIMIT_MARKERS = (
    "429", "resource_exhausted", "resourceexhausted", "quota",
    "rate limit", "rate_limit", "ratelimit", "too many requests",
    "overloaded", "503", "unavailable", "try again later",
)


def is_rate_limit_error(exc: BaseException) -> bool:
    """True, если ошибка похожа на rate-limit / перегрузку провайдера."""
    msg = str(exc).lower()
    return any(m in msg for m in _RATE_LIMIT_MARKERS)


class AdaptiveConcurrency:
    """AIMD-контроллер одновременности для пачки LLM-задач."""

    def __init__(
        self,
        start: int,
        *,
        min_limit: int = 1,
        max_limit: int | None = None,
        backoff_base: float = 1.0,
        max_attempts: int = 4,
    ):
        self.max_limit = max(1, max_limit if max_limit is not None else start)
        self.min_limit = max(1, min_limit)
        self._limit = max(self.min_limit, min(start, self.max_limit))
        self._active = 0
        self._success_streak = 0
        self._cond = asyncio.Condition()
        self.backoff_base = backoff_base
        self.max_attempts = max_attempts
        # Диагностика: минимальный лимит, до которого опускались, и число
        # rate-limit срабатываний за время жизни контроллера.
        self.min_seen = self._limit
        self.rate_limit_hits = 0

    @property
    def limit(self) -> int:
        return self._limit

    async def _acquire(self) -> None:
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def _release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    async def _shrink(self) -> None:
        async with self._cond:
            new = max(self.min_limit, self._limit // 2)
            if new != self._limit:
                self._limit = new
                self.min_seen = min(self.min_seen, new)
                logger.warning(f"⚙️ AdaptiveConcurrency: rate-limit → снижаю одновременность до {new}")
            self._success_streak = 0
            self.rate_limit_hits += 1

    async def _grow(self) -> None:
        async with self._cond:
            self._success_streak += 1
            # Растём осторожно: +1 после streak ≈ 2×текущий лимит успехов.
            if self._limit < self.max_limit and self._success_streak >= self._limit * 2:
                self._limit += 1
                self._success_streak = 0
                self._cond.notify_all()

    async def run(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        """Выполнить задачу под адаптивным лимитом.

        coro_factory должен создавать НОВЫЙ awaitable на каждую попытку
        (корутину нельзя await-ить дважды).
        """
        attempt = 0
        while True:
            await self._acquire()
            try:
                result = await coro_factory()
            except Exception as exc:  # noqa: BLE001 — классифицируем ниже
                await self._release()
                if is_rate_limit_error(exc) and attempt < self.max_attempts:
                    attempt += 1
                    await self._shrink()
                    delay = self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    await asyncio.sleep(delay)
                    continue
                raise
            else:
                await self._release()
                await self._grow()
                return result
