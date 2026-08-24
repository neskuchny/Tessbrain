-- ============================================================================
-- TESSENT BRAIN — User Profiles v2 (W7 user-adaptive responses)
--
-- Хранит per-user адаптационный профиль: стиль коммуникации, уровень
-- детализации, текущий фокус, экспертиза. Используется
-- core/llm/system_prompt_builder.build_personalization_preamble() для
-- обогащения system_prompt'а при `LLMRouter.generate(personalize=True)`.
--
-- Tenant-scoped: одна строка = один (tenant_id, user_id). RLS-политика
-- из 020_multitenant_rls.sql применяется автоматически после ENABLE.
--
-- Идемпотентно: можно гонять много раз.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.user_profiles_v2 (
    id           BIGSERIAL PRIMARY KEY,
    user_id      TEXT NOT NULL,
    tenant_id    TEXT,                              -- NOT NULL после strict-migration
    profile      JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Зеркала ключевых полей в колонках для быстрых фильтров (опционально).
    role         TEXT,
    department   TEXT,
    preferred_language TEXT,
    communication_style TEXT,
    detail_level TEXT,
    last_curated_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, user_id)
);

CREATE INDEX IF NOT EXISTS user_profiles_v2_user_idx
    ON public.user_profiles_v2 (user_id);
CREATE INDEX IF NOT EXISTS user_profiles_v2_tenant_idx
    ON public.user_profiles_v2 (tenant_id);
CREATE INDEX IF NOT EXISTS user_profiles_v2_profile_gin
    ON public.user_profiles_v2 USING GIN (profile jsonb_path_ops);

-- Включаем RLS совместно с политикой `tenant_isolation` из 020.
ALTER TABLE public.user_profiles_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_profiles_v2 FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.user_profiles_v2;
CREATE POLICY tenant_isolation ON public.user_profiles_v2
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

-- Триггер обновления updated_at.
CREATE OR REPLACE FUNCTION public.user_profiles_v2_touch()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_profiles_v2_touch_trg ON public.user_profiles_v2;
CREATE TRIGGER user_profiles_v2_touch_trg
    BEFORE UPDATE ON public.user_profiles_v2
    FOR EACH ROW EXECUTE FUNCTION public.user_profiles_v2_touch();

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TRIGGER IF EXISTS user_profiles_v2_touch_trg ON public.user_profiles_v2;
-- DROP FUNCTION IF EXISTS public.user_profiles_v2_touch();
-- DROP POLICY IF EXISTS tenant_isolation ON public.user_profiles_v2;
-- ALTER TABLE public.user_profiles_v2 DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.user_profiles_v2;
-- COMMIT;
