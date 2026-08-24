-- Phase A.4: persisted capture-observations
--
-- Capture-агенты (32 шт в backend/core/capture/agents/) запускаются на
-- каждой встрече и извлекают rich структуры (CommunicationProfile,
-- psychological_profile, work_preferences, и т.д.). До этого они
-- возвращали выходы только в граф/extraction-results — терялись по
-- контексту «кто из участников какой».
--
-- Эта миграция даёт хранилище, привязывающее каждый capture-output к
-- конкретному (user_id, meeting_id, agent_type). PersonaAggregator
-- читает оттуда вместо realtime-LLM вызовов.
--
-- Дизайн: одна запись = один (user_id, meeting_id, agent_type), JSON-payload
-- содержит rich-структуру агента. Идемпотентно по уникальному ключу.

CREATE TABLE IF NOT EXISTS public.persona_observations (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    tenant_id   UUID NOT NULL,
    meeting_id  UUID,
    -- ↑ NULL допустим для observations извлечённых не из встречи (chat,
    -- voice note, manual entry)
    agent_type  TEXT NOT NULL,
    -- ↑ имя capture-агента: communication_style_agent,
    -- psychological_analyzer, work_preferences_agent, etc.
    payload     JSONB NOT NULL,
    -- ↑ rich-структура агента (CommunicationProfile.to_dict() etc.)
    confidence  REAL NOT NULL DEFAULT 1.0,
    -- ↑ агент сам может пометить confidence в payload, мы дублируем
    -- top-level для быстрой фильтрации
    source_type TEXT NOT NULL DEFAULT 'capture_agent',
    -- ↑ capture_agent / manual / mini_tess_input / voice_note
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Идемпотентно: при повторной обработке встречи — UPDATE существующего
    -- (user_id, meeting_id, agent_type) вместо вставки дубля
    CONSTRAINT uniq_persona_obs UNIQUE (user_id, meeting_id, agent_type)
);

CREATE INDEX IF NOT EXISTS idx_persona_obs_user_agent
    ON public.persona_observations (user_id, agent_type, extracted_at DESC);

CREATE INDEX IF NOT EXISTS idx_persona_obs_tenant
    ON public.persona_observations (tenant_id, extracted_at DESC);

CREATE INDEX IF NOT EXISTS idx_persona_obs_meeting
    ON public.persona_observations (meeting_id);
