# -*- coding: utf-8 -*-
"""
Corrections Module - Система исправлений данных.

Компоненты:
- TwoPhaseManager: Двухфазная система исправлений (день/ночь)
- NightlyConsolidationService: Ночная консолидация данных
"""

from .nightly_consolidation import (
    NightlyConsolidationService,
    run_nightly_consolidation,
)
from .two_phase_manager import (
    Correction,
    CorrectionStatus,
    CorrectionType,
    TwoPhaseManager,
    get_two_phase_manager,
)

__all__ = [
    "Correction",
    "CorrectionStatus",
    "CorrectionType",
    "NightlyConsolidationService",
    "TwoPhaseManager",
    "get_two_phase_manager",
    "run_nightly_consolidation",
]

