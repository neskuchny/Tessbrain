-- ============================================================================
-- TESSENT BRAIN — k-anonymous aggregation (P2 §6 + п.7 360-review)
--
-- Контекст: Mini Tess просит server-side кросс-юзерную агрегацию по тенанту:
--   §6 — анонимный агрегированный сигнал наверх: копим вклады разных
--        пользователей, эмитим руководителю ТОЛЬКО при ≥k разных контрибьюторов
--        (k-anonymity, дефолт k=5). До порога — ничего не раскрываем.
--   п.7 — 360-review: собираем фидбек от нескольких рейтеров о субъекте;
--        выдаём агрегат с подавлением метрик, у которых <k оценок.
--
-- Обе задачи делят ОДИН примитив: «копить вклады от distinct user'ов,
-- подавлять ниже k, раскрывать/эмитить на пороге». Поэтому одна таблица
-- contributions + emit-once guard.
--
-- Приватность: contributor_user_id хранится ТОЛЬКО для подсчёта distinct
-- (k-anonymity). Он НИКОГДА не уходит в эмитируемый/выдаваемый агрегат —
-- наружу идут лишь payload'ы и счётчики. Эмит происходит ровно один раз
-- (aggregation_emissions с UNIQUE-guard под гонку).
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Вклады. Один вклад на (tenant, domain, aggregate_key, contributor) — upsert
-- (повторный contribute от того же юзера обновляет payload, не плодит distinct).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.aggregation_contributions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id           UUID NOT NULL,
    domain              TEXT NOT NULL,        -- 'manager_signal' | 'review_360'
    aggregate_key       TEXT NOT NULL,        -- signal: topic; 360: subject_user_id
    contributor_user_id UUID NOT NULL,        -- ТОЛЬКО для distinct-count, не эмитится
    payload             JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_aggregation_contribution
        UNIQUE (tenant_id, domain, aggregate_key, contributor_user_id)
);

-- быстрый distinct-count и выборка вкладов по ключу
CREATE INDEX IF NOT EXISTS idx_aggregation_contrib_key
    ON public.aggregation_contributions (tenant_id, domain, aggregate_key);

-- ----------------------------------------------------------------------------
-- Emit-once guard (для push-доменов вроде manager_signal). Вставка строки =
-- «этот ключ уже эмитнут». UNIQUE гарантирует ровно одну победу в гонке.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.aggregation_emissions (
    tenant_id       UUID NOT NULL,
    domain          TEXT NOT NULL,
    aggregate_key   TEXT NOT NULL,
    distinct_count  INTEGER NOT NULL,
    target_user_id  UUID,                    -- кому эмитнули (manager)
    emit_ref        TEXT,                    -- bridge_message_id / signal_id
    emitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_aggregation_emission
        PRIMARY KEY (tenant_id, domain, aggregate_key)
);

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS public.aggregation_emissions;
-- DROP TABLE IF EXISTS public.aggregation_contributions;
-- COMMIT;
