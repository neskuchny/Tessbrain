# -*- coding: utf-8 -*-
"""Weekly cost insights — финансовая аналитика поверх llm_usage.

Комплементарно к night-insight'ам в `core/sleep/insight_collector`
(те ищут аномалии в встречах/сущностях, эти — в расходах). Издаёт
insight-объекты в **том же формате**, что использует `/api/v1/meetflow/
insights` → HomeTab их обрабатывает без правок.

Алгоритмы (все простые, объяснимые эвристики — не ML):

  1) `expensive_meetings_this_week` — top-N встреч за 7 дней по cost.
     Подсвечивает «вот эти встречи обработка съела дороже всего —
     посмотри почему» (длинный транскрипт? много кастомных промптов?).

  2) `surface_spike` — если за 7 дней одна surface (`ingest`/`digest`/
     `automation`) дала cost >= 3× cost'а за предыдущие 21 день —
     insight «массовый ingest сожрал X$». Защита от «вдруг откуда».

  3) `daily_spending_spike` — день со spend > median(28 days) * 1.5 →
     insight с конкретной датой и top-3 моделями этого дня.

Идея «editorial briefing»: вместо «вот сырой список расходов» — система
сама находит **аномалию** и описывает её человеческой речью.

Привязка к ранжированию HomeTab:
  - priority: critical (>= $10), high (>= $1), medium (<$1)
  - confidence: 0.6-0.95 в зависимости от «статистической силы» сигнала

Tenant-isolation: все запросы фильтруют по tenant_id (если задан).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _priority_from_cost(cost_usd: float) -> str:
    if cost_usd >= 10.0:
        return "critical"
    if cost_usd >= 1.0:
        return "high"
    if cost_usd >= 0.1:
        return "medium"
    return "low"


def _get_db_path() -> str:
    from backend.core.llm.usage_tracker import get_usage_tracker
    return get_usage_tracker().store.db_path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _week_ago_iso() -> str:
    return (_utc_now() - timedelta(days=7)).isoformat()


def _month_ago_iso() -> str:
    return (_utc_now() - timedelta(days=28)).isoformat()


def _expensive_meetings(tenant_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """Top-N встреч за 7 дней по total_cost."""
    db_path = _get_db_path()
    where = ["meeting_id IS NOT NULL", "timestamp >= ?"]
    params: List[Any] = [_week_ago_iso()]
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)
    where_sql = " AND ".join(where)
    params.append(limit)

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(f"""
                SELECT meeting_id,
                       SUM(total_cost) AS cost,
                       SUM(total_tokens) AS tokens,
                       COUNT(*) AS llm_calls,
                       MAX(timestamp) AS last_at
                FROM llm_usage
                WHERE {where_sql}
                GROUP BY meeting_id
                ORDER BY cost DESC
                LIMIT ?
            """, params)
            rows = [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as e:
        logger.warning("expensive_meetings: SQLite error %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        cost = float(r["cost"] or 0)
        if cost < 0.01:  # не показываем копеечные «находки»
            continue
        meeting_id = r["meeting_id"]
        out.append({
            "id": f"weekly:expensive_meeting:{meeting_id}",
            "type": "expensive_meeting",
            "title": f"Обработка встречи {meeting_id[:8]}… стоила "
                     f"${cost:.2f}",
            "description": (
                f"За последние 7 дней эта встреча сожрала {r['llm_calls']} "
                f"LLM-вызовов и {int(r['tokens'] or 0):,} токенов. "
                f"Если регулярно дорого — проверьте длину транскрипта или "
                f"количество саммари/задач, которые система пытается извлечь."
            ),
            "priority": _priority_from_cost(cost),
            "confidence": 0.9,  # факт из llm_usage, не догадка
            "entities": [{"id": meeting_id, "name": f"meeting:{meeting_id[:8]}"}],
            "actions": [
                "Открыть /usage/by-meeting/" + str(meeting_id),
                "Проверить транскрипт встречи",
            ],
            "discovered_at": _utc_now().isoformat(),
            "source": "weekly_cost_attribution",
            "is_read": False,
        })
    return out


def _surface_spike(tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """surface с cost'ом за 7 дней >= 3× cost'а за 28-7=21 день до этого."""
    db_path = _get_db_path()
    week_ago = _week_ago_iso()
    month_ago = _month_ago_iso()

    where_recent = ["surface IS NOT NULL", "timestamp >= ?"]
    where_prior = ["surface IS NOT NULL",
                   "timestamp >= ?", "timestamp < ?"]
    p_recent: List[Any] = [week_ago]
    p_prior: List[Any] = [month_ago, week_ago]
    if tenant_id:
        where_recent.append("tenant_id = ?"); p_recent.append(tenant_id)
        where_prior.append("tenant_id = ?"); p_prior.append(tenant_id)

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(f"""
                SELECT surface, SUM(total_cost) AS cost
                FROM llm_usage WHERE {' AND '.join(where_recent)}
                GROUP BY surface
            """, p_recent)
            recent = {r["surface"]: float(r["cost"] or 0) for r in cur.fetchall()}
            cur.execute(f"""
                SELECT surface, SUM(total_cost) AS cost
                FROM llm_usage WHERE {' AND '.join(where_prior)}
                GROUP BY surface
            """, p_prior)
            prior = {r["surface"]: float(r["cost"] or 0) for r in cur.fetchall()}
    except sqlite3.Error as e:
        logger.warning("surface_spike: SQLite error %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for surface, recent_cost in recent.items():
        prior_cost = prior.get(surface, 0.0)
        # Если за прошлый период расходов почти не было — «новая активность»
        # сама по себе сигнал, если в абсолюте >= $0.50
        is_new = prior_cost < 0.05 and recent_cost >= 0.5
        is_spike = prior_cost >= 0.05 and recent_cost >= 3 * prior_cost \
                   and recent_cost >= 0.5
        if not (is_new or is_spike):
            continue
        multiplier = (recent_cost / prior_cost) if prior_cost > 0 else float("inf")
        out.append({
            "id": f"weekly:surface_spike:{surface}",
            "type": "surface_spike",
            "title": (f"Расходы на «{surface}» выросли в "
                      f"{multiplier:.1f}× за неделю"
                      if not is_new
                      else f"Новая активность: «{surface}» сожрал ${recent_cost:.2f}"),
            "description": (
                f"За последние 7 дней surface={surface} потратил "
                f"${recent_cost:.2f}, "
                + (f"vs ${prior_cost:.2f} за предыдущие 21 день. "
                   if not is_new else "до этого активности почти не было. ")
                + "Проверьте, не запустился ли массовый ingest / нет ли утечки."
            ),
            "priority": _priority_from_cost(recent_cost),
            "confidence": 0.85 if is_spike else 0.7,
            "entities": [{"id": surface, "name": f"surface:{surface}"}],
            "actions": [
                f"Открыть /usage/attribution?group_by=meeting_id&surface={surface}",
                "Проверить логи taskiq-jobs за неделю",
            ],
            "discovered_at": _utc_now().isoformat(),
            "source": "weekly_cost_attribution",
            "is_read": False,
        })
    return out


def _daily_spending_spike(tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """День с spend > median(28 days) * 1.5 — флаг.

    Median здесь надёжнее avg: один большой ingest-день не должен поднимать
    «норму» для остальных.
    """
    db_path = _get_db_path()
    where = ["timestamp >= ?"]
    params: List[Any] = [_month_ago_iso()]
    if tenant_id:
        where.append("tenant_id = ?")
        params.append(tenant_id)

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(f"""
                SELECT DATE(timestamp) AS day, SUM(total_cost) AS cost
                FROM llm_usage WHERE {' AND '.join(where)}
                GROUP BY DATE(timestamp)
                ORDER BY day DESC
            """, params)
            rows = [(r["day"], float(r["cost"] or 0)) for r in cur.fetchall()]
    except sqlite3.Error as e:
        logger.warning("daily_spending_spike: SQLite error %s", e)
        return []

    if len(rows) < 7:  # слишком мало данных для статистики
        return []

    costs = sorted(c for _, c in rows)
    median = costs[len(costs) // 2]
    threshold = max(median * 1.5, 0.5)  # минимум $0.50 чтобы не шуметь

    out: List[Dict[str, Any]] = []
    for day, cost in rows[:7]:  # за последнюю неделю
        if cost <= threshold:
            continue
        ratio = (cost / median) if median > 0 else float("inf")
        out.append({
            "id": f"weekly:daily_spike:{day}",
            "type": "daily_spending_spike",
            "title": f"{day}: расходы ${cost:.2f} — выше нормы в {ratio:.1f}×",
            "description": (
                f"В этот день затраты на LLM выросли в {ratio:.1f}× по сравнению "
                f"с медианой за 28 дней (${median:.2f}). "
                "Откройте /usage/attribution?period=today чтобы увидеть, "
                "какая встреча/job/surface спровоцировала."
            ),
            "priority": _priority_from_cost(cost),
            "confidence": 0.8,
            "entities": [{"id": day, "name": f"day:{day}"}],
            "actions": [
                f"Посмотреть /usage/daily/{day}",
                "Сравнить с предыдущим днём",
            ],
            "discovered_at": _utc_now().isoformat(),
            "source": "weekly_cost_attribution",
            "is_read": False,
        })
    return out


def compute_weekly_insights(
    tenant_id: Optional[str] = None,
    *,
    max_meetings: int = 3,
    publish_events: bool = True,
) -> List[Dict[str, Any]]:
    """Собрать все weekly insights и (опц.) опубликовать через signals.

    Ранжирование (для hero-card в HomeTab) — простая сортировка
    приоритет → cost. Возвращается в формате, идентичном
    `/api/v1/meetflow/insights` → HomeTab отображает их без правок.
    """
    insights: List[Dict[str, Any]] = []
    insights.extend(_expensive_meetings(tenant_id, max_meetings))
    insights.extend(_surface_spike(tenant_id))
    insights.extend(_daily_spending_spike(tenant_id))

    # Ранжирование: critical > high > medium > low, потом по cost (если он
    # выводим из entity name или type — иначе по confidence).
    PRIO = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    insights.sort(
        key=lambda i: (PRIO.get(i.get("priority", "low"), 0),
                       i.get("confidence", 0)),
        reverse=True,
    )

    # Pulse на фронт: каждое новое insight-событие → подсветка в HomeTab
    if publish_events:
        try:
            from backend.api.websocket.signals_ws import publish_signal
            for ins in insights:
                publish_signal(
                    "insight_added",
                    {
                        "id": ins["id"],
                        "type": ins["type"],
                        "title": ins["title"],
                        "priority": ins["priority"],
                    },
                    tenant_id=tenant_id,
                )
        except Exception:
            logger.debug("publish weekly insights signals failed", exc_info=True)

    return insights
