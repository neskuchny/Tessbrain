"""Unit-тесты для core.messengers.links (W30) — in-memory режим."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.messengers.links import (
    SUPPORTED_PLATFORMS,
    MessengerLinkService,
    OnboardingToken,
)


def _run(coro):
    return asyncio.run(coro)


# === issue_token ======================================================

def test_issue_token_returns_record() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    assert rec.token
    assert len(rec.token) == 32   # 16 bytes hex
    assert rec.user_id == "u-1"
    assert rec.platform == "telegram"
    assert rec.is_valid()


def test_issue_token_rejects_unknown_platform() -> None:
    svc = MessengerLinkService()
    with pytest.raises(ValueError):
        _run(svc.issue_token(user_id="u-1", platform="signal"))


def test_token_validity_expires() -> None:
    """Минимальный TTL=1, expires_at в будущем — is_valid=True."""
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram", ttl_minutes=1))
    assert rec.is_valid() is True


def test_used_token_invalid() -> None:
    rec = OnboardingToken(
        token="x", user_id="u", platform="telegram",
        expires_at="2099-01-01T00:00:00+00:00",
        used_at="2026-01-01T00:00:00+00:00",
    )
    assert rec.is_valid() is False


def test_supported_platforms_contains_expected() -> None:
    assert "telegram" in SUPPORTED_PLATFORMS
    assert "slack" in SUPPORTED_PLATFORMS
    assert "whatsapp" in SUPPORTED_PLATFORMS


# === redeem_token ======================================================

def test_redeem_token_creates_link() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    link = _run(svc.redeem_token(
        token=rec.token, external_id="12345", external_username="alice",
    ))
    assert link is not None
    assert link.user_id == "u-1"
    assert link.platform == "telegram"
    assert link.external_id == "12345"
    assert link.external_username == "alice"
    assert link.active is True


def test_redeem_invalid_token_returns_none() -> None:
    svc = MessengerLinkService()
    link = _run(svc.redeem_token(token="bogus", external_id="1"))
    assert link is None


def test_redeem_empty_token_returns_none() -> None:
    svc = MessengerLinkService()
    assert _run(svc.redeem_token(token="", external_id="1")) is None
    rec = _run(svc.issue_token(user_id="u", platform="telegram"))
    assert _run(svc.redeem_token(token=rec.token, external_id="")) is None


def test_redeem_token_twice_returns_none_second() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    first = _run(svc.redeem_token(token=rec.token, external_id="111"))
    second = _run(svc.redeem_token(token=rec.token, external_id="222"))
    assert first is not None
    assert second is None   # уже used


# === lookup_by_external ===============================================

def test_lookup_by_external_after_redeem() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    _run(svc.redeem_token(token=rec.token, external_id="12345"))

    found = _run(svc.lookup_by_external(platform="telegram", external_id="12345"))
    assert found is not None
    assert found.user_id == "u-1"


def test_lookup_unknown_returns_none() -> None:
    svc = MessengerLinkService()
    assert _run(svc.lookup_by_external(platform="telegram", external_id="ghost")) is None


def test_lookup_empty_args_returns_none() -> None:
    svc = MessengerLinkService()
    assert _run(svc.lookup_by_external(platform="", external_id="1")) is None
    assert _run(svc.lookup_by_external(platform="telegram", external_id="")) is None


# === list_for_user ====================================================

def test_list_for_user_returns_active_only() -> None:
    svc = MessengerLinkService()
    t1 = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    t2 = _run(svc.issue_token(user_id="u-1", platform="slack"))
    link1 = _run(svc.redeem_token(token=t1.token, external_id="111"))
    link2 = _run(svc.redeem_token(token=t2.token, external_id="222"))

    items = _run(svc.list_for_user(user_id="u-1"))
    assert {l.id for l in items} == {link1.id, link2.id}

    _run(svc.deactivate(link_id=link1.id))
    items_after = _run(svc.list_for_user(user_id="u-1"))
    assert {l.id for l in items_after} == {link2.id}


def test_list_for_empty_user_returns_empty() -> None:
    svc = MessengerLinkService()
    assert _run(svc.list_for_user(user_id="")) == []


# === touch ============================================================

def test_touch_updates_last_seen() -> None:
    svc = MessengerLinkService()
    rec = _run(svc.issue_token(user_id="u-1", platform="telegram"))
    link = _run(svc.redeem_token(token=rec.token, external_id="42"))
    assert link.last_seen_at is None

    _run(svc.touch(link_id=link.id))
    found = _run(svc.lookup_by_external(platform="telegram", external_id="42"))
    assert found.last_seen_at is not None


def test_touch_unknown_link_no_op() -> None:
    svc = MessengerLinkService()
    # Не должно raise.
    _run(svc.touch(link_id="bogus"))
    _run(svc.touch(link_id=""))


# === deactivate =======================================================

def test_deactivate_returns_false_for_unknown() -> None:
    svc = MessengerLinkService()
    assert _run(svc.deactivate(link_id="bogus")) is False
    assert _run(svc.deactivate(link_id="")) is False
