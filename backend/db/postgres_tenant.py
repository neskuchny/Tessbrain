"""Хелпер для tenant-aware Postgres-сессий (Phase 7e).

Идея: после миграции `020_multitenant_rls.sql` каждая SQL-сессия должна
выполнять `SET LOCAL app.tenant_id = '<tenant>'`, чтобы политика
`tenant_rls_predicate` пропускала запросы.

Использование:

    from sqlalchemy.ext.asyncio import AsyncSession
    from backend.db.postgres_tenant import set_tenant_for_session

    async with pg.session() as session:
        await set_tenant_for_session(session)        # читает tenant из контекста
        rows = await session.execute(text("SELECT * FROM documents"))

ИЛИ декоратор для legacy кода:

    @with_tenant_session
    async def my_handler(session): ...

Если tenant_id отсутствует в контексте — функция ничего не делает, и RLS
позволит видеть только legacy-строки с tenant_id IS NULL (промежуточное
состояние во время backfill'а).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _current_tenant_id() -> Optional[str]:
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


async def set_tenant_for_session(session: Any, tenant_id: Optional[str] = None) -> None:
    """Применить SET LOCAL app.tenant_id для текущей транзакции.

    Если tenant_id не передан явно, берётся из observability.tenant_context.
    Если и в контексте пусто — просто no-op (и legacy-строки с tenant_id IS NULL
    будут видны).

    Используется только внутри `async with pg.session()` или другого
    транзакционного scope'а: SET LOCAL действует до конца транзакции.
    """
    tenant = tenant_id if tenant_id is not None else _current_tenant_id()
    if tenant is None:
        return

    # Защита от SQL-injection: tenant_id должен быть UUID/коротким хешем.
    # Не доверяем «свободному» строковому значению.
    if not _is_safe_tenant_id(tenant):
        raise ValueError(f"Unsafe tenant_id format: {tenant!r}")

    try:
        from sqlalchemy import text
        # Используем set_config(name, value, local) — функция, а не SET LOCAL,
        # чтобы можно было параметризовать значение и не строить SQL руками.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": tenant},
        )
    except Exception as exc:
        logger.warning("set_tenant_for_session failed (RLS may reject queries): %s", exc)


async def set_persona_tenant(session: Any, tenant: str) -> None:
    """Явно выставить app.tenant_id для persona-сессии (Phase A.6 RLS).

    Persona-таблицы хранят tenant_id == user_id (PK personas = user_id), и
    persona_tenant_rls_predicate (миграция 210) изолирует по этому значению.
    Здесь мы выставляем app.tenant_id = переданный tenant (обычно user_id
    обрабатываемой persona), чтобы СУБД на уровне RLS блокировала чтение/запись
    чужих строк — defense-in-depth поверх app-фильтра по user_id.

    Best-effort: при пустом/небезопасном значении или сбое — no-op (RLS тогда
    действует как до адаптации: невыставленный setting → строки видны). Должно
    вызываться ВНУТРИ транзакционного scope (pg.session), т.к. SET LOCAL живёт
    до конца транзакции.
    """
    if not tenant or not _is_safe_tenant_id(str(tenant)):
        return
    try:
        from sqlalchemy import text
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": str(tenant)},
        )
    except Exception as exc:
        logger.debug("set_persona_tenant failed (RLS lenient fallback): %s", exc)


_SAFE_RE = None


def _is_safe_tenant_id(value: str) -> bool:
    """tenant_id принимается из JWT/header — фильтруем только безопасные форматы.

    Принимаем UUID, slug ([a-zA-Z0-9_-]{1,64}). Всё остальное отбрасываем,
    чтобы исключить прокидывание `'; DROP TABLE...` через SET.
    """
    global _SAFE_RE
    if _SAFE_RE is None:
        import re
        _SAFE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
    return bool(_SAFE_RE.match(value))
