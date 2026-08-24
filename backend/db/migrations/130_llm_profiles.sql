-- ============================================================================
-- TESSENT BRAIN — Per-tenant LLM profiles (W42)
--
-- Позволяет switch'ить активный LLM provider/endpoint в runtime через UI:
--   - managed API (gemini / openai)
--   - local self-hosted (vLLM / Ollama / TGI)
--   - RunPod cloud pod (OpenAI-compat URL)
--   - TurboQuant (наш fork с квантизацией, OpenAI-compat URL)
--
-- API key хранится зашифрованным через pgcrypto (W11) — отдельная column
-- bytea, не plaintext в config'е. Расшифровка только в backend через
-- pgp_sym_decrypt с master key из ENV.
--
-- RLS: каждый tenant видит только свои профили. Один is_default per tenant.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.llm_profiles (
    id              TEXT PRIMARY KEY,                  -- llm_<hex>
    tenant_id       TEXT,                              -- RLS isolation
    name            TEXT NOT NULL,                     -- "RunPod Qwen3 30B"
    provider        TEXT NOT NULL,                     -- gemini/openai/local_vllm/runpod/turboquant
    base_url        TEXT,                              -- OpenAI-compatible endpoint (NULL для managed gemini/openai)
    model           TEXT NOT NULL,                     -- "Qwen/Qwen3-30B-A3B" / "gpt-4o-mini" / etc
    api_key_encrypted BYTEA,                           -- pgp_sym_encrypt(...) — может быть NULL для local_vllm без auth
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    is_fallback     BOOLEAN NOT NULL DEFAULT FALSE,    -- secondary автоматически активируется при failure default
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,-- temperature/max_tokens defaults, custom params
    last_health_check_at TIMESTAMPTZ,
    last_health_ok  BOOLEAN,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name),
    CHECK (provider IN ('gemini', 'openai', 'local_vllm', 'runpod', 'turboquant'))
);

CREATE INDEX IF NOT EXISTS llm_profiles_tenant_idx
    ON public.llm_profiles (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS llm_profiles_default_idx
    ON public.llm_profiles (tenant_id)
    WHERE is_default = TRUE;
CREATE INDEX IF NOT EXISTS llm_profiles_fallback_idx
    ON public.llm_profiles (tenant_id)
    WHERE is_fallback = TRUE;

ALTER TABLE public.llm_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.llm_profiles FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.llm_profiles;
CREATE POLICY tenant_isolation ON public.llm_profiles
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

-- updated_at autobump.
CREATE OR REPLACE FUNCTION public.llm_profiles_touch()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS llm_profiles_touch ON public.llm_profiles;
CREATE TRIGGER llm_profiles_touch
    BEFORE UPDATE ON public.llm_profiles
    FOR EACH ROW EXECUTE FUNCTION public.llm_profiles_touch();

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TRIGGER IF EXISTS llm_profiles_touch ON public.llm_profiles;
-- DROP FUNCTION IF EXISTS public.llm_profiles_touch();
-- DROP POLICY IF EXISTS tenant_isolation ON public.llm_profiles;
-- ALTER TABLE public.llm_profiles DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.llm_profiles;
-- COMMIT;
