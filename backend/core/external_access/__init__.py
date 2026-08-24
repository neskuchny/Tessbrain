# -*- coding: utf-8 -*-
"""
External Access System - Система внешнего доступа к данным компании.

Позволяет подрядчикам (инвесторы, маркетинг, аудиторы) получать
информацию о компании с автоматической фильтрацией конфиденциальных данных.
"""

from .access_gateway import AccessGateway, get_access_gateway
from .audit_logger import AuditEntry, AuditLogger, get_audit_logger
from .data_aggregator import DataAggregator
from .security_filter import DataClassification, SecurityFilter

__all__ = [
    "AccessGateway",
    "AuditEntry",
    "AuditLogger",
    "DataAggregator",
    "DataClassification",
    "SecurityFilter",
    "get_access_gateway",
    "get_audit_logger",
]

