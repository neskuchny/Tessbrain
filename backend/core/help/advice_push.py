# -*- coding: utf-8 -*-
"""
Периодический пуш персонального совета в Telegram.

Отправляем через тот же бот, к которому пользователь привязал свой Telegram
(MessengerLink → external_id = chat_id). Отдельный бот тоже возможен — задать
`GUIDE_PUSH_BOT_TOKEN`, иначе берётся основной `telegram_bot_token`.

Рассылка ВЫКЛЮЧЕНА по умолчанию (env `GUIDE_WEEKLY_PUSH=on`) — чтобы не спамить
и не тратить токены; частота — не чаще раза в неделю (задача scheduler).
Ничего не raises.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def weekly_push_enabled() -> bool:
    return os.getenv("GUIDE_WEEKLY_PUSH", "").strip().lower() in ("1", "on", "true", "yes")


def _bot_token() -> str:
    tok = os.getenv("GUIDE_PUSH_BOT_TOKEN", "").strip()
    if tok:
        return tok
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    try:
        from backend.config import get_settings
        return (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
    except Exception:
        return ""


async def _send_telegram(chat_id: str, text: str, *, user_id: str = "") -> bool:
    # Токен: платформенный, а если не задан — BYO bot_token из «Интеграций»
    # пользователя (общий резолвер). Даёт слать, вписав токен во вкладке.
    token = ""
    if user_id:
        try:
            from backend.core.messengers.links import resolve_telegram_bot_token
            token = (await resolve_telegram_bot_token(user_id)) or ""
        except Exception:
            token = ""
    if not token:
        token = _bot_token()
    if not (token and chat_id):
        return False
    try:
        # markdown (## заголовки, **жирный**, таблицы) → Telegram-HTML с
        # фолбэком в чистый текст; длинный текст режется на куски, а не
        # обрубается на 4000 символов
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        res = await post_telegram_text(token, chat_id, text, timeout=10.0)
        return bool(res["ok"])
    except Exception as e:
        logger.warning("advice push send failed: %s", e)
        return False


async def _telegram_chat_for_user(user_id: str) -> Optional[str]:
    """chat_id для отправки: привязка платформенного бота, а при её отсутствии —
    вручную заданный Default Chat ID из «Интеграций» (общий резолвер, чтобы
    доставка работала и без вебхук-привязки, напр. на локалхосте)."""
    try:
        from backend.core.messengers.links import resolve_telegram_chat_id
        return await resolve_telegram_chat_id(user_id)
    except Exception as e:
        logger.debug("telegram chat lookup failed: %s", e)
    return None


async def notify_user_telegram(user_id: str, text: str) -> bool:
    """Отправить произвольное сообщение пользователю в его привязанный Telegram.
    False, если нет привязки/бота. Никогда не raises. Переиспользуется, напр.,
    для уведомления «мы починили ваш тикет»."""
    if not (user_id and text):
        return False
    chat_id = await _telegram_chat_for_user(user_id)
    if not chat_id:
        return False
    return await _send_telegram(chat_id, text, user_id=user_id)


async def _user_email(user_id: str) -> Optional[str]:
    """Email пользователя из профиля, если указан."""
    try:
        from backend.memory.user_profiles import get_user_profile_service
        p = await get_user_profile_service().get_profile(user_id)
        email = (getattr(p, "email", None) or "").strip()
        return email if "@" in email else None
    except Exception:
        return None


async def notify_user_email(user_id: str, subject: str, body: str,
                            *, to: Optional[str] = None,
                            attachments: Optional[list] = None) -> bool:
    """Написать пользователю (или на явный `to`) на почту. attachments —
    [{"filename","content_b64","mime"}]. False, если нет email/транспорта.
    Никогда не raises."""
    if not (user_id and subject and body):
        return False
    email = (to or "").strip() or await _user_email(user_id)
    if not email:
        return False
    try:
        from backend.core.notifications.email import EmailMessage, send_email
        res = await send_email(EmailMessage(
            to=email, subject=subject, text_body=body,
            attachments=list(attachments or [])))
        return bool(getattr(res, "ok", False))
    except Exception as e:
        logger.debug("notify_user_email failed: %s", e)
        return False


async def notify_user(user_id: str, *, subject: str, text: str) -> dict[str, bool]:
    """Уведомить пользователя всеми доступными каналами (Telegram + email).
    Возвращает {telegram, email}. Best-effort."""
    tg = await notify_user_telegram(user_id, text)
    em = await notify_user_email(user_id, subject, text)
    return {"telegram": tg, "email": em}


async def push_advice_to_user(user_id: str) -> bool:
    """Сгенерировать совет и отправить пользователю в его Telegram. False —
    если нет привязки/совета/бота. Никогда не raises."""
    if not user_id:
        return False
    chat_id = await _telegram_chat_for_user(user_id)
    if not chat_id:
        return False
    try:
        from backend.core.help.usage_advisor import build_advice
        adv = await build_advice(user_id)
    except Exception as e:
        logger.debug("advice build failed: %s", e)
        return False
    text = (adv or {}).get("advice", "").strip()
    if not text:
        return False
    return await _send_telegram(chat_id, f"💡 Совет по Tessbrain под вас:\n\n{text}")


async def push_weekly_advice(*, limit: int = 2000) -> int:
    """Разослать совет всем пользователям с профилем и привязанным Telegram.
    ВЫКЛ по умолчанию — только при GUIDE_WEEKLY_PUSH=on. Возвращает число
    отправленных."""
    if not weekly_push_enabled():
        logger.debug("weekly advice push disabled (GUIDE_WEEKLY_PUSH off)")
        return 0
    if not _bot_token():
        logger.debug("weekly advice push: no bot token")
        return 0
    try:
        from backend.memory.user_profiles import get_user_profile_service
        uids = await get_user_profile_service().list_user_ids(limit=limit)
    except Exception as e:
        logger.warning("weekly push: list users failed: %s", e)
        return 0
    sent = 0
    for uid in uids:
        try:
            if await push_advice_to_user(uid):
                sent += 1
        except Exception:
            continue
    if sent:
        logger.info("📨 Weekly advice pushed to %d users", sent)
    return sent


__all__ = ["push_advice_to_user", "push_weekly_advice", "weekly_push_enabled",
           "notify_user_telegram", "notify_user_email", "notify_user"]
