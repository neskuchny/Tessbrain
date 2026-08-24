"""Unit-тесты для OpenHandsExecutor (W19).

Без сети — мокаем httpx. Покрываем _map_oh_status, init validation,
status/result mapping.
"""
from __future__ import annotations

import asyncio

import pytest
from backend.core.executors.backends.openhands import (
    OpenHandsExecutor,
    _map_oh_status,
)
from backend.core.executors.base import ExecutorError, TaskStatus

# === _map_oh_status =====================================================

@pytest.mark.parametrize("oh,expected", [
    ("queued", TaskStatus.QUEUED),
    ("pending", TaskStatus.QUEUED),
    ("STARTING", TaskStatus.QUEUED),
    ("running", TaskStatus.RUNNING),
    ("in_progress", TaskStatus.RUNNING),
    ("active", TaskStatus.RUNNING),
    ("finished", TaskStatus.DONE),
    ("done", TaskStatus.DONE),
    ("completed", TaskStatus.DONE),
    ("success", TaskStatus.DONE),
    ("failed", TaskStatus.FAILED),
    ("error", TaskStatus.FAILED),
    ("crashed", TaskStatus.FAILED),
    ("cancelled", TaskStatus.CANCELLED),
    ("canceled", TaskStatus.CANCELLED),
    ("stopped", TaskStatus.CANCELLED),
    ("unknown_state", TaskStatus.UNKNOWN),
    ("", TaskStatus.UNKNOWN),
])
def test_map_oh_status(oh: str, expected: TaskStatus) -> None:
    assert _map_oh_status(oh) == expected


# === Init ===============================================================

def test_init_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENHANDS_BASE_URL", raising=False)
    with pytest.raises(ExecutorError):
        OpenHandsExecutor()


def test_init_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_BASE_URL", "http://oh:3000/")
    e = OpenHandsExecutor()
    assert e.base_url == "http://oh:3000"


def test_init_picks_up_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_BASE_URL", "http://oh:3000")
    monkeypatch.setenv("OPENHANDS_API_KEY", "secret-token")
    e = OpenHandsExecutor()
    assert e.api_key == "secret-token"
    assert "Authorization" in e._headers()


def test_init_explicit_args_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_BASE_URL", "http://from-env:3000")
    e = OpenHandsExecutor(base_url="http://override:3000")
    assert e.base_url == "http://override:3000"


def test_headers_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENHANDS_BASE_URL", "http://oh:3000")
    monkeypatch.delenv("OPENHANDS_API_KEY", raising=False)
    e = OpenHandsExecutor()
    headers = e._headers()
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"
