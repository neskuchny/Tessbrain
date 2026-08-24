"""Delivery routing — куда отправить готовый артефакт.

Phase B Coffee scenario, шаг 3. После того как RolePipeline сгенерировал
артефакт, нужно решить КУДА его отправить:
- Telegram pinned message с preview (быстро, в дороге)
- Email черновик (для последующей отправки или сразу через Gmail integration)
- Notion / Confluence страница (для долгосрочного reference)
- Cursor/Claude Code workspace (если артефакт — ТЗ для AI-исполнителя)

Решение принимается на основе:
1. RolePipelineOutput.recommended_channels — pipeline сам подсказывает
2. Persona.behavioral.channel_preferences — что юзер реально предпочитает
3. Доступные интеграции юзера (IntegrationRegistry)

Контракт:
- pick_channels(output, persona, available_integrations) → ordered list[str]
  Сортирует по приоритету: что подсказал pipeline + чем юзер пользуется
- deliver_artifact(output, channel, user_id, ...) → delivery_result
  Реально отправляет через соответствующую интеграцию

Best-effort: если канал недоступен (нет токена, integration disabled) —
пишем в лог и пробуем следующий. Если все упали — оставляем артефакт как
"pending_delivery" для UI/manual pickup.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DeliveryChannel(str, enum.Enum):
    """Доступные каналы доставки."""

    TELEGRAM = "telegram"
    EMAIL = "email"
    EMAIL_DRAFT = "email_draft"  # просто draft в DB, не отправляем
    SLACK = "slack"
    NOTION = "notion"
    CONFLUENCE = "confluence"
    CLAUDE_CODE = "claude_code"  # отправить в Claude Code workspace
    CURSOR = "cursor"             # Cursor workspace
    WEBHOOK = "webhook"           # generic webhook
    PENDING = "pending"            # ничего не отправлено, ждём manual pickup


@dataclass
class DeliveryRecipient:
    """Куда конкретно доставить (адрес/идентификатор внутри канала)."""

    user_id: str
    channel: DeliveryChannel
    address: str
    # ↑ для telegram — chat_id, для email — emails, для notion — page_id,
    # для claude_code/cursor — workspace path/repo URL


@dataclass
class DeliveryResult:
    success: bool
    channel: DeliveryChannel
    delivered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    external_ref: Optional[str] = None  # ID отправленного сообщения / draft / pageId
    error_message: str = ""


# ─────────────────────────────────────────────────────────────────
# pick_channels: выбор каналов
# ─────────────────────────────────────────────────────────────────


def pick_channels(
    *,
    pipeline_output: Any,  # RolePipelineOutput
    persona: Optional[Any] = None,
    available_integrations: Optional[list[str]] = None,
    channel_engagement: Optional[dict[str, float]] = None,
) -> list[str]:
    """Вернуть упорядоченный список каналов: первый = главный, остальные fallback.

    Алгоритм:
    1. Берём `pipeline_output.recommended_channels` (что подсказал pipeline)
    2. Сортируем согласно Persona.behavioral.channel_preferences
       (первые предпочтения юзера — наверх списка)
    3. Phase H: если задан `channel_engagement` (выученный из feedback) —
       reorder так, чтобы каналы где юзер РЕАЛЬНО реагирует были первыми.
       Каналы без данных сохраняют относительный порядок (neutral=0).
    4. Фильтруем по available_integrations (если задано)
    5. Если результат пустой — fallback на ["telegram", "pending"]
    """
    recommended = list(getattr(pipeline_output, "recommended_channels", []) or [])
    if not recommended:
        recommended = ["telegram"]

    persona_prefs: list[str] = []
    if persona is not None:
        try:
            persona_prefs = [
                c.value if hasattr(c, "value") else str(c)
                for c in (persona.behavioral.channel_preferences or [])
            ]
        except Exception:
            persona_prefs = []

    # Сортируем recommended так, чтобы любимые юзером каналы были первыми
    def _prefer_rank(ch: str) -> int:
        if ch in persona_prefs:
            return persona_prefs.index(ch)
        return len(persona_prefs) + 1

    ordered = sorted(set(recommended), key=_prefer_rank)

    # Phase H: reorder по выученному engagement. Stable sort по -engagement:
    # каналы с положительным engagement → вверх, с отрицательным → вниз,
    # без данных (0.0) сохраняют относительный порядок (Python sort stable).
    if channel_engagement:
        ordered.sort(key=lambda ch: -channel_engagement.get(ch, 0.0))

    # Filter по available_integrations если задано
    if available_integrations is not None:
        avail = set(available_integrations)
        ordered = [c for c in ordered if c in avail or c == "telegram"]
        # telegram всегда оставляем как universal fallback

    if not ordered:
        ordered = ["telegram", "pending"]

    return ordered


# ─────────────────────────────────────────────────────────────────
# deliver_artifact: реальная отправка
# ─────────────────────────────────────────────────────────────────


async def deliver_artifact(
    *,
    pipeline_output: Any,
    channel: str,
    user_id: str,
    recipient_address: Optional[str] = None,
) -> DeliveryResult:
    """Отправить артефакт по заданному каналу. Best-effort.

    Args:
        pipeline_output: RolePipelineOutput с content_markdown
        channel: один из DeliveryChannel.value
        user_id: для lookup'а адреса через MessengerLinks/integrations
        recipient_address: явный override адреса (опционально)

    Returns:
        DeliveryResult с success флагом и external_ref для tracking.
    """
    ch = channel.lower()
    title = getattr(pipeline_output, "title", "Артефакт")
    body = getattr(pipeline_output, "content_markdown", "")

    # ─── TELEGRAM ───
    if ch == DeliveryChannel.TELEGRAM.value:
        return await _deliver_telegram(
            user_id=user_id,
            title=title,
            body=body,
            address=recipient_address,
        )

    # ─── EMAIL_DRAFT ───
    if ch == DeliveryChannel.EMAIL_DRAFT.value:
        # Phase B.2: реальный draft в Gmail (если интеграция подключена),
        # иначе fallback на «pending» — UI покажет draft для ручной отправки.
        return await _deliver_email_draft(
            user_id=user_id,
            pipeline_output=pipeline_output,
            recipient_address=recipient_address,
        )

    # ─── EMAIL ───
    if ch == DeliveryChannel.EMAIL.value:
        return await _deliver_email(
            user_id=user_id,
            pipeline_output=pipeline_output,
            recipient_address=recipient_address,
        )

    # ─── NOTION ───
    if ch == DeliveryChannel.NOTION.value:
        return await _deliver_notion(
            user_id=user_id,
            title=title,
            body=body,
        )

    # ─── CLAUDE_CODE / CURSOR (через executor) ───
    if ch in {DeliveryChannel.CLAUDE_CODE.value, DeliveryChannel.CURSOR.value}:
        # Audit-фикс (F2-N): уважаем pipeline_output.recommended_executor, если
        # задан — раньше executor_name выбирался ТОЛЬКО по каналу, а
        # recommended_executor был dead input. Канал даёт дефолт.
        default_executor = "claude_code_cli" if ch == "claude_code" else "cursor_cli"
        recommended = getattr(pipeline_output, "recommended_executor", None)
        return await _deliver_to_executor(
            user_id=user_id,
            pipeline_output=pipeline_output,
            executor_name=str(recommended) if recommended else default_executor,
            channel=DeliveryChannel(ch),
        )

    # ─── PENDING (fallback) ───
    logger.info(
        "[coffee.delivery] no channel matched %r — saving as pending for user=%s",
        ch, user_id,
    )
    return DeliveryResult(
        success=True,
        channel=DeliveryChannel.PENDING,
        external_ref=None,
    )


async def _deliver_telegram(
    *,
    user_id: str,
    title: str,
    body: str,
    address: Optional[str],
) -> DeliveryResult:
    """Отправить артефакт в Telegram через привязанный chat_id."""
    try:
        # Токен тем же резолвером, что остальная доставка: платформенный →
        # BYO-токен пользователя из «Интеграций»
        from backend.core.messengers.links import resolve_telegram_bot_token
        bot_token = await resolve_telegram_bot_token(user_id)
        if not bot_token:
            return DeliveryResult(
                success=False,
                channel=DeliveryChannel.TELEGRAM,
                error_message="telegram_bot_token не задан",
            )

        # Общий резолвер: привязка бота (messenger_links) → Default Chat ID
        # из «Интеграций». Раньше тут был только первый шаг вручную — у
        # пользователей без привязки «Подключить» (а Chat ID вписан руками)
        # доставка честно отвечала «no telegram chat_id linked», хотя система
        # знала, куда слать. Тот же класс бага, что /brain_on до интеграций.
        chat_id = address
        if not chat_id:
            try:
                from backend.core.messengers.links import (
                    resolve_telegram_chat_id,
                )
                chat_id = await resolve_telegram_chat_id(user_id)
            except Exception as exc:
                logger.debug("telegram chat_id resolve failed: %s", exc)

        if not chat_id:
            return DeliveryResult(
                success=False,
                channel=DeliveryChannel.TELEGRAM,
                error_message="no telegram chat_id linked для user_id",
            )

        # parse_mode="Markdown" ломался на непарной */_ в тексте LLM (400 →
        # сообщение терялось), а **жирный**/таблицы всё равно не рендерил.
        # Общий хелпер: markdown → HTML, фолбэк в чистый текст.
        from backend.core.notifications.telegram_format import (
            post_telegram_text,
        )
        res = await post_telegram_text(
            bot_token, chat_id, f"**📌 {title}**\n\n{body[:3500]}",
            timeout=30.0)
        if not res["ok"]:
            return DeliveryResult(
                success=False,
                channel=DeliveryChannel.TELEGRAM,
                error_message="telegram sendMessage failed",
            )
        msg_id = res.get("message_id")
        return DeliveryResult(
            success=True,
            channel=DeliveryChannel.TELEGRAM,
            external_ref=str(msg_id) if msg_id else None,
        )
    except Exception as exc:
        logger.warning("telegram delivery failed: %s", exc)
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.TELEGRAM,
            error_message=str(exc),
        )


def _extract_email_payload(pipeline_output: Any) -> tuple[str, str]:
    """Достать (subject, body) из RolePipelineOutput.

    SalesPipeline кладёт subject/body в content_structured. Для остальных
    pipelines fallback на title + content_markdown.
    """
    structured = getattr(pipeline_output, "content_structured", None) or {}
    if isinstance(structured, dict):
        subj = structured.get("subject") or ""
        body = structured.get("body") or ""
        if subj and body:
            return subj, body
    title = getattr(pipeline_output, "title", "") or "Артефакт встречи"
    body = getattr(pipeline_output, "content_markdown", "") or ""
    return title, body


async def _resolve_email_address(user_id: str, override: Optional[str]) -> Optional[str]:
    """Найти email-адрес. override → user_integrations.email_address → None."""
    if override and "@" in override:
        return override.strip()
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            r = await session.execute(
                text(
                    "SELECT email FROM public.users "
                    "WHERE id = CAST(:uid AS UUID) LIMIT 1"
                ),
                {"uid": user_id},
            )
            row = r.first()
            if row and row[0]:
                return str(row[0]).strip()
    except Exception as exc:
        logger.debug("email lookup failed for %s: %s", user_id, exc)
    return None


async def _load_gmail_for_user(user_id: str) -> Optional[Any]:
    """Поднять GmailIntegration с креденшалами юзера. None если нет."""
    try:
        from backend.integrations.registry import IntegrationRegistry
        reg = IntegrationRegistry()
        await reg.load_for_user(user_id)
        gmail = reg.get("gmail")
        if gmail is None or not hasattr(gmail, "send_email"):
            return None
        return gmail
    except Exception as exc:
        logger.debug("gmail load failed for %s: %s", user_id, exc)
        return None


async def _deliver_email(
    *,
    user_id: str,
    pipeline_output: Any,
    recipient_address: Optional[str],
) -> DeliveryResult:
    """Реальная отправка письма через Gmail (Phase B.2)."""
    subject, body = _extract_email_payload(pipeline_output)
    to_addr = await _resolve_email_address(user_id, recipient_address)
    if not to_addr:
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.EMAIL,
            error_message="no email address for user_id",
        )
    gmail = await _load_gmail_for_user(user_id)
    if gmail is None:
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.EMAIL,
            error_message="Gmail integration not configured",
        )
    try:
        import json as _json
        raw = await gmail.send_email(to=to_addr, subject=subject, body=body)
        # send_email возвращает JSON-строку
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            parsed = None
        # Audit-фикс: НЕ дефолтить success=True. Не-dict / dict без success →
        # fail, чтобы non-dict ответ не маскировался под доставку.
        if isinstance(parsed, dict) and parsed.get("success") and not parsed.get("error"):
            return DeliveryResult(
                success=True,
                channel=DeliveryChannel.EMAIL,
                external_ref=str(parsed.get("message_id") or ""),
            )
        if isinstance(parsed, dict):
            err = parsed.get("error") or "send failed"
        else:
            err = f"unexpected send_email response: {str(raw)[:200]}"
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.EMAIL,
            error_message=str(err),
        )
    except Exception as exc:
        logger.warning("gmail send failed for %s: %s", user_id, exc)
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.EMAIL,
            error_message=str(exc),
        )


async def _deliver_email_draft(
    *,
    user_id: str,
    pipeline_output: Any,
    recipient_address: Optional[str],
) -> DeliveryResult:
    """Создать draft в Gmail (Phase B.2). Если Gmail не настроен —
    success=True с external_ref=None: artifact уже лежит в coffee_artifacts,
    UI покажет его для ручной отправки.
    """
    subject, body = _extract_email_payload(pipeline_output)
    to_addr = await _resolve_email_address(user_id, recipient_address) or ""
    gmail = await _load_gmail_for_user(user_id)
    if gmail is None or not hasattr(gmail, "create_draft"):
        logger.info(
            "[coffee.delivery] email_draft: no gmail integration; stored in DB only"
        )
        return DeliveryResult(
            success=True,
            channel=DeliveryChannel.EMAIL_DRAFT,
            external_ref=None,
        )
    try:
        import json as _json
        raw = await gmail.create_draft(to=to_addr, subject=subject, body=body)
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            parsed = None
        # Audit-фикс: НЕ дефолтить success=True. Раньше Gmail-error без явного
        # 'success' ключа (e.g. {"error":"invalid_grant"}) рапортовался как
        # delivered с external_ref=None — silent data loss. Теперь:
        # - не-dict ответ → fail
        # - dict без явного truthy success / draft_id / id → fail
        if isinstance(parsed, dict):
            draft_id = parsed.get("draft_id") or parsed.get("id")
            ok = bool(parsed.get("success")) or bool(draft_id)
            if ok and not parsed.get("error"):
                return DeliveryResult(
                    success=True,
                    channel=DeliveryChannel.EMAIL_DRAFT,
                    external_ref=str(draft_id) if draft_id else None,
                )
            return DeliveryResult(
                success=False,
                channel=DeliveryChannel.EMAIL_DRAFT,
                error_message=str(parsed.get("error") or "gmail create_draft returned no draft_id"),
            )
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.EMAIL_DRAFT,
            error_message=f"unexpected create_draft response: {str(raw)[:200]}",
        )
    except Exception as exc:
        logger.warning("gmail draft failed for %s: %s", user_id, exc)
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.EMAIL_DRAFT,
            error_message=str(exc),
        )


async def _deliver_notion(
    *,
    user_id: str,
    title: str,
    body: str,
) -> DeliveryResult:
    """Создать страницу в Notion через user'ский Notion integration token."""
    try:
        from backend.integrations.registry import IntegrationRegistry
        reg = IntegrationRegistry()
        await reg.load_for_user(user_id)
        # NotionIntegration.create_page(title, content_markdown)
        notion = reg.get("notion")
        if notion is None or not hasattr(notion, "create_page"):
            return DeliveryResult(
                success=False,
                channel=DeliveryChannel.NOTION,
                error_message="Notion integration not loaded для user_id",
            )
        result = await notion.create_page(title=title, content_markdown=body)
        page_id = result.get("page_id") if isinstance(result, dict) else None
        return DeliveryResult(
            success=bool(result),
            channel=DeliveryChannel.NOTION,
            external_ref=page_id,
        )
    except Exception as exc:
        logger.warning("notion delivery failed: %s", exc)
        return DeliveryResult(
            success=False,
            channel=DeliveryChannel.NOTION,
            error_message=str(exc),
        )


