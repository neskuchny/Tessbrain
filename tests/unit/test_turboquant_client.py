"""Unit-тесты для core.llm.turboquant_client (W42a).

TurboQuant API имеет 2 особенности vs OpenAI-compat:
1. profile_id передаётся в body вместо/вместе с model
2. Response в двойной упаковке: {"response": {choices, usage}}

Тесты используют fake httpx через monkeypatch.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from backend.core.llm.turboquant_client import (
    TurboQuantClient,
    TurboQuantUsage,
    _parse_json_tolerantly,
    _unwrap_response,
    build_turboquant_client_from_profile,
)


def _run(coro):
    return asyncio.run(coro)


# === _unwrap_response =================================================

def test_unwrap_with_response_wrapper() -> None:
    raw = {"response": {"choices": [{"message": {"content": "hi"}}]}}
    inner = _unwrap_response(raw)
    assert inner["choices"][0]["message"]["content"] == "hi"


def test_unwrap_without_wrapper() -> None:
    raw = {"choices": [{"message": {"content": "hi"}}]}
    inner = _unwrap_response(raw)
    assert inner["choices"][0]["message"]["content"] == "hi"


def test_unwrap_none() -> None:
    assert _unwrap_response(None) == {}
    assert _unwrap_response("string") == {}


# === _parse_json_tolerantly ===========================================

def test_parse_json_plain() -> None:
    assert _parse_json_tolerantly('{"a": 1}') == {"a": 1}


def test_parse_json_with_fence() -> None:
    text = '```json\n{"x": 2}\n```'
    assert _parse_json_tolerantly(text) == {"x": 2}


def test_parse_json_with_fence_no_lang() -> None:
    text = '```\n{"y": 3}\n```'
    assert _parse_json_tolerantly(text) == {"y": 3}


def test_parse_json_invalid_returns_empty() -> None:
    assert _parse_json_tolerantly("not json") == {}
    assert _parse_json_tolerantly("") == {}


def test_parse_json_array_not_dict_returns_empty() -> None:
    """[1, 2] это валидный JSON но не dict — возвращаем {}."""
    assert _parse_json_tolerantly("[1, 2]") == {}


# === TurboQuantClient.__init__ ========================================

def test_init_requires_base_url() -> None:
    with pytest.raises(ValueError):
        TurboQuantClient(base_url="", profile_id="x")


def test_init_requires_profile_id() -> None:
    with pytest.raises(ValueError):
        TurboQuantClient(base_url="http://x", profile_id="")


def test_init_strips_trailing_slash() -> None:
    c = TurboQuantClient(base_url="http://x/", profile_id="p")
    assert c.base_url == "http://x"


def test_chat_completions_url() -> None:
    c = TurboQuantClient(base_url="https://api.example.com", profile_id="p")
    assert c.chat_completions_url == "https://api.example.com/v1/chat/completions"


def test_health_url() -> None:
    c = TurboQuantClient(base_url="https://api.example.com", profile_id="p")
    assert c.health_url == "https://api.example.com/health/live"


def test_models_url() -> None:
    c = TurboQuantClient(base_url="https://api.example.com", profile_id="p")
    assert c.models_url == "https://api.example.com/v1/models"


def test_headers_with_api_key() -> None:
    c = TurboQuantClient(base_url="http://x", profile_id="p", api_key="dev-token-123")
    h = c._headers()
    assert h["Authorization"] == "Bearer dev-token-123"
    assert h["Content-Type"] == "application/json"


def test_headers_without_api_key() -> None:
    c = TurboQuantClient(base_url="http://x", profile_id="p")
    h = c._headers()
    assert "Authorization" not in h


# === Mocked generate / health_check ===================================


class _FakeAsyncResponse:
    def __init__(self, *, status_code: int = 200, json_data: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or json.dumps(self._json)

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            from httpx import HTTPStatusError
            raise HTTPStatusError(
                f"http {self.status_code}",
                request=None, response=None,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    def __init__(self, *, response: _FakeAsyncResponse, capture: dict[str, Any]) -> None:
        self._response = response
        self._capture = capture

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *a) -> None:
        return None

    async def post(self, url: str, *, headers=None, json=None) -> _FakeAsyncResponse:
        self._capture.update({"url": url, "headers": headers or {}, "body": json or {}, "method": "POST"})
        return self._response

    async def get(self, url: str, *, headers=None) -> _FakeAsyncResponse:
        self._capture.update({"url": url, "headers": headers or {}, "method": "GET"})
        return self._response


def _patch_httpx(monkeypatch, response: _FakeAsyncResponse) -> dict[str, Any]:
    """Подменить httpx.AsyncClient — вернуть dict для capture'а аргументов."""
    import httpx
    capture: dict[str, Any] = {}

    def _factory(*args, **kwargs):
        return _FakeAsyncClient(response=response, capture=capture)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return capture


# === generate (success path) ==========================================

