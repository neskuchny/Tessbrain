"""Unit-тесты для core.sharing.grants (W31, in-memory режим)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from backend.core.sharing.grants import (
    MAX_RESOURCES_PER_BUNDLE,
    ShareGrantInput,
    ShareGrantService,
)


def _run(coro):
    return asyncio.run(coro)


# === ShareGrantInput.validate =========================================

def test_input_validate_ok() -> None:
    ShareGrantInput("document", "doc_1", "read").validate()
    ShareGrantInput("meeting", "m_1", "read+download").validate()


def test_input_invalid_type() -> None:
    with pytest.raises(ValueError):
        ShareGrantInput("blob", "x", "read").validate()


def test_input_invalid_permissions() -> None:
    with pytest.raises(ValueError):
        ShareGrantInput("document", "x", "delete").validate()


def test_input_empty_resource_id() -> None:
    with pytest.raises(ValueError):
        ShareGrantInput("document", "", "read").validate()


# === create_bundle ====================================================

def test_create_bundle_ok() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="alice",
        grantee_email="bob@partner.com",
        resources=[ShareGrantInput("document", "doc_1")],
        note="board pack",
        ttl_days=30,
    ))
    assert b.id.startswith("shr_")
    assert b.grantee_email == "bob@partner.com"
    assert b.note == "board pack"
    assert len(b.token) >= 20
    assert len(b.grants) == 1
    assert b.grants[0].resource_id == "doc_1"
    assert b.is_active() is True


def test_create_normalizes_email_lowercase() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="alice",
        grantee_email="  Bob@Partner.COM  ",
        resources=[ShareGrantInput("document", "doc_1")],
    ))
    assert b.grantee_email == "bob@partner.com"


def test_create_invalid_email_raises() -> None:
    svc = ShareGrantService()
    with pytest.raises(ValueError):
        _run(svc.create_bundle(
            owner_user_id="a", grantee_email="not-email",
            resources=[ShareGrantInput("document", "x")],
        ))


def test_create_no_owner_raises() -> None:
    svc = ShareGrantService()
    with pytest.raises(ValueError):
        _run(svc.create_bundle(
            owner_user_id="", grantee_email="b@x.com",
            resources=[ShareGrantInput("document", "x")],
        ))


def test_create_empty_resources_raises() -> None:
    svc = ShareGrantService()
    with pytest.raises(ValueError):
        _run(svc.create_bundle(
            owner_user_id="a", grantee_email="b@x.com", resources=[],
        ))


def test_create_too_many_resources_raises() -> None:
    svc = ShareGrantService()
    too_many = [
        ShareGrantInput("document", f"d_{i}")
        for i in range(MAX_RESOURCES_PER_BUNDLE + 1)
    ]
    with pytest.raises(ValueError):
        _run(svc.create_bundle(
            owner_user_id="a", grantee_email="b@x.com", resources=too_many,
        ))


def test_create_clamps_ttl_to_max() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
        ttl_days=99999,
    ))
    exp = datetime.fromisoformat(b.expires_at)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    assert exp - datetime.now(timezone.utc) <= timedelta(days=366)


# === get_by_token =====================================================

def test_get_by_token_returns_bundle() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
    ))
    found = _run(svc.get_by_token(token=b.token))
    assert found is not None
    assert found.id == b.id


def test_get_by_unknown_token_returns_none() -> None:
    svc = ShareGrantService()
    assert _run(svc.get_by_token(token="bogus")) is None


def test_get_by_empty_token_returns_none() -> None:
    svc = ShareGrantService()
    assert _run(svc.get_by_token(token="")) is None


# === list_for_user ====================================================

def test_list_for_user_returns_owners_bundles() -> None:
    svc = ShareGrantService()
    a = _run(svc.create_bundle(
        owner_user_id="alice", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
    ))
    _ = _run(svc.create_bundle(
        owner_user_id="charlie", grantee_email="d@x.com",
        resources=[ShareGrantInput("document", "y")],
    ))
    out = _run(svc.list_for_user(user_id="alice"))
    assert {b.id for b in out} == {a.id}


def test_list_for_empty_user_returns_empty() -> None:
    svc = ShareGrantService()
    assert _run(svc.list_for_user(user_id="")) == []


# === revoke ===========================================================

def test_revoke_deactivates_bundle() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
    ))
    assert b.is_active() is True
    ok = _run(svc.revoke(bundle_id=b.id))
    assert ok is True
    found = _run(svc.get_by_token(token=b.token))
    assert found.is_active() is False


def test_revoke_unknown_returns_false() -> None:
    svc = ShareGrantService()
    assert _run(svc.revoke(bundle_id="bogus")) is False
    assert _run(svc.revoke(bundle_id="")) is False


def test_revoke_twice_idempotent() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
    ))
    assert _run(svc.revoke(bundle_id=b.id)) is True
    # Второй revoke по уже revoked — not active path → возвращает False (нечего ревокать).
    assert _run(svc.revoke(bundle_id=b.id)) is False


# === to_dict include_token gating =====================================

def test_to_dict_hides_token_by_default() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
    ))
    d = b.to_dict()
    assert "token" not in d
    d2 = b.to_dict(include_token=True)
    assert d2["token"] == b.token


def test_to_dict_includes_resources() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[
            ShareGrantInput("document", "x"),
            ShareGrantInput("meeting", "m1"),
        ],
    ))
    d = b.to_dict()
    types = {r["resource_type"] for r in d["resources"]}
    assert types == {"document", "meeting"}
