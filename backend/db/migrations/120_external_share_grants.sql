-- ============================================================================
-- TESSENT BRAIN — Per-document external share grants (W31)
--
-- Поверх category-уровня external_access (W18) добавляем гранулярный
-- per-resource шеринг.
--
-- Поток:
--   1) Owner в UI выбирает 1+ ресурс (document / meeting / collection)
--      и email получателя, длительность.
--   2) Backend создаёт share_grant'ы (по одному на ресурс) с одним общим
--      grant_bundle_id и issues один token URL.
--   3) Получатель открывает /shared/<token>, вводит email, мы проверяем
--      совпадение с grantee_email, выдаём short-lived viewer JWT
--      (audience='shared-viewer'), который ограничен scope'ом bundle'а.
--   4) Каждое открытие пишется в share_audit_views.
--
-- RLS: основной grant'ом владеет tenant_id (изолирован по предикату).
-- Partner-side доступ контролируется application-level через
-- bundle_id + email match, не RLS — getгрантов делается под service-role.
-- ============================================================================

BEGIN;

-- === Bundles =============================================================
-- Один логический "share" = bundle (Alice → bob@partner.com на 30 дней),
-- содержит N grants (по 1 на ресурс).

CREATE TABLE IF NOT EXISTS public.share_bundles (
    id              TEXT PRIMARY KEY,                 -- shr_<hex>
    tenant_id       TEXT,
    owner_user_id   TEXT NOT NULL,
    grantee_email   TEXT NOT NULL,                    -- lowercased
    grantee_org     TEXT,                             -- optional partner org name (UI hint)
    note            TEXT NOT NULL DEFAULT '',         -- "Q4 board pack for investor"
    token           TEXT NOT NULL UNIQUE,             -- 32 hex chars (URL-safe)
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS share_bundles_tenant_idx
    ON public.share_bundles (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS share_bundles_owner_idx
    ON public.share_bundles (owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS share_bundles_token_active_idx
    ON public.share_bundles (token)
    WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS share_bundles_grantee_idx
    ON public.share_bundles (lower(grantee_email))
    WHERE revoked_at IS NULL;

ALTER TABLE public.share_bundles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.share_bundles FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.share_bundles;
CREATE POLICY tenant_isolation ON public.share_bundles
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));


-- === Per-resource grants =================================================
-- Каждая запись = "у этого bundle есть доступ к этому document/meeting/etc".
-- Bundle может содержать N разных ресурсов, partner видит их все.

CREATE TABLE IF NOT EXISTS public.share_grants (
    id              TEXT PRIMARY KEY,
    bundle_id       TEXT NOT NULL REFERENCES public.share_bundles(id) ON DELETE CASCADE,
    tenant_id       TEXT,
    resource_type   TEXT NOT NULL,                    -- document / meeting / collection
    resource_id     TEXT NOT NULL,
    permissions     TEXT NOT NULL DEFAULT 'read',     -- read / read+download (future: comment)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bundle_id, resource_type, resource_id)
);

CREATE INDEX IF NOT EXISTS share_grants_bundle_idx
    ON public.share_grants (bundle_id);
CREATE INDEX IF NOT EXISTS share_grants_resource_idx
    ON public.share_grants (resource_type, resource_id);

ALTER TABLE public.share_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.share_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.share_grants;
CREATE POLICY tenant_isolation ON public.share_grants
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));


-- === Audit views =========================================================
-- Каждое открытие /shared/<token> или последующий просмотр ресурса.
-- Append-only через триггер (как audit_events).

CREATE TABLE IF NOT EXISTS public.share_audit_views (
    id              TEXT PRIMARY KEY,
    bundle_id       TEXT NOT NULL,                    -- не FK: bundle может быть удалён, audit остаётся
    tenant_id       TEXT,
    grantee_email   TEXT,
    action          TEXT NOT NULL,                    -- landing_open / email_verified / resource_view
    resource_type   TEXT,
    resource_id     TEXT,
    ip_address      TEXT,
    user_agent      TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS share_audit_bundle_idx
    ON public.share_audit_views (bundle_id, created_at DESC);
CREATE INDEX IF NOT EXISTS share_audit_tenant_idx
    ON public.share_audit_views (tenant_id, created_at DESC);

CREATE OR REPLACE FUNCTION public.share_audit_views_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'share_audit_views is append-only (% denied)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS share_audit_views_no_update ON public.share_audit_views;
CREATE TRIGGER share_audit_views_no_update
    BEFORE UPDATE ON public.share_audit_views
    FOR EACH ROW EXECUTE FUNCTION public.share_audit_views_immutable();
DROP TRIGGER IF EXISTS share_audit_views_no_delete ON public.share_audit_views;
CREATE TRIGGER share_audit_views_no_delete
    BEFORE DELETE ON public.share_audit_views
    FOR EACH ROW EXECUTE FUNCTION public.share_audit_views_immutable();

ALTER TABLE public.share_audit_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.share_audit_views FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.share_audit_views;
CREATE POLICY tenant_isolation ON public.share_audit_views
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP POLICY IF EXISTS tenant_isolation ON public.share_audit_views;
-- DROP TRIGGER IF EXISTS share_audit_views_no_update ON public.share_audit_views;
-- DROP TRIGGER IF EXISTS share_audit_views_no_delete ON public.share_audit_views;
-- DROP FUNCTION IF EXISTS public.share_audit_views_immutable();
-- ALTER TABLE public.share_audit_views DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.share_audit_views;
-- DROP POLICY IF EXISTS tenant_isolation ON public.share_grants;
-- ALTER TABLE public.share_grants DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.share_grants;
-- DROP POLICY IF EXISTS tenant_isolation ON public.share_bundles;
-- ALTER TABLE public.share_bundles DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.share_bundles;
-- COMMIT;
