# -*- coding: utf-8 -*-
"""
Hybrid Search Orchestrator - Координатор гибридного поиска.

Объединяет:
- Vector Search (семантический поиск по эмбеддингам)
- BM25 Search (лексический поиск по ключевым словам)
- Graph Search (поиск по графу знаний)

Использует RRF Fusion для объединения результатов.

Стратегии поиска:
- SEMANTIC_FIRST: Приоритет векторному поиску
- LEXICAL_FIRST: Приоритет BM25
- BALANCED: Равные веса
- ADAPTIVE: Автоматический выбор на основе запроса
"""
import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .bm25_searcher import BM25Searcher, BM25SearchResult, get_bm25_searcher
from .rrf_fusion import RRFFusion

logger = logging.getLogger(__name__)


class HybridSearchStrategy(Enum):
    """Стратегии гибридного поиска"""
    SEMANTIC_FIRST = "semantic_first"  # Приоритет векторному поиску
    LEXICAL_FIRST = "lexical_first"    # Приоритет BM25
    BALANCED = "balanced"              # Равные веса
    ADAPTIVE = "adaptive"              # Автоматический выбор


@dataclass
class HybridSearchResult:
    """Результат гибридного поиска"""
    doc_id: str
    score: float
    text: str
    metadata: Dict[str, Any]
    sources: List[str]
    vector_score: Optional[float] = None
    bm25_score: Optional[float] = None
    graph_score: Optional[float] = None
    matched_terms: List[str] = field(default_factory=list)


@dataclass
class HybridSearchResponse:
    """Полный ответ гибридного поиска"""
    results: List[HybridSearchResult]
    query: str
    strategy_used: str
    total_found: int
    search_time_ms: int
    stats: Dict[str, Any] = field(default_factory=dict)


