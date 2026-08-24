-- 277_access_stores_invites_broadcasts.sql — D1b: инвайты и broadcasts в Postgres.
--
-- Продолжение 276 (row-per-entity, doc JSONB — логика сторов не меняется,
-- только слой персистентности). Глобальные сторы (не tenant-scoped), пишутся
-- через sync-сессии access_repo с advisory-локом.

-- Приглашения в организации (data/org_invites.json).
-- token_hash индексируем: consume/lookup идёт по SHA256(raw) — O(1) вместо
-- скана всех инвайтов.
CREATE TABLE IF NOT EXISTS org_invite_store (
    invite_id   TEXT PRIMARY KEY,
    org_id      TEXT,
    token_hash  TEXT,
    doc         JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_invite_store_org ON org_invite_store (org_id);
CREATE INDEX IF NOT EXISTS idx_invite_store_token ON org_invite_store (token_hash);

-- Трансляции стратегии (data/broadcasts/<org_id>.json): одна строка на org,
-- doc = весь файл {"broadcasts": [...]} — сохраняем гранулярность файла.
CREATE TABLE IF NOT EXISTS broadcast_store (
    org_id      TEXT PRIMARY KEY,
    doc         JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
