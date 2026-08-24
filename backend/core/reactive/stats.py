"""Reactive-engine observability aggregation (Phase O — dashboard).

Единая точка, собирающая «здоровье» движка из его таблиц для дашборда:
- reactive_signals      — сигналы по статусам (queued/delivered/expired/...);
- reactive_feedback     — реакции по kind + общий engagement-score;
- cognitive_load_samples— распределение load-уровней + средний score;
- reactive_calibration  — сколько профилей реально применяется (прошли cold-start);
- rollout               — текущее состояние канареечного раската.

Скоуп: если задан user_id — только он; иначе tenant_id; иначе глобально (admin).
Всё best-effort/never-raise: любая упавшая секция отдаёт {} или нули, дашборд
не падает целиком из-за одной кривой выборки.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _scope_clause(user_id: Optional[str], tenant_id: Optional[str], col_alias: str = "") -> tuple[str, dict]:
    """Вернуть (sql_fragment, params) для скоупа. col_alias — префикс таблицы."""
    p = col_alias + "." if col_alias else ""
    if user_id:
        return f" AND {p}user_id = CAST(:scope_uid AS UUID)", {"scope_uid": user_id}
    if tenant_id:
        return f" AND {p}tenant_id = CAST(:scope_tid AS UUID)", {"scope_tid": tenant_id}
    return "", {}


async def _signals_by_status(session, scope_sql, scope_params, window_hours) -> dict[str, int]:
    from sqlalchemy import text
    r = await session.execute(
        text(f"""
            SELECT status, COUNT(*) AS n
            FROM public.reactive_signals
            WHERE created_at > now() - make_interval(hours => :wh){scope_sql}
            GROUP BY status
        """),
        {"wh": window_hours, **scope_params},
    )
    return {str(row[0]): int(row[1]) for row in r.fetchall()}


async def _feedback_summary(session, scope_sql, scope_params, window_hours) -> dict[str, Any]:
    from sqlalchemy import text
    from backend.core.reactive.feedback import FEEDBACK_WEIGHTS
    r = await session.execute(
        text(f"""
            SELECT kind, COUNT(*) AS n
            FROM public.reactive_feedback
            WHERE created_at > now() - make_interval(hours => :wh){scope_sql}
            GROUP BY kind
        """),
        {"wh": window_hours, **scope_params},
    )
    by_kind = {str(row[0]): int(row[1]) for row in r.fetchall()}
    total = sum(by_kind.values())
    # engagement = взвешенное среднее реакций (тот же вес что в калибровке)
    weighted = sum(FEEDBACK_WEIGHTS.get(k, 0.0) * n for k, n in by_kind.items())
    engagement = round(weighted / total, 3) if total else 0.0
    return {"by_kind": by_kind, "total": total, "engagement_score": engagement}


async def _load_distribution(session, scope_sql, scope_params, window_hours) -> dict[str, Any]:
    from sqlalchemy import text
    r = await session.execute(
        text(f"""
            SELECT load_level, COUNT(*) AS n, AVG(load_score) AS avg_score
            FROM public.cognitive_load_samples
            WHERE sampled_at > now() - make_interval(hours => :wh){scope_sql}
            GROUP BY load_level
        """),
        {"wh": window_hours, **scope_params},
    )
    by_level: dict[str, int] = {}
    weighted_avg_num = 0.0
    total = 0
    for row in r.fetchall():
        lvl, n, avg = str(row[0]), int(row[1]), float(row[2] or 0.0)
        by_level[lvl] = n
        weighted_avg_num += avg * n
        total += n
    avg_score = round(weighted_avg_num / total, 3) if total else 0.0
    return {"by_level": by_level, "samples": total, "avg_load_score": avg_score}


async def _calibration_summary(session, scope_sql, scope_params) -> dict[str, Any]:
    from sqlalchemy import text
    from backend.core.reactive.calibration import MIN_SAMPLES
    # reactive_calibration без временного окна — скоуп только по user/tenant
    r = await session.execute(
        text(f"""
            SELECT
              COUNT(*) AS profiles,
              COUNT(*) FILTER (
                WHERE sample_count >= :min_s AND threshold_delta <> 0
              ) AS applied,
              AVG(engagement_score) AS avg_eng,
              AVG(threshold_delta) FILTER (WHERE sample_count >= :min_s) AS avg_delta
            FROM public.reactive_calibration
            WHERE 1=1{scope_sql}
        """),
        {"min_s": MIN_SAMPLES, **scope_params},
    )
    row = r.first()
    if not row:
        return {"profiles": 0, "applied": 0, "avg_engagement": 0.0, "avg_applied_delta": 0.0}
    return {
        "profiles": int(row[0] or 0),
        "applied": int(row[1] or 0),
        "avg_engagement": round(float(row[2] or 0.0), 3),
        "avg_applied_delta": round(float(row[3] or 0.0), 2),
    }


async def reactive_dashboard(
    *,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Собрать дашборд-снимок здоровья reactive-движка. Never-raise.

    Returns структуру с секциями signals / feedback / load / calibration /
    rollout + метаданными scope/window. Любая упавшая секция → безопасный дефолт.
    """
    window_hours = max(1, min(int(window_hours or 24), 24 * 30))  # 1ч .. 30д
    scope_sql, scope_params = _scope_clause(user_id, tenant_id)

    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "user" if user_id else "tenant" if tenant_id else "global",
        "window_hours": window_hours,
        "signals": {},
        "feedback": {"by_kind": {}, "total": 0, "engagement_score": 0.0},
        "load": {"by_level": {}, "samples": 0, "avg_load_score": 0.0},
        "calibration": {"profiles": 0, "applied": 0, "avg_engagement": 0.0, "avg_applied_delta": 0.0},
        "rollout": {},
    }

    try:
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            for key, coro in (
                ("signals", _signals_by_status(session, scope_sql, scope_params, window_hours)),
                ("feedback", _feedback_summary(session, scope_sql, scope_params, window_hours)),
                ("load", _load_distribution(session, scope_sql, scope_params, window_hours)),
                ("calibration", _calibration_summary(session, scope_sql, scope_params)),
            ):
                try:
                    out[key] = await coro
                except Exception as exc:
                    logger.debug("dashboard section %s failed: %s", key, exc)
    except Exception as exc:
        logger.warning("reactive_dashboard db unavailable: %s", exc)

    try:
        from backend.core.reactive.rollout import rollout_status
        out["rollout"] = rollout_status(user_id)
    except Exception as exc:
        logger.debug("dashboard rollout failed: %s", exc)

    return out


__all__ = ["reactive_dashboard"]
