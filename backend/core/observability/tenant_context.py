"""Каноничный источник tenant_id для текущего запроса/таски.

Архитектура:
- TenantResolverMiddleware (api/middleware/tenant_resolver.py) на каждом
  HTTP-запросе извлекает tenant_id из JWT-claim'а или X-Tenant-Id заголовка
  и кладёт в contextvar.
- Любой модуль (Qdrant filter, Neo4j filter, UsageTracker, LLM-quota) читает
  его через get_current_tenant(). Это устраняет десинхронизацию между
  middleware и сервисами.

Tenant_id:
- В JWT-токене (Supabase / собственный) ожидается claim `tenant_id` или
  `org_id` или `app_metadata.tenant_id`.
- В заголовке `X-Tenant-Id`, если клиент явно его прислал (полезно для
  внутренних сервисов и тестов).
- Если ничего не задано — `None`. Сервисы должны решать сами, что делать
  с `None`: либо пропустить (single-tenant дев-режим), либо отклонить
  (production multi-tenant контракт).
"""
from __future__ import annotations

import contextvars
from typing import Optional

_current_tenant: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "tenant_id",
    default=None,
)


def set_current_tenant(tenant_id: Optional[str]) -> contextvars.Token:
    """Установить tenant_id для текущего контекста. Возвращает Token для reset."""
    return _current_tenant.set(tenant_id)


def get_current_tenant() -> Optional[str]:
    """Текущий tenant_id или None."""
    return _current_tenant.get()


def reset_current_tenant(token: contextvars.Token) -> None:
    """Восстановить предыдущее значение tenant_id."""
    _current_tenant.reset(token)


def require_current_tenant() -> str:
    """Текущий tenant_id или RuntimeError. Используется в storage-слое, где
    обращение без tenant'а считается багом интеграции (например, забыли
    подключить middleware)."""
    tenant = _current_tenant.get()
    if tenant is None:
        raise RuntimeError(
            "tenant_id is not set in current context. "
            "Ensure TenantResolverMiddleware is wired or set_current_tenant() was called.",
        )
    return tenant
