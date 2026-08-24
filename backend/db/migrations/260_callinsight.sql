-- Migration 260: интеграция с CallInsight Pro (docs/CALLINSIGHT_INTEGRATION.md).
-- Их webhooks несут event_id (uuid) и ретраятся при 5xx — нужна
-- дедупликация на нашей стороне. Добавляем event_id в общий сырой лог
-- integration_events + частичный UNIQUE (NULL-ы старых строк не мешают).

ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS event_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_events_source_event_id
    ON integration_events(source, event_id)
    WHERE event_id IS NOT NULL;
