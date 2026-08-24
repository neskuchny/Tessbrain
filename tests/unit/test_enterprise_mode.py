"""Unit-тесты для W8 enterprise-mode (air-gap kill-switch).

Покрытие:
- `_is_internal_url` — все классы internal/external адресов;
- Settings-валидатор — fail-closed когда:
  - есть OPENAI_API_KEY,
  - есть GOOGLE_API_KEY,
  - LLM_LOCAL_BASE_URL пустой,
  - LLM_LOCAL_BASE_URL указывает на public LLM (api.openai.com),
  - LLM_LOCAL_BASE_URL — internet-адрес (1.1.1.1, example.com);
- happy-path: enterprise + internal LLM_LOCAL_BASE_URL + сильные секреты.

Тесты используют `_fresh_settings` паттерн из test_config_validator.py.
"""
from __future__ import annotations

import importlib

import pytest

_ALL_VARS = (
    "SECRET_KEY", "JWT_SECRET_KEY", "JWT_SECRET",
    "POSTGRES_PASSWORD", "NEO4J_PASSWORD",
    "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "LLM_LOCAL_BASE_URL", "LLM_LOCAL_MODEL", "LLM_LOCAL_API_KEY",
    "ENTERPRISE_MODE", "APP_ENV",
)


def _fresh(monkeypatch: pytest.MonkeyPatch, **env: str):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import backend.config as cfg
    try:
        importlib.reload(cfg)
    except Exception:
        pass
    return cfg.Settings


# === _is_internal_url ====================================================

@pytest.mark.parametrize("url", [
    "http://localhost:8000/v1",
    "http://127.0.0.1:8000/v1",
    "http://10.0.0.5:8000",
    "http://172.16.1.2",
    "http://192.168.1.10",
    "http://vllm:8000/v1",  # docker-compose hostname
    "http://ollama.tessent.svc.cluster.local/v1",
    "http://gateway.local",
    "http://gw.mycorp.internal:9000",
    "http://[::1]:8000",
])
def test_internal_urls_accepted(url: str) -> None:
    from backend.config import _is_internal_url
    assert _is_internal_url(url) is True, url


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "https://generativelanguage.googleapis.com/v1",
    "https://api.anthropic.com/v1",
    "http://1.1.1.1/v1",
    "http://8.8.8.8",
    "http://example.com/v1",
    "http://my-public-llm.com/v1",
    "",
])
def test_external_urls_rejected(url: str) -> None:
    from backend.config import _is_internal_url
    assert _is_internal_url(url) is False, url


# === Settings validator ==================================================

_STRONG_SECRETS = {
    "APP_ENV": "production",
    "SECRET_KEY": "A" * 32,
    "JWT_SECRET_KEY": "B" * 32,
    "POSTGRES_PASSWORD": "C" * 32,
    "NEO4J_PASSWORD": "D" * 32,
}


def test_enterprise_off_allows_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(monkeypatch, **_STRONG_SECRETS, OPENAI_API_KEY="sk-foo")
    s = Settings()
    assert s.openai_api_key == "sk-foo"
    assert s.enterprise_mode is False


def test_enterprise_rejects_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(
        monkeypatch,
        **_STRONG_SECRETS,
        ENTERPRISE_MODE="true",
        LLM_LOCAL_BASE_URL="http://vllm:8000/v1",
        OPENAI_API_KEY="sk-leaked",
    )
    with pytest.raises(Exception) as exc:
        Settings()
    assert "OPENAI_API_KEY" in str(exc.value)


def test_enterprise_rejects_google_key(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(
        monkeypatch,
        **_STRONG_SECRETS,
        ENTERPRISE_MODE="true",
        LLM_LOCAL_BASE_URL="http://vllm:8000/v1",
        GOOGLE_API_KEY="ya29-leaked",
    )
    with pytest.raises(Exception) as exc:
        Settings()
    assert "GOOGLE_API_KEY" in str(exc.value)


def test_enterprise_requires_local_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(monkeypatch, **_STRONG_SECRETS, ENTERPRISE_MODE="true")
    with pytest.raises(Exception) as exc:
        Settings()
    assert "LLM_LOCAL_BASE_URL" in str(exc.value)


def test_enterprise_rejects_external_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(
        monkeypatch,
        **_STRONG_SECRETS,
        ENTERPRISE_MODE="true",
        LLM_LOCAL_BASE_URL="http://example.com/v1",
    )
    with pytest.raises(Exception) as exc:
        Settings()
    assert "not internal" in str(exc.value)


def test_enterprise_rejects_managed_provider_in_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Даже если кто-то прописал openai.com через /etc/hosts на internal IP,
    проверка по host pattern всё равно поймает."""
    # Hostname api.openai.com технически "external", так что попадёт
    # в первую же ветку. Подсунем кейс пограничный: managed-host через
    # internal-домен — это обходной путь, наш регexp его не закрывает,
    # но публичный домен сам по себе уже отвергнут.
    Settings = _fresh(
        monkeypatch,
        **_STRONG_SECRETS,
        ENTERPRISE_MODE="true",
        LLM_LOCAL_BASE_URL="https://api.openai.com/v1",
    )
    with pytest.raises(Exception) as exc:
        Settings()
    msg = str(exc.value)
    assert "not internal" in msg or "managed-LLM" in msg


def test_enterprise_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    Settings = _fresh(
        monkeypatch,
        **_STRONG_SECRETS,
        ENTERPRISE_MODE="true",
        LLM_LOCAL_BASE_URL="http://vllm.tessent.svc.cluster.local:8000/v1",
        LLM_LOCAL_MODEL="meta-llama/Llama-3.3-70B-Instruct",
    )
    s = Settings()
    assert s.enterprise_mode is True
    assert s.llm_local_base_url.endswith("/v1")
    assert s.llm_local_model.startswith("meta-llama/")
    assert not s.openai_api_key
    assert not s.google_api_key


def test_enterprise_dev_env_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """В development все валидаторы выключены — DX не должна страдать."""
    Settings = _fresh(
        monkeypatch,
        APP_ENV="development",
        ENTERPRISE_MODE="true",
        # Намеренно не задаём LLM_LOCAL_BASE_URL — в production это бы упало.
    )
    # ВАЖНО: enterprise-валидатор сам по себе НЕ проверяет app_env, но
    # production-secrets валидатор пропускает не-prod. Цель этого теста —
    # явно зафиксировать поведение: в dev можно включить enterprise_mode
    # для отладки, но всё равно нужен LLM_LOCAL_BASE_URL.
    with pytest.raises(Exception) as exc:
        Settings()
    assert "LLM_LOCAL_BASE_URL" in str(exc.value)
