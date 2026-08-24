"""Bridge REST endpoints (P2 §5) — user→user транспорт внутри Tessbrain.

Закрывает отсутствовавший контракт «корпоративного моста» для Mini Tess
(EPIC D): отправить сообщение другому пользователю + забрать свой inbox +
подтвердить прочтение.

Endpoints:
- POST   /bridge/messages              — отправить сообщение to_user_id
- GET    /bridge/inbox                 — входящие текущего юзера
- GET    /bridge/outbox                — исходящие текущего юзера
- POST   /bridge/messages/{id}/ack     — пометить прочитанным

Все требуют JWT. Сообщения durable (bridge_messages) + опционально пушатся
получателю через reactive signal_queue (signal_type='bridge_message') →
drain → Telegram.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.api.middleware.auth_middleware import get_user_id_from_token
from backend.core.bridge import (
    list_inbox,
    list_outbox,
    mark_read,
    send_message,
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


@post("/messages", status_code=201)
async def send_message_endpoint(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Отправить сообщение другому пользователю.

    Body:
        {
          "to_user_id": "<uuid>",                 # required
          "body_markdown": "текст",               # тело
          "title": "...",                          # optional
          "message_type": "message"               # message|manager_signal|
                                                  #   manager_signal_anon|aggregate
          "payload": {...},                        # optional структурные данные
          "push": true                             # default true — пушить в Telegram
        }

    Отправитель = текущий юзер (из JWT). 404 если получатель не найден.
    """
    from_user = _extract_user(authorization)
    to_user = data.get("to_user_id")
    if not to_user or not isinstance(to_user, str):
        raise HTTPException(status_code=400, detail="to_user_id required")

    res = await send_message(
        from_user_id=from_user,
        to_user_id=to_user,
        body_markdown=str(data.get("body_markdown") or ""),
        title=data.get("title"),
        payload=data.get("payload") if isinstance(data.get("payload"), dict) else None,
        message_type=str(data.get("message_type") or "message"),
        push=bool(data.get("push", True)),
    )
    if not res.get("ok"):
        reason = res.get("reason") or "send failed"
        # «recipient not found» → 404, остальное → 400
        code = 404 if "not found" in reason else 400
        raise HTTPException(status_code=code, detail=reason)
    return {
        "success": True,
        "message_id": res["message_id"],
        "signal_id": res.get("signal_id"),
    }


@get("/inbox", status_code=200)
async def inbox_endpoint(
    status: Optional[str] = Parameter(query="status", default=None),
    limit: int = Parameter(query="limit", default=50),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Входящие сообщения текущего юзера. ?status=unread для непрочитанных."""
    uid = _extract_user(authorization)
    msgs = await list_inbox(user_id=uid, status=status, limit=int(limit or 50))
    return {
        "user_id": uid,
        "count": len(msgs),
        "messages": [m.to_dict() for m in msgs],
    }


@get("/outbox", status_code=200)
async def outbox_endpoint(
    limit: int = Parameter(query="limit", default=50),
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Исходящие сообщения текущего юзера."""
    uid = _extract_user(authorization)
    msgs = await list_outbox(user_id=uid, limit=int(limit or 50))
    return {
        "user_id": uid,
        "count": len(msgs),
        "messages": [m.to_dict() for m in msgs],
    }


@post("/messages/{message_id:str}/ack", status_code=200)
async def ack_endpoint(
    message_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Пометить входящее сообщение прочитанным. Только получатель может."""
    uid = _extract_user(authorization)
    ok = await mark_read(message_id=message_id, user_id=uid)
    if not ok:
        # уже прочитано / не существует / не наше
        raise HTTPException(
            status_code=404,
            detail="message not found, not yours, or already read",
        )
    return {"success": True, "message_id": message_id, "status": "read"}


router = Router(
    path="/bridge",
    route_handlers=[
        send_message_endpoint,
        inbox_endpoint,
        outbox_endpoint,
        ack_endpoint,
    ],
    tags=["Bridge"],
)