def test_generate_unwraps_double_wrapped_response(monkeypatch) -> None:
    """Главный TurboQuant корнер: response внутри `response`."""
    fake = _FakeAsyncResponse(json_data={
        "response": {
            "choices": [{"message": {"content": "ответ от TurboQuant"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }
    })
    capture = _patch_httpx(monkeypatch, fake)

    c = TurboQuantClient(
        base_url="https://api.example.com", profile_id="gemma4_e4b_it_w4a16",
        api_key="dev-token-xxx",
    )
    out = _run(c.generate("кто такой Ленин?", system_prompt="Будь краток"))
    assert out == "ответ от TurboQuant"

    # Verify request shape.
    body = capture["body"]
    assert body["profile_id"] == "gemma4_e4b_it_w4a16"
    assert body["model"] == "gemma4_e4b_it_w4a16"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == "Будь краток"
    assert body["messages"][1]["content"] == "кто такой Ленин?"
    assert capture["headers"]["Authorization"] == "Bearer dev-token-xxx"


def test_generate_handles_unwrapped_response(monkeypatch) -> None:
    """Defensive: если когда-нибудь TurboQuant перейдёт на стандарт — тоже работает."""
    fake = _FakeAsyncResponse(json_data={
        "choices": [{"message": {"content": "плоский ответ"}}],
    })
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    out = _run(c.generate("test"))
    assert out == "плоский ответ"


def test_generate_no_choices_returns_empty(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={"response": {"choices": []}})
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    out = _run(c.generate("test"))
    assert out == ""


def test_generate_no_system_prompt(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={"response": {"choices": [{"message": {"content": "ok"}}]}})
    capture = _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    _run(c.generate("test"))
    # Только user сообщение, без system.
    assert len(capture["body"]["messages"]) == 1
    assert capture["body"]["messages"][0]["role"] == "user"


def test_generate_message_with_list_content(monkeypatch) -> None:
    """OpenAI-compat content может быть list[dict] с text-частями."""
    fake = _FakeAsyncResponse(json_data={
        "response": {
            "choices": [{"message": {"content": [
                {"type": "text", "text": "часть 1"},
                {"type": "text", "text": "часть 2"},
            ]}}],
        }
    })
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    out = _run(c.generate("test"))
    assert "часть 1" in out
    assert "часть 2" in out


def test_generate_passes_temperature_top_p_max_tokens(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={"response": {"choices": [{"message": {"content": "x"}}]}})
    capture = _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    _run(c.generate("test", max_tokens=512, temperature=0.7, top_p=0.9))
    body = capture["body"]
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9


def test_generate_omits_presence_penalty_when_zero(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={"response": {"choices": [{"message": {"content": "x"}}]}})
    capture = _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    _run(c.generate("test", presence_penalty=0.0))
    assert "presence_penalty" not in capture["body"]


def test_generate_includes_presence_penalty_when_nonzero(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={"response": {"choices": [{"message": {"content": "x"}}]}})
    capture = _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    _run(c.generate("test", presence_penalty=0.5))
    assert capture["body"]["presence_penalty"] == 0.5


# === generate_json ====================================================


def test_generate_json_parses_object(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={
        "response": {"choices": [{"message": {"content": '{"answer": 42}'}}]},
    })
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    out = _run(c.generate_json("test"))
    assert out == {"answer": 42}


def test_generate_json_strips_fence(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={
        "response": {"choices": [{"message": {"content": '```json\n{"x": 1}\n```'}}]},
    })
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    out = _run(c.generate_json("test"))
    assert out == {"x": 1}


# === health_check =====================================================


def test_health_check_ok(monkeypatch) -> None:
    fake = _FakeAsyncResponse(status_code=200, json_data={"status": "ok"})
    capture = _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    ok, err = _run(c.health_check())
    assert ok is True
    assert err is None
    assert capture["url"] == "https://x/health/live"


def test_health_check_failure_returns_status(monkeypatch) -> None:
    fake = _FakeAsyncResponse(status_code=503, text="upstream gone")
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    ok, err = _run(c.health_check())
    assert ok is False
    assert "503" in (err or "")


# === list_models =====================================================


def test_list_models_returns_data(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={
        "data": [
            {"id": "gemma4_e4b_it_w4a16", "runtime": "vllm", "task": "chat"},
            {"id": "turboquant_research", "runtime": "vllm", "task": "chat"},
        ]
    })
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    items = _run(c.list_models())
    assert len(items) == 2
    assert items[0]["id"] == "gemma4_e4b_it_w4a16"


def test_list_models_handles_empty(monkeypatch) -> None:
    fake = _FakeAsyncResponse(json_data={"data": []})
    _patch_httpx(monkeypatch, fake)
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    assert _run(c.list_models()) == []


# === get_usage ========================================================


def test_get_usage_extracts_tokens() -> None:
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    raw = {"response": {"usage": {"prompt_tokens": 100, "completion_tokens": 25}}}
    usage = _run(c.get_usage(raw))
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 25
    assert usage.total_tokens == 125


def test_get_usage_no_usage_returns_zero() -> None:
    c = TurboQuantClient(base_url="https://x", profile_id="p")
    usage = _run(c.get_usage({"response": {}}))
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0


# === build_turboquant_client_from_profile =============================


def test_factory_builds_client() -> None:
    c = build_turboquant_client_from_profile(
        base_url="https://api.example.com",
        profile_id_or_model="gemma4_e4b_it_w4a16",
        api_key="dev-token",
    )
    assert isinstance(c, TurboQuantClient)
    assert c.profile_id == "gemma4_e4b_it_w4a16"
    assert c.api_key == "dev-token"


def test_factory_uses_config_timeout() -> None:
    c = build_turboquant_client_from_profile(
        base_url="https://x", profile_id_or_model="p",
        api_key=None, config={"timeout": 60.0},
    )
    assert c.timeout == 60.0


def test_factory_default_timeout() -> None:
    c = build_turboquant_client_from_profile(
        base_url="https://x", profile_id_or_model="p", api_key=None,
    )
    assert c.timeout == 300.0
