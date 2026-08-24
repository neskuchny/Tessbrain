-- Migration 010: Create SIMA tables (Система Интерактивного Мышления и Анализа)
-- Product design platform tables for Tessent Brain integration
-- All tables prefixed with sima_ to avoid conflicts

-- ============================================================
-- 1. SIMA Projects
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,  -- мультитенантность (привязка к пользователю Tessent)

    name TEXT NOT NULL,
    description TEXT,
    goal TEXT,
    target_audience TEXT,
    mvp_description TEXT,
    ideal_product TEXT,

    -- Нарратив (5 разделов)
    narrative_problem TEXT,
    narrative_solution TEXT,
    narrative_how_it_works TEXT,
    narrative_product_map TEXT,
    narrative_decisions TEXT,

    -- Генерированное ТЗ
    generated_tz TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_projects_user_id ON sima_projects(user_id);
CREATE INDEX IF NOT EXISTS idx_sima_projects_created_at ON sima_projects(created_at DESC);

-- ============================================================
-- 2. SIMA Blocks
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_blocks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    -- Подсхема: если parent_block_id != null, блок вложен внутрь другого
    parent_block_id UUID REFERENCES sima_blocks(id) ON DELETE CASCADE,

    -- Базовые
    name TEXT NOT NULL,
    label TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'feature',         -- core, feature, integration, data, agent
    layer TEXT NOT NULL DEFAULT 'mvp',            -- problem, solution, mvp, medium, ideal
    color TEXT NOT NULL DEFAULT '#6366F1',

    -- Позиция на холсте
    position_x DOUBLE PRECISION NOT NULL DEFAULT 0,
    position_y DOUBLE PRECISION NOT NULL DEFAULT 0,

    -- === СЕКЦИЯ «ЗАЧЕМ» (бизнес) ===
    business_value TEXT,
    user_benefit TEXT,
    problem_solved TEXT,
    impact_if_removed TEXT,
    metrics TEXT,

    -- === СЕКЦИЯ «ЧТО» (функционал) ===
    description TEXT,
    goal TEXT,
    input_data TEXT,
    output_data TEXT,
    acceptance_criteria TEXT,     -- JSON
    user_flow TEXT,
    edge_cases TEXT,

    -- === СЕКЦИЯ «КАК» (техника) ===
    implementation TEXT,
    block_tech_stack TEXT,        -- JSON
    api_endpoints TEXT,           -- JSON
    data_model TEXT,
    files_to_create TEXT,         -- JSON
    technical_deps TEXT,          -- JSON
    estimated_complexity TEXT NOT NULL DEFAULT 'medium',  -- low, medium, high

    -- === СЕКЦИЯ «КОНТЕКСТ» (аналитика) ===
    analogues TEXT,
    alternatives TEXT,
    risks TEXT,
    future_evolution TEXT,
    problems TEXT,

    -- Мини-нарратив блока
    block_narrative TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_blocks_project_id ON sima_blocks(project_id);
CREATE INDEX IF NOT EXISTS idx_sima_blocks_project_parent ON sima_blocks(project_id, parent_block_id);

-- ============================================================
-- 3. SIMA Connections
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    -- Подсхема
    parent_block_id UUID,

    from_block_id UUID NOT NULL REFERENCES sima_blocks(id) ON DELETE CASCADE,
    to_block_id UUID NOT NULL REFERENCES sima_blocks(id) ON DELETE CASCADE,

    -- Тип связи
    type TEXT NOT NULL DEFAULT 'data_flow',  -- data_flow, trigger, dependency, condition

    -- Подпись и описание
    label TEXT,
    description TEXT,

    -- Что передаётся
    data_description TEXT,
    data_format TEXT,        -- JSON, REST, gRPC, WebSocket, Event
    data_example TEXT,
    data_volume TEXT,

    -- Условия
    condition TEXT,
    error_handling TEXT,

    -- Бизнес-контекст
    business_meaning TEXT,

    -- Визуал
    color TEXT NOT NULL DEFAULT '#6366F1',
    style TEXT NOT NULL DEFAULT 'solid',     -- solid, dashed, dotted
    animated BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_connections_project_id ON sima_connections(project_id);
CREATE INDEX IF NOT EXISTS idx_sima_connections_from_block ON sima_connections(from_block_id);
CREATE INDEX IF NOT EXISTS idx_sima_connections_to_block ON sima_connections(to_block_id);

-- ============================================================
-- 4. SIMA Sessions
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    name TEXT,
    summary TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_sessions_project_id ON sima_sessions(project_id);

-- ============================================================
-- 5. SIMA Messages
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,
    session_id UUID REFERENCES sima_sessions(id) ON DELETE SET NULL,

    role TEXT NOT NULL,       -- user, assistant, system
    content TEXT NOT NULL,
    actions TEXT,             -- JSON: AI действия

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_messages_project_id ON sima_messages(project_id);
CREATE INDEX IF NOT EXISTS idx_sima_messages_session_id ON sima_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sima_messages_created_at ON sima_messages(created_at);

-- ============================================================
-- 6. SIMA Decisions
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    title TEXT NOT NULL,
    description TEXT NOT NULL,
    reasoning TEXT,
    alternatives TEXT,
    session_id UUID,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_decisions_project_id ON sima_decisions(project_id);

