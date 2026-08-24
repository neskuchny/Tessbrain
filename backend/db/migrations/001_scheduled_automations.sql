-- Migration: Scheduled Automations
-- Таблица для хранения отложенных и повторяющихся задач автоматизации

-- ============================================
-- Scheduled Automations (Отложенные и повторяющиеся задачи)
-- ============================================
CREATE TABLE IF NOT EXISTS scheduled_automations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    
    -- Владелец задачи
    user_id VARCHAR(255) NOT NULL,
    
    -- Информация о задаче
    name VARCHAR(255) NOT NULL,                    -- Название задачи
    description TEXT,                              -- Описание
    action_type VARCHAR(100) NOT NULL,             -- Тип действия: send_telegram, create_calendar_event, create_notion_page, etc.
    action_data JSONB DEFAULT '{}',                -- Параметры действия (JSON)
    
    -- Планирование
    schedule_type VARCHAR(50) NOT NULL DEFAULT 'once',  -- once | recurring
    execute_at TIMESTAMPTZ,                        -- Время выполнения (для once)
    cron_expression VARCHAR(100),                  -- Cron выражение (для recurring): "0 9 * * 1" = каждый понедельник в 9:00
    interval_seconds INTEGER,                      -- Альтернатива cron: интервал в секундах
    
    -- Статус выполнения
    status VARCHAR(50) DEFAULT 'active',           -- active | paused | completed | failed | cancelled
    last_run_at TIMESTAMPTZ,                       -- Последнее выполнение
    last_run_status VARCHAR(50),                   -- success | failed
    last_run_result JSONB,                         -- Результат последнего выполнения
    last_error TEXT,                               -- Текст последней ошибки
    run_count INTEGER DEFAULT 0,                   -- Количество выполнений
    
    -- Ограничения
    max_runs INTEGER,                              -- Максимальное количество выполнений (NULL = бесконечно)
    expires_at TIMESTAMPTZ,                        -- Срок действия задачи
    
    -- Метаданные
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Связь с исходным сообщением/сессией (опционально)
    source_message TEXT,                           -- Исходное сообщение пользователя
    session_id VARCHAR(255)                        -- ID сессии чата
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_automation_user ON scheduled_automations(user_id);
CREATE INDEX IF NOT EXISTS idx_automation_status ON scheduled_automations(status);
CREATE INDEX IF NOT EXISTS idx_automation_execute_at ON scheduled_automations(execute_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_automation_schedule_type ON scheduled_automations(schedule_type);

-- ============================================
-- Automation Execution Log (История выполнения)
-- ============================================
CREATE TABLE IF NOT EXISTS automation_execution_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    automation_id UUID NOT NULL REFERENCES scheduled_automations(id) ON DELETE CASCADE,
    
    -- Результат выполнения
    status VARCHAR(50) NOT NULL,                   -- success | failed | skipped
    result JSONB,                                  -- Результат (JSON)
    error_message TEXT,                            -- Сообщение об ошибке
    
    -- Время
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,                           -- Длительность в миллисекундах
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exec_log_automation ON automation_execution_log(automation_id);
CREATE INDEX IF NOT EXISTS idx_exec_log_created ON automation_execution_log(created_at DESC);

-- ============================================
-- Триггер для обновления updated_at
-- ============================================
CREATE OR REPLACE FUNCTION update_automation_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_automation_updated_at ON scheduled_automations;
CREATE TRIGGER trigger_automation_updated_at
    BEFORE UPDATE ON scheduled_automations
    FOR EACH ROW
    EXECUTE FUNCTION update_automation_updated_at();

-- ============================================
-- Комментарии
-- ============================================
COMMENT ON TABLE scheduled_automations IS 'Отложенные и повторяющиеся задачи автоматизации';
COMMENT ON COLUMN scheduled_automations.action_type IS 'Тип действия: send_telegram, create_calendar_event, create_notion_page, send_email, create_yougile_task';
COMMENT ON COLUMN scheduled_automations.schedule_type IS 'once - одноразовая задача, recurring - повторяющаяся';
COMMENT ON COLUMN scheduled_automations.cron_expression IS 'Cron формат: минуты часы день_месяца месяц день_недели';


