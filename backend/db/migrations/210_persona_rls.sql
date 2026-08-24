-- ============================================================================
-- TESSENT BRAIN — Persona-таблицы под Row-Level Security (Phase A.6)
--
-- Контекст: persona-таблицы (personas, persona_field_history,
-- persona_observations, persona_external_sources) были намеренно вне
-- RLS-набора миграции 020. У них tenant_id UUID NOT NULL, и де-факто
-- tenant_id == user_id (PK personas = user_id; ни один caller не передаёт
-- реальный tenant — всегда фолбэк `tenant_id or user_id`).
--
-- Почему НЕ переиспользуем стандартный public.tenant_rls_predicate (020):
-- тот пропускает строки через `target_tenant IS NULL` (legacy-окно). У
-- persona НЕТ NULL-строк (NOT NULL), поэтому при невыставленном app.tenant_id
-- стандартный предикат сделал бы ВСЕ строки невидимыми → сломал бы чтение
-- (cognitive_load читает persona_observations с apply_tenant=False и т.д.).
--
-- Поэтому persona-предикат использует ДРУГУЮ поблажку: escape-hatch на
-- НЕВЫСТАВЛЕННЫЙ app.tenant_id (а не на NULL-строку):
--   • app.tenant_id не выставлен  → пропускаем всё (текущее поведение,
--     ни один из 37 apply_tenant=False читателей не ломается);
--   • app.tenant_id выставлен      → строгое совпадение tenant_id (изоляция).
--
-- Так RLS — defense-in-depth поверх уже существующей app-фильтрации по
-- user_id: persona-сессии явно выставляют app.tenant_id = свой tid (=user_id),
-- и СУБД блокирует cross-user чтение даже если в каком-то запросе забыли
-- WHERE user_id. До адаптации (или для admin/backup) — поведение прежнее.
--
-- Идемпотентно: можно гонять много раз.
-- ============================================================================

BEGIN;

-- Persona-специфичный предикат (escape-hatch на невыставленный setting).
CREATE OR REPLACE FUNCTION public.persona_tenant_rls_predicate(target_tenant UUID)
RETURNS BOOLEAN AS $$
    SELECT
        NULLIF(current_setting('app.tenant_id', true), '') IS NULL
        OR target_tenant::text = NULLIF(current_setting('app.tenant_id', true), '');
$$ LANGUAGE sql STABLE;

DO $$
DECLARE
    tbl TEXT;
    tables TEXT[] := ARRAY[
        'public.personas',
        'public.persona_field_history',
        'public.persona_observations',
        'public.persona_external_sources'
    ];
    pol_name TEXT := 'tenant_isolation';
BEGIN
    FOREACH tbl IN ARRAY tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema || '.' || table_name = tbl
        ) THEN
            EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
            -- FORCE — приложение подключается как owner-роль (tessent); без
            -- FORCE owner обходит RLS и политика была бы no-op.
            EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
            EXECUTE format('DROP POLICY IF EXISTS %I ON %s', pol_name, tbl);
            EXECUTE format(
                'CREATE POLICY %I ON %s '
                'USING (public.persona_tenant_rls_predicate(tenant_id)) '
                'WITH CHECK (public.persona_tenant_rls_predicate(tenant_id))',
                pol_name, tbl
            );
        END IF;
    END LOOP;
END$$;

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DO $$ DECLARE tbl TEXT; tables TEXT[] := ARRAY[
--     'public.personas','public.persona_field_history',
--     'public.persona_observations','public.persona_external_sources']; BEGIN
--   FOREACH tbl IN ARRAY tables LOOP
--     EXECUTE format('ALTER TABLE %s NO FORCE ROW LEVEL SECURITY', tbl);
--     EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', tbl);
--     EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', tbl);
--   END LOOP;
-- END$$;
-- DROP FUNCTION IF EXISTS public.persona_tenant_rls_predicate(UUID);
-- COMMIT;
