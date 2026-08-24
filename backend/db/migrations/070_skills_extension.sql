-- ============================================================================
-- TESSENT BRAIN — Skills extension over agent_automations (W16)
--
-- Расширяет существующую таблицу agent_automations skill-полями.
-- Skill = AgentAutomation с is_skill=true, параметризованной instruction
-- (через template) и явным parameters_schema для UI и validation.
--
-- Дизайн:
-- - НЕ новая таблица — переиспользуем agent_automations и весь её
--   runtime (ReAct loop, scheduler, trace, cost tracking).
-- - is_skill=false по умолчанию: existing записи остаются "обычными
--   автоматизациями". Skill — это просто шаблон для повторного запуска
--   с подстановкой параметров.
-- - parameters_schema хранится как JSONB: {name, type, default, required}.
-- - parent_skill_id + version дают простую history (без git/branching).
--
-- Применяется через `python -m backend.db.migrate`.
-- Идемпотентно через `IF NOT EXISTS`.
-- ============================================================================

BEGIN;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS is_skill BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS instruction_template TEXT;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS parameters_schema JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS parent_skill_id TEXT;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS shared_in_tenant BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.agent_automations
    ADD COLUMN IF NOT EXISTS source_conversation_id TEXT;

-- Индекс для быстрых list-skills запросов: WHERE is_skill=true AND tenant_id=?
CREATE INDEX IF NOT EXISTS agent_automations_skills_idx
    ON public.agent_automations (is_skill, user_id)
    WHERE is_skill = TRUE;

-- Индекс для chat-trigger lookup по имени.
CREATE INDEX IF NOT EXISTS agent_automations_skill_name_idx
    ON public.agent_automations (lower(name))
    WHERE is_skill = TRUE;

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP INDEX IF EXISTS agent_automations_skill_name_idx;
-- DROP INDEX IF EXISTS agent_automations_skills_idx;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS source_conversation_id;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS shared_in_tenant;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS version;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS parent_skill_id;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS parameters_schema;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS instruction_template;
-- ALTER TABLE public.agent_automations DROP COLUMN IF EXISTS is_skill;
-- COMMIT;
