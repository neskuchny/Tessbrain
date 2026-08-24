-- Phase E reactive learning loop: delivery feedback + per-user calibration
--
-- Phase C/D доставляли сигналы по ФИКСИРОВАННЫМ порогам (threshold 0/50/70/90
-- по load level). Это top trade-off из READINESS_ARCHITECTURE.md §11:
-- «не учитывает per-user calibration».
--
-- Phase E замыкает контур обратной связи:
-- 1. Когда сигнал доставлен — фиксируем РЕАКЦИЮ юзера (reactive_feedback):
--    engaged / acted / snoozed / ignored / dismissed.
-- 2. Периодически пересчитываем per-user профиль (reactive_calibration):
--    threshold_delta (насколько агрессивнее/осторожнее слать) на основе
--    истории реакций.
-- 3. delivery_gate применяет threshold_delta поверх базового порога.
--
-- Чем больше юзер реально пользуется доставленным — тем агрессивнее шлём;
-- чем больше игнорит/закрывает — тем осторожнее. Self-tuning.

CREATE TABLE IF NOT EXISTS public.reactive_feedback (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- На что реакция (любое опционально — feedback может быть по сигналу
    -- из очереди ИЛИ по артефакту доставленному напрямую без очереди).
    signal_id       UUID,
    artifact_id     UUID,
    signal_type     TEXT NOT NULL DEFAULT 'generic',
    -- ↑ coffee_strategic_brief / coffee_tz_document / action_recommendation ...

    -- Реакция
    kind            TEXT NOT NULL,
    -- ↑ "acted"     — взял в работу (сильный +)
    -- ↑ "engaged"   — открыл/прочитал (+)
    -- ↑ "snoozed"   — отложил (тайминг был не идеален; нейтрально/мягкий -)
    -- ↑ "ignored"   — не открыл в окне актуальности (-)
    -- ↑ "dismissed" — явно закрыл/«не нужно» (сильный -)

    -- Контекст для обучения
    load_at_delivery REAL,
    -- ↑ какой был load когда доставили (для анализа «при каком load юзер
    --   всё равно реагирует»)
    latency_seconds  INTEGER,
    -- ↑ сколько прошло от доставки до реакции (быстро = релевантно)
    channel          TEXT,
    -- ↑ через какой канал доставлено (telegram/email/...)

    details          JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reactive_feedback_user_time
    ON public.reactive_feedback (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reactive_feedback_signal
    ON public.reactive_feedback (signal_id)
    WHERE signal_id IS NOT NULL;


-- Per-user калибровочный профиль. Один ряд на юзера, пересчитывается
-- recompute_calibration_task (cron) или on-demand.
CREATE TABLE IF NOT EXISTS public.reactive_calibration (
    user_id         UUID PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Сдвиг базового порога. Отрицательный = слать агрессивнее
    -- (юзер хорошо реагирует), положительный = осторожнее.
    -- Применяется как effective_threshold = base + threshold_delta,
    -- клиппится в [0; 95].
    threshold_delta REAL NOT NULL DEFAULT 0.0,

    -- Опциональные per-user override весов компонентов load (NULL = default).
    -- Phase E пока их не вычисляет (только threshold_delta), но колонки
    -- зарезервированы для будущей weight-калибровки.
    weight_calendar  REAL,
    weight_activity  REAL,
    weight_sentiment REAL,
    weight_off_hours REAL,

    -- Метаданные обучения
    sample_count    INTEGER NOT NULL DEFAULT 0,
    -- ↑ сколько feedback-событий учтено. Калибровка применяется только
    --   при sample_count >= MIN_SAMPLES (см. calibration.py), иначе
    --   delta игнорируется (cold-start protection).
    engagement_score REAL NOT NULL DEFAULT 0.0,
    -- ↑ средневзвешенная реакция в [-2; +2] для transparency UI

    details         JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reactive_calibration_tenant
    ON public.reactive_calibration (tenant_id);
