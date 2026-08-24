-- ============================================================================
-- TESSENT BRAIN — pgcrypto enable + helper functions (W11 enterprise-pack).
--
-- Включает расширение pgcrypto (стандартное в Postgres 9+) и определяет
-- две стандартизованные функции для encrypt/decrypt PII-полей:
--
--   tessent_encrypt(plaintext TEXT, key TEXT) → BYTEA
--   tessent_decrypt(ciphertext BYTEA, key TEXT) → TEXT
--
-- Используется symmetric-encryption (PGP_SYM_*). Key передаётся
-- application-слоем из env (`PGCRYPTO_FIELD_KEY`) — НЕ хранится в БД.
-- Ротация ключа = re-encrypt всех значений новой утилитой
-- (см. tools/rotate_pgcrypto_key.py — TODO).
--
-- Применение в схеме:
--    INSERT INTO users(email_enc) VALUES (tessent_encrypt('a@b.com', :key));
--    SELECT tessent_decrypt(email_enc, :key) FROM users;
--
-- ВАЖНО: ключ при decrypt'е — это SECRET. Не логировать, не светить
-- в EXPLAIN VERBOSE. По возможности использовать prepared statement
-- с параметрами, чтобы ключ не попал в логи запросов.
--
-- Идемпотентно.
-- ============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION public.tessent_encrypt(plaintext TEXT, key TEXT)
RETURNS BYTEA
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT pgp_sym_encrypt(plaintext, key, 'cipher-algo=aes256, compress-algo=2')
$$;

CREATE OR REPLACE FUNCTION public.tessent_decrypt(ciphertext BYTEA, key TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT pgp_sym_decrypt(ciphertext, key)
$$;

-- Helper: вернуть NULL если decrypt не удался (для миграционных сценариев).
CREATE OR REPLACE FUNCTION public.tessent_decrypt_safe(ciphertext BYTEA, key TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
BEGIN
    RETURN pgp_sym_decrypt(ciphertext, key);
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$;

COMMIT;

-- ============================================================================
-- ОТКАТ
-- ============================================================================
-- BEGIN;
-- DROP FUNCTION IF EXISTS public.tessent_decrypt_safe(BYTEA, TEXT);
-- DROP FUNCTION IF EXISTS public.tessent_decrypt(BYTEA, TEXT);
-- DROP FUNCTION IF EXISTS public.tessent_encrypt(TEXT, TEXT);
-- -- DROP EXTENSION IF EXISTS pgcrypto;  -- НЕ откатывать без согласования —
-- -- может ломать другие приложения, использующие digest()/gen_random_uuid().
-- COMMIT;
