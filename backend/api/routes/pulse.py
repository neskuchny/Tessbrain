# -*- coding: utf-8 -*-
"""Пульс исполнения — API.

GET  /pulse/report — собрать пульс-отчёт (реестр → детекторы → сводка) и
     сохранить в историю отчётов; ничего никому не рассылает.
POST /pulse/send   — то же + доставить владельцу (Telegram/email).

Фаза 2 — люди и пуши:
GET  /pulse/people           — реестр «сотрудник ↔ каналы» (+ дыры без канала)
POST /pulse/people           — досклеить человека (email/TG/руководитель/mute)
POST /pulse/people/autofill  — заглушки по всем assignee из задач
POST /pulse/push?dry_run=true— предпросмотр, кому что уйдёт (без отправки)
POST /pulse/push             — отправить (только при ENABLE_EXECUTION_PULSE_PUSH)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from litestar import Request, Router, get, post
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


@get("/report")
async def pulse_report(request: Request,
                       user_id: str = Parameter(query="user_id",
                                                required=True),
                       full: bool = Parameter(query="full", default=False,
                                              required=False)) -> Dict[str, Any]:
    """full=true — секции без обрезки «…и ещё N» (полные списки задач)."""
    uid = _resolve_user(request, user_id)
    from backend.core.pulse import build_pulse_report
    return await build_pulse_report(uid, deliver=False, full=full)


@post("/send")
async def pulse_send(request: Request,
                     user_id: str = Parameter(query="user_id",
                                              required=True)) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.pulse import build_pulse_report
    return await build_pulse_report(uid, deliver=True)


class PersonUpsert(BaseModel):
    name: str
    user_id: Optional[str] = None
    names: Optional[list] = None
    email: str = ""
    telegram_chat_id: str = ""
    reports_to: str = ""
    quiet_hours: Optional[list] = None
    muted: Optional[bool] = None


@get("/people")
async def pulse_people(request: Request,
                       user_id: str = Parameter(query="user_id",
                                                required=True)) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.pulse.people_registry import load_registry
    reg = load_registry(uid)
    rows = sorted(reg.values(), key=lambda r: (r.get("names") or [""])[0])
    return {"people": rows,
            "without_channel": [r for r in rows
                                if not (r.get("email")
                                        or r.get("telegram_chat_id"))
                                and not r.get("muted")]}


@post("/people")
async def pulse_people_upsert(data: PersonUpsert,
                              request: Request) -> Dict[str, Any]:
    uid = _resolve_user(request, data.user_id)
    from backend.core.pulse.people_registry import upsert_person
    try:
        rec = upsert_person(uid, name=data.name, names=data.names,
                            email=data.email,
                            telegram_chat_id=data.telegram_chat_id,
                            reports_to=data.reports_to,
                            quiet_hours=data.quiet_hours, muted=data.muted)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "person": rec}


@post("/people/autofill")
async def pulse_people_autofill(request: Request,
                                user_id: str = Parameter(query="user_id",
                                                         required=True)) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.pulse.ledger import build_ledger
    from backend.core.pulse.people_registry import autofill_from_tasks
    ledger = await build_ledger(uid, emit_events=False)
    return {"status": "success",
            **autofill_from_tasks(uid, ledger["tasks"])}


@post("/push")
async def pulse_push(request: Request,
                     user_id: str = Parameter(query="user_id", required=True),
                     dry_run: bool = Parameter(query="dry_run",
                                               default=False)) -> Dict[str, Any]:
    uid = _resolve_user(request, user_id)
    from backend.core.pulse.push_engine import send_pushes
    return await send_pushes(uid, dry_run=dry_run)


@post("/telegram-webhook", status_code=200)
async def pulse_telegram_webhook(request: Request,
                                 data: Dict[str, Any]) -> Dict[str, Any]:
    """Входящие ответы сотрудников боту (замыкание петли «ответ → задачник»).

    Безопасность fail-closed: секрет обязателен. При настройке вебхука у
    Telegram указывается secret_token, и он должен совпасть с env
    PULSE_TG_WEBHOOK_SECRET — иначе 403 (нет секрета в env = endpoint закрыт).
    Настройка бота: setWebhook(url=https://<host>/api/v1/pulse/telegram-webhook,
    secret_token=$PULSE_TG_WEBHOOK_SECRET).
    """
    import os
    secret = os.environ.get("PULSE_TG_WEBHOOK_SECRET", "").strip()
    got = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secret or got != secret:
        raise HTTPException(status_code=403, detail="bad webhook secret")

    msg = (data or {}).get("message") or {}
    chat_id = str(((msg.get("chat") or {}).get("id")) or "")
    text = str(msg.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True}     # сервисные апдейты просто подтверждаем

    from backend.core.pulse.inbound import _load_index, handle_reply
    res = await handle_reply(chat_id, text)

    # ответить человеку тем же ботом (токен тенанта-владельца из контекста)
    owner_uid = (_load_index().get(chat_id) or {}).get("owner_uid") or ""
    try:
        from backend.core.pulse.push_engine import _send_telegram
        await _send_telegram(owner_uid, chat_id, res.get("reply") or "Принято.")
    except Exception:
        logger.warning("pulse webhook: ответ не отправлен", exc_info=True)
    return {"ok": True, "matched": res.get("matched", False)}


router = Router(path="/pulse",
                route_handlers=[pulse_report, pulse_send, pulse_people,
                                pulse_people_upsert, pulse_people_autofill,
                                pulse_push, pulse_telegram_webhook],
                tags=["Execution Pulse"])
