"""Aggregation endpoints — P2 §6 (manager signals) + п.7 (360-review).

k-anonymous кросс-юзерная агрегация по тенанту. Вклады анонимны: личность
контрибьютора/рейтера используется только для distinct-count, наружу не идёт.

Endpoints:
- POST /aggregation/manager-signal      — анонимный вклад в сигнал наверх;
                                           эмит руководителю при >= k.
- GET  /aggregation/manager-signal/status — статус bucket'а (count vs k).
- POST /aggregation/360/feedback        — peer-фидбек о субъекте.
- GET  /aggregation/360/{subject_user_id} — собранный 360-review.

tenant_id берётся из контекста (set middleware), fallback — user_id.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.aggregation import (
    DEFAULT_K,
    build_360_review,
    contribute_manager_signal,
    distinct_count,
    submit_360_feedback,
)
from backend.core.aggregation.core import (
    DOMAIN_MANAGER_SIGNAL,
)

logger = logging.getLogger(__name__)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization required")
    try:
        uid = get_user_id_from_token(authorization)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc
    if not uid:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return uid


def _tenant(user_id: str) -> str:
    """tenant_id из контекста, fallback на user_id (инвариант кодовой базы)."""
    try:
        from backend.core.observability.tenant_context import get_current_tenant
        return get_current_tenant() or user_id
    except Exception:
        return user_id


def _assert_may_read_360(asker_uid: str, subject_user_id: str) -> None:
    """Кто вправе смотреть 360 человека: он сам, его руководитель по цепочке
    подчинения, администратор организации.

    Раньше проверки не было вовсе — эндпоинт требовал лишь валидный токен, и
    любой сотрудник мог собрать 360 на любого другого. Порог анонимности
    защищает авторов отзывов друг от друга, но он никак не ограничивает
    круг читателей результата; без этой проверки peer-фидбек о человеке был
    доступен всей компании.

    Коллеге того же отдела доступ НЕ даём: 360 — инструмент разговора о
    развитии между человеком и его руководителем, а не общедоступная
    характеристика. Это сознательно строже, чем политика доступа к слепку.
    """
    if asker_uid == subject_user_id:
        return
    try:
        from backend.core.ingest.membership import get_org_for_user
        from backend.core.twin.policy import _is_org_admin, is_manager_of
        org_id = get_org_for_user(asker_uid) or ""
        if org_id and is_manager_of(asker_uid, subject_user_id, org_id):
            return
        if org_id and _is_org_admin(asker_uid, org_id):
            return
    except Exception as exc:
        # Fail-CLOSED: не смогли подтвердить право — не отдаём. В отличие от
        # доступа к слепку (там сбой чтения не должен ломать работу), здесь
        # цена ошибки — раскрытие отзывов коллег о человеке.
        logger.warning("360 access check failed for %s→%s: %s",
                       asker_uid, subject_user_id, exc)
        raise HTTPException(
            status_code=403,
            detail="Не удалось подтвердить право на просмотр 360",
        ) from exc
    raise HTTPException(
        status_code=403,
        detail="360-review доступен самому человеку, его руководителю и "
               "администратору организации",
    )


@post("/manager-signal", status_code=200)
async def manager_signal_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Анонимный вклад в агрегированный сигнал наверх.

    Body:
        {
          "aggregate_key": "remote_work_policy",   # тема (required)
          "manager_user_id": "<uuid>",             # кому эмитить (required)
          "payload": {...},                         # содержимое вклада
          "k": 5,                                   # порог (default 5)
          "title": "..."                            # optional заголовок эмита
        }

    Контрибьютор = текущий JWT-юзер (анонимен в агрегате). При достижении k
    разных контрибьюторов — агрегат эмитится руководителю через bridge.
    """
    uid = _extract_user(authorization)
    akey = data.get("aggregate_key")
    manager = data.get("manager_user_id")
    if not akey or not manager:
        raise HTTPException(
            status_code=400,
            detail="aggregate_key and manager_user_id required",
        )
    res = await contribute_manager_signal(
        tenant_id=_tenant(uid),
        aggregate_key=str(akey),
        manager_user_id=str(manager),
        contributor_user_id=uid,
        payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
        k=int(data.get("k") or DEFAULT_K),
        title=data.get("title"),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("reason") or "failed")
    # distinct_count и emitted отдаём, но НЕ раскрываем contributions, пока
    # текущий юзер не имеет права (это not-his-business) — наружу только метрики.
    return {
        "success": True,
        "distinct_count": res["distinct_count"],
        "k": res["k"],
        "emitted": res["emitted"],
        "bridge_message_id": res.get("bridge_message_id"),
    }


@get("/manager-signal/status", status_code=200)
async def manager_signal_status_endpoint(
    aggregate_key: str = Parameter(query="aggregate_key"),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Статус bucket'а: сколько разных контрибьюторов набралось. НЕ раскрывает
    вклады (только счётчик)."""
    uid = _extract_user(authorization)
    if not aggregate_key:
        raise HTTPException(status_code=400, detail="aggregate_key required")
    dcount = await distinct_count(
        tenant_id=_tenant(uid),
        domain=DOMAIN_MANAGER_SIGNAL,
        aggregate_key=aggregate_key,
    )
    return {"aggregate_key": aggregate_key, "distinct_count": dcount}


@post("/360/feedback", status_code=200)
async def review360_feedback_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Peer-фидбек о субъекте (один рейтер = один вклад, upsert).

    Body:
        {
          "subject_user_id": "<uuid>",             # о ком (required)
          "ratings": {"leadership": 4, ...},        # компетенция → оценка
          "comment": "..."                          # свободный текст
        }

    Рейтер = текущий JWT-юзер (анонимен в ревью). Нельзя оценивать себя.
    """
    uid = _extract_user(authorization)
    subject = data.get("subject_user_id")
    if not subject:
        raise HTTPException(status_code=400, detail="subject_user_id required")
    res = await submit_360_feedback(
        tenant_id=_tenant(uid),
        subject_user_id=str(subject),
        rater_user_id=uid,
        ratings=data.get("ratings") if isinstance(data.get("ratings"), dict) else None,
        comment=data.get("comment"),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("reason") or "failed")
    return {
        "success": True,
        "distinct_raters": res["distinct_raters"],
    }


@get("/360/{subject_user_id:str}", status_code=200)
async def review360_build_endpoint(
    subject_user_id: str,
    k: int = Parameter(query="k", default=DEFAULT_K),
    include_self: bool = Parameter(query="include_self", default=True),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Собранный 360-review субъекта. others-блок подавлён, пока рейтеров < k;
    per-компетенция подавляется при < k оценок. self-view — из персоны."""
    uid = _extract_user(authorization)
    _assert_may_read_360(uid, subject_user_id)
    review = await build_360_review(
        tenant_id=_tenant(uid),
        subject_user_id=subject_user_id,
        k=int(k or DEFAULT_K),
        include_self=bool(include_self),
    )
    return review


router = Router(
    path="/aggregation",
    route_handlers=[
        manager_signal_endpoint,
        manager_signal_status_endpoint,
        review360_feedback_endpoint,
        review360_build_endpoint,
    ],
    tags=["Aggregation"],
)
