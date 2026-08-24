"""W30: Webhook signature verification.

Telegram: header `X-Telegram-Bot-Api-Secret-Token` сравнивается с tenant
config'ом (constant-time). Это собственный механизм Telegram'а — secret
выставляется при `setWebhook`.

Slack: HMAC-SHA256 от `v0:{timestamp}:{body}` с `slack_signing_secret`,
сравнить с `X-Slack-Signature`. Reject если |now - ts| > 5 минут (replay).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SignatureMismatch(Exception):
    """Raised когда подпись не совпала / устарела."""


def verify_telegram_secret(
    *,
    expected_secret: Optional[str],
    received_secret: Optional[str],
) -> bool:
    """Constant-time сравнение Telegram secret_token.

    Если expected пустой — считаем что верификация выключена (dev mode).
    В production обязательно set'нуть secret через `setWebhook`.
    """
    if not expected_secret:
        logger.warning(
            "verify_telegram_secret: no secret configured — accepting (dev mode)"
        )
        return True
    if not received_secret:
        return False
    return hmac.compare_digest(
        expected_secret.encode("utf-8"),
        received_secret.encode("utf-8"),
    )


def verify_slack_signature(
    *,
    signing_secret: Optional[str],
    timestamp: Optional[str],
    body: bytes,
    received_signature: Optional[str],
    max_age_seconds: int = 300,
) -> bool:
    """Slack-style HMAC verification.

    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac_sha256(signing_secret, base)
    """
    if not signing_secret:
        logger.warning(
            "verify_slack_signature: no signing_secret — accepting (dev mode)"
        )
        return True
    if not timestamp or not received_signature:
        return False

    # Replay protection.
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > max_age_seconds:
        return False

    base = f"v0:{timestamp}:".encode("utf-8") + (body or b"")
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        base,
        hashlib.sha256,
    ).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, received_signature)
