# -*- coding: utf-8 -*-
"""Резолв chat_id для Telegram: привязка бота → фолбэк на вручную заданный
Default Chat ID из «Интеграций». Нужен, когда вебхук-привязки нет (локалхост),
но пользователь вписал Chat ID руками — доставка всё равно должна работать."""
from __future__ import annotations

import asyncio

from backend.core.messengers import links as ml


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Link:
    def __init__(self, external_id):
        self.external_id = external_id


class _Svc:
    def __init__(self, link):
        self._link = link

    async def find_link_for_user(self, *, user_id, platform):
        return self._link


def _patch_service(monkeypatch, link):
    async def _get_svc():
        return _Svc(link)
    monkeypatch.setattr(ml, "get_messenger_links_service", _get_svc)


def _patch_integration(monkeypatch, keys):
    import backend.api.routes.integrations as integ

    async def _keys(uid, prov):
        return keys
    monkeypatch.setattr(integ, "get_user_integration_keys", _keys)


def test_prefers_bot_link(monkeypatch):
    _patch_service(monkeypatch, _Link("555"))
    _patch_integration(monkeypatch, {"default_chat_id": "-100999"})
    assert _run(ml.resolve_telegram_chat_id("u")) == "555"


def test_falls_back_to_default_chat_id(monkeypatch):
    """Нет привязки → берём вручную заданный Default Chat ID."""
    _patch_service(monkeypatch, None)
    _patch_integration(monkeypatch, {"default_chat_id": "-284540446"})
    assert _run(ml.resolve_telegram_chat_id("u")) == "-284540446"


def test_none_when_nothing(monkeypatch):
    _patch_service(monkeypatch, None)
    _patch_integration(monkeypatch, {})
    assert _run(ml.resolve_telegram_chat_id("u")) is None


def test_empty_user_short_circuits():
    assert _run(ml.resolve_telegram_chat_id("")) is None
