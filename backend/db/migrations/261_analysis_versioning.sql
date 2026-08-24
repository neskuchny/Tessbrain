-- Migration 261: версионирование playbook'ов (Analysis Engine)
--
-- Контекст. Методику разбора (playbook) можно править. Старые отчёты
-- (run'ы) должны оставаться привязаны к версии методики, по которой
-- делались — для воспроизводимости и аудита («отчёт сделан по версии N»).
--
-- analysis_playbooks.version  — инкрементится при каждом update.
-- analysis_runs.playbook_version — снапшот версии на момент запуска.
--
-- Идемпотентна: ADD COLUMN IF NOT EXISTS.

ALTER TABLE public.analysis_playbooks
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE public.analysis_runs
    ADD COLUMN IF NOT EXISTS playbook_version INTEGER NOT NULL DEFAULT 1;

COMMENT ON COLUMN public.analysis_playbooks.version IS
    'Incremented on each update. Runs snapshot this into playbook_version.';
COMMENT ON COLUMN public.analysis_runs.playbook_version IS
    'Playbook version at run time — for reproducibility/audit.';
