-- ============================================================================
-- 270_temporal_store.sql — Postgres-бэкенд для TemporalTracker (эволюция
-- объектов во времени). Нужен для мульти-реплики под нагрузку 3000: per-user
-- SQLite-файлы не шарятся между нодами, Postgres — шарится + RLS-изоляция.
--
-- Дефолт продукта — по-прежнему SQLite (backend/core/temporal/temporal_tracker).
-- Postgres-путь включается флагом окружения TESSENT_TEMPORAL_BACKEND=postgres.
-- Данные переносятся скриптом scripts/migrate_temporal_to_postgres.py.
--
-- tenant_id = org_id (для компаний) ИЛИ user_id (solo) — та же семантика, что
-- у графа/вектора. RLS-политика повторяет 020_multitenant_rls.sql.
-- Идемпотентно: можно гонять много раз.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.temporal_versions (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    entity_id   TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    version     INTEGER NOT NULL,
    data        JSONB NOT NULL,
    ts          TEXT NOT NULL,          -- строковый timestamp (как в SQLite: сравнение ts <= date)
    changed_by  TEXT,
    change_type TEXT,
    UNIQUE (tenant_id, entity_id, version)
);

CREATE TABLE IF NOT EXISTS public.temporal_change_logs (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  TEXT,
    entity_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    field      TEXT NOT NULL,
    old_value  JSONB,
    new_value  JSONB,
    ts         TEXT NOT NULL,
    changed_by TEXT
);

CREATE TABLE IF NOT EXISTS public.temporal_timeline_events (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT,
    entity_id   TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    event_data  JSONB,
    event_time  TEXT NOT NULL,
    meeting_id  TEXT,
    description TEXT
);

-- Индексы (все запросы tenant-scoped)
CREATE INDEX IF NOT EXISTS temporal_versions_tenant_entity_idx
    ON public.temporal_versions (tenant_id, entity_id, version DESC);
CREATE INDEX IF NOT EXISTS temporal_versions_tenant_type_idx
    ON public.temporal_versions (tenant_id, entity_type);
CREATE INDEX IF NOT EXISTS temporal_versions_tenant_ts_idx
    ON public.temporal_versions (tenant_id, ts);
CREATE INDEX IF NOT EXISTS temporal_change_logs_tenant_entity_idx
    ON public.temporal_change_logs (tenant_id, entity_id);
CREATE INDEX IF NOT EXISTS temporal_timeline_tenant_entity_idx
    ON public.temporal_timeline_events (tenant_id, entity_id, event_time);

-- RLS: та же политика, что и у остальных tenant-scoped таблиц (020_multitenant_rls)
DO $$
DECLARE
    tbl TEXT;
    tables TEXT[] := ARRAY[
        'public.temporal_versions',
        'public.temporal_change_logs',
        'public.temporal_timeline_events'
    ];
BEGIN
    FOREACH tbl IN ARRAY tables LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
        EXECUTE format($f$
            DROP POLICY IF EXISTS tenant_isolation ON %s;
            CREATE POLICY tenant_isolation ON %s
                USING (tenant_id IS NULL
                       OR tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id IS NULL
                       OR tenant_id = current_setting('app.tenant_id', true));
        $f$, tbl, tbl);
    END LOOP;
END$$;

COMMIT;
