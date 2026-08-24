"""W31: append-only audit для shared bundles.

Пишется в `share_audit_views` (table immutable через triggers, см
миграцию 120). Дублируется в основной `audit_log.emit` для consolidated
SOC 2 trail.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


_INSERT_AUDIT_SQL = """
INSERT INTO public.share_audit_views
    (id, bundle_id, grantee_email, action, resource_type, resource_id,
     ip_address, user_agent, metadata)
VALUES (:id, :bundle_id, :grantee_email, :action, :resource_type, :resource_id,
        :ip, :ua, CAST(:metadata AS JSONB))
"""


async def record_view(
    *,
    postgres: Any,
    bundle_id: str,
    action: str,
    grantee_email: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Best-effort write в share_audit_views. Никогда не raises."""
    if not bundle_id or not action:
        return
    try:
        from backend.core.observability import metrics as _m
        _m.record_share_view(action)
    except Exception:
        pass
    if postgres is None:
        # Без БД просто логируем — для dev / тестов.
        logger.info(
            "share_audit (memory): bundle=%s action=%s resource=%s/%s",
            bundle_id, action, resource_type, resource_id,
        )
        return
    try:
        async with postgres.session() as session:
            await session.execute(_INSERT_AUDIT_SQL, {
                "id": f"sav_{uuid.uuid4().hex[:16]}",
                "bundle_id": bundle_id,
                "grantee_email": (grantee_email or "").lower() or None,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "ip": ip_address,
                "ua": (user_agent or "")[:300] or None,
                "metadata": json.dumps(metadata or {}, ensure_ascii=False),
            })
    except Exception as exc:
        logger.warning("share_audit write failed: %s", exc)
