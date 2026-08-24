-- ============================================================================
-- TESSENT BRAIN — Multi-tenant backfill (Phase 7e foundation)
--
-- Контекст: миграция 020 включила RLS на ~11 таблицах с lenient-предикатом
-- (`tenant_id IS NULL OR tenant_id = setting`), но `tenant_id` оставался
-- NULL у legacy-строк. Шаблон `020a_multitenant_backfill.sql.template`
-- ждал кастомного маппинга user_id → tenant_id из таблицы `users`, которой
-- в этом репо НЕТ как такового.
--
-- Реальный инвариант кодовой базы (проверен по всем persona/coffee/reactive
-- INSERT-сайтам): везде используется `tenant_id or user_id` fallback, т.е.
-- де-факто `tenant_id == user_id::text`. Здесь мы канонизируем это для
-- NULL-строк: backfill `tenant_id := user_id::text` где user_id есть.
--
-- Зачем: без этого 020b_multitenant_rls_strict.sql (NOT NULL + strict
-- predicate) НЕ применить — он fail-fast при NULL-tenant_id строках. Это
-- открывает путь к strict-RLS, но САМО ПО СЕБЕ ничего не активирует:
-- lenient-предикат продолжает работать как раньше. Безопасно идемпотентно.
--
-- НЕ трогаем таблицы без user_id (scheduled_automations, automation_execution_log
-- и т.п.): там нет очевидного владельца — оставляем NULL, и они продолжают
-- работать через lenient-поблажку. Strict-mode на них — отдельное решение
-- после явного определения владельца.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    -- Только таблицы с user_id (verified): documents, validation_results,
    -- bookmarks, user_profiles_v2, messenger_links, messenger_onboarding_tokens,
    -- share_grants, bwe_decision_events, bwe_outcome_events.
    -- llm_profiles / share_bundles / sima_* — без user_id (или owner_user_id),
    -- их трогаем отдельно ниже.
    tbl TEXT;
    user_tables TEXT[] := ARRAY[
        'public.documents',
        'public.validation_results',
        'public.bookmarks',
        'public.user_profiles_v2',
        'public.messenger_links',
        'public.messenger_onboarding_tokens',
        'public.share_grants',
        'public.bwe_decision_events',
        'public.bwe_outcome_events'
    ];
    updated INTEGER;
BEGIN
    FOREACH tbl IN ARRAY user_tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema || '.' || table_name = tbl
        )
        AND EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema || '.' || table_name = tbl
              AND column_name = 'user_id'
        )
        AND EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema || '.' || table_name = tbl
              AND column_name = 'tenant_id'
        ) THEN
            EXECUTE format(
                'UPDATE %s SET tenant_id = user_id::text '
                'WHERE tenant_id IS NULL AND user_id IS NOT NULL',
                tbl
            );
            GET DIAGNOSTICS updated = ROW_COUNT;
            RAISE NOTICE 'backfill % rows in %', updated, tbl;
        END IF;
    END LOOP;
END$$;

-- ---------------------------------------------------------------------------
-- owner_user_id: share_bundles использует owner_user_id вместо user_id.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'share_bundles'
    )
    AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'share_bundles'
          AND column_name = 'owner_user_id'
    ) THEN
        UPDATE public.share_bundles
           SET tenant_id = owner_user_id::text
         WHERE tenant_id IS NULL AND owner_user_id IS NOT NULL;
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- Diagnostic: показать, у каких ещё RLS-таблиц остались NULL tenant_id
-- (без user_id-колонки — их strict-mode пока не покрывает).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    tbl TEXT;
    rls_tables TEXT[] := ARRAY[
        'public.documents',
        'public.scheduled_automations',
        'public.automation_execution_log',
        'public.knowledge_sync_subscriptions',
        'public.processed_meetings',
        'public.sima_projects',
        'public.sima_blocks',
        'public.sima_connections',
        'public.sima_messages',
        'public.bwe_decision_events',
        'public.bwe_outcome_events',
        'public.bwe_wisdom_patterns',
        'public.validation_results',
        'public.llm_profiles',
        'public.messenger_links',
        'public.messenger_onboarding_tokens',
        'public.share_bundles',
        'public.share_grants',
        'public.bookmarks',
        'public.user_profiles_v2'
    ];
    cnt BIGINT;
BEGIN
    FOREACH tbl IN ARRAY rls_tables LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema || '.' || table_name = tbl
        )
        AND EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema || '.' || table_name = tbl
              AND column_name = 'tenant_id'
        ) THEN
            EXECUTE format(
                'SELECT count(*) FROM %s WHERE tenant_id IS NULL', tbl
            ) INTO cnt;
            IF cnt > 0 THEN
                RAISE NOTICE 'STILL NULL: % (% rows) — strict-mode blocked', tbl, cnt;
            END IF;
        END IF;
    END LOOP;
END$$;

COMMIT;

-- ============================================================================
-- После применения проверьте:
--   SELECT 'documents' tbl, COUNT(*) FROM documents WHERE tenant_id IS NULL
--     UNION ALL ...
-- Если все нули — можно гонять 020b_multitenant_rls_strict.sql (отдельно,
-- по продуманному решению). Этот файл НИЧЕГО не активирует.
-- ============================================================================
