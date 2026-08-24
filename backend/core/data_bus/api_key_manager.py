# -*- coding: utf-8 -*-
"""
API Key Manager — Генерация и верификация API ключей шины данных.

Формат ключа: tdb_{type_prefix}_{random_hex_32}
Примеры:
  tdb_inv_a3f8c2e1b9d7... — для инвестора
  tdb_ctr_b7d9e4f2c1a3... — для подрядчика
  tdb_aia_c1a2b3d4e5f6... — для AI-агента
  tdb_int_d4e5f6a7b8c9... — для внутреннего отдела
  tdb_prt_e5f6a7b8c9d0... — для партнёра
"""
import hashlib
import logging
import secrets
from typing import Tuple

from .models import ConsumerType

logger = logging.getLogger(__name__)

# Маппинг типов потребителей на префиксы ключей
TYPE_PREFIXES = {
    ConsumerType.INVESTOR: "inv",
    ConsumerType.CONTRACTOR: "ctr",
    ConsumerType.PARTNER: "prt",
    ConsumerType.AI_AGENT: "aia",
    ConsumerType.INTERNAL_DEPT: "int",
    ConsumerType.AUDITOR: "aud",
    ConsumerType.CONSULTANT: "con",
    ConsumerType.HR_PARTNER: "hrp",
}


class APIKeyManager:
    """
    Управление API ключами шины данных.

    Генерирует криптографически стойкие ключи,
    хеширует SHA-256 для хранения, проверяет при запросах.
    """

    @staticmethod
    def generate_key(consumer_type: ConsumerType) -> Tuple[str, str, str]:
        """
        Сгенерировать новый API ключ.

        Args:
            consumer_type: Тип потребителя

        Returns:
            (full_key, key_hash, key_prefix)
            - full_key: полный ключ для выдачи потребителю (НЕ хранится в БД)
            - key_hash: SHA-256 хеш для хранения
            - key_prefix: первые символы для визуального опознания
        """
        type_prefix = TYPE_PREFIXES.get(consumer_type, "gen")
        random_part = secrets.token_hex(24)  # 48 символов hex

        full_key = f"tdb_{type_prefix}_{random_part}"
        key_hash = APIKeyManager.hash_key(full_key)
        key_prefix = f"tdb_{type_prefix}_{random_part[:8]}..."

        logger.info(f"Generated API key for {consumer_type.value}: {key_prefix}")

        return full_key, key_hash, key_prefix

    @staticmethod
    def hash_key(api_key: str) -> str:
        """
        Хешировать API ключ для безопасного хранения.

        Args:
            api_key: Полный API ключ

        Returns:
            SHA-256 хеш
        """
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @staticmethod
    def verify_key(api_key: str, stored_hash: str) -> bool:
        """
        Проверить API ключ по хешу.

        Args:
            api_key: Ключ от потребителя
            stored_hash: Хеш из БД

        Returns:
            True если ключ верный
        """
        computed_hash = APIKeyManager.hash_key(api_key)
        # Сравнение в константном времени для защиты от timing attacks
        return secrets.compare_digest(computed_hash, stored_hash)

    @staticmethod
    def extract_type_from_key(api_key: str) -> str:
        """
        Извлечь тип потребителя из API ключа (по префиксу).

        Args:
            api_key: API ключ

        Returns:
            Строка типа ("inv", "ctr", ...) или "unknown"
        """
        parts = api_key.split("_")
        if len(parts) >= 3 and parts[0] == "tdb":
            return parts[1]
        return "unknown"

    @staticmethod
    def validate_key_format(api_key: str) -> bool:
        """
        Проверить формат API ключа (без проверки по базе).

        Args:
            api_key: API ключ

        Returns:
            True если формат валидный
        """
        if not api_key or not isinstance(api_key, str):
            return False
        parts = api_key.split("_")
        if len(parts) != 3:
            return False
        if parts[0] != "tdb":
            return False
        if parts[1] not in TYPE_PREFIXES.values():
            return False
        if len(parts[2]) != 48:  # 24 bytes hex
            return False
        return True
