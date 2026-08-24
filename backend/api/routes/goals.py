# -*- coding: utf-8 -*-
"""Goals API — ведение целей (эпиков) и недельный обзор движения.

Endpoints:
- GET    /goals/                 — список целей (фильтры level/status)
- POST   /goals/                 — создать цель
- PATCH  /goals/{id}             — обновить (прогресс/статус/связи задач)
- DELETE /goals/{id}             — удалить
- GET    /goals/progress         — текущий прогресс по задачам графа
- GET    /goals/weekly-review    — недельный обзор: дельта + рекомендации
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from litestar import Request, Router, delete, get, patch, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter
from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _resolve_user(request: Request, user_id: Optional[str]) -> str:
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, _src = trusted_user_id(request.headers, user_id or "")
    except PermissionError:
        raise HTTPException(status_code=403, detail="token/user_id mismatch")
    except Exception:
        uid = user_id or ""
    if not uid:
        raise HTTPException(status_code=401, detail="user_id required")
    return uid


class GoalCreate(BaseModel):
    title: str
    level: str  # company | department | person
    user_id: Optional[str] = None
    owner: str = ""
    department: str = ""
    description: str = ""
    target_date: str = ""
    parent_id: str = ""
    metric: str = ""
    # OKR: ключевые результаты [{title, kind: metric|milestone|binary,
    # start, target, current, unit, metric_name?, done?, fraction?}],
    # квартал ("2026-Q3", по умолчанию текущий, если есть KR) и тип
    # обязательства (committed требует 100%, aspirational — 70%).
    key_results: Optional[List[Dict[str, Any]]] = None
    cycle: str = ""
    commitment: str = "committed"


class CheckinBody(BaseModel):
    user_id: Optional[str] = None
    author: str = ""
    note: str = ""
    confidence: Optional[float] = None
    # [{"kr_id"|"title": ..., "current"|"done"|"fraction": ...}]
    kr_updates: Optional[List[Dict[str, Any]]] = None


class CycleCloseBody(BaseModel):
    user_id: Optional[str] = None


class GoalPatch(BaseModel):
    user_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    department: Optional[str] = None
    metric: Optional[str] = None
    target_date: Optional[str] = None
    progress: Optional[int] = None
    status: Optional[str] = None
    parent_id: Optional[str] = None
    task_ids: Optional[List[str]] = None


@get("/")
async def list_goals(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    level: Optional[str] = Parameter(query="level", default=None, required=False),
    status: Optional[str] = Parameter(query="status", default="active", required=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.goals.goal_tracker import goal_store_for_user
    goals = goal_store_for_user(uid).list_goals(level=level, status=status)
    return {"goals": goals, "count": len(goals)}


@post("/")
async def create_goal(data: GoalCreate, request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    from backend.core.goals.goal_tracker import goal_store_for_user
    try:
        goal = goal_store_for_user(uid).add_goal(
            title=data.title, level=data.level, owner=data.owner,
            department=data.department, description=data.description,
            target_date=data.target_date, parent_id=data.parent_id,
            metric=data.metric, key_results=data.key_results,
            cycle=data.cycle, commitment=data.commitment)
        return {"status": "success", "goal": goal}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@patch("/{goal_id:str}")
async def update_goal(goal_id: str, data: GoalPatch, request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    from backend.core.goals.goal_tracker import goal_store_for_user
    patch_data = {k: v for k, v in data.model_dump().items()
                  if v is not None and k != "user_id"}
    goal = goal_store_for_user(uid).update_goal(goal_id, patch_data)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"status": "success", "goal": goal}


@delete("/{goal_id:str}", status_code=200)
async def remove_goal(
    goal_id: str,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.goals.goal_tracker import goal_store_for_user
    ok = goal_store_for_user(uid).delete_goal(goal_id)
    return {"status": "success" if ok else "error",
            "message": None if ok else "goal not found"}


@get("/progress")
async def goals_progress(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.goals.goal_tracker import compute_goals_progress
    goals = await compute_goals_progress(uid)
    return {"goals": goals, "count": len(goals)}


@get("/weekly-review")
async def weekly_review_endpoint(
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None, required=False),
    with_llm: bool = Parameter(query="with_llm", default=True, required=False),
) -> Dict[str, Any]:
    """Недельный обзор движения к целям: дельта против прошлой недели +
    (опц.) рекомендации, что поменять. Сохраняет снимок недели."""
    uid = _resolve_user(request, user_id)
    from backend.core.goals.goal_tracker import weekly_review
    try:
        return await weekly_review(uid, with_llm=with_llm)
    except Exception as e:
        logger.error(f"weekly_review failed: {e}", exc_info=True)
        return {"error": str(e)}


@post("/{goal_id:str}/checkin")
async def goal_checkin(goal_id: str, data: CheckinBody,
                       request: Request) -> Dict[str, Any]:
    """Чекин по цели: обновить значения ключевых результатов, записать
    заметку и уверенность. Оценка цели пересчитывается из KR."""
    uid = _resolve_user(request, data.user_id)
    from backend.core.goals.goal_tracker import goal_store_for_user
    res = goal_store_for_user(uid).add_checkin(
        goal_id, author=data.author or uid, note=data.note,
        confidence=data.confidence, kr_updates=data.kr_updates)
    if not res:
        raise HTTPException(status_code=404, detail="goal not found")
    return {"status": "success", **res}


@get("/cycles/current")
async def current_cycle(request: Request,
                        user_id: Optional[str] = Parameter(
                            query="user_id", default=None, required=False),
                        ) -> Dict[str, Any]:
    """Текущий квартал, его границы и цели в нём."""
    uid = _resolve_user(request, user_id)
    from backend.core.goals.goal_tracker import goal_store_for_user
    from backend.core.goals.okr import quarter_bounds, quarter_of
    cycle = quarter_of()
    goals = [g for g in goal_store_for_user(uid).list_goals()
             if g.get("cycle") == cycle]
    return {"cycle": cycle, "bounds": quarter_bounds(cycle),
            "goals": goals, "count": len(goals)}


@post("/cycles/{cycle:str}/close")
async def close_cycle_endpoint(cycle: str, data: CycleCloseBody,
                               request: Request) -> Dict[str, Any]:
    """Закрыть квартал: финальные оценки и грейды по правилам метода
    (обязательная цель требует 100%, амбициозная — 70%). Цели закрываются;
    сводка возвращается и остаётся в записях целей."""
    uid = _resolve_user(request, data.user_id)
    from backend.core.goals.okr import quarter_bounds
    if quarter_bounds(cycle) is None:
        raise HTTPException(status_code=400,
                            detail="cycle должен быть вида 2026-Q3")
    from backend.core.goals.goal_tracker import goal_store_for_user
    return {"status": "success",
            **goal_store_for_user(uid).close_cycle(cycle)}


router = Router(
    path="/goals",
    route_handlers=[list_goals, create_goal, update_goal, remove_goal,
                    goals_progress, weekly_review_endpoint,
                    goal_checkin, current_cycle, close_cycle_endpoint],
    tags=["Goals"],
)
