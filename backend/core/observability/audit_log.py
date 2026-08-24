"""Audit log emitter (W9 enterprise-pack).

Записывает события безопасности и data-mutation одновременно:
1. в structlog (всегда) — попадает в logfiles + любые SIEM-коллекторы;
2. в таблицу `public.audit_events` (best-effort) — для запросов из
   приложения и compliance-отчётов.

Дизайн:
- Pure-Python helper, без жёстких зависимостей от LiteStar/HTTP-стека —
  можно вызывать из любого места.
- БД-запись best-effort: если Postgres недоступен или миграция 040 ещё
  не применена, событие всё равно уходит в structlog. Audit-log не должен
  блокировать бизнес-операцию.
- payload_hash вместо raw payload — чтобы не дублировать PII в логах,
  но сохранить «отпечаток» для forensics.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


def _payload_hash(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    try:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canonical = str(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_tenant() -> Optional[str]:
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant()
    except Exception:
        return None


def _get_role_name() -> Optional[str]:
    try:
        from backend.core.auth.rbac import current_role
        r = current_role()
        return r.name if r is not None else None
    except Exception:
        return None


async def emit(
    *,
    action: str,
    user_id: Optional[str] = None,
    resource: Optional[str] = None,
    payload: Any = None,
    metadata: Optional[Mapping[str, Any]] = None,
    request_id: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Записать одно audit-событие.

    Args:
        action: иерархическое имя действия, e.g. 'project.delete', 'gdpr.export'.
                Рекомендация — двухуровневое имя `<resource_type>.<verb>`.
        user_id: субъект действия. Если None — берётся как 'system'.
        resource: целевой ресурс, e.g. 'project:abc-123'.
        payload: значимая нагрузка (хешируется, в БД raw НЕ записывается).
        metadata: произвольные key-value, попадают в metadata JSONB.
        request_id, ip, user_agent: коррелируют с HTTP-запросом.

    Не возбуждает исключений: любая ошибка БД логируется и проглатывается.
    """
    tenant_id = _get_tenant()
    actor_role = _get_role_name()
    payload_hash = _payload_hash(payload)
    md = dict(metadata) if metadata else {}

    # Канонический structlog event — всегда.
    logger.info(
        "audit.%s",
        action,
        extra={
            "audit_action": action,
            "tenant_id": tenant_id,
            "user_id": user_id or "system",
            "actor_role": actor_role,
            "resource": resource,
            "request_id": request_id,
            "ip": ip,
            "user_agent": user_agent,
            "payload_hash": payload_hash,
            "metadata": md,
        },
    )

    # Best-effort запись в БД.
    try:
        from backend.db.postgres import PostgresClient
        client = PostgresClient()
        async with client.session() as session:  # type: ignore[attr-defined]
            await session.execute(
                """
                INSERT INTO public.audit_events
                  (tenant_id, user_id, actor_role, action, resource,
                   request_id, ip, user_agent, payload_hash, metadata)
                VALUES (:tenant_id, :user_id, :actor_role, :action, :resource,
                        :request_id, CAST(:ip AS INET), :user_agent,
                        :payload_hash, CAST(:metadata AS JSONB))
                """,
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "actor_role": actor_role,
                    "action": action,
                    "resource": resource,
                    "request_id": request_id,
                    "ip": ip,
                    "user_agent": user_agent,
                    "payload_hash": payload_hash,
                    "metadata": json.dumps(md, ensure_ascii=False),
                },
            )
    except Exception as exc:
        # Самая частая причина — миграция 040 не применена либо PG offline.
        # В обоих случаях лог уже ушёл, бизнес-операция не должна падать.
        logger.debug("audit_log: DB insert skipped (%s)", exc)


__all__ = ["emit"]
