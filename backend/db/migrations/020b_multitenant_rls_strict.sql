-- ============================================================================
-- TESSENT BRAIN — Multi-tenant RLS strict-mode (Phase 7e finalization)
--
-- Применять ПОСЛЕ:
--   1. `020_multitenant_rls.sql` — добавил tenant_id колонку и базовую RLS
--      политику с поблажкой `tenant_id IS NULL OR matches`.
--   2. backfill (см. `020a_multitenant_backfill.sql.template`) — все строки
--      должны иметь не-NULL tenant_id.
--
-- Что делает:
--   - Проверяет что во всех tenant-scoped таблицах нет NULL tenant_id.
--     Если есть — миграция падает, RAISE EXCEPTION + ROLLBACK. Это явная
--     защита от случайного запуска strict-mode без backfill'а.
--   - ALTER COLUMN tenant_id SET NOT NULL — теперь любой INSERT без
--     tenant_id будет отброшен СУБД, а не RLS-политикой.
--   - DROP старой политики `tenant_isolation` (с NULL-поблажкой) и
--     CREATE новой `tenant_isolation_strict`: только `tenant_id =
--     current_setting('app.tenant_id', true)`. Никаких NULL.
--   - Обновляет функцию `tenant_rls_predicate(target)` — убирает первое
--     условие `target IS NULL OR ...`.
--
-- Идемпотентно: можно гонять много раз, повторный запуск ничего не сломает
-- (повторный CREATE POLICY заменит существующую).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Pre-flight: убедиться что нигде не осталось NULL tenant_id.
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
    null_count BIGINT;
    offenders TEXT := '';
BEGIN
    FOREACH tbl IN ARRAY tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema || '.' || table_name = tbl
        ) THEN
            EXECUTE format(
                'SELECT COUNT(*) FROM %s WHERE tenant_id IS NULL',
                tbl
            ) INTO null_count;
            IF null_count > 0 THEN
                offenders := offenders || tbl || '=' || null_count || ' ';
            END IF;
        END IF;
    END LOOP;

    IF offenders <> '' THEN
        RAISE EXCEPTION
            'Cannot enable strict tenant RLS: rows with NULL tenant_id remain (%). '
            'Run backfill (020a_multitenant_backfill.sql.template) first.',
            trim(offenders);
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 2. ALTER tenant_id SET NOT NULL по всем таблицам.
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
            EXECUTE format('ALTER TABLE %s ALTER COLUMN tenant_id SET NOT NULL', tbl);
        END IF;
    END LOOP;
END$$;

-- ---------------------------------------------------------------------------
-- 3. Обновляем predicate: убираем NULL-поблажку.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.tenant_rls_predicate(target_tenant TEXT)
RETURNS BOOLEAN AS $$
    -- strict: NULL больше не пропускается; пустая app.tenant_id тоже фейлит.
    SELECT
        target_tenant IS NOT NULL
        AND target_tenant = NULLIF(current_setting('app.tenant_id', true), '');
$$ LANGUAGE sql STABLE;

-- ---------------------------------------------------------------------------
-- 4. Пересоздаём политику. Имя меняется на `tenant_isolation_strict`,
-- старая `tenant_isolation` дропается, чтобы оператор по имени видел, в
-- каком режиме сейчас стоит RLS.
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
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', tbl);
            EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_strict ON %s', tbl);
            EXECUTE format(
                'CREATE POLICY tenant_isolation_strict ON %s '
                'USING (public.tenant_rls_predicate(tenant_id)) '
                'WITH CHECK (public.tenant_rls_predicate(tenant_id))',
                tbl
            );
        END IF;
    END LOOP;
END$$;

COMMIT;

-- ============================================================================
-- ОТКАТ к permissive-режиму (если что-то пошло не так):
-- ============================================================================
-- BEGIN;
-- CREATE OR REPLACE FUNCTION public.tenant_rls_predicate(target_tenant TEXT)
-- RETURNS BOOLEAN AS $$
--   SELECT
--     target_tenant IS NULL
--     OR target_tenant = NULLIF(current_setting('app.tenant_id', true), '');
-- $$ LANGUAGE sql STABLE;
--
-- DO $$
-- DECLARE tbl TEXT; tables TEXT[] := ARRAY[/* ... */];
-- BEGIN
--   FOREACH tbl IN ARRAY tables LOOP
--     EXECUTE format('DROP POLICY IF EXISTS tenant_isolation_strict ON %s', tbl);
--     EXECUTE format('CREATE POLICY tenant_isolation ON %s '
--                    'USING (public.tenant_rls_predicate(tenant_id)) '
--                    'WITH CHECK (public.tenant_rls_predicate(tenant_id))', tbl);
--     EXECUTE format('ALTER TABLE %s ALTER COLUMN tenant_id DROP NOT NULL', tbl);
--   END LOOP;
-- END$$;
-- COMMIT;
