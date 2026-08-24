-- Migration 091: сгенерированные документы/регламенты в БД (было — один
-- JSON-файл на процесс).
--
-- Проблема JSON-стора: у каждого granian-воркера свой in-memory dict +
-- общий файл без локов → документ, созданный воркером A, невидим воркеру B
-- («регламент не открывается»), а конкурентная запись теряла данные.
-- Единый источник правды — эта таблица.
--
-- Прогнать в Supabase (SQL Editor) проекта MeetFlow.

CREATE TABLE IF NOT EXISTS generated_documents (
    document_id       TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    document_type     TEXT NOT NULL DEFAULT 'regulation',
    version           TEXT NOT NULL DEFAULT '1.0',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary           TEXT DEFAULT '',
    topic             TEXT DEFAULT '',
    content_markdown  TEXT DEFAULT '',
    content_html      TEXT DEFAULT '',
    keywords          JSONB DEFAULT '[]',
    source_meetings   JSONB DEFAULT '[]',
    authors           JSONB DEFAULT '[]',
    sections          JSONB DEFAULT '[]',
    table_of_contents JSONB DEFAULT '[]',
    status            TEXT DEFAULT 'draft',
    confidence        REAL DEFAULT 0.0,
    word_count        INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_generated_documents_user
    ON generated_documents(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generated_documents_type
    ON generated_documents(user_id, document_type);

COMMENT ON TABLE generated_documents IS 'Документы/регламенты, сгенерированные из встреч (DocumentGenerationSystem)';
