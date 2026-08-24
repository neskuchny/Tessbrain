"""Authoritative external/override field store for Persona (Phase A.5).

Единый durable-слой «авторитетных» значений полей Persona, которые должны
ПОБЕЖДАТЬ автоматическую агрегацию из встреч и переживать rebuild:

- manual override founder'а (POST /persona/me/override, source_type='manual');
- push профиля сотрудника из Mini Tess (POST /persona/me/self,
  source_type='mini_tess_input') — маппинг их S1/S2/S3 на наши поля +
  free-form extended-секции (S4 cogni / narrative biography).

До этого override применялся только к JSONB-блобу и затирался ближайшим
PersonaAggregator.build_for_user. Теперь значения лежат в
persona_external_sources, а агрегатор после сборки из встреч RE-APPLY'ит их
последними (см. aggregator.reapply_external) → они всегда актуальны.

Все функции best-effort/never-raise, в духе остального persona-слоя.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from backend.core.persona.models import (
    AbstractionLevel,
    AreaAssessment,
    ChannelPreference,
    CommunicationStyle,
    Persona,
)

logger = logging.getLogger(__name__)

# Какие section.field разрешено писать извне. extended.* — отдельно (free-form).
# Зеркалит whitelist из /persona/me/override (исторический контракт).
OVERRIDE_WHITELIST: dict[str, set[str]] = {
    "communication_cognitive": {
        "preferred_style", "vocabulary_register", "metaphor_domains",
        "abstraction_level", "preferred_language",
    },
    "behavioral": {
        "channel_preferences", "peak_hours_utc", "do_not_disturb_windows_utc",
    },
    "development": {"active_goals", "decision_style"},
    "strengths_weaknesses": {"strong", "developing", "blind_spots"},
}

# source_type значения (совпадают с ProvenanceSource.value).
SOURCE_MANUAL = "manual"
SOURCE_MINI_TESS = "mini_tess_input"
SOURCE_EXTERNAL = "external"


def _coerce(section_name: str, field_name: str, value: Any) -> Any:
    """Привести raw JSON-значение к типу поля Persona (enum'ы, AreaAssessment).

    Без этого to_public_dict() падал бы на .value у строки вместо enum'а.
    Неизвестные/простые поля (списки, строки) — как есть.
    """
    if section_name == "communication_cognitive":
        if field_name == "preferred_style":
            return value if isinstance(value, CommunicationStyle) else CommunicationStyle(value)
        if field_name == "abstraction_level":
            return value if isinstance(value, AbstractionLevel) else AbstractionLevel(value)
    if section_name == "behavioral" and field_name == "channel_preferences":
        return [
            v if isinstance(v, ChannelPreference) else ChannelPreference(v)
            for v in (value or [])
        ]
    if section_name == "strengths_weaknesses":
        out = []
        for a in (value or []):
            if isinstance(a, AreaAssessment):
                out.append(a)
            elif isinstance(a, dict):
                out.append(AreaAssessment(
                    area=str(a.get("area", "")),
                    confidence=float(a.get("confidence") or 0.0),
                    evidence_meeting_ids=list(
                        a.get("evidence_meeting_ids") or a.get("evidence") or []
                    ),
                ))
        return out
    return value


def apply_field(
    persona: Persona, field_path: str, value: Any, *, allow_extended: bool = True,
) -> tuple[bool, str]:
    """Применить значение к live-Persona по dotted-path. (ok, error_message).

    Разрешено:
    - whitelisted section.field (с type-coercion);
    - extended.<key> — free-form vendor-секция (cogni / biography / ...),
      ТОЛЬКО если allow_extended=True.

    allow_extended=False используется /me/override (founder-правка одного
    типизированного поля): этот endpoint исторически не давал писать
    free-form extended.* — расширять его surface нельзя. Free-form extended
    идёт исключительно через /me/self (push профиля из Mini Tess).
    """
    parts = field_path.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False, "field_path must be section.field"
    section_name, field_name = parts[0], parts[1]

    if section_name == "extended":
        if not allow_extended:
            return False, "extended.* not writable via this endpoint (use /me/self)"
        if not isinstance(persona.extended, dict):
            persona.extended = {}
        persona.extended[field_name] = value
        return True, ""

    allowed = OVERRIDE_WHITELIST.get(section_name)
    if not allowed:
        return False, f"section '{section_name}' not editable via external push"
    if field_name not in allowed:
        return False, f"field '{field_name}' not allowed in '{section_name}'"
    section = getattr(persona, section_name, None)
    if section is None:
        return False, f"section '{section_name}' missing on persona"
    try:
        setattr(section, field_name, _coerce(section_name, field_name, value))
    except Exception as exc:
        return False, f"value type mismatch: {exc}"
    return True, ""


async def record_external_field(
    *,
    user_id: str,
    field_path: str,
    value: Any,
    source_type: str,
    actor: str,
    confidence: float = 1.0,
    note: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> bool:
    """Upsert авторитетного значения поля в persona_external_sources.

    Best-effort: при недоступности БД возвращает False, не кидает.
    """
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
                    INSERT INTO public.persona_external_sources
                        (user_id, tenant_id, field_path, source_type, actor,
                         value, confidence, note, recorded_at)
                    VALUES
                        (CAST(:uid AS UUID), CAST(:tid AS UUID), :fp, :st, :actor,
                         CAST(:val AS JSONB), :conf, :note, now())
                    ON CONFLICT (user_id, field_path) DO UPDATE SET
                        value       = EXCLUDED.value,
                        source_type = EXCLUDED.source_type,
                        actor       = EXCLUDED.actor,
                        confidence  = EXCLUDED.confidence,
                        note        = EXCLUDED.note,
                        recorded_at = now()
                """),
                {
                    "uid": user_id, "tid": tid, "fp": field_path,
                    "st": source_type, "actor": actor,
                    "val": json.dumps(value, ensure_ascii=False),
                    "conf": float(confidence), "note": note,
                },
            )
        return True
    except Exception as exc:
        logger.warning("record_external_field failed for %s: %s", field_path, exc)
        return False


