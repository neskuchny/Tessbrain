"""Тесты еженедельного пуша персонального совета в Telegram (гейтинг + путь)."""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


def test_disabled_by_default(monkeypatch) -> None:
    from backend.core.help import advice_push as ap
    monkeypatch.delenv("GUIDE_WEEKLY_PUSH", raising=False)
    assert ap.weekly_push_enabled() is False
    assert _run(ap.push_weekly_advice()) == 0     # ничего не рассылаем


def test_enabled_flag(monkeypatch) -> None:
    from backend.core.help import advice_push as ap
    monkeypatch.setenv("GUIDE_WEEKLY_PUSH", "on")
    assert ap.weekly_push_enabled() is True


def test_push_no_link_returns_false(monkeypatch) -> None:
    from backend.core.help import advice_push as ap

    async def no_chat(uid):
        return None

    monkeypatch.setattr(ap, "_telegram_chat_for_user", no_chat)
    assert _run(ap.push_advice_to_user("u1")) is False


def test_push_happy_path(monkeypatch) -> None:
    from backend.core.help import advice_push as ap
    import backend.core.help.usage_advisor as ua

    async def chat(uid):
        return "12345"

    async def fake_advice(uid, **k):
        return {"advice": "Начните с Brain.", "personalized": True, "tools": []}

    sent = {}

    async def fake_send(chat_id, text, *, user_id=""):
        sent["chat"] = chat_id
        sent["text"] = text
        return True

    monkeypatch.setattr(ap, "_telegram_chat_for_user", chat)
    monkeypatch.setattr(ua, "build_advice", fake_advice)
    monkeypatch.setattr(ap, "_send_telegram", fake_send)

    assert _run(ap.push_advice_to_user("u1")) is True
    assert sent["chat"] == "12345" and "Начните с Brain" in sent["text"]


def test_notify_user_telegram(monkeypatch) -> None:
    from backend.core.help import advice_push as ap

    async def chat(uid):
        return "999"

    sent = {}

    async def fake_send(chat_id, text, *, user_id=""):
        sent["chat"] = chat_id
        sent["text"] = text
        return True

    monkeypatch.setattr(ap, "_telegram_chat_for_user", chat)
    monkeypatch.setattr(ap, "_send_telegram", fake_send)
    assert _run(ap.notify_user_telegram("u1", "мы починили")) is True
    assert sent["chat"] == "999" and "починили" in sent["text"]


def test_notify_user_no_link(monkeypatch) -> None:
    from backend.core.help import advice_push as ap

    async def no_chat(uid):
        return None

    monkeypatch.setattr(ap, "_telegram_chat_for_user", no_chat)
    assert _run(ap.notify_user_telegram("u1", "text")) is False
    assert _run(ap.notify_user_telegram("", "text")) is False   # пустые → False


def test_notify_user_email(monkeypatch) -> None:
    from backend.core.help import advice_push as ap
    import backend.memory.user_profiles as up
    import backend.core.notifications.email as em

    class Prof:
        email = "client@example.com"

    class Svc:
        async def get_profile(self, uid):
            return Prof()

    class Res:
        ok = True

    sent = {}

    async def fake_send(msg, transport=None):
        sent["to"] = msg.to
        sent["subject"] = msg.subject
        return Res()

    monkeypatch.setattr(up, "get_user_profile_service", lambda: Svc())
    monkeypatch.setattr(em, "send_email", fake_send)
    assert _run(ap.notify_user_email("u1", "Тема", "Тело")) is True
    assert sent["to"] == "client@example.com" and sent["subject"] == "Тема"


def test_notify_user_email_no_address(monkeypatch) -> None:
    from backend.core.help import advice_push as ap
    import backend.memory.user_profiles as up

    class Prof:
        email = None

    class Svc:
        async def get_profile(self, uid):
            return Prof()

    monkeypatch.setattr(up, "get_user_profile_service", lambda: Svc())
    assert _run(ap.notify_user_email("u1", "s", "b")) is False


def test_notify_user_combines_channels(monkeypatch) -> None:
    from backend.core.help import advice_push as ap

    async def tg(uid, text):
        return True

    async def em(uid, subject, body):
        return False

    monkeypatch.setattr(ap, "notify_user_telegram", tg)
    monkeypatch.setattr(ap, "notify_user_email", em)
    res = _run(ap.notify_user("u1", subject="s", text="t"))
    assert res == {"telegram": True, "email": False}


def test_weekly_iterates_profiles(monkeypatch) -> None:
    from backend.core.help import advice_push as ap
    import backend.memory.user_profiles as up

    monkeypatch.setenv("GUIDE_WEEKLY_PUSH", "on")
    monkeypatch.setattr(ap, "_bot_token", lambda: "token")

    class FakeSvc:
        async def list_user_ids(self, limit=5000):
            return ["u1", "u2", "u3"]

    monkeypatch.setattr(up, "get_user_profile_service", lambda: FakeSvc())

    pushed = []

    async def fake_push(uid):
        pushed.append(uid)
        return uid != "u2"        # u2 без привязки → не отправлено

    monkeypatch.setattr(ap, "push_advice_to_user", fake_push)
    n = _run(ap.push_weekly_advice())
    assert n == 2 and pushed == ["u1", "u2", "u3"]
