# -*- coding: utf-8 -*-
"""
Think модуль - KAG Reasoning Engine + AG2 Agentic Level.

Компоненты:
- QueryAnalyzer - анализ и классификация запросов
- LogicalFormPlanner - планирование шагов рассуждения
- GraphTraverser - обход графа и выполнение запросов
- ReasoningAgent - multi-hop reasoning
- ResponseSynthesizer - синтез финального ответа
- DeepAnalysisAgent - глубокий анализ
- AdaptiveSearchOrchestrator - адаптивный поиск с fallback
- ReflectionAgent - оценка качества
- DataProfilingAgent - профилирование данных
- NumericalDataSearchAgent - поиск числовых данных
- SmartFilterAgent - интеллектуальная фильтрация

AG2 Agentic Level (v2):
- AG2AgenticEngineV2 - улучшенный движок с современными паттернами
- TessentRAGRetriever - RAG интеграция для обогащения контекста
- Context Variables, Handoffs, Guardrails, Structured Outputs
"""
from .adaptive_search import AdaptiveSearchOrchestrator

# AG2 / autogen — тяжёлая необязательная зависимость (requirements.txt:
# `ag2[openai,gemini]>=0.9`). Раньше эти импорты стояли на верхнем уровне, и
# отсутствие autogen роняло ВЕСЬ backend: любой роут, который тянет
# backend.core.think, падал с ModuleNotFoundError ещё до старта приложения.
# Установка ag2 может не пройти (например, на Python 3.13 колёс может не быть),
# и это не повод не пускать пользователя в память, поиск и чат.
# Теперь ядро поднимается без ag2, а агентные фичи честно недоступны.
try:
    # A2A (Agent-to-Agent) Protocol
    from .ag2_a2a_integration import (
        A2ACallResult,
        A2AServerConfig,
        AgentCardInfo,
        TessentA2AClient,
        TessentA2AOrchestrator,
        TessentA2AServer,
        create_a2a_client,
        create_a2a_orchestrator,
        create_a2a_server,
        setup_tessent_ecosystem,
    )

    # Legacy AG2 Engine (для обратной совместимости)
    from .ag2_engine import AG2ReasoningEngine

    # AG2 Agentic Level V2
    from .ag2_engine_v2 import (
        AG2AgenticEngineV2,
        AgentResponse,
        AnalysisResult,
        DeepAnalysisReport,
        ResearchStep,
        create_ag2_engine,
        create_analysis_context,
    )
    from .ag2_rag_integration import (
        EnrichedQuery,
        RAGContext,
        RetrievedDocument,
        TessentRAGRetriever,
        create_rag_retriever,
        get_rag_context,
    )

    AG2_AVAILABLE = True
    AG2_IMPORT_ERROR = None
except ImportError as _exc:  # pragma: no cover - зависит от окружения
    import logging as _logging

    AG2_AVAILABLE = False
    AG2_IMPORT_ERROR = _exc
    _logging.getLogger(__name__).warning(
        "AG2/autogen недоступен (%s). Память, поиск и чат работают; "
        "агентные возможности (A2A, AG2-движки, RAG-ретривер) выключены. "
        "Чтобы включить: pip install 'ag2[openai,gemini]>=0.9'",
        _exc,
    )

    # Имена объявлены, чтобы `from backend.core.think import X` не падал
    # с ImportError, а дал понятный None на месте использования.
    A2ACallResult = A2AServerConfig = AgentCardInfo = None
    TessentA2AClient = TessentA2AOrchestrator = TessentA2AServer = None
    create_a2a_client = create_a2a_orchestrator = create_a2a_server = None
    setup_tessent_ecosystem = None
    AG2ReasoningEngine = None
    AG2AgenticEngineV2 = AgentResponse = AnalysisResult = None
    DeepAnalysisReport = ResearchStep = None
    create_ag2_engine = create_analysis_context = None
    EnrichedQuery = RAGContext = RetrievedDocument = None
    TessentRAGRetriever = create_rag_retriever = get_rag_context = None
from .data_profiling_agent import DataProfilingAgent
from .deep_analysis_agent import DeepAnalysisAgent
from .graph_traverser import GraphTraverser
from .logical_planner import LogicalFormPlanner, ReasoningPlan, ReasoningStep
from .numerical_search_agent import NumericalDataSearchAgent
from .query_analyzer import QueryAnalyzer, QueryIntent, QueryType
from .reasoning_engine import ReasoningEngine
from .reflection_agent import ReflectionAgent
from .smart_filter_agent import SmartFilterAgent

# Task Specification System - Система генерации ТЗ и выполнения задач
from .task_specification import (
    TaskAnalyzerAgent,
    TaskExecutorAgent,
    TaskSpecificationSystem,
)
from .task_specification.orchestrator import create_task_specification_system

__all__ = [
    "AG2_AVAILABLE",
    "AG2_IMPORT_ERROR",
    "A2ACallResult",
    "A2AServerConfig",
    # AG2 Agentic Level V2
    "AG2AgenticEngineV2",
    # Legacy
    "AG2ReasoningEngine",
    "AdaptiveSearchOrchestrator",
    "AgentCardInfo",
    "AgentResponse",
    "AnalysisResult",
    # Новые агенты
    "DataProfilingAgent",
    # Агенты анализа
    "DeepAnalysisAgent",
    "DeepAnalysisReport",
    "EnrichedQuery",
    "GraphTraverser",
    "LogicalFormPlanner",
    "NumericalDataSearchAgent",
    # KAG Reasoning
    "QueryAnalyzer",
    "QueryIntent",
    "QueryType",
    "RAGContext",
    "ReasoningEngine",
    "ReasoningPlan",
    "ReasoningStep",
    "ReflectionAgent",
    "ResearchStep",
    "RetrievedDocument",
    "SmartFilterAgent",
    "TaskAnalyzerAgent",
    "TaskExecutorAgent",
    # Task Specification System
    "TaskSpecificationSystem",
    "TessentA2AClient",
    "TessentA2AOrchestrator",
    # A2A Protocol
    "TessentA2AServer",
    # RAG Integration
    "TessentRAGRetriever",
    "create_a2a_client",
    "create_a2a_orchestrator",
    "create_a2a_server",
    "create_ag2_engine",
    "create_analysis_context",
    "create_rag_retriever",
    "create_task_specification_system",
    "get_rag_context",
    "setup_tessent_ecosystem",
]
