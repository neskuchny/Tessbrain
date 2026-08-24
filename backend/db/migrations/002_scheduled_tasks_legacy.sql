-- Migration: Create scheduled_tasks table for legacy MeetFlow agents
-- This table is used by alfa_asynk_meetflow agents

-- Create scheduled_tasks table
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    action_type VARCHAR(100) NOT NULL,
    action_data JSONB DEFAULT '{}',
    execute_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',  -- pending, running, completed, failed
    result JSONB,
    error TEXT,
    user_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_execute_at ON scheduled_tasks (execute_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status ON scheduled_tasks (status);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user_id ON scheduled_tasks (user_id);

-- Enable RLS
ALTER TABLE scheduled_tasks ENABLE ROW LEVEL SECURITY;

-- Policy for authenticated users
CREATE POLICY "Users can manage their own scheduled_tasks" ON scheduled_tasks
    FOR ALL USING (true);

COMMENT ON TABLE scheduled_tasks IS 'Legacy scheduled tasks table for MeetFlow agents';


