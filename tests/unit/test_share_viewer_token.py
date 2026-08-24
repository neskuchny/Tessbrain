"""Unit-тесты для core.sharing.viewer_token (W31)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import jwt as _jwt_mod
import pytest
from backend.core.sharing.viewer_token import (
    VIEWER_AUDIENCE,
    decode_viewer_token,
    issue_viewer_token,
    viewer_token_remaining_seconds,
)

_SECRET = "test-share-secret-xyz"


def _is_real_pyjwt() -> bool:
    """conftest подсовывает stub если PyJWT/cryptography сломаны.

    Stub'овский encode возвращает токены с пустым header (`e30.`) и без
    подписи — signature-зависимые тесты в таком режиме не имеют смысла.
    """
    sample = _jwt_mod.encode({"a": 1}, "k", algorithm="HS256")
    return not sample.startswith("e30.")


_REAL_JWT = _is_real_pyjwt()
_skip_if_stub = pytest.mark.skipif(
    not _REAL_JWT, reason="signature verification needs real PyJWT",
)


def test_issue_and_decode_roundtrip() -> None:
    tok = issue_viewer_token(
        bundle_id="shr_1", grantee_email="bob@x.com",
        ttl_minutes=5, secret=_SECRET,
    )
    claims = decode_viewer_token(tok, secret=_SECRET)
    assert claims is not None
    assert claims.bundle_id == "shr_1"
    assert claims.grantee_email == "bob@x.com"
    assert claims.expires_at > int(time.time())


@_skip_if_stub
def test_decode_with_wrong_secret_fails() -> None:
    tok = issue_viewer_token(
        bundle_id="shr_1", grantee_email="b@x.com", secret=_SECRET,
    )
    assert decode_viewer_token(tok, secret="other-secret") is None


def test_decode_garbage_returns_none() -> None:
    assert decode_viewer_token("not-a-jwt", secret=_SECRET) is None
    assert decode_viewer_token("", secret=_SECRET) is None


def test_email_lowercased() -> None:
    tok = issue_viewer_token(
        bundle_id="x", grantee_email="Bob@Partner.COM", secret=_SECRET,
    )
    claims = decode_viewer_token(tok, secret=_SECRET)
    assert claims.grantee_email == "bob@partner.com"


def test_ttl_clamped_to_bundle_expiry() -> None:
    """Если bundle expiring через 30 секунд, TTL не может быть больше."""
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)
    tok = issue_viewer_token(
        bundle_id="x", grantee_email="b@x.com",
        ttl_minutes=60,   # был бы 3600s
        bundle_expires_at=soon,
        secret=_SECRET,
    )
    claims = decode_viewer_token(tok, secret=_SECRET)
    remaining = claims.expires_at - int(time.time())
    assert remaining <= 30


def test_minimum_ttl_60s() -> None:
    """ttl_minutes=0 → forced to 60 секунд минимум."""
    tok = issue_viewer_token(
        bundle_id="x", grantee_email="b@x.com",
        ttl_minutes=0, secret=_SECRET,
    )
    claims = decode_viewer_token(tok, secret=_SECRET)
    remaining = claims.expires_at - int(time.time())
    assert remaining >= 50


def test_remaining_seconds_helper() -> None:
    tok = issue_viewer_token(
        bundle_id="x", grantee_email="b@x.com",
        ttl_minutes=10, secret=_SECRET,
    )
    claims = decode_viewer_token(tok, secret=_SECRET)
    rem = viewer_token_remaining_seconds(claims)
    assert 500 <= rem <= 600


def test_issue_requires_bundle_and_email() -> None:
    with pytest.raises(ValueError):
        issue_viewer_token(bundle_id="", grantee_email="x@y.com")
    with pytest.raises(ValueError):
        issue_viewer_token(bundle_id="x", grantee_email="")


def test_audience_constant() -> None:
    assert VIEWER_AUDIENCE == "shared-viewer"
