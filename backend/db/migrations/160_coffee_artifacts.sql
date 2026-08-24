-- Phase B.2 final: persistent coffee artifacts
--
-- Coffee orchestrator генерирует артефакты (strategic_brief, email_draft,
-- tz_document, jira_cards, summary) для каждого участника встречи.
-- До этого они жили только в памяти orchestrator'а и доставлялись в каналы —
-- если delivery упал или founder хочет re-fetch через UI, артефакт терялся.
--
-- Эта таблица даёт persistent storage для всех артефактов + tracking
-- доставок. UI/Mini Tess читают историю через GET /coffee/artifacts/{meeting_id}.

CREATE TABLE IF NOT EXISTS public.coffee_artifacts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    -- ↑ кому артефакт предназначен (один из meeting.participants)
    tenant_id       UUID NOT NULL,
    meeting_id      UUID NOT NULL,
    -- ↑ откуда сгенерировано
    role            TEXT NOT NULL,
    -- ↑ Role enum value: founder/sales/dev/pm/design/hr/other
    artifact_type   TEXT NOT NULL,
    -- ↑ strategic_brief/email_draft/tz_document/jira_cards/summary/...

    title           TEXT NOT NULL,
    content_markdown TEXT NOT NULL,
    content_structured JSONB NOT NULL DEFAULT '{}',
    -- ↑ specific-for-artifact fields: для email_draft — {subject,body},
    -- для jira_cards — {cards[]}, etc.

    -- Generation tracking
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    generation_duration_ms INTEGER NOT NULL DEFAULT 0,
    generation_cost_usd REAL NOT NULL DEFAULT 0.0,

    -- Delivery tracking
    recommended_channels TEXT[] NOT NULL DEFAULT '{}',
    recommended_executor TEXT,
    -- ↑ NULL если артефакт для человека, имя backend если для AI
    delivered_channel    TEXT,
    -- ↑ NULL если ещё не доставлено, имя канала если успешно
    delivered_at         TIMESTAMPTZ,
    delivered_external_ref TEXT,
    -- ↑ message_id из Telegram, page_id из Notion, task_id из Executor, etc.
    delivery_attempts    INTEGER NOT NULL DEFAULT 0,
    last_delivery_error  TEXT,

    -- Status: pending / delivered / failed / superseded
    status TEXT NOT NULL DEFAULT 'pending',
    -- ↑ pending — ещё не доставлено
    -- ↑ delivered — успешно прошло
    -- ↑ failed — все каналы упали, ждёт manual pickup
    -- ↑ superseded — старая версия, есть новая после re-generation

    -- При re-generation помечаем предыдущий artifact superseded
    superseded_by UUID  -- ссылка на свежий artifact
);

CREATE INDEX IF NOT EXISTS idx_coffee_artifacts_meeting
    ON public.coffee_artifacts (meeting_id, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_coffee_artifacts_user_status
    ON public.coffee_artifacts (user_id, status, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_coffee_artifacts_tenant
    ON public.coffee_artifacts (tenant_id, generated_at DESC);

-- При re-generation: старые pending → superseded автоматически
-- (через query в storage.py при сохранении нового)
