# -*- coding: utf-8 -*-
"""Переписки как источник мозга — API.

GET  /chat-sources            — источники (TG-группы, Slack-каналы)
POST /chat-sources/toggle     — включить/выключить источник
POST /chat-sources/digest     — буфер источника → KB-документ (в мозг)
POST /chat-sources/slack/sync — забрать историю Slack-каналов

Функция за флагом ENABLE_CHAT_INGEST (OFF): без него запись не ведётся,
список и уже накопленные буферы читать можно (честно показать состояние).
Auth: Bearer (trusted_user_id), как mark_research/client_sim.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from litestar import Router, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

logger = logging.getLogger(__name__)


def _caller(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        from backend.core.auth.service_token import trusted_user_id
        uid, src = trusted_user_id({"authorization": authorization}, "")
        if src != "unverified" and uid:
            return str(uid)
    except PermissionError:
        raise HTTPException(status_code=401, detail="token not authorized")
    except Exception:
        pass
    try:
        import jwt
        payload = jwt.decode(authorization[7:],
                             options={"verify_signature": False})
        sub = payload.get("sub") or payload.get("user_id")
        if sub:
            return str(sub)
    except Exception:
        pass
    raise HTTPException(status_code=401, detail="Not authenticated")


@get("/chat-sources")
async def chat_sources_list(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return {"status": "success", "enabled_flag": ci.ingest_enabled(),
            "sources": ci.list_sources(user_id)}


@post("/chat-sources/toggle")
async def chat_sources_toggle(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    ok = ci.set_source_enabled(user_id, str(data.get("key") or ""),
                               bool(data.get("enabled")))
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "источник не найден"})}


@post("/chat-sources/mode")
async def chat_sources_mode(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Режим источника: brain (в цифровой мозг) | context_only (только
    выбираемый контекст чата, в мозг не выгружается)."""
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    ok = ci.set_source_mode(user_id, str(data.get("key") or ""),
                            str(data.get("mode") or ""))
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "источник/режим не распознан"})}


