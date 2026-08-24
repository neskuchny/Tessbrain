# -*- coding: utf-8 -*-
"""
Task Specification System - Система генерации ТЗ на основе РЕАЛЬНЫХ данных.

ГЛАВНЫЙ ПРИНЦИП: ТЗ создаётся на основе данных компании, а не абстрактно!

Алгоритм:
1. Понять задачу - что нужно сделать
2. Определить какие данные нужны
3. Поискать данные в графах, векторах, снепшотах
4. Собрать контекст - факты, мнения, цифры
5. Создать ТЗ на основе реальных данных
6. Выполнить задачу (опционально)

Модули:
- TaskSpecificationSystem - главный оркестратор
- DataDrivenTaskSystem - data-driven система (основная логика)
- TaskAnalyzerAgent - глубокий анализ задачи (используется meeting_pipeline)
- TaskExecutorAgent - выполнение задач по ТЗ

НЕ экспортируются (аудит: нигде не подключены, оставлены как опциональные
строительные блоки — импортируйте напрямую из модуля, если будете подключать):
- context_search.ContextSearchAgent - мультимодальный поиск контекста
- spec_generator.SpecificationGeneratorAgent - генерация детального ТЗ
  (реальная генерация идёт через DataDrivenTaskSystem)
"""

from .data_driven_orchestrator import DataDrivenTaskSystem
from .orchestrator import TaskSpecificationSystem, create_task_specification_system
from .task_analyzer import TaskAnalyzerAgent
from .task_executor import TaskExecutorAgent

__all__ = [
    "DataDrivenTaskSystem",
    "TaskAnalyzerAgent",
    "TaskExecutorAgent",
    "TaskSpecificationSystem",
    "create_task_specification_system",
]

