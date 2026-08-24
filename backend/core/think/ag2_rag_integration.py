# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - AG2 RAG Integration
====================================

Интеграция Retrieval-Augmented Generation (RAG) с AG2 агентами.

Особенности:
- Гибридный поиск (vector + BM25 + graph)
- Автоматическое обогащение контекста
- Кэширование результатов поиска
- Интеграция с существующей базой знаний Tessbrain
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ==========================================
# PYDANTIC MODELS
# ==========================================

class RetrievedDocument(BaseModel):
    """Извлеченный документ"""
    doc_id: str = Field(..., description="ID документа")
    content: str = Field(..., description="Содержимое")
    source_type: str = Field(..., description="Тип источника (meeting/task/decision/entity)")
    source_id: str = Field("", description="ID источника")
    score: float = Field(0.0, description="Релевантность (0-1)")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RAGContext(BaseModel):
    """Контекст для RAG"""
    query: str = Field(..., description="Исходный запрос")
    documents: List[RetrievedDocument] = Field(default_factory=list)
    total_found: int = Field(0)
    search_time_ms: int = Field(0)
    sources_summary: str = Field("", description="Краткое описание источников")


class EnrichedQuery(BaseModel):
    """Обогащенный запрос"""
    original_query: str
    expanded_queries: List[str] = Field(default_factory=list)
    context_snippet: str = Field("")
    relevant_entities: List[str] = Field(default_factory=list)
    relevant_projects: List[str] = Field(default_factory=list)


# ==========================================
# RAG RETRIEVER
# ==========================================

