"""Unit-тесты для core.llm.prompt_cache (W2 phase 4b).

Проверяем:
- Стабильность ключа от инпутов.
- Гейт по temperature: только t=0 кэшируется.
- Fail-open при недоступности Redis.
- Stats counters.
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.llm import prompt_cache
from backend.core.llm.prompt_cache import (
    _key,
    get_cache_stats,
    get_cached_response,
    store_response,
)


def test_key_stable_for_same_inputs() -> None:
    k1 = _key("gpt-4o", "hello", "be helpful", 0.0)
    k2 = _key("gpt-4o", "hello", "be helpful", 0.0)
    assert k1 == k2


def test_key_differs_when_model_changes() -> None:
    k1 = _key("gpt-4o", "hello", "sys", 0.0)
    k2 = _key("gpt-4o-mini", "hello", "sys", 0.0)
    assert k1 != k2


def test_key_differs_when_system_prompt_changes() -> None:
    k1 = _key("m", "p", "sys-A", 0.0)
    k2 = _key("m", "p", "sys-B", 0.0)
    assert k1 != k2


def test_key_includes_temperature() -> None:
    k1 = _key("m", "p", "s", 0.0)
    k2 = _key("m", "p", "s", 0.7)
    assert k1 != k2


def test_key_handles_none_system_prompt() -> None:
    # None и "" не должны различаться (оба обнуляются в логике _key).
    k1 = _key("m", "p", None, 0.0)
    k2 = _key("m", "p", "", 0.0)
    assert k1 == k2


def test_temperature_above_zero_skips_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не-детерминированный путь (t > 0) не должен ни читать, ни писать кэш."""
    # Reset stats для чистоты.
    monkeypatch.setattr(prompt_cache, "_stats", {"lookups": 0, "hits": 0, "stores": 0, "errors": 0})

    async def go() -> None:
        r = await get_cached_response(model="m", prompt="p", system_prompt=None, temperature=0.7)
        assert r is None
        await store_response(model="m", prompt="p", system_prompt=None, temperature=0.7, response="x")

    asyncio.run(go())
    stats = get_cache_stats()
    # Никаких lookups (мы вышли early до счётчика) и никаких stores.
    assert stats["lookups"] == 0
    assert stats["stores"] == 0


def test_redis_unreachable_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если backend.db.redis_client.get_redis() падает, get_cached_response
    возвращает None и не падает; store_response — no-op."""
    monkeypatch.setattr(prompt_cache, "_stats", {"lookups": 0, "hits": 0, "stores": 0, "errors": 0})

    async def go() -> None:
        # При temperature=0 кэш активен. Redis у нас отсутствует → тихий None.
        r = await get_cached_response(model="m", prompt="p", system_prompt=None, temperature=0.0)
        assert r is None
        await store_response(model="m", prompt="p", system_prompt=None, temperature=0.0, response="x")

    asyncio.run(go())
    stats = get_cache_stats()
    # lookup произошёл, но Redis недоступен → 0 hits, 0 stores.
    assert stats["lookups"] == 1
    assert stats["hits"] == 0
    assert stats["stores"] == 0


def test_stats_reports_hit_rate_zero_when_no_lookups() -> None:
    # Если lookups=0, hit_rate должен быть валидным числом, не div-by-zero.
    fresh = {"lookups": 0, "hits": 0, "stores": 0, "errors": 0}
    import backend.core.llm.prompt_cache as pc
    pc._stats = fresh
    stats = get_cache_stats()
    assert stats["hit_rate"] == 0.0
