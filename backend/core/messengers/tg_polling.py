# -*- coding: utf-8 -*-
"""Long-polling Telegram-бота — для локальной разработки без туннеля.

Telegram доставляет входящие двумя взаимоисключающими способами:
1. Вебхук — нужен ПУБЛИЧНЫЙ https-адрес (прод);
2. getUpdates (long-polling) — бэкенд сам забирает апдейты; публичный
   адрес не нужен вообще.

Локальный сервер вебхук получить не может — бот «молчал», хотя отправка
пушей работала (исходящие вебхука не требуют). Поллер закрывает дыру:
TELEGRAM_POLLING=on → фоновая задача тянет getUpdates и прогоняет каждый
апдейт через ТОТ ЖЕ process_telegram_update, что и вебхук (личные чаты,
группы с /brain_on, голосовые — всё одинаково).

Важно: при старте поллер снимает вебхук (deleteWebhook) — иначе Telegram
отвечает 409. На проде флаг держите выключенным, там вебхук.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_POLL_TIMEOUT = 25       # long-poll seconds
_ERROR_BACKOFF = 5.0


def polling_enabled() -> bool:
    return os.environ.get("TELEGRAM_POLLING", "").strip().lower() in (
        "1", "on", "true", "yes")


def _bot_token() -> str:
    try:
        from backend.config import get_settings
        tok = (getattr(get_settings(), "telegram_bot_token", "") or "").strip()
        if tok:
            return tok
    except Exception:
        pass
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


async def poll_once(client: Any, token: str, offset: int,
                    process: Callable[[Dict[str, Any]], Any]) -> int:
    """Один цикл getUpdates → обработка. Возврат: новый offset.
    Ошибка одного апдейта не роняет остальные."""
    r = await client.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        params={"offset": offset, "timeout": _POLL_TIMEOUT,
                "allowed_updates": '["message","edited_message"]'})
    data = r.json() if r.status_code == 200 else {}
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates failed: {r.status_code} "
                           f"{str(data.get('description') or '')[:200]}")
    updates: List[Dict[str, Any]] = data.get("result") or []
    for upd in updates:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        try:
            await process(upd)
        except Exception:
            logger.warning("tg polling: update processing failed",
                           exc_info=True)
    return offset


async def run_polling_loop() -> None:
    """Фоновая задача: снимает вебхук и крутит getUpdates. Never-raise."""
    token = _bot_token()
    if not token:
        logger.warning("tg polling: TELEGRAM_POLLING=on, но токен бота не "
                       "задан (TELEGRAM_BOT_TOKEN) — поллер не запущен")
        return
    try:
        import httpx
        from backend.api.routes.messengers import process_telegram_update
    except Exception:
        logger.warning("tg polling: зависимости недоступны", exc_info=True)
        return

    offset = 0
    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT + 10) as client:
        # getUpdates и вебхук взаимоисключающи — снимаем вебхук с явным
        # предупреждением (на проде этот флаг должен быть выключен)
        try:
            await client.post(
                f"https://api.telegram.org/bot{token}/deleteWebhook",
                json={"drop_pending_updates": False})
            logger.warning("📥 tg polling: вебхук снят, апдейты забираются "
                           "long-polling'ом (локальный режим)")
        except Exception:
            logger.debug("tg polling: deleteWebhook failed", exc_info=True)

        while True:
            try:
                offset = await poll_once(client, token, offset,
                                         process_telegram_update)
            except asyncio.CancelledError:
                logger.info("tg polling: остановлен")
                return
            except Exception as e:
                logger.warning("tg polling: цикл упал (%s) — пауза %.0fс",
                               str(e)[:200], _ERROR_BACKOFF)
                try:
                    await asyncio.sleep(_ERROR_BACKOFF)
                except asyncio.CancelledError:
                    return


_task: Optional[asyncio.Task] = None


def start_polling_if_enabled() -> bool:
    """Запустить поллер фоновой задачей (идемпотентно). True — запущен."""
    global _task
    if not polling_enabled():
        return False
    if _task is not None and not _task.done():
        return True
    _task = asyncio.get_event_loop().create_task(run_polling_loop())
    return True