class TessentRAGRetriever:
    """
    RAG Retriever для Tessbrain.

    Использует существующую инфраструктуру:
    - VectorIndexer для семантического поиска
    - BM25Searcher для ключевого поиска
    - GraphBuilder для графового поиска
    """

    def __init__(
        self,
        vector_indexer=None,
        graph_builder=None,
        bm25_searcher=None,
        llm_router=None,
    ):
        """
        Args:
            vector_indexer: VectorIndexer для семантического поиска
            graph_builder: GraphBuilder для графового поиска
            bm25_searcher: BM25Searcher для ключевого поиска
            llm_router: LLM роутер для query expansion
        """
        self.vector_indexer = vector_indexer
        self.graph_builder = graph_builder
        self.bm25_searcher = bm25_searcher
        self.llm = llm_router

        # Кэш результатов
        self._cache: Dict[str, Tuple[RAGContext, datetime]] = {}
        self._cache_ttl_seconds = 300  # 5 минут

        logger.info(f"✅ TessentRAGRetriever initialized (vector={vector_indexer is not None}, graph={graph_builder is not None}, bm25={bm25_searcher is not None})")

    async def retrieve(
        self,
        query: str,
        top_k: int = 10,
        use_cache: bool = True,
        search_types: List[str] | None = None,
    ) -> RAGContext:
        """
        Извлечь релевантные документы.

        Args:
            query: Поисковый запрос
            top_k: Количество результатов
            use_cache: Использовать кэш
            search_types: Типы поиска ["vector", "bm25", "graph"]

        Returns:
            RAGContext с найденными документами
        """
        start_time = datetime.now()
        search_types = search_types or ["vector", "bm25", "graph"]

        # Проверяем кэш
        cache_key = f"{query}:{top_k}:{','.join(search_types)}"
        if use_cache and cache_key in self._cache:
            cached_result, cached_time = self._cache[cache_key]
            if (datetime.now() - cached_time).total_seconds() < self._cache_ttl_seconds:
                logger.debug(f"📦 RAG cache hit for: {query[:50]}...")
                return cached_result

        all_results = []

        # 1. Vector Search
        if "vector" in search_types and self.vector_indexer:
            try:
                vector_results = await self._vector_search(query, top_k * 2)
                all_results.extend(vector_results)
            except Exception as e:
                logger.warning(f"Vector search error: {e}")

        # 2. BM25 Search
        if "bm25" in search_types and self.bm25_searcher:
            try:
                bm25_results = await self._bm25_search(query, top_k)
                all_results.extend(bm25_results)
            except Exception as e:
                logger.warning(f"BM25 search error: {e}")

        # 3. Graph Search
        if "graph" in search_types and self.graph_builder:
            try:
                graph_results = await self._graph_search(query, top_k)
                all_results.extend(graph_results)
            except Exception as e:
                logger.warning(f"Graph search error: {e}")

        # Дедупликация и ранжирование
        unique_docs = self._deduplicate_and_rank(all_results, top_k)

        # Формируем контекст
        search_time_ms = int((datetime.now() - start_time).total_seconds() * 1000)

        context = RAGContext(
            query=query,
            documents=unique_docs,
            total_found=len(all_results),
            search_time_ms=search_time_ms,
            sources_summary=self._generate_sources_summary(unique_docs),
        )

        # Сохраняем в кэш
        if use_cache:
            self._cache[cache_key] = (context, datetime.now())

        logger.info(f"📚 RAG retrieved {len(unique_docs)} docs in {search_time_ms}ms for: {query[:50]}...")

        return context

    async def _vector_search(self, query: str, top_k: int) -> List[RetrievedDocument]:
        """Семантический поиск через VectorIndexer"""
        results = []

        if hasattr(self.vector_indexer, 'search'):
            # Синхронный метод
            search_results = self.vector_indexer.search(query, top_k=top_k)
        elif hasattr(self.vector_indexer, 'a_search'):
            # Асинхронный метод
            search_results = await self.vector_indexer.a_search(query, top_k=top_k)
        else:
            return results

        for item in search_results:
            doc = RetrievedDocument(
                doc_id=item.get("id", ""),
                content=item.get("text", item.get("content", "")),
                source_type=self._detect_source_type(item.get("id", "")),
                source_id=item.get("meeting_id", item.get("source_id", "")),
                score=item.get("score", 0.0),
                metadata={
                    "search_type": "vector",
                    "title": item.get("title", ""),
                }
            )
            results.append(doc)

        return results

    async def _bm25_search(self, query: str, top_k: int) -> List[RetrievedDocument]:
        """Ключевой поиск через BM25"""
        results = []

        if hasattr(self.bm25_searcher, 'search'):
            search_results = self.bm25_searcher.search(query, top_k=top_k)
        else:
            return results

        for item in search_results:
            doc = RetrievedDocument(
                doc_id=item.get("id", ""),
                content=item.get("text", item.get("content", "")),
                source_type=self._detect_source_type(item.get("id", "")),
                source_id=item.get("meeting_id", ""),
                score=item.get("score", 0.0),
                metadata={
                    "search_type": "bm25",
                }
            )
            results.append(doc)

        return results

    async def _graph_search(self, query: str, top_k: int) -> List[RetrievedDocument]:
        """Графовый поиск через GraphBuilder"""
        results = []

        if not self.graph_builder:
            return results

        # Поиск связанных сущностей
        if hasattr(self.graph_builder, 'search_entities'):
            entities = self.graph_builder.search_entities(query, limit=top_k)

            for entity in entities:
                doc = RetrievedDocument(
                    doc_id=entity.get("id", ""),
                    content=entity.get("description", entity.get("name", "")),
                    source_type="entity",
                    source_id=entity.get("id", ""),
                    score=entity.get("score", 0.5),
                    metadata={
                        "search_type": "graph",
                        "entity_type": entity.get("type", ""),
                        "name": entity.get("name", ""),
                    }
                )
                results.append(doc)

        return results

    def _detect_source_type(self, doc_id: str) -> str:
        """Определить тип источника по ID"""
        if not doc_id:
            return "unknown"

        doc_id_lower = doc_id.lower()

        if "meeting" in doc_id_lower or "chunk_" in doc_id_lower:
            return "meeting"
        elif "task" in doc_id_lower:
            return "task"
        elif "decision" in doc_id_lower:
            return "decision"
        elif "entity" in doc_id_lower:
            return "entity"
        elif "project" in doc_id_lower:
            return "project"
        elif "person" in doc_id_lower:
            return "person"
        else:
            return "document"

    def _deduplicate_and_rank(
        self,
        results: List[RetrievedDocument],
        top_k: int,
    ) -> List[RetrievedDocument]:
        """Дедупликация и ранжирование результатов"""
        # Дедупликация по doc_id
        seen_ids = set()
        unique_results = []

        for doc in results:
            if doc.doc_id not in seen_ids:
                seen_ids.add(doc.doc_id)
                unique_results.append(doc)

        # RRF (Reciprocal Rank Fusion) для комбинирования результатов
        # Группируем по search_type
        by_type: Dict[str, List[RetrievedDocument]] = {}
        for doc in unique_results:
            search_type = doc.metadata.get("search_type", "unknown")
            if search_type not in by_type:
                by_type[search_type] = []
            by_type[search_type].append(doc)

        # Вычисляем RRF score
        k = 60  # RRF параметр
        rrf_scores: Dict[str, float] = {}

        for search_type, docs in by_type.items():
            for rank, doc in enumerate(docs):
                if doc.doc_id not in rrf_scores:
                    rrf_scores[doc.doc_id] = 0
                rrf_scores[doc.doc_id] += 1 / (k + rank + 1)

        # Обновляем score и сортируем
        for doc in unique_results:
            doc.score = rrf_scores.get(doc.doc_id, doc.score)

        unique_results.sort(key=lambda x: x.score, reverse=True)

        return unique_results[:top_k]

    def _generate_sources_summary(self, docs: List[RetrievedDocument]) -> str:
        """Генерация краткого описания источников"""
        if not docs:
            return "Источники не найдены"

        source_counts: Dict[str, int] = {}
        for doc in docs:
            source_type = doc.source_type
            source_counts[source_type] = source_counts.get(source_type, 0) + 1

        parts = []
        for source_type, count in source_counts.items():
            parts.append(f"{count} {source_type}")

        return f"Найдено: {', '.join(parts)}"

    async def expand_query(self, query: str) -> EnrichedQuery:
        """
        Расширение запроса с помощью LLM.

        Args:
            query: Исходный запрос

        Returns:
            EnrichedQuery с расширенными запросами
        """
        enriched = EnrichedQuery(original_query=query)

        if not self.llm:
            return enriched

        try:
            prompt = f"""Проанализируй запрос пользователя и предложи 2-3 альтернативные формулировки для поиска в корпоративной базе знаний.

ЗАПРОС: {query}

Верни JSON:
{{
    "expanded_queries": ["альтернатива 1", "альтернатива 2"],
    "relevant_entities": ["возможные имена/названия"],
    "relevant_projects": ["возможные проекты"]
}}

Только JSON, без markdown."""

            response = await self.llm.generate(prompt, model_tier="standard")

            if response:
                try:
                    data = json.loads(response)
                    enriched.expanded_queries = data.get("expanded_queries", [])
                    enriched.relevant_entities = data.get("relevant_entities", [])
                    enriched.relevant_projects = data.get("relevant_projects", [])
                except json.JSONDecodeError:
                    logger.debug("suppressed exception", exc_info=True)

        except Exception as e:
            logger.warning(f"Query expansion error: {e}")

        return enriched

    def format_context_for_agent(
        self,
        context: RAGContext,
        max_length: int = 4000,
    ) -> str:
        """
        Форматирование контекста для агента.

        Args:
            context: RAG контекст
            max_length: Максимальная длина

        Returns:
            Отформатированный контекст
        """
        if not context.documents:
            return "В базе знаний не найдено релевантной информации."

        parts = []
        current_length = 0

        parts.append(f"📚 КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ ({context.sources_summary}):\n")

        for i, doc in enumerate(context.documents, 1):
            doc_text = f"\n[{i}] {doc.source_type.upper()}"
            if doc.metadata.get("title"):
                doc_text += f" - {doc.metadata['title']}"
            doc_text += f"\n{doc.content}\n"

            if current_length + len(doc_text) > max_length:
                break

            parts.append(doc_text)
            current_length += len(doc_text)

        parts.append("\n---\n")

        return "".join(parts)


