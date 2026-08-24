-- issue #110 пакет 4.a: PG-зеркало схемы llm_usage из SqliteUsageStore.
-- Создаётся для подготовки prod-окружения. PostgresUsageStore (пакет 4.b)
-- будет писать сюда через PostgresClient.session().
--
-- Совместимо с RLS: tenant_id колонка + индекс. Применить вручную через
-- backend/db/migrate.py при готовности подключить PG-store.

CREATE TABLE IF NOT EXISTS llm_usage (
    id BIGSERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,                      -- ISO-8601 UTC, оставляем TEXT
                                                  -- для битового совпадения со SQLite
                                                  -- (миграция SQLite→PG → COPY без cast)
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    model_tier TEXT DEFAULT 'standard',

    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,

    input_cost DOUBLE PRECISION DEFAULT 0.0,
    output_cost DOUBLE PRECISION DEFAULT 0.0,
    cached_cost DOUBLE PRECISION DEFAULT 0.0,
    total_cost DOUBLE PRECISION DEFAULT 0.0,

    user_id TEXT,
    session_id TEXT,
    agent_mode TEXT,
    request_type TEXT,
    tenant_id TEXT,

    -- issue #110 пакет 1: атрибуция «по сущности»
    document_id TEXT,
    meeting_id TEXT,
    job_id TEXT,
    surface TEXT,

    success SMALLINT DEFAULT 1,
    error TEXT,
    latency_ms INTEGER DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_timestamp   ON llm_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user_id     ON llm_usage(user_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_model       ON llm_usage(model);
CREATE INDEX IF NOT EXISTS idx_llm_usage_agent_mode  ON llm_usage(agent_mode);
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_id   ON llm_usage(tenant_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_document
    ON llm_usage(tenant_id, document_id) WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_meeting
    ON llm_usage(tenant_id, meeting_id) WHERE meeting_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_usage_tenant_job
    ON llm_usage(tenant_id, job_id)     WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_usage_surface
    ON llm_usage(surface)               WHERE surface IS NOT NULL;

-- RLS: tenant-isolation политика. Раньше делегировалась 020c_llm_usage_rls.sql,
-- но 020c сортируется ДО 261 → на свежей БД таблицы ещё нет, 020c становится
-- no-op и помечается применённой, и RLS не навешивался (leak). Поэтому включаем
-- RLS прямо здесь, сразу после создания таблицы. tenant_rls_predicate уже есть
-- из 020 (020 < 261). Идемпотентно. На уровне PG-store (пакет 4.b) тенант берётся
-- из observability.tenant_context и применяется через SET LOCAL app.tenant_id
-- (см. backend/db/postgres_tenant.py).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'tenant_rls_predicate'
    ) THEN
        EXECUTE 'ALTER TABLE public.llm_usage ENABLE ROW LEVEL SECURITY';
        -- FORCE — иначе owner всё равно видит всё (нужно для cross-tenant тестов).
        EXECUTE 'ALTER TABLE public.llm_usage FORCE ROW LEVEL SECURITY';
        EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON public.llm_usage';
        EXECUTE
            'CREATE POLICY tenant_isolation ON public.llm_usage '
            'USING (public.tenant_rls_predicate(tenant_id)) '
            'WITH CHECK (public.tenant_rls_predicate(tenant_id))';
    END IF;
END$$;
