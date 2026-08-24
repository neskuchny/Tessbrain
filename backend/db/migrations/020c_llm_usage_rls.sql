-- ============================================================================
-- TESSENT BRAIN — RLS на llm_usage (issue #112 hotfix; долг по issue #110)
--
-- Контекст: миграция 261_llm_usage.sql создала таблицу llm_usage с
-- tenant_id колонкой, но RLS забыли включить. Это означает, что любой
-- код, выполняющий SELECT FROM llm_usage без явного WHERE tenant_id,
-- видит расходы и метаданные ВСЕХ тенантов (см. PostgresUsageStore
-- daily_aggregate/total_aggregate/recent — leak).
--
-- Эта миграция:
--   1. Гарантирует наличие индекса по tenant_id (для производительности
--      политики).
--   2. Включает FORCE ROW LEVEL SECURITY.
--   3. Создаёт политику `tenant_isolation` через ту же предикат-функцию
--      `tenant_rls_predicate(target_tenant)` из миграции 020 — единый
--      контракт для всей системы.
--
-- Идемпотентно: можно гонять много раз.
--
-- ПОСЛЕ применения миграции backend должен передавать tenant_id из
-- contextvar в каждой сессии (см. db/postgres_tenant.set_tenant_for_session).
-- Прямой psycopg2-доступ из PostgresUsageStore — обновлён в этом же
-- коммите, чтобы выполнять SET LOCAL app.tenant_id.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Таблица llm_usage создаётся ПОЗЖЕ (261_llm_usage.sql сортируется после
-- 020c). Поэтому и индекс, и RLS применяем ТОЛЬКО если таблица уже есть —
-- иначе на свежей БД раннер падал с UndefinedTableError на CREATE INDEX.
-- Когда 261 создаст таблицу + свой tenant-индекс, эта миграция уже помечена
-- применённой; RLS для llm_usage навешивает сам 261 (см. его хвост).
-- Идемпотентно: no-op, пока таблицы нет; полноценно, когда есть.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'llm_usage'
    ) THEN
        -- 1. Индекс по tenant_id (для производительности политики).
        EXECUTE 'CREATE INDEX IF NOT EXISTS llm_usage_tenant_id_idx '
                'ON public.llm_usage (tenant_id)';
        -- 2. tenant_rls_predicate уже есть из 020. Включаем RLS + политику.
        EXECUTE 'ALTER TABLE public.llm_usage ENABLE ROW LEVEL SECURITY';
        -- FORCE — иначе owner всё равно видит всё (нужно для тестов
        -- cross-tenant isolation).
        EXECUTE 'ALTER TABLE public.llm_usage FORCE ROW LEVEL SECURITY';
        EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON public.llm_usage';
        EXECUTE
            'CREATE POLICY tenant_isolation ON public.llm_usage '
            'USING (public.tenant_rls_predicate(tenant_id)) '
            'WITH CHECK (public.tenant_rls_predicate(tenant_id))';
    END IF;
END$$;

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- ALTER TABLE public.llm_usage DISABLE ROW LEVEL SECURITY;
-- DROP POLICY IF EXISTS tenant_isolation ON public.llm_usage;
-- COMMIT;
