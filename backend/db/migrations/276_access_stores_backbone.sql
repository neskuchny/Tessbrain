-- 276_access_stores_backbone.sql — D1a: перенос сторов доступов в Postgres.
--
-- Членства/холдинги/орг-метадата раньше жили в JSON-файлах (data/*.json),
-- которые переписывались целиком на каждую мутацию и сканировались целиком
-- на list. Это потолок масштаба (multi-node/NFS небезопасно, O(all) на op).
--
-- Модель: row-per-entity. Общий столбец `doc` (JSONB) хранит полную запись
-- как в файле (вся логика membership/org_store остаётся неизменной — меняется
-- только слой персистентности). Индексируемые столбцы вынесены для будущих
-- O(1)/O(members) fast-path'ов. `updated_at` — для кэш-инвалидации по TTL.
--
-- Эти сторы ГЛОБАЛЬНЫЕ (не tenant-scoped: членство связывает user↔org поверх
-- тенантов), поэтому НЕ покрываются RLS `tenant_isolation` и читаются/пишутся
-- через sync-сессии с apply_tenant=False (см. backend/core/ingest/access_repo.py).

CREATE TABLE IF NOT EXISTS org_membership_store (
    user_id     TEXT PRIMARY KEY,
    org_id      TEXT,                 -- активная организация (для list_members)
    doc         JSONB NOT NULL,       -- полная запись: role, department, secondary_orgs, ...
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_membership_store_org ON org_membership_store (org_id);

CREATE TABLE IF NOT EXISTS org_metadata_store (
    org_id         TEXT PRIMARY KEY,
    parent_org_id  TEXT,             -- дерево холдинг→корпорация→филиал (для get_children)
    doc            JSONB NOT NULL,   -- name, created_by, created_at, custom_roles, ...
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_metadata_store_parent ON org_metadata_store (parent_org_id);

CREATE TABLE IF NOT EXISTS holding_store (
    holding_id     TEXT PRIMARY KEY,
    owner_user_id  TEXT,             -- владелец холдинга (для get_holdings_for_owner)
    doc            JSONB NOT NULL,   -- name, org_ids, org_policies, created_at, ...
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_holding_store_owner ON holding_store (owner_user_id);
