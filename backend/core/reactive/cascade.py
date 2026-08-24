"""Cascade invalidation (Phase C).

Когда что-то отменяется — отмена задачи, перенос встречи, откат
решения — мы не должны продолжать слать связанные с этим артефакты.
Cascade берёт триггер-событие и:
1. Находит зависимые сущности (coffee_artifacts, reactive_signals)
2. Помечает их superseded/cancelled
3. Логирует в cascade_events для аудита и UI «эти briefs неактуальны»

API:
- record_cascade(...)  — сохранить событие
- apply_cascade(...)   — комбо: записать + инвалидировать связанные

Используется из:
- ActionEngine когда action.status=cancelled
- Coffee orchestrator когда meeting.transcription пересобран
- Capture pipeline если decision был помечен reverted

All best-effort.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CascadeEvent:
    """Сохранённое cascade-событие."""

    id: str
    user_id: str
    trigger_kind: str
    trigger_ref: str
    invalidated: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    triggered_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "trigger_kind": self.trigger_kind,
            "trigger_ref": self.trigger_ref,
            "invalidated": self.invalidated,
            "reason": self.reason,
            "triggered_at": self.triggered_at,
        }


async def record_cascade(
    *,
    user_id: str,
    trigger_kind: str,
    trigger_ref: str,
    invalidated: list[dict[str, Any]],
    reason: str = "",
    tenant_id: Optional[str] = None,
) -> Optional[str]:
    """Сохранить cascade event. Возвращает id или None."""
    try:
        from sqlalchemy import text
        from backend.db.postgres import get_postgres
        pg = await get_postgres()
        cid = str(uuid.uuid4())
        tid = tenant_id or user_id
        async with pg.session(apply_tenant=False) as session:
            await session.execute(
                text("""
                    INSERT INTO public.cascade_events
                        (id, user_id, tenant_id, trigger_kind, trigger_ref,
                         invalidated, reason)
                    VALUES
                        (CAST(:cid AS UUID), CAST(:uid AS UUID),
                         CAST(:tid AS UUID), :tk, :tr,
                         CAST(:inv AS JSONB), :rsn)
                """),
                {
                    "cid": cid,
                    "uid": user_id,
                    "tid": tid,
                    "tk": trigger_kind,
                    "tr": trigger_ref,
                    "inv": json.dumps(invalidated, ensure_ascii=False, default=str),
                    "rsn": reason[:1000],
                },
            )
        return cid
    except Exception as exc:
        logger.warning("record_cascade failed: %s", exc)
        return None


async def apply_cascade(
    *,
    user_id: str,
    trigger_kind: str,
    trigger_ref: str,
    affected_artifact_ids: Optional[list[str]] = None,
    affected_signal_ids: Optional[list[str]] = None,
    auto_resolve: bool = False,
    reason: str = "",
    tenant_id: Optional[str] = None,
) -> Optional[CascadeEvent]:
    """Инвалидировать артефакты + сигналы и записать cascade-событие.

    Phase J: при `auto_resolve=True` автоматически ищет зависимости через
    `resolver.resolve_dependencies(trigger_kind, trigger_ref)` и мерджит их
    с явно переданными списками. Поддержанные kind'ы: meeting_*, artifact_*
    (см. resolver.py). Прочие — auto_resolve становится no-op.

    Никогда не бросает; пустые списки = no-op. Возвращает CascadeEvent
    с полным списком того, что было реально инвалидировано.
    """
    invalidated: list[dict[str, Any]] = []

    # Phase J: автоматический discover зависимостей
    auto_artifact_ids: list[str] = []
    auto_signal_ids: list[str] = []
    if auto_resolve:
        try:
            from backend.core.reactive.resolver import resolve_dependencies
            auto_artifact_ids, auto_signal_ids = await resolve_dependencies(
                trigger_kind, trigger_ref,
            )
        except Exception as exc:
            logger.debug("auto_resolve failed: %s", exc)

    # Мердж явных и авто-найденных (dedup, сохраняя порядок)
    def _merge(explicit: Optional[list[str]], auto: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in (explicit or []) + auto:
            sx = str(x)
            if sx and sx not in seen:
                seen.add(sx)
                out.append(sx)
        return out

    merged_artifact_ids = _merge(affected_artifact_ids, auto_artifact_ids)
    merged_signal_ids = _merge(affected_signal_ids, auto_signal_ids)

    # 1. coffee_artifacts → status='superseded' (мы используем существующее
    # значение, чтобы UI знал «больше не актуально»; альтернатива — добавить
    # 'cancelled' enum-значение, но superseded уже несёт нужную семантику).
    # Audit-фикс: WHERE добавляет user_id чтобы caller не мог отменить чужие
    # артефакты (POST /reactive/cascade принимает body-supplied ids).
    # Audit-фикс: invalidated.append только при rowcount > 0 — нулевые UPDATEs
    # больше не попадают в cascade_events.invalidated и API-response.
    if merged_artifact_ids:
        try:
            from sqlalchemy import text
            from backend.db.postgres import get_postgres
            pg = await get_postgres()
            async with pg.session(apply_tenant=False) as session:
                for aid in merged_artifact_ids:
                    try:
                        res = await session.execute(
                            text("""
                                UPDATE public.coffee_artifacts
                                SET status = 'superseded'
                                WHERE id = CAST(:aid AS UUID)
                                  AND user_id = CAST(:uid AS UUID)
                                  AND status IN ('pending', 'delivered')
                            """),
                            {"aid": aid, "uid": user_id},
                        )
                        if (getattr(res, "rowcount", 0) or 0) > 0:
                            invalidated.append({"kind": "coffee_artifact", "id": aid})
                    except Exception as exc:
                        logger.debug("cascade artifact %s: %s", aid, exc)
        except Exception as exc:
            logger.warning("cascade artifacts batch failed: %s", exc)

    # 2. reactive_signals → status='cancelled' (тот же user_id-фильтр)
    if merged_signal_ids:
        try:
            from sqlalchemy import text
            from backend.db.postgres import get_postgres
            pg = await get_postgres()
            async with pg.session(apply_tenant=False) as session:
                for sid in merged_signal_ids:
                    try:
                        res = await session.execute(
                            text("""
                                UPDATE public.reactive_signals
                                SET status = 'cancelled',
                                    updated_at = now()
                                WHERE id = CAST(:sid AS UUID)
                                  AND user_id = CAST(:uid AS UUID)
                                  AND status = 'queued'
                            """),
                            {"sid": sid, "uid": user_id},
                        )
                        if (getattr(res, "rowcount", 0) or 0) > 0:
                            invalidated.append({"kind": "reactive_signal", "id": sid})
                    except Exception as exc:
                        logger.debug("cascade signal %s: %s", sid, exc)
        except Exception as exc:
            logger.warning("cascade signals batch failed: %s", exc)

    cid = await record_cascade(
        user_id=user_id,
        trigger_kind=trigger_kind,
        trigger_ref=trigger_ref,
        invalidated=invalidated,
        reason=reason,
        tenant_id=tenant_id,
    )
    # Audit-фикс: даже если запись в cascade_events упала (audit-row insert
    # error), UPDATE'ы артефактов/сигналов УЖЕ выполнены. Не врать
    # caller'у про success=false — это destructive операция, которая
    # фактически произошла. Возвращаем CascadeEvent с id=cid (или пустой
    # строкой как маркер «audit lost»).
    return CascadeEvent(
        id=cid or "",
        user_id=user_id,
        trigger_kind=trigger_kind,
        trigger_ref=trigger_ref,
        invalidated=invalidated,
        reason=reason,
    )


__all__ = [
    "CascadeEvent",
    "apply_cascade",
    "record_cascade",
]
