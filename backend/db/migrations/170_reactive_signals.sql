-- Phase C Reactive engine: signal queue + cognitive_load samples
--
-- Идея Reactive engine:
-- Tessent НЕ молотит уведомления как обычный бот. Перед каждой
-- доставкой (Coffee artifact, action recommendation, learning nudge)
-- спрашиваем у Reactive: «Сейчас удачный момент?»
--
-- Cognitive load оценивается по:
-- - Календарная плотность (Google Calendar busy-windows)
-- - Sentiment (последние NLP-сигналы из messengers/captures)
-- - Peak hours (когда юзер обычно отвечает быстро)
-- - Активные cascade-блоки (если только что отменили задачу — не дёргаем)
--
-- Сигналы, которые НЕ влезли в окно «удачного момента», паркуются в
-- reactive_signals и drain-ятся когда load падает.

CREATE TABLE IF NOT EXISTS public.reactive_signals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    -- Тип сигнала: какой подсистемой создан
    signal_type     TEXT NOT NULL,
    -- ↑ "coffee_artifact" | "action_recommendation" | "learning_nudge" |
    --    "cascade_invalidation" | "calendar_reminder" | ...

    -- Привязка к источнику (опц.). Например, для coffee_artifact это
    -- coffee_artifacts.id, для action_recommendation — action_id и т.д.
    source_kind     TEXT,
    source_id       TEXT,

    -- Контент для delivery
    title           TEXT NOT NULL,
    body_markdown   TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    -- ↑ структура зависит от signal_type: для coffee_artifact —
    -- recommended_channels, recommended_executor; для cascade —
    -- список invalidated artifact_ids; и т.д.

    -- Приоритет (0 — drop ниже всех, 100 — bypass cognitive_load)
    priority        SMALLINT NOT NULL DEFAULT 50,
    -- ↑ Гайдлайн:
    -- ↑ 90+ — critical (security alert, missed deadline)
    -- ↑ 70-89 — важно, не ждать долго (action в течение часа)
    -- ↑ 40-69 — обычные briefs/nudges
    -- ↑ 0-39 — можно сдвинуть на следующий low-load window

    -- Когда сигнал стал актуален и когда станет неактуален
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,
    -- ↑ после valid_until сигнал считается expired и не доставляется

    -- Статус доставки
    status          TEXT NOT NULL DEFAULT 'queued',
    -- ↑ "queued" — ждёт drain
    -- ↑ "delivered" — отправлено
    -- ↑ "expired" — valid_until прошёл
    -- ↑ "cancelled" — отменено через cascade
    -- ↑ "dropped" — отброшено (load слишком высокий + priority низкий)

    delivered_at    TIMESTAMPTZ,
    delivered_via   TEXT,
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reactive_signals_user_status
    ON public.reactive_signals (user_id, status, priority DESC, valid_from ASC);

CREATE INDEX IF NOT EXISTS idx_reactive_signals_source
    ON public.reactive_signals (source_kind, source_id)
    WHERE source_kind IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reactive_signals_drain
    ON public.reactive_signals (status, valid_from)
    WHERE status = 'queued';


-- Cognitive load samples: история оценок load для конкретного user'а.
-- Используется UI ("сейчас плотный день, отложу"), ML-обучение на
-- закономерностях, и debugging.
CREATE TABLE IF NOT EXISTS public.cognitive_load_samples (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    sampled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Итоговая оценка load в [0.0; 1.0]
    -- 0.0 = идеально спокоен, 1.0 = в огне
    load_score      REAL NOT NULL,

    -- Уровень для UI: "low" | "medium" | "high" | "critical"
    load_level      TEXT NOT NULL,

    -- Компоненты оценки для transparency
    component_calendar  REAL NOT NULL DEFAULT 0.0,
    component_sentiment REAL NOT NULL DEFAULT 0.0,
    component_recent_activity REAL NOT NULL DEFAULT 0.0,
    component_off_hours REAL NOT NULL DEFAULT 0.0,

    -- Контекст оценки (зачем считали)
    reason          TEXT,
    -- ↑ "pre_delivery_check" | "scheduled_drain" | "manual_query" | ...

    details         JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_cognitive_load_user_time
    ON public.cognitive_load_samples (user_id, sampled_at DESC);

-- Cascade invalidations: лог того, как одно событие (task cancel,
-- meeting reschedule) инвалидирует связанные артефакты/задачи.
-- Хранится для аудита и для UI «эти артефакты теперь неактуальны».
CREATE TABLE IF NOT EXISTS public.cascade_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    trigger_kind    TEXT NOT NULL,
    -- ↑ "task_cancelled" | "meeting_rescheduled" | "decision_reverted" | ...
    trigger_ref     TEXT NOT NULL,
    -- ↑ ID того, что случилось

    -- Что было инвалидировано
    invalidated     JSONB NOT NULL DEFAULT '[]',
    -- ↑ [{"kind":"coffee_artifact", "id":"..."}, ...]

    reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_cascade_events_user_time
    ON public.cascade_events (user_id, triggered_at DESC);
