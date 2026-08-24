# -*- coding: utf-8 -*-
"""
Отправка сообщений в группу саппорта Tessbrain (разработчикам).

Прямой вызов Telegram Bot API токеном приложения в чат из
`TESSENT_SUPPORT_CHAT_ID` (env) — не зависит от пользовательских интеграций.
Если не настроено — тихо возвращает False (тикет всё равно сохранён в стор).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(_bot_token() and _support_chat_id())


def _bot_token() -> str:
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        from backend.config import get_settings
        return (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
    except Exception:
        return ""


def _support_chat_id() -> str:
    return os.getenv("TESSENT_SUPPORT_CHAT_ID", "").strip()


async def send_to_support(text: str) -> bool:
    """Отправить текст в группу саппорта. Никогда не raises."""
    token, chat = _bot_token(), _support_chat_id()
    if not (token and chat):
        logger.debug("support notify skipped: not configured "
                     "(TELEGRAM_BOT_TOKEN + TESSENT_SUPPORT_CHAT_ID)")
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text[:4000],
                      "disable_web_page_preview": True})
            return r.status_code == 200
    except Exception as e:
        logger.warning("support notify failed: %s", e)
        return False


__all__ = ["send_to_support", "is_configured"]
