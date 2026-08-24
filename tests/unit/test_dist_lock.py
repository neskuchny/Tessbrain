"""Unit-тесты для observability.dist_lock (W2 phase 5).

Проверяем fail-open поведение, когда Redis недоступен. Полные
acquire/release Redis-сценарии требуют живого Redis и относятся к
интеграционным тестам.
"""
from __future__ import annotations

import asyncio

from backend.core.observability.dist_lock import try_acquire


def test_fail_open_when_redis_unavailable() -> None:
    """Если backend.db.redis_client.get_redis() недоступен → yield True
    (fail-open). Это сознательное поведение для single-replica дев-режима.
    """
    async def go() -> bool:
        async with try_acquire("test-lock", ttl_seconds=60) as got:
            return got

    assert asyncio.run(go()) is True


def test_namespace_in_key_format() -> None:
    """Smoke: import-модуля не падает; конструкция try_acquire не валится
    на синтаксис аргументов.
    """
    async def go() -> bool:
        async with try_acquire("nightly", namespace="prod_lock", ttl_seconds=10) as got:
            return got

    assert asyncio.run(go()) is True
