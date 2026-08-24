-- ============================================================================
-- TESSENT BRAIN — Bookmarks registry (W17)
--
-- Хранит URL-закладки (презентации, методологии, спецификации) с описанием
-- и тегами. Векторное представление для семантического поиска живёт в
-- Qdrant (collection `bookmarks_<tenant_id>`); здесь — primary metadata.
--
-- Дизайн:
-- - Tenant-scoped: RLS привязывает строки к tenant_id, как user_profiles_v2.
-- - section_id — опциональная привязка к разделу "регламенты/документы"
--   (UUID/string из DocumentsPanel; freeform — мы не валидируем).
-- - tags TEXT[] — для list-фильтров (быстрее чем JSONB для маленьких массивов).
-- - qdrant_point_id хранит UUID точки в Qdrant; NULL пока не индексировано.
-- - last_fetched_at + content_summary — кэш auto-enrich.
-- - status: active | broken | archived — для periodic re-crawl.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bookmarks (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT,
    user_id         TEXT NOT NULL,                -- кто добавил
    section_id      TEXT,                          -- "regulations", "presentations", custom
    url             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    tags            TEXT[] NOT NULL DEFAULT '{}',
    content_summary TEXT,                          -- LLM-генеренный summary тела страницы
    qdrant_point_id TEXT,
    status          TEXT NOT NULL DEFAULT 'active',-- active | broken | archived
    last_fetched_at TIMESTAMPTZ,
    fetch_error     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, url)
);

-- Индексы под типовые запросы UI:
CREATE INDEX IF NOT EXISTS bookmarks_tenant_section_idx
    ON public.bookmarks (tenant_id, section_id, created_at DESC);
CREATE INDEX IF NOT EXISTS bookmarks_user_idx
    ON public.bookmarks (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS bookmarks_tags_gin
    ON public.bookmarks USING GIN (tags);
CREATE INDEX IF NOT EXISTS bookmarks_status_idx
    ON public.bookmarks (status)
    WHERE status != 'archived';

-- RLS — те же правила что в W4 (`tenant_rls_predicate`).
ALTER TABLE public.bookmarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bookmarks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.bookmarks;
CREATE POLICY tenant_isolation ON public.bookmarks
    USING (public.tenant_rls_predicate(tenant_id))
    WITH CHECK (public.tenant_rls_predicate(tenant_id));

-- Триггер updated_at
CREATE OR REPLACE FUNCTION public.bookmarks_touch()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bookmarks_touch_trg ON public.bookmarks;
CREATE TRIGGER bookmarks_touch_trg
    BEFORE UPDATE ON public.bookmarks
    FOR EACH ROW EXECUTE FUNCTION public.bookmarks_touch();

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TRIGGER IF EXISTS bookmarks_touch_trg ON public.bookmarks;
-- DROP FUNCTION IF EXISTS public.bookmarks_touch();
-- DROP POLICY IF EXISTS tenant_isolation ON public.bookmarks;
-- ALTER TABLE public.bookmarks DISABLE ROW LEVEL SECURITY;
-- DROP TABLE IF EXISTS public.bookmarks;
-- COMMIT;
