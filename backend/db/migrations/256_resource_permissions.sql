-- Migration 256: единая модель прав на ресурсы + аудит
-- (docs/ACCESS_CONTROL_DESIGN.md, S1+S5; по образцу MeetFlow
-- resource_permissions, но с одним источником правды user_id).

CREATE TABLE IF NOT EXISTS resource_permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- project | dataset | artifact | report | snapshot | session
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    -- user | team
    grantee_type TEXT NOT NULL DEFAULT 'user',
    grantee_id TEXT NOT NULL,
    -- read | write | admin
    permission_type TEXT NOT NULL DEFAULT 'read',
    granted_by TEXT NOT NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- один грант на пару (ресурс, получатель)
    UNIQUE (resource_type, resource_id, grantee_type, grantee_id)
);
CREATE INDEX IF NOT EXISTS idx_resperm_resource
    ON resource_permissions(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_resperm_grantee
    ON resource_permissions(grantee_type, grantee_id);

-- Аудит доступов/изменений прав: кто, когда, к чему, что сделал
CREATE TABLE IF NOT EXISTS permission_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,        -- grant | revoke | check_denied
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_permaudit_resource
    ON permission_audit(resource_type, resource_id, at DESC);
