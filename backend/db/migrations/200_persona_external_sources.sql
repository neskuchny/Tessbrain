-- 200_persona_external_sources.sql
-- Authoritative per-field overrides & external pushes for Persona.
--
-- Зачем: до этого manual-override (POST /persona/me/override) применялся
-- только к JSONB-блобу personas.payload, а PersonaAggregator.build_for_user
-- на следующем rebuild ПЕРЕЗАТИРАЛ его данными из встреч (provenance MANUAL
-- не уважался — это был latent data-loss баг). Плюс не было канала, чтобы
-- внешний клиент (Mini Tess) запушил профиль сотрудника целиком (S1/S2/S3 +
-- extended cogni/biography), переживающий rebuild.
--
-- Решение: durable-таблица «авторитетных» значений полей. И /me/override
-- (source_type='manual'), и новый /me/self (source_type='mini_tess_input'
-- /'external') пишут сюда. Aggregator после агрегации из встреч RE-APPLY'ит
-- эти значения последними → они всегда побеждают. UNIQUE(user_id, field_path)
-- — одно актуальное значение на поле (upsert), история по-прежнему в
-- persona_field_history.

CREATE TABLE IF NOT EXISTS public.persona_external_sources (
    id           BIGSERIAL PRIMARY KEY,
    user_id      UUID NOT NULL,
    tenant_id    UUID NOT NULL,
    field_path   TEXT NOT NULL,          -- "development.active_goals", "extended.cogni"
    source_type  TEXT NOT NULL,          -- 'manual' | 'mini_tess_input' | 'external'
    actor        TEXT NOT NULL,          -- "user:<uuid>" | "mini_tess" | external actor
    value        JSONB NOT NULL,         -- авторитетное значение поля
    confidence   REAL NOT NULL DEFAULT 1.0,
    note         TEXT,                   -- рацио/комментарий (→ raw_evidence в audit)
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uniq_persona_external UNIQUE (user_id, field_path)
);

CREATE INDEX IF NOT EXISTS idx_persona_external_user
    ON public.persona_external_sources (user_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_persona_external_tenant
    ON public.persona_external_sources (tenant_id, recorded_at DESC);
