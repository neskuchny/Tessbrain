-- Migration 259: интеграция с Daily Pulse (INTEGRATION_TESSENT, фазы 0-2).
-- Идея: identity-маппинг внешних систем + лог входящих событий
-- (event_log тоже копит, но это сырая аудит-лента для отладки/реплея).

-- Универсальный маппинг внешних идентификаторов (Daily Pulse, MeetFlow и т.п.)
CREATE TABLE IF NOT EXISTS external_identity_mapping (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_system TEXT NOT NULL,        -- 'daily_pulse' | 'meetflow' | ...
    external_id TEXT NOT NULL,
    tessent_user_id TEXT NOT NULL,
    email TEXT,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (external_system, external_id)
);
CREATE INDEX IF NOT EXISTS idx_extid_tessent
    ON external_identity_mapping(tessent_user_id);
CREATE INDEX IF NOT EXISTS idx_extid_email
    ON external_identity_mapping(email);

-- Сырой лог входящих интеграционных событий (для отладки/реплея;
-- агрегированные факты идут параллельно в event_log/insights)
CREATE TABLE IF NOT EXISTS integration_events (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    event TEXT NOT NULL,
    org_ref TEXT,
    team_ref TEXT,
    user_ref TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    signature_ok BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_integration_events_source_time
    ON integration_events(source, received_at DESC);
