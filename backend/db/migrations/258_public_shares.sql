-- Migration 258: публичные ссылки на одиночные ресурсы (S4).
-- В отличие от sharing.py bundles — share на ОДИН ресурс с токеном/
-- TTL/паролем/лимитами просмотров (как meeting_shares у MeetFlow).

CREATE TABLE IF NOT EXISTS resource_shares (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    share_token TEXT NOT NULL UNIQUE,
    access_level TEXT NOT NULL DEFAULT 'read',   -- read | comment
    created_by TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    max_views INTEGER,
    current_views INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT,                          -- bcrypt; NULL = без пароля
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_resource_shares_resource
    ON resource_shares(resource_type, resource_id, is_active);

CREATE TABLE IF NOT EXISTS resource_share_logs (
    id BIGSERIAL PRIMARY KEY,
    share_id UUID NOT NULL REFERENCES resource_shares(id) ON DELETE CASCADE,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_share_logs_share ON resource_share_logs(share_id, at DESC);

-- S6: численные лимиты тарифа поверх флагов
CREATE TABLE IF NOT EXISTS tenant_limits (
    tenant_id TEXT PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free',
    monthly_tokens_usd NUMERIC,                  -- 0/NULL = без лимита
    datasets_max INTEGER,
    projects_max INTEGER,
    storage_mb INTEGER,
    seats INTEGER,
    can_use_premium_tier BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
