-- ============================================================================
-- TESSENT BRAIN — Business Crystal (Understanding Layer, Module A)
--
-- Контекст: расширяем существующий Business Wisdom Engine
-- (bwe_wisdom_patterns) до полноценных «кристаллов» по ARC-методологии
-- (docs/TESSENT_UNDERSTANDING_LAYER_DESIGN.md, §4). Добавляем причинно-
-- логическое обогащение, которого не хватает: границы применимости
-- (not_law), открытые вопросы (open_holes), асимметричную численную
-- уверенность (confidence_score) и provenance в Graphiti.
--
-- ВАЖНО (порядок применения — см. reality-check №1 в дизайне):
--   Таблица bwe_wisdom_patterns обычно создаётся НЕ этим runner'ом, а
--   SQLAlchemy create_all (wisdom_engine.py / postgres.py) В РАНТАЙМЕ api.
--   Этот файл (migrate, one-shot) бежит ДО api → на чистой БД таблицы
--   ещё нет. Поэтому СНАЧАЛА CREATE TABLE IF NOT EXISTS (зеркало
--   bwe_schema.sql, без триггера — updated_at держит ORM onupdate),
--   ПОТОМ ADD COLUMN IF NOT EXISTS. Идемпотентно для всех случаев:
--   таблицы нет / есть от bwe_schema.sql / есть от create_all.
--
-- НЕ вводим новой axis-таксономии (reality-check №2): «оси» кристалла —
-- это уже существующие tension_type (BWEDecisionEvent) + company_vitek
-- + KnowledgeCategory. Новые поля их КОМПОЗИРУЮТ/денормализуют, а не
-- дублируют. Единственное реально новое — not_law / open_holes.
--
-- Обратная совместимость: все колонки nullable / с дефолтом. Строковый
-- confidence ('low'/'medium'/'high') НЕ трогаем — добавляем отдельный
-- confidence_score (float). Старые читатели работают без изменений.
-- ============================================================================

BEGIN;

-- 1. Гарантируем существование базовой таблицы (fresh DB: до create_all).
--    Зеркало bwe_schema.sql, минус триггер updated_at (его держит ORM).
CREATE TABLE IF NOT EXISTS bwe_wisdom_patterns (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 VARCHAR(255) NOT NULL,
    pattern_name            VARCHAR(200) NOT NULL,
    prototype_vector        JSONB NOT NULL DEFAULT '[]'::jsonb,
    instances               JSONB DEFAULT '[]'::jsonb,
    outcome_distribution    JSONB DEFAULT '{}'::jsonb,
    vitek_sensitivity       TEXT,
    context_differentiators JSONB DEFAULT '[]'::jsonb,
    confidence              VARCHAR(20) DEFAULT 'low'
                            CHECK (confidence IN ('low', 'medium', 'high')),
    needs_analysis_update   BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bwe_wp_user_id    ON bwe_wisdom_patterns(user_id);
CREATE INDEX IF NOT EXISTS idx_bwe_wp_confidence ON bwe_wisdom_patterns(confidence);
CREATE INDEX IF NOT EXISTS idx_bwe_wp_updated_at ON bwe_wisdom_patterns(updated_at DESC);

-- 2. Crystal-поля. Композиция существующих таксономий (денормализация),
--    НЕ новая axis-классификация.
--    Денормализованный доминирующий tension_type связанных Decision Events
--    (источник истины — bwe_decision_events.tension_type). Для быстрого
--    фильтра поиска по «оси», как уже делает wisdom.py:174.
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS primary_tension VARCHAR(100);

--    Границы применимости по фазе компании (company_vitek 1..4). Если
--    кристалл валиден только на росте(2)-зрелости(3) — это not_law по фазе.
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS vitek_phase_min SMALLINT;
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS vitek_phase_max SMALLINT;

--    Категория знания из существующего KnowledgeCategory enum
--    (summary/case/methodology/.../antipattern/workflow), если применимо.
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS knowledge_category VARCHAR(50);

-- 3. Реально новое из ARC-методологии (причинно-логическое обогащение).
--    not_law — текстовые границы: «НЕ всегда, а при условии Z».
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS not_law JSONB DEFAULT '[]'::jsonb;

--    open_holes — что схема ещё не покрывает (честные пробелы).
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS open_holes JSONB DEFAULT '[]'::jsonb;

--    confidence_score — численная асимметричная уверенность (подтверждение
--    поднимает медленно, опровержение роняет быстро). NULL = ещё не
--    считалась → читатели падают на строковый confidence (back-compat).
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION;

--    Provenance: id эпизодов Graphiti, на которых кристалл основан.
ALTER TABLE bwe_wisdom_patterns
    ADD COLUMN IF NOT EXISTS evidence_episode_ids JSONB DEFAULT '[]'::jsonb;

-- 4. Индексы под новые пути поиска (Module D / wisdom_index.search_with_axis).
--    Частичные — только по непустым значениям, чтобы не раздувать.
CREATE INDEX IF NOT EXISTS idx_bwe_wp_primary_tension
    ON bwe_wisdom_patterns(primary_tension)
    WHERE primary_tension IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bwe_wp_knowledge_category
    ON bwe_wisdom_patterns(knowledge_category)
    WHERE knowledge_category IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bwe_wp_confidence_score
    ON bwe_wisdom_patterns(confidence_score DESC)
    WHERE confidence_score IS NOT NULL;

COMMIT;
