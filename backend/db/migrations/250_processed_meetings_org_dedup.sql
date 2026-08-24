-- Migration 250: org-aware idempotency for processed_meetings
--
-- Контекст. processed_meetings из миграции 003 был дедуп-журналом на уровне
-- (user_id, meeting_id) — то есть «этот юзер уже прогнал эту встречу». Этого
-- достаточно для personal-graph и для Mini Tess (личный коуч), но недостаточно
-- для корпоративного слоя:
--   * один и тот же Zoom-meeting может прилететь через двух участников
--     организации (двух user_id) и быть пере-обработан дважды → дубликаты
--     Decision/Task в org-графе;
--   * один и тот же транскрипт может прийти через разные внешние ID
--     (повторный экспорт из Zoom после правки тайтла), нужен fallback-дедуп
--     по содержимому.
--
-- Решение — расширить таблицу nullable-колонками. Старый UNIQUE(user_id,
-- meeting_id) остаётся: solo-юзеры и Mini Tess pipe продолжают работать без
-- изменений; колонки nullable → backfill не обязателен. Новый partial-unique
-- активируется только когда все три новых поля заполнены (corp-канал).
--
-- Идемпотентна: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

ALTER TABLE public.processed_meetings
    ADD COLUMN IF NOT EXISTS org_id UUID,
    ADD COLUMN IF NOT EXISTS source TEXT,
    ADD COLUMN IF NOT EXISTS external_id TEXT,
    ADD COLUMN IF NOT EXISTS content_hash TEXT,
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS processing_lease_until TIMESTAMPTZ;

COMMENT ON COLUMN public.processed_meetings.org_id IS
    'Org owning this episode in the shared org-graph. NULL for solo/Mini Tess users.';
COMMENT ON COLUMN public.processed_meetings.source IS
    'Provenance: meetflow | zoom | telegram_voice | manual | mini_tess | gcal …';
COMMENT ON COLUMN public.processed_meetings.external_id IS
    'External system ID (Zoom meeting ID, Gcal event ID). NULL for manual uploads.';
COMMENT ON COLUMN public.processed_meetings.content_hash IS
    'sha256(normalized_transcript) — fallback dedup when external_id differs.';
COMMENT ON COLUMN public.processed_meetings.processing_lease_until IS
    'Soft lock: another worker can claim this row after this time. NULL = no active lease.';

-- Основной дедуп для corp-канала: одна запись на (org, source, external_id).
-- Partial — старые строки (без org_id) под уникальность не подпадают.
CREATE UNIQUE INDEX IF NOT EXISTS uq_processed_meetings_org_source_extid
    ON public.processed_meetings (org_id, source, external_id)
    WHERE org_id IS NOT NULL
      AND source IS NOT NULL
      AND external_id IS NOT NULL;

-- Fallback-дедуп по контенту (когда external_id отличается, а транскрипт тот же).
CREATE INDEX IF NOT EXISTS idx_processed_meetings_content_hash
    ON public.processed_meetings (content_hash)
    WHERE content_hash IS NOT NULL;

-- Org-scoped sweeps (admin: «покажи все processed по нашему org»).
CREATE INDEX IF NOT EXISTS idx_processed_meetings_org_id
    ON public.processed_meetings (org_id)
    WHERE org_id IS NOT NULL;

-- Stale-lease поиск (recovery после crash worker'а).
CREATE INDEX IF NOT EXISTS idx_processed_meetings_lease
    ON public.processed_meetings (processing_lease_until)
    WHERE processing_lease_until IS NOT NULL;

-- RLS-политики из 003 остаются по user_id. Доступ к org-строкам через
-- admin-канал (service-role) — отдельный layer, не в этой миграции.
