"""Unit-тесты для backend.config (W1 phase 0) — production-secrets validator.

Дублирует CI-job `config-smoke`, но в виде нормального pytest-теста, чтобы
покрытие было видимо в coverage report.
"""
from __future__ import annotations

import importlib

import pytest

_SECRET_VARS = (
    "SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET",
    "POSTGRES_PASSWORD", "NEO4J_PASSWORD",
)


def _fresh_settings(monkeypatch: pytest.MonkeyPatch, **env: str):
    """Перезагрузить backend.config с заданным env, вернуть Settings класс.

    Сначала удаляем все известные секрет-env, затем выставляем только
    переданные. Reload может выкинуть ValidationError на module-level
    `settings = get_settings()` — это нормально для production-ветки;
    глотаем его и возвращаем класс, чтобы тест мог сам вызвать Settings()
    под pytest.raises.
    """
    for var in _SECRET_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import backend.config as cfg
    try:
        importlib.reload(cfg)
    except Exception:
        # Module-level Settings() упало на валидаторе — сам класс уже
        # импортирован, возвращаем его.
        pass
    return cfg.Settings


def test_dev_accepts_default_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh_settings(monkeypatch, APP_ENV="development")
    s = Settings()
    assert s.app_env == "development"
    # Дефолтные значения остаются.
    assert s.secret_key == "change-me-in-production"


def test_production_rejects_default_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh_settings(monkeypatch, APP_ENV="production")
    with pytest.raises(Exception) as exc_info:
        Settings()
    assert "Insecure default secrets" in str(exc_info.value)
    assert "SECRET_KEY" in str(exc_info.value)


def test_production_accepts_strong_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh_settings(
        monkeypatch,
        APP_ENV="production",
        SECRET_KEY="A" * 32,
        JWT_SECRET_KEY="B" * 32,
        POSTGRES_PASSWORD="C" * 32,
        NEO4J_PASSWORD="D" * 32,
    )
    s = Settings()
    assert s.app_env == "production"
    assert s.secret_key == "A" * 32


def test_production_lists_all_offenders(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh_settings(
        monkeypatch,
        APP_ENV="production",
        # SECRET_KEY переопределён, остальные — на дефолте → должны попасть в список.
        SECRET_KEY="A" * 32,
    )
    with pytest.raises(Exception) as exc_info:
        Settings()
    msg = str(exc_info.value)
    assert "SECRET_KEY" not in msg or "JWT_SECRET_KEY" in msg  # SECRET_KEY валиден, JWT — нет
    for offender in ("JWT_SECRET_KEY", "POSTGRES_PASSWORD", "NEO4J_PASSWORD"):
        assert offender in msg


def test_staging_treated_as_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """staging — Literal['development','staging','production'] но в логике
    валидатор включается ТОЛЬКО при app_env == 'production'."""
    Settings = _fresh_settings(monkeypatch, APP_ENV="staging")
    # Должно пройти даже на дефолтных секретах.
    s = Settings()
    assert s.app_env == "staging"
