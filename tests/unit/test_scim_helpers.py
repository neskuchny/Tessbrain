"""Unit-тесты для backend.api.routes.scim — pure helpers (W15).

Полные HTTP-тесты SCIM endpoints требуют поднятого UserProfileService
с реальной БД — это integration scope. Здесь покрываем filter-parser
и SCIM ↔ profile конверторы.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

# Litestar импортируется внутри scim.py на module-level. В sandbox litestar
# не установлен — load через importlib и заглушка для litestar.
_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend" / "api" / "routes" / "scim.py"
)


def _ensure_litestar_stub() -> None:
    """Если litestar не установлен — minimal stub для импорта routes/scim.py."""
    try:
        import litestar
    except ImportError:
        ls = types.ModuleType("litestar")
        ls.Router = lambda **kw: None  # type: ignore[attr-defined]
        for verb in ("get", "post", "put", "patch", "delete"):
            setattr(ls, verb, lambda *a, **kw: (lambda fn: fn))
        sys.modules["litestar"] = ls

        exc_mod = types.ModuleType("litestar.exceptions")

        class HTTPException(Exception):
            def __init__(self, status_code: int = 500, detail: str = "") -> None:
                self.status_code = status_code
                self.detail = detail

        exc_mod.HTTPException = HTTPException
        sys.modules["litestar.exceptions"] = exc_mod

        params_mod = types.ModuleType("litestar.params")
        params_mod.Parameter = lambda **kw: None
        sys.modules["litestar.params"] = params_mod


def _load_scim() -> types.ModuleType:
    _ensure_litestar_stub()
    _spec = importlib.util.spec_from_file_location("_scim_isolated", _PATH)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    return _module


_scim = _load_scim()


# === filter parser =======================================================

@pytest.mark.parametrize("filter_str,expected", [
    ('userName eq "alice"', ("username", "alice")),
    ('userName Eq "Bob"', ("username", "Bob")),
    ('email eq "a@b.com"', ("email", "a@b.com")),
    ('id eq "user-123"', ("id", "user-123")),
])
def test_parse_known_filters(filter_str: str, expected: tuple[str, str]) -> None:
    assert _scim._parse_filter(filter_str) == expected


@pytest.mark.parametrize("filter_str", [
    None,
    "",
    "userName co \"alice\"",  # contains operator — не поддерживаем
    "phoneNumber eq \"+1\"",   # неподдерживаемый attr
    "userName eq alice",         # без кавычек
    "garbage",
])
def test_parse_invalid_filters_returns_none(filter_str) -> None:
    assert _scim._parse_filter(filter_str) is None


# === user_to_scim serializer =============================================

def test_user_to_scim_minimal() -> None:
    out = _scim._user_to_scim("u-1", {"email": "a@b.com"})
    assert out["id"] == "u-1"
    assert out["userName"] == "a@b.com"
    assert out["active"] is True
    assert out["emails"][0]["value"] == "a@b.com"
    assert out["emails"][0]["primary"] is True
    assert _scim._SCIM_USER_SCHEMA in out["schemas"]


def test_user_to_scim_inactive_when_restricted() -> None:
    out = _scim._user_to_scim("u-1", {
        "email": "a@b.com", "processing_restricted": True,
    })
    assert out["active"] is False


def test_user_to_scim_inactive_when_deleted() -> None:
    out = _scim._user_to_scim("u-1", {
        "email": "a@b.com", "deleted_at": "2026-01-01T00:00:00Z",
    })
    assert out["active"] is False


def test_user_to_scim_no_email_returns_empty_emails() -> None:
    out = _scim._user_to_scim("u-1", {})
    assert out["emails"] == []
    assert out["userName"] == "u-1"  # fallback to user_id


def test_user_to_scim_uses_userName_field() -> None:
    out = _scim._user_to_scim("u-1", {"user_name": "alice", "email": "a@b.com"})
    assert out["userName"] == "alice"


# === scim_to_profile_updates ============================================

def test_scim_to_updates_basic() -> None:
    upd = _scim._scim_to_profile_updates({
        "userName": "alice",
        "emails": [{"value": "a@b.com", "primary": True, "type": "work"}],
        "active": True,
        "name": {"givenName": "Alice", "familyName": "Smith"},
    })
    assert upd["user_name"] == "alice"
    assert upd["email"] == "a@b.com"
    assert upd["processing_restricted"] is False
    assert upd["given_name"] == "Alice"
    assert upd["family_name"] == "Smith"


def test_scim_to_updates_inactive_sets_restriction() -> None:
    """active=false должен трансформироваться в processing_restricted=true."""
    upd = _scim._scim_to_profile_updates({"active": False})
    assert upd["processing_restricted"] is True


def test_scim_to_updates_picks_primary_email() -> None:
    upd = _scim._scim_to_profile_updates({
        "emails": [
            {"value": "personal@x.com", "type": "home"},
            {"value": "work@x.com", "type": "work", "primary": True},
        ],
    })
    assert upd["email"] == "work@x.com"


def test_scim_to_updates_no_primary_picks_first() -> None:
    upd = _scim._scim_to_profile_updates({
        "emails": [
            {"value": "first@x.com"},
            {"value": "second@x.com"},
        ],
    })
    assert upd["email"] == "first@x.com"


def test_scim_to_updates_empty_returns_empty() -> None:
    assert _scim._scim_to_profile_updates({}) == {}


def test_scim_error_format() -> None:
    err = _scim._scim_error(404, "user not found")
    assert err["status"] == "404"
    assert err["detail"] == "user not found"
    assert _scim._SCIM_ERROR_SCHEMA in err["schemas"]


def test_scim_error_with_scim_type() -> None:
    err = _scim._scim_error(400, "bad input", scim_type="invalidValue")
    assert err["scimType"] == "invalidValue"
