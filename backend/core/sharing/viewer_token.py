"""W31: short-lived viewer JWT для partner-side доступа.

Поток:
1. Partner открывает /shared/<token> → landing page.
2. Вводит email, который мы сверяем с bundle.grantee_email.
3. При совпадении выдаём ViewerJWT (audience='shared-viewer'),
   срок жизни 60 минут (или до bundle.expires_at, что раньше).
4. Все последующие partner-side endpoints проверяют ViewerJWT и
   достают `bundle_id` для scope check'а.

Подпись HS256 секретом, отдельным от основного `jwt_secret_key` —
чтобы compromise одного не затрагивал другой. Если share-secret пустой,
fallback на основной (но в production обязательно set'нуть).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

logger = logging.getLogger(__name__)


VIEWER_AUDIENCE = "shared-viewer"
DEFAULT_TTL_MINUTES = 60


@dataclass
class ViewerClaims:
    bundle_id: str
    grantee_email: str
    expires_at: int   # epoch seconds


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_secret(provided: Optional[str]) -> str:
    if provided:
        return provided
    try:
        from backend.config import get_settings
        s = get_settings()
        share = (getattr(s, "share_jwt_secret", "") or "").strip()
        if share:
            return share
        # Fallback на основной — позволяет dev'у работать без отдельной env.
        return getattr(s, "jwt_secret_key", "") or "dev-share-secret"
    except Exception:
        return "dev-share-secret"


def issue_viewer_token(
    *,
    bundle_id: str,
    grantee_email: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    bundle_expires_at: Optional[datetime] = None,
    secret: Optional[str] = None,
) -> str:
    if not bundle_id:
        raise ValueError("bundle_id required")
    if not grantee_email:
        raise ValueError("grantee_email required")

    ttl_seconds = max(60, int(ttl_minutes) * 60)
    exp = _now() + timedelta(seconds=ttl_seconds)
    if bundle_expires_at is not None:
        if bundle_expires_at.tzinfo is None:
            bundle_expires_at = bundle_expires_at.replace(tzinfo=timezone.utc)
        exp = min(exp, bundle_expires_at)

    payload = {
        "sub": f"share:{bundle_id}",
        "bundle_id": bundle_id,
        "email": grantee_email.lower(),
        "aud": VIEWER_AUDIENCE,
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, _resolve_secret(secret), algorithm="HS256")


def decode_viewer_token(
    token: str,
    *,
    secret: Optional[str] = None,
) -> Optional[ViewerClaims]:
    """Verify + decode. None если invalid / expired / wrong audience."""
    if not token:
        return None
    try:
        decoded = jwt.decode(
            token,
            _resolve_secret(secret),
            algorithms=["HS256"],
            audience=VIEWER_AUDIENCE,
        )
    except Exception as exc:
        logger.debug("decode_viewer_token: %s", exc)
        return None
    bundle_id = decoded.get("bundle_id")
    email = decoded.get("email")
    exp = decoded.get("exp")
    if not bundle_id or not email or not isinstance(exp, int):
        return None
    return ViewerClaims(
        bundle_id=str(bundle_id),
        grantee_email=str(email).lower(),
        expires_at=int(exp),
    )


def viewer_token_remaining_seconds(claims: ViewerClaims) -> int:
    return max(0, claims.expires_at - int(_now().timestamp()))


__all__ = [
    "VIEWER_AUDIENCE",
    "ViewerClaims",
    "decode_viewer_token",
    "issue_viewer_token",
    "viewer_token_remaining_seconds",
]
