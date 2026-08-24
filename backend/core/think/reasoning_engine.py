# -*- coding: utf-8 -*-
"""
Reasoning Engine - главный компонент KAG.

Объединяет:
- QueryAnalyzer - анализ запросов
- LogicalFormPlanner - планирование
- GraphTraverser - выполнение запросов
- ResponseSynthesizer - синтез ответов
- DeepAnalysisAgent - глубокий анализ (NEW!)
- AdaptiveSearchOrchestrator - адаптивный поиск с fallback (NEW!)
- ReflectionAgent - оценка качества (NEW!)

Реализует multi-hop reasoning для ответов на сложные вопросы.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..memory import MemoryManager, get_memory_manager
from ..memory.memgpt_memory import MemGPTMemoryManager
from ..search.graph_trace_builder import build_graph_trace
from ..sleep.company_snapshot_agent import get_company_snapshot_agent
from ..sleep.snapshot_generator import SnapshotGenerator
from .adaptive_search import AdaptiveSearchOrchestrator
from .data_profiling_agent import DataProfilingAgent
from .deep_analysis_agent import DeepAnalysisAgent
from .graph_traverser import GraphTraverser
from .logical_planner import (
    LogicalFormPlanner,
    ReasoningPlan,
    ReasoningStep,
    SearchStrategy,
    StepType,
)
from .numerical_search_agent import NumericalDataSearchAgent
from .query_analyzer import QueryAnalyzer, QueryComplexity, QueryIntent, QueryType
from .reflection_agent import ReflectionAgent
from .smart_filter_agent import SmartFilterAgent

logger = logging.getLogger(__name__)


@dataclass
class ReasoningResult:
    """Результат рассуждения"""
    # Основной ответ
    answer: str

    # Структурированные данные
    data: Dict[str, Any] = field(default_factory=dict)

    # Цепочка рассуждений
    reasoning_chain: List[Dict[str, Any]] = field(default_factory=list)

    # Источники
    sources: List[Dict[str, Any]] = field(default_factory=list)

    # Аномалии/предупреждения
    anomalies: List[Dict[str, Any]] = field(default_factory=list)

    # Рекомендации
    recommendations: List[str] = field(default_factory=list)

    # Уверенность
    confidence: float = 0.8

    # Метаданные
    query_type: str = ""
    processing_time_ms: int = 0
    steps_executed: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class ReasoningEngine:
    """
    Движок рассуждений Tessbrain.

    Пример использования:
    ```python
    engine = ReasoningEngine(graph_builder, vector_indexer, llm_router)

    result = await engine.reason("Что с проектом Tessbrain?")

    logger.info(result.answer)
    logger.info(result.data)
    logger.info(result.reasoning_chain)
    ```
    """

    def __init__(
        self,
        graph_builder=None,
        vector_indexer=None,
        llm_router=None,
        memory_manager: Optional[MemoryManager] = None
    ):
        """
        Args:
            graph_builder: GraphBuilder instance
            vector_indexer: VectorIndexer instance
            llm_router: LLM Router для генерации ответов
            memory_manager: MemoryManager для долгосрочной памяти
        """
        self.graph = graph_builder
        self.vectors = vector_indexer
        self.llm = llm_router

        # Компоненты
        self.query_analyzer = QueryAnalyzer(llm_router)
        self.planner = LogicalFormPlanner()
        self.traverser = GraphTraverser(graph_builder, vector_indexer)

        # NEW: Агенты глубокого анализа
        self.deep_analysis_agent = DeepAnalysisAgent(self.traverser, llm_router)
        self.adaptive_search = AdaptiveSearchOrchestrator(self.traverser, vector_indexer, llm_router)
        self.reflection_agent = ReflectionAgent(llm_router)

        # NEW: Дополнительные агенты
        self.data_profiling_agent = DataProfilingAgent(graph_builder, vector_indexer)
        self.numerical_search_agent = NumericalDataSearchAgent(graph_builder, vector_indexer, llm_router)
        self.smart_filter_agent = SmartFilterAgent(llm_router)

        # Snapshot Generator (для контекста)
        self.snapshot_generator = SnapshotGenerator(
            graph_builder=graph_builder,
            vector_indexer=vector_indexer
        )

        # Company Snapshot Agent (умный слепок компании)
        try:
            self.company_snapshot = get_company_snapshot_agent(llm_router, graph_builder)
            logger.info("✅ Company Snapshot Agent integrated")
        except Exception as e:
            logger.warning(f"⚠️ Company Snapshot Agent not available: {e}")
            self.company_snapshot = None

        # Memory Manager (для долгосрочной памяти - Mem0)
        try:
            self.memory = memory_manager or get_memory_manager(graph_builder)
            logger.info("✅ Memory Manager (Mem0) integrated")
        except Exception as e:
            logger.warning(f"⚠️ Memory Manager not available: {e}")
            self.memory = None

        # MemGPT Memory Manager (трёхуровневая память)
        try:
            self.memgpt = MemGPTMemoryManager(
                working_max_tokens=16000,
                recall_db_path="data/recall_storage.db"
            )
            logger.info("✅ MemGPT Memory Manager integrated")
        except Exception as e:
            logger.warning(f"⚠️ MemGPT Memory Manager not available: {e}")
            self.memgpt = None

        # Статистика
        self.stats = {
            "queries_processed": 0,
            "total_steps_executed": 0,
            "cache_hits": 0,
            "deep_analysis_used": 0,
            "adaptive_search_used": 0,
            "memory_recalls": 0,
            "data_profiling_used": 0,
            "numerical_search_used": 0,
            "smart_filter_used": 0,
            "memgpt_context_used": 0
        }

    async def reason(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        strategy: Optional[SearchStrategy] = None,
        allowed_groups: Optional[List[str]] = None
    ) -> ReasoningResult:
        """
        Выполнить рассуждение по запросу.

        Args:
            query: Текст запроса пользователя
            context: Дополнительный контекст
            strategy: Стратегия поиска (QUICK/STANDARD/DEEP). Если None - авто-выбор.
            allowed_groups: Разрешенные группы доступа (RBAC). None = все группы.

        Returns:
            ReasoningResult с ответом и данными
        """
        start_time = datetime.now()
        self.stats["queries_processed"] += 1

        # Сохраняем allowed_groups для использования в методах выполнения шагов
        self._current_allowed_groups = allowed_groups

        # Сохраняем фильтр по встрече из контекста
        self._current_meeting_filter = None
        if context and context.get("meeting_filter"):
            self._current_meeting_filter = context["meeting_filter"]
            logger.info(f"📅 Meeting filter active: {self._current_meeting_filter}")

        try:
            # 0. Получаем контекст из MemGPT (трёхуровневая память)
            user_id = context.get("user_id", "default") if context else "default"

            if self.memgpt:
                try:
                    # Инициализируем MemGPT если нужно
                    if not getattr(self.memgpt, '_initialized', False):
                        await self.memgpt.initialize()

                    # Получаем контекст из всех уровней памяти
                    memgpt_result = await self.memgpt.get_context(
                        user_id=user_id,
                        query=query,
                        include_recall=True,
                        include_archival=True
                    )

                    if memgpt_result.get("working_context") or memgpt_result.get("recall_context"):
                        self.stats["memgpt_context_used"] += 1
                        recall_ctx = memgpt_result.get('recall_context') or []
                        logger.info(f"🧠 MemGPT context: working={memgpt_result.get('working_context', {}).get('stats', {}).get('items_count', 0)} items, "
                                   f"recall={len(recall_ctx)} conversations")
                except Exception as e:
                    logger.warning(f"⚠️ MemGPT context retrieval failed: {e}")

            # 0b. Recall из Mem0 (если доступна)
            memory_context = None
            if self.memory and context and context.get("user_id"):
                try:
                    memory_result = await self.memory.recall(
                        query=query,
                        user_id=context["user_id"],
                        include_graph=False,  # Граф уже используем отдельно
                        limit=5
                    )
                    if memory_result.get("memories"):
                        memory_context = memory_result["combined_context"]
                        self.stats["memory_recalls"] += 1
                        logger.info(f"📝 Recalled {len(memory_result['memories'])} memories from Mem0")
                except Exception as e:
                    logger.warning(f"⚠️ Memory recall failed: {e}")

            # 0.5 NEW: Профилирование данных
            try:
                data_profile = await self.data_profiling_agent.profile_available_data()
                await self.data_profiling_agent.get_data_availability_for_query(query)
                self.stats["data_profiling_used"] += 1
                logger.info(f"📊 Data profile: {data_profile['graph']['total_nodes']} nodes, quality: {data_profile['data_quality']['overall']}%")
            except Exception as e:
                logger.warning(f"⚠️ Data profiling failed: {e}")
                data_profile = None

            # 0.6 NEW: Получаем Company Snapshot для контекста
            company_context = None
            if self.company_snapshot:
                try:
                    company_context = self.company_snapshot.get_snapshot_for_prompt()
                    if company_context and len(company_context) > 50:
                        logger.info(f"🏢 Company snapshot loaded ({len(company_context)} chars)")
                except Exception as e:
                    logger.warning(f"⚠️ Company snapshot failed: {e}")

            # 1. Анализируем запрос
            logger.info(f"🔍 Analyzing query: {query[:50]}...")
            intent = await self.query_analyzer.analyze(query)

            # Определяем стратегию
            if not strategy:
                if intent.complexity == QueryComplexity.SIMPLE:
                    strategy = SearchStrategy.QUICK
                elif intent.complexity == QueryComplexity.COMPLEX:
                    # Используем динамический DEEP план для сложных запросов
                    strategy = SearchStrategy.DEEP
                else:
                    strategy = SearchStrategy.STANDARD

            logger.info(
                f"   Query type: {intent.query_type.value}, "
                f"Complexity: {intent.complexity.value}, "
                f"Strategy: {strategy.value}"
            )

            # 2. Создаём план рассуждения
            logger.info("📋 Creating reasoning plan...")

            if strategy == SearchStrategy.DEEP:
                # Динамическое планирование для DEEP стратегии
                plan = await self.planner.create_deep_plan(intent, self.llm)
            else:
                # Шаблонное планирование для остальных
                plan = self.planner.create_plan(intent, strategy=strategy)

            plan = self.planner.optimize_plan(plan)

            logger.info(f"   Plan has {len(plan.steps)} steps")

            # 3. Выполняем план
            logger.info("⚙️ Executing plan...")
            executed_steps = await self._execute_plan(plan, context)

            # 4. Собираем данные из шагов
            collected_data = self._collect_step_results(plan)

            # 4.1 NEW: Оцениваем качество результатов
            reflection = await self.reflection_agent.evaluate_results(
                query=query,
                context=collected_data,
                understanding={
                    "query_type": intent.query_type.value,
                    "entities": intent.entities
                }
            )

            # 4.2 NEW: Если данных недостаточно - используем адаптивный поиск
            if not reflection["is_sufficient"]:
                logger.warning(f"⚠️ Качество недостаточно ({reflection['quality_score']}/10), запускаю адаптивный поиск...")
                self.stats["adaptive_search_used"] += 1

                adaptive_result = await self.adaptive_search.adaptive_search(
                    query=query,
                    understanding={
                        "query_type": intent.query_type.value,
                        "entities": intent.entities
                    },
                    max_attempts=3
                )

                # Объединяем данные
                if adaptive_result.get("final_context"):
                    final_ctx = adaptive_result["final_context"]
                    collected_data["nodes"].extend(final_ctx.get("nodes", []))
                    collected_data["vector_results"].extend(final_ctx.get("vector_results", []))

                    # Убираем дубликаты
                    seen_ids = set()
                    unique_nodes = []
                    for node in collected_data["nodes"]:
                        node_id = node.get("id", str(node))
                        if node_id not in seen_ids:
                            seen_ids.add(node_id)
                            unique_nodes.append(node)
                    collected_data["nodes"] = unique_nodes

            # 4.3 NEW: Для числовых запросов используем NumericalDataSearchAgent
            numerical_data = None
            if self._is_numerical_query(query):
                logger.info("🔢 Запускаю поиск числовых данных...")
                self.stats["numerical_search_used"] += 1

                try:
                    numerical_data = await self.numerical_search_agent.search(query)
                    if numerical_data.get("kpis") or numerical_data.get("metrics"):
                        collected_data["numerical_data"] = numerical_data
                except Exception as e:
                    logger.warning(f"⚠️ Numerical search failed: {e}")

            # 4.4 NEW: Фильтрация результатов через SmartFilterAgent
            if collected_data.get("nodes") or collected_data.get("vector_results"):
                logger.info("🔍 Фильтрация результатов...")
                self.stats["smart_filter_used"] += 1

                try:
                    all_results = collected_data.get("nodes", []) + collected_data.get("vector_results", [])
                    filtered_results = await self.smart_filter_agent.filter_results(
                        query=query,
                        results=all_results,
                        max_results=30
                    )

                    # Группируем по темам
                    grouped = await self.smart_filter_agent.group_by_topic(query, filtered_results)
                    collected_data["grouped_results"] = grouped

                    # Извлекаем инсайты
                    insights = await self.smart_filter_agent.extract_key_insights(query, filtered_results)
                    collected_data["key_insights"] = insights

                except Exception as e:
                    logger.warning(f"⚠️ Smart filter failed: {e}")

            # 4.5 NEW: Для сложных запросов используем глубокий анализ
            deep_analysis_result = None
            if intent.complexity == QueryComplexity.COMPLEX or strategy == SearchStrategy.DEEP:
                logger.info("🧠 Запускаю глубокий анализ...")
                self.stats["deep_analysis_used"] += 1

                deep_analysis_result = await self.deep_analysis_agent.perform_deep_analysis(
                    query=query,
                    context_data=collected_data
                )

            # 5. Синтезируем ответ (с учётом контекста памяти и company snapshot)
            logger.info("✨ Synthesizing response...")

            # Добавляем контекст памяти в collected_data
            if memory_context:
                collected_data["memory_context"] = memory_context

            # Добавляем company snapshot в collected_data
            if company_context:
                collected_data["company_context"] = company_context

            answer, structured_data = await self._synthesize_response(
                intent, plan, collected_data, deep_analysis_result
            )

            # 6. Формируем результат
            processing_time = int((datetime.now() - start_time).total_seconds() * 1000)
            reasoning_chain = self._build_reasoning_chain(plan)
            sources = self._extract_sources(collected_data)
            confidence = self._calculate_confidence(plan)
            evidence_contract = self._build_evidence_contract(
                sources=sources,
                reasoning_chain=reasoning_chain,
                confidence=confidence,
                query_type=intent.query_type.value,
            )
            graph_trace = await build_graph_trace(
                graph_builder=self.graph,
                sources=sources,
                reasoning_steps=evidence_contract.get("inferred_path", []),
                title="Reasoning provenance trace",
            )
            if graph_trace:
                evidence_contract["graph_trace"] = graph_trace

            result = ReasoningResult(
                answer=answer,
                data=structured_data,
                reasoning_chain=reasoning_chain,
                sources=sources,
                anomalies=collected_data.get("anomalies", []),
                recommendations=self._generate_recommendations(intent, structured_data),
                confidence=confidence,
                query_type=intent.query_type.value,
                processing_time_ms=processing_time,
                steps_executed=executed_steps,
                metadata={
                    "query": query,
                    "intent": {
                        "type": intent.query_type.value,
                        "complexity": intent.complexity.value,
                        "entities": intent.entities
                    },
                    "plan_id": plan.plan_id,
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "evidence_contract": evidence_contract,
                    "graph_trace": graph_trace,
                }
            )

            logger.info(
                f"✅ Reasoning complete in {processing_time}ms, "
                f"{executed_steps} steps executed"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Reasoning error: {e}")

            return ReasoningResult(
                answer=f"Произошла ошибка при обработке запроса: {e!s}",
                confidence=0.0,
                metadata={"error": str(e)}
            )

    async def _execute_plan(
        self,
        plan: ReasoningPlan,
        context: Optional[Dict[str, Any]]
    ) -> int:
        """Выполнить план рассуждения"""
        executed = 0
        max_iterations = len(plan.steps) * 2  # Защита от бесконечного цикла

        for _ in range(max_iterations):
            # Получаем готовые к выполнению шаги
            ready_steps = plan.get_next_steps()

            if not ready_steps:
                break

            # Выполняем параллельно если возможно
            tasks = [
                self._execute_step(step, plan, context)
                for step in ready_steps
            ]

            await asyncio.gather(*tasks)
            executed += len(ready_steps)

        plan.status = "completed" if plan.is_completed() else "partial"
        self.stats["total_steps_executed"] += executed

        return executed

    async def _execute_step(
        self,
        step: ReasoningStep,
        plan: ReasoningPlan,
        context: Optional[Dict[str, Any]]
    ):
        """Выполнить один шаг плана"""
        step.status = "running"

        try:
            # Получаем результаты зависимых шагов
            dep_results = {}
            for dep_id in step.depends_on:
                dep_step = plan._get_step(dep_id)
                if dep_step and dep_step.result:
                    dep_results[dep_id] = dep_step.result

            # Выполняем в зависимости от типа
            if step.step_type == StepType.GRAPH_QUERY:
                step.result = await self._execute_graph_query(step, dep_results)

            elif step.step_type == StepType.VECTOR_SEARCH:
                step.result = await self._execute_vector_search(step, dep_results)

            elif step.step_type == StepType.HYBRID_SEARCH:
                step.result = await self._execute_hybrid_search(step, dep_results)

            elif step.step_type == StepType.AGGREGATE:
                step.result = await self._execute_aggregate(step, dep_results)

            elif step.step_type == StepType.FILTER:
                step.result = self._execute_filter(step, dep_results)

            elif step.step_type == StepType.SORT:
                step.result = self._execute_sort(step, dep_results)

            elif step.step_type == StepType.ANALYZE:
                step.result = await self._execute_analyze(step, dep_results)

            elif step.step_type == StepType.COMPARE:
                step.result = self._execute_compare(step, dep_results)

            elif step.step_type == StepType.DETECT_ANOMALY:
                step.result = self._execute_detect_anomaly(step, dep_results)

            elif step.step_type == StepType.SYNTHESIZE:
                step.result = await self._execute_synthesize(step, dep_results, plan.query_intent)

            else:
                step.result = {"warning": f"Unknown step type: {step.step_type}"}

            step.status = "completed"

        except Exception as e:
            logger.error(f"Step {step.step_id} failed: {e}")
            step.status = "failed"
            step.result = {"error": str(e)}

    async def _execute_graph_query(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Выполнить запрос к графу"""
        params = step.params

        # Определяем что искать
        node_type = params.get("node_type", "Entity")
        entities = params.get("entities", [])
        target_entity = params.get("target_entity")
        query_subtype = params.get("query_subtype", "")

        results = []
        context_data = {}

        # Специальные запросы с контекстом
        if query_subtype == "person_context" or node_type == "Person":
            name = ""
            if target_entity:
                name = target_entity.get("name", "")
            elif entities:
                name = entities[0].get("name", "") if entities else ""

            if name:
                # Используем специализированный метод
                person_context = await self.traverser.find_person_with_context(name)
                if person_context.get("person"):
                    results.append(person_context["person"])
                    # Добавляем связанные данные
                    for task in person_context.get("tasks", []):
                        task["_label"] = "Task"
                        results.append(task)
                    for meeting in person_context.get("meetings", []):
                        meeting["_label"] = "Meeting"
                        results.append(meeting)
                    for decision in person_context.get("decisions", []):
                        decision["_label"] = "Decision"
                        results.append(decision)
                    context_data = person_context

        elif query_subtype == "meeting_context" or node_type == "Meeting":
            title = ""
            if target_entity:
                title = target_entity.get("name", target_entity.get("title", ""))

            if title:
                meeting_context = await self.traverser.find_meeting_with_context(title=title)
                if meeting_context.get("meeting"):
                    results.append(meeting_context["meeting"])
                    for p in meeting_context.get("participants", []):
                        p["_label"] = "Person"
                        results.append(p)
                    for t in meeting_context.get("tasks", []):
                        t["_label"] = "Task"
                        results.append(t)
                    context_data = meeting_context

        elif query_subtype == "all_tasks" or (node_type == "Task" and not target_entity):
            # Получаем сводку по всем задачам
            tasks_summary = await self.traverser.get_all_tasks_summary(limit=50)
            results = tasks_summary.get("tasks", [])
            context_data = tasks_summary

        elif query_subtype == "all_people" or (node_type == "Person" and not target_entity and not entities):
            # Получаем сводку по всем людям
            people_summary = await self.traverser.get_all_people_summary(limit=50)
            results = [{"_label": "Person", **p} for p in people_summary.get("people", [])]
            context_data = people_summary

        # Если есть целевая сущность — ищем её
        elif target_entity:
            name = target_entity.get("name", "")
            if name:
                node = await self.traverser.find_node_by_name(name, node_type)
                if node:
                    results.append(node)

                    # Получаем связи
                    if params.get("include_relationships"):
                        rels = await self.traverser.get_relationships(node.get("id", ""))
                        context_data["_relationships"] = rels

        # Если есть список сущностей — ищем их
        elif entities:
            for entity in entities:
                name = entity.get("name", "")
                if name:
                    node = await self.traverser.find_node_by_name(name)
                    if node:
                        results.append(node)

        # Иначе — общий поиск по типу
        else:
            filters = params.get("filter", {})
            limit = params.get("limit", 20)
            nodes = await self.traverser.find_nodes(
                node_type, filters, limit,
                allowed_groups=self._current_allowed_groups  # RBAC
            )
            results.extend(nodes)

        # Фильтрация по meeting_id если активен фильтр
        if self._current_meeting_filter and results:
            filtered_results = []
            for node in results:
                node_meeting_id = node.get("meeting_id", "")
                # Проверяем, относится ли узел к выбранной встрече
                if self._current_meeting_filter in str(node.get("id", "")) or \
                   node_meeting_id == self._current_meeting_filter or \
                   self._current_meeting_filter in node_meeting_id:
                    filtered_results.append(node)

            if filtered_results:
                results = filtered_results
                logger.info(f"📅 Filtered to {len(results)} nodes from meeting {self._current_meeting_filter[:20]}")

        return {"nodes": results, "count": len(results), "context": context_data}

    async def _execute_vector_search(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Выполнить векторный поиск"""
        params = step.params

        query = params.get("query", params.get("normalized_query", ""))
        collections = params.get("collections")
        limit = params.get("limit", 10)

        results = await self.traverser.vector_search(
            query=query,
            collections=collections,
            limit=limit,
            allowed_groups=self._current_allowed_groups  # RBAC
        )

        return {"results": results, "count": len(results)}

    async def _execute_hybrid_search(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Выполнить гибридный поиск"""
        params = step.params

        query = params.get("query", params.get("normalized_query", ""))
        node_type = params.get("node_type", "Entity")
        limit = params.get("limit", 10)

        results = await self.traverser.hybrid_search(query, node_type, limit)

        return {"results": results, "count": len(results)}

    async def _execute_aggregate(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Агрегировать данные"""
        params = step.params
        aggregate_type = params.get("aggregate_type", "count")

        # Собираем все данные из зависимых шагов
        all_data = []
        for _dep_id, dep_result in dep_results.items():
            if isinstance(dep_result, dict):
                if "nodes" in dep_result:
                    all_data.extend(dep_result["nodes"])
                elif "results" in dep_result:
                    all_data.extend(dep_result["results"])

        return await self.traverser.aggregate_data(all_data, aggregate_type)

    def _execute_filter(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Фильтровать данные"""
        params = step.params
        filter_by = params.get("filter_by", {})

        # Собираем данные
        all_data = []
        for dep_result in dep_results.values():
            if isinstance(dep_result, dict):
                if "nodes" in dep_result:
                    all_data.extend(dep_result["nodes"])
                elif "results" in dep_result:
                    all_data.extend(dep_result["results"])

        # Фильтруем
        filtered = []
        for item in all_data:
            match = True
            for key, value in filter_by.items():
                if item.get(key) != value:
                    match = False
                    break
            if match:
                filtered.append(item)

        return {"results": filtered, "count": len(filtered)}

    def _execute_sort(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Сортировать данные"""
        params = step.params
        sort_by = params.get("sort_by", "score")
        order = params.get("order", "desc")

        # Собираем данные
        all_data = []
        for dep_result in dep_results.values():
            if isinstance(dep_result, dict):
                if "nodes" in dep_result:
                    all_data.extend(dep_result["nodes"])
                elif "results" in dep_result:
                    all_data.extend(dep_result["results"])

        # Сортируем
        reverse = order == "desc"
        sorted_data = sorted(
            all_data,
            key=lambda x: x.get(sort_by, 0) if x.get(sort_by) is not None else 0,
            reverse=reverse
        )

        return {"results": sorted_data, "count": len(sorted_data)}

    async def _execute_analyze(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """LLM анализ данных"""
        if not self.llm:
            return {"analysis": "LLM не доступен для анализа"}

        params = step.params
        analyze_type = params.get("analyze_type", "general")

        # Собираем данные для анализа
        data_summary = []
        for dep_id, dep_result in dep_results.items():
            if isinstance(dep_result, dict):
                data_summary.append(f"Шаг {dep_id}: {len(dep_result.get('results', dep_result.get('nodes', [])))} результатов")

        # Простой анализ без LLM для скорости
        return {
            "analysis": f"Проанализировано данных: {', '.join(data_summary)}",
            "analyze_type": analyze_type
        }

    def _execute_compare(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Сравнить сущности"""
        # Получаем данные для сравнения
        entities_data = []
        for dep_result in dep_results.values():
            if isinstance(dep_result, dict):
                if "nodes" in dep_result:
                    entities_data.extend(dep_result["nodes"])

        if len(entities_data) < 2:
            return {"comparison": "Недостаточно данных для сравнения"}

        # Простое сравнение
        entity1 = entities_data[0]
        entity2 = entities_data[1]

        comparison = {
            "entity1": {
                "name": entity1.get("name", "Unknown"),
                "type": entity1.get("type", entity1.get("_label", "Unknown"))
            },
            "entity2": {
                "name": entity2.get("name", "Unknown"),
                "type": entity2.get("type", entity2.get("_label", "Unknown"))
            },
            "differences": [],
            "similarities": []
        }

        # Находим общие и различающиеся поля
        all_keys = set(entity1.keys()) | set(entity2.keys())
        for key in all_keys:
            if key.startswith("_"):
                continue
            val1 = entity1.get(key)
            val2 = entity2.get(key)
            if val1 == val2:
                comparison["similarities"].append(key)
            else:
                comparison["differences"].append({
                    "field": key,
                    "value1": val1,
                    "value2": val2
                })

        return comparison

    def _execute_detect_anomaly(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Обнаружить аномалии"""
        params = step.params
        anomaly_types = params.get("anomaly_types", [])

        anomalies = []

        # Собираем данные
        all_data = []
        for dep_result in dep_results.values():
            if isinstance(dep_result, dict):
                if "nodes" in dep_result:
                    all_data.extend(dep_result["nodes"])

        # Проверяем аномалии
        for item in all_data:
            item_type = item.get("type", item.get("_label", ""))

            # Просроченные задачи
            if "overdue" in anomaly_types and item_type in ("Task", "task"):
                deadline = item.get("deadline", "")
                status = item.get("status", "")
                if deadline and status not in ("completed", "done"):
                    # Простая проверка (в реальности нужно сравнивать даты)
                    anomalies.append({
                        "type": "potential_overdue",
                        "item": item.get("title", item.get("name", "")),
                        "severity": "medium"
                    })

            # Неназначенные задачи
            if "unassigned" in anomaly_types and item_type in ("Task", "task"):
                if not item.get("assignee"):
                    anomalies.append({
                        "type": "unassigned",
                        "item": item.get("title", item.get("name", "")),
                        "severity": "low"
                    })

        return {"anomalies": anomalies, "count": len(anomalies)}

    async def _execute_synthesize(
        self,
        step: ReasoningStep,
        dep_results: Dict[str, Any],
        intent: QueryIntent
    ) -> Dict[str, Any]:
        """Синтезировать ответ"""
        params = step.params
        template = params.get("template", "general_response")

        # Собираем все данные
        all_data = {}
        for dep_id, dep_result in dep_results.items():
            all_data[dep_id] = dep_result

        return {
            "template": template,
            "data": all_data,
            "intent_type": intent.query_type.value
        }

    def _collect_step_results(self, plan: ReasoningPlan) -> Dict[str, Any]:
        """Собрать результаты всех шагов"""
        collected = {
            "nodes": [],
            "relationships": [],
            "vector_results": [],
            "anomalies": [],
            "aggregations": {}
        }

        for step in plan.steps:
            if not step.result or step.status != "completed":
                continue

            result = step.result

            if step.step_type == StepType.GRAPH_QUERY:
                if "nodes" in result:
                    collected["nodes"].extend(result["nodes"])

            elif step.step_type in (StepType.VECTOR_SEARCH, StepType.HYBRID_SEARCH):
                if "results" in result:
                    collected["vector_results"].extend(result["results"])

            elif step.step_type == StepType.AGGREGATE:
                collected["aggregations"][step.step_id] = result

            elif step.step_type == StepType.DETECT_ANOMALY:
                if "anomalies" in result:
                    collected["anomalies"].extend(result["anomalies"])

        return collected

    async def _synthesize_response(
        self,
        intent: QueryIntent,
        plan: ReasoningPlan,
        collected_data: Dict[str, Any],
        deep_analysis_result: Optional[Dict[str, Any]] = None
    ) -> tuple[str, Dict[str, Any]]:
        """Синтезировать финальный ответ"""

        # Структурированные данные
        structured = {
            "entities_found": len(collected_data.get("nodes", [])),
            "vector_matches": len(collected_data.get("vector_results", [])),
            "anomalies_detected": len(collected_data.get("anomalies", [])),
            "aggregations": collected_data.get("aggregations", {})
        }

        # NEW: Если есть результат глубокого анализа - используем его
        if deep_analysis_result and deep_analysis_result.get("final_answer"):
            answer = deep_analysis_result["final_answer"]
            structured["deep_analysis"] = {
                "confidence": deep_analysis_result.get("confidence", 0.7),
                "key_findings": deep_analysis_result.get("key_findings", []),
                "recommendations": deep_analysis_result.get("recommendations", [])
            }

            # Оцениваем качество ответа
            quality_assessment = await self.reflection_agent.assess_answer_quality(
                query=intent.original_query,
                answer=answer,
                context=collected_data
            )

            # Если качество низкое - используем fallback
            if quality_assessment.get("quality_score", 5) < 4:
                logger.warning("⚠️ Качество ответа низкое, использую fallback генерацию")
                if self.llm:
                    answer = await self._generate_answer_with_llm(intent, collected_data)
                else:
                    answer = self._generate_answer_simple(intent, collected_data)

            return answer, structured

        # Генерируем текстовый ответ
        if self.llm:
            answer = await self._generate_answer_with_llm(intent, collected_data)
        else:
            answer = self._generate_answer_simple(intent, collected_data)

        return answer, structured

    def _generate_answer_simple(
        self,
        intent: QueryIntent,
        data: Dict[str, Any]
    ) -> str:
        """Простая генерация ответа без LLM"""
        nodes = data.get("nodes", [])
        vector_results = data.get("vector_results", [])
        anomalies = data.get("anomalies", [])
        aggregations = data.get("aggregations", {})

        parts = []

        # Заголовок
        if intent.query_type == QueryType.PROJECT_STATUS:
            entity_name = intent.entities[0].get("name", "проект") if intent.entities else "проект"
            parts.append(f"📊 **Статус: {entity_name}**\n")
        elif intent.query_type == QueryType.PERSON_STATUS:
            entity_name = intent.entities[0].get("name", "человек") if intent.entities else "человек"
            parts.append(f"👤 **Информация: {entity_name}**\n")
        elif intent.query_type == QueryType.CHANGE:
            parts.append("🔄 **Что изменилось:**\n")
        elif intent.query_type == QueryType.AUDIT:
            parts.append("🧾 **Аудит состояния:**\n")
        elif intent.query_type == QueryType.DISCOVERY:
            parts.append("🧠 **Найденные скрытые сигналы и инсайты:**\n")
        elif intent.query_type == QueryType.SEARCH_DECISIONS:
            parts.append("📋 **Найденные решения:**\n")
        elif intent.query_type == QueryType.SEARCH_TASKS:
            parts.append("✅ **Найденные задачи:**\n")
        else:
            parts.append("📌 **Результаты поиска:**\n")

        # Данные из графа
        if nodes:
            parts.append(f"\n🔗 Найдено в графе: {len(nodes)} элементов")

            # Группируем по типу
            by_type = {}
            for node in nodes[:10]:  # Ограничиваем
                node_type = node.get("type", node.get("_label", "unknown"))
                if node_type not in by_type:
                    by_type[node_type] = []
                by_type[node_type].append(node)

            for node_type, items in by_type.items():
                parts.append(f"\n**{node_type}** ({len(items)}):")
                for item in items[:5]:
                    name = item.get("name", item.get("title", item.get("summary", "—")))
                    if isinstance(name, str) and len(name) > 50:
                        name = name[:50] + "..."
                    parts.append(f"  • {name}")

        # Данные из векторного поиска
        if vector_results:
            parts.append(f"\n\n🔍 Релевантные фрагменты: {len(vector_results)}")
            for vr in vector_results[:3]:
                score = vr.get("score", 0)
                vr_type = vr.get("type", "unknown")
                name = vr.get("name", vr.get("title", vr.get("summary", "—")))
                if isinstance(name, str) and len(name) > 50:
                    name = name[:50] + "..."
                parts.append(f"  • [{vr_type}] {name} (релевантность: {score:.2f})")

        # Аномалии
        if anomalies:
            parts.append(f"\n\n⚠️ Обнаружено аномалий: {len(anomalies)}")
            for a in anomalies[:3]:
                parts.append(f"  • {a.get('type', 'unknown')}: {a.get('item', '—')}")

        # Агрегации
        for _agg_id, agg_data in aggregations.items():
            if "metrics" in agg_data:
                metrics = agg_data["metrics"]
                parts.append("\n\n📈 **Метрики:**")
                for key, value in metrics.items():
                    parts.append(f"  • {key}: {value}")

        if not nodes and not vector_results:
            parts.append("\n❌ По вашему запросу ничего не найдено.")

        return "\n".join(parts)

    async def _generate_answer_with_llm(
        self,
        intent: QueryIntent,
        data: Dict[str, Any]
    ) -> str:
        """Генерация ответа с помощью LLM"""
        # Сначала генерируем простой ответ как fallback
        simple_answer = self._generate_answer_simple(intent, data)

        # Используем Company Snapshot из collected_data (новый умный snapshot)
        # или fallback на старый snapshot generator
        company_snapshot = data.get("company_context")
        if not company_snapshot:
            try:
                company_snapshot = await self.snapshot_generator.get_current_snapshot_text()
            except Exception as e:
                logger.warning(f"Failed to get snapshot: {e}")
                company_snapshot = "Нет данных о состоянии компании."

        # Добавляем контекст памяти если есть
        memory_context = data.get("memory_context", "")

        # Для сложных запросов используем LLM
        try:
            # Формируем секцию памяти если есть
            memory_section = ""
            if memory_context:
                memory_section = f"""
📝 ПАМЯТЬ О ПОЛЬЗОВАТЕЛЕ:
{memory_context}
"""

            prompt = f"""Ты — аналитик Tessbrain, цифрового мозга компании.
Твоя задача — дать четкий, структурированный и полезный ответ на вопрос пользователя.

📊 КОНТЕКСТ КОМПАНИИ (Company Snapshot):
{company_snapshot}
{memory_section}
🔍 НАЙДЕННЫЕ ДАННЫЕ (из графа и документов):
{simple_answer}

❓ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:
"{intent.original_query}"

ИНСТРУКЦИЯ:
1. Используй КОНТЕКСТ КОМПАНИИ как "базу знаний" о текущем состоянии. Это актуальный слепок компании с информацией о команде, проектах, задачах и проблемах.
2. Используй НАЙДЕННЫЕ ДАННЫЕ для деталей и фактов. Если данные из поиска противоречат контексту, отдавай приоритет более свежим данным.
3. Будь кратким, но не упускай важные детали.
4. Используй Markdown для оформления (списки, жирный шрифт).
5. Если данных недостаточно, честно скажи об этом и укажи что именно неизвестно.

Сформируй ответ:"""

            response = await self.llm.generate(
                prompt=prompt,
                temperature=0.3,
                max_tokens=2000
            )

            return response

        except Exception as e:
            logger.warning(f"LLM generation failed: {e}")
            return simple_answer

    def _build_reasoning_chain(self, plan: ReasoningPlan) -> List[Dict[str, Any]]:
        """Построить цепочку рассуждений"""
        chain = []

        for step in plan.steps:
            chain.append({
                "step_id": step.step_id,
                "type": step.step_type.value,
                "description": step.description,
                "status": step.status,
                "result_summary": self._summarize_result(step.result) if step.result else None
            })

        return chain

    def _summarize_result(self, result: Any) -> str:
        """Краткое описание результата шага"""
        if isinstance(result, dict):
            if "count" in result:
                return f"{result['count']} элементов"
            if "nodes" in result:
                return f"{len(result['nodes'])} узлов"
            if "results" in result:
                return f"{len(result['results'])} результатов"
            if "anomalies" in result:
                return f"{len(result['anomalies'])} аномалий"
            return "данные получены"
        return str(result)[:50]

    def _extract_sources(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Извлечь источники данных"""
        sources = []
        seen = set()

        # Из графа
        for node in data.get("nodes", []):
            node_id = node.get("id", node.get("meeting_id", ""))
            if node_id and node_id not in seen:
                seen.add(node_id)
                snippet = (
                    node.get("description")
                    or node.get("summary")
                    or node.get("details")
                    or node.get("text")
                    or ""
                )
                sources.append({
                    "type": node.get("_label", "node"),
                    "id": node_id,
                    "name": node.get("name", node.get("title", "")),
                    "meeting_id": node.get("meeting_id", ""),
                    "entity_id": node.get("entity_id", node_id),
                    "document_id": node.get("document_id", ""),
                    "snapshot_type": node.get("snapshot_type", ""),
                    "snippet": str(snippet)[:240] if snippet else "",
                    "evidence_role": "direct",
                    "retrieval_stage": "graph",
                    "retrieval_sources": ["graph"],
                })

        # Из векторного поиска
        for vr in data.get("vector_results", []):
            source_id = vr.get("id", vr.get("meeting_id", ""))
            if source_id and source_id not in seen:
                seen.add(source_id)
                vr_type = vr.get("type", vr.get("_label", "vector"))
                snippet = (
                    vr.get("text")
                    or vr.get("summary")
                    or vr.get("description")
                    or vr.get("content")
                    or ""
                )
                sources.append({
                    "type": vr_type,
                    "id": source_id,
                    "name": vr.get("name", vr.get("title", "")),
                    "score": vr.get("score", 0),
                    "meeting_id": vr.get("meeting_id", ""),
                    "entity_id": vr.get("entity_id", source_id),
                    "document_id": vr.get("document_id", ""),
                    "snapshot_type": vr.get("snapshot_type", ""),
                    "snippet": str(snippet)[:240] if snippet else "",
                    "evidence_role": "supporting" if str(vr_type).lower() in {"chunk", "document_chunk", "vector"} else "direct",
                    "retrieval_stage": "vector",
                    "retrieval_sources": ["vector"],
                })

        # Из результатов шагов (рекурсивно ищем)
        for key, value in data.items():
            if isinstance(value, dict):
                # Проверяем results внутри шагов
                results = value.get("results", [])
                if isinstance(results, list):
                    for item in results:
                        if isinstance(item, dict):
                            item_id = item.get("id", "")
                            if item_id and item_id not in seen:
                                seen.add(item_id)
                                snippet = (
                                    item.get("description")
                                    or item.get("summary")
                                    or item.get("details")
                                    or item.get("text")
                                    or item.get("content")
                                    or ""
                                )
                                sources.append({
                                    "type": item.get("_label", key),
                                    "id": item_id,
                                    "name": item.get("name", item.get("title", ""))[:50],
                                    "meeting_id": item.get("meeting_id", ""),
                                    "entity_id": item.get("entity_id", item_id),
                                    "document_id": item.get("document_id", ""),
                                    "snapshot_type": item.get("snapshot_type", ""),
                                    "snippet": str(snippet)[:240] if snippet else "",
                                    "evidence_role": "supporting",
                                    "retrieval_stage": "graph" if key in {"nodes", "graph_results"} else "reasoning_step",
                                    "retrieval_sources": ["graph"] if key in {"nodes", "graph_results"} else ["reasoning_step"],
                                })

        return sources[:15]  # Ограничиваем

    def _build_evidence_contract(
        self,
        sources: List[Dict[str, Any]],
        reasoning_chain: List[Dict[str, Any]],
        confidence: float,
        query_type: str,
    ) -> Dict[str, Any]:
        """Собрать компактный evidence contract для legacy reasoning path."""
        direct_evidence = []
        for source in sources:
            if source.get("evidence_role") != "direct":
                continue
            direct_evidence.append({
                "id": source.get("id", ""),
                "type": source.get("type", ""),
                "title": source.get("name", ""),
                "snippet": source.get("snippet", ""),
                "score": source.get("score"),
                "meeting_id": source.get("meeting_id", ""),
                "document_id": source.get("document_id", ""),
                "entity_id": source.get("entity_id", source.get("id", "")),
                "snapshot_type": source.get("snapshot_type", ""),
                "retrieval_stage": source.get("retrieval_stage", ""),
                "retrieval_sources": source.get("retrieval_sources", []),
            })
            if len(direct_evidence) >= 4:
                break

        if not direct_evidence:
            for source in sources[:3]:
                direct_evidence.append({
                    "id": source.get("id", ""),
                    "type": source.get("type", ""),
                    "title": source.get("name", ""),
                    "snippet": source.get("snippet", ""),
                    "score": source.get("score"),
                    "meeting_id": source.get("meeting_id", ""),
                    "document_id": source.get("document_id", ""),
                    "entity_id": source.get("entity_id", source.get("id", "")),
                    "snapshot_type": source.get("snapshot_type", ""),
                    "retrieval_stage": source.get("retrieval_stage", ""),
                    "retrieval_sources": source.get("retrieval_sources", []),
                })

        inferred_path = [
            step.get("description", "")
            for step in reasoning_chain
            if step.get("description")
        ][:6]

        ambiguities = []
        if len(sources) < 2:
            ambiguities.append("Найдено мало независимых источников, вывод может быть неполным.")
        if not any(source.get("evidence_role") == "direct" for source in sources):
            ambiguities.append("Прямых структурных источников недостаточно, часть ответа опирается на supporting evidence.")
        if confidence < 0.6:
            ambiguities.append("Общая уверенность по reasoning pipeline ниже желаемой.")

        confidence_label = "high" if confidence >= 0.8 else "medium" if confidence >= 0.6 else "low"
        return {
            "query_type": query_type,
            "direct_evidence": direct_evidence,
            "inferred_path": inferred_path,
            "ambiguities": ambiguities,
            "confidence_envelope": {
                "score": round(confidence, 3),
                "label": confidence_label,
                "source_count": len(sources),
                "direct_evidence_count": len(direct_evidence),
            },
        }

    def _generate_recommendations(
        self,
        intent: QueryIntent,
        data: Dict[str, Any]
    ) -> List[str]:
        """Генерировать рекомендации"""
        recommendations = []

        anomalies = data.get("aggregations", {}).get("anomalies", [])

        if isinstance(anomalies, dict) and anomalies.get("anomalies"):
            for a in anomalies["anomalies"]:
                if a.get("type") == "unassigned":
                    recommendations.append(f"Назначьте исполнителя для задачи: {a.get('item', '')}")
                elif a.get("type") == "potential_overdue":
                    recommendations.append(f"Проверьте сроки задачи: {a.get('item', '')}")

        return recommendations

    def _calculate_confidence(self, plan: ReasoningPlan) -> float:
        """Рассчитать уверенность в результате"""
        if not plan.steps:
            return 0.5

        completed = sum(1 for s in plan.steps if s.status == "completed")
        failed = sum(1 for s in plan.steps if s.status == "failed")

        if failed > 0:
            return max(0.3, 0.8 - (failed * 0.1))

        return min(0.95, 0.7 + (completed * 0.05))

    def _is_numerical_query(self, query: str) -> bool:
        """Определить, является ли запрос числовым."""
        numerical_keywords = [
            "сколько", "количество", "число", "процент", "%",
            "бюджет", "стоимость", "цена", "расход", "доход",
            "kpi", "метрик", "показател", "статистик",
            "всего", "итого", "сумма", "среднее",
            "больше", "меньше", "максимум", "минимум"
        ]
        query_lower = query.lower()
        return any(kw in query_lower for kw in numerical_keywords)

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику"""
        return self.stats.copy()

    async def remember_conversation(
        self,
        session_id: str,
        user_id: str,
        query: str,
        answer: str,
        importance: float = 0.5
    ) -> Dict[str, Any]:
        """
        Сохранить разговор в долгосрочную память.

        Args:
            session_id: ID сессии
            user_id: ID пользователя
            query: Вопрос пользователя
            answer: Ответ системы
            importance: Важность (0.0-1.0)

        Returns:
            Результат сохранения
        """
        results = {}

        # 1. Сохраняем в MemGPT (трёхуровневая память)
        if self.memgpt:
            try:
                # Инициализируем если нужно
                if not self.memgpt.initialized:
                    await self.memgpt.initialize()

                await self.memgpt.store_conversation(
                    user_id=user_id,
                    query=query,
                    response=answer,
                    importance=importance,
                    metadata={"session_id": session_id}
                )
                results["memgpt"] = "saved"
                logger.debug("💾 Conversation saved to MemGPT")
            except Exception as e:
                logger.warning(f"⚠️ MemGPT save failed: {e}")
                results["memgpt"] = f"error: {e}"

        # 2. Сохраняем в Mem0 (если доступна)
        if self.memory:
            try:
                messages = [
                    {"role": "user", "content": query},
                    {"role": "assistant", "content": answer}
                ]

                result = await self.memory.remember_conversation(
                    session_id=session_id,
                    user_id=user_id,
                    messages=messages,
                    extract_entities=False  # Сущности уже в графе
                )

                results["mem0"] = result
                logger.debug(f"💾 Conversation saved to Mem0: {result}")

            except Exception as e:
                logger.error(f"❌ Failed to save to Mem0: {e}")
                results["mem0"] = f"error: {e}"

        if not self.memgpt and not self.memory:
            return {"status": "memory_not_available"}

        return results

    async def get_user_memory_profile(self, user_id: str) -> Dict[str, Any]:
        """
        Получить профиль пользователя из памяти.

        Args:
            user_id: ID пользователя

        Returns:
            Профиль с предпочтениями и фактами
        """
        if not self.memory:
            return {"status": "memory_not_available"}

        return await self.memory.get_user_profile(user_id)

    async def get_data_profile(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Получить профиль доступных данных.

        Args:
            force_refresh: Принудительно обновить кеш

        Returns:
            Профиль данных с метриками и рекомендациями
        """
        return await self.data_profiling_agent.profile_available_data(force_refresh)

    def get_data_profile_summary(self) -> str:
        """
        Получить текстовое описание профиля данных.

        Returns:
            Текстовое описание
        """
        return self.data_profiling_agent.get_profile_summary()

