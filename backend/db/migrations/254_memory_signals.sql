-- ============================================================================
-- TESSENT BRAIN — Memory signals (B1 bi-temporal + B2 retrieval-as-learning)
--
-- Контекст: ТЗ_AGI/01_IDEAL_MEMORY §1-2. Две идеи идеальной памяти, которых
-- у BWE не было:
--   B2 retrieval-as-learning: извлекли кристалл → access_count++ → медленнее
--      забывается (сопротивление decay). Утилитарный сигнал из самих запросов.
--   B1 bi-temporal: known_from (когда МЫ узнали) уже есть = created_at;
--      добавляем valid_until (до КОГДА решение/контекст истинно). Решения с
--      истёкшим контекстом не формируют/обновляют кристаллы.
--
-- Как 250-253: ПОЛНОЕ зеркало таблиц (fresh DB — migrate до create_all),
-- затем ADD COLUMN IF NOT EXISTS. Всё nullable/с дефолтом. Идемпотентно.
-- ============================================================================

BEGIN;

-- ── B2: кристаллы ──────────────────────────────────────────────────────────
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
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS access_count INTEGER DEFAULT 0;
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ;

-- ── B1: решения ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bwe_decision_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL,
    meeting_id  VARCHAR(255),
    meeting_source VARCHAR(500),
    situation   TEXT NOT NULL,
    tension_type VARCHAR(100),
    options_considered JSONB,
    decision    TEXT NOT NULL,
    rationale   TEXT,
    company_vitek INTEGER DEFAULT 1 CHECK (company_vitek BETWEEN 1 AND 4),
    market_context TEXT,
    decision_confidence VARCHAR(20) DEFAULT 'medium'
                        CHECK (decision_confidence IN ('low', 'medium', 'high')),
    situation_embedding JSONB,
    pattern_id  VARCHAR(255),
    outcome_linked BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bwe_de_user_id ON bwe_decision_events(user_id);
ALTER TABLE bwe_decision_events
    ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ;

COMMIT;
