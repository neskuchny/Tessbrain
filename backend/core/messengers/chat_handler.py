"""W34: production chat_handler для messenger inbound.

Подключает `route_inbound_message` к LLM pipeline. Каждое сообщение
от привязанного пользователя в Telegram/Slack идёт в `LLMRouter.generate`
с system prompt'ом для бота.

Дизайн:
- Lazy-инициализация LLMRouter — webhook handler'у не надо ждать его
  при холодном старте.
- Best-effort: при недоступности LLM возвращаем graceful fallback,
  не raises (route_inbound_message сам catch'ит, но мы перестраховываемся).
- Длина ответа клампится (Telegram cap 4096, Slack chat.postMessage
  блочно режет ~3000) — берём 3500 как безопасный потолок.
- Никакого RAG/KAG/sessions сейчас — это полноценная chat_completions
  ветка из W11+, для мессенджера слишком тяжело (5-30с) и не подходит
  под UX чата. Простой Q&A с системным промптом — производит ответ
  за 1-3с.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# Reply length guard — Telegram cap 4096, оставляем запас для emoji/спецсимволов.
DEFAULT_MAX_REPLY_CHARS = 3500


# Загружается lazily из i18n catalog в _resolve_system_prompt().
# Старый SYSTEM_PROMPT здесь оставляем как default fallback для совместимости
# с местами где импортировали константу до W43.
SYSTEM_PROMPT = (
    "Ты — Tessbrain, корпоративный AI-ассистент компании. "
    "Отвечай кратко (≤ 5 предложений), на русском языке, по существу. "
    "Если вопрос требует доступа к внутренним данным компании которые "
    "ты не знаешь — честно скажи 'нужно посмотреть в системе' и предложи "
    "открыть веб-UI для подробного ответа. Не выдумывай факты."
)


def _resolve_system_prompt(default: str) -> str:
    """W43: load system prompt from i18n catalog using current request locale."""
    try:
        from backend.core.i18n import t
        return t("messenger.system_prompt")
    except Exception:
        return default


# Тип возвращаемого callable'а — совместим с messengers.router.ChatHandler.
ChatHandler = Callable[[str, str], Awaitable[str]]


class MessengerChatHandler:
    """Wrapper над LLMRouter с messenger-specific поведением."""

    def __init__(
        self,
        *,
        llm_router: Any = None,
        max_reply_chars: int = DEFAULT_MAX_REPLY_CHARS,
        system_prompt: str = SYSTEM_PROMPT,
        temperature: float = 0.5,
        max_tokens: int = 1500,
    ) -> None:
        self._router = llm_router
        self.max_reply_chars = max(200, int(max_reply_chars))
        self.system_prompt = system_prompt
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)

    async def __call__(self, user_id: str, text: str) -> str:
        import time as _time

        from backend.core.i18n import t as _t
        from backend.core.observability import metrics as _m

        text = (text or "").strip()
        if not text:
            return _t("messenger.chat_empty_question")
        router = await self._get_router()
        if router is None:
            _m.record_messenger_chat(
                platform="unknown", outcome="unavailable", duration_s=0.0,
            )
            return _t("messenger.chat_unavailable")
        # System prompt resolves per-request locale (i18n catalog).
        system_prompt = _resolve_system_prompt(self.system_prompt)
        # Дно воронки визуальных отчётов: если пользователю недавно доставили
        # визуальный отчёт — его содержание подмешивается контекстом, и вопрос
        # «что за динамит у отдела продаж?» понимается без уточнений.
        # Best-effort: нет свежего отчёта → промпт прежний байт-в-байт.
        prompt = text
        try:
            from backend.core.board.report_context import context_block
            _rc = context_block(user_id)
            if _rc:
                prompt = f"{_rc}\n\nВопрос пользователя: {text}"
        except Exception:
            logger.debug("report context inject skipped", exc_info=True)
        # Память компании в мессенджере. Раньше бот ядра отвечал БЕЗ
        # обращения к графу и предлагал открыть веб — из мессенджера мозг
        # был недоступен. Полный конвейер чата сюда не тянем (5-30с против
        # ожидаемых 1-3с в мессенджере) — берём лёгкий слой: BM25-индекс
        # знаний пользователя, top-6 фрагментов, с жёстким лимитом времени.
        # Best-effort: индекса нет / не успели — промпт прежний, бот честно
        # предложит веб. Выключение: MESSENGER_MEMORY_SEARCH=off.
        try:
            import os as _os
            if _os.getenv("MESSENGER_MEMORY_SEARCH", "on").strip().lower() in (
                    "1", "on", "true", "yes"):
                mem = await self._memory_context(user_id, text)
                if mem:
                    prompt = (
                        f"=== ИЗ ПАМЯТИ КОМПАНИИ (по теме вопроса) ===\n{mem}\n"
                        f"Опирайся на эти фрагменты, если они отвечают на "
                        f"вопрос; не выдумывай сверх них.\n\n"
                        f"{prompt}")
        except Exception:
            logger.debug("memory context inject skipped", exc_info=True)
        t_start = _time.monotonic()
        try:
            reply = await router.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            logger.warning("MessengerChatHandler.generate failed: %s", exc)
            _m.record_messenger_chat(
                platform="unknown", outcome="failure",
                duration_s=_time.monotonic() - t_start,
            )
            return _t("messenger.chat_failed")
        _m.record_messenger_chat(
            platform="unknown", outcome="success",
            duration_s=_time.monotonic() - t_start,
        )
        return self._clamp(reply or _t("messenger.chat_empty_reply"))

    async def _memory_context(self, user_id: str, question: str,
                              *, top_k: int = 6,
                              timeout_s: float = 3.0) -> str:
        """Top-k фрагментов из памяти компании для вопроса. Never-raises.

        Гибридный поиск (если движки уже собраны) с BM25-фолбэком; бюджет
        времени прежний: мессенджер ждать не должен. Пусто/не успели → "".
        """
        import asyncio

        async def _search() -> str:
            # Раньше — голый BM25 (один канал из трёх): бот терял участников
            # на вопросах-перечислениях, которые brain-чат находил
            # (SEARCH_CONSUMERS_AUDIT, правка №3). Теперь гибрид, но БЕЗ
            # оплаты сборки движков из бюджета мессенджера:
            # build_if_missing=False — гибрид только если уже собран, иначе
            # BM25 сейчас и фоновая сборка к следующему сообщению.
            from backend.core.search.context_fragments import topic_fragments
            frags, _engine = await topic_fragments(
                user_id, question, top_k=top_k, build_if_missing=False)
            return "\n---\n".join(frags)

        try:
            return await asyncio.wait_for(_search(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.debug("memory context: не успели за %.1fс — без памяти",
                         timeout_s)
            return ""
        except Exception:
            logger.debug("memory context failed", exc_info=True)
            return ""

    async def _get_router(self) -> Any:
        if self._router is not None:
            return self._router
        try:
            from backend.core.llm.router import get_llm_router
            self._router = get_llm_router()
        except Exception as exc:
            logger.debug("LLMRouter import failed: %s", exc)
            self._router = None
        return self._router

    def _clamp(self, text: str) -> str:
        text = str(text)
        if len(text) <= self.max_reply_chars:
            return text
        return text[: self.max_reply_chars - 1] + "…"


async def build_messenger_chat_handler() -> Optional[ChatHandler]:
    """Production builder — webhook'и зовут это раз за request.

    Возвращает callable вида `(user_id, text) -> reply_text` совместимый
    с `route_inbound_message(chat_handler=...)`.
    """
    handler = MessengerChatHandler()
    return handler.__call__


__all__ = [
    "DEFAULT_MAX_REPLY_CHARS",
    "SYSTEM_PROMPT",
    "MessengerChatHandler",
    "build_messenger_chat_handler",
]
