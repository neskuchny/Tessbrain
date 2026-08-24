-- SIMA Фаза 5: маркер зрелости артефакта в галерее.
-- draft (черновик) → verified (проверен) → canon (эталон). Показывается
-- бейджем на карточке; помогает отличать надёжные переиспользуемые кубики
-- от сырых. Синтезированные артефакты создаются как draft.
ALTER TABLE sima_artifacts
    ADD COLUMN IF NOT EXISTS maturity TEXT NOT NULL DEFAULT 'draft';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sima_artifacts_maturity_check'
    ) THEN
        ALTER TABLE sima_artifacts
            ADD CONSTRAINT sima_artifacts_maturity_check
            CHECK (maturity IN ('draft', 'verified', 'canon'));
    END IF;
END $$;
