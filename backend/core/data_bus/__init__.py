# -*- coding: utf-8 -*-
"""
Data Bus — Шина данных Tessbrain.

Система контролируемого доступа к данным мозга компании
для внутренних и внешних потребителей.

Компоненты:
- models: Модели данных (Consumer, SharingPolicy, DataChannel, AuditEntry)
- api_key_manager: Генерация и верификация API ключей
- redaction_engine: Фильтрация и маскирование конфиденциальных данных
- bus_service: Центральный сервис шины данных
- rate_limiter: Rate limiting (Redis + in-memory fallback)
"""

from .api_key_manager import APIKeyManager
from .bus_service import DataBusService, get_data_bus_service
from .models import (
    ConsumerType,
    DataBusAuditEntry,
    DataBusConsumer,
    DataChannel,
    SharingPolicy,
)
from .rate_limiter import RateLimiter
from .redaction_engine import RedactionEngine

__all__ = [
    "APIKeyManager",
    "ConsumerType",
    "DataBusAuditEntry",
    "DataBusConsumer",
    "DataBusService",
    "DataChannel",
    "RateLimiter",
    "RedactionEngine",
    "SharingPolicy",
    "get_data_bus_service",
]
