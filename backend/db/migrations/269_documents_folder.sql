-- Migration 269: папки для сгенерированных документов и организация
-- библиотеки («нужно чтобы для документов можно было ещё папки сделать»).
-- Supabase-only (таблица documents создаётся в 001): применять в
-- Supabase SQL Editor. Идемпотентна. До применения код сохраняет
-- документы без folder (защитный ретрай в doc_store.save_document).

ALTER TABLE public.documents
    ADD COLUMN IF NOT EXISTS folder TEXT DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_documents_folder
    ON public.documents(user_id, folder);
