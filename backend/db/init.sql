-- TESSENT BRAIN - Database Initialization
-- PostgreSQL + TimescaleDB

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text search
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ============================================
-- Organizations (Multi-tenancy)
-- ============================================
CREATE TABLE IF NOT EXISTS organizations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_org_slug ON organizations(slug);

-- ============================================
-- Users
-- ============================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255),
    role VARCHAR(50) DEFAULT 'employee',  -- admin, manager, employee
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(organization_id, email)
);

CREATE INDEX IF NOT EXISTS idx_user_org ON users(organization_id);
CREATE INDEX IF NOT EXISTS idx_user_email ON users(email);

-- ============================================
-- Meetings
-- ============================================
CREATE TABLE IF NOT EXISTS meetings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    
    title VARCHAR(500),
    transcript TEXT,
    meeting_date TIMESTAMPTZ,
    duration_minutes INTEGER,
    
    -- Processing status
    status VARCHAR(50) DEFAULT 'pending',  -- pending, processing, completed, failed
    processed_at TIMESTAMPTZ,
    
    -- Metadata
    participants TEXT[],
    project_id UUID,
    metadata JSONB DEFAULT '{}',
    
    -- Analysis results (cached)
    analysis_summary JSONB,
    entities_count INTEGER DEFAULT 0,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_meeting_org ON meetings(organization_id);
