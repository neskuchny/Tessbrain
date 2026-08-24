"""Unit-тесты для core.messengers.chat_handler (W34)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.messengers.chat_handler import (
    DEFAULT_MAX_REPLY_CHARS,
    MessengerChatHandler,
    build_messenger_chat_handler,
)


def _run(coro):
    return asyncio.run(coro)


# === Fake LLM router ==================================================

class _FakeLLM:
    def __init__(self, *, reply: str = "ok", raise_exc: Exception | None = None) -> None:
        self.reply = reply
        self.raise_exc = raise_exc
        self.last_kwargs: dict | None = None

    async def generate(self, *, prompt, system_prompt=None, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        self.last_kwargs = {
            "prompt": prompt,
            "system_prompt": system_prompt,
            **kwargs,
        }
        return self.reply


# === MessengerChatHandler =============================================

def test_handler_returns_llm_reply() -> None:
    llm = _FakeLLM(reply="это ответ")
    h = MessengerChatHandler(llm_router=llm)
    out = _run(h("u-1", "что у нас по Q4?"))
    assert out == "это ответ"


def test_handler_passes_text_to_llm_as_prompt() -> None:
    llm = _FakeLLM()
    h = MessengerChatHandler(llm_router=llm)
    _run(h("u-1", "привет"))
    assert llm.last_kwargs["prompt"] == "привет"
    assert "Tessbrain" in (llm.last_kwargs["system_prompt"] or "")


def test_handler_strips_input() -> None:
    llm = _FakeLLM()
    h = MessengerChatHandler(llm_router=llm)
    _run(h("u-1", "   hello   "))
    assert llm.last_kwargs["prompt"] == "hello"


def test_handler_empty_text_returns_hint_no_llm_call() -> None:
    llm = _FakeLLM()
    h = MessengerChatHandler(llm_router=llm)
    out = _run(h("u-1", "   "))
    assert "вопрос" in out.lower()
    assert llm.last_kwargs is None   # не звонили в LLM


def test_handler_clamps_long_reply() -> None:
    long = "x" * (DEFAULT_MAX_REPLY_CHARS + 100)
    h = MessengerChatHandler(llm_router=_FakeLLM(reply=long))
    out = _run(h("u-1", "..."))
    assert len(out) == DEFAULT_MAX_REPLY_CHARS
    assert out.endswith("…")


def test_handler_does_not_clamp_short_reply() -> None:
    h = MessengerChatHandler(llm_router=_FakeLLM(reply="короткий"))
    out = _run(h("u-1", "hi"))
    assert out == "короткий"


def test_handler_custom_clamp_size() -> None:
    long = "y" * 500
    h = MessengerChatHandler(
        llm_router=_FakeLLM(reply=long),
        max_reply_chars=300,
    )
    out = _run(h("u-1", "..."))
    assert len(out) == 300
    assert out.endswith("…")


def test_handler_clamp_floor_is_200() -> None:
    """Защитный пол — даже 0 не должен дать 0-длинный ответ."""
    h = MessengerChatHandler(llm_router=_FakeLLM(reply="x"), max_reply_chars=0)
    assert h.max_reply_chars == 200


def test_handler_llm_failure_returns_safe_message() -> None:
    h = MessengerChatHandler(llm_router=_FakeLLM(raise_exc=RuntimeError("nope")))
    out = _run(h("u-1", "hi"))
    assert "ошибк" in out.lower() or "повтор" in out.lower()


def test_handler_no_router_returns_unavailable_message() -> None:
    """Если глобальный LLM недоступен — graceful fallback."""
    class _BrokenInit(MessengerChatHandler):
        async def _get_router(self):
            return None
    h = _BrokenInit()
    out = _run(h("u-1", "hi"))
    assert "недоступн" in out.lower() or "браузер" in out.lower()


def test_handler_empty_llm_reply_replaced() -> None:
    h = MessengerChatHandler(llm_router=_FakeLLM(reply=""))
    out = _run(h("u-1", "hi"))
    assert out  # не пусто


def test_handler_passes_temperature_and_max_tokens() -> None:
    llm = _FakeLLM()
    h = MessengerChatHandler(llm_router=llm, temperature=0.2, max_tokens=500)
    _run(h("u-1", "hi"))
    assert llm.last_kwargs["temperature"] == 0.2
    assert llm.last_kwargs["max_tokens"] == 500


# === build_messenger_chat_handler =====================================

def test_builder_returns_callable() -> None:
    fn = _run(build_messenger_chat_handler())
    assert callable(fn)


def test_builder_handler_runs_endtoend(monkeypatch) -> None:
    """С подменённым get_llm_router builder отдаёт работающий handler."""
    fake = _FakeLLM(reply="from-builder")

    def _fake_get_router():
        return fake

    monkeypatch.setattr(
        "backend.core.llm.router.get_llm_router",
        _fake_get_router,
        raising=False,
    )
    fn = _run(build_messenger_chat_handler())
    out = _run(fn("u-1", "hello"))
    assert out == "from-builder"
