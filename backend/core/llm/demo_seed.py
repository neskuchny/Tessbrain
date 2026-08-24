# -*- coding: utf-8 -*-
"""Demo seed для wow-screen — заполняет llm_usage реалистичными данными
для свежего тенанта (issue #110 wow-step + frontend critical #1).

Без этого HomeTab у нового пользователя показывает пустой empty state,
и wow-эффект не выстреливает. Endpoint POST /api/v1/usage/seed-demo
вызывает эту функцию.

Что создаёт:
  - 3 встречи разной стоимости (одна 'дорогая' для hero card)
  - automation job-spike для daily_spending_spike insight'а
  - расход в разных моделях (gpt-4o-mini, gpt-4o)

После seed:
  - /insights/weekly возвращает >=1 expensive_meeting + 1 surface_spike
  - /usage/by-meeting/mtg-acme-deal-2026 имеет recent[] для sparkline

Идемпотентно: если у тенанта уже >5 записей в llm_usage с surface='ingest' —
пропуск (return seeded=0).
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def seed_demo_data(
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Засеять демо-данные. Возвращает {success, seeded, message}."""
    try:
        from backend.core.llm.usage_tracker import UsageRecord, get_usage_tracker
        tracker = get_usage_tracker()

        # Идемпотентность: если уже есть данные — не дублируем
        existing = tracker.store.aggregate_by_entity(
            "surface", "ingest", tenant_id)
        if existing and existing.get("agg", {}).get("requests", 0) > 5:
            return {
                "success": True, "seeded": 0,
                "message": "tenant already has data (>5 records), skipped",
            }

        seeded = 0
        # (meeting_id, total_cost USD, surface, n_calls)
        meetings = [
            ("mtg-acme-deal-2026", 8.50, "ingest", 4),  # hero card
            ("mtg-q4-review", 2.30, "ingest", 3),
            ("mtg-team-sync", 0.45, "ingest", 2),
        ]
        now = datetime.now(timezone.utc)
        for mtg_id, total_cost, surface, n_calls in meetings:
            per_call = total_cost / n_calls
            for _ in range(n_calls):
                # Распределяем по 7 дням, чтобы sparkline было что показать
                days_ago = random.uniform(0.5, 6.5)
                ts = now - timedelta(days=days_ago)
                rec = UsageRecord(
                    timestamp=ts.isoformat(),
                    provider="openai", model="gpt-4o-mini",
                    input_tokens=int(per_call * 4 * 1_000_000 / 0.15) // 4,
                    output_tokens=int(per_call * 4 * 1_000_000 / 0.60) // 4,
                    total_tokens=int(per_call * 1_000_000),
                    input_cost=round(per_call * 0.4, 6),
                    output_cost=round(per_call * 0.6, 6),
                    total_cost=round(per_call, 6),
                    meeting_id=mtg_id, surface=surface,
                    agent_mode="automation",
                    request_type="meeting_summary_telegram",
                    tenant_id=tenant_id, user_id=user_id,
                )
                tracker.store.save(rec)
                seeded += 1

        # Один большой 'automation' job для daily-spike insight'а:
        # вчера несколько вызовов разом → median будет ~$1/день, вчера ~$0.68
        for i in range(3):
            ts = now - timedelta(days=1, hours=i)
            rec = UsageRecord(
                timestamp=ts.isoformat(),
                provider="openai", model="gpt-4o",
                input_tokens=50_000, output_tokens=10_000,
                total_tokens=60_000,
                input_cost=0.125, output_cost=0.100,
                total_cost=0.225,
                job_id="bulk-ingest-yesterday", surface="automation",
                agent_mode="automation",
                tenant_id=tenant_id, user_id=user_id,
            )
            tracker.store.save(rec)
            seeded += 1

        return {
            "success": True,
            "seeded": seeded,
            "message": f"created {seeded} demo records; "
                       f"refresh /insights/weekly to see findings",
        }
    except Exception as e:
        logger.error(f"seed_demo_data failed: {e}")
        return {"success": False, "error": str(e)}