CREATE INDEX IF NOT EXISTS idx_meeting_date ON meetings(meeting_date DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_status ON meetings(status);
CREATE INDEX IF NOT EXISTS idx_meeting_project ON meetings(project_id);

-- Convert to hypertable for time-series optimization
SELECT create_hypertable('meetings', 'created_at', if_not_exists => TRUE);

-- ============================================
-- Entities (Graph nodes metadata)
-- ============================================
CREATE TABLE IF NOT EXISTS entities (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    
    -- Identity
    neo4j_id VARCHAR(255),  -- Reference to Neo4j node
    entity_type VARCHAR(50) NOT NULL,  -- person, project, task, etc.
    name VARCHAR(255) NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    
    -- External references
    external_ids JSONB DEFAULT '{}',
    
    -- Content
    description TEXT,
    properties JSONB DEFAULT '{}',
    
    -- Embedding reference
    qdrant_id VARCHAR(255),
    
    -- Temporal
    valid_from TIMESTAMPTZ DEFAULT NOW(),
    valid_to TIMESTAMPTZ DEFAULT 'infinity',
    
    -- Importance (for decay)
    importance FLOAT DEFAULT 0.5,
    last_mentioned_at TIMESTAMPTZ DEFAULT NOW(),
    mention_count INTEGER DEFAULT 1,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_entity_org ON entities(organization_id);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entities USING gin(name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_entity_neo4j ON entities(neo4j_id);

-- ============================================
-- Snapshots
-- ============================================
CREATE TABLE IF NOT EXISTS snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    
    -- Target
    entity_id UUID REFERENCES entities(id),
    entity_type VARCHAR(50) NOT NULL,
    
    -- Snapshot data
    snapshot_data JSONB NOT NULL,
    health_score FLOAT,
    
    -- Metadata
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_snapshot_entity ON snapshots(entity_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_type ON snapshots(entity_type);
CREATE INDEX IF NOT EXISTS idx_snapshot_generated ON snapshots(generated_at DESC);

-- Convert to hypertable
SELECT create_hypertable('snapshots', 'created_at', if_not_exists => TRUE);

-- ============================================
-- Anomalies
-- ============================================
CREATE TABLE IF NOT EXISTS anomalies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    
    -- Target
    entity_id UUID REFERENCES entities(id),
    entity_type VARCHAR(50),
    
    -- Anomaly details
    anomaly_type VARCHAR(100) NOT NULL,
    severity VARCHAR(20) DEFAULT 'medium',  -- low, medium, high, critical
    description TEXT NOT NULL,
    evidence JSONB DEFAULT '{}',
    
    -- Status
    status VARCHAR(50) DEFAULT 'open',  -- open, acknowledged, resolved, false_positive
    resolved_at TIMESTAMPTZ,
    resolved_by UUID REFERENCES users(id),
    
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_anomaly_org ON anomalies(organization_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_entity ON anomalies(entity_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_status ON anomalies(status);
CREATE INDEX IF NOT EXISTS idx_anomaly_severity ON anomalies(severity);

-- Convert to hypertable
SELECT create_hypertable('anomalies', 'created_at', if_not_exists => TRUE);

-- ============================================
-- Chat Sessions
-- ============================================
CREATE TABLE IF NOT EXISTS chat_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    user_id UUID REFERENCES users(id),
    
    -- Session data
    messages JSONB DEFAULT '[]',
    context JSONB DEFAULT '{}',
    
    -- Metadata
    last_activity TIMESTAMPTZ DEFAULT NOW(),
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_org ON chat_sessions(organization_id);
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_activity ON chat_sessions(last_activity DESC);

-- ============================================
-- Background Tasks
-- ============================================
CREATE TABLE IF NOT EXISTS background_tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID REFERENCES organizations(id),
    
    -- Task details
    task_type VARCHAR(100) NOT NULL,
    task_data JSONB DEFAULT '{}',
    
    -- Scheduling
    scheduled_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    
    -- Status
    status VARCHAR(50) DEFAULT 'pending',  -- pending, running, completed, failed
    error_message TEXT,
    result JSONB,
    
    -- Retries
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_status ON background_tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_scheduled ON background_tasks(scheduled_at);
CREATE INDEX IF NOT EXISTS idx_task_type ON background_tasks(task_type);

-- ============================================
-- Audit Log
-- ============================================
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID REFERENCES organizations(id),
    user_id UUID REFERENCES users(id),
    
    -- Action details
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100),
    resource_id UUID,
    
    -- Data
    old_data JSONB,
    new_data JSONB,
    metadata JSONB DEFAULT '{}',
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_org ON audit_log(organization_id);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

-- Convert to hypertable
SELECT create_hypertable('audit_log', 'created_at', if_not_exists => TRUE);

-- ============================================
-- Default Organization (for development)
-- ============================================
INSERT INTO organizations (id, name, slug)
VALUES ('00000000-0000-0000-0000-000000000001', 'Default Organization', 'default')
ON CONFLICT (slug) DO NOTHING;

-- Default Admin User
INSERT INTO users (organization_id, email, name, role, password_hash)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'admin@tessent.local',
    'Admin',
    'admin',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4.GZPzIoaQPoF.Gy'  -- password: admin
)
ON CONFLICT (organization_id, email) DO NOTHING;

-- ============================================
-- Functions
-- ============================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply trigger to tables with updated_at
CREATE TRIGGER update_organizations_updated_at
    BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_users_updated_at
    BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_meetings_updated_at
    BEFORE UPDATE ON meetings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_entities_updated_at
    BEFORE UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_chat_sessions_updated_at
    BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================
-- Views
-- ============================================

-- View: Active entities with stats
CREATE OR REPLACE VIEW v_entity_stats AS
SELECT 
    e.id,
    e.organization_id,
    e.entity_type,
    e.name,
    e.importance,
    e.mention_count,
    e.last_mentioned_at,
    COUNT(DISTINCT s.id) as snapshot_count,
    COUNT(DISTINCT a.id) as anomaly_count
FROM entities e
LEFT JOIN snapshots s ON e.id = s.entity_id
LEFT JOIN anomalies a ON e.id = a.entity_id AND a.status = 'open'
WHERE e.valid_to = 'infinity'
GROUP BY e.id;

-- View: Recent meetings with entity counts
CREATE OR REPLACE VIEW v_recent_meetings AS
SELECT 
    m.id,
    m.organization_id,
    m.title,
    m.meeting_date,
    m.status,
    m.entities_count,
    m.created_at
FROM meetings m
WHERE m.created_at > NOW() - INTERVAL '30 days'
ORDER BY m.meeting_date DESC;

COMMENT ON TABLE organizations IS 'Multi-tenant organizations';
COMMENT ON TABLE users IS 'System users with roles';
COMMENT ON TABLE meetings IS 'Meeting transcripts and analysis';
COMMENT ON TABLE entities IS 'Knowledge graph entities metadata';
COMMENT ON TABLE snapshots IS 'Entity state snapshots';
COMMENT ON TABLE anomalies IS 'Detected anomalies and issues';
COMMENT ON TABLE chat_sessions IS 'Chat conversation history';
COMMENT ON TABLE background_tasks IS 'Background job queue';
COMMENT ON TABLE audit_log IS 'System audit trail';



