# -*- coding: utf-8 -*-
"""Численные лимиты тарифа поверх флагов (ACCESS_CONTROL_DESIGN, S6).

Флаги — функциональные гейты («что вообще доступно»). Лимиты — числа:
сколько токенов в месяц, датасетов, проектов, мест в команде. Источники:
- monthly_tokens_usd: usage_tracker уже считает (берём текущий месяц);
- datasets_max/projects_max: список через registry/db (best-effort);
- seats: members организации, опционально.

Ничего не блокирует автоматически: маршрут вызывает limits_status и сам
решает что показать/отказать — аддитивно, не ломает прежнее поведение.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def get_tenant_limits(tenant_id: str) -> Dict[str, Any]:
    """Лимиты тенанта из tenant_limits. Пусто → план free без лимитов."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            res = await session.execute(
                text("SELECT plan, monthly_tokens_usd, datasets_max, "
                     "projects_max, storage_mb, seats, "
                     "can_use_premium_tier FROM tenant_limits "
                     "WHERE tenant_id = :t"), {"t": str(tenant_id)})
            row = res.fetchone()
            if row:
                return {k: row._mapping[k]
                        for k in row._mapping.keys()}
    except Exception as e:
        logger.debug(f"tenant_limits unavailable: {e}")
    return {"plan": "free", "can_use_premium_tier": False}


async def set_tenant_limits(tenant_id: str, *, plan: str = "free",
                            monthly_tokens_usd: Optional[float] = None,
                            datasets_max: Optional[int] = None,
                            projects_max: Optional[int] = None,
                            storage_mb: Optional[int] = None,
                            seats: Optional[int] = None,
                            can_use_premium_tier: bool = False) -> dict:
    """UPSERT по tenant_id. Числовые None → без лимита (NULL в БД)."""
    try:
        from sqlalchemy import text

        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session() as session:
            await session.execute(
                text("""INSERT INTO tenant_limits
                    (tenant_id, plan, monthly_tokens_usd, datasets_max,
                     projects_max, storage_mb, seats, can_use_premium_tier)
                    VALUES (:t, :p, :mtu, :dm, :pm, :sm, :s, :prem)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        plan = :p, monthly_tokens_usd = :mtu,
                        datasets_max = :dm, projects_max = :pm,
                        storage_mb = :sm, seats = :s,
                        can_use_premium_tier = :prem,
                        updated_at = NOW()"""),
                {"t": str(tenant_id), "p": plan, "mtu": monthly_tokens_usd,
                 "dm": datasets_max, "pm": projects_max, "sm": storage_mb,
                 "s": seats, "prem": bool(can_use_premium_tier)})
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _used_dollars_this_month() -> float:
    """Затраты текущего месяца из usage_tracker (best-effort)."""
    try:
        from datetime import datetime as dt
        from datetime import timezone

        from backend.core.llm.usage_tracker import get_usage_tracker
        now = dt.now(timezone.utc)
        return float(get_usage_tracker()
                     .get_period_stats(start_date=now.strftime("%Y-%m-01"))
                     .get("total_cost", 0.0))
    except Exception:
        return 0.0


async def limits_status(tenant_id: str, *,
                        datasets_count: Optional[int] = None,
                        projects_count: Optional[int] = None,
                        seats_count: Optional[int] = None) -> dict:
    """Статус лимитов: usage и сколько осталось. Маршрут сам решает,
    блокировать или предупредить."""
    lim = await get_tenant_limits(tenant_id)
    used_usd = _used_dollars_this_month()

    def _check(name: str, used: Any, cap: Any) -> Optional[dict]:
        if cap in (None, 0):
            return None
        used_n = float(used or 0)
        cap_n = float(cap)
        return {"limit": name, "used": used_n, "cap": cap_n,
                "remaining": max(0.0, cap_n - used_n),
                "over": used_n > cap_n}

    items = [c for c in (
        _check("monthly_tokens_usd", used_usd,
               lim.get("monthly_tokens_usd")),
        _check("datasets", datasets_count, lim.get("datasets_max")),
        _check("projects", projects_count, lim.get("projects_max")),
        _check("seats", seats_count, lim.get("seats")),
    ) if c]
    return {"plan": lim.get("plan", "free"),
            "can_use_premium_tier": bool(lim.get("can_use_premium_tier",
                                                 False)),
            "checks": items,
            "any_over": any(c["over"] for c in items),
            "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat()}
