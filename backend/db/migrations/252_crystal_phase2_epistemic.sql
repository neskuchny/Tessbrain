-- ============================================================================
-- TESSENT BRAIN — Business Crystal Phase 2 D: epistemic markup
--
-- Контекст: docs/INTEGRATION_STATUS.md §B (T2 D). Порт EpistemicType из
-- Tessent (core/models.py) на BWEDecisionEvent: различаем, ЧЕМ является
-- источник решения — наблюдением, выводом, чьим-то утверждением или
-- прогнозом. При синтезе observed весит выше claimed (антигаллюцинация).
--
-- Default 'claimed' — консервативно (как у Tessent): пока LLM не
-- классифицировал, считаем «кто-то утверждает». Это и есть прежнее
-- поведение (все источники равны) → обратносовместимо.
--
-- Как 250/251: CREATE TABLE IF NOT EXISTS (fresh DB — migrate до create_all),
-- затем ADD COLUMN IF NOT EXISTS. Идемпотентно.
-- ============================================================================

BEGIN;

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

-- D. Эпистемический тип источника решения.
--    observed  — прямое наблюдение / верифицируемый факт (высший вес)
--    inferred  — вывод/интерпретация системы
--    claimed   — чьё-то утверждение (default, консервативно)
--    predicted — прогноз (проверяется позже)
ALTER TABLE bwe_decision_events
    ADD COLUMN IF NOT EXISTS epistemic_type VARCHAR(20) DEFAULT 'claimed';

COMMIT;
