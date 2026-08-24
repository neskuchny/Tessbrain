-- 020a_multitenant_backfill.sql
-- Runnable-бэкфилл tenant_id для sima_* ПЕРЕД strict-RLS (020b).
--
-- Зачем: 020b намеренно падает, пока в sima_projects/blocks/connections/messages
-- есть строки с NULL tenant_id («Cannot enable strict tenant RLS: rows with NULL
-- tenant_id remain … Run backfill first»). Ручной шаблон
-- 020a_multitenant_backfill.sql.template бэкфиллит из users.tenant_id, но в
-- single-user/локальной установке users.tenant_id часто NULL → тупик.
--
-- Здесь используем tenant_id = собственный user_id строки: именно так штампует
-- tenant узлы графа (GraphBuilder.create_node: tenant_id по умолчанию = user_id),
-- так что модель консистентна, и нет зависимости от таблицы public.users.
--
-- Идемпотентна: трогает только строки с tenant_id IS NULL. Версия 020a
-- сортируется ДО 020b → выполняется раньше strict-RLS.
BEGIN;

-- Проекты: свой user_id как tenant.
UPDATE public.sima_projects
   SET tenant_id = user_id
 WHERE tenant_id IS NULL
   AND user_id IS NOT NULL;

-- Дочерние сущности: tenant из родительского проекта.
UPDATE public.sima_blocks AS b
   SET tenant_id = p.tenant_id
  FROM public.sima_projects AS p
 WHERE b.project_id = p.id
   AND b.tenant_id IS NULL
   AND p.tenant_id IS NOT NULL;

UPDATE public.sima_connections AS c
   SET tenant_id = p.tenant_id
  FROM public.sima_projects AS p
 WHERE c.project_id = p.id
   AND c.tenant_id IS NULL
   AND p.tenant_id IS NOT NULL;

UPDATE public.sima_messages AS m
   SET tenant_id = p.tenant_id
  FROM public.sima_projects AS p
 WHERE m.project_id = p.id
   AND m.tenant_id IS NULL
   AND p.tenant_id IS NOT NULL;

COMMIT;
