# -*- coding: utf-8 -*-
"""Узел «Уведомление» рапортует ЧЕСТНО: если доставка не настроена
(Telegram/почта не привязаны) — ok:False с понятной причиной, а не ложный ✓."""
from __future__ import annotations

import asyncio

from backend.core.board import process_engine as pe


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_photo_delivery_failure_is_not_green(monkeypatch):
    async def tg_fail(user_id, image_file, caption=""):
        return False
    monkeypatch.setattr(pe, "_send_telegram_photo", tg_fail)
    out = _run(pe._process_handler(
        "notify", {"channel": "telegram"},
        [{"text": "карта", "image_file": "/tmp/x.png"}], {"user_id": "u"}))
    assert out["ok"] is False and "Telegram" in out["error"]


def test_text_not_delivered_is_not_green(monkeypatch):
    import backend.core.help.advice_push as ap

    async def nu(user_id, *, subject, text):
        return {"telegram": False, "email": False}
    monkeypatch.setattr(ap, "notify_user", nu)
    out = _run(pe._process_handler(
        "notify", {"channel": "telegram", "text": "привет"}, [], {"user_id": "u"}))
    assert out["ok"] is False and "не доставлено" in out["error"].lower()


def test_text_delivered_is_ok(monkeypatch):
    import backend.core.help.advice_push as ap

    async def nu(user_id, *, subject, text):
        return {"telegram": True, "email": False}
    monkeypatch.setattr(ap, "notify_user", nu)
    out = _run(pe._process_handler(
        "notify", {"channel": "telegram", "text": "привет"}, [], {"user_id": "u"}))
    assert out["ok"] is True


def test_notify_substitutes_input_placeholder(monkeypatch):
    # {{input}} в тексте узла должно замениться результатом предыдущего шага —
    # иначе в Telegram уходит литеральный «{{input}}» и пустое тело отчёта.
    import backend.core.help.advice_push as ap
    captured = {}

    async def nu(user_id, *, subject, text):
        captured["text"] = text
        return {"telegram": True, "email": False}

    monkeypatch.setattr(ap, "notify_user", nu)
    out = _run(pe._process_handler(
        "notify",
        {"channel": "telegram", "text": "📊 Отчёт\n{{input}}"},
        [{"text": "Выручка за неделю: 1.2М"}], {"user_id": "u"}))
    assert out["ok"] is True
    assert "{{input}}" not in captured["text"]
    assert "Выручка за неделю: 1.2М" in captured["text"]
    # и в возвращаемом тексте тоже нет литерального плейсхолдера
    assert "{{input}}" not in out["text"]


def test_photo_delivered_is_ok(monkeypatch):
    async def tg_ok(user_id, image_file, caption=""):
        return True
    monkeypatch.setattr(pe, "_send_telegram_photo", tg_ok)
    out = _run(pe._process_handler(
        "notify", {"channel": "telegram"},
        [{"text": "карта", "image_file": "/tmp/x.png"}], {"user_id": "u"}))
    assert out["ok"] is True and out.get("photo") is True
