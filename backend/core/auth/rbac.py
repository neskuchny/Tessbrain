"""RBAC core (W9 enterprise-pack).

Минимальная роль-модель, достаточная для:
- защиты mutating endpoints (создание/удаление ресурсов = ADMIN+),
- read-only доступа для VIEWER,
- delegation editor-задач (EDITOR создаёт, но не удаляет).

Источник роли: JWT-claim `role` (или nested `app_metadata.role`).
Источник пользователя/тенанта: `core.observability.tenant_context`.

Дизайн:
- Pure-Python, без зависимостей кроме стандартной библиотеки.
- Iterable enum со встроенным сравнением «роль не ниже X».
- Декоратор `@require_role(Role.ADMIN)` для async-handler'ов.
- Helper `current_role()` для inline-проверок (если декоратор не подходит).

В production roles рекомендуется класть в JWT через IdP (Keycloak/Auth0/
Azure AD/Cognito). Сейчас — явная инициализация через `set_current_role`.
"""
from __future__ import annotations

import contextvars
import functools
import logging
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)


class Role(IntEnum):
    """Иерархия ролей. Большее значение = больше прав.

    `IntEnum` чтобы можно было писать `current_role() >= Role.EDITOR` —
    проверка покрывает «роль не ниже X».
    """
    NONE = 0
    VIEWER = 10
    EDITOR = 20
    ADMIN = 30
    OWNER = 40

    @classmethod
    def parse(cls, value: Any) -> "Role":
        """Безопасно распарсить роль из JWT-claim'а.

        Принимает строки `viewer/editor/admin/owner` (case-insensitive),
        целые числа и сами Role. Любое неизвестное значение → NONE
        (fail-closed: лучше отказать, чем дать лишние права).
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            try:
                return cls(value)
            except ValueError:
                return cls.NONE
        if isinstance(value, str):
            try:
                return cls[value.strip().upper()]
            except KeyError:
                return cls.NONE
        return cls.NONE


_current_role: contextvars.ContextVar[Role] = contextvars.ContextVar(
    "current_role", default=Role.NONE
)


def set_current_role(role: Role) -> contextvars.Token:
    """Установить роль для текущего контекста.

    Возвращает `Token` для последующего `_current_role.reset(token)`.
    Обычно вызывается middleware'ом сразу после `set_current_tenant`.
    """
    return _current_role.set(role)


def current_role() -> Role:
    """Текущая роль или Role.NONE."""
    return _current_role.get()


def reset_current_role(token: contextvars.Token) -> None:
    _current_role.reset(token)


class PermissionDenied(Exception):
    """Поднимается, когда current_role() < required_role.

    Handler должен поймать и вернуть 403 — на уровне ASGI это делает
    общий exception_handler, см. backend/api/app.py.
    """

    def __init__(self, required: Role, actual: Role) -> None:
        self.required = required
        self.actual = actual
        super().__init__(
            f"permission denied: requires {required.name}, got {actual.name}"
        )


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def require_role(required: Role) -> Callable[[F], F]:
    """Декоратор: handler требует минимальной роли.

    Пример:
        @require_role(Role.ADMIN)
        async def delete_project(project_id: str) -> None: ...
    """
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            actual = current_role()
            if actual < required:
                logger.warning(
                    "RBAC deny: %s requires %s, current=%s",
                    fn.__qualname__, required.name, actual.name,
                )
                raise PermissionDenied(required, actual)
            return await fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


def extract_role_from_claims(claims: dict[str, Any]) -> Role:
    """Найти роль в JWT-claims по нескольким известным местам.

    Порядок:
    1. `claims["role"]`
    2. `claims["app_metadata"]["role"]` (Supabase / Auth0 convention)
    3. `claims["realm_access"]["roles"][0]` (Keycloak)
    4. `claims["roles"][0]` (generic list)
    Любое неизвестное → Role.NONE.
    """
    candidates: list[Any] = [claims.get("role")]
    am = claims.get("app_metadata")
    if isinstance(am, dict):
        candidates.append(am.get("role"))
    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        roles = realm.get("roles")
        if isinstance(roles, list) and roles:
            candidates.append(roles[0])
    roles = claims.get("roles")
    if isinstance(roles, list) and roles:
        candidates.append(roles[0])

    for c in candidates:
        if c is None:
            continue
        role = Role.parse(c)
        if role != Role.NONE:
            return role
    return Role.NONE


__all__ = [
    "PermissionDenied",
    "Role",
    "current_role",
    "extract_role_from_claims",
    "require_role",
    "reset_current_role",
    "set_current_role",
]


def _ensure_module_in_package() -> None:
    """No-op — оставлено для будущей регистрации decorator'ов."""
    pass


_ensure_module_in_package()
