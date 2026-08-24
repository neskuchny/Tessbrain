# -*- coding: utf-8 -*-
"""
Memory Layer для Tessbrain.

Компоненты:
1. Mem0 - простая долгосрочная память для AI агентов
2. MemGPT-Style - продвинутая трёхуровневая память:
   - Working Context (8-16K tokens) - активная сессия
   - Recall Storage (SQLite) - история запросов
   - Archival Storage (KG+Vector) - полная база знаний
3. Hebbian Learning - усиление связей при совместном упоминании
4. Frequency Tracker - отслеживание частоты упоминаний
"""

from .agent_memory import AgentMemoryManager, MemoryType, get_agent_memory_manager
from .context_loader import ChatMemoryContextLoader, LoadedMemoryContext, MemoryContextBlock
from .emotional_salience import EmotionalSalienceAnalyzer, get_emotional_analyzer
from .frequency_tracker import FrequencyTracker, get_frequency_tracker
from .hebbian_learning import HebbianLearning, get_hebbian_learning
from .mem0_client import Mem0Client, MemoryManager, get_mem0_client, get_memory_manager
from .memgpt_memory import (
    ArchivalStorageManager,
    ContextItem,
    MemGPTMemoryManager,
    RecallStorageManager,
    WorkingContextManager,
    get_memgpt_manager,
    initialize_memgpt_manager,
)
from .memory_consolidator import (
    ContentQualityScorer,
    MemoryCluster,
    MemoryConsolidator,
    MemorySummary,
    get_memory_consolidator,
)


class MemoryManagerImportError(Exception):
    """Raised when memory manager cannot be imported."""
    pass

__all__ = [
    # Agent Memory
    "AgentMemoryManager",
    "ArchivalStorageManager",
    # Context Loader
    "ChatMemoryContextLoader",
    "ContentQualityScorer",
    "ContextItem",
    # Emotional Salience
    "EmotionalSalienceAnalyzer",
    # Frequency Tracker
    "FrequencyTracker",
    # Hebbian Learning
    "HebbianLearning",
    "LoadedMemoryContext",
    # Mem0
    "Mem0Client",
    # MemGPT-Style
    "MemGPTMemoryManager",
    "MemoryCluster",
    # Memory Consolidator
    "MemoryConsolidator",
    "MemoryContextBlock",
    "MemoryManager",
    "MemoryManagerImportError",
    "MemorySummary",
    "MemoryType",
    "RecallStorageManager",
    "WorkingContextManager",
    "get_agent_memory_manager",
    "get_emotional_analyzer",
    "get_frequency_tracker",
    "get_hebbian_learning",
    "get_mem0_client",
    "get_memgpt_manager",
    "get_memory_consolidator",
    "get_memory_manager",
    "initialize_memgpt_manager",
]

