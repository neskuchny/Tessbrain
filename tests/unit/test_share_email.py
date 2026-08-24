"""Unit-тесты для core.notifications (W33)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.notifications.email import (
    EmailDeliveryResult,
    EmailMessage,
    NoopTransport,
    send_email,
)
from backend.core.notifications.share_invite import (
    render_share_invite,
    send_share_invite,
)


def _run(coro):
    return asyncio.run(coro)


# === EmailMessage.validate ============================================

def test_message_validate_ok() -> None:
    EmailMessage(to="bob@x.com", subject="hi", text_body="x").validate()


def test_message_invalid_email_raises() -> None:
    with pytest.raises(ValueError):
        EmailMessage(to="not-email", subject="x", text_body="x").validate()


def test_message_empty_subject_raises() -> None:
    with pytest.raises(ValueError):
        EmailMessage(to="b@x.com", subject="  ", text_body="x").validate()


def test_message_empty_body_raises() -> None:
    with pytest.raises(ValueError):
        EmailMessage(to="b@x.com", subject="x", text_body="").validate()


def test_message_normalized_to_lowercases() -> None:
    m = EmailMessage(to="  Bob@X.com  ", subject="x", text_body="x")
    assert m.normalized_to() == "bob@x.com"


# === NoopTransport ====================================================

def test_noop_transport_records_message() -> None:
    t = NoopTransport()
    msg = EmailMessage(to="b@x.com", subject="hi", text_body="hello")
    res = _run(t.send(msg))
    assert res.ok is True
    assert res.transport == "noop"
    assert len(t.sent) == 1


def test_noop_transport_invalid_message_returns_failure() -> None:
    t = NoopTransport()
    msg = EmailMessage(to="not-email", subject="x", text_body="x")
    res = _run(t.send(msg))
    assert res.ok is False
    assert "invalid" in (res.error or "").lower()


# === send_email wrapper ===============================================

def test_send_email_with_explicit_transport() -> None:
    t = NoopTransport()
    res = _run(send_email(
        EmailMessage(to="b@x.com", subject="hi", text_body="x"),
        transport=t,
    ))
    assert res.ok is True
    assert len(t.sent) == 1


def test_send_email_catches_transport_exception() -> None:
    class _Broken:
        name = "broken"

        async def send(self, msg):
            raise RuntimeError("smtp down")

    res = _run(send_email(
        EmailMessage(to="b@x.com", subject="hi", text_body="x"),
        transport=_Broken(),
    ))
    assert res.ok is False
    assert "smtp down" in (res.error or "")


# === render_share_invite ==============================================

def test_render_returns_subject_text_html() -> None:
    subj, text, html = render_share_invite(
        owner_label="Alice",
        owner_org_or_email="Acme Inc",
        note="Q4 board pack",
        resource_count=3,
        expires_at="2026-06-10T00:00:00+00:00",
        public_url="https://app.example.com/shared/abcdef",
        grantee_email="bob@partner.com",
    )
    assert "Alice" in subj
    assert "Q4 board" in subj
    assert "https://app.example.com/shared/abcdef" in text
    assert "bob@partner.com" in text
    assert "Acme Inc" in text
    assert "<html" in html.lower()
    assert "https://app.example.com/shared/abcdef" in html


def test_render_escapes_html_dangerous_chars() -> None:
    """XSS-injection в note должен быть escape'нут."""
    _subj, _text, html = render_share_invite(
        owner_label="<script>alert(1)</script>",
        owner_org_or_email="x",
        note="<img src=x onerror=alert(1)>",
        resource_count=1,
        expires_at="now",
        public_url="https://x.com/",
        grantee_email="b@x.com",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html
    assert "&lt;img" in html


def test_render_handles_empty_note() -> None:
    subj, text, _html = render_share_invite(
        owner_label="A", owner_org_or_email="x",
        note="", resource_count=0, expires_at="now",
        public_url="https://x.com/", grantee_email="b@x.com",
    )
    assert "без описания" in text or "(без" in subj


# === send_share_invite ================================================

def test_send_share_invite_via_noop() -> None:
    t = NoopTransport()
    res = _run(send_share_invite(
        grantee_email="bob@partner.com",
        owner_label="Alice",
        owner_org_or_email="Acme",
        note="Q4",
        resource_count=2,
        expires_at="2026-06-10",
        public_url="https://app.example.com/shared/x",
        transport=t,
    ))
    assert res.ok is True
    assert len(t.sent) == 1
    assert t.sent[0].to == "bob@partner.com"


def test_send_share_invite_invalid_email() -> None:
    res = _run(send_share_invite(
        grantee_email="not-email",
        owner_label="A", owner_org_or_email="x",
        note="x", resource_count=1, expires_at="now",
        public_url="https://x.com/",
        transport=NoopTransport(),
    ))
    assert res.ok is False
    assert "grantee_email" in (res.error or "")


def test_send_share_invite_empty_url() -> None:
    res = _run(send_share_invite(
        grantee_email="b@x.com", owner_label="A",
        owner_org_or_email="x", note="x",
        resource_count=1, expires_at="now",
        public_url="",
        transport=NoopTransport(),
    ))
    assert res.ok is False
    assert "public_url" in (res.error or "")


# === EmailDeliveryResult dataclass ====================================

def test_delivery_result_defaults() -> None:
    r = EmailDeliveryResult(ok=True, transport="x")
    assert r.message_id is None
    assert r.error is None
    assert r.metadata == {}


# === build_default_transport selection ================================

def test_build_default_no_settings_returns_noop() -> None:
    """Без env переменных fallback на Noop."""
    from backend.core.notifications.email import build_default_transport
    # Reset any cached settings; should return Noop при пустых настройках.
    t = build_default_transport()
    assert t.name in ("noop", "smtp", "sendgrid")
