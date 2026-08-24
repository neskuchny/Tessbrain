-- ============================================================================
-- TESSENT BRAIN — Multi-tenant RLS migration (Phase 7)
--
-- Применяется опционально. После апгрейда:
--   1. Backend в каждой сессии Postgres должен выполнить
--      `SET LOCAL app.tenant_id = '<tenant-uuid>'` (см. db/postgres_tenant.py).
--   2. Без этого SET ни одна RLS-политика не пропустит запрос к
--      tenant-scoped таблицам — кроме legacy строк с tenant_id IS NULL,
--      которые остаются видимыми (миграционное окно).
--   3. После backfill всех строк выполните 020b_multitenant_rls_strict.sql,
--      чтобы превратить tenant_id в NOT NULL и убрать «нулевую» поблажку.
--
-- Идемпотентно: можно гонять много раз.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Добавляем колонку tenant_id в каждую tenant-scoped таблицу.
--    Сейчас все эти таблицы уже имеют user_id; tenant_id — это группа из
--    нескольких user_id, нужная для team / org-уровня изоляции.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    tbl TEXT;
    tables TEXT[] := ARRAY[
        'public.documents',
        'public.scheduled_automations',
        'public.automation_execution_log',
        'public.knowledge_sync_subscriptions',
        'public.processed_meetings',
        'public.sima_projects',
        'public.sima_blocks',
        'public.sima_connections',
        'public.sima_messages',
        'public.bwe_decision_events',
        'public.bwe_outcome_events',
        'public.bwe_wisdom_patterns'
    ];
BEGIN
    FOREACH tbl IN ARRAY tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema || '.' || table_name = tbl
        ) THEN
            EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS tenant_id TEXT', tbl);
            EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %s (tenant_id)',
                           replace(tbl, 'public.', '') || '_tenant_id_idx', tbl);
        END IF;
    END LOOP;
END$$;

-- ---------------------------------------------------------------------------
-- 2. Включаем RLS и создаём политику «tenant_id IS NULL OR matches setting».
--    Имя политики едино, чтобы её было легко обновлять/дропать.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.tenant_rls_predicate(target_tenant TEXT)
RETURNS BOOLEAN AS $$
    SELECT
        target_tenant IS NULL
        OR target_tenant = NULLIF(current_setting('app.tenant_id', true), '');
$$ LANGUAGE sql STABLE;

DO $$
DECLARE
    tbl TEXT;
    tables TEXT[] := ARRAY[
        'public.documents',
        'public.scheduled_automations',
        'public.automation_execution_log',
        'public.knowledge_sync_subscriptions',
        'public.processed_meetings',
        'public.sima_projects',
        'public.sima_blocks',
        'public.sima_connections',
        'public.sima_messages',
        'public.bwe_decision_events',
        'public.bwe_outcome_events',
        'public.bwe_wisdom_patterns'
    ];
    pol_name TEXT := 'tenant_isolation';
BEGIN
    FOREACH tbl IN ARRAY tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema || '.' || table_name = tbl
        ) THEN
            EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
            -- FORCE — иначе superuser/owner всё равно видит всё, что
            -- неинформативно тестировать.
            EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
            -- Дропаем старую политику, если есть, и создаём заново.
            EXECUTE format(
                'DROP POLICY IF EXISTS %I ON %s', pol_name, tbl
            );
            EXECUTE format(
                'CREATE POLICY %I ON %s '
                'USING (public.tenant_rls_predicate(tenant_id)) '
                'WITH CHECK (public.tenant_rls_predicate(tenant_id))',
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
-- DO $$ DECLARE tbl TEXT; tables TEXT[] := ARRAY[...]; BEGIN
--   FOREACH tbl IN ARRAY tables LOOP
--     EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', tbl);
--     EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', tbl);
--   END LOOP;
-- END$$;
-- DROP FUNCTION IF EXISTS public.tenant_rls_predicate(TEXT);
-- COMMIT;
