-- 264: серверное хранение досок (Board «Мои доски»), P1 BOARD_ROADMAP.
-- Раньше доски жили только файлом (JSON download) + шаблоны в localStorage
-- браузера. Эта таблица даёт «Мои доски» на сервере (переживают смену браузера,
-- доступны на всех устройствах), общий фундамент и для креатива, и для процессов.
-- Идемпотентна.

CREATE TABLE IF NOT EXISTS public.board_workflows (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    org_id     TEXT,
    name       TEXT NOT NULL DEFAULT 'Без названия',
    -- creative — ИИ-холст (картинки/текст); process — конвейер (P2/P3).
    kind       TEXT NOT NULL DEFAULT 'creative',
    -- Граф доски (nodes/edges/edgeStyle). Тяжёлые картинки НЕ храним здесь —
    -- в графе только ссылки (см. BOARD_ROADMAP P1); JSONB остаётся компактным.
    graph      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Список «Мои доски» по пользователю, свежие сверху.
CREATE INDEX IF NOT EXISTS idx_board_workflows_user
    ON public.board_workflows (user_id, updated_at DESC);