async def list_external_fields(user_id: str) -> list[dict[str, Any]]:
    """Все авторитетные поля юзера. Best-effort → [] при сбое."""
    rows_out: list[dict[str, Any]] = []
    try:
        from backend.db.postgres import get_postgres
        from backend.db.postgres_tenant import set_persona_tenant
        from sqlalchemy import text
        pg = await get_postgres()
        async with pg.session(apply_tenant=False) as session:
            await set_persona_tenant(session, user_id)  # Phase A.6 persona-RLS
            rows = await session.execute(
                text("""
                    SELECT field_path, source_type, actor, value, confidence,
                           note, recorded_at
                    FROM public.persona_external_sources
                    WHERE user_id = CAST(:uid AS UUID)
                    ORDER BY recorded_at DESC
                """),
                {"uid": user_id},
            )
            for r in rows:
                val = r[3]
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                rows_out.append({
                    "field_path": r[0],
                    "source_type": r[1],
                    "actor": r[2],
                    "value": val,
                    "confidence": float(r[4] or 0.0),
                    "note": r[5],
                    "recorded_at": r[6].isoformat() if r[6] else None,
                })
    except Exception as exc:
        logger.warning("list_external_fields failed: %s", exc)
    return rows_out


async def reapply_external(persona: Persona, user_id: str) -> list[tuple[str, str, str]]:
    """Применить ВСЕ авторитетные значения к persona (они побеждают агрегацию).

    Возвращает [(field_path, source_type, actor)] для записи provenance.
    Вызывается из PersonaAggregator.build_for_user ПОСЛЕ агрегации из встреч.
    """
    applied: list[tuple[str, str, str]] = []
    for rec in await list_external_fields(user_id):
        ok, err = apply_field(persona, rec["field_path"], rec["value"])
        if ok:
            applied.append((rec["field_path"], rec["source_type"], rec["actor"]))
        else:
            logger.debug("reapply_external skip %s: %s", rec["field_path"], err)
    return applied


__all__ = [
    "OVERRIDE_WHITELIST",
    "SOURCE_MANUAL",
    "SOURCE_MINI_TESS",
    "SOURCE_EXTERNAL",
    "apply_field",
    "record_external_field",
    "list_external_fields",
    "reapply_external",
]
