-- 266: предложения автоматизации (SIMA P2, звенья A/B/C бизнес-цепочки).
-- Tessent замечает, что процесс стоит автоматизировать → сохраняет это как
-- СТРУКТУРНЫЙ объект-предложение (а не только Markdown-отчёт/надж) → показывает
-- пользователю с кнопками «Принять/Отклонить». На «Принять» формируется ТЗ и
-- заводится проект в SIMA (через seed-from-spec, звено D). Идемпотентна.

CREATE TABLE IF NOT EXISTS public.automation_proposals (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL,           -- мультитенантность
    title             TEXT NOT NULL,           -- коротко «что автоматизируем»
    problem           TEXT,                    -- почему это стоит автоматизировать
    proposed_action   TEXT,                    -- что предлагаем сделать
    expected_benefit  TEXT,                    -- ожидаемая польза / ROI
    evidence          TEXT,                    -- откуда вывод (цитаты встреч/док-тов)
    -- источник: night_opportunity | automation_audit | manual | …
    source            TEXT NOT NULL DEFAULT 'manual',
    -- pending | accepted | dismissed
    status            TEXT NOT NULL DEFAULT 'pending',
    -- заполняется при accept — связка с созданным проектом SIMA
    sima_project_id   UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Лента «что предлагает Tessent» по пользователю, свежие сверху, фильтр по статусу.
CREATE INDEX IF NOT EXISTS idx_automation_proposals_user_status
    ON public.automation_proposals (user_id, status, created_at DESC);

-- Дедуп: не плодить одинаковые предложения от детектора (один заголовок в
-- статусе pending на пользователя). Частичный уникальный индекс.
CREATE UNIQUE INDEX IF NOT EXISTS uq_automation_proposals_pending_title
    ON public.automation_proposals (user_id, title)
    WHERE status = 'pending';
