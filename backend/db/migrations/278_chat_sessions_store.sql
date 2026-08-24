-- 278_chat_sessions_store.sql — D1c: чат-сессии и реестр папок в Postgres.
--
-- Раньше: data/chat_sessions/<user_id>/<session_id>.json + _folders.json —
-- переписки на файлах (конкурентность, multi-node, ретеншн — мимо БД).
-- Теперь: строка-на-сессию, doc JSONB = тот же объект, что лежал в файле
-- (логика sessions.py не меняется — маршрутизируются только хелперы IO).
--
-- user_id — тенант-ключ: все выборки идут строго с фильтром по user_id
-- (изоляция на слое приложения, как у остальных per-user сторов; клиент
-- ходит владельцем БД, RLS здесь не несущая).

CREATE TABLE IF NOT EXISTS chat_session_store (
    user_id     TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    doc         JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_session_user ON chat_session_store (user_id);

-- Реестр папок чатов: одна строка на пользователя, doc = {"folders": [...]}.
CREATE TABLE IF NOT EXISTS chat_folder_store (
    user_id     TEXT PRIMARY KEY,
    doc         JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
