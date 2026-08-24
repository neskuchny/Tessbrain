"""Unit-тесты для core.llm.active_profile + ProfileBackedClient (W42b)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.llm.active_profile import (
    ActiveProfileResolver,
    get_active_profile_resolver,
    invalidate_active_profile,
    reset_resolver,
)
from backend.core.llm.profile_client import ProfileBackedClient


def _run(coro):
    return asyncio.run(coro)


# === ProfileBackedClient.enabled detection =============================

def test_turboquant_needs_base_url_and_model() -> None:
    c = ProfileBackedClient(
        provider="turboquant", base_url=None, model="p", api_key=None,
    )
    assert c.enabled is False

    c2 = ProfileBackedClient(
        provider="turboquant", base_url="https://x", model="p", api_key="dev-token",
    )
    assert c2.enabled is True


def test_openai_needs_api_key() -> None:
    c = ProfileBackedClient(
        provider="openai", base_url=None, model="gpt-4o-mini", api_key=None,
    )
    assert c.enabled is False
    c2 = ProfileBackedClient(
        provider="openai", base_url=None, model="gpt-4o-mini", api_key="sk-x",
    )
    assert c2.enabled is True


def test_runpod_needs_base_url_and_key() -> None:
    c_no_url = ProfileBackedClient(
        provider="runpod", base_url=None, model="qwen", api_key="rp-key",
    )
    assert c_no_url.enabled is False
    c_no_key = ProfileBackedClient(
        provider="runpod", base_url="https://x/v1", model="qwen", api_key=None,
    )
    assert c_no_key.enabled is False
    c_ok = ProfileBackedClient(
        provider="runpod", base_url="https://x/v1", model="qwen", api_key="rp-key",
    )
    assert c_ok.enabled is True


def test_local_vllm_needs_base_url_only() -> None:
    c_no_url = ProfileBackedClient(
        provider="local_vllm", base_url=None, model="qwen", api_key=None,
    )
    assert c_no_url.enabled is False
    c_ok = ProfileBackedClient(
        provider="local_vllm", base_url="http://localhost:8000/v1",
        model="qwen", api_key=None,
    )
    assert c_ok.enabled is True


def test_gemini_needs_api_key() -> None:
    c = ProfileBackedClient(
        provider="gemini", base_url=None, model="gemini-2.0-flash", api_key=None,
    )
    assert c.enabled is False
    c2 = ProfileBackedClient(
        provider="gemini", base_url=None, model="gemini-2.0-flash",
        api_key="AIzaSy...",
    )
    assert c2.enabled is True


def test_unsupported_provider_disabled() -> None:
    # anthropic давно поддержан нативным SDK (BYOK на нём работает) — тест
    # проверяет ДЕЙСТВИТЕЛЬНО неизвестного провайдера.
    c = ProfileBackedClient(
        provider="gigachat", base_url="https://api", model="giga", api_key="k",
    )
    assert c.enabled is False


def test_empty_provider_disabled() -> None:
    c = ProfileBackedClient(
        provider="", base_url="x", model="y", api_key="z",
    )
    assert c.enabled is False


# === BaseLLMClient compatibility =======================================

def test_model_and_api_key_propagated() -> None:
    c = ProfileBackedClient(
        provider="turboquant", base_url="https://x",
        model="gemma4_e4b_it_w4a16", api_key="dev-token",
    )
    assert c.model == "gemma4_e4b_it_w4a16"
    assert c.api_key == "dev-token"


def test_stats_initialized() -> None:
    c = ProfileBackedClient(
        provider="turboquant", base_url="https://x", model="p", api_key="t",
    )
    s = c.get_stats()
    assert s["model"] == "p"
    assert s["enabled"] is True


# === Lazy backend init для turboquant ==================================

def test_lazy_backend_turboquant(monkeypatch) -> None:
    """Backend инициализируется только при первом generate()."""
    c = ProfileBackedClient(
        provider="turboquant", base_url="https://x",
        model="gemma4_e4b_it_w4a16", api_key="dev-token",
    )
    assert c._backend is None
    backend = _run(c._ensure_backend())
    assert backend is not None
    from backend.core.llm.turboquant_client import TurboQuantClient
    assert isinstance(backend, TurboQuantClient)
    assert backend.profile_id == "gemma4_e4b_it_w4a16"


# === ProfileBackedClient.generate (via TurboQuant backend mocked) ======


class _FakeAsyncResp:
    def __init__(self, json_data) -> None:
        self.status_code = 200
        self._json = json_data
        self.text = ""

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


class _FakeAsyncClient:
    def __init__(self, response, capture) -> None:
        self._response = response
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, url, *, headers=None, json=None):
        self._capture.update({"url": url, "body": json or {}, "headers": headers or {}})
        return self._response


def test_generate_turboquant_via_profile_client(monkeypatch) -> None:
    """End-to-end: ProfileBackedClient(turboquant) → TurboQuantClient → fake HTTP."""
    import httpx

    captured: dict = {}
    fake = _FakeAsyncResp({
        "response": {
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    })

    def _factory(*args, **kwargs):
        return _FakeAsyncClient(fake, captured)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    c = ProfileBackedClient(
        provider="turboquant", base_url="https://example",
        model="profile-1", api_key="dev-token",
    )
    out = _run(c.generate("hi", system_prompt="be nice"))
    assert out == "answer"
    assert captured["url"] == "https://example/v1/chat/completions"
    assert captured["body"]["profile_id"] == "profile-1"


# === ActiveProfileResolver ============================================

def test_resolver_returns_none_when_no_profile() -> None:
    """Без подключения к БД resolver не находит профиль."""
    reset_resolver()
    r = ActiveProfileResolver()
    assert _run(r.get_active_client()) is None


def test_invalidate_clears_cache() -> None:
    reset_resolver()
    r = ActiveProfileResolver()
    # Никаких профилей → None
    assert _run(r.get_active_client()) is None
    _run(r.invalidate())
    assert _run(r.current_profile_id()) is None


def test_global_resolver_singleton() -> None:
    reset_resolver()
    a = get_active_profile_resolver()
    b = get_active_profile_resolver()
    assert a is b


def test_reset_resolver_creates_new_instance() -> None:
    a = get_active_profile_resolver()
    reset_resolver()
    b = get_active_profile_resolver()
    assert a is not b


def test_invalidate_active_profile_public_hook() -> None:
    """Public hook не должен падать даже без resolver state."""
    reset_resolver()
    _run(invalidate_active_profile())


# === Resolver caches built client =====================================


def test_resolver_caches_client(monkeypatch) -> None:
    """Если БД даёт active profile, resolver кэширует client между вызовами."""
    reset_resolver()
    r = ActiveProfileResolver()

    # Мокаем _load_active_profile_and_key чтобы вернуть fake profile.
    class _FakeProfile:
        id = "llm_test"
        provider = "local_vllm"
        base_url = "http://localhost:8000/v1"
        model = "qwen"
        config = {}
        has_api_key = False

    async def _fake_load():
        return _FakeProfile(), None

    monkeypatch.setattr(r, "_load_active_profile_and_key", _fake_load)
    c1 = _run(r.get_active_client())
    c2 = _run(r.get_active_client())
    assert c1 is c2
    assert c1.provider == "local_vllm"
    assert c1.enabled is True


def test_resolver_returns_none_when_profile_not_enabled(monkeypatch) -> None:
    """Profile найден но invalid config → resolver вернёт None."""
    reset_resolver()
    r = ActiveProfileResolver()

    class _BadProfile:
        id = "llm_bad"
        provider = "openai"
        base_url = None
        model = "gpt-4o-mini"
        config = {}
        has_api_key = False

    async def _fake_load():
        return _BadProfile(), None   # no api_key → openai client disabled

    monkeypatch.setattr(r, "_load_active_profile_and_key", _fake_load)
    out = _run(r.get_active_client())
    assert out is None
