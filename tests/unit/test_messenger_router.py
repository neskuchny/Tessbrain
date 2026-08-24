"""Unit-тесты для core.messengers.router (W30)."""
from __future__ import annotations

import asyncio

from backend.core.messengers.links import MessengerLinkService
from backend.core.messengers.router import route_inbound_message


def _run(coro):
    return asyncio.run(coro)


# === /start <token> path ==============================================

def test_start_with_valid_token_links_account() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))

    reply = _run(route_inbound_message(
        platform="telegram",
        external_id="12345",
        text=f"/start {rec.token}",
        username="alice",
        link_service=svc,
    ))
    assert reply.is_onboarding is True
    assert reply.linked_user_id == "u-1"
    assert "u-1"[:8] in reply.reply_text or "привязан" in reply.reply_text.lower()

    # последующий lookup находит link.
    found = _run(svc.lookup_by_external(platform="telegram", external_id="12345"))
    assert found is not None


def test_start_with_invalid_token_returns_error() -> None:
    svc = MessengerLinkService()
    reply = _run(route_inbound_message(
        platform="telegram", external_id="12345",
        text="/start bogus_token",
        link_service=svc,
    ))
    assert reply.is_onboarding is True
    assert reply.linked_user_id is None
    assert "невалид" in reply.reply_text.lower() or "истёк" in reply.reply_text.lower()


def test_start_without_token_treated_as_unknown() -> None:
    """`/start` без токена — это просто команда, не onboarding."""
    svc = MessengerLinkService()
    reply = _run(route_inbound_message(
        platform="telegram", external_id="12345",
        text="/start",
        link_service=svc,
    ))
    # Не привязан → help-сообщение, не onboarding flow (нет токена).
    assert reply.is_onboarding is False
    assert reply.linked_user_id is None
    assert "привяз" in reply.reply_text.lower()


# === unlinked external_id ============================================

def test_unlinked_user_gets_help_message() -> None:
    svc = MessengerLinkService()
    reply = _run(route_inbound_message(
        platform="telegram", external_id="ghost",
        text="hello", link_service=svc,
    ))
    assert reply.linked_user_id is None
    assert "привяз" in reply.reply_text.lower()


# === linked user — chat handler =======================================

def test_linked_user_message_calls_chat_handler() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    _run(svc.redeem_token(token=rec.token, external_id="42"))

    captured = {}

    async def _handler(user_id: str, text: str) -> str:
        captured["user_id"] = user_id
        captured["text"] = text
        return "answer-from-chat"

    reply = _run(route_inbound_message(
        platform="telegram", external_id="42",
        text="кто такой Ленин?",
        link_service=svc,
        chat_handler=_handler,
    ))
    assert captured == {"user_id": "u-1", "text": "кто такой Ленин?"}
    assert reply.linked_user_id == "u-1"
    assert reply.reply_text == "answer-from-chat"


def test_linked_no_handler_falls_back_to_echo() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    _run(svc.redeem_token(token=rec.token, external_id="42"))

    reply = _run(route_inbound_message(
        platform="telegram", external_id="42", text="hi",
        link_service=svc, chat_handler=None,
    ))
    assert reply.reply_text.startswith("(echo)")


def test_linked_handler_failure_returns_safe_message() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    _run(svc.redeem_token(token=rec.token, external_id="42"))

    async def _broken(user_id: str, text: str) -> str:
        raise RuntimeError("LLM down")

    reply = _run(route_inbound_message(
        platform="telegram", external_id="42",
        text="ping", link_service=svc, chat_handler=_broken,
    ))
    assert reply.linked_user_id == "u-1"
    # W43: fallback теперь через i18n catalog, проверяем что вернулся error-message
    lowered = reply.reply_text.lower()
    assert "ошибк" in lowered or "не получилось" in lowered or "couldn't" in lowered


def test_linked_empty_text_gives_hint() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    _run(svc.redeem_token(token=rec.token, external_id="42"))

    reply = _run(route_inbound_message(
        platform="telegram", external_id="42", text="   ",
        link_service=svc,
    ))
    assert "Напиши" in reply.reply_text or "вопрос" in reply.reply_text.lower()


def test_linked_command_other_than_start_gives_hint() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    _run(svc.redeem_token(token=rec.token, external_id="42"))

    reply = _run(route_inbound_message(
        platform="telegram", external_id="42", text="/help",
        link_service=svc,
    ))
    # /help не обрабатывается как чат — отдаём hint.
    assert reply.linked_user_id == "u-1"


# === touch on inbound =================================================

def test_inbound_touches_last_seen() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    link = _run(svc.redeem_token(token=rec.token, external_id="42"))
    assert link.last_seen_at is None

    _run(route_inbound_message(
        platform="telegram", external_id="42", text="hi", link_service=svc,
    ))
    found = _run(svc.lookup_by_external(platform="telegram", external_id="42"))
    assert found.last_seen_at is not None
