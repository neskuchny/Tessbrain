-- Migration 257: команды для модели прав (ACCESS_CONTROL_DESIGN, S3).
-- Источник правды user_id один — Supabase UUID (не повторяем двух-ID
-- наследие MeetFlow).

CREATE TABLE IF NOT EXISTS teams (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id TEXT,                       -- опционально: команда внутри org
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_teams_org ON teams(org_id);

CREATE TABLE IF NOT EXISTS team_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    -- роль определяет ПОТОЛОК прав на ресурсах команды
    -- (viewer→read, member→write, admin/owner→admin)
    role TEXT NOT NULL DEFAULT 'member',
    added_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);
