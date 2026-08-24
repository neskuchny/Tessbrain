-- ============================================================================
-- TESSENT BRAIN — Validation results store (W23)
--
-- Долгосрочное хранилище отчётов validator'а. Дополняет Redis-based
-- TaskHandle store (W19, TTL=7 дней) — здесь результаты живут постоянно
-- для аудита, аналитики, A/B-сравнения и human-review queue.
--
-- Связь с executor:
-- - `task_handle_id` — ссылка на TaskHandle (Redis может уже истёк, но
--   мы сохранили snapshot artifacts).
-- - `decision` ∈ auto_approve / human_review / reject (W21 enum).
-- - `aggregate_score` — итоговый score 0..10.
-- - `reports` — JSONB-массив ValidatorReport.to_dict().
-- - `artifacts_summary` — list metadata (без bytes/content) для UI listing.
--
-- Tenant-scoped через RLS (как audit_events / bookmarks).
-- Append-only по convention: UPDATE разрешён только для `human_decision`/
-- `reviewed_at` (после ручной проверки).
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.validation_results (
    id                 TEXT PRIMARY KEY,
    tenant_id          TEXT,
    user_id            TEXT NOT NULL,
    task_handle_id     TEXT,                                  -- nullable если standalone /run
    task_type          TEXT,                                   -- landing/api/...
    decision           TEXT NOT NULL,                          -- auto_approve | human_review | reject
    aggregate_score    NUMERIC(4, 2) NOT NULL DEFAULT 0,
    blocker_seen       BOOLEAN NOT NULL DEFAULT FALSE,
    rationale          TEXT NOT NULL DEFAULT '',
    reports            JSONB NOT NULL DEFAULT '[]'::jsonb,
    artifacts_summary  JSONB NOT NULL DEFAULT '[]'::jsonb,
    issue_count        INTEGER NOT NULL DEFAULT 0,
    -- Human review (заполняется когда оператор обработал)
    reviewed_by        TEXT,
    reviewed_at        TIMESTAMPTZ,
    human_decision     TEXT,                                   -- approve | reject | escalate
    human_notes        TEXT,
    -- Metadata
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индексы под типовые запросы:
-- 1. История юзера (UI: «мои validation runs»).
CREATE INDEX IF NOT EXISTS validation_results_user_idx
    ON public.validation_results (tenant_id, user_id, created_at DESC);

-- 2. Human-review queue.
CREATE INDEX IF NOT EXISTS validation_results_review_idx
    ON public.validation_results (tenant_id, decision, created_at DESC)
    WHERE decision = 'human_review' AND reviewed_at IS NULL;

-- 3. Lookup по task_handle_id.
CREATE INDEX IF NOT EXISTS validation_results_task_idx
    ON public.validation_results (task_handle_id)
    WHERE task_handle_id IS NOT NULL;

-- 4. Per-tenant quota — count active validations за интервал.
CREATE INDEX IF NOT EXISTS validation_results_tenant_recent_idx
    ON public.validation_results (tenant_id, created_at DESC);

-- RLS — стандартно, переиспользуем `tenant_rls_predicate`.
ALTER TABLE public.validation_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.validation_results FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.validation_results;
CREATE POLICY tenant_isolation ON public.validation_results
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

-- Триггер: разрешаем UPDATE ТОЛЬКО для human-review полей.
-- Это защита от случайной перезаписи immutable data.
CREATE OR REPLACE FUNCTION public.validation_results_protect_immutable()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.id != OLD.id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.user_id != OLD.user_id
       OR NEW.task_handle_id IS DISTINCT FROM OLD.task_handle_id
       OR NEW.task_type IS DISTINCT FROM OLD.task_type
       OR NEW.decision != OLD.decision
       OR NEW.aggregate_score != OLD.aggregate_score
       OR NEW.blocker_seen != OLD.blocker_seen
       OR NEW.rationale != OLD.rationale
       OR NEW.reports::TEXT != OLD.reports::TEXT
       OR NEW.artifacts_summary::TEXT != OLD.artifacts_summary::TEXT
       OR NEW.issue_count != OLD.issue_count
       OR NEW.created_at != OLD.created_at
    THEN
        RAISE EXCEPTION 'validation_results: immutable field modified'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS validation_results_immutable ON public.validation_results;
CREATE TRIGGER validation_results_immutable
    BEFORE UPDATE ON public.validation_results
    FOR EACH ROW EXECUTE FUNCTION public.validation_results_protect_immutable();

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TRIGGER IF EXISTS validation_results_immutable ON public.validation_results;
-- DROP FUNCTION IF EXISTS public.validation_results_protect_immutable();
-- DROP POLICY IF EXISTS tenant_isolation ON public.validation_results;
-- ALTER TABLE public.validation_results DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.validation_results;
-- COMMIT;
