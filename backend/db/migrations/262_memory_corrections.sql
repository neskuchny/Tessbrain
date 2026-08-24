-- issue #112 T-8: memory_corrections review queue.
--
-- Хранит историю human-исправлений attribution (кто на самом деле взял
-- задачу / принял решение). Используется для:
--   1) Review queue UI (T-10) — список ожидающих проверки
--   2) Active learning — статистика 'LLM ошибся здесь' для будущего
--      улучшения extraction-промптов
--
-- Совместимо с RLS: tenant_id колонка + индекс. Применить вручную через
-- backend/db/migrate.py.

CREATE TABLE IF NOT EXISTS memory_corrections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT,

    -- Что исправляем
    entity_type VARCHAR(20) NOT NULL,   -- 'task' | 'decision' | 'mention' | 'person'
    entity_id TEXT NOT NULL,            -- id узла в Neo4j (task_id / decision_id)

    -- Оригинальная атрибуция (что система предположила)
    original_attribution TEXT,          -- имя/speaker, которое система выбрала
    original_confidence VARCHAR(10),    -- low|medium|high на момент создания
    review_reason TEXT,                 -- почему попало в очередь

    -- Исправление (что сказал человек)
    suggested_correction TEXT,          -- предложенное системой (если есть)
    correction TEXT,                    -- финальное исправление человека
    correction_action VARCHAR(20),      -- 'confirm' | 'fix' | 'delete' | 'skip'

    -- Workflow
    needs_review BOOLEAN DEFAULT TRUE,
    reviewed_by UUID,                   -- user_id проверившего
    reviewed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Очередь по тенанту (самый частый запрос: «что ждёт проверки в т-A»)
CREATE INDEX IF NOT EXISTS idx_mem_corrections_tenant_review
    ON memory_corrections(tenant_id, needs_review)
    WHERE needs_review = TRUE;

-- Дедупликация: один entity не должен дважды попадать в очередь
CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_corrections_entity
    ON memory_corrections(tenant_id, entity_type, entity_id)
    WHERE needs_review = TRUE;

-- История исправлений для active learning (по дате)
CREATE INDEX IF NOT EXISTS idx_mem_corrections_reviewed
    ON memory_corrections(tenant_id, reviewed_at)
    WHERE reviewed_at IS NOT NULL;
