-- ============================================================
-- Business Wisdom Engine (BWE) — Database Schema
-- ============================================================
-- Миграция для PostgreSQL. Запускать однократно.
-- Таблицы также создаются автоматически через SQLAlchemy
-- при первом запуске если TESSENT_AUTO_MIGRATE=true
-- ============================================================

-- Слой 1: Decision Events
-- Структурированные решения, извлечённые из транскриптов встреч
CREATE TABLE IF NOT EXISTS bwe_decision_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL,
    meeting_id  VARCHAR(255),
    meeting_source VARCHAR(500),

    -- Тип ситуации (нормализованный, не конкретика)
    situation   TEXT NOT NULL,
    tension_type VARCHAR(100),   -- growth_vs_margin | speed_vs_quality | ...
    options_considered JSONB,    -- list[str]
    decision    TEXT NOT NULL,
    rationale   TEXT,

    -- Контекст компании в момент решения
    company_vitek INTEGER DEFAULT 1 CHECK (company_vitek BETWEEN 1 AND 4),
    market_context TEXT,
    decision_confidence VARCHAR(20) DEFAULT 'medium' CHECK (decision_confidence IN ('low', 'medium', 'high')),

    -- JEPA-encoded вектор ситуации (1536-dim OpenAI embedding)
    situation_embedding JSONB,

    -- Связь с паттерном (устанавливается при кластеризации)
    pattern_id  VARCHAR(255),

    -- Флаг: привязан ли исход
    outcome_linked BOOLEAN DEFAULT FALSE,

    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Уникальный индекс для идемпотентности (user_id + meeting_id + decision)
CREATE UNIQUE INDEX IF NOT EXISTS uq_bwe_de_user_meeting_decision
    ON bwe_decision_events(user_id, meeting_id, decision)
    WHERE meeting_id IS NOT NULL;

-- Индексы для bwe_decision_events
CREATE INDEX IF NOT EXISTS idx_bwe_de_user_id ON bwe_decision_events(user_id);
CREATE INDEX IF NOT EXISTS idx_bwe_de_meeting_id ON bwe_decision_events(meeting_id);
CREATE INDEX IF NOT EXISTS idx_bwe_de_pattern_id ON bwe_decision_events(pattern_id);
-- Индекс для инкрементальной кластеризации (поиск по user_id WHERE pattern_id IS NULL)
CREATE INDEX IF NOT EXISTS idx_bwe_de_unassigned ON bwe_decision_events(user_id) WHERE pattern_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_bwe_de_outcome_linked ON bwe_decision_events(outcome_linked);
CREATE INDEX IF NOT EXISTS idx_bwe_de_created_at ON bwe_decision_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bwe_de_tension_type ON bwe_decision_events(tension_type);


-- Слой 2: Outcome Events
-- Исходы принятых решений (из ретроспектив, метрик, упоминаний)
CREATE TABLE IF NOT EXISTS bwe_outcome_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL,
    decision_event_id UUID NOT NULL REFERENCES bwe_decision_events(id) ON DELETE CASCADE,
    meeting_id  VARCHAR(255),

    outcome_type VARCHAR(20) NOT NULL CHECK (outcome_type IN ('success', 'failure', 'partial', 'unknown')),
    outcome_description TEXT NOT NULL,
    delay_days  INTEGER,                    -- задержка от решения до исхода
    causal_factors JSONB,                   -- list[str] — что повлияло на исход
    impact_magnitude FLOAT DEFAULT 0.5 CHECK (impact_magnitude BETWEEN 0.0 AND 1.0),

    -- JEPA-encoded вектор исхода
    outcome_embedding JSONB,

    -- Источник: ретроспектива / метрика / упоминание в другой встрече
    source_type VARCHAR(30) DEFAULT 'retrospective' CHECK (source_type IN ('retrospective', 'metric', 'inline')),

    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы для bwe_outcome_events
CREATE INDEX IF NOT EXISTS idx_bwe_oe_user_id ON bwe_outcome_events(user_id);
CREATE INDEX IF NOT EXISTS idx_bwe_oe_decision_id ON bwe_outcome_events(decision_event_id);
CREATE INDEX IF NOT EXISTS idx_bwe_oe_outcome_type ON bwe_outcome_events(outcome_type);


-- Слой 4: Wisdom Patterns (Wisdom Index в реляционном хранилище)
-- Паттерны — кластеры похожих ситуаций с распределением исходов
-- Дублируются в Qdrant (коллекция bwe_wisdom_patterns) для векторного поиска
CREATE TABLE IF NOT EXISTS bwe_wisdom_patterns (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     VARCHAR(255) NOT NULL,
    pattern_name VARCHAR(200) NOT NULL,

    -- Центроид кластера в latent space (prototype vector)
    prototype_vector JSONB NOT NULL,

    -- Список UUID Decision Events, образующих паттерн
    instances   JSONB DEFAULT '[]'::jsonb,

    -- Распределение исходов: {"success": 3, "failure": 1, "partial": 0, "unknown": 0}
    outcome_distribution JSONB DEFAULT '{}'::jsonb,

    -- Как паттерн работает на разных витках компании
    vitek_sensitivity TEXT,

    -- Факторы, отличающие успех от провала
    context_differentiators JSONB DEFAULT '[]'::jsonb,

    -- Уверенность на основе количества случаев
    -- low: <3 случаев, medium: 3-9, high: 10+
    confidence  VARCHAR(20) DEFAULT 'low' CHECK (confidence IN ('low', 'medium', 'high')),

    -- Флаг: нужен ли пересчёт context_differentiators (при добавлении новых исходов)
    needs_analysis_update BOOLEAN DEFAULT FALSE,

    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы для bwe_wisdom_patterns
CREATE INDEX IF NOT EXISTS idx_bwe_wp_user_id ON bwe_wisdom_patterns(user_id);
CREATE INDEX IF NOT EXISTS idx_bwe_wp_confidence ON bwe_wisdom_patterns(confidence);
CREATE INDEX IF NOT EXISTS idx_bwe_wp_updated_at ON bwe_wisdom_patterns(updated_at DESC);


-- Триггер автообновления updated_at
CREATE OR REPLACE FUNCTION update_bwe_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_bwe_de_updated_at
    BEFORE UPDATE ON bwe_decision_events
    FOR EACH ROW EXECUTE FUNCTION update_bwe_updated_at();

CREATE TRIGGER trigger_bwe_wp_updated_at
    BEFORE UPDATE ON bwe_wisdom_patterns
    FOR EACH ROW EXECUTE FUNCTION update_bwe_updated_at();


-- ============================================================
-- Комментарии к архитектуре
-- ============================================================
-- Qdrant коллекция 'bwe_wisdom_patterns':
--   Создаётся автоматически при старте WisdomIndex.
--   Хранит prototype_vector каждого паттерна для косинусного поиска.
--   payload: {pattern_uuid, user_id, pattern_name, confidence,
--             outcome_distribution, total_cases}
--
-- Поток данных BWE:
--   Транскрипт → DecisionExtractor → bwe_decision_events
--   Ретроспектива → OutcomeLinker → bwe_outcome_events
--   Trigger → PatternEncoder → bwe_wisdom_patterns + Qdrant
--   Query → WisdomIndex (Qdrant search) → WisdomEngine → Ответ
-- ============================================================
