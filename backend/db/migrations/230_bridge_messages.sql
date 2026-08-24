-- ============================================================================
-- TESSENT BRAIN — Bridge messages (P2 §5: user→user транспорт)
--
-- Контекст: Mini Tess (EPIC D «корпоративный мост») умеет фрейм-перевод под
-- cogni_profile получателя, inbox от руководителя, сигналы наверх — но в
-- контракте Tessent НЕ было способа доставить сообщение от одного user_id
-- другому внутри того же Tessent. Это и есть отсутствующий транспорт.
--
-- Модель: durable outbox/inbox. Отправитель пишет строку; получатель читает
-- свой inbox (polling) И/ИЛИ получает push через reactive signal_queue
-- (signal_type='bridge_message') → drain → Telegram. Двойная доставка
-- защищена idempotency на стороне drain (dedup), а в inbox статус 'unread'.
--
-- Тенант: tenant_id = tenant_id or from_user (как везде в кодовой базе,
-- де-факто single-tenant-per-user). Скоупинг — app-level по to_user_id;
-- RLS на эту таблицу не вешаем (как reactive_signals) — отдельное решение.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bridge_messages (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    from_user_id    UUID NOT NULL,
    to_user_id      UUID NOT NULL,
    tenant_id       UUID NOT NULL,
    message_type    TEXT NOT NULL DEFAULT 'message',
    -- 'message' | 'manager_signal' | 'manager_signal_anon' | 'aggregate' | ...
    title           TEXT,
    body_markdown   TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'unread',
    -- 'unread' | 'read' | 'acked' | 'deleted'
    signal_id       UUID,            -- linked reactive_signal (push), если был
    read_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- inbox: «мои непрочитанные, свежие сверху»
CREATE INDEX IF NOT EXISTS idx_bridge_messages_inbox
    ON public.bridge_messages (to_user_id, status, created_at DESC);
-- outbox: «что я отправил»
CREATE INDEX IF NOT EXISTS idx_bridge_messages_outbox
    ON public.bridge_messages (from_user_id, created_at DESC);
-- tenant-агрегации/админ
CREATE INDEX IF NOT EXISTS idx_bridge_messages_tenant
    ON public.bridge_messages (tenant_id, created_at DESC);

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP TABLE IF EXISTS public.bridge_messages;
-- COMMIT;
