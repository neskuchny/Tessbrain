-- ============================================================================
-- TESSENT BRAIN — Business Crystal Phase 1 (порт механизмов из Tessent)
--
-- Контекст: docs/INTEGRATION_STATUS.md §B. Однонаправленный порт зрелых,
-- заземлённых механизмов Tessent (agi_small_tess) в BWE-кристалл. Phase 1:
--   A. Time-decay уверенности с contradiction-multiplier
--      (адаптация modules/apoptosis.py: pc·(1−rate·mult)^days_stale).
--      У Tessent это ночной job; у нас нет фонового движка BWE, поэтому
--      decay применяется ЛЕНИВО на чтении (ранжирование), а здесь храним
--      только ЯКОРЬ свежести — last_calibrated_at (когда исходы последний
--      раз меняли распределение). contradiction_ratio берётся из
--      outcome_distribution на лету, отдельная колонка не нужна.
--   B. evolution_log — версионированная история переформулировок кристалла
--      (адаптация modules/concept_crystallizer.py: ["v1: …","v2: …"]).
--
-- Обратная совместимость: обе колонки nullable / с дефолтом. OFF
-- (crystal_enrichment_enabled=False) → не пишутся и не читаются. Старые
-- читатели не зависят. Идемпотентно (ADD COLUMN IF NOT EXISTS).
-- ============================================================================

BEGIN;

-- Гарантия существования таблицы (как в 250: fresh DB — migrate до create_all).
CREATE TABLE IF NOT EXISTS bwe_wisdom_patterns (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 VARCHAR(255) NOT NULL,
    pattern_name            VARCHAR(200) NOT NULL,
    prototype_vector        JSONB NOT NULL DEFAULT '[]'::jsonb,
    instances               JSONB DEFAULT '[]'::jsonb,
    outcome_distribution    JSONB DEFAULT '{}'::jsonb,
    vitek_sensitivity       TEXT,
    context_differentiators JSONB DEFAULT '[]'::jsonb,
    confidence              VARCHAR(20) DEFAULT 'low',
    needs_analysis_update   BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- A. Якорь свежести для time-decay. NULL → кристалл ещё не калибровался
--    исходами; decay-хелпер трактует NULL как «не угасать» (back-compat).
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS last_calibrated_at TIMESTAMPTZ;

-- B. История эволюции summary/differentiators: ["v1: …", "v2: …"].
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS evolution_log JSONB DEFAULT '[]'::jsonb;

COMMIT;
