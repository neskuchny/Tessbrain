"""Persisted capture-observations storage (Phase A.4).

Слой между capture-агентами и PersonaAggregator. Capture-агенты
(communication_style_agent, psychological_analyzer, work_preferences_agent,
personal_interests_agent, chat_preference_extractor, ...) сохраняют свои
rich-выходы сюда с user-attribution. PersonaAggregator читает оттуда
вместо realtime LLM-вызовов.

Контракт:
- save_observation(user_id, meeting_id, agent_type, payload, ...)
  Идемпотентно по (user_id, meeting_id, agent_type) — повторный вызов
  обновляет payload.
- list_observations(user_id, agent_types?=, since?=, limit?=)
  Возвращает payload'ы в обратном хронологическом порядке.

Best-effort: при недоступности БД молча возвращаем [] / False — capture
pipeline не должен валиться из-за отсутствия Persona-storage.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def save_observation(
    *,
    user_id: str,
    agent_type: str,
    payload: dict[str, Any],
    meeting_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    confidence: float = 1.0,
    source_type: str = "capture_agent",
) -> bool:
    """Сохранить один capture-observation для user_id.

    Идемпотентно по (user_id, meeting_id, agent_type) — UPSERT.
    Возвращает True если успешно сохранено, False иначе (не raise).
    """
    if not (user_id and agent_type and isinstance(payload, dict)):
        logger.debug(
            "save_observation: missing required fields user_id=%s agent=%s",
            user_id, agent_type,
        )
        return False
    try:
        from backend.db.postgres import get_postgres
        from backend.db.postgres_tenant import set_persona_tenant
        from sqlalchemy import text
        pg = await get_postgres()
        tid = tenant_id or user_id
        async with pg.session(apply_tenant=False) as session:
            await set_persona_tenant(session, tid)  # Phase A.6 persona-RLS
            await session.execute(
                text("""
                    INSERT INTO public.persona_observations
                        (user_id, tenant_id, meeting_id, agent_type, payload,
                         confidence, source_type, extracted_at)
                    VALUES
                        (:uid, :tid, :mid, :agent, CAST(:payload AS JSONB),
                         :conf, :src, now())
                    ON CONFLICT (user_id, meeting_id, agent_type) DO UPDATE SET
                        payload      = EXCLUDED.payload,
                        confidence   = EXCLUDED.confidence,
                        source_type  = EXCLUDED.source_type,
                        extracted_at = now()
                """),
                {
                    "uid": user_id,
                    "tid": tid,
                    "mid": meeting_id,
                    "agent": agent_type,
                    "payload": json.dumps(payload, ensure_ascii=False, default=str),
                    "conf": float(confidence),
                    "src": source_type,
                },
            )
        return True
    except Exception as exc:
        logger.warning(
            "save_observation failed for user=%s agent=%s: %s",
            user_id, agent_type, exc,
        )
        return False


async def list_observations(
    *,
    user_id: str,
    agent_types: Optional[list[str]] = None,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Получить observations user'а в обратном хронологическом порядке.

    Args:
        user_id: чей профиль строим
        agent_types: фильтр по списку типов (None = все)
        since: только observations after datetime (None = без ограничения)
        limit: максимум записей

    Returns:
        список dict'ов {meeting_id, agent_type, payload, confidence, extracted_at}
        Пустой список если ничего нет / БД недоступна.
    """
    if not user_id:
        return []
    try:
        from backend.db.postgres import get_postgres
        from sqlalchemy import text
        pg = await get_postgres()

        # Динамическая фильтрация без SQL-injection: WHERE собирается из
        # параметризованных частей.
        conditions = ["user_id = :uid"]
        params: dict[str, Any] = {"uid": user_id, "lim": int(limit)}

        if agent_types:
            placeholders = []
            for i, at in enumerate(agent_types):
                k = f"at_{i}"
                placeholders.append(f":{k}")
                params[k] = at
            conditions.append(f"agent_type IN ({','.join(placeholders)})")

        if since is not None:
            conditions.append("extracted_at >= :since")
            params["since"] = since

        sql = f"""
            SELECT meeting_id, agent_type, payload, confidence,
                   source_type, extracted_at
            FROM public.persona_observations
            WHERE {' AND '.join(conditions)}
            ORDER BY extracted_at DESC
            LIMIT :lim
        """
        from backend.db.postgres_tenant import set_persona_tenant
        async with pg.session(apply_tenant=False) as session:
            await set_persona_tenant(session, user_id)  # Phase A.6 persona-RLS
            rows = await session.execute(text(sql), params)
            result: list[dict[str, Any]] = []
            for r in rows:
                payload = r[2]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                result.append({
                    "meeting_id": str(r[0]) if r[0] else None,
                    "agent_type": r[1],
                    "payload": payload,
                    "confidence": float(r[3] or 0.0),
                    "source_type": r[4],
                    "extracted_at": r[5].isoformat() if r[5] else None,
                })
            return result
    except Exception as exc:
        logger.warning("list_observations failed for user=%s: %s", user_id, exc)
        return []


__all__ = ["save_observation", "list_observations"]
