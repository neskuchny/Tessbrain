"""W19: enterprise validator должен проверять executor_backend."""
from __future__ import annotations

import importlib

import pytest

_VARS = (
    "SECRET_KEY", "JWT_SECRET_KEY", "POSTGRES_PASSWORD", "NEO4J_PASSWORD",
    "OPENAI_API_KEY", "GOOGLE_API_KEY",
    "LLM_LOCAL_BASE_URL", "ENTERPRISE_MODE", "APP_ENV",
    "EXECUTOR_BACKEND", "OPENHANDS_BASE_URL",
)


def _fresh(monkeypatch: pytest.MonkeyPatch, **env: str):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import backend.config as cfg
    try:
        importlib.reload(cfg)
    except Exception:
        pass
    return cfg.Settings


_BASE_PROD = {
    "APP_ENV": "production",
    "SECRET_KEY": "A" * 32,
    "JWT_SECRET_KEY": "B" * 32,
    "POSTGRES_PASSWORD": "C" * 32,
    "NEO4J_PASSWORD": "D" * 32,
    "ENTERPRISE_MODE": "true",
    "LLM_LOCAL_BASE_URL": "http://vllm.svc.cluster.local:8000/v1",
}


def test_enterprise_rejects_claude_code_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(monkeypatch, **_BASE_PROD, EXECUTOR_BACKEND="claude_code_cli")
    with pytest.raises(Exception) as exc:
        Settings()
    assert "claude_code_cli" in str(exc.value)


def test_enterprise_rejects_cursor_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(monkeypatch, **_BASE_PROD, EXECUTOR_BACKEND="cursor_cli")
    with pytest.raises(Exception) as exc:
        Settings()
    assert "cursor_cli" in str(exc.value)


def test_enterprise_openhands_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(monkeypatch, **_BASE_PROD, EXECUTOR_BACKEND="openhands")
    with pytest.raises(Exception) as exc:
        Settings()
    assert "OPENHANDS_BASE_URL" in str(exc.value)


def test_enterprise_openhands_rejects_external_url(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(
        monkeypatch, **_BASE_PROD,
        EXECUTOR_BACKEND="openhands",
        OPENHANDS_BASE_URL="https://api.example.com",
    )
    with pytest.raises(Exception) as exc:
        Settings()
    msg = str(exc.value)
    assert "OPENHANDS_BASE_URL" in msg
    assert "not internal" in msg


def test_enterprise_openhands_internal_url_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(
        monkeypatch, **_BASE_PROD,
        EXECUTOR_BACKEND="openhands",
        OPENHANDS_BASE_URL="http://openhands:3000",
    )
    s = Settings()
    assert s.executor_backend == "openhands"


def test_enterprise_noop_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(monkeypatch, **_BASE_PROD, EXECUTOR_BACKEND="noop")
    s = Settings()
    assert s.executor_backend == "noop"


def test_enterprise_off_allows_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без enterprise_mode managed-executors разрешены."""
    Settings = _fresh(
        monkeypatch,
        APP_ENV="production",
        SECRET_KEY="A" * 32,
        JWT_SECRET_KEY="B" * 32,
        POSTGRES_PASSWORD="C" * 32,
        NEO4J_PASSWORD="D" * 32,
        EXECUTOR_BACKEND="claude_code_cli",
    )
    s = Settings()
    assert s.executor_backend == "claude_code_cli"
