"""Unit-тесты для core.messengers.verify (W30)."""
from __future__ import annotations

import hashlib
import hmac
import time

from backend.core.messengers.verify import (
    verify_slack_signature,
    verify_telegram_secret,
)

# === Telegram ==========================================================

def test_telegram_no_secret_configured_accepts() -> None:
    """Без сконфигурированного secret — dev mode, accept anything."""
    assert verify_telegram_secret(expected_secret=None, received_secret=None) is True
    assert verify_telegram_secret(expected_secret="", received_secret="x") is True


def test_telegram_match() -> None:
    assert verify_telegram_secret(
        expected_secret="abc123", received_secret="abc123",
    ) is True


def test_telegram_mismatch() -> None:
    assert verify_telegram_secret(
        expected_secret="abc123", received_secret="bad",
    ) is False


def test_telegram_missing_received() -> None:
    assert verify_telegram_secret(
        expected_secret="abc123", received_secret=None,
    ) is False


# === Slack =============================================================

def _slack_signature(secret: str, ts: str, body: bytes) -> str:
    base = f"v0:{ts}:".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_slack_no_secret_accepts() -> None:
    assert verify_slack_signature(
        signing_secret=None, timestamp="1", body=b"x", received_signature="y",
    ) is True


def test_slack_valid_signature() -> None:
    secret = "test_secret"
    ts = str(int(time.time()))
    body = b'{"event":"hi"}'
    sig = _slack_signature(secret, ts, body)
    assert verify_slack_signature(
        signing_secret=secret, timestamp=ts, body=body, received_signature=sig,
    ) is True


def test_slack_bad_signature() -> None:
    ts = str(int(time.time()))
    assert verify_slack_signature(
        signing_secret="s", timestamp=ts, body=b"x",
        received_signature="v0=deadbeef",
    ) is False


def test_slack_replay_too_old() -> None:
    secret = "s"
    ts = str(int(time.time()) - 600)   # 10 минут назад
    body = b"x"
    sig = _slack_signature(secret, ts, body)
    assert verify_slack_signature(
        signing_secret=secret, timestamp=ts, body=body,
        received_signature=sig, max_age_seconds=300,
    ) is False


def test_slack_missing_timestamp_or_signature() -> None:
    assert verify_slack_signature(
        signing_secret="s", timestamp=None, body=b"x", received_signature="v0=x",
    ) is False
    assert verify_slack_signature(
        signing_secret="s", timestamp="1", body=b"x", received_signature=None,
    ) is False


def test_slack_non_numeric_timestamp() -> None:
    assert verify_slack_signature(
        signing_secret="s", timestamp="abc", body=b"x", received_signature="v0=x",
    ) is False
