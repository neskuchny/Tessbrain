-- ============================================================
-- TESSENT DATA BUS — Миграция для Supabase (PostgreSQL)
-- ============================================================
-- Запустить через Supabase Dashboard → SQL Editor
-- или через supabase db push
-- ============================================================

-- 1. Политики доступа
CREATE TABLE IF NOT EXISTS data_bus_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    
    -- Что видно
    allowed_node_types TEXT[] DEFAULT '{}',
    blocked_node_types TEXT[] DEFAULT '{}',
    max_access_level INT DEFAULT 1,
    allowed_projects TEXT[] DEFAULT '{"*"}',
    
    -- Фильтрация
    field_filters JSONB DEFAULT '{}',
    redaction_patterns JSONB DEFAULT '[]',
    
    -- Лимиты
    max_queries_per_day INT DEFAULT 100,
    max_queries_per_hour INT DEFAULT 20,
    max_results_per_query INT DEFAULT 50,
    
    -- Аудит
    audit_required BOOLEAN DEFAULT true,
    notify_admin BOOLEAN DEFAULT false,
    
    -- Статус
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 2. Потребители шины данных
CREATE TABLE IF NOT EXISTS data_bus_consumers (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    consumer_type TEXT NOT NULL,  -- investor, contractor, partner, ai_agent, internal_dept, auditor, consultant, hr_partner
    contact_email TEXT DEFAULT '',
    
    -- Аутентификация
    api_key_hash TEXT NOT NULL,
    api_key_prefix TEXT NOT NULL,
    
    -- Политика
    sharing_policy_id TEXT REFERENCES data_bus_policies(id),
    allowed_project_ids TEXT[] DEFAULT '{}',
    
    -- Статус
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ,
    last_access_at TIMESTAMPTZ,
    
    -- Мета
    metadata JSONB DEFAULT '{}'
);

-- 3. Каналы доставки
CREATE TABLE IF NOT EXISTS data_bus_channels (
    id TEXT PRIMARY KEY,
    consumer_id TEXT NOT NULL REFERENCES data_bus_consumers(id),
    channel_type TEXT NOT NULL,  -- api, webhook, telegram_bot, slack_bot
    endpoint_url TEXT,
    chat_id TEXT,
    subscriptions TEXT[] DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 4. Аудит-лог
CREATE TABLE IF NOT EXISTS data_bus_audit (
    id TEXT PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT now(),
    consumer_id TEXT REFERENCES data_bus_consumers(id),
    consumer_name TEXT,
    channel_type TEXT,
    
    -- Запрос
    query_type TEXT,      -- search, snapshot, subscribe
    query_text TEXT,
    query_params JSONB DEFAULT '{}',
    
    -- Результат
    results_count INT DEFAULT 0,
    redacted_count INT DEFAULT 0,
    policy_applied TEXT,
    
    -- Мета
    ip_address TEXT,
    response_time_ms INT DEFAULT 0,
    status_code INT DEFAULT 200,
    error TEXT
);

-- ============================================================
-- Индексы
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_dbc_tenant ON data_bus_consumers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_dbc_api_key_hash ON data_bus_consumers(api_key_hash);
CREATE INDEX IF NOT EXISTS idx_dbc_active ON data_bus_consumers(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_dbc_type ON data_bus_consumers(consumer_type);

CREATE INDEX IF NOT EXISTS idx_dbp_tenant ON data_bus_policies(tenant_id);

CREATE INDEX IF NOT EXISTS idx_dba_consumer ON data_bus_audit(consumer_id);
CREATE INDEX IF NOT EXISTS idx_dba_timestamp ON data_bus_audit(timestamp);
CREATE INDEX IF NOT EXISTS idx_dba_type ON data_bus_audit(query_type);

CREATE INDEX IF NOT EXISTS idx_dbch_consumer ON data_bus_channels(consumer_id);

-- ============================================================
-- RLS (Row Level Security) — опционально
-- ============================================================

-- ALTER TABLE data_bus_consumers ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE data_bus_policies ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE data_bus_audit ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE data_bus_channels ENABLE ROW LEVEL SECURITY;

-- Пользователь видит только своих потребителей:
-- CREATE POLICY "Users see own consumers" ON data_bus_consumers
--   FOR SELECT USING (tenant_id = auth.uid()::text);

-- ============================================================
-- Готово!
-- ============================================================