async def _deliver_to_executor(
    *,
    user_id: str,
    pipeline_output: Any,
    executor_name: str,
    channel: DeliveryChannel = DeliveryChannel.CLAUDE_CODE,
) -> DeliveryResult:
    """Отправить ТЗ в Executor (claude_code_cli / cursor_cli / codex_cli).

    Создаёт TaskSubmission через ExecutorBackend и возвращает task_id
    как external_ref. Реальное исполнение — асинхронно. `channel` —
    исходный канал доставки (для корректной маркировки DeliveryResult вне
    зависимости от того, какой именно executor_name выбран).
    """
    try:
        from backend.core.executors.base import TaskSubmission
        from backend.core.executors.registry import get_backend
        backend = get_backend(executor_name)
        # ТЗ передаём как prompt; markdown сохраняется в context для CLI
        sub = TaskSubmission(
            prompt=getattr(pipeline_output, "content_markdown", "")[:50000],
            metadata={
                "coffee_artifact_type": getattr(pipeline_output, "artifact_type", ""),
                "coffee_role": getattr(pipeline_output, "role", "").value
                if hasattr(getattr(pipeline_output, "role", ""), "value")
                else str(getattr(pipeline_output, "role", "")),
                "user_id": user_id,
                "title": getattr(pipeline_output, "title", ""),
                "executor": executor_name,
            },
        )
        handle = await backend.submit(sub)
        return DeliveryResult(
            success=True,
            channel=channel,
            external_ref=handle.task_id,
        )
    except Exception as exc:
        logger.warning("executor delivery failed: %s", exc)
        return DeliveryResult(
            success=False,
            channel=channel,
            error_message=str(exc),
        )


__all__ = [
    "DeliveryChannel",
    "DeliveryRecipient",
    "DeliveryResult",
    "deliver_artifact",
    "pick_channels",
]
