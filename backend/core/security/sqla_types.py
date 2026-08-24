"""SQLAlchemy TypeDecorator для pgcrypto field-encryption (W15).

`EncryptedString` — drop-in замена `Column(String)` с прозрачным
шифрованием через `tessent_encrypt`/`tessent_decrypt` (миграция 050).

Использование:
    class User(Base):
        id = Column(Integer, primary_key=True)
        email_enc = Column(EncryptedString(255))   # ← вместо String

    user.email_enc = "a@b.com"   # plaintext в Python; в БД пишется BYTEA
    print(user.email_enc)         # "a@b.com" (decrypted на bind)

Дизайн:
- Pure SQLAlchemy TypeDecorator над BYTEA.
- Ключ берётся из `PGCRYPTO_FIELD_KEY` env при каждой операции — secret
  rotation без рестарта процесса.
- Если ключ отсутствует, любая попытка чтения/записи поднимает
  `FieldKeyMissingError` — fail-closed.
- В `process_bind_param`/`process_result_value` используем
  `func.tessent_encrypt`/`func.tessent_decrypt` — это ВСТРАИВАЕТСЯ в
  SQL запрос как функция Postgres, ключ передаётся как bind param
  (а не литерал), не попадает в логи.

ВАЖНО: для CRUD-запросов (insert/update/select) это работает прозрачно.
Для raw SELECT через `text(...)` нужно вручную вызывать
`tessent_encrypt`/`tessent_decrypt` — см. `core/security/encryption.py`.
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from sqlalchemy import LargeBinary, func
    from sqlalchemy.types import TypeDecorator
    _SQLA_AVAILABLE = True
except ImportError:
    _SQLA_AVAILABLE = False
    TypeDecorator = object  # type: ignore[misc, assignment]
    LargeBinary = None       # type: ignore[assignment]
    func = None              # type: ignore[assignment]

from .encryption import FieldKeyMissingError, get_field_key


class EncryptedString(TypeDecorator):  # type: ignore[misc]
    """Зашифрованная строковая колонка.

    Storage: BYTEA (pgcrypto pgp_sym_encrypt output).
    Python type: str (plaintext в памяти; не логируется по best-effort).

    Args:
        length: подсказка для миграций; на самом деле длина BYTEA не
                ограничена нашими функциями.
    """
    impl = LargeBinary
    cache_ok = True

    def __init__(self, length: Optional[int] = None) -> None:
        if not _SQLA_AVAILABLE:
            raise RuntimeError(
                "sqlalchemy not installed; install to use EncryptedString"
            )
        super().__init__()
        self.length = length

    def bind_expression(self, bindvalue: Any) -> Any:
        """Оборачиваем bind-параметр в `tessent_encrypt(:value, :key)`.

        Это означает: в SQL запросе генерируется `tessent_encrypt(:p1, :p2)`,
        где :p1 — plaintext, :p2 — current key. Plaintext не персистится
        в logs (через bind), key — тоже bind, не литерал.
        """
        return func.tessent_encrypt(bindvalue, get_field_key())

    def column_expression(self, column: Any) -> Any:
        """Оборачиваем SELECT'ое значение в `tessent_decrypt(col, :key)`."""
        return func.tessent_decrypt(column, get_field_key())

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        """str → bytes/None.

        bind_expression уже оборачивает в `tessent_encrypt`; здесь нам
        нужно лишь убедиться что value — string или None.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)
        # Sanity-check ключа на этапе bind — если его нет, лучше упасть
        # тут, чем получить cryptic SQL ошибку.
        get_field_key()
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        """tessent_decrypt уже вернул TEXT — просто отдаём str."""
        if value is None:
            return None
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", errors="replace")
        return value

    def __repr__(self) -> str:
        return f"EncryptedString(length={self.length})"


__all__ = ["EncryptedString", "FieldKeyMissingError"]
