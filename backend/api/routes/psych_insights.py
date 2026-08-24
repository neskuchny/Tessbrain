# -*- coding: utf-8 -*-
"""Чтение психоаналитики встреч (PsychInsight) — экран для руководителя.

Слой сохраняется с грифом и намеренно не индексируется в общий поиск
(backend/core/capture/psych_persistence.py). Этот роут — единственный
предусмотренный способ его читать, и весь контроль доступа живёт здесь.

Круг доступа — тот же, что у 360 (см. _assert_may_read_360 в
aggregation.py), и по той же причине:
  - САМ человек видит свои мотивы и прогнозы. Принцип «откуда вы это про
    меня взяли» работает только если человек может увидеть ВСЁ, что
    система о нём думает, — включая неприятное.
  - РУКОВОДИТЕЛЬ по цепочке подчинения — этот слой для него и собирался.
  - АДМИНИСТРАТОР организации.
  - Остальным — 403. Fail-closed: не смогли подтвердить право — не отдаём;
    цена ошибки — раскрытие «признаков манипуляции» о коллеге.

Групповая динамика встречи — отдельный случай: она касается многих людей
сразу, поэтому «сам»-доступа нет — только руководители (founder/admin/
manager), как у отчётов сверки организации.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from litestar import Request, Router, get
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)

_KINDS_PERSON = ("motives", "behavior_forecast", "recommendation")


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


def _assert_may_read_person_insights(asker_uid: str, person_id: str) -> None:
    """Сам владелец / его руководитель по цепочке / админ организации."""
    try:
        from backend.core.ingest.membership import get_org_for_user
        from backend.core.twin.policy import (
            _is_org_admin,
            _owner_of_twin,
            is_manager_of,
        )
        org_id = get_org_for_user(asker_uid) or ""
        owner_uid = _owner_of_twin(person_id, org_id) if org_id else None
        if owner_uid and owner_uid == asker_uid:
            return
        if org_id and owner_uid and is_manager_of(asker_uid, owner_uid, org_id):
            return
        if org_id and _is_org_admin(asker_uid, org_id):
            return
    except Exception as exc:
        logger.warning("psych insights access check failed for %s→%s: %s",
                       asker_uid, person_id, exc)
        raise HTTPException(
            status_code=403,
            detail="Не удалось подтвердить право на чтение психоаналитики",
        ) from exc
    # Человек без сшитого аккаунта (внешний/кандидат): субъекта «сам» нет,
    # руководителя нет — остаётся только админ, и он уже отработал выше.
    # Открывать мотивы внешних людей всем сотрудникам не будем: слепок
    # внешнего открыт, но этот слой острее слепка.
    raise HTTPException(
        status_code=403,
        detail="Психоаналитика доступна самому человеку, его руководителю "
               "и администратору организации",
    )


def _require_manager(user_id: str) -> None:
    """Групповая динамика — только руководителям (как отчёты сверки)."""
    try:
        from backend.core.ingest import membership
        role = membership.get_user_org_base_role(user_id)
    except Exception:
        role = None
    if role not in ("founder", "admin", "manager"):
        raise HTTPException(
            status_code=403,
            detail="Групповая динамика встреч доступна только руководителям")


async def _load_insights(user_id: str) -> List[Dict[str, Any]]:
    """Все PsychInsight-узлы тенанта. merged view — тот же путь чтения,
    что у карточек и инсайтов."""
    from backend.core.store.graph_view import merged_graph_view_for_user
    gb = await merged_graph_view_for_user(user_id, use_networkx=None)
    try:
        return await gb.find_nodes_by_label(
            "PsychInsight", limit=500, strict_tenant=True) or []
    finally:
        try:
            await gb.close(save=False)
        except Exception:
            logger.debug("psych insights: graph close skipped", exc_info=True)


def _matches_person(node: Dict[str, Any], person_name: str) -> bool:
    who = str(node.get("participant") or "").strip().lower()
    return bool(who) and who == person_name.strip().lower()


@get("/person/{person_id:str}")
async def person_insights(
    person_id: str,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Мотивы, прогнозы поведения и рекомендации по конкретному человеку,
    сгруппированные по виду, свежие сверху."""
    uid = _resolve_user(request, user_id)
    _assert_may_read_person_insights(uid, person_id)

    # Имя человека — из его Person-узла: инсайты привязаны по имени
    # участника встречи, а спрашивают по id узла.
    person_name = ""
    try:
        from backend.core.store.graph_view import merged_graph_view_for_user
        gb = await merged_graph_view_for_user(uid, use_networkx=None)
        try:
            node = await gb.get_node_by_id(person_id)
            person_name = str((node or {}).get("name") or "")
        finally:
            try:
                await gb.close(save=False)
            except Exception:
                pass
    except Exception:
        logger.debug("psych insights: person lookup failed", exc_info=True)
    if not person_name:
        raise HTTPException(status_code=404, detail="Человек не найден")

    nodes = await _load_insights(uid)
    grouped: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _KINDS_PERSON}
    for n in nodes:
        kind = n.get("kind")
        if kind not in grouped:
            continue
        if kind == "recommendation":
            # Рекомендации: адресные — по совпадению цели, командные не
            # тащим в карточку человека.
            if not _matches_person({"participant": n.get("target")}, person_name):
                continue
        elif not _matches_person(n, person_name):
            continue
        grouped[kind].append(n)
    for k in grouped:
        grouped[k].sort(key=lambda x: str(x.get("created_at") or ""),
                        reverse=True)

    return {
        "person_id": person_id,
        "person_name": person_name,
        "insights": grouped,
        "counts": {k: len(v) for k, v in grouped.items()},
        "disclaimer": (
            "Наблюдения модели по разговорам на встречах — гипотезы, а не "
            "диагнозы. Слой не участвует в общем поиске и виден только "
            "самому человеку, его руководителю и администратору."
        ),
    }


@get("/meeting/{meeting_id:str}")
async def meeting_dynamics(
    meeting_id: str,
    request: Request,
    user_id: Optional[str] = Parameter(query="user_id", default=None),
) -> Dict[str, Any]:
    """Групповая динамика конкретной встречи (альянсы, влияние, доверие)."""
    uid = _resolve_user(request, user_id)
    _require_manager(uid)

    nodes = await _load_insights(uid)
    dynamics = [n for n in nodes
                if n.get("kind") == "group_dynamics"
                and str(n.get("meeting_id") or "") == meeting_id]
    return {
        "meeting_id": meeting_id,
        "dynamics": dynamics,
        "found": bool(dynamics),
    }


router = Router(path="/psych-insights",
                route_handlers=[person_insights, meeting_dynamics],
                tags=["Psych Insights"])
