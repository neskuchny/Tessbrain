"""W31: проверка resource в scope bundle'а.

Используется на каждом partner-side request:
    GET /shared/api/document/<doc_id>?token=<jwt>
    → decode JWT → bundle_id → check_resource_in_scope("document", doc_id, bundle)

Если ресурса нет в grants → ScopeViolation → 403.
"""
from __future__ import annotations

from typing import Optional

from backend.core.sharing.grants import ShareBundle


class ScopeViolation(Exception):
    """Получатель пытается достать ресурс вне выданного scope'а."""

    def __init__(self, resource_type: str, resource_id: str) -> None:
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(
            f"resource not in share scope: {resource_type}:{resource_id}"
        )


def check_resource_in_scope(
    *,
    bundle: ShareBundle,
    resource_type: str,
    resource_id: str,
) -> str:
    """Проверить, что ресурс в bundle scope; вернуть permissions."""
    if not bundle.is_active():
        raise ScopeViolation(resource_type, resource_id)
    for grant in bundle.grants:
        if (grant.resource_type == resource_type
                and grant.resource_id == resource_id):
            return grant.permissions
    raise ScopeViolation(resource_type, resource_id)


def find_grant(
    *,
    bundle: ShareBundle,
    resource_type: str,
    resource_id: str,
) -> Optional[str]:
    """Soft-проверка: возвращает permissions или None."""
    try:
        return check_resource_in_scope(
            bundle=bundle,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    except ScopeViolation:
        return None
