"""Unit-тесты для core.sharing.scope (W31)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.sharing.grants import ShareGrantInput, ShareGrantService
from backend.core.sharing.scope import (
    ScopeViolation,
    check_resource_in_scope,
    find_grant,
)


def _run(coro):
    return asyncio.run(coro)


def _make_bundle(resources):
    svc = ShareGrantService()
    return _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=resources,
    ))


def test_in_scope_returns_permissions() -> None:
    b = _make_bundle([ShareGrantInput("document", "doc_1", "read")])
    assert check_resource_in_scope(
        bundle=b, resource_type="document", resource_id="doc_1",
    ) == "read"


def test_out_of_scope_raises() -> None:
    b = _make_bundle([ShareGrantInput("document", "doc_1")])
    with pytest.raises(ScopeViolation) as exc:
        check_resource_in_scope(
            bundle=b, resource_type="document", resource_id="other",
        )
    assert exc.value.resource_id == "other"


def test_wrong_type_same_id_raises() -> None:
    b = _make_bundle([ShareGrantInput("document", "x")])
    with pytest.raises(ScopeViolation):
        check_resource_in_scope(
            bundle=b, resource_type="meeting", resource_id="x",
        )


def test_revoked_bundle_blocks_everything() -> None:
    svc = ShareGrantService()
    b = _run(svc.create_bundle(
        owner_user_id="a", grantee_email="b@x.com",
        resources=[ShareGrantInput("document", "x")],
    ))
    _run(svc.revoke(bundle_id=b.id))
    with pytest.raises(ScopeViolation):
        check_resource_in_scope(
            bundle=b, resource_type="document", resource_id="x",
        )


def test_find_grant_returns_none_when_out_of_scope() -> None:
    b = _make_bundle([ShareGrantInput("document", "x")])
    assert find_grant(
        bundle=b, resource_type="meeting", resource_id="x",
    ) is None


def test_find_grant_returns_permissions_when_in_scope() -> None:
    b = _make_bundle([ShareGrantInput("document", "x", "read+download")])
    assert find_grant(
        bundle=b, resource_type="document", resource_id="x",
    ) == "read+download"
