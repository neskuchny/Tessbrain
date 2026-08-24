-- Migration 268: тикеты обратной связи в Supabase (апгрейд с JSONL-MVP)
--
-- Цикл обратной связи (guide_feedback): жалобы/пожелания пользователей с
-- LLM-триажом «где потенциальная поломка» → разработчикам. JSONL-стор был
-- MVP (не мультитенантен, set_status переписывал весь файл); эта таблица —
-- primary, JSONL остаётся fallback'ом при недоступном Supabase.
--
-- Supabase-only (как 003/250/267): применять в Supabase SQL Editor,
-- локальный runner пропускает (_SKIP_NAMES в migrate.py). Идемпотентна.

CREATE TABLE IF NOT EXISTS public.feedback_tickets (
    id          TEXT PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT NOW(),
    user_id     TEXT,
    kind        TEXT DEFAULT 'problem',   -- problem | wish | confusion
    message     TEXT,
    area        JSONB DEFAULT '[]',
    triage      TEXT,
    context     JSONB DEFAULT '{}',
    status      TEXT DEFAULT 'open',      -- open | resolved | dismissed
    resolution  TEXT DEFAULT '',
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_feedback_user   ON public.feedback_tickets(user_id);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON public.feedback_tickets(status);
CREATE INDEX IF NOT EXISTS idx_feedback_ts     ON public.feedback_tickets(ts);
