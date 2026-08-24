"""PersonaStore — read/write persona в Postgres + in-process TTL cache.

Phase A.2: добавлена реальная postgres-persist через таблицу `public.personas`
(миграция `140_personas.sql`). До этого Persona жила только в памяти и
терялась при рестарте.

Storage:
- Postgres `public.personas` (PK=user_id): JSON blob с full Persona +
  tenant_id для RLS совместимости + schema_version для будущих миграций
- In-process TTL cache (60s) — снимает нагрузку на повторные /persona/me
- Mini Tess push в /persona/me/clients инвалидирует cache того user'а

Контракты:
- get(user_id) — None если Persona ещё не построена
- upsert(persona) — атомарный INSERT ... ON CONFLICT DO UPDATE
- record_provenance(user_id, entry) — пишет одну запись в persona_field_history
  И обновляет entries в Persona payload
- invalidate(user_id) — сбросить cache (после ручного override через UI)

Все методы never-raise (best-effort): Persona — обогащение опыта, она не
должна валить chat / login / Mini Tess push'и. При недоступности БД
fallback в in-memory как до A.2.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any, Optional

from backend.core.persona.models import (
    AbstractionLevel,
    BehavioralProfile,
    ChannelPreference,
    ClientArchetype,
    ClientInteraction,
    CommunicationCognitive,
    CommunicationStyle,
    DevelopmentProfile,
    Persona,
    PersonaProvenance,
    PersonaProvenanceEntry,
    ProvenanceSource,
    StrengthsWeaknesses,
    AreaAssessment,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────


class _MemoryCache:
    """Простой TTL-кэш. Заменим на Redis после Phase A.3, если станет узким."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, Persona]] = {}

    def get(self, user_id: str) -> Optional[Persona]:
        item = self._data.get(user_id)
        if not item:
            return None
        expires_at, persona = item
        if time.time() > expires_at:
            self._data.pop(user_id, None)
            return None
        return persona

    def set(self, user_id: str, persona: Persona) -> None:
        self._data[user_id] = (time.time() + self._ttl, persona)

    def invalidate(self, user_id: str) -> None:
        self._data.pop(user_id, None)


# ─────────────────────────────────────────────────────────────────
# Serialization: Persona ↔ JSON blob
# ─────────────────────────────────────────────────────────────────


def _persona_to_blob(p: Persona) -> dict[str, Any]:
    """Сериализация Persona в JSON для хранения в Postgres.

    Отличается от `to_public_dict()` — здесь храним ВСЕ поля, включая
    evidence_meeting_ids в provenance и raw_evidence. `to_public_dict()`
    отдаёт «чистый» вид для Mini Tess / UI без внутренних данных.
    """
    blob = dataclasses.asdict(p)
    # Enums в dataclasses.asdict превращаются в Enum-объекты — приводим
    # к строкам, чтобы JSONB лежал чистым.
    def _normalize(obj: Any) -> Any:
        if hasattr(obj, "value") and hasattr(obj, "name"):  # Enum
            return obj.value
        if isinstance(obj, dict):
            return {k: _normalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_normalize(v) for v in obj]
        return obj
    return _normalize(blob)


