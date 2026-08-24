-- Phase F: per-signal-type calibration granularity
--
-- Phase E (migration 180) учил ОДИН threshold_delta на юзера. Но реакции
-- зависят от ТИПА контента: founder может acted на strategic_brief но
-- dismissed на jira_cards. Один общий delta это усредняет и теряет сигнал.
--
-- Phase F расширяет reactive_calibration до композитного ключа
-- (user_id, signal_type). Строка signal_type='*' — user-wide aggregate
-- (fallback при cold-start конкретного типа).
--
-- Совместимость: существующие ряды (Phase E) получают signal_type='*'
-- через DEFAULT — это ровно их прежняя семантика (user-wide).

ALTER TABLE public.reactive_calibration
    ADD COLUMN IF NOT EXISTS signal_type TEXT NOT NULL DEFAULT '*';
-- ↑ '*' = user-wide aggregate; иначе конкретный тип
--   (coffee_strategic_brief / coffee_jira_cards / action_recommendation ...)

-- Пересобираем PK на (user_id, signal_type).
-- DROP старого PK безопасен: ряды уже имеют signal_type='*' из DEFAULT,
-- так что (user_id, '*') остаётся уникальным.
ALTER TABLE public.reactive_calibration
    DROP CONSTRAINT IF EXISTS reactive_calibration_pkey;

ALTER TABLE public.reactive_calibration
    ADD PRIMARY KEY (user_id, signal_type);

-- Индекс для «все профили юзера» (UI transparency: показать per-type разбивку)
CREATE INDEX IF NOT EXISTS idx_reactive_calibration_user
    ON public.reactive_calibration (user_id, signal_type);
