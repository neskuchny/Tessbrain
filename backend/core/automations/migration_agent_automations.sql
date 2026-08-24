-- ============================================================
-- TESSENT AGENT AUTOMATIONS — Миграция для Supabase (PostgreSQL)
-- ============================================================
-- Таблица для "агентных автоматизаций" в стиле OpenClaw:
-- вместо жёстких ActionType, задача описывается текстом,
-- а AI-агент сам выбирает и вызывает нужные инструменты.
-- ============================================================

-- 1. Основная таблица агентных автоматизаций
CREATE TABLE IF NOT EXISTS agent_automations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    
    -- Описание задачи
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    instruction TEXT NOT NULL,  -- Текстовая инструкция для агента (NL)
    
    -- Расписание
    schedule_type TEXT NOT NULL DEFAULT 'once',          -- once | recurring | event
    execute_at TIMESTAMPTZ,                              -- Для once: когда выполнить
    cron_expression TEXT,                                 -- Для recurring: cron
    interval_seconds INT,                                -- Для recurring: интервал
    
    -- Event-triggered
    trigger_type TEXT NOT NULL DEFAULT 'schedule',       -- schedule | event
    event_type TEXT,                                     -- Для event: meeting_ended, task_overdue, etc.
    event_filter JSONB DEFAULT '{}',                     -- Фильтр на payload события
    
    -- Условия (проверяются ПЕРЕД выполнением)
    conditions JSONB DEFAULT '[]',
    
    -- AI конфигурация
    model_tier TEXT NOT NULL DEFAULT 'standard',          -- standard | premium
    max_react_steps INT NOT NULL DEFAULT 5,              -- Лимит шагов ReAct loop
    
    -- Статус
    status TEXT NOT NULL DEFAULT 'active',               -- active | paused | completed | failed | cancelled
    last_run_at TIMESTAMPTZ,
    last_run_status TEXT,                                -- success | failed | skipped
    last_error TEXT,
    run_count INT NOT NULL DEFAULT 0,
    
    -- Ограничения
    max_runs INT,                                        -- NULL = без ограничений
    expires_at TIMESTAMPTZ,
    
    -- Trace последнего выполнения
    last_run_trace JSONB DEFAULT '[]',                   -- [{step, tool, args, observation, thought}]
    
    -- LLM Usage tracking
    total_tokens_used INT NOT NULL DEFAULT 0,
    total_cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    last_run_tokens INT NOT NULL DEFAULT 0,
    last_run_cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    
    -- Метаданные
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Таблица логов выполнения агентных автоматизаций
CREATE TABLE IF NOT EXISTS agent_automation_logs (
    id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL REFERENCES agent_automations(id) ON DELETE CASCADE,
    
    -- Результат
    status TEXT NOT NULL,                                -- success | failed | skipped
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    duration_ms INT,
    
    -- Trace (полный)
    trace JSONB DEFAULT '[]',                            -- [{step, tool, args, observation, thought}]
    final_answer TEXT DEFAULT '',
    error_message TEXT,
    
    -- LLM Usage
    input_tokens INT NOT NULL DEFAULT 0,
    output_tokens INT NOT NULL DEFAULT 0,
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0.0,
    model_tier TEXT DEFAULT 'standard'
);

-- ============================================================
-- Индексы
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_aa_user_id ON agent_automations(user_id);
CREATE INDEX IF NOT EXISTS idx_aa_status ON agent_automations(status);
CREATE INDEX IF NOT EXISTS idx_aa_schedule_type ON agent_automations(schedule_type);
CREATE INDEX IF NOT EXISTS idx_aa_execute_at ON agent_automations(execute_at) WHERE execute_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_aa_cron ON agent_automations(cron_expression) WHERE cron_expression IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_aa_event_type ON agent_automations(event_type) WHERE event_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_aa_created_at ON agent_automations(created_at);

CREATE INDEX IF NOT EXISTS idx_aal_automation_id ON agent_automation_logs(automation_id);
CREATE INDEX IF NOT EXISTS idx_aal_started_at ON agent_automation_logs(started_at);
CREATE INDEX IF NOT EXISTS idx_aal_status ON agent_automation_logs(status);

-- ============================================================
-- Готово!
-- ============================================================
