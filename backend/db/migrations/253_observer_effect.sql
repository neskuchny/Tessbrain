-- ============================================================================
-- TESSENT BRAIN — Observer Effect (D1, уникальный вклад BWE)
--
-- Контекст: docs/INTEGRATION_STATUS.md §D1. Observer effect (§3.6 диагноза
-- Tessent) — дрейф «что говорят на встречах» vs «что произошло». У BWE есть
-- ОБЕ стороны сравнения: решение/ситуация (что сказано) + BWEOutcomeEvent
-- (что произошло). Не хватало одного — ОЖИДАНИЯ на момент решения.
--
-- predicted_outcome фиксирует, каким встреча ВИДЕЛА исход в момент решения:
--   success   — ожидали, что сработает
--   failure   — шли на риск, ожидали проблемы
--   uncertain — без явного ожидания (default, консервативно)
-- Сравнение predicted vs фактический outcome_type (BWEOutcomeEvent) даёт
-- drift: систематическая переоценка (predicted=success, actual=failure) —
-- сигнатура observer-effect (встречи как перформанс, не отражение реальности).
--
-- Как 250-252: CREATE TABLE IF NOT EXISTS + ADD COLUMN IF NOT EXISTS.
-- Идемпотентно. Default 'uncertain' → обратносовместимо (нет ожидания = нет
-- вклада в drift).
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

-- D1. Ожидаемый на момент решения исход (для дрейфа ожидание−факт).
ALTER TABLE bwe_decision_events
    ADD COLUMN IF NOT EXISTS predicted_outcome VARCHAR(20) DEFAULT 'uncertain';

COMMIT;
