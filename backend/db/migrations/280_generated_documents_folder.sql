-- Migration 280: колонка folder у СГЕНЕРИРОВАННЫХ документов
-- (generated_documents). Миграция 269 добавляла folder только в таблицу
-- documents (база знаний) — а doc_store.save_document/set_folder пишут
-- folder в generated_documents, которой колонки не досталось. Реальный
-- кейс: PGRST204 «Could not find the 'folder' column of
-- 'generated_documents'» валил сохранение регламента из встреч.
-- Идемпотентна; применять в Supabase SQL Editor. До применения код
-- сохраняет документы без folder (защитный ретрай в doc_store).

ALTER TABLE public.generated_documents
    ADD COLUMN IF NOT EXISTS folder TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_generated_documents_folder
    ON public.generated_documents(user_id, folder);
