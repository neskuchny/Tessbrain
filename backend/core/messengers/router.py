"""W30: route inbound messenger message → chat backend → reply text.

Это тонкая обёртка: webhook handler парсит платформо-специфичный payload
в нормализованный (`platform`, `external_id`, `text`, `username`),
вызывает `route_inbound_message(...)` — ответный текст возвращается
caller'у который сам отправляет его обратно через Telegram/Slack API.

Дизайн:
- /start <token> → redeem token, не вызываем чат, возвращаем
  приветственное сообщение
- любое другое сообщение → lookup link по external_id; если нет — отдаём
  "не привязан, /start <token> для линковки"
- если есть → вызываем chat-генератор (через context-injected callable —
  чтобы не хардкодить зависимость от LLMRouter в core)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from backend.core.messengers.links import MessengerLink, MessengerLinkService

logger = logging.getLogger(__name__)


# Тип: async fn(user_id, text) -> reply_text. Внедряется webhook'ом —
# в test'ах подменяется на stub, в prod — на LLM/chat pipeline.
ChatHandler = Callable[[str, str], Awaitable[str]]


@dataclass
class InboundReply:
    """То что webhook должен отправить обратно через messenger API."""
    reply_text: str
    linked_user_id: Optional[str] = None  # None если ещё не привязан
    is_onboarding: bool = False           # True если /start <token>


_HELP_TEMPLATE_KEY = "messenger.help_unlinked"
_LINKED_TEMPLATE_KEY = "messenger.linked_success"
_ERR_INVALID_TOKEN_KEY = "messenger.invalid_token"
_ASK_QUESTION_HINT_KEY = "messenger.ask_question_hint"


def _parse_start_command(text: str) -> Optional[str]:
    """`/start <token>` → token (или None)."""
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].lower() not in ("/start", "/start@bot"):
        return None
    return parts[1].strip() if len(parts) > 1 else None


async def route_inbound_message(
    *,
    platform: str,
    external_id: str,
    text: str,
    username: Optional[str] = None,
    link_service: MessengerLinkService,
    chat_handler: Optional[ChatHandler] = None,
) -> InboundReply:
    """Главный entry point inbound flow.

    Возвращает InboundReply.reply_text — caller отправляет в мессенджер.
    """
    from backend.core.i18n import t

    text = (text or "").strip()

    # 1. /start <token> — onboarding.
    token = _parse_start_command(text)
    if token is not None:
        link = await link_service.redeem_token(
            token=token,
            external_id=external_id,
            external_username=username,
        )
        if link is None:
            return InboundReply(
                reply_text=t(_ERR_INVALID_TOKEN_KEY),
                is_onboarding=True,
            )
        return InboundReply(
            reply_text=t(_LINKED_TEMPLATE_KEY, user_id=link.user_id[:8]),
            linked_user_id=link.user_id,
            is_onboarding=True,
        )

    # 2. Обычное сообщение — нужна привязка.
    link = await link_service.lookup_by_external(
        platform=platform, external_id=external_id,
    )
    if link is None:
        return InboundReply(reply_text=t(_HELP_TEMPLATE_KEY))

    await link_service.touch(link_id=link.id)

    # 3. Пустое сообщение / только команды без обработки → подсказка.
    if not text or text.startswith("/"):
        return InboundReply(
            reply_text=t(_ASK_QUESTION_HINT_KEY),
            linked_user_id=link.user_id,
        )

    # 3.5. Человек-в-цикле (§C): если для этого чата есть приостановленный
    # прогон доски, ждущий ответа — этот текст И ЕСТЬ ответ. Продолжаем прогон,
    # а не гоним сообщение в общий чат. Полностью за флагом ENABLE_BOARD_WAIT —
    # без него ветка не выполняется (find вернёт None только при наличии стора,
    # но мы даже не заходим), поведение прежнее.
    try:
        from backend.core.board.process_engine import board_wait_enabled
        if board_wait_enabled():
            from backend.core.board import board_wait_store as _bw
            waiting = _bw.find_waiting_for_chat(link.user_id, external_id)
            if waiting:
                from backend.core.board.process_engine import resume_board
                res = await resume_board(link.user_id, waiting.get("wait_id"), text)
                from backend.core.i18n import t as _t
                if res.get("status") == "waiting":
                    msg = _t("messenger.board_wait_next")
                elif res.get("status") == "success":
                    msg = _t("messenger.board_wait_resumed")
                else:
                    msg = _t("messenger.board_wait_failed")
                return InboundReply(reply_text=msg, linked_user_id=link.user_id)
    except Exception:
        logger.debug("board wait resume check skipped", exc_info=True)

    # 4. Делегируем чат handler'у (если внедрён). Без него — echo.
    if chat_handler is None:
        return InboundReply(
            reply_text=f"(echo) {text}",
            linked_user_id=link.user_id,
        )

    try:
        reply = await chat_handler(link.user_id, text)
    except Exception as exc:
        logger.warning("chat_handler failed for %s: %s", link.user_id[:8], exc)
        reply = t("messenger.chat_failed")
    return InboundReply(
        reply_text=reply or t("messenger.chat_empty_reply"),
        linked_user_id=link.user_id,
    )