-- ============================================================
-- 7. SIMA Personas
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_personas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    role TEXT NOT NULL,
    tech_level TEXT NOT NULL DEFAULT 'basic',          -- none, basic, intermediate, advanced, expert
    domain_knowledge TEXT NOT NULL DEFAULT 'basic',    -- none, basic, deep
    goal TEXT NOT NULL,
    time_budget TEXT NOT NULL DEFAULT 'неограничено',
    motivation TEXT NOT NULL DEFAULT 'medium',         -- high, medium, low
    patience_level TEXT NOT NULL DEFAULT 'medium',     -- low, medium, high
    traits JSONB NOT NULL DEFAULT '[]'::jsonb,
    fears JSONB NOT NULL DEFAULT '[]'::jsonb,
    previous_experience JSONB NOT NULL DEFAULT '[]'::jsonb,
    constraints JSONB NOT NULL DEFAULT '[]'::jsonb,
    archetype TEXT NOT NULL DEFAULT 'custom',          -- best_case, average, worst_case, custom

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_personas_project_id ON sima_personas(project_id);

-- ============================================================
-- 8. SIMA Simulation Reports
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_simulation_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,
    persona_id UUID NOT NULL REFERENCES sima_personas(id) ON DELETE CASCADE,

    scope TEXT NOT NULL,             -- mvp, medium, ideal, full
    summary JSONB NOT NULL,          -- { totalSteps, timeToValue, completionRate, overallVerdict, oneLiner }
    steps JSONB NOT NULL,            -- SimulationStep[]
    friction_points JSONB NOT NULL,  -- FrictionPoint[]
    recommendations JSONB NOT NULL,  -- Recommendation[]
    analysis_time INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_simulation_reports_project ON sima_simulation_reports(project_id, created_at DESC);

-- ============================================================
-- 9. SIMA Strategist Reports
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_strategist_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    scope TEXT NOT NULL,             -- full, layer, block
    scope_id TEXT,                   -- layerSlug или blockId
    verdict JSONB NOT NULL,          -- { emoji, oneLiner, confidence }
    top_actions JSONB NOT NULL,      -- TopAction[]
    sections JSONB NOT NULL,         -- Section[]
    raw_analysis TEXT,               -- полные данные от линз
    analysis_time INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_strategist_reports_project ON sima_strategist_reports(project_id, created_at DESC);

-- ============================================================
-- 10. SIMA Project Snapshots
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_project_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    label TEXT,
    type TEXT NOT NULL DEFAULT 'auto',  -- auto, manual
    blocks_data JSONB NOT NULL,         -- BlockData[]
    connections_data JSONB NOT NULL,    -- ConnectionData[]
    project_data JSONB,                 -- основные поля проекта

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_snapshots_project ON sima_project_snapshots(project_id, created_at DESC);

-- ============================================================
-- 11. SIMA Data Sources
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_data_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES sima_projects(id) ON DELETE CASCADE,

    name TEXT NOT NULL,
    source_type TEXT NOT NULL,        -- file, text, api, tessent_meetings, tessent_knowledge
    mime_type TEXT,
    content TEXT NOT NULL,
    raw_metadata JSONB,

    status TEXT NOT NULL DEFAULT 'uploaded',  -- uploaded, processing, analyzed, error

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_data_sources_project_id ON sima_data_sources(project_id);

-- ============================================================
-- 12. SIMA Data Insights
-- ============================================================
CREATE TABLE IF NOT EXISTS sima_data_insights (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    data_source_id UUID NOT NULL REFERENCES sima_data_sources(id) ON DELETE CASCADE,
    project_id UUID NOT NULL,

    category TEXT NOT NULL,          -- user_need, pain_point, feature_idea, market_trend, tech_requirement, business_metric, quote, persona_trait
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.7,
    priority TEXT NOT NULL DEFAULT 'medium',  -- high, medium, low

    linked_block_id UUID,
    applied BOOLEAN NOT NULL DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_data_insights_project_id ON sima_data_insights(project_id);
CREATE INDEX IF NOT EXISTS idx_sima_data_insights_source ON sima_data_insights(data_source_id);

-- ============================================================
-- Triggers for updated_at
-- ============================================================
CREATE OR REPLACE FUNCTION sima_update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER sima_projects_updated_at
    BEFORE UPDATE ON sima_projects
    FOR EACH ROW EXECUTE FUNCTION sima_update_timestamp();

CREATE TRIGGER sima_blocks_updated_at
    BEFORE UPDATE ON sima_blocks
    FOR EACH ROW EXECUTE FUNCTION sima_update_timestamp();

CREATE TRIGGER sima_connections_updated_at
    BEFORE UPDATE ON sima_connections
    FOR EACH ROW EXECUTE FUNCTION sima_update_timestamp();

CREATE TRIGGER sima_sessions_updated_at
    BEFORE UPDATE ON sima_sessions
    FOR EACH ROW EXECUTE FUNCTION sima_update_timestamp();

CREATE TRIGGER sima_data_sources_updated_at
    BEFORE UPDATE ON sima_data_sources
    FOR EACH ROW EXECUTE FUNCTION sima_update_timestamp();
