# -*- coding: utf-8 -*-
"""Входящие вебхуки: письмо (AI-почта) и заявка из CRM/формы → событие доски.

Внешний mail-провайдер (SendGrid Inbound Parse / Mailgun route / свой релей) или
CRM/веб-форма шлёт сюда POST → эмитим событие `new_email` / `new_lead` в EventBus
→ процесс-доски, подписанные на это событие, запускаются (текст письма/заявки
течёт в граф как вход триггера).

Аутентификация вебхука: общий секрет INBOUND_WEBHOOK_SECRET (заголовок
X-Webhook-Secret или ?secret=). Не задан — открыто (для разработки). Never-raise.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from litestar import Router, post
from litestar.params import Parameter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InboundEmailRequest(BaseModel):
    """Нормализованное входящее письмо."""
    user_id: str = Field(..., description="ID пользователя-получателя (тенант)")
    from_addr: str = Field(default="", description="Адрес отправителя")
    subject: str = Field(default="", description="Тема письма")
    body: str = Field(default="", description="Текст письма")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Доп. поля (attachments и т.п.)")


class InboundLeadRequest(BaseModel):
    """Входящая заявка из CRM/формы."""
    user_id: str = Field(..., description="ID пользователя-получателя (тенант)")
    source: str = Field(default="crm", description="Источник заявки (crm|form|…)")
    title: str = Field(default="", description="Короткий заголовок заявки")
    body: str = Field(default="", description="Текст заявки")
    meta: Dict[str, Any] = Field(default_factory=dict, description="client/email/amount и т.п.")


def _secret_ok(secret: Optional[str]) -> bool:
    want = (os.getenv("INBOUND_WEBHOOK_SECRET", "") or "").strip()
    if not want:
        return True  # секрет не задан — открыто (dev)
    return str(secret or "").strip() == want


async def _emit(event_type: str, *, user_id: str, payload: Dict[str, Any]) -> bool:
    """Эмитить событие в EventBus (+ идемпотентно подписать доски). Never-raise."""
    try:
        from backend.core.automations.event_bus import get_event_bus
        bus = get_event_bus()
        try:
            from backend.core.board.event_triggers import register_board_event_handlers
            register_board_event_handlers()  # идемпотентно
        except Exception as _e:
            logger.debug("inbound: board handlers registration skipped: %s", _e)
        await bus.emit(event_type=event_type, payload=payload,
                       user_id=user_id, source="inbound_webhook")
        return True
    except Exception as e:
        logger.warning("inbound emit failed (%s): %s", event_type, e)
        return False


@post("/inbound/email")
async def inbound_email(
    data: InboundEmailRequest,
    secret: Optional[str] = Parameter(header="X-Webhook-Secret", default=None),
    secret_q: Optional[str] = Parameter(query="secret", default=None),
) -> Dict[str, Any]:
    """Входящее письмо → событие `new_email`. Доски с триггером «Входящее
    письмо» запустятся; текст письма (От/Тема/Тело) течёт в граф."""
    if not _secret_ok(secret or secret_q):
        return {"status": "error", "message": "invalid webhook secret"}
    uid = (data.user_id or "").strip()
    if not uid:
        return {"status": "error", "message": "user_id required"}
    text_parts = []
    if data.from_addr:
        text_parts.append(f"От: {data.from_addr}")
    if data.subject:
        text_parts.append(f"Тема: {data.subject}")
    if data.body:
        text_parts.append("\n" + data.body)
    payload = {
        "text": "\n".join(text_parts).strip() or (data.subject or "письмо"),
        "from": data.from_addr, "subject": data.subject, "body": data.body,
        **(data.meta or {}),
    }
    ok = await _emit("new_email", user_id=uid, payload=payload)
    return {"status": "success" if ok else "error", "event_type": "new_email"}


@post("/inbound/lead")
async def inbound_lead(
    data: InboundLeadRequest,
    secret: Optional[str] = Parameter(header="X-Webhook-Secret", default=None),
    secret_q: Optional[str] = Parameter(query="secret", default=None),
) -> Dict[str, Any]:
    """Входящая заявка (CRM/форма) → событие `new_lead`. Доски с триггером
    «Входящая заявка» запустятся; текст заявки течёт в граф."""
    if not _secret_ok(secret or secret_q):
        return {"status": "error", "message": "invalid webhook secret"}
    uid = (data.user_id or "").strip()
    if not uid:
        return {"status": "error", "message": "user_id required"}
    parts = []
    if data.title:
        parts.append(data.title)
    if data.body:
        parts.append(data.body)
    payload = {
        "text": "\n".join(parts).strip() or "заявка",
        "title": data.title, "body": data.body, "lead_source": data.source,
        **(data.meta or {}),
    }
    ok = await _emit("new_lead", user_id=uid, payload=payload)
    return {"status": "success" if ok else "error", "event_type": "new_lead"}


@post("/inbound/imap/poll")
async def inbound_imap_poll(
    secret: Optional[str] = Parameter(header="X-Webhook-Secret", default=None),
    secret_q: Optional[str] = Parameter(query="secret", default=None),
) -> Dict[str, Any]:
    """Ручной прогон IMAP-поллера (для внешнего cron или проверки). Тянет
    непрочитанные письма и эмитит `new_email`. Работает только при включённом
    ENABLE_IMAP_POLLER; иначе вернёт enabled=false и ничего не сделает."""
    if not _secret_ok(secret or secret_q):
        return {"status": "error", "message": "invalid webhook secret"}
    try:
        from backend.core.automations.imap_poller import poll_once
        res = await poll_once()
        return {"status": "success", **res}
    except Exception as e:
        logger.warning("inbound imap poll failed: %s", e)
        return {"status": "error", "message": str(e)}


router = Router(path="",
                route_handlers=[inbound_email, inbound_lead, inbound_imap_poll],
                tags=["Inbound Webhooks"])