def _blob_to_persona(user_id: str, blob: dict[str, Any]) -> Persona:
    """Десериализация JSON-blob → Persona. Совместимо со старыми schema_version'ами."""
    p = Persona(user_id=user_id)
    p.schema_version = blob.get("schema_version", "1.0")
    p.source_meetings_count = int(blob.get("source_meetings_count", 0))
    p.confidence = float(blob.get("confidence", 0.0))
    p.created_at = blob.get("created_at", p.created_at)
    p.updated_at = blob.get("updated_at", p.updated_at)

    cc = blob.get("communication_cognitive") or {}
    p.communication_cognitive = CommunicationCognitive(
        preferred_style=_safe_enum(CommunicationStyle, cc.get("preferred_style"), CommunicationStyle.DEFAULT),
        vocabulary_register=cc.get("vocabulary_register"),
        metaphor_domains=list(cc.get("metaphor_domains") or []),
        abstraction_level=_safe_enum(AbstractionLevel, cc.get("abstraction_level"), AbstractionLevel.BALANCED),
        preferred_language=cc.get("preferred_language", "ru"),
        speech_patterns=list(cc.get("speech_patterns") or []),
    )

    bh = blob.get("behavioral") or {}
    p.behavioral = BehavioralProfile(
        peak_hours_utc=list(bh.get("peak_hours_utc") or []),
        low_load_windows_utc=list(bh.get("low_load_windows_utc") or []),
        avg_meetings_per_day=float(bh.get("avg_meetings_per_day") or 0.0),
        meeting_load_p95=float(bh.get("meeting_load_p95") or 0.0),
        channel_preferences=[
            _safe_enum(ChannelPreference, c, ChannelPreference.TELEGRAM)
            for c in bh.get("channel_preferences") or []
        ],
        response_latency_p50_min=bh.get("response_latency_p50_min"),
        context_switching_tolerance=bh.get("context_switching_tolerance"),
        do_not_disturb_windows_utc=list(bh.get("do_not_disturb_windows_utc") or []),
    )

    dv = blob.get("development") or {}
    p.development = DevelopmentProfile(
        current_projects=list(dv.get("current_projects") or []),
        active_goals=list(dv.get("active_goals") or []),
        recent_learning=list(dv.get("recent_learning") or []),
        decision_style=dv.get("decision_style"),
    )

    sw = blob.get("strengths_weaknesses") or {}
    p.strengths_weaknesses = StrengthsWeaknesses(
        strong=[_assess(a) for a in sw.get("strong") or []],
        developing=[_assess(a) for a in sw.get("developing") or []],
        blind_spots=[_assess(a) for a in sw.get("blind_spots") or []],
    )

    cp = blob.get("client_profiles") or []
    p.client_profiles = [_client_archetype(c) for c in cp]

    # Расширенные vendor-секции (extended.cogni / extended.biography и т.п.)
    ext = blob.get("extended")
    p.extended = dict(ext) if isinstance(ext, dict) else {}

    prov = blob.get("provenance") or {}
    entries_raw = prov.get("entries") if isinstance(prov, dict) and "entries" in prov else prov
    if isinstance(entries_raw, dict):
        for path, raw in entries_raw.items():
            if not isinstance(raw, dict):
                continue
            try:
                p.provenance.record(PersonaProvenanceEntry(
                    field_path=raw.get("field_path", path),
                    source_type=_safe_enum(ProvenanceSource, raw.get("source_type"), ProvenanceSource.INFERRED),
                    agent_or_actor=raw.get("agent_or_actor", "unknown"),
                    meeting_ids=list(raw.get("meeting_ids") or []),
                    extracted_at=raw.get("extracted_at", p.updated_at),
                    confidence=float(raw.get("confidence") or 1.0),
                    raw_evidence=raw.get("raw_evidence"),
                ))
            except Exception:
                logger.debug("provenance entry parse failed for %s", path, exc_info=True)
    return p


def _safe_enum(enum_cls, raw: Any, default: Any):
    if raw is None:
        return default
    try:
        return enum_cls(raw)
    except Exception:
        return default


def _assess(raw: dict[str, Any]) -> AreaAssessment:
    return AreaAssessment(
        area=str(raw.get("area", "")),
        confidence=float(raw.get("confidence") or 0.0),
        evidence_meeting_ids=list(raw.get("evidence_meeting_ids") or raw.get("evidence") or []),
    )


def _client_archetype(raw: dict[str, Any]) -> ClientArchetype:
    interactions = [
        ClientInteraction(
            date=str(i.get("date", "")),
            channel=str(i.get("channel", "unknown")),
            summary=str(i.get("summary", "")),
            sentiment=i.get("sentiment"),
        )
        for i in raw.get("interactions") or []
        if isinstance(i, dict)
    ]
    return ClientArchetype(
        client_id=str(raw.get("client_id", "")),
        name=str(raw.get("name", "")),
        archetype=raw.get("archetype"),
        industry=raw.get("industry"),
        deal_stage=raw.get("deal_stage"),
        interactions=interactions,
        founder_notes=raw.get("founder_notes"),
        extracted_features=dict(raw.get("extracted_features") or {}),
    )


# ─────────────────────────────────────────────────────────────────
# PersonaStore
# ─────────────────────────────────────────────────────────────────


