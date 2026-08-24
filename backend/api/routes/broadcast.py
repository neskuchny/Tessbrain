"""REST для «Трансляции стратегии» (strategy relay).

Руководитель пишет одно послание → система готовит персональную версию
для каждого члена организации (фрейм из Persona/Mini Tess) → руководитель
правит и отправляет → сотрудники видят СВОЮ версию (inbox) и жмут «Принял».

- POST /broadcast/strategy        {message} → черновик со всеми версиями
- GET  /broadcast/list            мои объявления (руководитель)
- POST /broadcast/{bid}/version   {member_uid, text} — ручная правка версии
- POST /broadcast/{bid}/send      отправить (telegram best-effort + сигнал)
- GET  /broadcast/inbox           мои входящие (сотрудник)
- POST /broadcast/{bid}/ack       {question?} — «понял, принял»
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

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


@post("/strategy")
async def create_strategy_broadcast(
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.broadcast.strategy_relay import create_broadcast
    try:
        return await create_broadcast(uid, str((data or {}).get("message") or ""))
    except Exception as e:  # noqa: BLE001
        logger.error(f"broadcast create failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@get("/list")
async def list_strategy_broadcasts(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.broadcast.strategy_relay import list_broadcasts
    return list_broadcasts(uid)


@post("/{bid:str}/version")
async def update_broadcast_version(
    bid: str,
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.broadcast.strategy_relay import update_version
    return update_version(uid, bid,
                          str((data or {}).get("member_uid") or ""),
                          str((data or {}).get("text") or ""))


@post("/{bid:str}/send")
async def send_strategy_broadcast(
    bid: str,
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.broadcast.strategy_relay import send_broadcast
    try:
        return await send_broadcast(uid, bid)
    except Exception as e:  # noqa: BLE001
        logger.error(f"broadcast send failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@get("/inbox")
async def my_broadcast_inbox(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.broadcast.strategy_relay import inbox
    return inbox(uid)


@post("/{bid:str}/ack")
async def ack_broadcast(
    bid: str,
    data: Dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> Dict[str, Any]:
    uid = _extract_user(authorization)
    from backend.core.broadcast.strategy_relay import ack
    return ack(uid, bid, question=str((data or {}).get("question") or ""))


router = Router(
    path="/broadcast",
    route_handlers=[
        create_strategy_broadcast,
        list_strategy_broadcasts,
        update_broadcast_version,
        send_strategy_broadcast,
        my_broadcast_inbox,
        ack_broadcast,
    ],
    tags=["Strategy Broadcast"],
)
