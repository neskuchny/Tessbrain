# -*- coding: utf-8 -*-
"""Свой лимит и расход месяца — то, что нужно любой установке.

Что здесь НЕ лежит: цены, чек-аут и webhook платёжного провайдера. Они
переехали в billing_payments.py, потому что нужны только тому, кто
продаёт подписки от своего имени; компания, поднявшая систему у себя,
ими не пользуется, а прайс-сетку в открытом виде публиковать нельзя.

Пул месяца читается из tenant_limits — оттуда же, откуда его берёт
энфорсинг квот (core/llm/quota.py). Раньше маршрут брал пул из
прайс-пресета, а энфорсинг — из таблицы, и эти два числа могли
разойтись: пользователь видел один остаток, а упирался в другой.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Request, Router, get, post
from litestar.params import Body, Parameter

logger = logging.getLogger(__name__)


def _uid(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _ = trusted_user_id(request.headers, user_id or "")
        return uid or (user_id or "")
    except Exception:
        return user_id or ""


def _my_tenant(uid: str) -> str:
    """Тенант текущего пользователя. Convention 220: tenant_id == user_id
    для tenant-less; иначе из контекста запроса (middleware)."""
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return str(get_current_tenant() or uid or "")
    except Exception:
        return uid or ""


@get("/me")
async def my_billing(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Свой тариф + расход месяца vs включённый пул. Свой тенант, без admin."""
    uid = _uid(request, user_id)
    tenant = _my_tenant(uid)
    from backend.core.auth.tenant_limits import get_tenant_limits
    limits = await get_tenant_limits(tenant)
    used_month = 0.0
    try:
        from backend.core.llm.quota import _daily_metric, _window_since
        used_month = _daily_metric(
            "", scope="tenant", subject_id=tenant,
            metric="total_cost", since_date=_window_since("month"))
    except Exception:
        logger.debug("month usage lookup failed", exc_info=True)
    plan_key = str(limits.get("plan") or "free")
    # Пул — из tenant_limits, то есть из того же места, где его читает
    # энфорсинг. Прайс-пресет здесь больше не участвует: он жил в другой
    # таблице и мог разойтись с реально применяемым лимитом.
    pool = limits.get("monthly_tokens_usd")
    return {
        "tenant_id": tenant,
        "plan": plan_key,
        "limits": limits,
        "month_used_usd": round(float(used_month or 0.0), 4),
        "included_monthly_usd": pool,
        "pool_remaining_usd": (round(max(0.0, float(pool) - float(used_month or 0.0)), 4)
                               if isinstance(pool, (int, float)) and pool else None),
        "seats": limits.get("seats"),
    }


router = Router(path="/billing",
                route_handlers=[my_billing],
                tags=["Billing"])
