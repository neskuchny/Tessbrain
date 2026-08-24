# -*- coding: utf-8 -*-
"""Разбор (Анализ) гоним тенантской моделью DeepSeek v4 Pro, но при любой
проблеме — безопасный фолбэк на дефолтный роутер (Gemini): прогон не падает."""
from __future__ import annotations

import asyncio

from backend.core.analysis import run_engine as re


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Inner:
    async def generate_json(self, prompt, **k):
        return {"src": "fallback"}


def _wrap(dsobj):
    w = re._TenantJsonLLM(_Inner(), "u")
    w._resolved = True
    w._client = dsobj  # инъекция клиента, минуя резолв ключа
    return w


def test_uses_deepseek_when_available():
    class DS:
        enabled = True
        async def generate_json(self, prompt, *, system_prompt=None,
                                 temperature=0.2, max_tokens=8192):
            return {"src": "deepseek"}
    out = _run(_wrap(DS()).generate_json("p", system_prompt="s"))
    assert out == {"src": "deepseek"}


def test_falls_back_on_empty_json():
    class DS:
        enabled = True
        async def generate_json(self, *a, **k):
            return {}
    assert _run(_wrap(DS()).generate_json("p")) == {"src": "fallback"}


def test_falls_back_on_error():
    class DS:
        enabled = True
        async def generate_json(self, *a, **k):
            raise RuntimeError("api down")
    assert _run(_wrap(DS()).generate_json("p")) == {"src": "fallback"}


def test_falls_back_when_no_client():
    assert _run(_wrap(None).generate_json("p")) == {"src": "fallback"}


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("ANALYSIS_TENANT_DEEPSEEK", raising=False)
    assert re._tenant_model_enabled() is True
    monkeypatch.setenv("ANALYSIS_TENANT_DEEPSEEK", "off")
    assert re._tenant_model_enabled() is False


def test_no_key_no_client(monkeypatch):
    """Без ключа (Интеграции пусты + env пуст) клиент не строится → None."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    import backend.api.routes.integrations as integ

    async def empty_keys(uid, prov):
        return {}
    monkeypatch.setattr(integ, "get_user_integration_keys", empty_keys)
    assert _run(re._tenant_deepseek_client("u")) is None
