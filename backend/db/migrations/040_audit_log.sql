-- ============================================================================
-- TESSENT BRAIN — Audit log (W9 enterprise-pack)
--
-- Append-only журнал событий безопасности и data-mutation. Используется для:
-- - SOC 2 CC7.2 (System Operations / Logging),
-- - GDPR Art. 30 (record of processing activities),
-- - incident-response forensics.
--
-- Tenant-scoped: каждое событие принадлежит ровно одному tenant_id, RLS
-- закрывает cross-tenant чтение.
--
-- Идемпотентно. После применения этой миграции `audit_log.emit()` начнёт
-- писать сюда; до миграции — пишет только в structlog (no-op в БД).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.audit_events (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    TEXT,                                  -- NOT NULL после strict-migration
    user_id      TEXT,                                  -- субъект действия
    actor_role   TEXT,                                  -- VIEWER/EDITOR/ADMIN/OWNER (W9 RBAC)
    action       TEXT NOT NULL,                         -- e.g. 'project.delete', 'gdpr.export'
    resource     TEXT,                                  -- e.g. 'project:abc-123'
    request_id   TEXT,                                  -- coordinate с structlog
    ip           INET,                                  -- client IP
    user_agent   TEXT,
    payload_hash TEXT,                                  -- SHA-256 от значимой нагрузки (без PII)
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,    -- произвольные key-value
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индексы под типовые запросы аудита.
CREATE INDEX IF NOT EXISTS audit_events_tenant_idx
    ON public.audit_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_user_idx
    ON public.audit_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_action_idx
    ON public.audit_events (action, created_at DESC);

-- Append-only enforcement: запрещаем UPDATE и DELETE на уровне БД.
-- Это критично для compliance — без этого триггер audit-log можно
-- "стереть" через `DELETE FROM audit_events`.
CREATE OR REPLACE FUNCTION public.audit_events_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% denied)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_no_update ON public.audit_events;
CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON public.audit_events
    FOR EACH ROW EXECUTE FUNCTION public.audit_events_immutable();

DROP TRIGGER IF EXISTS audit_events_no_delete ON public.audit_events;
CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON public.audit_events
    FOR EACH ROW EXECUTE FUNCTION public.audit_events_immutable();

-- RLS: каждый tenant видит только свои события.
ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.audit_events;
CREATE POLICY tenant_isolation ON public.audit_events
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

COMMIT;

-- ============================================================================
-- ОТКАТ (для прода — НЕ выполнять без архивирования содержимого таблицы!)
-- ============================================================================
-- BEGIN;
-- DROP POLICY IF EXISTS tenant_isolation ON public.audit_events;
-- ALTER TABLE public.audit_events DISABLE ROW LEVEL SECURITY;
-- DROP TRIGGER IF EXISTS audit_events_no_update ON public.audit_events;
-- DROP TRIGGER IF EXISTS audit_events_no_delete ON public.audit_events;
-- DROP FUNCTION IF EXISTS public.audit_events_immutable();
-- DROP TABLE IF EXISTS public.audit_events;
-- COMMIT;
