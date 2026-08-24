-- 279_metric_registry_store.sql — D1e/B3: реестр метрик per-user SQLite → Postgres.
--
-- Раньше: data/metrics/<user_id>.db (sqlite-файл на пользователя) — вне
-- транзакций/multi-node/ретеншна БД и недостижим для org-доступа (B3 time-series).
-- Теперь: общие таблицы с user_id-ключом, схема 1:1 с прежней sqlite
-- (MetricRegistryPG наследует всю pure-логику summary/divergences).

CREATE TABLE IF NOT EXISTS metric_registry (
    user_id     TEXT NOT NULL,
    metric_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,
    unit        TEXT,
    category    TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, metric_id),
    UNIQUE (user_id, name_norm)
);

CREATE TABLE IF NOT EXISTS metric_point (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    metric_id   TEXT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    period      TEXT,
    at          TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('meeting','dataset','manual')),
    source_id   TEXT,
    kind        TEXT NOT NULL DEFAULT 'fact' CHECK (kind IN ('fact','plan')),
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_point_metric ON metric_point (user_id, metric_id, at);
CREATE INDEX IF NOT EXISTS idx_metric_point_source ON metric_point (user_id, source_type, source_id);
