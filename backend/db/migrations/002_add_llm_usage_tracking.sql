-- Migration: Add LLM Usage Tracking to Scheduled Automations
-- Добавляет колонки для отслеживания использования LLM в автоматизациях

-- Добавляем колонки для трекинга LLM
ALTER TABLE scheduled_automations 
ADD COLUMN IF NOT EXISTS total_tokens_used INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS total_cost_usd DECIMAL(10, 6) DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS last_run_tokens INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS last_run_cost_usd DECIMAL(10, 6) DEFAULT 0.0;

-- Комментарии
COMMENT ON COLUMN scheduled_automations.total_tokens_used IS 'Общее количество использованных токенов LLM';
COMMENT ON COLUMN scheduled_automations.total_cost_usd IS 'Общая стоимость использования LLM в USD';
COMMENT ON COLUMN scheduled_automations.last_run_tokens IS 'Токены использованные в последнем запуске';
COMMENT ON COLUMN scheduled_automations.last_run_cost_usd IS 'Стоимость последнего запуска в USD';

