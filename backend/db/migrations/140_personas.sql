-- Phase A.2: persona persistence
-- Контракт стабильный (см. backend/core/persona/models.py).
-- Один blob на user_id с tenant_id для RLS совместимости.
--
-- Идемпотентно: при повторном применении ничего не ломает.

CREATE TABLE IF NOT EXISTS public.personas (
    user_id        UUID PRIMARY KEY,
    tenant_id      UUID NOT NULL,
    schema_version TEXT NOT NULL DEFAULT '1.0',
    payload        JSONB NOT NULL,
    -- ↑ полный сериализованный Persona.to_public_dict() — изменения
    -- контракта требуют bump schema_version и миграции payload'а.

    source_meetings_count INTEGER NOT NULL DEFAULT 0,
    confidence            REAL    NOT NULL DEFAULT 0.0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_personas_tenant
    ON public.personas (tenant_id);

CREATE INDEX IF NOT EXISTS idx_personas_updated_at
    ON public.personas (updated_at DESC);

-- Provenance-история храним отдельно — позволяет audit «как менялись поля».
-- Mini Tess через будущий /persona/me/audit сможет показать timeline.
CREATE TABLE IF NOT EXISTS public.persona_field_history (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    tenant_id   UUID NOT NULL,
    field_path  TEXT NOT NULL,
    -- ↑ dotted-path: "communication_cognitive.preferred_style"
    source_type TEXT NOT NULL,
    -- ↑ enum: capture_agent / manual / mini_tess_input / inferred / external
    agent_or_actor TEXT NOT NULL,
    meeting_ids    TEXT[] NOT NULL DEFAULT '{}',
    confidence     REAL NOT NULL DEFAULT 1.0,
    raw_evidence   TEXT,
    -- ↑ короткий snippet для UI «откуда это знает Tessent»
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_persona_field_history_user_field
    ON public.persona_field_history (user_id, field_path, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_persona_field_history_tenant
    ON public.persona_field_history (tenant_id, recorded_at DESC);
