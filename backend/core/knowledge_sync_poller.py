# -*- coding: utf-8 -*-
"""
Safety-поллер синхронизации: досинкивает встречи, пропущенные webhook'ом.

Коннект с MeetFlow — push-only (webhook). Если webhook потерялся/не настроен,
новая встреча не подхватится автоматически (только кнопкой). Этот поллер —
страховка: раз в N минут проходит по ВСЕМ активным подпискам с
auto_process_new_meetings=true и запускает тот же `process_meetings_for_
subscription`. Дедуп (`processed_meetings`) гарантирует, что работа делается
ТОЛЬКО для реально новых встреч → повторные проходы почти бесплатны.

ВЫКЛ по умолчанию (env `SYNC_POLLER=on`) — чтобы не запускать тяжёлый ингест
без явного согласия. Никогда не raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def poller_enabled() -> bool:
    return os.getenv("SYNC_POLLER", "").strip().lower() in ("1", "on", "true", "yes")


async def poll_all_subscriptions(*, limit: int = 1000) -> dict[str, Any]:
    """Пройти по активным авто-подпискам и досинкать новые встречи.
    Возвращает {enabled, polled, processed, users}. Best-effort."""
    if not poller_enabled():
        return {"enabled": False, "polled": 0, "processed": 0, "users": 0}
    try:
        from backend.core.knowledge_sync import process_meetings_for_subscription
        from backend.db.supabase_client import get_supabase_client
        sb = get_supabase_client()
        subs = await sb._request(
            "GET", "/rest/v1/knowledge_sync_subscriptions",
            params={"status": "eq.active",
                    "auto_process_new_meetings": "eq.true",
                    "select": "*", "limit": str(limit)}) or []
    except Exception as e:
        logger.warning("sync poller: list subscriptions failed: %s", e)
        return {"enabled": True, "polled": 0, "processed": 0, "users": 0}

    # Cost-guard: максимум НОВЫХ встреч на одну подписку за прогон. Если дедуп
    # деградировал (processed_meetings не пишется), без лимита поллер переработал
    # бы весь бэклог каждый цикл и жёг деньги. Остаток досинкается позже.
    max_per_sub = int(os.getenv("SYNC_POLL_MAX_PER_RUN", "25") or "25")

    polled = 0
    processed = 0
    skipped = 0
    touched: set[str] = set()
    capped_subs = 0
    for sub in subs:
        uid = sub.get("user_id")
        if not uid:
            continue
        try:
            stats = await process_meetings_for_subscription(
                subscription_id=sub["id"], user_id=uid,
                project_id=sub.get("project_id"),
                folder_id=sub.get("folder_id"),
                max_process=max_per_sub)
            polled += 1
            n = int((stats or {}).get("processed", 0) or 0)
            processed += n
            skipped += int((stats or {}).get("skipped", 0) or 0)
            if (stats or {}).get("capped"):
                capped_subs += 1
            if n:
                touched.add(uid)
        except Exception as e:
            logger.warning("sync poller: sub %s failed: %s", sub.get("id"), e)

    # Граф обновился — перечитываем runtime тем, у кого появились встречи.
    for uid in touched:
        try:
            from backend.api.routes.chat import reload_graph_for_user
            await reload_graph_for_user(uid)
        except Exception:
            logger.debug("sync poller: graph reload skipped for %s", uid)

    if processed:
        logger.info(
            "🔄 sync poller: обработано %d новых, пропущено %d (уже в памяти), "
            "по %d подпискам (%d польз.)%s",
            processed, skipped, polled, len(touched),
            f"; лимит сработал на {capped_subs} подписках" if capped_subs else "")
        if capped_subs:
            logger.warning(
                "sync poller: %d подписок упёрлись в лимит max_process — если "
                "это повторяется каждый цикл, проверь запись в processed_meetings "
                "(dedup): встречи не помечаются completed и жгут токены заново.",
                capped_subs)
    return {"enabled": True, "polled": polled, "processed": processed,
            "skipped": skipped, "capped_subs": capped_subs,
            "users": len(touched)}


__all__ = ["poll_all_subscriptions", "poller_enabled"]
