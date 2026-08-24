# -*- coding: utf-8 -*-
"""Админ-API тарифов (docs/BILLING_MODEL.md).

- GET  /admin/billing/tenant/{tenant_id}   — текущий тариф+капы тенанта
- POST /admin/billing/tenant/{tenant_id}   — применить тариф / докупку
       body: {plan: "start", extra_topup_usd?: 40.0}

Это операторский слой ДО платёжки: после оплаты (счёт юрлица или вебхук
ЮKassa `payment.succeeded`) вызывается apply_plan — вся квотная механика
уже за ним (месячный пул энфорсится на каждом LLM-вызове). Admin-guard —
как у /admin/llm-profiles (JWT role >= ADMIN).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Body, Parameter

logger = logging.getLogger(__name__)


def _require_admin(authorization: Optional[str]) -> str:
    """Требует роль ADMIN. Раньше роль бралась из `decode_claims_guarded`,
    который в compat-режиме (без JWT-секрета или при enable_strict_chat_auth=off)
    возвращает claims БЕЗ проверки подписи — то есть подделанный `role: admin`
    проходил. Теперь роль признаётся только из подтверждённой подписи
    (backend.core.auth.admin_guard).
    """
    from backend.core.auth.admin_guard import require_admin
    return require_admin(authorization)[0]


@get("/tenant/{tenant_id:str}")
async def get_tenant_billing(
    tenant_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Текущий тариф/капы тенанта (из tenant_limits) + расход месяца."""
    _require_admin(authorization)
    from backend.core.auth.tenant_limits import get_tenant_limits
    limits = await get_tenant_limits(tenant_id)
    used_month = None
    try:
        from backend.core.llm.quota import _daily_metric, _window_since
        used_month = _daily_metric(
            "", scope="tenant", subject_id=tenant_id,
            metric="total_cost", since_date=_window_since("month"))
    except Exception:
        logger.debug("month usage lookup failed", exc_info=True)
    return {"tenant_id": tenant_id, "limits": limits,
            "month_used_usd": used_month}


@post("/tenant/{tenant_id:str}")
async def set_tenant_billing(
    tenant_id: str,
    data: dict = Body(),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Выставить лимиты тенанту напрямую: месячный пул, места, доступ к
    премиум-моделям.

    Раньше маршрут принимал ключ тарифа и разворачивал его через
    прайс-пресеты. Это связывало управление лимитами с коммерческой
    сеткой: администратор установки, которая ничего не продаёт, не мог
    поднять кому-то пул, не выбрав «тариф». Теперь числа задаются
    прямо — а тем, кто продаёт подписки, тариф разворачивает
    billing_payments.py на webhook оплаты.
    """
    admin_id = _require_admin(authorization)
    d = data or {}
    plan = str(d.get("plan") or "").strip() or None
    monthly = d.get("monthly_tokens_usd")
    seats = d.get("seats")
    premium = d.get("can_use_premium_tier")
    if monthly is None and seats is None and premium is None and plan is None:
        raise HTTPException(
            status_code=400,
            detail="нужно хотя бы одно: monthly_tokens_usd, seats, "
                   "can_use_premium_tier или plan (метка)")
    # set_tenant_limits — UPSERT: незаданные поля затрутся дефолтами
    # (plan → "free", can_use_premium_tier → False). Поэтому сначала
    # читаем текущие лимиты и меняем только то, что прислали.
    from backend.core.auth.tenant_limits import (get_tenant_limits,
                                                 set_tenant_limits)
    current = await get_tenant_limits(tenant_id) or {}
    res = await set_tenant_limits(
        tenant_id,
        plan=(plan if plan is not None else str(current.get("plan") or "free")),
        monthly_tokens_usd=(float(monthly) if monthly is not None
                            else current.get("monthly_tokens_usd")),
        datasets_max=current.get("datasets_max"),
        projects_max=current.get("projects_max"),
        storage_mb=current.get("storage_mb"),
        seats=(int(seats) if seats is not None else current.get("seats")),
        can_use_premium_tier=(bool(premium) if premium is not None
                              else bool(current.get("can_use_premium_tier"))))
    if isinstance(res, dict) and res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    logger.info("billing: %s set limits tenant=%s plan=%s monthly=%s seats=%s",
                admin_id, tenant_id, plan, monthly, seats)
    return {"success": True, "tenant_id": tenant_id, "applied": {
        "plan": plan, "monthly_tokens_usd": monthly, "seats": seats,
        "can_use_premium_tier": premium}}


billing_admin_router = Router(
    path="/admin/billing",
    route_handlers=[get_tenant_billing, set_tenant_billing],
    tags=["Billing Admin"],
)
