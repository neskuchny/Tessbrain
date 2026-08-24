# -*- coding: utf-8 -*-
"""Узел llmGenerate доски уважает выбранные провайдер/модель (реальный вызов,
без заглушек) и мягко откатывается на дефолт тенанта при отсутствии ключа."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.board import process_engine as pe


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_provider_map_covers_all_board_providers():
    assert set(pe._BOARD_LLM_PROVIDERS) == {
        "google", "openai", "anthropic", "deepseek", "qwen", "xai"}
    # id провайдера в «Интеграциях»: google → google_ai (там лежит его ключ)
    assert pe._BOARD_LLM_PROVIDERS["google"][1] == "google_ai"
    assert pe._BOARD_LLM_PROVIDERS["deepseek"][1] == "deepseek"


def test_no_key_returns_none_soft_fallback(monkeypatch):
    for k in ("XAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert _run(pe._generate_with_board_model(None, "xai", "grok-4", "привет")) is None


def test_unknown_provider_or_empty_model_returns_none(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    assert _run(pe._generate_with_board_model(None, "bogus", "m", "p")) is None
    assert _run(pe._generate_with_board_model(None, "xai", "", "p")) is None


def test_uses_selected_provider_when_env_key_present(monkeypatch):
    """С env-ключом платформы узел реально зовёт выбранную модель."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-plat")
    seen = {}

    class FakeClient:
        enabled = True

        def __init__(self, *, provider, base_url, model, api_key):
            seen.update(provider=provider, model=model, api_key=api_key)

        async def generate(self, prompt, *, temperature=0.7, max_tokens=2048):
            seen.update(prompt=prompt, temperature=temperature, max_tokens=max_tokens)
            return "  сгенерировано deepseek  "

    monkeypatch.setattr(
        "backend.core.llm.profile_client.ProfileBackedClient", FakeClient)
    out = _run(pe._generate_with_board_model(
        None, "deepseek", "deepseek-v4-pro", "посчитай",
        temperature=0.3, max_tokens=999))
    assert out == "сгенерировано deepseek"          # обрезка пробелов
    assert seen["provider"] == "deepseek" and seen["model"] == "deepseek-v4-pro"
    assert seen["api_key"] == "sk-plat"
    assert seen["temperature"] == 0.3 and seen["max_tokens"] == 999


def test_integration_key_takes_priority_over_env(monkeypatch):
    """Ключ из вкладки «Интеграции» тенанта важнее env платформы."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-plat")
    seen = {}

    async def fake_keys(user_id, provider):
        assert provider == "deepseek"
        return {"api_key": "sk-tenant"}

    import backend.api.routes.integrations as integ
    monkeypatch.setattr(integ, "get_user_integration_keys", fake_keys)

    class FakeClient:
        enabled = True

        def __init__(self, *, provider, base_url, model, api_key):
            seen["api_key"] = api_key

        async def generate(self, prompt, *, temperature=0.7, max_tokens=2048):
            return "ok"

    monkeypatch.setattr(
        "backend.core.llm.profile_client.ProfileBackedClient", FakeClient)
    out = _run(pe._generate_with_board_model(
        "user-1", "deepseek", "deepseek-v4-pro", "p"))
    assert out == "ok"
    assert seen["api_key"] == "sk-tenant"           # из «Интеграций», не env


def test_disabled_client_returns_none(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-test")

    class Disabled:
        enabled = False

        def __init__(self, **kw):
            pass

    monkeypatch.setattr(
        "backend.core.llm.profile_client.ProfileBackedClient", Disabled)
    assert _run(pe._generate_with_board_model(None, "xai", "grok-4", "p")) is None


def test_generate_exception_is_swallowed_to_none(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-test")

    class Boom:
        enabled = True

        def __init__(self, **kw):
            pass

        async def generate(self, *a, **k):
            raise RuntimeError("api down")

    monkeypatch.setattr(
        "backend.core.llm.profile_client.ProfileBackedClient", Boom)
    # падение провайдера → None → вызывающий откатится на дефолт (не крашит прогон)
    assert _run(pe._generate_with_board_model(None, "xai", "grok-4", "p")) is None
