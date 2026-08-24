# -*- coding: utf-8 -*-
"""Синхронизация организации — отчёт по 4 уровням + индекс S(t) (CogniLayer Ф2).

- GET /sync/report — полный отчёт: внутри отделов / отдел↔отдел / каскад
  company→dept / руководители, сводный индекс S(t) с компонентами.
- GET /sync/index  — история S(t) для тренда (дашборд генерального).

Данные — только из тенанта запрашивающего (его goal_tracker, его граф) +
метаданные членства орга. За тем же флагом, что и Столп B
(cross_dept_desync_enabled) — это его обобщение на все уровни.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token

logger = logging.getLogger(__name__)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    try:
        user_id = get_user_id_from_token(authorization)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id


def _guard_enabled() -> None:
    from backend.config import settings as _settings
    if not getattr(_settings, "cross_dept_desync_enabled", False):
        raise HTTPException(status_code=403, detail="org sync disabled "
                            "(cross_dept_desync_enabled=False)")


def _require_manager(user_id: str) -> None:
    """Отчёты сверки — только руководителям (founder/admin/manager).

    Отчёт раскрывает имена коллег/руководителей и тексты их целей по всему
    оргу; в UI дашборд руководительский — API обязан совпадать с этим, иначе
    любой сотрудник вычитывал бы уровни и конфликты всей организации."""
    try:
        from backend.core.ingest import membership
        role = membership.get_user_org_base_role(user_id)
    except Exception:
        role = None
    if role not in ("founder", "admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="доступ к сверке организации — только для руководителей")


@get("/report")
async def sync_report(
    judge: bool = False,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """?judge=true — LLM-судья подтверждает/отклоняет soft-пары (дороже/дольше);
    отклонённые судьёй не входят в индекс."""
    _guard_enabled()
    uid = _extract_user(authorization)
    _require_manager(uid)
    from backend.core.observability import audit_log
    await audit_log.emit(action="sync.report.read", user_id=uid)
    from backend.core.think.org_sync import org_sync_report
    return await org_sync_report(uid, judge=judge)


@get("/index")
async def sync_index_history(
    limit: int = 60,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    _guard_enabled()
    uid = _extract_user(authorization)
    _require_manager(uid)
    from backend.core.think.org_sync import index_history
    return {"history": index_history(uid, limit=max(1, min(500, limit)))}


@get("/predictive")
async def sync_predictive(
    limit: int = 30,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Ранние сигналы предиктивной сверки: решения встреч, тянущие против
    активных целей (копятся при ингесте, свежие первыми)."""
    _guard_enabled()
    uid = _extract_user(authorization)
    _require_manager(uid)
    from backend.core.think.predictive_sync import recent_warnings
    return {"warnings": recent_warnings(uid, limit=max(1, min(100, limit)))}


@post("/cascade/propose")
async def cascade_propose(
    data: dict,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Черновики целей отделов для цели компании (Ф3). Ничего не записывает —
    принятие отдельным явным действием (/cascade/accept)."""
    _guard_enabled()
    uid = _extract_user(authorization)
    _require_manager(uid)
    from backend.core.think.cascade_assist import propose_department_goals
    return await propose_department_goals(
        uid,
        goal_id=str((data or {}).get("goal_id") or "") or None,
        goal_text=str((data or {}).get("goal_text") or "") or None,
    )


@post("/cascade/accept")
async def cascade_accept(
    data: dict,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Принять черновик каскада — создать department-цель (human gate)."""
    _guard_enabled()
    uid = _extract_user(authorization)
    _require_manager(uid)
    from backend.core.think.cascade_assist import accept_proposal
    return accept_proposal(
        uid,
        department=str((data or {}).get("department") or ""),
        title=str((data or {}).get("title") or ""),
        company_goal_id=str((data or {}).get("company_goal_id") or ""),
        rationale=str((data or {}).get("rationale") or ""),
    )


router = Router(
    path="/sync",
    route_handlers=[sync_report, sync_index_history, sync_predictive,
                    cascade_propose, cascade_accept],
    tags=["Org Sync"],
)
