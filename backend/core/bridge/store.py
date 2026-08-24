"""Bridge message store — durable outbox/inbox для user→user транспорта.

Зеркалит паттерн reactive/signal_queue.py: Postgres, apply_tenant=False,
text() SQL, best-effort never-raise. Дополнительно опционально пушит
сообщение в reactive signal_queue (signal_type='bridge_message'), чтобы оно
дошло до получателя через существующий drain→delivery пайплайн (Telegram),
а не только лежало в inbox для polling'а.

Таблица: public.bridge_messages (миграция 230_bridge_messages.sql).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Допустимые типы сообщений (мягкая валидация — unknown не валит, но
# нормализуется к 'message').
MESSAGE_TYPES = {
    "message",                # обычное сообщение user→user
    "manager_signal",         # именной сигнал наверх (к руководителю)
    "manager_signal_anon",    # анонимный сигнал наверх
    "aggregate",              # анонимный агрегированный (k-anonymity)
}

_DEFAULT_VALID_UNTIL_MIN = 14 * 24 * 60  # push-сигнал живёт 14 дней


@dataclass
class BridgeMessage:
    """Одно сообщение в bridge outbox/inbox."""

    id: str
    from_user_id: str
    to_user_id: str
    message_type: str
    title: Optional[str]
    body_markdown: Optional[str]
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "unread"
    signal_id: Optional[str] = None
    read_at: Optional[str] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "from_user_id": self.from_user_id,
            "to_user_id": self.to_user_id,
            "message_type": self.message_type,
            "title": self.title,
            "body_markdown": self.body_markdown,
            "payload": self.payload,
            "status": self.status,
            "signal_id": self.signal_id,
            "read_at": self.read_at,
            "created_at": self.created_at,
        }


def _normalize_type(t: Optional[str]) -> str:
    t = (t or "message").strip().lower()
    return t if t in MESSAGE_TYPES else "message"


async def _user_exists(session: Any, user_id: str) -> bool:
    """Проверить, что получатель существует. Best-effort: при ошибке/отсутствии
    таблицы users → True (не блокируем доставку из-за диагностики)."""
    try:
        from sqlalchemy import text
        r = await session.execute(
            text("SELECT 1 FROM public.users WHERE id = CAST(:uid AS UUID) LIMIT 1"),
            {"uid": user_id},
        )
        return r.first() is not None
    except Exception as exc:
        logger.debug("bridge _user_exists check skipped: %s", exc)
        return True


async def send_message(
    *,
    from_user_id: str,
    to_user_id: str,
    body_markdown: str = "",
    title: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    message_type: str = "message",
    tenant_id: Optional[str] = None,
    push: bool = True,
) -> dict[str, Any]:
    """Отправить сообщение to_user_id. Never-raise.

    Записывает строку в bridge_messages (status='unread'). Если push=True —
    дополнительно кладёт reactive-сигнал (signal_type='bridge_message') в
    очередь получателя, чтобы drain доставил его в Telegram. signal_id
    линкуется в строку.

    Returns:
        {"ok": bool, "message_id": str|None, "signal_id": str|None,
         "reason": str}
    """
    mtype = _normalize_type(message_type)
    result: dict[str, Any] = {
        "ok": False, "message_id": None, "signal_id": None, "reason": "",
    }
    if not from_user_id or not to_user_id:
        result["reason"] = "from_user_id and to_user_id required"
        return result

    mid = str(uuid.uuid4())
    tid = tenant_id or from_user_id

    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            if not await _user_exists(session, to_user_id):
                result["reason"] = f"recipient {to_user_id} not found"
                return result

            # 1. Опциональный push: enqueue ПОСЛЕ валидации получателя, чтобы
            # не создать сироту-сигнал на несуществующего пользователя.
            signal_id: Optional[str] = None
            if push:
                try:
                    from backend.core.reactive.signal_queue import enqueue_signal
                    signal_id = await enqueue_signal(
                        user_id=to_user_id,
                        signal_type="bridge_message",
                        title=title or "Сообщение",
                        body_markdown=body_markdown or "",
                        payload={
                            "from_user_id": from_user_id,
                            "bridge_message_id": mid,
                            "message_type": mtype,
                            "recommended_channels": ["telegram"],
                            **(payload or {}),
                        },
                        priority=60,
                        tenant_id=tid,
                        valid_until_minutes=_DEFAULT_VALID_UNTIL_MIN,
                        source_kind="bridge_message",
                        source_id=mid,
                    )
                except Exception as exc:
                    logger.debug("bridge push enqueue failed (inbox-only): %s", exc)

            # 2. Durable строка в bridge_messages.
            await session.execute(
                text("""
                    INSERT INTO public.bridge_messages
                        (id, from_user_id, to_user_id, tenant_id, message_type,
                         title, body_markdown, payload, status, signal_id)
                    VALUES
                        (CAST(:id AS UUID), CAST(:fu AS UUID), CAST(:tu AS UUID),
                         CAST(:tid AS UUID), :mtype, :title, :body,
                         CAST(:pl AS JSONB), 'unread',
                         CAST(:sig AS UUID))
                """),
                {
                    "id": mid,
                    "fu": from_user_id,
                    "tu": to_user_id,
                    "tid": tid,
                    "mtype": mtype,
                    "title": (title or "")[:500] or None,
                    "body": (body_markdown or "")[:50000],
                    "pl": json.dumps(payload or {}, ensure_ascii=False, default=str),
                    "sig": signal_id,
                },
            )
        result.update(ok=True, message_id=mid, signal_id=signal_id,
                      reason="sent")
        return result
    except Exception as exc:
        logger.warning("bridge send_message failed: %s", exc)
        result["reason"] = f"db error: {exc}"
        return result


def _row_to_msg(r: Any) -> BridgeMessage:
    pl = r[7]
    if isinstance(pl, str):
        try:
            pl = json.loads(pl)
        except Exception:
            pl = {}
    return BridgeMessage(
        id=str(r[0]),
        from_user_id=str(r[1]),
        to_user_id=str(r[2]),
        message_type=r[3],
        title=r[4],
        body_markdown=r[5] or "",
        payload=pl or {},
        status=r[6],
        signal_id=str(r[8]) if r[8] else None,
        read_at=r[9].isoformat() if r[9] else None,
        created_at=r[10].isoformat() if r[10] else None,
    )


_SELECT_COLS = (
    "id, from_user_id, to_user_id, message_type, title, body_markdown, "
    "status, payload, signal_id, read_at, created_at"
)


async def list_inbox(
    *,
    user_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[BridgeMessage]:
    """Входящие сообщения user_id (свежие сверху). Best-effort → []."""
    if not user_id:
        return []
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        conditions = ["to_user_id = CAST(:uid AS UUID)"]
        params: dict[str, Any] = {"uid": user_id, "lim": max(1, min(int(limit), 200))}
        if status:
            conditions.append("status = :st")
            params["st"] = status
        where = " AND ".join(conditions)
        sql = f"""
            SELECT {_SELECT_COLS}
            FROM public.bridge_messages
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :lim
        """
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(text(sql), params)
            return [_row_to_msg(r) for r in rows]
    except Exception as exc:
        logger.warning("bridge list_inbox failed: %s", exc)
        return []


async def list_outbox(
    *,
    user_id: str,
    limit: int = 50,
) -> list[BridgeMessage]:
    """Исходящие сообщения user_id. Best-effort → []."""
    if not user_id:
        return []
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        sql = f"""
            SELECT {_SELECT_COLS}
            FROM public.bridge_messages
            WHERE from_user_id = CAST(:uid AS UUID)
            ORDER BY created_at DESC
            LIMIT :lim
        """
        async with pg.session(apply_tenant=False) as session:
            rows = await session.execute(
                text(sql), {"uid": user_id, "lim": max(1, min(int(limit), 200))},
            )
            return [_row_to_msg(r) for r in rows]
    except Exception as exc:
        logger.warning("bridge list_outbox failed: %s", exc)
        return []


async def mark_read(*, message_id: str, user_id: str) -> bool:
    """Пометить сообщение прочитанным. Только получатель может (to_user_id
    фильтр в WHERE → нельзя ack чужое). Returns True если строка обновлена."""
    if not message_id or not user_id:
        return False
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            res = await session.execute(
                text("""
                    UPDATE public.bridge_messages
                    SET status = 'read', read_at = now(), updated_at = now()
                    WHERE id = CAST(:mid AS UUID)
                      AND to_user_id = CAST(:uid AS UUID)
                      AND status = 'unread'
                """),
                {"mid": message_id, "uid": user_id},
            )
        return (getattr(res, "rowcount", 0) or 0) > 0
    except Exception as exc:
        logger.warning("bridge mark_read failed: %s", exc)
        return False
