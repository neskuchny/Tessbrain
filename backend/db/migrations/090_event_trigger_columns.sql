-- Migration 090: событийные триггеры и цепочки для классических автоматизаций
--
-- Код (automation_service.create_automation) давно пишет trigger_type /
-- event_type / event_filter / conditions / chain_* — но миграции на эти
-- колонки НЕ БЫЛО (001 создавала только базовую схему). Итог: любой insert
-- событийной автоматизации (шаблон «Ко всем новым встречам», условия,
-- цепочки) падал Supabase 400 «column not found».
--
-- Прогнать в Supabase (SQL Editor или psql) проекта MeetFlow.

ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS trigger_type VARCHAR(20) NOT NULL DEFAULT 'schedule';
ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS event_type VARCHAR(100);
ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS event_filter JSONB;
ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS conditions JSONB;
ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS chain_next UUID;
ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS chain_parent UUID;
ALTER TABLE scheduled_automations
    ADD COLUMN IF NOT EXISTS chain_pass_result BOOLEAN DEFAULT FALSE;

-- Событийные автоматизации ищутся при рестарте планировщика
CREATE INDEX IF NOT EXISTS idx_automation_trigger
    ON scheduled_automations(trigger_type)
    WHERE trigger_type <> 'schedule';

COMMENT ON COLUMN scheduled_automations.trigger_type IS 'schedule (по времени) | event (по событию, напр. meeting_ended)';
COMMENT ON COLUMN scheduled_automations.event_type IS 'Тип события для trigger_type=event: meeting_ended, ...';
