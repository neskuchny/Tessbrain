"""Scope-based permissions (W15).

Дополняет role-based RBAC (`rbac.py`) explicit-scopes для случаев,
когда чистая иерархия ролей недостаточна — например, EDITOR может
читать ВСЕ projects, но writing допустим только в project'ах своего
team. Здесь решение принимается per-scope.

Дизайн:
- `Permission` — Enum со строковыми scope (`project:read`, `project:write`).
  Иерархия НЕ автоматическая — VIEWER не получает `project:read`
  бесплатно; mapping явный в `_DEFAULT_ROLE_PERMS`.
- `has_permission(perm)` — точечная проверка на текущей роли в context.
- `@require_permission(perm)` — декоратор, аналог `@require_role`.
- Кастомный mapping через `set_role_permissions(role, perms)` — для
  случаев когда конкретный tenant хочет дать VIEWER расширенные права.

Permissions в системе пока НЕ навешены на endpoints — это инфраструктура
готовая к расширению. Существующие endpoints используют role-checks
напрямую; мигрировать на scope-checks по мере необходимости.
"""
from __future__ import annotations

import functools
import logging
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

from .rbac import PermissionDenied, Role, current_role

logger = logging.getLogger(__name__)


class Permission(str, Enum):
    """Стандартные scope'ы. Расширяется при появлении новых ресурсов."""

    # Project resource
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    PROJECT_DELETE = "project:delete"

    # User resource (для admin actions)
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    USER_DELETE = "user:delete"

    # Audit log
    AUDIT_READ = "audit:read"

    # Admin tools (миграции, system info)
    ADMIN_MIGRATE = "admin:migrate"
    ADMIN_BACKUP = "admin:backup"

    # GDPR endpoints (self-service всегда доступны;
    # admin-инициированные delete — это admin:user.delete)
    GDPR_SELF = "gdpr:self"

    # SCIM (provisioning)
    SCIM_PROVISION = "scim:provision"


# Default role → permission mapping. Иерархичный: ADMIN включает
# всё что может EDITOR, OWNER — всё что ADMIN. NONE — ничего.
_DEFAULT_ROLE_PERMS: dict[Role, frozenset[Permission]] = {
    Role.NONE: frozenset(),
    Role.VIEWER: frozenset({
        Permission.PROJECT_READ,
        Permission.GDPR_SELF,
    }),
    Role.EDITOR: frozenset({
        Permission.PROJECT_READ,
        Permission.PROJECT_WRITE,
        Permission.GDPR_SELF,
    }),
    Role.ADMIN: frozenset({
        Permission.PROJECT_READ,
        Permission.PROJECT_WRITE,
        Permission.PROJECT_DELETE,
        Permission.USER_READ,
        Permission.USER_WRITE,
        Permission.AUDIT_READ,
        Permission.ADMIN_MIGRATE,
        Permission.ADMIN_BACKUP,
        Permission.GDPR_SELF,
        Permission.SCIM_PROVISION,
    }),
    Role.OWNER: frozenset({
        # OWNER = все permissions. Реализовано через перечисление
        # явно (а не через `frozenset(Permission)`) чтобы новые
        # permissions требовали осознанного решения «давать ли OWNER'у».
        Permission.PROJECT_READ,
        Permission.PROJECT_WRITE,
        Permission.PROJECT_DELETE,
        Permission.USER_READ,
        Permission.USER_WRITE,
        Permission.USER_DELETE,
        Permission.AUDIT_READ,
        Permission.ADMIN_MIGRATE,
        Permission.ADMIN_BACKUP,
        Permission.GDPR_SELF,
        Permission.SCIM_PROVISION,
    }),
}

# Mutable copy — позволяет per-tenant overrides через set_role_permissions.
_role_perms: dict[Role, frozenset[Permission]] = dict(_DEFAULT_ROLE_PERMS)


def get_role_permissions(role: Role) -> frozenset[Permission]:
    return _role_perms.get(role, frozenset())


def set_role_permissions(role: Role, perms: frozenset[Permission]) -> None:
    """Override default permissions for a role.

    Для tests/per-tenant настройки. В прод — лучше через config-file
    чтобы изменения были voiced и audit-able.
    """
    _role_perms[role] = perms


def reset_role_permissions() -> None:
    """Восстановить default mapping. Используется в тестах."""
    global _role_perms
    _role_perms = dict(_DEFAULT_ROLE_PERMS)


def has_permission(perm: Permission, *, role: Role | None = None) -> bool:
    """True если current_role (или указанная) имеет permission."""
    actual = role if role is not None else current_role()
    return perm in _role_perms.get(actual, frozenset())


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def require_permission(perm: Permission) -> Callable[[F], F]:
    """Декоратор: handler требует конкретного permission.

    Поднимает `PermissionDenied(required=Role.NONE, actual=current_role)` —
    `required` тут не очень осмысленен, но мы переиспользуем тот же
    exception для совместимости с auth-middleware.
    """
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            actual = current_role()
            if not has_permission(perm, role=actual):
                logger.warning(
                    "Permission deny: %s requires %s, current_role=%s",
                    fn.__qualname__, perm.value, actual.name,
                )
                raise PermissionDenied(Role.NONE, actual)
            return await fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


__all__ = [
    "Permission",
    "get_role_permissions",
    "has_permission",
    "require_permission",
    "reset_role_permissions",
    "set_role_permissions",
]
