# -*- coding: utf-8 -*-
"""
Tessbrain - Search Module

Модуль гибридного поиска, объединяющий:
- Vector Search (семантический поиск по эмбеддингам)
- BM25 Search (лексический поиск по ключевым словам)
- Graph Search (поиск по графу знаний)
- RRF Fusion (объединение результатов)
- Enhanced Orchestrator (умная координация с LLM)
"""

from .bm25_searcher import (
    BM25Document,
    BM25Searcher,
    BM25SearchResult,
    get_bm25_searcher,
    tokenize_russian,
)
from .consulting_analyzer import AnalysisResult, ConsultingAnalyzer, get_consulting_analyzer
from .deep_research_engine import DeepResearchEngine, ResearchReport, get_deep_research_engine
from .enhanced_search_orchestrator import (
    EnhancedSearchOrchestrator,
    SearchResult,
    get_enhanced_search_orchestrator,
)
from .hybrid_search_orchestrator import (
    HybridSearchOrchestrator,
    HybridSearchResponse,
    HybridSearchResult,
    HybridSearchStrategy,
    get_hybrid_search_orchestrator,
)
from .reasoning_chains import (
    Evidence,
    Hypothesis,
    ReasoningChainEngine,
    ReasoningResult,
    get_reasoning_chain_engine,
)
from .rrf_fusion import FusedResult, RRFFusion, fuse_multiple_sources, fuse_search_results
from .search_mode_router import SearchMode, SearchModeRouter, get_search_mode_router

__all__ = [
    "AnalysisResult",
    "BM25Document",
    "BM25SearchResult",
    # BM25
    "BM25Searcher",
    # Consulting Analyzer
    "ConsultingAnalyzer",
    # Deep Research Engine
    "DeepResearchEngine",
    # Enhanced Orchestrator
    "EnhancedSearchOrchestrator",
    "Evidence",
    "FusedResult",
    # Hybrid Search
    "HybridSearchOrchestrator",
    "HybridSearchResponse",
    "HybridSearchResult",
    "HybridSearchStrategy",
    "Hypothesis",
    # RRF Fusion
    "RRFFusion",
    # Reasoning Chains
    "ReasoningChainEngine",
    "ReasoningResult",
    "ResearchReport",
    "SearchMode",
    # Search Mode Router
    "SearchModeRouter",
    "SearchResult",
    "fuse_multiple_sources",
    "fuse_search_results",
    "get_bm25_searcher",
    "get_consulting_analyzer",
    "get_deep_research_engine",
    "get_enhanced_search_orchestrator",
    "get_hybrid_search_orchestrator",
    "get_reasoning_chain_engine",
    "get_search_mode_router",
    "tokenize_russian",
]