class HybridSearchOrchestrator:
    """
    Оркестратор гибридного поиска.

    Координирует:
    1. Векторный поиск (VectorIndexer)
    2. Лексический поиск (BM25Searcher)
    3. Поиск по графу (GraphBuilder)
    4. RRF Fusion для объединения

    Использование:
    ```python
    orchestrator = HybridSearchOrchestrator(
        vector_indexer=vector_indexer,
        graph_builder=graph_builder,
        user_id="user123"
    )

    response = await orchestrator.search(
        query="статус проекта Tessbrain",
        strategy=HybridSearchStrategy.ADAPTIVE,
        top_k=20
    )
    ```
    """

    def __init__(
        self,
        vector_indexer=None,
        graph_builder=None,
        bm25_searcher: Optional[BM25Searcher] = None,
        user_id: Optional[str] = None,
        rrf_k: int = 60
    ):
        """
        Args:
            vector_indexer: VectorIndexer для семантического поиска
            graph_builder: GraphBuilder для поиска по графу
            bm25_searcher: BM25Searcher для лексического поиска
            user_id: ID пользователя для per-user индексов
            rrf_k: Константа RRF
        """
        self.vector_indexer = vector_indexer
        self.graph_builder = graph_builder
        self.user_id = user_id
        self.rrf_k = rrf_k

        # BM25 Searcher (per-user если указан user_id)
        self.bm25 = bm25_searcher or get_bm25_searcher(user_id=user_id)

        # Веса по умолчанию для разных стратегий
        self.strategy_weights = {
            HybridSearchStrategy.SEMANTIC_FIRST: {
                "vector": 1.0,
                "bm25": 0.5,
                "graph": 0.7
            },
            HybridSearchStrategy.LEXICAL_FIRST: {
                "vector": 0.5,
                "bm25": 1.0,
                "graph": 0.7
            },
            HybridSearchStrategy.BALANCED: {
                "vector": 1.0,
                "bm25": 1.0,
                "graph": 0.8
            },
            HybridSearchStrategy.ADAPTIVE: {
                "vector": 1.0,
                "bm25": 0.8,
                "graph": 0.7
            }
        }

        # Статистика
        self.stats = {
            "total_searches": 0,
            "vector_searches": 0,
            "bm25_searches": 0,
            "graph_searches": 0,
            "avg_results": 0.0
        }

    async def _empty_results(self) -> List[Any]:
        """Возвращает пустой список (для asyncio.gather)"""
        return []

    async def search(
        self,
        query: str,
        strategy: HybridSearchStrategy = HybridSearchStrategy.ADAPTIVE,
        top_k: int = 20,
        filters: Optional[Dict[str, Any]] = None,
        collections: Optional[List[str]] = None,
        include_graph: bool = True,
        min_score: float = 0.0
    ) -> HybridSearchResponse:
        """
        Выполнить гибридный поиск.

        Args:
            query: Поисковый запрос
            strategy: Стратегия поиска
            top_k: Количество результатов
            filters: Фильтры по метаданным
            collections: Коллекции для векторного поиска
            include_graph: Включить поиск по графу
            min_score: Минимальный score

        Returns:
            HybridSearchResponse с результатами
        """
        import time
        start_time = time.time()

        self.stats["total_searches"] += 1

        # Определяем стратегию
        if strategy == HybridSearchStrategy.ADAPTIVE:
            strategy = self._detect_optimal_strategy(query)
            logger.info(f"🎯 Adaptive strategy selected: {strategy.value}")

        weights = self.strategy_weights[strategy]

        # Перечислительный запрос («покажи всех, кто…», «сколько…», «и в A,
        # и в B») → поднимаем вес графа и прижимаем вектор. Причина —
        # доказанный предел одновекторного поиска (arXiv 2508.21038): число
        # top-k подмножеств, которые вектор способен вернуть, ограничено его
        # размерностью; перечисление и пересечение — худший для него класс.
        # Осторожно по построению: ни один канал не отключается — меняются
        # только веса слияния; детектор со строгими паттернами (лучше
        # пропустить, чем ложно сдвинуть ранги); выключатель ENUM_GRAPH_BOOST.
        _verdict = None   # виден и ниже — уверенному структурному ответу
        try:
            import os as _os
            if _os.getenv("ENUM_GRAPH_BOOST", "on").strip().lower() in (
                    "1", "on", "true", "yes"):
                from backend.core.search.enumerative_detect import detect
                _verdict = detect(query)
                if _verdict:
                    weights = dict(weights)
                    weights["graph"] = max(weights.get("graph", 0.7), 1.3)
                    weights["vector"] = min(weights.get("vector", 1.0), 0.6)
                    self.stats["enumerative_queries"] = (
                        self.stats.get("enumerative_queries", 0) + 1)
                    logger.info(
                        "🧮 Enumerative query (%s: %r) → graph-канал усилен",
                        _verdict.kind, _verdict.matched)
        except Exception:
            logger.debug("enumerative detect skipped", exc_info=True)

        # Запускаем поиски параллельно
        tasks = []

        # Vector search (фильтры выбора встреч/документов теперь доходят
        # и сюда — раньше их получал только BM25, и то со сломанным
        # сравнением списка со скаляром)
        if self.vector_indexer:
            tasks.append(self._vector_search(query, top_k * 2, collections,
                                             filters))
            self.stats["vector_searches"] += 1
        else:
            tasks.append(self._empty_results())

        # BM25 search
        tasks.append(self._bm25_search(query, top_k * 2, filters))
        self.stats["bm25_searches"] += 1

        # Graph search (мягкий фильтр: узлы с чужим meeting_id/document_id
        # отбрасываются, кросс-сущности без привязки остаются)
        if include_graph and self.graph_builder:
            tasks.append(self._graph_search(query, top_k * 2, filters))
            self.stats["graph_searches"] += 1
        else:
            tasks.append(self._empty_results())

        # Выполняем параллельно
        vector_results, bm25_results, graph_results = await asyncio.gather(*tasks)

        # RRF Fusion
        fusion = RRFFusion(k=self.rrf_k)

        if vector_results:
            fusion.add_results(
                "vector",
                vector_results,
                weight=weights["vector"],
                id_field="id",
                score_field="score"
            )

        if bm25_results:
            # Конвертируем BM25SearchResult в dict
            bm25_dicts = [
                {
                    "id": r.doc_id,
                    "score": r.score,
                    "text": r.text,
                    "metadata": r.metadata,
                    "matched_terms": r.matched_terms
                }
                for r in bm25_results
            ]
            fusion.add_results(
                "bm25",
                bm25_dicts,
                weight=weights["bm25"]
            )

        if graph_results:
            fusion.add_results(
                "graph",
                graph_results,
                weight=weights["graph"]
            )

        # Получаем объединённые результаты. При включённом реранкере пул
        # шире (top_k*3): второй проход читает кандидатов и переставляет,
        # обрезка до top_k — после него; выключен → прежнее top_k байт-в-байт.
        try:
            from backend.core.search.reranker import reranker_enabled
            _rr_on = reranker_enabled()
        except Exception:
            _rr_on = False
        fused = fusion.fuse(top_k=top_k * 3 if _rr_on else top_k)

        # Реранкер вторым проходом (arXiv 2508.21038: кросс-энкодер — выход
        # из предела одновекторного поиска; у gbrain стоит в бою). Default
        # OFF — новый код, на наших данных не проверен. Переставляет, не
        # фильтрует; сбой/таймаут → исходный порядок.
        if _rr_on:
            try:
                from backend.core.search.reranker import rerank
                fused = await rerank(
                    query, fused,
                    text_of=lambda fr: (fr.data.get("text")
                                        or fr.data.get("content")
                                        or fr.data.get("title") or ""),
                )
                fused = fused[:top_k]
            except Exception as e:
                logger.debug(f"reranker skipped (non-critical): {e}")
                fused = fused[:top_k]

        # Темпоральный буст по близости даты документа к now
        # (MEMORY_DESIGN_PRINCIPLES.md §6 №2). За флагом OFF → no-op. Гарантии:
        # buster ≤+20%, релевантность не доминируется; без дат — буст=0; оригиналы
        # не мутируются (используем обёртку). Реализовано в temporal_rerank.
        try:
            from backend.core.config.feature_flags import get_feature_flags
            if get_feature_flags().enable_temporal_retrieval_boost:
                from backend.core.search.temporal_rerank import apply_temporal_rerank
                # query прокинут ради диапазонного режима: «что решили в марте»
                # бустит документы ВНУТРИ марта, а не свежие (буст свежести на
                # таких вопросах тянул в противоположную сторону). Периода в
                # вопросе нет → прежний буст свежести байт-в-байт.
                fused = apply_temporal_rerank(fused, query=query)
        except Exception as e:
            logger.debug(f"Temporal rerank skipped (non-critical): {e}")

        # Уверенный структурный ответ (CONFIDENT_GRAPH, default OFF).
        # «Кто был на встрече X» → если граф дал узлы-Person и вопрос
        # реляционный, отдаём ТОЛЬКО их, без добора лексическим шумом.
        # Условия и гарантии — в confident_graph.py; выключено → байт-в-байт
        # прежняя выдача. Сбой → прежняя выдача (никогда не роняем поиск).
        try:
            from backend.core.search.confident_graph import (
                confident_enabled, pick_confident)
            if confident_enabled() and _verdict:
                from backend.core.search.answer_type import (
                    expected_answer_type, PERSON)
                _exp = expected_answer_type(query)
                if _exp == PERSON:
                    _typed_ids = [
                        str(g.get("id") or "")
                        for g in (graph_results or [])
                        if isinstance(g, dict)
                        and str((g.get("metadata") or {}).get("type")
                                or g.get("type") or "").lower() == "person"]
                    _picked = pick_confident(
                        fused, _typed_ids,
                        verdict_truthy=True, expected_type=_exp)
                    if _picked is not None:
                        fused = _picked
                        self.stats["confident_graph"] = (
                            self.stats.get("confident_graph", 0) + 1)
                        logger.info(
                            "🎯 Уверенный структурный ответ: %d узлов-Person",
                            len(fused))
        except Exception:
            logger.debug("confident graph skipped", exc_info=True)

        # Конвертируем в HybridSearchResult
        results = []
        for fr in fused:
            if fr.rrf_score < min_score:
                continue

            # Извлекаем текст - пробуем разные поля (text, content, summary, description, title)
            text = fr.data.get("text", "")
            if not text:
                text = fr.data.get("content", "")
            if not text:
                text = fr.data.get("summary", "")
            if not text:
                text = fr.data.get("description", "")
            if not text:
                # Для задач/решений - собираем из нескольких полей
                title = fr.data.get("title", "")
                desc = fr.data.get("description", "")
                if title or desc:
                    text = f"{title}. {desc}".strip(". ")
            if not text:
                # Для сущностей
                text = fr.data.get("name", "")

            metadata = fr.data.get("metadata", {})
            if not metadata:
                metadata = {k: v for k, v in fr.data.items() if k not in ["id", "text", "content", "score", "vector"]}

            results.append(HybridSearchResult(
                doc_id=fr.doc_id,
                score=fr.rrf_score,
                text=text,
                metadata=metadata,
                sources=fr.sources,
                vector_score=fr.original_scores.get("vector"),
                bm25_score=fr.original_scores.get("bm25"),
                graph_score=fr.original_scores.get("graph"),
                matched_terms=fr.data.get("matched_terms", [])
            ))

        # Агрегирующий запрос → расширение «хит → вся встреча». Живой замер
        # на харнессе: суммы и счёт разбросаны по разговорам, top-K по
        # релевантности не достаёт все вхождения (multi-session 0.23–0.43),
        # подача разговоров с попаданиями целиком даёт 0.57. Осторожно по
        # построению: только при строгой детекции агрегации; ничего не
        # удаляется — куски встреч ДОБАВЛЯЮТСЯ в хвост с score=0 (мы не
        # выдумываем релевантность, которой не меряли) и пометкой источника;
        # кап добавки; фильтры выбора встреч уважаются; сбой → пропуск.
        # Выключатель AGGREGATE_EXPAND=off.
        try:
            import os as _os
            if _os.getenv("AGGREGATE_EXPAND", "on").strip().lower() in (
                    "1", "on", "true", "yes") and results:
                from backend.core.search.enumerative_detect import detect
                _v = detect(query)
                if _v and _v.kind == "count":
                    _expanded = self._expand_hit_meetings(results, filters)
                    if _expanded:
                        results.extend(_expanded)
                        self.stats["aggregate_expansions"] = (
                            self.stats.get("aggregate_expansions", 0) + 1)
                        logger.info(
                            "🧮 Aggregate expand: +%d кусков из встреч с "
                            "попаданиями (%r)", len(_expanded), _v.matched)
        except Exception:
            logger.debug("aggregate expand skipped", exc_info=True)

        # Вычисляем время
        search_time_ms = int((time.time() - start_time) * 1000)

        # Обновляем статистику
        total_results = len(results)
        self.stats["avg_results"] = (
            (self.stats["avg_results"] * (self.stats["total_searches"] - 1) + total_results)
            / self.stats["total_searches"]
        )

        logger.info(
            f"🔍 Hybrid search: {total_results} results in {search_time_ms}ms "
            f"(vector={len(vector_results)}, bm25={len(bm25_results)}, graph={len(graph_results)})"
        )

        return HybridSearchResponse(
            results=results,
            query=query,
            strategy_used=strategy.value,
            total_found=total_results,
            search_time_ms=search_time_ms,
            stats={
                "vector_results": len(vector_results),
                "bm25_results": len(bm25_results),
                "graph_results": len(graph_results),
                "fusion_stats": fusion.get_stats()
            }
        )

    async def _vector_search(
        self,
        query: str,
        top_k: int,
        collections: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Векторный поиск"""
        if not self.vector_indexer:
            logger.warning("⚠️ Vector search skipped: no vector_indexer")
            return []

        if not self.vector_indexer.connected:
            logger.warning("⚠️ Vector search skipped: vector_indexer not connected")
            return []

        try:
            # Используем метод search из VectorIndexer
            # score_threshold=0.0 - минимальный порог, чтобы получить все результаты
            # RRF fusion сам отфильтрует нерелевантные
            results = await self.vector_indexer.search(
                query=query,
                limit=top_k,  # VectorIndexer uses 'limit' instead of 'top_k'
                collections=collections,
                score_threshold=0.0,  # Минимальный порог для гибридного поиска
                filter_conditions=filters or None
            )

            logger.debug(f"🔍 Vector search raw results: {len(results)} for query '{query[:50]}...'")

            # Нормализуем формат и извлекаем текст из разных полей
            normalized = []
            for r in results:
                if isinstance(r, dict):
                    # Пробуем извлечь текст из разных полей
                    text = r.get("text", "")
                    if not text:
                        text = r.get("content", "")
                    if not text:
                        text = r.get("summary", "")
                    if not text:
                        text = r.get("description", "")
                    if not text:
                        # Собираем из title + description
                        title = r.get("title", r.get("name", ""))
                        desc = r.get("description", r.get("summary", ""))
                        if title or desc:
                            text = f"{title}. {desc}".strip(". ")

                    # Метаданные - все поля кроме id, score, vector
                    metadata = r.get("metadata", {})
                    if not metadata:
                        metadata = {k: v for k, v in r.items()
                                   if k not in ["id", "doc_id", "score", "vector", "text", "content"]}

                    normalized.append({
                        "id": r.get("id", r.get("doc_id", "")),
                        "score": r.get("score", 0.0),
                        "text": text,
                        "content": text,  # Дублируем для совместимости
                        "metadata": metadata
                    })
                else:
                    # Если это объект
                    text = getattr(r, "text", "") or getattr(r, "content", "") or getattr(r, "summary", "") or getattr(r, "description", "")
                    normalized.append({
                        "id": getattr(r, "id", getattr(r, "doc_id", "")),
                        "score": getattr(r, "score", 0.0),
                        "text": text,
                        "content": text,
                        "metadata": getattr(r, "metadata", {})
                    })

            return normalized

        except Exception as e:
            logger.error(f"❌ Vector search error: {e}")
            return []

    async def _bm25_search(
        self,
        query: str,
        top_k: int,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[BM25SearchResult]:
        """BM25 лексический поиск"""
        try:
            results = self.bm25.search(
                query=query,
                top_k=top_k,
                filters=filters
            )
            return results

        except Exception as e:
            logger.error(f"❌ BM25 search error: {e}")
            return []

    def _expand_hit_meetings(self, results: List["HybridSearchResult"],
                             filters: Optional[Dict[str, Any]] = None,
                             *, top_hits: int = 5, max_meetings: int = 3,
                             cap: int = 24) -> List["HybridSearchResult"]:
        """Куски встреч, куда попали верхние хиты, — целиком (для агрегации).

        Только из BM25-хранилища (оно уже в памяти, ноль обращений куда-либо);
        встречи — из верхних top_hits результатов, не более max_meetings;
        кап на добавку — расширение дополняет контекст, а не заливает его.
        score=0 и sources=["expansion"] — честная пометка: релевантность
        этих кусков не мерилась, они здесь ради полноты агрегата.
        """
        docs = getattr(self.bm25, "documents", None) or {}
        if not docs:
            return []
        top_meetings: List[str] = []
        for r in results[:top_hits]:
            mid = (r.metadata or {}).get("meeting_id")
            if mid and not isinstance(mid, (list, tuple)) \
                    and mid not in top_meetings:
                top_meetings.append(mid)
            if len(top_meetings) >= max_meetings:
                break
        if not top_meetings:
            return []
        existing = {r.doc_id for r in results}
        from backend.core.search.filter_match import metadata_matches_filters
        out: List[HybridSearchResult] = []
        for doc_id, doc in docs.items():
            if doc_id in existing:
                continue
            meta = doc.metadata or {}
            if meta.get("meeting_id") not in top_meetings:
                continue
            if filters and not metadata_matches_filters(meta, filters):
                continue
            out.append(HybridSearchResult(
                doc_id=doc_id, score=0.0, text=doc.text or "",
                metadata=meta, sources=["expansion"]))
            if len(out) >= cap:
                break
        return out

    async def _graph_search(
        self,
        query: str,
        top_k: int,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Поиск по графу знаний с учётом весов узлов и рёбер."""
        if not self.graph_builder:
            return []

        try:
            # Проверяем включены ли веса
            use_weights = False
            try:
                from backend.core.config import flags
                use_weights = flags.enable_graph_weights
            except ImportError:
                logger.debug("suppressed exception", exc_info=True)

            # Ищем узлы по имени/названию
            results = []

            # Поиск по ВСЕМ типам узлов, которые создаются при sync
            # Базовые (Tier 1): Person, Task, Decision, Meeting, Entity, KPI
            # Расширенные: Project, Product, Team, Department
            # Знания: Knowledge, Regulation, Template
            # Аналитика: Risk, Contradiction, PsychologicalProfile, Scenario
            node_types = [
                # Люди и организация
                "Person", "Team", "Department",
                # Проекты и продукты
                "Project", "Product",
                # События и решения
                "Meeting", "Decision", "Task",
                # Сущности и метрики
                "Entity", "KPI",
                # Знания (14 категорий: cases, methodologies, templates, regulations, best_practices...)
                "Knowledge", "Regulation", "Template",
                # Аналитика (Tier 3+)
                "Risk", "Contradiction",
                # Психология и прогнозы
                "PsychologicalProfile", "Scenario",
            ]

            for node_type in node_types:
                nodes = await self.graph_builder.find_nodes_by_label(
                    label=node_type,
                    limit=top_k // len(node_types) + 1
                )

                # Фильтруем по релевантности к запросу
                query_lower = query.lower()
                query_words = set(query_lower.split())

                for node in nodes:
                    # defense-in-depth: чужой private-узел (скопирован в
                    # org-граф до фикса promote) не показываем; СВОЙ private
                    # (personal-граф, без promoted_from) остаётся владельцу
                    if str(node.get("access_group") or "").lower() == "private":
                        promoted_from = node.get("promoted_from_user_id")
                        if promoted_from and promoted_from != self.user_id:
                            continue
                    # мягкий фильтр выбора: узел с СОБСТВЕННОЙ привязкой к
                    # другой встрече/документу — мимо; узлы без привязки
                    # (Person/Project/KPI — кросс-встречные) остаются
                    if filters:
                        from backend.core.search.filter_match import graph_node_passes_filters
                        if not graph_node_passes_filters(node, filters):
                            continue
                    name = str(node.get("name", node.get("title", ""))).lower()
                    description = str(node.get("description", node.get("summary", ""))).lower()

                    # Дополнительные текстовые поля для расширенных типов узлов
                    extra_text = ""
                    if node_type == "Knowledge":
                        extra_text = str(node.get("knowledge_category", node.get("category", ""))).lower()
                    elif node_type == "Risk":
                        extra_text = str(node.get("mitigation", "")).lower()
                    elif node_type == "PsychologicalProfile":
                        extra_text = str(node.get("personality_traits", "")).lower()
                    elif node_type == "Contradiction":
                        extra_text = f"{node.get('new_fact', '')} {node.get('existing_fact', '')}".lower()
                    elif node_type == "Product":
                        extra_text = f"{node.get('features', '')} {node.get('target_audience', '')}".lower()
                    elif node_type == "Scenario":
                        extra_text = str(node.get("conditions", "")).lower()

                    # Объединяем весь текст для поиска
                    full_text = f"{name} {description} {extra_text}".strip()
                    text_words = set(full_text.split())

                    # Простой scoring по совпадению слов
                    overlap = len(query_words & text_words)

                    # Бонус за частичное совпадение (substring)
                    substring_bonus = 0
                    for qw in query_words:
                        if len(qw) > 3:  # Только для слов длиннее 3 символов
                            for tw in text_words:
                                if qw in tw or tw in qw:
                                    substring_bonus += 0.3

                    base_score = overlap / max(len(query_words), 1) + substring_bonus

                    # НОВОЕ: Учитываем веса узлов
                    if use_weights and base_score > 0.1:
                        node_weight = node.get("weight", 1.0)
                        frequency_score = node.get("frequency_score", 0.0)
                        emotional_salience = node.get("emotional_salience", 0.0)

                        # Формула: base × (1 + α×weight + β×freq + γ×emotion)
                        weight_multiplier = (
                            1.0
                            + 0.10 * (node_weight - 1.0)
                            + 0.05 * frequency_score
                            + 0.15 * emotional_salience
                        )
                        total_score = base_score * max(weight_multiplier, 0.1)
                    else:
                        total_score = base_score

                    # Добавляем все узлы с хоть каким-то совпадением
                    if total_score > 0.1:
                        # Собираем полный текст для контекста
                        full_text_content = node.get("name", node.get("title", ""))
                        desc = node.get("description", node.get("summary", ""))
                        if desc:
                            full_text_content = f"{full_text_content}. {desc}"

                        # GROUNDING: Подтягиваем текст чанков
                        chunks_text = ""
                        try:
                            if hasattr(self.graph_builder, 'get_node_chunks'):
                                chunks = await self.graph_builder.get_node_chunks(node.get("id", ""))
                                if chunks:
                                    chunks_text = "\n\nЦитаты:\n" + "\n".join([f"- {c}" for c in chunks[:3]])
                                    full_text_content += chunks_text
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to get chunks for node {node.get('id')}: {e}")

                        # Собираем расширенные метаданные в зависимости от типа узла
                        node_metadata = {
                            "type": node_type,
                            "name": node.get("name", node.get("title", "")),
                            "description": desc,
                            "meeting_id": node.get("meeting_id", node.get("source_meeting_id", "")),
                            # Веса для ранжирования
                            "weight": node.get("weight", 1.0),
                            "frequency_score": node.get("frequency_score", 0.0),
                            "emotional_salience": node.get("emotional_salience", 0.0),
                            "has_chunks": bool(chunks_text),
                        }

                        # Расширенные метаданные по типам
                        if node_type == "Knowledge":
                            node_metadata["knowledge_category"] = node.get("knowledge_category", node.get("category", ""))
                        elif node_type == "Risk":
                            node_metadata["probability"] = node.get("probability", 0)
                            node_metadata["impact"] = node.get("impact", "")
                        elif node_type == "KPI":
                            node_metadata["numeric_value"] = node.get("numeric_value")
                            node_metadata["unit"] = node.get("unit", "")
                            node_metadata["trend"] = node.get("trend", "")
                            node_metadata["category"] = node.get("category", "")
                        elif node_type == "Product":
                            node_metadata["features"] = node.get("features", "")
                            node_metadata["target_audience"] = node.get("target_audience", "")
                        elif node_type == "Contradiction":
                            node_metadata["new_fact"] = node.get("new_fact", "")
                            node_metadata["existing_fact"] = node.get("existing_fact", "")
                        elif node_type == "Task":
                            node_metadata["status"] = node.get("status", "")
                            node_metadata["priority"] = node.get("priority", "")
                            node_metadata["assignee"] = node.get("assignee", "")
                        elif node_type == "Decision":
                            node_metadata["decision_category"] = node.get("category", "")
                            node_metadata["confidence"] = node.get("confidence", 0)
                        elif node_type == "Person":
                            node_metadata["role"] = node.get("role", "")
                            node_metadata["department"] = node.get("department", "")

                        results.append({
                            "id": node.get("id", ""),
                            "score": total_score,
                            "text": full_text_content,
                            "content": full_text_content,
                            "metadata": node_metadata,
                        })

            # Сортируем по score
            results.sort(key=lambda x: x["score"], reverse=True)

            logger.info(f"📊 Graph search found {len(results)} results for query: {query[:50]}...")

            return results[:top_k]

        except Exception as e:
            logger.error(f"❌ Graph search error: {e}")
            return []

    async def causal_chain_search(
        self,
        query: str,
        top_k: int = 10,
        max_depth: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Поиск каузальных цепочек: "почему X?" → причина → причина причины.

        Алгоритм:
        1. Graph search находит релевантные узлы
        2. Для каждого узла обходим связи CAUSED_BY, LED_TO, RESULTED_IN
        3. Строим цепочку причин до max_depth уровней

        Returns:
            Список каузальных цепочек [{chain: [...], explanation: "..."}]
        """
        if not self.graph_builder:
            return []

        try:
            # 1. Находим стартовые узлы через обычный graph search
            seed_results = await self._graph_search(query, top_k=top_k)

            if not seed_results:
                return []

            # Типы каузальных связей
            causal_rel_types = {
                "CAUSED_BY", "LED_TO", "RESULTED_IN", "INFLUENCED",
                "BLOCKED_BY", "DEPENDS_ON", "TRIGGERED",
            }

            chains = []
            visited = set()

            for seed in seed_results[:5]:  # Ограничиваем стартовые узлы
                seed_id = seed.get("id", "")
                if not seed_id or seed_id in visited:
                    continue

                visited.add(seed_id)

                # 2. BFS по каузальным связям
                chain = [{
                    "id": seed_id,
                    "name": seed.get("metadata", {}).get("name", seed_id),
                    "type": seed.get("metadata", {}).get("type", "Unknown"),
                    "text": seed.get("text", ""),
                    "depth": 0,
                }]

                current_ids = [seed_id]

                for depth in range(1, max_depth + 1):
                    next_ids = []

                    for node_id in current_ids:
                        try:
                            rels = await self.graph_builder.get_node_relationships(node_id)

                            # Исходящие каузальные связи
                            for rel in rels.get("outgoing", []):
                                rel_type = rel.get("type", rel.get("_type", ""))
                                target_id = rel.get("target", "")

                                if rel_type in causal_rel_types and target_id and target_id not in visited:
                                    visited.add(target_id)
                                    target_node = await self.graph_builder.get_node_by_id(target_id)

                                    if target_node:
                                        chain.append({
                                            "id": target_id,
                                            "name": target_node.get("name", target_node.get("title", target_id)),
                                            "type": target_node.get("_label", "Unknown"),
                                            "relation": rel_type,
                                            "text": target_node.get("description", target_node.get("summary", "")),
                                            "depth": depth,
                                        })
                                        next_ids.append(target_id)

                            # Входящие каузальные связи (обратные)
                            for rel in rels.get("incoming", []):
                                rel_type = rel.get("type", rel.get("_type", ""))
                                source_id = rel.get("source", "")

                                if rel_type in causal_rel_types and source_id and source_id not in visited:
                                    visited.add(source_id)
                                    source_node = await self.graph_builder.get_node_by_id(source_id)

                                    if source_node:
                                        chain.append({
                                            "id": source_id,
                                            "name": source_node.get("name", source_node.get("title", source_id)),
                                            "type": source_node.get("_label", "Unknown"),
                                            "relation": f"(reverse) {rel_type}",
                                            "text": source_node.get("description", source_node.get("summary", "")),
                                            "depth": depth,
                                        })
                                        next_ids.append(source_id)

                        except Exception as e:
                            logger.warning(f"⚠️ Causal traversal error for {node_id}: {e}")

                    current_ids = next_ids
                    if not next_ids:
                        break

                if len(chain) > 1:  # Нашли хотя бы одну связь
                    # Формируем текстовое объяснение цепочки
                    explanation_parts = []
                    for _i, node in enumerate(chain):
                        rel = node.get("relation", "")
                        prefix = f"  {'→ ' * node['depth']}" if node["depth"] > 0 else ""
                        rel_text = f" ({rel})" if rel else ""
                        explanation_parts.append(
                            f"{prefix}[{node['type']}] {node['name']}{rel_text}: {node['text'][:100]}"
                        )

                    chains.append({
                        "seed_id": seed_id,
                        "chain": chain,
                        "depth": max(n["depth"] for n in chain),
                        "length": len(chain),
                        "explanation": "\n".join(explanation_parts),
                    })

            logger.info(f"🔗 Causal search: {len(chains)} chains found (max depth: {max(c['depth'] for c in chains) if chains else 0})")
            return chains

        except Exception as e:
            logger.error(f"❌ Causal chain search error: {e}")
            return []

    def _detect_optimal_strategy(self, query: str) -> HybridSearchStrategy:
        """
        Определить оптимальную стратегию на основе запроса.

        Эвристики:
        - Короткие запросы (1-2 слова) → LEXICAL_FIRST
        - Вопросы (что, как, почему) → SEMANTIC_FIRST
        - Точные названия/имена → LEXICAL_FIRST
        - Описательные запросы → SEMANTIC_FIRST
        """
        query_lower = query.lower().strip()
        words = query_lower.split()

        # Короткие запросы - лексический поиск лучше
        if len(words) <= 2:
            return HybridSearchStrategy.LEXICAL_FIRST

        # Вопросы - семантический поиск лучше понимает интент
        question_words = ["что", "как", "почему", "зачем", "когда", "где", "кто", "какой", "какая", "какие"]
        if any(query_lower.startswith(qw) for qw in question_words):
            return HybridSearchStrategy.SEMANTIC_FIRST

        # Точные названия (с заглавных букв или в кавычках)
        if '"' in query or "'" in query:
            return HybridSearchStrategy.LEXICAL_FIRST

        # Технические термины (латиница, аббревиатуры)
        latin_ratio = sum(1 for c in query if c.isascii() and c.isalpha()) / max(len(query), 1)
        if latin_ratio > 0.5:
            return HybridSearchStrategy.LEXICAL_FIRST

        # По умолчанию - сбалансированный
        return HybridSearchStrategy.BALANCED

    async def index_document(
        self,
        doc_id: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        index_vector: bool = True,
        index_bm25: bool = True
    ) -> None:
        """
        Индексировать документ в обоих индексах.

        Args:
            doc_id: ID документа
            text: Текст документа
            metadata: Метаданные
            index_vector: Индексировать в векторный индекс
            index_bm25: Индексировать в BM25
        """
        if not text:
            return

        # BM25 индексация
        if index_bm25:
            self.bm25.add_document(doc_id, text, metadata)

        # Векторная индексация
        if index_vector and self.vector_indexer:
            try:
                await self.vector_indexer.add_document(
                    doc_id=doc_id,
                    text=text,
                    metadata=metadata
                )
            except Exception as e:
                logger.error(f"❌ Vector indexing error: {e}")

    async def index_documents_batch(
        self,
        documents: List[Tuple[str, str, Optional[Dict[str, Any]]]],
        index_vector: bool = True,
        index_bm25: bool = True
    ) -> int:
        """
        Индексировать пакет документов.

        Args:
            documents: Список (doc_id, text, metadata)
            index_vector: Индексировать в векторный индекс
            index_bm25: Индексировать в BM25

        Returns:
            Количество проиндексированных документов
        """
        indexed = 0

        # BM25 batch
        if index_bm25:
            indexed = self.bm25.add_documents_batch(documents)

        # Vector batch (по одному, т.к. нужны embeddings)
        if index_vector and self.vector_indexer:
            for doc_id, text, metadata in documents:
                try:
                    await self.vector_indexer.add_document(
                        doc_id=doc_id,
                        text=text,
                        metadata=metadata
                    )
                except Exception as e:
                    logger.error(f"❌ Vector indexing error for {doc_id}: {e}")

        logger.info(f"📚 Indexed {indexed} documents in hybrid index")
        return indexed

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        return {
            **self.stats,
            "bm25_stats": self.bm25.get_stats(),
            "vector_stats": self.vector_indexer.get_stats() if self.vector_indexer else None
        }

    def save_indices(self) -> None:
        """Сохранить индексы на диск"""
        self.bm25.save_index()
        # Vector indexer обычно сохраняется автоматически


# Singleton instances per user
_hybrid_instances: Dict[str, HybridSearchOrchestrator] = {}


def peek_hybrid_search_orchestrator(user_id: Optional[str] = None):
    """Кэшированный оркестратор пользователя или None — БЕЗ создания.

    Нужен потребителям с жёстким бюджетом времени (мессенджер): фабрика ниже
    при промахе создаёт инстанс из чего дали, а peek позволяет решить «гибрид
    только если уже собран, иначе быстрый фолбэк + фоновая сборка».
    """
    return _hybrid_instances.get(user_id or "default")


def get_hybrid_search_orchestrator(
    vector_indexer=None,
    graph_builder=None,
    user_id: Optional[str] = None
) -> HybridSearchOrchestrator:
    """
    Получить HybridSearchOrchestrator (per-user singleton).

    Args:
        vector_indexer: VectorIndexer instance
        graph_builder: GraphBuilder instance
        user_id: ID пользователя

    Returns:
        HybridSearchOrchestrator instance
    """
    global _hybrid_instances

    cache_key = user_id or "default"

    if cache_key not in _hybrid_instances:
        _hybrid_instances[cache_key] = HybridSearchOrchestrator(
            vector_indexer=vector_indexer,
            graph_builder=graph_builder,
            user_id=user_id
        )

    return _hybrid_instances[cache_key]

