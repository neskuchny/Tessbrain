-- Migration: реконсиляция схемы подписок knowledge_sync с кодом.
-- Код (create/list/patch подписок, knowledge_sync.py) использует колонки
-- subscription_type / auto_process_new_meetings / include_subfolders и НЕ
-- шлёт name — а в 003 их нет, зато name NOT NULL. На свежей БД создание
-- подписки падало. Здесь добиваем недостающее (идемпотентно).
--
-- ПРИМЕЧАНИЕ: в живой БД (проект MeetFlow) эти колонки уже добавлены руками,
-- а колонка `name` полностью удалена — поэтому name-часть обёрнута в guard,
-- чтобы миграция не падала ни на свежей (name NOT NULL есть), ни на живой
-- (name нет) БД.

ALTER TABLE public.knowledge_sync_subscriptions
    ADD COLUMN IF NOT EXISTS subscription_type TEXT NOT NULL DEFAULT 'project',
    ADD COLUMN IF NOT EXISTS auto_process_new_meetings BOOLEAN NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS include_subfolders BOOLEAN NOT NULL DEFAULT false;

-- Снять NOT NULL с name ТОЛЬКО если колонка существует (иначе no-op).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'knowledge_sync_subscriptions'
          AND column_name = 'name'
    ) THEN
        ALTER TABLE public.knowledge_sync_subscriptions ALTER COLUMN name DROP NOT NULL;
    END IF;
END $$;

-- Индекс под глобальный safety-поллер (активные авто-подписки).
CREATE INDEX IF NOT EXISTS idx_knowledge_sync_subs_active_auto
    ON public.knowledge_sync_subscriptions (status, auto_process_new_meetings)
    WHERE status = 'active';
