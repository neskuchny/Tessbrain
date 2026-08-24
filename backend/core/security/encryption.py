"""PII at-rest encryption helper (W11 enterprise-pack).

Тонкая обёртка над pgcrypto-функциями (миграция 050). Используется в
тех редких местах, где нужно зашифровать поле БЕЗ schema-изменения
типа колонки — например, новый optional `phone_enc BYTEA` в существующей
таблице `users`.

Дизайн:
- Helper'ы возвращают SQL-фрагменты, не значения. Реальный
  encrypt/decrypt происходит в Postgres — application никогда не
  держит plaintext в памяти дольше необходимого.
- Ключ не хранится в Settings — берём из env `PGCRYPTO_FIELD_KEY`
  при каждом вызове. Это намеренно: secret-rotation без рестарта
  процесса требует чтобы код не кэшировал ключ.

Использование с asyncpg / SQLAlchemy:

    from backend.core.security.encryption import (
        encrypt_expr, decrypt_expr, get_field_key,
    )

    key = get_field_key()
    await session.execute(
        text(f"INSERT INTO users(email_enc) VALUES ({encrypt_expr(':email')})"),
        {"email": "a@b.com", "_key": key},  # см. EncryptedColumn note
    )

Для полной интеграции рекомендуется построить SQLAlchemy
TypeDecorator — но это уже application-specific, см. SOPs в
ENTERPRISE.md.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class FieldKeyMissingError(RuntimeError):
    """Raised когда PGCRYPTO_FIELD_KEY не задан, а encrypt вызван.

    Fail-closed: лучше упасть, чем тихо записать plaintext в BYTEA-поле.
    """


def get_field_key() -> str:
    """Прочитать field-encryption ключ из env.

    Никаких defaults: если ключ не задан и кто-то пытается шифровать —
    поднимаем `FieldKeyMissingError`.
    """
    key = os.environ.get("PGCRYPTO_FIELD_KEY", "").strip()
    if not key:
        raise FieldKeyMissingError(
            "PGCRYPTO_FIELD_KEY env not set; cannot encrypt PII fields. "
            "Generate via: openssl rand -base64 48"
        )
    return key


def encrypt_expr(value_placeholder: str, key_placeholder: str = ":pgc_key") -> str:
    """Вернуть SQL-выражение, шифрующее значение из placeholder.

    `value_placeholder` обычно равен `:email` или подобному
    bound-параметру — caller подсунет plaintext в `bind_params` ОДИН РАЗ.
    """
    return f"public.tessent_encrypt({value_placeholder}, {key_placeholder})"


def decrypt_expr(column_name: str, key_placeholder: str = ":pgc_key") -> str:
    """Вернуть SQL-выражение, дешифрующее BYTEA-колонку."""
    return f"public.tessent_decrypt({column_name}, {key_placeholder})"


def decrypt_safe_expr(column_name: str, key_placeholder: str = ":pgc_key") -> str:
    """Как decrypt_expr, но при ошибке возвращает NULL (для миграций)."""
    return f"public.tessent_decrypt_safe({column_name}, {key_placeholder})"


def is_enabled() -> bool:
    """Включена ли на уровне env field-encryption."""
    try:
        get_field_key()
        return True
    except FieldKeyMissingError:
        return False


def generate_test_key() -> str:
    """Сгенерировать stub-ключ для unit-тестов.

    Не использовать в production — вернуть «test-key-do-not-use-in-prod».
    """
    return "test-key-do-not-use-in-prod"


__all__ = [
    "FieldKeyMissingError",
    "decrypt_expr",
    "decrypt_safe_expr",
    "encrypt_expr",
    "generate_test_key",
    "get_field_key",
    "is_enabled",
]


_ = Optional  # keep import for future TypeDecorator helper
