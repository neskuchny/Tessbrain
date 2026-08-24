# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Templates & Workflows Module
=============================================

Система шаблонов и рабочих процессов:
- Создание и управление шаблонами
- Выполнение цепочек агентов
- Автоматические триггеры
- Извлечение регламентов и шаблонов из встреч
"""

from .regulation_extractor import RegulationExtractorAgent
from .template_executor import TemplateExecutor
from .template_extractor import TemplateExtractorAgent
from .template_model import (
    ExecutionStatus,
    TemplateCategory,
    TemplateExecution,
    TemplateStep,
    TemplateVisibility,
    WorkflowTemplate,
)
from .template_registry import TemplateRegistry, get_template_registry

__all__ = [
    "ExecutionStatus",
    # Extractors
    "RegulationExtractorAgent",
    "TemplateCategory",
    "TemplateExecution",
    # Executor
    "TemplateExecutor",
    "TemplateExtractorAgent",
    # Registry
    "TemplateRegistry",
    "TemplateStep",
    "TemplateVisibility",
    # Models
    "WorkflowTemplate",
    "get_template_registry"
]


