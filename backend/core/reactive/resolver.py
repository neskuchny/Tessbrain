"""Cascade dependency resolver (Phase J).

До Phase J `apply_cascade` требовал явно передать ВСЕ зависимые artifact_ids
и signal_ids. Это работало, но возлагало на caller'а знание схемы:
«если отменили meeting, нужно найти все его coffee_artifacts, потом все
reactive_signals по каждому артефакту...». Phase J автоматизирует обход
графа зависимостей через существующие FK-like колонки — без миграций.

Граф зависимостей:

  meeting (meeting_id)
     │
     │ coffee_artifacts.meeting_id
     ▼
  coffee_artifact (id)
     │
     │ reactive_signals.source_kind='coffee_artifact'
     │ reactive_signals.source_id=<artifact.id>
     ▼
  reactive_signal (id)

  + meeting_id может быть продублирован в reactive_signals.payload->>'meeting_id'
    (Coffee orchestrator кладёт это поле при enqueue)

  + coffee_artifacts.superseded_by — обратная связь для idempotency:
    повторный cascade по уже superseded артефакту не делает ничего

API:
- resolve_dependencies(trigger_kind, trigger_ref)
    → (artifact_ids: list[str], signal_ids: list[str])
  Pure graph walk. Best-effort: при сбое возвращает ([], []).

Поддержанные trigger_kind'ы:
- "meeting_cancelled" / "meeting_rescheduled":
    trigger_ref = meeting_id
    → все coffee_artifacts этой встречи (status IN pending/delivered)
    → все reactive_signals с source_id из этих артефактов
    → плюс reactive_signals.payload->>'meeting_id' = ref
- "artifact_superseded" / "artifact_cancelled":
    trigger_ref = artifact_id
    → reactive_signals where source_kind='coffee_artifact' AND source_id=ref

Прочие kind'ы (task_cancelled, decision_reverted, ...) возвращают ([], [])
— для них схема пока не несёт линков; caller передаёт явные списки.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


_MEETING_TRIGGERS = {"meeting_cancelled", "meeting_rescheduled"}
_ARTIFACT_TRIGGERS = {"artifact_superseded", "artifact_cancelled"}


async def resolve_dependencies(
    trigger_kind: str,
    trigger_ref: str,
) -> tuple[list[str], list[str]]:
    """Найти все артефакты и сигналы, зависящие от триггера.

    Never-raise: при любом сбое возвращает ([], []).
    """
    kind = (trigger_kind or "").strip().lower()
    ref = (trigger_ref or "").strip()
    if not kind or not ref:
        return [], []

    try:
        if kind in _MEETING_TRIGGERS:
            return await _resolve_meeting(ref)
        if kind in _ARTIFACT_TRIGGERS:
            return [], await _resolve_signals_for_artifact(ref)
    except Exception as exc:
        logger.debug(
            "resolve_dependencies failed for %s/%s: %s",
            kind, ref, exc,
        )
    return [], []


async def _resolve_meeting(meeting_id: str) -> tuple[list[str], list[str]]:
    """meeting_id → (artifact_ids активных, signal_ids очереди)."""
    from sqlalchemy import text
    from backend.db.postgres import get_postgres
    pg = await get_postgres()
    artifact_ids: list[str] = []
    signal_ids: list[str] = []
    async with pg.session(apply_tenant=False) as session:
        # 1. coffee_artifacts этой встречи (только живые — superseded/failed уже отработаны)
        rows = await session.execute(
            text("""
                SELECT id FROM public.coffee_artifacts
                WHERE meeting_id = CAST(:mid AS UUID)
                  AND status IN ('pending', 'delivered')
            """),
            {"mid": meeting_id},
        )
        artifact_ids = [str(r[0]) for r in rows]

        # 2. reactive_signals: через source_id артефактов ИЛИ payload.meeting_id
        if artifact_ids:
            rows = await session.execute(
                text("""
                    SELECT id FROM public.reactive_signals
                    WHERE status = 'queued'
                      AND (
                            (source_kind = 'coffee_artifact'
                             AND source_id = ANY(:aids))
                         OR payload->>'meeting_id' = :mid_text
                      )
                """),
                {"aids": artifact_ids, "mid_text": meeting_id},
            )
        else:
            # Артефактов нет, но сигналы по meeting_id всё равно могут быть
            rows = await session.execute(
                text("""
                    SELECT id FROM public.reactive_signals
                    WHERE status = 'queued'
                      AND payload->>'meeting_id' = :mid_text
                """),
                {"mid_text": meeting_id},
            )
        signal_ids = [str(r[0]) for r in rows]
    return artifact_ids, signal_ids


async def _resolve_signals_for_artifact(artifact_id: str) -> list[str]:
    """artifact_id → signal_ids активных сигналов на этот артефакт."""
    from sqlalchemy import text
    from backend.db.postgres import get_postgres
    pg = await get_postgres()
    async with pg.session(apply_tenant=False) as session:
        rows = await session.execute(
            text("""
                SELECT id FROM public.reactive_signals
                WHERE status = 'queued'
                  AND source_kind = 'coffee_artifact'
                  AND source_id = :aid
            """),
            {"aid": artifact_id},
        )
        return [str(r[0]) for r in rows]


__all__ = ["resolve_dependencies"]
