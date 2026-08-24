-- 265: расписание для процесс-досок (P3 триггеры). Колонка last_triggered_at —
-- когда доску последний раз запускал планировщик; по ней дедупим (не гоняем
-- заново раньше интервала → без runaway-расхода). Идемпотентна.
ALTER TABLE public.board_workflows
    ADD COLUMN IF NOT EXISTS last_triggered_at TIMESTAMPTZ;