@post("/chat-sources/files")
async def chat_sources_files(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Собирать ли вложения-документы этого источника в базу знаний
    (по умолчанию — да; выключение не трогает уже собранные файлы)."""
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    ok = ci.set_source_files(user_id, str(data.get("key") or ""),
                             bool(data.get("enabled")))
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "источник не найден"})}


@post("/chat-sources/digest")
async def chat_sources_digest(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return await ci.digest_to_brain(user_id, str(data.get("key") or ""))


@post("/chat-sources/slack/sync")
async def chat_sources_slack_sync(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return await ci.sync_slack(
        user_id, [str(c) for c in (data.get("channels") or [])])


@get("/chat-sources/messages")
async def chat_sources_messages(
    key: str = Parameter(query="key"),
    limit: int = Parameter(query="limit", default=100, required=False),
    offset: int = Parameter(query="offset", default=0, required=False),
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Просмотр буфера источника (хронологический порядок)."""
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return {"status": "success", **ci.read_buffer(user_id, key, limit=limit,
                                                  offset=offset)}


@post("/chat-sources/import")
async def chat_sources_import(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Ручная загрузка старой переписки: экспорт Telegram (result.json)
    или WhatsApp (.txt)."""
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return ci.import_dialog(user_id, title=str(data.get("title") or ""),
                            content=str(data.get("content") or ""))


@get("/chat-sources/participants")
async def chat_sources_participants(
    key: str = Parameter(query="key"),
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Участники + кандидаты сшивки с людьми из встреч (без подтверждения
    пользователем сшивка нигде не утверждается)."""
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return await ci.suggest_participant_matches(user_id, key)


@post("/chat-sources/confirm-identity")
async def chat_sources_confirm_identity(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    ok = ci.confirm_participant(
        user_id, str(data.get("key") or ""), str(data.get("author") or ""),
        str(data.get("person_id") or ""),
        str(data.get("person_name") or ""))
    return {"status": "success" if ok else "error",
            **({} if ok else {"message": "участник не найден"})}


@post("/chat-sources/analyze")
async def chat_sources_analyze(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Переписка → проверяемые наборы данных (каждый пункт с цитатой)."""
    from backend.core.messengers import chat_ingest as ci
    user_id = _caller(authorization)
    return await ci.analyze_source(user_id, str(data.get("key") or ""))


_TG_WEBHOOK_PATH = "/api/v1/webhooks/messengers/telegram"


@get("/chat-sources/telegram/webhook-info")
async def tg_webhook_info(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Диагностика «бот молчит»: куда реально смотрит вебхук бота.

    Telegram доставляет сообщения групп ТОЛЬКО на публичный webhook-URL —
    локальный сервер без туннеля бот не видит в принципе. Этот эндпоинт
    показывает getWebhookInfo глазами бота: url (пусто = вебхук не настроен),
    накопившиеся апдейты, последнюю ошибку доставки."""
    user_id = _caller(authorization)
    from backend.core.messengers.links import resolve_telegram_bot_token
    token = await resolve_telegram_bot_token(user_id)
    if not token:
        return {"status": "error",
                "message": ("токен бота не настроен — впишите его в "
                            "«Интеграции → Telegram» или TELEGRAM_BOT_TOKEN")}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"https://api.telegram.org/bot{token}/getWebhookInfo")
            data = (r.json() or {}).get("result") or {}
    except Exception as e:
        return {"status": "error", "message": f"Telegram API недоступен: {e}"}
    url = str(data.get("url") or "")
    hints = []
    if not url:
        hints.append("Вебхук НЕ настроен — Telegram никуда не шлёт "
                     "сообщения. Укажите публичный URL бэкенда ниже и "
                     "нажмите «Настроить вебхук».")
    elif _TG_WEBHOOK_PATH not in url:
        hints.append(f"Вебхук смотрит на «{url}» — это не путь этого "
                     f"бэкенда ({_TG_WEBHOOK_PATH}). Перенастройте.")
    if data.get("last_error_message"):
        hints.append(f"Последняя ошибка доставки: "
                     f"{data['last_error_message']}")
    from backend.core.messengers.chat_ingest import ingest_enabled
    if not ingest_enabled():
        hints.append("ENABLE_CHAT_INGEST выключен — даже при рабочем "
                     "вебхуке сбор групп не включится.")
    hints.append("Privacy mode бота должен быть выключен (@BotFather → "
                 "/setprivacy → Disable), иначе бот не видит обычные "
                 "сообщения группы (команды /brain_on видит всегда).")
    return {"status": "success", "url": url,
            "pending_update_count": data.get("pending_update_count", 0),
            "last_error_message": data.get("last_error_message") or "",
            "expected_path": _TG_WEBHOOK_PATH, "hints": hints}


@post("/chat-sources/telegram/setup-webhook")
async def tg_setup_webhook(
    data: dict[str, Any],
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Настроить вебхук бота на этот бэкенд: {base_url: "https://host"}.

    base_url обязан быть ПУБЛИЧНЫМ https (Telegram требует). Секрет берётся
    из настроек (telegram_webhook_secret), если задан."""
    user_id = _caller(authorization)
    base = str(data.get("base_url") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        return {"status": "error",
                "message": ("нужен публичный https-URL бэкенда (Telegram "
                            "не принимает http и не достучится до "
                            "localhost — используйте туннель, например "
                            "cloudflared/ngrok, или прод-хост)")}
    from backend.core.messengers.links import resolve_telegram_bot_token
    token = await resolve_telegram_bot_token(user_id)
    if not token:
        return {"status": "error", "message": "токен бота не настроен"}
    payload: dict[str, Any] = {
        "url": f"{base}{_TG_WEBHOOK_PATH}",
        "allowed_updates": ["message", "edited_message"],
    }
    try:
        from backend.config import get_settings
        secret = (getattr(get_settings(), "telegram_webhook_secret", "")
                  or "").strip()
        if secret:
            payload["secret_token"] = secret
    except Exception:
        pass
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json=payload)
            res = r.json() or {}
    except Exception as e:
        return {"status": "error", "message": f"Telegram API недоступен: {e}"}
    if not res.get("ok"):
        return {"status": "error",
                "message": f"Telegram отказал: {res.get('description')}"}
    return {"status": "success", "url": payload["url"],
            "message": ("Вебхук настроен. Теперь в группе с ботом отправьте "
                        "/brain_on — бот должен ответить.")}


@get("/chat-sources/failures")
async def chat_sources_failures(
    limit: int = 100,
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Что не доехало при сборе переписок — и почему.

    Сбой сети или лимит провайдера раньше терялся молча: в мозге просто
    чего-то не оказывалось. Здесь видно, какой источник и на какой стадии
    не прошёл даже после повторов."""
    from backend.core.ingest.resilience import failures_summary, list_failures
    user_id = _caller(authorization)
    return {
        "status": "success",
        "failures": list_failures(user_id, limit=limit),
        "summary": failures_summary(user_id),
        "note": ("Запись здесь означает, что данные не попали в память после "
                 "всех повторов. Перезапустите сбор источника вручную — "
                 "пропущенное дотянется."),
    }


@post("/chat-sources/failures/clear")
async def chat_sources_failures_clear(
    authorization: Optional[str] = Parameter(header="Authorization",
                                             default=None),
) -> dict[str, Any]:
    """Очистить журнал после разбора. Данные это не восстанавливает —
    только убирает отметки о сбоях."""
    from backend.core.ingest.resilience import clear_failures
    user_id = _caller(authorization)
    return {"status": "success", "cleared": clear_failures(user_id)}


router = Router(path="", route_handlers=[
    chat_sources_list, chat_sources_toggle, chat_sources_mode,
    chat_sources_files,
    chat_sources_digest, chat_sources_slack_sync, chat_sources_messages,
    chat_sources_import, chat_sources_participants,
    chat_sources_confirm_identity, chat_sources_analyze,
    chat_sources_failures, chat_sources_failures_clear,
    tg_webhook_info, tg_setup_webhook,
], tags=["Chat Sources"])
