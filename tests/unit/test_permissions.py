"""Unit-тесты для core.auth.permissions (W15)."""
from __future__ import annotations

import asyncio

import pytest
from backend.core.auth.permissions import (
    Permission,
    get_role_permissions,
    has_permission,
    require_permission,
    reset_role_permissions,
    set_role_permissions,
)
from backend.core.auth.rbac import (
    PermissionDenied,
    Role,
    reset_current_role,
    set_current_role,
)


@pytest.fixture(autouse=True)
def _reset_perms_between_tests() -> None:
    """Каждый тест стартует с дефолт-mapping'ом."""
    reset_role_permissions()
    yield
    reset_role_permissions()


# === Default role-permission mapping =====================================

def test_none_has_no_permissions() -> None:
    perms = get_role_permissions(Role.NONE)
    assert perms == frozenset()


def test_viewer_has_read_only() -> None:
    perms = get_role_permissions(Role.VIEWER)
    assert Permission.PROJECT_READ in perms
    assert Permission.PROJECT_WRITE not in perms
    assert Permission.PROJECT_DELETE not in perms


def test_editor_has_write_not_delete() -> None:
    perms = get_role_permissions(Role.EDITOR)
    assert Permission.PROJECT_READ in perms
    assert Permission.PROJECT_WRITE in perms
    assert Permission.PROJECT_DELETE not in perms


def test_admin_has_delete_and_admin_tools() -> None:
    perms = get_role_permissions(Role.ADMIN)
    assert Permission.PROJECT_DELETE in perms
    assert Permission.ADMIN_MIGRATE in perms
    assert Permission.AUDIT_READ in perms
    assert Permission.SCIM_PROVISION in perms


def test_owner_has_user_delete() -> None:
    """USER_DELETE — только OWNER, чтобы случайный admin не снёс."""
    assert Permission.USER_DELETE in get_role_permissions(Role.OWNER)
    assert Permission.USER_DELETE not in get_role_permissions(Role.ADMIN)


# === has_permission ======================================================

def test_has_permission_uses_current_role() -> None:
    token = set_current_role(Role.EDITOR)
    try:
        assert has_permission(Permission.PROJECT_WRITE) is True
        assert has_permission(Permission.PROJECT_DELETE) is False
    finally:
        reset_current_role(token)


def test_has_permission_explicit_role_overrides_context() -> None:
    """Явный role= имеет приоритет над contextvar."""
    token = set_current_role(Role.NONE)
    try:
        assert has_permission(Permission.PROJECT_WRITE, role=Role.EDITOR) is True
    finally:
        reset_current_role(token)


def test_has_permission_anonymous_denies() -> None:
    """Без role context (Role.NONE) — все permissions отказаны."""
    assert has_permission(Permission.PROJECT_READ) is False


# === set_role_permissions / overrides ====================================

def test_override_role_permissions() -> None:
    """Tenant может расширить VIEWER до PROJECT_WRITE через override."""
    set_role_permissions(Role.VIEWER, frozenset({
        Permission.PROJECT_READ, Permission.PROJECT_WRITE,
    }))
    token = set_current_role(Role.VIEWER)
    try:
        assert has_permission(Permission.PROJECT_WRITE) is True
    finally:
        reset_current_role(token)


def test_reset_restores_defaults() -> None:
    set_role_permissions(Role.VIEWER, frozenset())
    reset_role_permissions()
    assert Permission.PROJECT_READ in get_role_permissions(Role.VIEWER)


# === require_permission decorator ========================================

def _run(coro):
    return asyncio.run(coro)


def test_require_permission_allows_when_present() -> None:
    @require_permission(Permission.PROJECT_WRITE)
    async def handler() -> str:
        return "ok"

    token = set_current_role(Role.EDITOR)
    try:
        assert _run(handler()) == "ok"
    finally:
        reset_current_role(token)


def test_require_permission_denies_when_missing() -> None:
    @require_permission(Permission.PROJECT_DELETE)
    async def handler() -> str:
        return "ok"

    token = set_current_role(Role.EDITOR)
    try:
        with pytest.raises(PermissionDenied):
            _run(handler())
    finally:
        reset_current_role(token)


def test_require_permission_anonymous_denies() -> None:
    @require_permission(Permission.PROJECT_READ)
    async def handler() -> str:
        return "ok"

    with pytest.raises(PermissionDenied):
        _run(handler())


def test_permission_enum_values_are_strings() -> None:
    """Permission — str-Enum, можно использовать как ключ JSON."""
    assert Permission.PROJECT_READ.value == "project:read"
    assert Permission.SCIM_PROVISION.value == "scim:provision"
