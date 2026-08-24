"""П.6 — анонимный агрегированный сигнал наверх (к руководителю).

Копим анонимные вклады разных пользователей по теме (aggregate_key). Когда
distinct контрибьюторов достигает k — эмитим агрегат руководителю ОДИН раз
через bridge (message_type='aggregate'). До порога наружу не уходит ничего.

Эмит-протокол (claim → send → release-on-failure):
  1. try_emit_once: атомарно застолбить ключ (выигрывает один при гонке);
  2. bridge.send_message руководителю;
  3. если доставка провалилась — release_emit, чтобы следующий contribute
     повторил (иначе ключ навсегда «эмитнут», но не доставлен).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.core.aggregation.core import (
    DEFAULT_K,
    DOMAIN_MANAGER_SIGNAL,
    _norm_k,
    aggregate_view,
    contribute,
    release_emit,
    try_emit_once,
)

logger = logging.getLogger(__name__)


async def contribute_manager_signal(
    *,
    tenant_id: str,
    aggregate_key: str,
    manager_user_id: str,
    contributor_user_id: str,
    payload: Optional[dict[str, Any]] = None,
    k: int = DEFAULT_K,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Анонимный вклад в сигнал наверх. Never-raise.

    Returns:
        {"ok", "distinct_count", "k", "emitted": bool,
         "bridge_message_id": str|None, "reason"}
    """
    out: dict[str, Any] = {
        "ok": False, "distinct_count": 0, "k": k, "emitted": False,
        "bridge_message_id": None, "reason": "",
    }
    if not (tenant_id and aggregate_key and manager_user_id and contributor_user_id):
        out["reason"] = "tenant_id, aggregate_key, manager_user_id, contributor_user_id required"
        return out

    dcount = await contribute(
        tenant_id=tenant_id,
        domain=DOMAIN_MANAGER_SIGNAL,
        aggregate_key=aggregate_key,
        contributor_user_id=contributor_user_id,
        payload=payload,
    )
    if dcount is None:
        out["reason"] = "contribute failed"
        return out
    out.update(ok=True, distinct_count=dcount, reason="contributed")

    # Ниже порога — молчим (k-anonymity).
    # Порог берём через _norm_k: он не даёт опустить его ниже DEFAULT_K.
    # Здесь k приходит из тела запроса — без пола отправитель мог указать
    # k=1 и доставить руководителю сигнал от единственного человека,
    # то есть снять анонимность, ради которой канал и существует.
    k = _norm_k(k)
    out["k"] = k
    if dcount < k:
        return out

    # Порог достигнут — пробуем эмит ровно один раз.
    won = await try_emit_once(
        tenant_id=tenant_id,
        domain=DOMAIN_MANAGER_SIGNAL,
        aggregate_key=aggregate_key,
        distinct_count=dcount,
        target_user_id=manager_user_id,
    )
    if not won:
        # Уже эмитнуто ранее — это нормально, просто не дублируем.
        out["reason"] = "threshold_reached_already_emitted"
        return out

    # Собираем агрегат (suppressed=False, т.к. dcount >= k) и шлём руководителю.
    view = await aggregate_view(
        tenant_id=tenant_id,
        domain=DOMAIN_MANAGER_SIGNAL,
        aggregate_key=aggregate_key,
        k=k,
    )
    try:
        from backend.core.bridge import send_message
        res = await send_message(
            from_user_id=tenant_id,            # системный отправитель (tenant)
            to_user_id=manager_user_id,
            title=title or f"Агрегированный сигнал: {aggregate_key}",
            body_markdown=(
                f"Анонимный агрегированный сигнал от {dcount} сотрудников "
                f"(порог k={k} достигнут)."
            ),
            payload={
                "kind": "aggregate_signal",
                "aggregate_key": aggregate_key,
                "distinct_count": dcount,
                "k": k,
                # contributions БЕЗ contributor-id (k-anonymity соблюдён в core)
                "contributions": view.contributions,
            },
            message_type="aggregate",
            push=True,
        )
    except Exception as exc:
        logger.warning("manager_signal emit send failed: %s", exc)
        res = {"ok": False, "reason": str(exc)}

    if res.get("ok"):
        out.update(emitted=True, bridge_message_id=res.get("message_id"),
                   reason="emitted")
    else:
        # Доставка не удалась — снимаем claim, чтобы повторить на след. вкладе.
        await release_emit(
            tenant_id=tenant_id, domain=DOMAIN_MANAGER_SIGNAL,
            aggregate_key=aggregate_key,
        )
        out["reason"] = f"emit_claim_released: {res.get('reason')}"
    return out
