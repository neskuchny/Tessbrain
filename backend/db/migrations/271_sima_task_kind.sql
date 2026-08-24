-- SIMA Фаза 1: тип проекта (продукт/книга/идея).
-- «Конструктор любых идей», а не только продуктов: тип задаёт язык бейджей,
-- заголовков и (позже) набор слотов карты. Значения — англ. ключи, лейблы
-- локализуются на фронте. Не путать с шаблонами (saas/mobile/…) — другая ось.
ALTER TABLE sima_projects
    ADD COLUMN IF NOT EXISTS task_kind TEXT NOT NULL DEFAULT 'product';

-- Мягкое ограничение допустимых значений (расширяемо новой миграцией).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sima_projects_task_kind_check'
    ) THEN
        ALTER TABLE sima_projects
            ADD CONSTRAINT sima_projects_task_kind_check
            CHECK (task_kind IN ('product', 'book', 'idea'));
    END IF;
END $$;
