"""Unit-тесты telegram_userbot — честные отказы без настройки/telethon."""
from __future__ import annotations

import asyncio

from backend.core.messengers import telegram_userbot as tub


def _run(coro):
    return asyncio.run(coro)


def test_is_configured_requires_all_three(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    assert tub.is_configured() is False
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "s")
    assert tub.is_configured() is False        # ещё не хватает api_id/hash
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    assert tub.is_configured() is True


def test_read_group_history_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_SESSION_STRING", raising=False)
    r = _run(tub.read_group_history("-100123", days=7))
    assert r["ok"] is False and "настроен" in r["reason"]


def test_read_group_history_telethon_missing(monkeypatch) -> None:
    # Настроено, но telethon не установлен в этом окружении → честный reason.
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "s")
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    r = _run(tub.read_group_history("-100123", days=7))
    # либо telethon отсутствует, либо (если вдруг стоит) — ошибка сессии;
    # в любом случае ok=False с понятной причиной, без исключений.
    assert r["ok"] is False and r.get("reason")


def test_resolve_target_numeric_vs_username() -> None:
    assert tub._resolve_target("-100123") == -100123
    assert tub._resolve_target("@chan") == "@chan"
    assert tub._resolve_target("  -100987  ") == -100987
