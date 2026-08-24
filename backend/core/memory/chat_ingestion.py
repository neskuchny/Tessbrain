# -*- coding: utf-8 -*-
"""
Chat → memory ingestion (третий контекст памяти, MEMORY_DESIGN_PRINCIPLES.md §6).

Минимальная безопасная запись чат-обмена (user_message + assistant_response) в
память пользователя как retrievable документ. Принципы:
- НЕДЕСТРУКТИВНО — сохраняем полный текст пары; никакого lossy-извлечения LLM
  (по гардрейлу: измерено в MEM0_STUDY.md, что наивное извлечение теряет recall).
- Per-user изоляция через user_id (тот же путь, что у встреч).
- Идемпотентно — id документа детерминирован от user_id+session+timestamp+хешей.
- За флагом enable_chat_memory_ingestion (default OFF) — продукт прежний.
- Вызывается fire-and-forget из чата (не блокирует ответ); все ошибки глушатся.

Что НЕ делаем здесь (намеренно, по дизайну):
- Не извлекаем «факты в стиле Mem0» (lossy, измеренная регрессия).
- Не пишем в граф (граф у нас под встречи; для чата сначала просто vector-recall).
- Не вызываем frequency_tracker/hebbian (это про сущности встреч; ingestion чата
  — про текст беседы, а не про граф сущностей).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional


def _short_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()[:10]


def build_chat_document(
    *, user_id: str, session_id: Optional[str], user_message: str,
    assistant_response: str, now: Optional[datetime] = None,
) -> tuple[str, dict]:
    """Собрать пару чат-обмена как один документ.

    Возвращает (text, metadata). Идемпотентный id строится так, что повторный
    вызов с тем же содержимым в ту же сессию даёт тот же id (vector_indexer
    .add_document делает upsert по id → дубликата не будет)."""
    now = now or datetime.now(timezone.utc)
    text = (
        f"[CHAT {now.isoformat()}]\n"
        f"User: {user_message}\n"
        f"Assistant: {assistant_response}"
    )
    doc_id = (
        f"chat_{user_id[:24]}_"
        f"{(session_id or 'none')[:16]}_"
        f"{_short_hash(user_message)}{_short_hash(assistant_response)}"
    )
    metadata = {
        "id": doc_id,
        "source": "chat",
        "user_id": user_id,
        "session_id": session_id,
        "timestamp": now.isoformat(),
        "user_message_len": len(user_message),
        "assistant_response_len": len(assistant_response),
        "title": user_message[:80],
    }
    return text, metadata


async def ingest_chat_exchange(
    *, user_id: str, session_id: Optional[str], user_message: str,
    assistant_response: str, indexer=None, access_group: str = "private",
) -> dict:
    """Записать чат-обмен в vector-память. Используется из роута chat fire-and-forget.

    Возвращает {"ingested": bool, "doc_id"?: str, "reason"?: str}. Любая ошибка —
    в reason; вызвавшего не роняем."""
    if not (user_id and user_message and assistant_response):
        return {"ingested": False, "reason": "empty fields"}
    text, meta = build_chat_document(
        user_id=user_id, session_id=session_id,
        user_message=user_message, assistant_response=assistant_response,
    )
    try:
        if indexer is None:
            from backend.core.store.vector_indexer import get_vector_indexer
            indexer = await get_vector_indexer()
        await indexer.add_document(text=text, metadata=meta, access_group=access_group)
        return {"ingested": True, "doc_id": meta["id"]}
    except Exception as e:
        return {"ingested": False, "reason": f"{type(e).__name__}: {e}"}