class PersonaStore:
    """Persistence-слой для Persona: postgres + in-process cache.

    BEST-EFFORT: при недоступности БД все методы возвращают плюс-минус
    разумный результат и НЕ кидают (logger.warning). Persona — это
    обогащение опыта, она не должна валить login / chat / Mini Tess push.
    """

    def __init__(self) -> None:
        self._cache = _MemoryCache(ttl_seconds=60.0)

    async def get(self, user_id: str) -> Optional[Persona]:
        """Вернуть Persona или None если ещё не построена."""
        if not user_id:
            return None
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        try:
            from backend.db.postgres import get_postgres
            from backend.db.postgres_tenant import set_persona_tenant
            from sqlalchemy import text
            pg = await get_postgres()
            async with pg.session(apply_tenant=False) as session:
                # Phase A.6: активируем persona-RLS по user_id (tenant==user_id).
                await set_persona_tenant(session, user_id)
                row = await session.execute(
                    text("SELECT payload FROM public.personas WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                rec = row.first()
                if rec is None or rec[0] is None:
                    return None
                blob = rec[0]
                if isinstance(blob, str):
                    blob = json.loads(blob)
                p = _blob_to_persona(user_id, blob)
                self._cache.set(user_id, p)
                return p
        except Exception as exc:
            logger.warning("PersonaStore.get fallback to None: %s", exc)
            return None

    async def upsert(self, persona: Persona, tenant_id: Optional[str] = None) -> None:
        """Сохранить Persona целиком. tenant_id — для RLS совместимости."""
        if not persona.user_id:
            logger.warning("PersonaStore.upsert called without user_id")
            return
        # Кэш — в первую очередь, чтобы /persona/me моментально увидел изменения
        self._cache.set(persona.user_id, persona)
        try:
            from backend.db.postgres import get_postgres
            from backend.db.postgres_tenant import set_persona_tenant
            from sqlalchemy import text
            pg = await get_postgres()
            blob = _persona_to_blob(persona)
            tid = tenant_id or persona.user_id  # single-tenant fallback
            async with pg.session(apply_tenant=False) as session:
                # Phase A.6: persona-RLS — WITH CHECK требует совпадения tid.
                await set_persona_tenant(session, tid)
                await session.execute(
                    text("""
                        INSERT INTO public.personas
                            (user_id, tenant_id, schema_version, payload,
                             source_meetings_count, confidence, created_at, updated_at)
                        VALUES
                            (:uid, :tid, :sv, CAST(:payload AS JSONB),
                             :smc, :conf, now(), now())
                        ON CONFLICT (user_id) DO UPDATE SET
                            payload               = EXCLUDED.payload,
                            schema_version        = EXCLUDED.schema_version,
                            source_meetings_count = EXCLUDED.source_meetings_count,
                            confidence            = EXCLUDED.confidence,
                            updated_at            = now()
                    """),
                    {
                        "uid": persona.user_id,
                        "tid": tid,
                        "sv": persona.schema_version,
                        "payload": json.dumps(blob, ensure_ascii=False),
                        "smc": persona.source_meetings_count,
                        "conf": persona.confidence,
                    },
                )
        except Exception as exc:
            logger.warning("PersonaStore.upsert postgres failed (cache-only): %s", exc)

    async def record_provenance(
        self,
        user_id: str,
        entry: PersonaProvenanceEntry,
        tenant_id: Optional[str] = None,
    ) -> None:
        """Записать источник для одного поля Persona.

        Делаем две вещи:
        1. Append в persona_field_history (для UI audit)
        2. Обновляем entries в Persona payload (для быстрого чтения)
        """
        persona = await self.get(user_id)
        if persona is None:
            persona = Persona(user_id=user_id)
        persona.provenance.record(entry)
        await self.upsert(persona, tenant_id=tenant_id)
        # История — best-effort, ошибка не валит main flow
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
                        INSERT INTO public.persona_field_history
                            (user_id, tenant_id, field_path, source_type,
                             agent_or_actor, meeting_ids, confidence, raw_evidence)
                        VALUES
                            (:uid, :tid, :fp, :st, :actor,
                             CAST(:mids AS TEXT[]), :conf, :ev)
                    """),
                    {
                        "uid": user_id,
                        "tid": tid,
                        "fp": entry.field_path,
                        "st": entry.source_type.value,
                        "actor": entry.agent_or_actor,
                        "mids": "{" + ",".join(entry.meeting_ids) + "}",
                        "conf": entry.confidence,
                        "ev": entry.raw_evidence,
                    },
                )
        except Exception as exc:
            logger.debug("persona_field_history append failed: %s", exc)

    async def invalidate(self, user_id: str) -> None:
        self._cache.invalidate(user_id)


_default_store: Optional[PersonaStore] = None


def get_persona_store() -> PersonaStore:
    global _default_store
    if _default_store is None:
        _default_store = PersonaStore()
    return _default_store


__all__ = ["PersonaStore", "get_persona_store"]
