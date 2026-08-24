"""Unit-тесты для core.auth.rbac (W9 enterprise-pack)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.auth.rbac import (
    PermissionDenied,
    Role,
    current_role,
    extract_role_from_claims,
    require_role,
    reset_current_role,
    set_current_role,
)

# === Role enum ===========================================================

def test_role_ordering() -> None:
    assert Role.NONE < Role.VIEWER < Role.EDITOR < Role.ADMIN < Role.OWNER


@pytest.mark.parametrize("value,expected", [
    ("admin", Role.ADMIN),
    ("ADMIN", Role.ADMIN),
    ("  Editor  ", Role.EDITOR),
    ("viewer", Role.VIEWER),
    ("owner", Role.OWNER),
    ("none", Role.NONE),
    (Role.ADMIN, Role.ADMIN),
    (30, Role.ADMIN),
    (10, Role.VIEWER),
])
def test_role_parse_known(value, expected) -> None:
    assert Role.parse(value) is expected


@pytest.mark.parametrize("value", [
    "superuser", "root", "moderator", None, 999, -1, [], {},
])
def test_role_parse_unknown_returns_none(value) -> None:
    """Fail-closed: неизвестное значение → NONE, не ADMIN."""
    assert Role.parse(value) is Role.NONE


# === Contextvar ==========================================================

def test_default_role_is_none() -> None:
    assert current_role() is Role.NONE


def test_set_and_reset_role() -> None:
    token = set_current_role(Role.EDITOR)
    try:
        assert current_role() is Role.EDITOR
    finally:
        reset_current_role(token)
    assert current_role() is Role.NONE


# === require_role decorator ==============================================

def _run(coro):
    return asyncio.run(coro)


def test_require_role_allows_higher_role() -> None:
    @require_role(Role.EDITOR)
    async def handler() -> str:
        return "ok"

    token = set_current_role(Role.ADMIN)
    try:
        assert _run(handler()) == "ok"
    finally:
        reset_current_role(token)


def test_require_role_allows_exact_role() -> None:
    @require_role(Role.ADMIN)
    async def handler() -> str:
        return "ok"

    token = set_current_role(Role.ADMIN)
    try:
        assert _run(handler()) == "ok"
    finally:
        reset_current_role(token)


def test_require_role_rejects_lower_role() -> None:
    @require_role(Role.ADMIN)
    async def handler() -> str:
        return "ok"

    token = set_current_role(Role.EDITOR)
    try:
        with pytest.raises(PermissionDenied) as exc:
            _run(handler())
        assert exc.value.required is Role.ADMIN
        assert exc.value.actual is Role.EDITOR
    finally:
        reset_current_role(token)


def test_require_role_rejects_anonymous() -> None:
    """Без явной роли — отказ. Это критично: handler не должен случайно
    обработать unauth-запрос как ADMIN."""

    @require_role(Role.VIEWER)
    async def handler() -> str:
        return "ok"

    with pytest.raises(PermissionDenied):
        _run(handler())


# === extract_role_from_claims ============================================

def test_extract_from_top_level_claim() -> None:
    assert extract_role_from_claims({"role": "admin"}) is Role.ADMIN


def test_extract_from_app_metadata() -> None:
    """Supabase / Auth0 convention."""
    claims = {"app_metadata": {"role": "editor"}}
    assert extract_role_from_claims(claims) is Role.EDITOR


def test_extract_from_keycloak_realm_access() -> None:
    claims = {"realm_access": {"roles": ["viewer", "user"]}}
    assert extract_role_from_claims(claims) is Role.VIEWER


def test_extract_from_generic_roles_list() -> None:
    assert extract_role_from_claims({"roles": ["owner"]}) is Role.OWNER


def test_extract_returns_none_when_unknown() -> None:
    assert extract_role_from_claims({"role": "superhero"}) is Role.NONE
    assert extract_role_from_claims({}) is Role.NONE


def test_extract_picks_first_known() -> None:
    """top-level role > app_metadata > keycloak > generic, первое известное."""
    claims = {
        "role": "viewer",  # known
        "app_metadata": {"role": "admin"},  # тоже known, но top-level выигрывает
    }
    assert extract_role_from_claims(claims) is Role.VIEWER


def test_extract_skips_unknown_falls_through() -> None:
    """Если top-level mусор — продолжаем искать в других местах."""
    claims = {
        "role": "garbage",
        "app_metadata": {"role": "editor"},
    }
    assert extract_role_from_claims(claims) is Role.EDITOR
