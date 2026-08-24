-- SIMA Фаза 3: жизненный статус блока и MVP-флаг.
-- status — done/wip/q(под вопросом)/idea, показывается точкой-индикатором на
-- кубике и включает вид «по статусу». is_mvp — войдёт ли блок в первую версию
-- (фильтр «только MVP»). Не путать с estimated_complexity (оценка сложности).
ALTER TABLE sima_blocks
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'idea',
    ADD COLUMN IF NOT EXISTS is_mvp BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sima_blocks_status_check'
    ) THEN
        ALTER TABLE sima_blocks
            ADD CONSTRAINT sima_blocks_status_check
            CHECK (status IN ('done', 'wip', 'q', 'idea'));
    END IF;
END $$;
