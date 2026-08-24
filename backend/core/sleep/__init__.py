# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Sleep Module (Ночной мозг)
==========================================

Модуль ночной обработки данных:
- Snapshot Generator — создание снимков состояния
- Company Snapshot Agent — умный слепок компании (NEW!)
- Night Processor — ночной анализ и поиск инсайтов
- Anomaly Detector — обнаружение аномалий и рисков
- Decay Manager — управление устареванием данных

Этот модуль работает асинхронно в фоне, анализируя данные
когда система не загружена активными запросами.
"""

from .anomaly_detector import Anomaly, AnomalyDetector
from .company_snapshot_agent import CompanySnapshotAgent, get_company_snapshot_agent
from .decay_manager import DecayManager
from .enhanced_snapshot import (
    CompanySnapshot,
    EnhancedSnapshotGenerator,
    PersonSnapshot,
    ProjectSnapshot,
    get_enhanced_snapshot_generator,
)
from .night_processor import NightInsight, NightProcessor
from .snapshot_generator import EntitySnapshot, Snapshot, SnapshotGenerator

__all__ = [
    "Anomaly",
    "AnomalyDetector",
    "CompanySnapshot",
    "CompanySnapshotAgent",
    "DecayManager",
    # Enhanced Snapshots
    "EnhancedSnapshotGenerator",
    "EntitySnapshot",
    "NightInsight",
    "NightProcessor",
    "PersonSnapshot",
    "ProjectSnapshot",
    "Snapshot",
    "SnapshotGenerator",
    "get_company_snapshot_agent",
    "get_enhanced_snapshot_generator",
]

