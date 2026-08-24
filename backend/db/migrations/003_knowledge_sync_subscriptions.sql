-- Migration: Knowledge Sync Subscriptions
-- Создание таблиц для синхронизации знаний из встреч

-- Таблица подписок на синхронизацию
CREATE TABLE IF NOT EXISTS public.knowledge_sync_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    project_id UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    folder_id UUID REFERENCES public.folders(id) ON DELETE SET NULL,
    sync_frequency TEXT NOT NULL DEFAULT 'manual', -- manual, hourly, daily, weekly
    status TEXT NOT NULL DEFAULT 'active', -- active, paused, syncing, error
    meetings_processed INTEGER DEFAULT 0,
    last_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_knowledge_sync_subscriptions_user_id 
    ON public.knowledge_sync_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_sync_subscriptions_project_id 
    ON public.knowledge_sync_subscriptions(project_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_sync_subscriptions_status 
    ON public.knowledge_sync_subscriptions(status);

-- RLS политики
ALTER TABLE public.knowledge_sync_subscriptions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own subscriptions" 
    ON public.knowledge_sync_subscriptions FOR SELECT 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create own subscriptions" 
    ON public.knowledge_sync_subscriptions FOR INSERT 
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own subscriptions" 
    ON public.knowledge_sync_subscriptions FOR UPDATE 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own subscriptions" 
    ON public.knowledge_sync_subscriptions FOR DELETE 
    USING (auth.uid() = user_id);

-- Таблица обработанных встреч
CREATE TABLE IF NOT EXISTS public.processed_meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    meeting_id UUID NOT NULL REFERENCES public.meetings(id) ON DELETE CASCADE,
    subscription_id UUID REFERENCES public.knowledge_sync_subscriptions(id) ON DELETE SET NULL,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'completed', -- completed, error, no_transcript
    UNIQUE(user_id, meeting_id)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_processed_meetings_user_id 
    ON public.processed_meetings(user_id);
CREATE INDEX IF NOT EXISTS idx_processed_meetings_meeting_id 
    ON public.processed_meetings(meeting_id);
CREATE INDEX IF NOT EXISTS idx_processed_meetings_subscription_id 
    ON public.processed_meetings(subscription_id);

-- RLS политики
ALTER TABLE public.processed_meetings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view own processed meetings" 
    ON public.processed_meetings FOR SELECT 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can create own processed meetings" 
    ON public.processed_meetings FOR INSERT 
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own processed meetings" 
    ON public.processed_meetings FOR UPDATE 
    USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own processed meetings" 
    ON public.processed_meetings FOR DELETE 
    USING (auth.uid() = user_id);

-- Функция обновления updated_at
CREATE OR REPLACE FUNCTION update_knowledge_sync_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Триггер для обновления updated_at
DROP TRIGGER IF EXISTS trigger_knowledge_sync_updated_at ON public.knowledge_sync_subscriptions;
CREATE TRIGGER trigger_knowledge_sync_updated_at
    BEFORE UPDATE ON public.knowledge_sync_subscriptions
    FOR EACH ROW
    EXECUTE FUNCTION update_knowledge_sync_updated_at();

