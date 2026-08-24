-- ============================================================================
-- TESSENT BRAIN — TZ templates (W18 improvements)
--
-- Хранит per-tenant customizable шаблоны технических заданий. Не хардкод
-- в коде, как обсуждали — компании разные, шаблоны разные.
--
-- Структура blocks JSONB:
--   [
--     {
--       "name": "hero",
--       "label": "Hero block",
--       "required": true,
--       "min_items": 1,
--       "description_for_llm": "...",
--       "fields": [
--         {"name": "headline", "required": true, "type": "text",
--          "description": "Главный заголовок ≤ 80 символов"},
--         {"name": "primary_cta", "required": true, "type": "text"}
--       ]
--     },
--     ...
--   ]
--
-- Один (tenant_id, task_type, name) — UNIQUE; default template для
-- task_type помечается `is_default=true` чтобы pipeline знал какой брать.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.tz_templates (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT,
    task_type       TEXT NOT NULL,                  -- landing/api/finmodel/...
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    blocks          JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, task_type, name)
);

CREATE INDEX IF NOT EXISTS tz_templates_tenant_type_idx
    ON public.tz_templates (tenant_id, task_type)
    WHERE is_default = TRUE;
CREATE INDEX IF NOT EXISTS tz_templates_lookup_idx
    ON public.tz_templates (tenant_id, task_type, name);

-- Гарантируем что у одного tenant'а — только один default per task_type.
-- (Делаем через partial unique index, чтобы NULL tenant_id не блокировал.)
CREATE UNIQUE INDEX IF NOT EXISTS tz_templates_one_default_idx
    ON public.tz_templates (tenant_id, task_type)
    WHERE is_default = TRUE;

ALTER TABLE public.tz_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tz_templates FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.tz_templates;
CREATE POLICY tenant_isolation ON public.tz_templates
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

CREATE OR REPLACE FUNCTION public.tz_templates_touch()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tz_templates_touch_trg ON public.tz_templates;
CREATE TRIGGER tz_templates_touch_trg
    BEFORE UPDATE ON public.tz_templates
    FOR EACH ROW EXECUTE FUNCTION public.tz_templates_touch();

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TRIGGER IF EXISTS tz_templates_touch_trg ON public.tz_templates;
-- DROP FUNCTION IF EXISTS public.tz_templates_touch();
-- DROP POLICY IF EXISTS tenant_isolation ON public.tz_templates;
-- ALTER TABLE public.tz_templates DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.tz_templates;
-- COMMIT;
