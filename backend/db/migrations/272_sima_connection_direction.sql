-- SIMA Фаза 2: направление связи между кубиками.
-- Видение: «соединить кубик с другим и написать какая связь — что из одного
-- в другой взять». Направление задаёт стрелки на канвасе:
--   one_way — источник → задача (по умолчанию),
--   two_way — двусторонняя (⇌),
--   to_task — явно «в задачу» (сборка смысла к центральному узлу).
ALTER TABLE sima_connections
    ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'one_way';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sima_connections_direction_check'
    ) THEN
        ALTER TABLE sima_connections
            ADD CONSTRAINT sima_connections_direction_check
            CHECK (direction IN ('one_way', 'two_way', 'to_task'));
    END IF;
END $$;
