# -*- coding: utf-8 -*-
"""
🔄 Adaptive Search - Адаптивный поиск с каскадным fallback.

Управляет циклом: поиск → оценка → повторный поиск (если нужно).
Использует каскадную стратегию:
1. Adaptive Search (Graph + Vector + LLM фильтрация)
2. Hybrid Search Fallback (расширенный поиск)
3. Simple Vector Fallback (последний рубеж)

Адаптировано из tess_search/module_10_2_intelligent_navigation.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .reflection_agent import ReflectionAgent

logger = logging.getLogger(__name__)


class AdaptiveSearchOrchestrator:
    """
    Оркестратор адаптивного поиска с рефлексией.

    Пример использования:
    ```python
    orchestrator = AdaptiveSearchOrchestrator(
        graph_traverser=traverser,
        vector_indexer=indexer,
        llm_router=llm
    )

    result = await orchestrator.adaptive_search(
        query="Чем занимается компания?",
        max_attempts=3
    )

    logger.info(result["final_answer"])
    ```
    """

    # Пороги для переключения стратегий
    THRESHOLDS = {
        "adaptive_to_hybrid": {
            "quality": 5,
            "completeness": 0.4
        },
        "hybrid_to_simple": {
            "quality": 3,
            "completeness": 0.2
        }
    }

    def __init__(
        self,
        graph_traverser=None,
        vector_indexer=None,
        llm_router=None
    ):
        """
        Args:
            graph_traverser: GraphTraverser для поиска в графе
            vector_indexer: VectorIndexer для векторного поиска
            llm_router: LLM Router для генерации
        """
        self.traverser = graph_traverser
        self.vectors = vector_indexer
        self.llm = llm_router

        # Инициализируем агент рефлексии
        self.reflection_agent = ReflectionAgent(llm_router)

        self.max_attempts = 3
        self.name = "AdaptiveSearchOrchestrator"

    async def adaptive_search(
        self,
        query: str,
        understanding: Optional[Dict[str, Any]] = None,
        max_attempts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Выполняет адаптивный поиск с рефлексией.

        Args:
            query: Поисковый запрос
            understanding: Понимание запроса (query_type, entities)
            max_attempts: Максимум попыток

        Returns:
            Результат поиска с контекстом
        """
        logger.info(f"🔄 Adaptive Search: начинаю поиск для '{query[:50]}...'")

        if max_attempts:
            self.max_attempts = max_attempts

        attempts = []
        current_strategy = "adaptive"
        final_context = None

        for attempt_num in range(1, self.max_attempts + 1):
            logger.info(f"\n{'='*50}")
            logger.info(f"🔍 Попытка {attempt_num}/{self.max_attempts} [СТРАТЕГИЯ: {current_strategy.upper()}]")
            logger.info(f"{'='*50}")

            # Выполняем поиск в зависимости от стратегии
            if current_strategy == "adaptive":
                context = await self._adaptive_search_step(query, understanding, attempts)
            elif current_strategy == "hybrid":
                context = await self._hybrid_search_step(query, understanding)
            else:  # simple
                context = await self._simple_search_step(query)

            # Оцениваем результаты
            reflection = await self.reflection_agent.evaluate_results(
                query=query,
                context=context,
                understanding=understanding,
                previous_attempts=attempts
            )

            quality = reflection["quality_score"]
            completeness = reflection["completeness"]

            logger.info(f"   📊 Качество: {quality}/10, Полнота: {completeness:.0%}")

            # Сохраняем попытку
            attempt_data = {
                "attempt_number": attempt_num,
                "strategy": current_strategy,
                "context": context,
                "reflection": reflection
            }
            attempts.append(attempt_data)

            # Проверяем достаточность
            if reflection["is_sufficient"]:
                logger.info("   ✅ Качество достаточное!")
                final_context = self._merge_contexts(attempts)
                break

            # Определяем следующую стратегию
            should_switch, next_strategy = self._should_switch_strategy(
                current_strategy, quality, completeness
            )

            if should_switch:
                logger.warning(f"   ⚠️ {current_strategy.upper()} недостаточно, переключаюсь на {next_strategy.upper()}")
                current_strategy = next_strategy
            elif attempt_num < self.max_attempts:
                logger.info(f"   🔄 Повторяю {current_strategy.upper()} с модификациями")
                # Используем retry_strategy из рефлексии
            else:
                logger.warning("   ❌ Достигнут лимит попыток")
                final_context = self._merge_contexts(attempts)

        if final_context is None:
            final_context = self._merge_contexts(attempts)

        result = {
            "query": query,
            "understanding": understanding,
            "attempts": attempts,
            "final_context": final_context,
            "total_attempts": len(attempts),
            "success": any(a["reflection"]["is_sufficient"] for a in attempts),
            "final_quality": attempts[-1]["reflection"]["quality_score"] if attempts else 0,
            "final_completeness": attempts[-1]["reflection"]["completeness"] if attempts else 0,
            "timestamp": datetime.now().isoformat()
        }

        logger.info(f"\n{'='*50}")
        logger.info(f"✅ Adaptive Search завершён: {len(attempts)} попыток, success={result['success']}")
        logger.info(f"{'='*50}\n")

        return result

    async def _adaptive_search_step(
        self,
        query: str,
        understanding: Optional[Dict[str, Any]],
        previous_attempts: List[Dict]
    ) -> Dict[str, Any]:
        """
        Шаг адаптивного поиска (Graph + Vector).
        """
        context = {
            "nodes": [],
            "vector_results": [],
            "source": "adaptive"
        }

        # Поиск в графе
        if self.traverser:
            try:
                # Определяем типы узлов для поиска
                node_types = ["Entity", "Person", "Task", "Decision", "Meeting", "Project"]

                # Извлекаем ключевые слова из запроса
                keywords = self._extract_keywords(query)

                for node_type in node_types:
                    # Поиск по ключевым словам
                    for keyword in keywords[:3]:
                        nodes = await self.traverser.find_nodes(
                            node_type,
                            {"name": keyword},
                            limit=10
                        )
                        context["nodes"].extend(nodes)

                    # Общий поиск по типу
                    type_nodes = await self.traverser.find_nodes(node_type, limit=5)
                    context["nodes"].extend(type_nodes)

                # Убираем дубликаты
                seen_ids = set()
                unique_nodes = []
                for node in context["nodes"]:
                    node_id = node.get("id", str(node))
                    if node_id not in seen_ids:
                        seen_ids.add(node_id)
                        unique_nodes.append(node)
                context["nodes"] = unique_nodes

            except Exception as e:
                logger.warning(f"Graph search error: {e}")

        # Векторный поиск
        if self.vectors:
            try:
                vector_results = await self.vectors.search(
                    query=query,
                    limit=20,
                    score_threshold=0.3
                )
                context["vector_results"] = vector_results
            except Exception as e:
                logger.warning(f"Vector search error: {e}")

        logger.info(
            f"   Adaptive: найдено {len(context['nodes'])} узлов, "
            f"{len(context['vector_results'])} векторов"
        )

        return context

    async def _hybrid_search_step(
        self,
        query: str,
        understanding: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Шаг гибридного поиска (расширенный).
        """
        context = {
            "nodes": [],
            "vector_results": [],
            "source": "hybrid"
        }

        # Расширенный поиск в графе
        if self.traverser:
            try:
                # Поиск по всем типам узлов с большим лимитом
                node_types = ["Entity", "Person", "Task", "Decision", "Meeting", "Project", "KPI"]

                for node_type in node_types:
                    nodes = await self.traverser.find_nodes(node_type, limit=20)
                    context["nodes"].extend(nodes)

                # Гибридный поиск
                hybrid_results = await self.traverser.hybrid_search(query, "Entity", limit=20)
                context["nodes"].extend(hybrid_results)

                # Убираем дубликаты
                seen_ids = set()
                unique_nodes = []
                for node in context["nodes"]:
                    node_id = node.get("id", str(node))
                    if node_id not in seen_ids:
                        seen_ids.add(node_id)
                        unique_nodes.append(node)
                context["nodes"] = unique_nodes

            except Exception as e:
                logger.warning(f"Hybrid graph search error: {e}")

        # Расширенный векторный поиск
        if self.vectors:
            try:
                # Поиск с низким порогом
                vector_results = await self.vectors.search(
                    query=query,
                    limit=50,
                    score_threshold=0.2
                )
                context["vector_results"] = vector_results
            except Exception as e:
                logger.warning(f"Hybrid vector search error: {e}")

        logger.info(
            f"   Hybrid: найдено {len(context['nodes'])} узлов, "
            f"{len(context['vector_results'])} векторов"
        )

        return context

    async def _simple_search_step(self, query: str) -> Dict[str, Any]:
        """
        Простой векторный поиск (последний рубеж).
        """
        context = {
            "nodes": [],
            "vector_results": [],
            "source": "simple"
        }

        # Только векторный поиск с очень низким порогом
        if self.vectors:
            try:
                vector_results = await self.vectors.search(
                    query=query,
                    limit=100,
                    score_threshold=0.1
                )
                context["vector_results"] = vector_results
            except Exception as e:
                logger.warning(f"Simple vector search error: {e}")

        # Если есть traverser, берём все узлы
        if self.traverser:
            try:
                for node_type in ["Entity", "Person", "Task", "Decision"]:
                    nodes = await self.traverser.find_nodes(node_type, limit=30)
                    context["nodes"].extend(nodes)
            except Exception as e:
                logger.warning(f"Simple graph search error: {e}")

        logger.info(
            f"   Simple: найдено {len(context['nodes'])} узлов, "
            f"{len(context['vector_results'])} векторов"
        )

        return context

    def _should_switch_strategy(
        self,
        current_strategy: str,
        quality: int,
        completeness: float
    ) -> tuple:
        """
        Определяет, нужно ли переключить стратегию.

        Returns:
            (should_switch, next_strategy)
        """
        if current_strategy == "adaptive":
            threshold = self.THRESHOLDS["adaptive_to_hybrid"]
            if quality < threshold["quality"] or completeness < threshold["completeness"]:
                return True, "hybrid"

        elif current_strategy == "hybrid":
            threshold = self.THRESHOLDS["hybrid_to_simple"]
            if quality < threshold["quality"] or completeness < threshold["completeness"]:
                return True, "simple"

        return False, current_strategy

    def _merge_contexts(self, attempts: List[Dict]) -> Dict[str, Any]:
        """
        Объединяет контексты из всех попыток.
        """
        merged = {
            "nodes": [],
            "vector_results": [],
            "sources": []
        }

        seen_node_ids = set()
        seen_vector_ids = set()

        for attempt in attempts:
            context = attempt.get("context", {})
            source = context.get("source", "unknown")
            merged["sources"].append(source)

            # Объединяем узлы
            for node in context.get("nodes", []):
                node_id = node.get("id", str(node))
                if node_id not in seen_node_ids:
                    seen_node_ids.add(node_id)
                    merged["nodes"].append(node)

            # Объединяем векторные результаты
            for vr in context.get("vector_results", []):
                vr_id = vr.get("id", str(vr))
                if vr_id not in seen_vector_ids:
                    seen_vector_ids.add(vr_id)
                    merged["vector_results"].append(vr)

        merged["total_nodes"] = len(merged["nodes"])
        merged["total_vectors"] = len(merged["vector_results"])

        return merged

    def _extract_keywords(self, query: str) -> List[str]:
        """Извлекает ключевые слова из запроса."""
        # Стоп-слова
        stop_words = {
            "что", "как", "где", "когда", "кто", "какой", "какая", "какие",
            "с", "в", "на", "по", "для", "от", "до", "из", "у", "о", "об",
            "и", "а", "но", "или", "если", "то", "это", "есть", "был", "была",
            "наш", "наша", "наши", "нашей", "нашего", "чем", "занимается"
        }

        words = query.lower().split()
        keywords = [w.strip("?.,!:;\"'") for w in words if w.lower() not in stop_words and len(w) > 2]

        return keywords



