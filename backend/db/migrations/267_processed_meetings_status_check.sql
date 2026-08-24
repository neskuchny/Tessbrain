-- Migration 267: расширить CHECK-словарь статусов processed_meetings
--
-- Контекст. В Supabase на processed_meetings висит CHECK со старым словарём:
--   status IN ('pending','processing','completed','failed')
-- (констрейнт создан вне репо-миграций — schema drift). Код же пишет ещё
-- 'running' (lease в claim_episode), 'no_transcript' (терминальный статус
-- для встреч без транскрипта) и раньше — свободное 'error: <текст>'.
--
-- Следствие drift'а: POST/PATCH с 'no_transcript' падал с 23514, строка
-- журнала НЕ создавалась, встреча каждый цикл поллера считалась «новой» и
-- переобрабатывалась заново — бесконечный цикл + runaway-расход LLM
-- (симптом: mark_completion POST failed ... 400 ... 23514 в логах).
--
-- Решение: заменить CHECK на объединение старого и кодового словарей.
-- Свободные 'error: ...' код больше не пишет (knowledge_sync теперь пишет
-- 'failed', текст ошибки — в last_error), поэтому whitelist остаётся строгим.
--
-- Supabase-only (таблица создаётся в 003, тоже Supabase-only): применять в
-- Supabase SQL Editor / через API, локальный runner пропускает (migrate.py).
-- Идемпотентна: DROP IF EXISTS + ADD.

ALTER TABLE public.processed_meetings
    DROP CONSTRAINT IF EXISTS processed_meetings_status_check;

ALTER TABLE public.processed_meetings
    ADD CONSTRAINT processed_meetings_status_check
    CHECK (status IN (
        'pending', 'processing', 'running',
        'completed', 'failed', 'no_transcript'
    ));
