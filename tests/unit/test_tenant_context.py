"""Unit-тесты для observability.tenant_context (W4 phase 7a).

Проверяет lifecycle contextvar: дефолт None, set/get/reset, изоляция между
asyncio-задачами (contextvars автоматически клонируется для каждого task).
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.observability.tenant_context import (
    get_current_tenant,
    require_current_tenant,
    reset_current_tenant,
    set_current_tenant,
)


def test_default_is_none() -> None:
    assert get_current_tenant() is None


def test_set_and_get() -> None:
    token = set_current_tenant("tenant-A")
    try:
        assert get_current_tenant() == "tenant-A"
    finally:
        reset_current_tenant(token)
    assert get_current_tenant() is None


def test_require_raises_when_unset() -> None:
    with pytest.raises(RuntimeError, match="tenant_id is not set"):
        require_current_tenant()


def test_require_returns_when_set() -> None:
    token = set_current_tenant("tenant-B")
    try:
        assert require_current_tenant() == "tenant-B"
    finally:
        reset_current_tenant(token)


def test_isolation_between_tasks() -> None:
    """asyncio.create_task копирует current contextvars → дочерняя задача
    видит родительский tenant, но изменения не утекают в обратном направлении.
    """
    async def child() -> str | None:
        # Дочерняя задача видит родительский tenant.
        observed = get_current_tenant()
        # Изменение здесь не должно повлиять на родителя.
        token = set_current_tenant("child-only")
        try:
            return observed
        finally:
            reset_current_tenant(token)

    async def parent() -> str | None:
        token = set_current_tenant("parent")
        try:
            child_observed = await asyncio.create_task(child())
            assert child_observed == "parent"  # ребёнок унаследовал
            return get_current_tenant()         # родитель не задет
        finally:
            reset_current_tenant(token)

    assert asyncio.run(parent()) == "parent"


def test_nested_tokens_unwind_in_lifo_order() -> None:
    t1 = set_current_tenant("a")
    t2 = set_current_tenant("b")
    assert get_current_tenant() == "b"
    reset_current_tenant(t2)
    assert get_current_tenant() == "a"
    reset_current_tenant(t1)
    assert get_current_tenant() is None
