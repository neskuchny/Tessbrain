"""Unit-тесты для db.postgres_tenant._is_safe_tenant_id (W4 phase 7e).

Проверяет regex-валидацию tenant_id перед SET LOCAL — защита от
SQL-injection через JWT/header.

Импорт сделан через importlib.util в обход backend/db/__init__.py — тот
тащит supabase_client со всеми зависимостями (httpx, tenacity), которые
не нужны для проверки одной regex-функции.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "db" / "postgres_tenant.py"
)
_spec = importlib.util.spec_from_file_location("_postgres_tenant_under_test", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_is_safe_tenant_id = _module._is_safe_tenant_id


@pytest.mark.parametrize("value", [
    "tenant-A",
    "abc123",
    "00000000-0000-0000-0000-000000000000",  # uuid
    "ACME_corp",
    "x",
    "a-b-c",
    "0",
    "A1B2",
])
def test_safe_values_pass(value: str) -> None:
    assert _is_safe_tenant_id(value) is True


@pytest.mark.parametrize("value", [
    "",                                       # пустое
    "tenant'; DROP TABLE users; --",          # классический SQL-injection
    "tenant\"",                               # двойная кавычка
    "tenant\nnewline",                        # перевод строки
    "tenant with spaces",
    "tenant.with.dots",                       # точки запрещены
    "tenant/slash",
    "tenant\\backslash",
    "tenant)",
    "tenant(",
    "a" * 65,                                 # > 64 символов — отклоняем
    "русский",                                # non-ASCII
    "tenant;commit",
])
def test_unsafe_values_rejected(value: str) -> None:
    assert _is_safe_tenant_id(value) is False


def test_max_length_boundary() -> None:
    """Допустимая граница — 64 символа."""
    assert _is_safe_tenant_id("a" * 64) is True
    assert _is_safe_tenant_id("a" * 65) is False
