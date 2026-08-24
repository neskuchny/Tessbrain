-- Migration 255: SIMA Artifacts — переиспользуемые артефакты между проектами
-- (из дизайн-прототипа Sima Remix: «Галерея артефактов»).
--
-- Идея: блок, подсхема, карта, ТЗ или вики сохраняются как артефакт с
-- тегами; при создании нового проекта готовый артефакт вставляется как
-- кубик на канвас. Артефакт принадлежит пользователю (не проекту) —
-- живёт между проектами; usage отслеживается списком проектов.

CREATE TABLE IF NOT EXISTS sima_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,

    -- block | subschema | map | tz | wiki
    artifact_type TEXT NOT NULL DEFAULT 'block',
    title TEXT NOT NULL,
    description TEXT,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,

    -- содержимое: для block — поля блока; для subschema/map — блоки+связи;
    -- для tz/wiki — markdown в payload->>'markdown'
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- откуда сохранён и где использован (массив project_id)
    source_project_id UUID,
    used_in JSONB NOT NULL DEFAULT '[]'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sima_artifacts_user ON sima_artifacts(user_id);
CREATE INDEX IF NOT EXISTS idx_sima_artifacts_type ON sima_artifacts(user_id, artifact_type);
