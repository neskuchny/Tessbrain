-- Migration 260: Analysis Engine (Playbook + Run)
--
-- Система повторяемого анализа больших документов «с точки зрения компании».
-- Сотрудник настраивает методику разбора (playbook) один раз, гоняет на
-- документах разных клиентов (run). Первый таргет — due-diligence/аудит.
--
-- Org-scoped (Wave 2): playbook'и и run'ы принадлежат org. RBAC по
-- access_level (P1 #5) применяется на уровне приложения.
--
-- Идемпотентна: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

-- === Playbooks: методика разбора (настраивается раз) ======================
CREATE TABLE IF NOT EXISTS public.analysis_playbooks (
    id               TEXT PRIMARY KEY,
    org_id           UUID,                          -- NULL = personal/solo
    name             TEXT NOT NULL,
    analysis_type    TEXT NOT NULL DEFAULT 'due_diligence',
    description      TEXT DEFAULT '',
    -- dimensions: [{key,label,rationale,extraction_hint,kind,computation,
    --              inputs[],benchmark{},weight}]
    dimensions       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- company_context: {use_snapshot,use_rule_book,custom}
    company_context  JSONB NOT NULL DEFAULT '{}'::jsonb,
    reference_report TEXT,                          -- few-shot пример отчёта
    output_template  JSONB NOT NULL DEFAULT '{}'::jsonb,  -- структура вывода
    created_by       TEXT,
    access_level     INTEGER NOT NULL DEFAULT 2,    -- P1 #5 RBAC (INTERNAL)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_analysis_playbooks_org
    ON public.analysis_playbooks (org_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_playbooks_type
    ON public.analysis_playbooks (analysis_type);

COMMENT ON TABLE public.analysis_playbooks IS
    'Reusable document-analysis methodology (dimensions + company context + output template).';

-- === Runs: один прогон playbook'а на документах ===========================
CREATE TABLE IF NOT EXISTS public.analysis_runs (
    id                  TEXT PRIMARY KEY,
    playbook_id         TEXT NOT NULL REFERENCES public.analysis_playbooks(id) ON DELETE CASCADE,
    org_id              UUID,
    user_id             TEXT,                       -- кто запустил
    input_document_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    client_context      TEXT,                       -- что обсуждалось с клиентом
    status              TEXT NOT NULL DEFAULT 'pending',
    -- steps_log: [{phase,action,detail,ts}] — петля «достал/посчитал/положил»
    steps_log           JSONB NOT NULL DEFAULT '[]'::jsonb,
    extracted_facts     JSONB NOT NULL DEFAULT '{}'::jsonb,
    computed_metrics    JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_report        JSONB,                      -- {markdown,sections,score,verdict}
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_analysis_runs_org
    ON public.analysis_runs (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_playbook
    ON public.analysis_runs (playbook_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_status
    ON public.analysis_runs (status)
    WHERE status NOT IN ('done', 'failed');

COMMENT ON TABLE public.analysis_runs IS
    'One execution of an analysis playbook against documents; tracks the extract→compute→analyze→assemble loop.';

-- RLS: org-scoped доступ обеспечивается на уровне приложения (как у
-- остального Wave 2 слоя). Здесь не включаем strict RLS — admin-канал
-- (service-role) пишет, читатели фильтруются route-логикой по org_id +
-- access_level. Полный RLS — отдельная Tenant-RLS wave при необходимости.
