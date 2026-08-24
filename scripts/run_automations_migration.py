# -*- coding: utf-8 -*-
"""
Скрипт для выполнения миграции таблиц автоматизаций в Supabase.
Запуск: python scripts/run_automations_migration.py
"""
import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# SQL для создания таблиц
MIGRATION_SQL = """
-- Создание таблицы scheduled_automations
CREATE TABLE IF NOT EXISTS scheduled_automations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    action_type VARCHAR(100) NOT NULL,
    action_data JSONB DEFAULT '{}',
    schedule_type VARCHAR(50) NOT NULL DEFAULT 'once',
    execute_at TIMESTAMPTZ,
    cron_expression VARCHAR(100),
    interval_seconds INTEGER,
    status VARCHAR(50) DEFAULT 'active',
    last_run_at TIMESTAMPTZ,
    last_run_status VARCHAR(50),
    last_run_result JSONB,
    last_error TEXT,
    run_count INTEGER DEFAULT 0,
    max_runs INTEGER,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    source_message TEXT,
    session_id VARCHAR(255)
);

-- Создание таблицы automation_execution_log
CREATE TABLE IF NOT EXISTS automation_execution_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    automation_id UUID NOT NULL REFERENCES scheduled_automations(id) ON DELETE CASCADE,
    status VARCHAR(50) NOT NULL,
    result JSONB,
    error_message TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_automation_user ON scheduled_automations(user_id);
CREATE INDEX IF NOT EXISTS idx_automation_status ON scheduled_automations(status);
CREATE INDEX IF NOT EXISTS idx_automation_schedule_type ON scheduled_automations(schedule_type);
CREATE INDEX IF NOT EXISTS idx_exec_log_automation ON automation_execution_log(automation_id);
CREATE INDEX IF NOT EXISTS idx_exec_log_created ON automation_execution_log(created_at DESC);
"""


async def run_migration():
    """Выполнить миграцию через Supabase SQL Editor API"""
    print("🚀 Running automations migration...")
    print(f"📍 Supabase URL: {SUPABASE_URL[:50]}...")

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ SUPABASE_URL or SUPABASE_KEY not configured!")
        print("\nПожалуйста, выполните SQL вручную в Supabase SQL Editor:")
        print("-" * 50)
        print(MIGRATION_SQL)
        print("-" * 50)
        return False

    # Попробуем выполнить через RPC (если есть функция exec_sql)
    # Или просто проверим, что таблицы создаются через REST API

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Проверим, существует ли таблица
        try:
            res = await client.get(
                f"{SUPABASE_URL}/rest/v1/scheduled_automations?select=id&limit=1",
                headers=headers
            )

            if res.status_code == 200:
                print("✅ Table 'scheduled_automations' already exists!")
                return True
            elif res.status_code == 404:
                print("⚠️ Table does not exist. Please run the migration SQL manually.")
            else:
                print(f"⚠️ Unexpected response: {res.status_code}")

        except Exception as e:
            print(f"❌ Error checking table: {e}")

    print("\n📋 Please run this SQL in Supabase SQL Editor (https://supabase.com/dashboard):")
    print("-" * 60)
    print(MIGRATION_SQL)
    print("-" * 60)

    return False


if __name__ == "__main__":
    asyncio.run(run_migration())