# ==========================================
# RAG-ENHANCED AGENT MIXIN
# ==========================================

class RAGEnhancedAgentMixin:
    """
    Mixin для добавления RAG возможностей к агентам.
    """

    def __init__(self, rag_retriever: TessentRAGRetriever = None, **kwargs):
        super().__init__(**kwargs)
        self.rag_retriever = rag_retriever

    async def enrich_message_with_rag(
        self,
        message: str,
        top_k: int = 5,
    ) -> str:
        """
        Обогатить сообщение контекстом из RAG.

        Args:
            message: Исходное сообщение
            top_k: Количество документов

        Returns:
            Обогащенное сообщение
        """
        if not self.rag_retriever:
            return message

        try:
            context = await self.rag_retriever.retrieve(message, top_k=top_k)

            if context.documents:
                context_text = self.rag_retriever.format_context_for_agent(context)
                return f"{context_text}\n\nЗАПРОС ПОЛЬЗОВАТЕЛЯ: {message}"

        except Exception as e:
            logger.warning(f"RAG enrichment error: {e}")

        return message


# ==========================================
# FACTORY FUNCTIONS
# ==========================================

def create_rag_retriever(
    vector_indexer=None,
    graph_builder=None,
    bm25_searcher=None,
    llm_router=None,
) -> TessentRAGRetriever:
    """
    Создать RAG Retriever.

    Args:
        vector_indexer: VectorIndexer
        graph_builder: GraphBuilder
        bm25_searcher: BM25Searcher
        llm_router: LLM роутер

    Returns:
        TessentRAGRetriever
    """
    return TessentRAGRetriever(
        vector_indexer=vector_indexer,
        graph_builder=graph_builder,
        bm25_searcher=bm25_searcher,
        llm_router=llm_router,
    )


async def get_rag_context(
    query: str,
    retriever: TessentRAGRetriever,
    top_k: int = 10,
) -> str:
    """
    Получить RAG контекст для запроса.

    Args:
        query: Запрос
        retriever: RAG Retriever
        top_k: Количество документов

    Returns:
        Отформатированный контекст
    """
    context = await retriever.retrieve(query, top_k=top_k)
    return retriever.format_context_for_agent(context)

