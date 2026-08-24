# -*- coding: utf-8 -*-
"""W30: Messenger inbound gateway routes.

Endpoints:
- ``POST /api/v1/webhooks/messengers/telegram``  — Telegram webhook
- ``POST /api/v1/webhooks/messengers/slack``     — Slack Events API webhook
- ``POST /api/v1/messengers/{platform}/onboarding/start`` (auth) — issue
  one-shot token + return deep-link для UI кнопки "Connect"
- ``GET  /api/v1/messengers/links`` (auth) — listing connected accounts
- ``DELETE /api/v1/messengers/links/{id}`` (auth) — revoke
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from litestar import Request, Router, delete, get, post
from litestar.exceptions import HTTPException
from litestar.params import Parameter

from backend.core.messengers.chat_handler import build_messenger_chat_handler
from backend.core.messengers.links import (
    SUPPORTED_PLATFORMS,
    MessengerLinkService,
)
from backend.core.messengers.router import route_inbound_message
from backend.core.messengers.verify import (
    verify_slack_signature,
    verify_telegram_secret,
)
from backend.core.observability import audit_log

logger = logging.getLogger(__name__)


def _decode_jwt_unsafe(token: str) -> dict[str, Any]:
    # issue #107 T-1: verify-first (strict → {} на плохой подписи → 401;
    # compat по умолчанию → decode без проверки, как раньше).
    from backend.core.auth.service_token import decode_claims_guarded
    return decode_claims_guarded(token)


def _extract_user(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        claims = _decode_jwt_unsafe(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub/user_id")
    return user_id


async def _build_service() -> MessengerLinkService:
    postgres = None
    try:
        from backend.db.postgres import get_postgres
        postgres = await get_postgres()
    except Exception:
        postgres = None
    return MessengerLinkService(postgres=postgres)


# === Onboarding (UI-facing) ============================================


@post("/messengers/{platform:str}/onboarding/start")
async def start_onboarding(
    platform: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    """Issue one-shot token + return deep-link для frontend кнопки.

    Frontend отрисует "Connect Telegram" → href = response["deeplink"].
    """
    user_id = _extract_user(authorization)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform}")

    svc = await _build_service()
    rec = await svc.issue_token(user_id=user_id, platform=platform)
    deeplink = await _build_deeplink(platform, rec.token)

    await audit_log.emit(
        action="messenger.onboarding.start",
        user_id=user_id,
        resource=f"messenger:{platform}",
        metadata={"token_prefix": rec.token[:6]},
    )
    out: dict[str, Any] = {
        "token": rec.token,
        "platform": platform,
        "expires_at": rec.expires_at,
        "deeplink": deeplink,
        "instructions": _instructions_for(platform),
    }
    # Явная причина, если deep-link не собрался (телеграм без токена/имени бота):
    # фронт покажет её вместо общей заглушки, и станет понятно ЧТО настроить.
    if platform == "telegram" and not deeplink:
        out["reason"] = ("Платформенный бот не настроен на сервере: задайте "
                         "TELEGRAM_BOT_TOKEN (и, по желанию, TELEGRAM_BOT_USERNAME) "
                         "в окружении бэкенда. Либо добавьте контакт вручную ниже.")
    return out


_cached_bot_username: Optional[str] = None
_bot_username_checked: bool = False  # негатив-кэш: getMe уже пробовали и не вышло


async def _telegram_bot_username() -> Optional[str]:
    """Юзернейм платформенного бота для deep-link t.me/<bot>?start=...

    Приоритет: settings.telegram_bot_username → getMe по токену бота
    (settings.telegram_bot_token / env TELEGRAM_BOT_TOKEN), с кэшем. Раньше
    без явно заданного username deep-link был None, и кнопка «Подключить
    Telegram» открывала не бота, а общий telegram — привязка не работала.

    Негатив-кэш: если getMe уже не дал имя (нет токена/сеть/бот невалиден),
    больше НЕ ходим по сети на каждый клик (иначе каждый вызов висел до 8с)."""
    global _cached_bot_username, _bot_username_checked
    from backend.config import get_settings
    s = get_settings()
    name = (getattr(s, "telegram_bot_username", "") or "").lstrip("@").strip()
    if name:
        return name
    if _cached_bot_username:
        return _cached_bot_username
    if _bot_username_checked:
        return None  # уже проверяли — не блокируем запрос повторным getMe
    token = ((getattr(s, "telegram_bot_token", "") or "").strip()
             or os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    if not token:
        _bot_username_checked = True
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        if r.status_code == 200:
            un = ((r.json() or {}).get("result") or {}).get("username")
            if un:
                _cached_bot_username = str(un).lstrip("@")
                return _cached_bot_username
    except Exception:
        logger.debug("getMe for bot username failed", exc_info=True)
    _bot_username_checked = True  # не долбим getMe на каждый клик
    return None


async def _build_deeplink(platform: str, token: str) -> Optional[str]:
    if platform == "telegram":
        # .strip() до guard: пробельный username не должен собрать битую ссылку
        bot = (await _telegram_bot_username() or "").strip()
        if not bot:
            return None
        return f"https://t.me/{bot}?start={token}"
    # Slack / WhatsApp: используют slash-command или просто paste; deep-link
    # в product-UI отображается как inline-инструкция.
    return None


def _instructions_for(platform: str) -> str:
    if platform == "telegram":
        return (
            "Нажми кнопку ниже — откроется бот. "
            "Жми Start, и аккаунт привяжется автоматически."
        )
    if platform == "slack":
        return (
            "В Slack-канале с ботом отправь команду:\n"
            "/tessent connect <token>"
        )
    return "Передай боту команду: /start <token>"


@get("/messengers/links")
async def list_links(
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    svc = await _build_service()
    links = await svc.list_for_user(user_id=user_id)
    return {"items": [link.to_dict() for link in links]}


@delete("/messengers/links/{link_id:str}", status_code=200)
async def revoke_link(
    link_id: str,
    authorization: Optional[str] = Parameter(header="Authorization", default=None),
) -> dict[str, Any]:
    user_id = _extract_user(authorization)
    svc = await _build_service()
    # Проверяем принадлежность.
    user_links = await svc.list_for_user(user_id=user_id)
    if not any(l.id == link_id for l in user_links):
        raise HTTPException(status_code=404, detail="link not found")
    ok = await svc.deactivate(link_id=link_id)
    await audit_log.emit(
        action="messenger.link.revoke",
        user_id=user_id,
        resource=f"messenger_link:{link_id}",
    )
    return {"revoked": ok, "link_id": link_id}


# === Webhooks (public, signature-verified) =============================


@post("/webhooks/messengers/telegram", status_code=200)
async def telegram_webhook(
    request: Request,
    secret_token: Optional[str] = Parameter(
        header="X-Telegram-Bot-Api-Secret-Token", default=None,
    ),
) -> dict[str, Any]:
    """Telegram webhook: верифицируем secret_token, нормализуем update,
    роутим в messenger.router и (если есть BOT_TOKEN) шлём ответ обратно.
    """
    from backend.config import get_settings
    s = get_settings()
    if not verify_telegram_secret(
        expected_secret=getattr(s, "telegram_webhook_secret", "") or None,
        received_secret=secret_token,
    ):
        raise HTTPException(status_code=401, detail="Bad secret token")

    payload = await request.json()
    return await process_telegram_update(payload)


async def process_telegram_update(payload: dict) -> dict[str, Any]:
    """Общая обработка одного Telegram-update.

    Вызывается двумя транспортами: вебхуком (прод, публичный https) и
    long-polling-поллером (локальная разработка, TELEGRAM_POLLING=on) —
    логика одна, различается только доставка апдейтов."""
    from backend.config import get_settings
    s = get_settings()
    message = payload.get("message") or payload.get("edited_message") or {}
    chat = message.get("chat") or {}
    text = message.get("text") or ""
    external_id = str(chat.get("id") or "")
    username = (chat.get("username") or "").strip() or None

    if not external_id:
        return {"ok": True, "skipped": "no_chat_id"}

    # Группа/супергруппа — это НЕ диалог с ботом, а источник переписки
    # (chat_ingest): бот молчит, кроме ответов на /brain_on|/brain_off.
    # Приватность: пока /brain_on не прозвучал — ничего не сохраняется.
    if str(chat.get("type") or "") in ("group", "supergroup"):
        try:
            from backend.core.messengers.chat_ingest import (
                handle_telegram_group_update,
            )
            group_reply = await handle_telegram_group_update(message)
        except Exception:
            logger.warning("chat_ingest: group update failed", exc_info=True)
            group_reply = None
        if group_reply:
            bot_token = (getattr(s, "telegram_bot_token", "") or "").strip()
            if bot_token:
                await _send_telegram(bot_token, external_id, group_reply)
        return {"ok": True, "group": True}

    # Phase A.4: voice/audio handler. Если в сообщении нет text но есть
    # voice/audio/video_note — транскрибируем через Gemini Audio и
    # подставляем как text. Никнейм/external_id остаются прежние, дальше
    # идёт обычный inbound flow.
    if not text and (message.get("voice") or message.get("audio") or message.get("video_note")):
        bot_token_for_dl = (getattr(s, "telegram_bot_token", "") or "").strip()
        if bot_token_for_dl:
            try:
                from backend.core.messengers.voice_handler import transcribe_telegram_voice
                transcribed = await transcribe_telegram_voice(bot_token_for_dl, message)
                if transcribed:
                    text = transcribed
                    # Лог даст знать что текст пришёл из голоса — для аудита.
                    logger.info(
                        "telegram_webhook: voice transcribed for chat=%s (%d chars)",
                        external_id, len(transcribed),
                    )
                else:
                    # Fallback: даём юзеру понятный ответ через обычный flow.
                    text = "🎤 Не удалось расшифровать голосовое сообщение. Попробуй текстом или повтори в более тихом месте."
            except Exception as exc:
                logger.warning("telegram_webhook: voice handler failed: %s", exc)
                text = "🎤 Не удалось расшифровать голосовое сообщение."

    svc = await _build_service()
    chat_handler = await build_messenger_chat_handler()
    reply = await route_inbound_message(
        platform="telegram",
        external_id=external_id,
        text=text,
        username=username,
        link_service=svc,
        chat_handler=chat_handler,
    )

    bot_token = (getattr(s, "telegram_bot_token", "") or "").strip()
    sent = False
    if bot_token:
        sent = await _send_telegram(bot_token, external_id, reply.reply_text)

    if reply.linked_user_id:
        from backend.core.observability import metrics as _m
        _m.record_messenger_event(
            platform="telegram",
            event="onboarding" if reply.is_onboarding else "inbound",
        )
        if bot_token:
            _m.record_messenger_event(
                platform="telegram",
                event="reply_sent" if sent else "reply_failed",
            )
        await audit_log.emit(
            action=("messenger.linked" if reply.is_onboarding else "messenger.message_in"),
            user_id=reply.linked_user_id,
            resource=f"messenger:telegram:{external_id}",
            metadata={"text_len": len(text), "sent": sent},
        )
    return {"ok": True, "sent": sent}


@post("/webhooks/messengers/slack", status_code=200)
async def slack_webhook(
    request: Request,
    timestamp: Optional[str] = Parameter(
        header="X-Slack-Request-Timestamp", default=None,
    ),
    signature: Optional[str] = Parameter(
        header="X-Slack-Signature", default=None,
    ),
) -> dict[str, Any]:
    """Slack Events API endpoint. Поддерживает url_verification challenge."""
    from backend.config import get_settings
    s = get_settings()
    body = await request.body()

    if not verify_slack_signature(
        signing_secret=getattr(s, "slack_signing_secret", "") or None,
        timestamp=timestamp,
        body=body,
        received_signature=signature,
    ):
        raise HTTPException(status_code=401, detail="Bad signature")

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        raise HTTPException(status_code=400, detail="Bad JSON")

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    event = payload.get("event") or {}
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return {"ok": True, "skipped": "bot_message"}

    text = event.get("text") or ""
    external_id = event.get("user") or ""
    if not external_id:
        return {"ok": True, "skipped": "no_user"}
    # Канал — куда отвечать; пользователь — кто спрашивает. Это РАЗНЫЕ поля:
    # личность берём из user (по ней ищется привязка к сотруднику), адрес
    # ответа — из channel. Ответ уходит веткой под вопросом, чтобы не
    # засорять общий канал.
    channel = event.get("channel") or ""
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "")

    svc = await _build_service()
    chat_handler = await build_messenger_chat_handler()
    reply = await route_inbound_message(
        platform="slack",
        external_id=external_id,
        text=text,
        link_service=svc,
        chat_handler=chat_handler,
    )

    bot_token = (getattr(s, "slack_bot_token", "") or "").strip()
    sent = False
    if bot_token and channel:
        sent = await _send_slack(bot_token, channel, reply.reply_text,
                                 thread_ts=thread_ts)

    if reply.linked_user_id:
        from backend.core.observability import metrics as _m
        _m.record_messenger_event(
            platform="slack",
            event="onboarding" if reply.is_onboarding else "inbound",
        )
        if bot_token:
            _m.record_messenger_event(
                platform="slack",
                event="reply_sent" if sent else "reply_failed",
            )
        await audit_log.emit(
            action=("messenger.linked" if reply.is_onboarding else "messenger.message_in"),
            user_id=reply.linked_user_id,
            resource=f"messenger:slack:{external_id}",
            metadata={"text_len": len(text), "sent": sent},
        )
    return {"ok": True, "sent": sent, "reply_text": reply.reply_text}


# === Outbound helper (Telegram) ========================================


async def _send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Ответ чата уходит с markdown-разметкой (## заголовки, **жирный**,
    таблицы) — конвертируем в Telegram-HTML с фолбэком в чистый текст,
    иначе пользователь видит сырые ** и |---| (жалоба «нет форматирования»)."""
    try:
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        res = await post_telegram_text(bot_token, chat_id, text, timeout=10.0)
        return bool(res["ok"])
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False


async def _send_slack(bot_token: str, channel: str, text: str,
                      *, thread_ts: str = "") -> bool:
    """Ответ в Slack: markdown → mrkdwn (иначе видны сырые звёздочки)."""
    try:
        from backend.core.notifications.slack_format import post_slack_text
        res = await post_slack_text(bot_token, channel, text,
                                    timeout=10.0, thread_ts=thread_ts)
        if not res["ok"] and res.get("error"):
            logger.warning("slack send failed: %s", res["error"])
        return bool(res["ok"])
    except Exception as exc:
        logger.warning("slack send failed: %s", exc)
        return False


router = Router(
    path="",
    route_handlers=[
        start_onboarding,
        list_links,
        revoke_link,
        telegram_webhook,
        slack_webhook,
    ],
    tags=["Messengers"],
)
