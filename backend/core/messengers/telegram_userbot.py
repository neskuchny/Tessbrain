# -*- coding: utf-8 -*-
"""
Telegram userbot reader — ЕДИНСТВЕННЫЙ способ читать историю ГРУППЫ.

Bot API историю группы читать не умеет: нет `getChatHistory`, а
`getUpdates`/webhook стримят только НОВЫЕ сообщения (privacy-mode прячет
чужие). Userbot — реальный аккаунт-участник группы через Telethon +
StringSession.

Однократная настройка оператором (иначе — честный отказ, ничего не падает):
  - TELEGRAM_API_ID / TELEGRAM_API_HASH — с my.telegram.org;
  - TELEGRAM_SESSION_STRING — StringSession аккаунта, который УЖЕ состоит в
    группе (сгенерировать один раз, см. docs/RESEARCH_DIGEST_AUTOMATIONS.md).

Telethon — ОПЦИОНАЛЬНАЯ зависимость: не установлена или сессия не задана →
{ok: False, reason: ...}. Никогда не raises.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    """True только если заданы все три параметра userbot-сессии."""
    return bool(
        os.getenv("TELEGRAM_SESSION_STRING")
        and os.getenv("TELEGRAM_API_ID")
        and os.getenv("TELEGRAM_API_HASH")
    )


def _resolve_target(chat: str) -> Any:
    """@username / ссылка / числовой id → аргумент для Telethon."""
    c = (chat or "").strip()
    if not c:
        return c
    # числовой id (в т.ч. отрицательный для супергрупп) → int
    try:
        return int(c)
    except (TypeError, ValueError):
        return c


async def read_group_history(chat: str, *, days: int = 7,
                             limit: int = 2000) -> dict[str, Any]:
    """История группы за последние `days` дней (хронологический порядок).

    Возвращает {ok, count, messages:[{sender, text, ts}]} или
    {ok: False, reason}. Никогда не raises."""
    if not is_configured():
        return {"ok": False,
                "reason": "userbot не настроен (TELEGRAM_SESSION_STRING/API_ID/API_HASH)"}
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except Exception:
        return {"ok": False, "reason": "telethon не установлен (pip install telethon)"}

    from datetime import datetime, timedelta, timezone

    try:
        api_id = int(os.getenv("TELEGRAM_API_ID") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "TELEGRAM_API_ID должен быть числом"}
    api_hash = os.getenv("TELEGRAM_API_HASH") or ""
    session = os.getenv("TELEGRAM_SESSION_STRING") or ""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    target = _resolve_target(chat)

    messages: list[dict[str, Any]] = []
    try:
        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {"ok": False, "reason": "сессия невалидна/не авторизована"}
            # iter_messages идёт от НОВЫХ к старым → как только вышли за окно,
            # останавливаемся.
            async for m in client.iter_messages(target, limit=limit):
                mdate = getattr(m, "date", None)
                if mdate is not None and mdate < cutoff:
                    break
                text = getattr(m, "message", None) or getattr(m, "text", None)
                if not text:
                    continue
                sender = ""
                try:
                    s = await m.get_sender()
                    sender = (getattr(s, "first_name", None)
                              or getattr(s, "username", None)
                              or str(getattr(m, "sender_id", "") or ""))
                except Exception:
                    sender = str(getattr(m, "sender_id", "") or "")
                messages.append({
                    "sender": sender,
                    "text": str(text)[:2000],
                    "ts": mdate.isoformat() if mdate else "",
                })
        finally:
            await client.disconnect()
    except Exception as e:
        logger.warning("telegram userbot read failed: %s", e)
        return {"ok": False, "reason": f"ошибка чтения: {e}"}

    messages.reverse()  # новые→старые ⇒ разворачиваем в хронологический
    return {"ok": True, "count": len(messages), "messages": messages}


__all__ = ["is_configured", "read_group_history"]
