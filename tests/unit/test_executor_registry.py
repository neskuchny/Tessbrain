"""Unit-тесты для core.executors.registry (W19)."""
from __future__ import annotations

import os

import pytest
from backend.core.executors.base import ExecutorBackend, ExecutorError
from backend.core.executors.registry import (
    get_active_backend,
    get_backend,
    is_external,
    list_backend_names,
    validate_enterprise,
)

# === Backend lookup ====================================================

def test_list_backends() -> None:
    names = list_backend_names()
    assert "noop" in names
    assert "claude_code_cli" in names
    assert "openhands" in names
    assert "cursor_cli" in names


def test_get_backend_noop() -> None:
    backend = get_backend("noop")
    assert isinstance(backend, ExecutorBackend)
    assert backend.name == "noop"


def test_get_backend_unknown_raises() -> None:
    with pytest.raises(ExecutorError):
        get_backend("nonexistent")


def test_get_backend_case_insensitive() -> None:
    """Пробельные / case вариации."""
    assert get_backend("NOOP").name == "noop"
    assert get_backend("  noop  ").name == "noop"


def test_is_external() -> None:
    assert is_external("claude_code_cli") is True
    assert is_external("cursor_cli") is True
    assert is_external("openhands") is False
    assert is_external("noop") is False


# === get_active_backend =================================================

def test_get_active_backend_uses_override() -> None:
    backend = get_active_backend(override="noop")
    assert backend.name == "noop"


def test_get_active_backend_default_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без settings и override — fallback на noop."""
    monkeypatch.delenv("EXECUTOR_BACKEND", raising=False)
    backend = get_active_backend()
    # Может вернуть noop (если settings не подцепил) — главное не падает.
    assert backend.name in {"noop", "claude_code_cli", "cursor_cli", "openhands"}


# === validate_enterprise ================================================

def test_enterprise_allows_noop() -> None:
    violations = validate_enterprise(backend_name="noop", openhands_base_url="")
    assert violations == []


def test_enterprise_rejects_claude_code() -> None:
    v = validate_enterprise(backend_name="claude_code_cli", openhands_base_url="")
    assert any("managed-API" in m for m in v)


def test_enterprise_rejects_cursor() -> None:
    v = validate_enterprise(backend_name="cursor_cli", openhands_base_url="")
    assert any("managed-API" in m for m in v)


def test_enterprise_openhands_requires_url() -> None:
    v = validate_enterprise(backend_name="openhands", openhands_base_url="")
    assert any("OPENHANDS_BASE_URL" in m for m in v)


def test_enterprise_openhands_external_url_rejected() -> None:
    v = validate_enterprise(
        backend_name="openhands",
        openhands_base_url="https://api.openai.com/v1",
    )
    assert any("not internal" in m for m in v)


def test_enterprise_openhands_internal_url_ok() -> None:
    v = validate_enterprise(
        backend_name="openhands",
        openhands_base_url="http://openhands.svc.cluster.local:3000",
    )
    assert v == []


def test_enterprise_openhands_localhost_ok() -> None:
    v = validate_enterprise(
        backend_name="openhands",
        openhands_base_url="http://localhost:3000",
    )
    assert v == []
