-- ============================================================================
-- TESSENT BRAIN — Messenger inbound gateway (W30)
--
-- Хранит:
--   1) `messenger_links` — связки (tenant_id, user_id, platform, external_id)
--      одной записью на пару (например: user X ↔ telegram chat_id 12345).
--   2) `messenger_onboarding_tokens` — одноразовые токены для self-service
--      привязки. Поток: user в UI → /onboarding/start → token → передаёт
--      боту через `/start <token>` → backend redeem'ит → создаёт link.
--
-- RLS-изолировано по tenant_id (tenant_rls_predicate из W4).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.messenger_links (
    id                TEXT PRIMARY KEY,
    tenant_id         TEXT,
    user_id           TEXT NOT NULL,
    platform          TEXT NOT NULL,                -- telegram / slack / whatsapp
    external_id       TEXT NOT NULL,                -- chat_id / slack_user_id / phone
    external_username TEXT,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    linked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ,
    UNIQUE (platform, external_id)                  -- один external_id = один user
);

CREATE INDEX IF NOT EXISTS messenger_links_user_idx
    ON public.messenger_links (tenant_id, user_id, platform);
CREATE INDEX IF NOT EXISTS messenger_links_lookup_idx
    ON public.messenger_links (platform, external_id)
    WHERE active = TRUE;

ALTER TABLE public.messenger_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messenger_links FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.messenger_links;
CREATE POLICY tenant_isolation ON public.messenger_links
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

-- === Onboarding tokens =====================================================

CREATE TABLE IF NOT EXISTS public.messenger_onboarding_tokens (
    token        TEXT PRIMARY KEY,                  -- 32 hex chars (URL-safe)
    tenant_id    TEXT,
    user_id      TEXT NOT NULL,
    platform     TEXT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ,
    used_by_external_id TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messenger_tokens_user_idx
    ON public.messenger_onboarding_tokens (tenant_id, user_id);
CREATE INDEX IF NOT EXISTS messenger_tokens_expiry_idx
    ON public.messenger_onboarding_tokens (expires_at)
    WHERE used_at IS NULL;

ALTER TABLE public.messenger_onboarding_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.messenger_onboarding_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.messenger_onboarding_tokens;
CREATE POLICY tenant_isolation ON public.messenger_onboarding_tokens
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP POLICY IF EXISTS tenant_isolation ON public.messenger_onboarding_tokens;
-- ALTER TABLE public.messenger_onboarding_tokens DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.messenger_onboarding_tokens;
-- DROP POLICY IF EXISTS tenant_isolation ON public.messenger_links;
-- ALTER TABLE public.messenger_links DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.messenger_links;
-- COMMIT;
