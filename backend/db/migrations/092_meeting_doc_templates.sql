-- Migration 092: сохранённые шаблоны документов «по встрече» (Режим A).
--
-- Пользователь загружает свой DOCX-шаблон (договор/КП/карточка с {{полями}})
-- и сохраняет как именованный — чтобы переиспользовать, а не грузить файл
-- каждый раз. Сам DOCX хранится base64 в text-колонке (шаблоны небольшие,
-- десятки КБ; отдельный Storage-бакет не нужен).
--
-- Прогнать в Supabase (SQL Editor) проекта MeetFlow.

CREATE TABLE IF NOT EXISTS meeting_doc_templates (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'custom',   -- kp | invoice | contract | card | custom
    docx_base64 TEXT NOT NULL DEFAULT '',
    placeholders JSONB DEFAULT '[]',              -- список полей (кеш для UI)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_meeting_doc_templates_user
    ON meeting_doc_templates(user_id, created_at DESC);

COMMENT ON TABLE meeting_doc_templates IS 'Именованные DOCX-шаблоны для генерации документов по встрече (Режим A)';
