# -*- coding: utf-8 -*-
"""
Оркестратор извлечения сущностей из встреч.
Координирует работу всех агентов и сохраняет результаты.

Адаптировано из tess_old/module_01_nlp_analysis/enhanced_orchestrator.py
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..llm.adaptive import AdaptiveConcurrency
from ..llm.router import LLMRouter, get_llm_router

# Templates & Regulations extraction
from ..templates import RegulationExtractorAgent, TemplateExtractorAgent
from .agents import (
    # Когнитивные измерения (CogniLayer Ф1)
    CogniDimensionsAgent,
    # Cross-Meeting анализ
    CrossMeetingAnalyzer,
    DecisionAgent,
    # Decision Learning (Module 11)
    DecisionLearningAgent,
    EnhancedKPIAgent,
    EntityAgent,
    FactCheckerAgent,
    # Прогнозирование (Module 09)
    FuturePredictor,
    # Goals Tracking (Module 17)
    GoalsTrackingAgent,
    IdeaAgent,
    # Intelligence Weights (Module 13)
    IntelligenceWeights,
    # Knowledge Extraction (Module 16)
    KnowledgeExtractionOrchestrator,
    KPIAgent,
    # Расширенные агенты
    OpinionAgent,
    ParticipantAgent,
    ProjectStatusAgent,
    # Психологический анализ (Module 08)
    PsychologicalAnalyzer,
    # Search Metadata (Module 18)
    SearchMetadataGenerator,
    # Synthesis & Night Analysis (Module 05)
    SynthesisAgent,
    TaskAgent,
)

# Validation Agent
from .validation_agent import ValidationAgent

logger = logging.getLogger(__name__)


class CaptureOrchestrator:
    """
    Оркестратор для извлечения сущностей из встреч.

    Возможности:
    - Параллельное выполнение агентов
    - Кеширование контекста для экономии
    - Cross-agent анализ
    - Интеграция с хранилищем

    Пример использования:
    ```python
    orchestrator = CaptureOrchestrator()
    results = await orchestrator.process_meeting(
        transcript="...",
        meeting_id="meeting_123",
        meeting_context={"project": "Alpha"}
    )
    ```
    """

    def __init__(
        self,
        llm_router: Optional[LLMRouter] = None,
        storage_manager=None,
        graph_builder=None,
        vector_indexer=None
    ):
        """
        Args:
            llm_router: Роутер LLM (если не указан, создается глобальный)
            storage_manager: Менеджер хранилища для сохранения результатов
            graph_builder: GraphBuilder для сохранения знаний в граф
            vector_indexer: VectorIndexer для индексации
        """
        self.name = "Capture Orchestrator"
        self.version = "2.0.0"

        # LLM роутер
        self.llm = llm_router or get_llm_router()

        # Storage manager
        self.storage = storage_manager

        # Graph и Vector хранилища (для KnowledgeExtraction)
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer

        # Инициализация агентов
        self.agents = {
            # Базовые агенты
            "decisions": DecisionAgent(),
            "tasks": TaskAgent(),
            "participants": ParticipantAgent(),
            "entities": EntityAgent(),
            "kpis": KPIAgent(),
            # Расширенные агенты
            "opinions": OpinionAgent(),
            "ideas": IdeaAgent(),
            "project_statuses": ProjectStatusAgent(),
            "enhanced_kpis": EnhancedKPIAgent(),
            "fact_checks": FactCheckerAgent(),
            # Когнитивные измерения участников (CogniLayer Ф1) — питают
            # extended["cogni"] персоны через persona_observations
            "cogni_profiles": CogniDimensionsAgent(),
        }

        # Устанавливаем LLM для всех агентов
        for agent in self.agents.values():
            agent.set_llm(self.llm)

        # Cross-Meeting Analyzer (отдельно от агентов, т.к. работает с графом)
        self.cross_meeting_analyzer = None  # Инициализируется при необходимости

        # Специализированные агенты (не участвуют в базовом извлечении)
        self.psychological_analyzer = PsychologicalAnalyzer(llm_router=self.llm)
        self.future_predictor = FuturePredictor(llm_router=self.llm)
        self.intelligence_weights = IntelligenceWeights(llm_router=self.llm)
        self.search_metadata_generator = SearchMetadataGenerator(llm_router=self.llm)

        # Новые агенты (Module 11, 05, 17)
        self.decision_learning_agent = DecisionLearningAgent(llm_router=self.llm)
        self.synthesis_agent = SynthesisAgent(llm_router=self.llm)
        self.goals_tracking_agent = GoalsTrackingAgent(llm_router=self.llm)

        # Knowledge Extraction (Module 16)
        self.knowledge_extractor = KnowledgeExtractionOrchestrator(
            llm_router=self.llm,
            graph_builder=self.graph_builder,
            vector_indexer=self.vector_indexer
        )

        # Templates & Regulations Extraction
        self.regulation_extractor = RegulationExtractorAgent(
            llm_router=self.llm,
            graph_builder=self.graph_builder
        )
        self.template_extractor = TemplateExtractorAgent(
            llm_router=self.llm,
            graph_builder=self.graph_builder,
            vector_indexer=self.vector_indexer
        )

        # Validation Agent
        self.validation_agent = ValidationAgent(
            llm_router=self.llm,
            graph_builder=self.graph_builder
        )

        # Конфигурация
        self.config = {
            "parallel_execution": True,
            "use_context_caching": True,
            # Прогрев кеша: параллельный старт агентов «бьёт» implicit-кеш Gemini
            # (он прогревается на ПЕРВОМ запросе). При True гоняем первого агента
            # отдельно (прогрев транскрипта), потом остальных параллельно. Дефолт
            # OFF — включать после замера реального hit% в логе «📊 Кеш встречи».
            "cache_warmup": False,
            "timeout_seconds": 300,
            "enable_cross_analysis": True,
            "enable_cross_meeting_links": True,
            "enable_psychological_analysis": True,
            "enable_future_prediction": True,
            "enable_intelligence_weights": True,
            "enable_search_metadata": True,
            # Новые модули
            "enable_decision_learning": True,
            "enable_synthesis_analysis": True,
            "enable_goals_tracking": True,
            "enable_knowledge_extraction": True,
            # Templates & Regulations
            "enable_regulation_extraction": True,
            "enable_template_extraction": True
        }

        # Статистика
        self.stats = {
            "meetings_processed": 0,
            "total_entities_extracted": 0,
            "errors": 0,
            "avg_processing_time": 0.0
        }

        logger.info(f"🚀 {self.name} v{self.version} initialized with {len(self.agents)} agents")

    async def process_meeting(
        self,
        transcript: str,
        meeting_id: Optional[str] = None,
        meeting_context: Optional[Dict[str, Any]] = None,
        agents_to_run: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Обработка встречи с извлечением всех сущностей.

        Args:
            transcript: Текст транскрипта встречи
            meeting_id: Уникальный ID встречи
            meeting_context: Дополнительный контекст (проект, участники, etc.)
            agents_to_run: Список агентов для запуска (если None - все)

        Returns:
            Результаты обработки со всеми извлеченными сущностями
        """
        start_time = datetime.now(timezone.utc)

        if not meeting_id:
            meeting_id = f"meeting_{start_time.strftime('%Y%m%d_%H%M%S')}"

        logger.info(f"🔄 Processing meeting {meeting_id} ({len(transcript)} chars)")

        try:
            # Устанавливаем контекст для кеширования (экономия до 90%)
            if self.config["use_context_caching"]:
                self.llm.set_context_for_caching(transcript)
                logger.info("📦 Context caching enabled")

            # Определяем какие агенты запускать
            active_agents = agents_to_run or list(self.agents.keys())

            # ─── Адаптивный тиринг (ЗА флагом, дефолт OFF) ───
            # Если тенант выбрал модель обработки (ЛК → model_router) и включён
            # enable_tiered_extraction — гоним извлечение ОДНОЙ моделью нативным
            # API; число вызовов подстраивается под её тир (frontier=1, lite≈9).
            # Любой сбой/нет ключа → tiered=None → штатный 9-агентный путь ниже
            # (дефолтную логику НЕ трогаем).
            agent_results = None
            agent_errors: Dict[str, Any] = {}
            _uid = (meeting_context or {}).get("user_id")
            try:
                from backend.core.config import flags as _flags
                if getattr(_flags, "enable_tiered_extraction", False) and _uid:
                    from backend.core.llm.extraction_route import run_tiered_for_user
                    _tiered = await run_tiered_for_user(_uid, transcript)
                    _secs = (_tiered or {}).get("sections") or {}
                    # используем только при РЕАЛЬНОМ контенте (иначе — агенты)
                    if any(isinstance(v, list) and v for v in _secs.values()):
                        agent_results = _secs
                        _st = _tiered.get("stats", {})
                        logger.info("🎚️ Tiered extraction: %s tier=%s calls=%s key=%s",
                                    _st.get("model"), _st.get("tier"),
                                    _st.get("actual_calls"), _st.get("key_source"))
            except Exception as _e:  # noqa: BLE001
                logger.warning("Tiered extraction fallback → агенты: %s", _e)
                agent_results = None

            # Запускаем агентов (штатный путь, если тиринг не сработал)
            if agent_results is None:
                if self.config["parallel_execution"]:
                    agent_results, agent_errors = await self._run_agents_parallel(
                        transcript, meeting_context, active_agents
                    )
                else:
                    agent_results, agent_errors = await self._run_agents_sequential(
                        transcript, meeting_context, active_agents
                    )

            # Cross-agent анализ
            cross_insights = {}
            if self.config["enable_cross_analysis"]:
                cross_insights = await self._perform_cross_analysis(agent_results)

            # Cross-meeting links (связи между встречами)
            cross_meeting_links = {}
            if self.config["enable_cross_meeting_links"]:
                cross_meeting_links = await self._analyze_cross_meeting_links(
                    meeting_id=meeting_id,
                    agent_results=agent_results,
                    meeting_context=meeting_context
                )

            # ─── Параллельный батч независимых аналитических фаз ───
            # Каждая фаза читает только agent_results/transcript/meeting_context
            # и ВОЗВРАЩАЕТ результат (запись в граф — позже, в indexing-стадии),
            # фазы не зависят друг от друга. Раньше шли последовательно
            # (psych+future+weights+search+goals ≈ 2.5 мин), теперь concurrently
            # под адаптивным лимитом (AIMD: при rate-limit сам сжимается). Время
            # ≈ самой долгой фазы.
            psychological_analysis = {}
            future_predictions = {}
            weighted_priorities = {}
            search_metadata = {}
            decision_learning = {}
            synthesis_analysis = {}
            goals_tracking = {}

            try:
                _phase_conc = max(1, int(os.getenv("DOWNSTREAM_PHASE_CONCURRENCY", "6")))
            except (TypeError, ValueError):
                _phase_conc = 6
            _phase_limiter = AdaptiveConcurrency(_phase_conc)
            _phase_out: Dict[str, Any] = {}

            async def _run_phase(_key, _factory, _default):
                try:
                    _phase_out[_key] = await _phase_limiter.run(_factory)
                except Exception as _pe:
                    logger.error(f"❌ Downstream phase {_key} failed: {_pe}")
                    _phase_out[_key] = _default

            _phase_tasks = []
            if self.config.get("enable_psychological_analysis"):
                _phase_tasks.append(_run_phase(
                    "psychological_analysis",
                    lambda: self._run_psychological_analysis(
                        transcript=transcript,
                        participants=agent_results.get("participants", []),
                        meeting_context=meeting_context,
                    ), {}))
            if self.config.get("enable_future_prediction"):
                _phase_tasks.append(_run_phase(
                    "future_predictions",
                    lambda: self._run_future_prediction(
                        transcript=transcript,
                        agent_results=agent_results,
                        meeting_context=meeting_context,
                    ), {}))
            if self.config.get("enable_intelligence_weights"):
                _phase_tasks.append(_run_phase(
                    "weighted_priorities",
                    lambda: self._calculate_intelligence_weights(
                        agent_results=agent_results,
                        meeting_context=meeting_context,
                    ), {}))
            if self.config.get("enable_search_metadata"):
                _phase_tasks.append(_run_phase(
                    "search_metadata",
                    lambda: self._generate_search_metadata(
                        meeting_id=meeting_id,
                        transcript=transcript,
                        agent_results=agent_results,
                    ), {}))
            if self.config.get("enable_decision_learning"):
                _phase_tasks.append(_run_phase(
                    "decision_learning",
                    lambda: self._run_decision_learning(
                        transcript=transcript,
                        meeting_id=meeting_id,
                        decisions=agent_results.get("decisions", []),
                    ), {}))
            if self.config.get("enable_synthesis_analysis"):
                _phase_tasks.append(_run_phase(
                    "synthesis_analysis",
                    lambda: self._run_synthesis_analysis(
                        agent_results=agent_results,
                        meeting_id=meeting_id,
                    ), {}))
            if self.config.get("enable_goals_tracking"):
                _phase_tasks.append(_run_phase(
                    "goals_tracking",
                    lambda: self._run_goals_tracking(
                        transcript=transcript,
                        meeting_id=meeting_id,
                        agent_results=agent_results,
                        meeting_context=meeting_context,
                    ), {}))

            if _phase_tasks:
                _t0 = datetime.now(timezone.utc)
                await asyncio.gather(*_phase_tasks)
                _dt = (datetime.now(timezone.utc) - _t0).total_seconds()
                logger.info(
                    f"⚡ Downstream-фазы ({len(_phase_tasks)}) параллельно за {_dt:.1f}s"
                    + (f"; rate-limit сжимал лимит до {_phase_limiter.min_seen}"
                       if _phase_limiter.rate_limit_hits else "")
                )

            psychological_analysis = _phase_out.get("psychological_analysis", {})
            future_predictions = _phase_out.get("future_predictions", {})
            weighted_priorities = _phase_out.get("weighted_priorities", {})
            search_metadata = _phase_out.get("search_metadata", {})
            decision_learning = _phase_out.get("decision_learning", {})
            synthesis_analysis = _phase_out.get("synthesis_analysis", {})
            goals_tracking = _phase_out.get("goals_tracking", {})

            # Knowledge Extraction (Module 16)
            knowledge_extraction = {}
            if self.config.get("enable_knowledge_extraction"):
                knowledge_extraction = await self._run_knowledge_extraction(
                    transcript=transcript,
                    meeting_id=meeting_id,
                    agent_results=agent_results,
                    meeting_context=meeting_context
                )

            # Извлечение регламентов (Templates & Workflows)
            regulations = []
            if self.config.get("enable_regulation_extraction"):
                regulations = await self._run_regulation_extraction(
                    transcript=transcript,
                    meeting_id=meeting_id,
                    agent_results=agent_results
                )

            # Извлечение шаблонов/чек-листов
            extracted_templates = []
            if self.config.get("enable_template_extraction"):
                extracted_templates = await self._run_template_extraction(
                    transcript=transcript,
                    meeting_id=meeting_id,
                    agent_results=agent_results
                )

            # Консолидация результатов
            results = self._consolidate_results(
                agent_results=agent_results,
                cross_insights=cross_insights,
                cross_meeting_links=cross_meeting_links,
                psychological_analysis=psychological_analysis,
                future_predictions=future_predictions,
                weighted_priorities=weighted_priorities,
                search_metadata=search_metadata,
                decision_learning=decision_learning,
                synthesis_analysis=synthesis_analysis,
                goals_tracking=goals_tracking,
                knowledge_extraction=knowledge_extraction,
                regulations=regulations,
                extracted_templates=extracted_templates,
                meeting_id=meeting_id,
                meeting_context=meeting_context,
                start_time=start_time,
                agent_errors=agent_errors,
            )

            # НОВОЕ: Усиление памяти (Hebbian Learning + Frequency Tracking)
            memory_reinforcement = await self._reinforce_memory(
                meeting_id=meeting_id,
                agent_results=agent_results,
                transcript=transcript
            )
            results["memory_reinforcement"] = memory_reinforcement

            # НОВОЕ: Валидация извлечённых данных
            validation_result = await self._validate_extracted_data(
                agent_results=agent_results,
                transcript=transcript,
                meeting_context=meeting_context
            )
            results["validation"] = validation_result

            # Очищаем кеш
            if self.config["use_context_caching"]:
                self.llm.clear_cache()

            # Сохраняем в storage если есть
            if self.storage:
                await self._save_to_storage(results)

            # Обновляем статистику
            self._update_stats(results, start_time)

            logger.info(
                f"✅ Meeting {meeting_id} processed: "
                f"{results['summary']['total_entities']} entities extracted"
            )

            return results

        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"❌ Error processing meeting {meeting_id}: {e}")

            # Очищаем кеш в случае ошибки
            if self.config["use_context_caching"]:
                self.llm.clear_cache()

            return {
                "meeting_id": meeting_id,
                "status": "error",
                "error": str(e),
                "results": {}
            }

    async def _run_agents_parallel(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]],
        active_agents: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Параллельный запуск агентов.

        Опц. прогрев кеша (`cache_warmup`): один агент выполняется ПЕРВЫМ
        (прогревает implicit-кеш транскрипта), остальные — параллельно после.
        Иначе одновременный старт бьёт кеш (не прогрет). Набор агентов и
        обработка ошибок идентичны — меняется только порядок первого."""
        results: Dict[str, List[Dict[str, Any]]] = {}
        errors: Dict[str, str] = {}

        def _mark_error(agent_name: str) -> None:
            agent = self.agents.get(agent_name)
            if agent is not None and not getattr(agent, "last_extract_ok", True):
                errors[agent_name] = (
                    getattr(agent, "last_extract_error", None) or "extract failed")

        remaining = [a for a in active_agents if a in self.agents]
        warmup = bool(self.config.get("cache_warmup")
                      and self.config.get("use_context_caching"))
        if warmup and len(remaining) > 1:
            first = remaining.pop(0)
            logger.info(f"🔥 Cache warmup: {first} first, then {len(remaining)} in parallel")
            try:
                results[first] = await self._safe_agent_execution(
                    self.agents[first], transcript, meeting_context)
                _mark_error(first)
            except Exception as e:
                logger.error(f"❌ Agent {first} failed: {e}")
                results[first] = []
                errors[first] = str(e)[:200]
        else:
            logger.info(f"⚡ Running {len(remaining)} agents in parallel")

        tasks = {
            agent_name: asyncio.create_task(
                self._safe_agent_execution(self.agents[agent_name], transcript, meeting_context))
            for agent_name in remaining
        }
        for agent_name, task in tasks.items():
            try:
                results[agent_name] = await task
            except Exception as e:
                logger.error(f"❌ Agent {agent_name} failed: {e}")
                results[agent_name] = []
                errors[agent_name] = str(e)[:200]
            else:
                _mark_error(agent_name)

        return results, errors

    async def _run_agents_sequential(
        self,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]],
        active_agents: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Последовательный запуск агентов"""
        logger.info(f"🔄 Running {len(active_agents)} agents sequentially")

        results = {}
        errors: Dict[str, str] = {}
        for agent_name in active_agents:
            if agent_name in self.agents:
                agent = self.agents[agent_name]
                results[agent_name] = await self._safe_agent_execution(
                    agent, transcript, meeting_context
                )
                if not getattr(agent, "last_extract_ok", True):
                    errors[agent_name] = (
                        getattr(agent, "last_extract_error", None)
                        or "extract failed")

        return results, errors

    async def _safe_agent_execution(
        self,
        agent,
        transcript: str,
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Безопасное выполнение агента с обработкой ошибок"""
        try:
            return await agent.extract(transcript, meeting_context)
        except Exception as e:
            logger.error(f"❌ Agent {agent.name} error: {e}")
            return []

    async def _analyze_cross_meeting_links(
        self,
        meeting_id: str,
        agent_results: Dict[str, List[Dict[str, Any]]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Анализ связей с другими встречами.
        """
        try:
            logger.info("🔗 Analyzing cross-meeting links")

            # Инициализируем analyzer per-user (иначе будет утечка/смешение данных между пользователями)
            user_id = (meeting_context or {}).get("user_id")
            if not user_id:
                logger.warning("⚠️ Cross-meeting links skipped: missing user_id in meeting_context")
                return {}

            if not hasattr(self, "_cross_meeting_analyzers") or self._cross_meeting_analyzers is None:
                self._cross_meeting_analyzers = {}

            if user_id not in self._cross_meeting_analyzers:
                from backend.core.store.graph_builder import GraphBuilder
                from backend.core.store.tenant_paths import graph_path_for_user

                graph = GraphBuilder(use_networkx=None, graph_storage_path=graph_path_for_user(user_id))  # Auto-detect backend
                await graph.connect()
                self._cross_meeting_analyzers[user_id] = CrossMeetingAnalyzer(
                    graph_builder=graph,
                    llm_router=self.llm,
                )

            analyzer = self._cross_meeting_analyzers[user_id]

            # Подготавливаем данные встречи
            meeting_data = {
                "participants": agent_results.get("participants", []),
                "tasks": agent_results.get("tasks", []),
                "decisions": agent_results.get("decisions", []),
                "entities": agent_results.get("entities", []),
                "projects": meeting_context.get("projects", []) if meeting_context else [],
                "date": meeting_context.get("date") if meeting_context else None
            }

            # Анализируем связи
            result = await analyzer.analyze_meeting_links(
                meeting_id=meeting_id,
                meeting_data=meeting_data,
                max_links=10
            )

            logger.info(f"  ✅ Found {len(result.get('links', []))} cross-meeting links")
            return result

        except Exception as e:
            logger.error(f"❌ Cross-meeting analysis error: {e}")
            return {}

    async def _run_psychological_analysis(
        self,
        transcript: str,
        participants: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Запуск психологического анализа (Module 08)"""
        try:
            logger.info("🧠 Running psychological analysis")
            result = await self.psychological_analyzer.analyze(
                transcript=transcript,
                participants=participants,
                meeting_context=meeting_context
            )
            logger.info(f"  ✅ Psychological analysis: {len(result.get('personality_profiles', []))} profiles created")
            return result
        except Exception as e:
            logger.error(f"❌ Psychological analysis error: {e}")
            return {}

    async def _run_future_prediction(
        self,
        transcript: str,
        agent_results: Dict[str, List[Dict[str, Any]]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Запуск прогнозирования (Module 09)"""
        try:
            logger.info("🔮 Running future prediction")
            result = await self.future_predictor.predict(
                transcript=transcript,
                projects=agent_results.get("project_statuses", []),
                tasks=agent_results.get("tasks", []),
                decisions=agent_results.get("decisions", []),
                meeting_context=meeting_context
            )
            logger.info(f"  ✅ Future prediction: {len(result.get('scenarios', []))} scenarios, {len(result.get('risks', []))} risks")
            return result
        except Exception as e:
            logger.error(f"❌ Future prediction error: {e}")
            return {}

    async def _calculate_intelligence_weights(
        self,
        agent_results: Dict[str, List[Dict[str, Any]]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Расчёт Intelligence Weights (Module 13)"""
        try:
            logger.info("⚖️ Calculating intelligence weights")
            result = await self.intelligence_weights.calculate_weights(
                tasks=agent_results.get("tasks", []),
                decisions=agent_results.get("decisions", []),
                projects=agent_results.get("project_statuses", []),
                meeting_context=meeting_context
            )
            logger.info(f"  ✅ Intelligence weights: {len(result.get('critical_items', []))} critical items")
            return result
        except Exception as e:
            logger.error(f"❌ Intelligence weights error: {e}")
            return {}

    async def _generate_search_metadata(
        self,
        meeting_id: str,
        transcript: str,
        agent_results: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        """Генерация Search Metadata (Module 18)"""
        try:
            logger.info("🔍 Generating search metadata")
            result = await self.search_metadata_generator.generate_metadata(
                meeting_id=meeting_id,
                transcript=transcript,
                extracted_data=agent_results
            )
            logger.info(f"  ✅ Search metadata: {len(result.get('keywords', []))} keywords, {len(result.get('topics', []))} topics")
            return result
        except Exception as e:
            logger.error(f"❌ Search metadata error: {e}")
            return {}

    async def _perform_cross_analysis(
        self,
        agent_results: Dict[str, List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        """
        Cross-agent анализ для выявления связей и паттернов.
        """
        logger.info("🧠 Performing cross-agent analysis")

        insights = {
            "decision_task_links": [],
            "participant_responsibilities": [],
            "entity_mentions": [],
            "kpi_trends": []
        }

        try:
            decisions = agent_results.get("decisions", [])
            tasks = agent_results.get("tasks", [])
            participants = agent_results.get("participants", [])

            # Связываем решения с задачами
            for decision in decisions:
                decision_id = decision.get("decision_id")
                action_items = decision.get("action_items", [])

                for action in action_items:
                    # Ищем соответствующую задачу
                    for task in tasks:
                        if self._is_related(action.get("action", ""), task.get("title", "")):
                            insights["decision_task_links"].append({
                                "decision_id": decision_id,
                                "task_id": task.get("task_id"),
                                "link_type": "creates"
                            })

            # Связываем участников с ответственностями
            for participant in participants:
                name = participant.get("name", "")
                responsibilities = []

                for task in tasks:
                    assignee = task.get("assignee", {})
                    if isinstance(assignee, dict) and name.lower() in assignee.get("name", "").lower():
                        responsibilities.append(task.get("task_id"))

                if responsibilities:
                    insights["participant_responsibilities"].append({
                        "participant_id": participant.get("participant_id"),
                        "name": name,
                        "task_ids": responsibilities
                    })

        except Exception as e:
            logger.error(f"❌ Cross-analysis error: {e}")

        return insights

    def _is_related(self, text1: str, text2: str) -> bool:
        """Простая проверка связи между текстами"""
        if not text1 or not text2:
            return False

        words1 = set(word.lower() for word in text1.split() if len(word) > 3)
        words2 = set(word.lower() for word in text2.split() if len(word) > 3)

        return len(words1 & words2) >= 2

    async def _run_decision_learning(
        self,
        transcript: str,
        meeting_id: str,
        decisions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Запуск Decision Learning анализа (Module 11)."""
        try:
            logger.info("📊 Running Decision Learning analysis")
            result = await self.decision_learning_agent.analyze_decisions(
                transcript=transcript,
                meeting_id=meeting_id,
                existing_decisions=decisions
            )
            logger.info(f"  ✅ Decision learning: {len(result.get('decisions', []))} decisions, "
                       f"{len(result.get('learning_data', []))} training examples")
            return result
        except Exception as e:
            logger.error(f"❌ Decision learning error: {e}")
            return {}

    async def _run_synthesis_analysis(
        self,
        agent_results: Dict[str, List[Dict[str, Any]]],
        meeting_id: str
    ) -> Dict[str, Any]:
        """Запуск Synthesis анализа (Module 05)."""
        try:
            logger.info("🔬 Running Synthesis analysis")
            result = await self.synthesis_agent.analyze_patterns(
                all_modules_data=agent_results,
                meeting_id=meeting_id
            )
            logger.info(f"  ✅ Synthesis: {len(result.get('success_patterns', []))} success patterns, "
                       f"{len(result.get('failure_patterns', []))} failure patterns")
            return result
        except Exception as e:
            logger.error(f"❌ Synthesis analysis error: {e}")
            return {}

    async def _run_goals_tracking(
        self,
        transcript: str,
        meeting_id: str,
        agent_results: Dict[str, List[Dict[str, Any]]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Запуск Goals Tracking анализа (Module 17)."""
        try:
            logger.info("🎯 Running Goals Tracking analysis")

            # Подготовка данных встречи
            meeting_data = {
                "transcript": transcript,
                "decisions": agent_results.get("decisions", []),
                "tasks": agent_results.get("tasks", []),
                "projects": agent_results.get("project_statuses", []),
                "participants": agent_results.get("participants", [])
            }

            result = await self.goals_tracking_agent.analyze_goals(
                meeting_data=meeting_data,
                meeting_id=meeting_id,
                context=meeting_context
            )
            logger.info(f"  ✅ Goals tracking: {len(result.get('goals', []))} goals, "
                       f"{len(result.get('conflicts', []))} conflicts")
            return result
        except Exception as e:
            logger.error(f"❌ Goals tracking error: {e}")
            return {}

    async def _run_knowledge_extraction(
        self,
        transcript: str,
        meeting_id: str,
        agent_results: Dict[str, List[Dict[str, Any]]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Запуск Knowledge Extraction (Module 16)"""
        try:
            logger.info("📚 Running Knowledge Extraction")

            # Подготовка данных встречи
            meeting_data = {
                "decisions": agent_results.get("decisions", []),
                "tasks": agent_results.get("tasks", []),
                "participants": agent_results.get("participants", []),
                "entities": agent_results.get("entities", []),
                "kpis": agent_results.get("kpis", []),
                "opinions": agent_results.get("opinions", []),
                "ideas": agent_results.get("ideas", []),
                "project_statuses": agent_results.get("project_statuses", []),
                "enhanced_kpis": agent_results.get("enhanced_kpis", []),
                "numbers": agent_results.get("kpis", [])  # KPIs содержат числовые данные
            }

            result = await self.knowledge_extractor.extract_knowledge(
                meeting_id=meeting_id,
                meeting_data=meeting_data,
                transcript=transcript,
                context=meeting_context
            )

            total = result.get("total_extracted", 0)
            logger.info(f"  ✅ Knowledge extraction: {total} items extracted")
            return result
        except Exception as e:
            logger.error(f"❌ Knowledge extraction error: {e}")
            return {}

    async def _run_regulation_extraction(
        self,
        transcript: str,
        meeting_id: str,
        agent_results: Dict[str, List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """Извлечение регламентов и правил из встречи."""
        try:
            logger.info("📋 Running Regulation Extraction")

            meeting_data = {
                "meeting_id": meeting_id,
                "transcript_raw": transcript,
                "decisions": agent_results.get("decisions", []),
                "tasks": agent_results.get("tasks", [])
            }

            regulations = await self.regulation_extractor.extract_regulations(meeting_data)

            logger.info(f"  ✅ Regulation extraction: {len(regulations)} regulations found")
            return [r.to_dict() for r in regulations]
        except Exception as e:
            logger.error(f"❌ Regulation extraction error: {e}")
            return []

    async def _run_template_extraction(
        self,
        transcript: str,
        meeting_id: str,
        agent_results: Dict[str, List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        """Извлечение шаблонов и чек-листов из встречи."""
        try:
            logger.info("📄 Running Template Extraction")

            meeting_data = {
                "meeting_id": meeting_id,
                "transcript_raw": transcript,
                "tasks": agent_results.get("tasks", []),
                "ideas": agent_results.get("ideas", []),
                "decisions": agent_results.get("decisions", [])
            }

            templates = await self.template_extractor.extract_templates(
                meeting_id=meeting_id,
                meeting_data=meeting_data
            )

            logger.info(f"  ✅ Template extraction: {len(templates)} templates found")
            return [t.to_dict() for t in templates]
        except Exception as e:
            logger.error(f"❌ Template extraction error: {e}")
            return []

    def _consolidate_results(
        self,
        agent_results: Dict[str, List[Dict[str, Any]]],
        cross_insights: Dict[str, Any],
        cross_meeting_links: Dict[str, Any],
        psychological_analysis: Dict[str, Any],
        future_predictions: Dict[str, Any],
        weighted_priorities: Dict[str, Any],
        search_metadata: Dict[str, Any],
        decision_learning: Dict[str, Any],
        synthesis_analysis: Dict[str, Any],
        goals_tracking: Dict[str, Any],
        knowledge_extraction: Dict[str, Any],
        regulations: List[Dict[str, Any]],
        extracted_templates: List[Dict[str, Any]],
        meeting_id: str,
        meeting_context: Optional[Dict[str, Any]],
        start_time: datetime,
        agent_errors: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Консолидация всех результатов"""
        end_time = datetime.now(timezone.utc)
        processing_time = (end_time - start_time).total_seconds()

        # Подсчёт сущностей
        total_entities = sum(len(items) for items in agent_results.values())

        return {
            "meeting_id": meeting_id,
            "status": "completed",
            "processing_timestamp": end_time.isoformat(),
            "processing_time_seconds": processing_time,

            # Метаданные
            # Ошибки базовых агентов: {agent: error}. Пусто = все отработали
            # (в т.ч. те, кто честно извлёк 0). Читает quality-gate встречи.
            "agent_errors": dict(agent_errors or {}),
            "metadata": {
                "orchestrator": self.name,
                "version": self.version,
                "agents_used": list(agent_results.keys()),
                "agents_failed": list((agent_errors or {}).keys()),
                "context": meeting_context or {}
            },

            # Результаты агентов (прямой доступ)
            "decisions": agent_results.get("decisions", []),
            "tasks": agent_results.get("tasks", []),
            "participants": agent_results.get("participants", []),
            "entities": agent_results.get("entities", []),
            "kpis": agent_results.get("kpis", []),
            # Расширенные результаты
            "opinions": agent_results.get("opinions", []),
            "ideas": agent_results.get("ideas", []),
            "project_statuses": agent_results.get("project_statuses", []),
            "enhanced_kpis": agent_results.get("enhanced_kpis", []),
            "fact_checks": agent_results.get("fact_checks", []),
            "cogni_profiles": agent_results.get("cogni_profiles", []),

            # Полные результаты агентов
            "agent_results": agent_results,

            # Cross-agent insights
            "cross_insights": cross_insights,

            # Cross-meeting links (связи между встречами)
            "cross_meeting_links": cross_meeting_links,

            # Психологический анализ (Module 08)
            "psychological_analysis": psychological_analysis,

            # Прогнозирование (Module 09)
            "future_predictions": future_predictions,

            # Intelligence Weights (Module 13)
            "weighted_priorities": weighted_priorities,

            # Search Metadata (Module 18)
            "search_metadata": search_metadata,

            # Decision Learning (Module 11)
            "decision_learning": decision_learning,

            # Synthesis Analysis (Module 05)
            "synthesis_analysis": synthesis_analysis,

            # Goals Tracking (Module 17)
            "goals_tracking": goals_tracking,

            # Knowledge Extraction (Module 16)
            "knowledge_extraction": knowledge_extraction,

            # Templates & Regulations
            "regulations": regulations,
            "extracted_templates": extracted_templates,

            # Сводка
            "summary": {
                "total_entities": total_entities,
                "decisions_count": len(agent_results.get("decisions", [])),
                "tasks_count": len(agent_results.get("tasks", [])),
                "participants_count": len(agent_results.get("participants", [])),
                "entities_count": len(agent_results.get("entities", [])),
                "kpis_count": len(agent_results.get("kpis", [])),
                # Расширенные счётчики
                "opinions_count": len(agent_results.get("opinions", [])),
                "ideas_count": len(agent_results.get("ideas", [])),
                "project_statuses_count": len(agent_results.get("project_statuses", [])),
                "enhanced_kpis_count": len(agent_results.get("enhanced_kpis", [])),
                "fact_checks_count": len(agent_results.get("fact_checks", [])),
                "cross_links_found": len(cross_insights.get("decision_task_links", [])),
                # Cross-meeting stats
                "related_meetings_found": len(cross_meeting_links.get("links", [])),
                "entity_evolution_tracked": len(cross_meeting_links.get("entity_evolution", [])),
                "recurring_topics": len(cross_meeting_links.get("recurring_topics", [])),
                # Психологический анализ
                "personality_profiles_count": len(psychological_analysis.get("personality_profiles", [])),
                "conflict_risks_count": len(psychological_analysis.get("conflict_risks", [])),
                # Прогнозирование
                "scenarios_count": len(future_predictions.get("scenarios", [])),
                "risks_count": len(future_predictions.get("risks", [])),
                "early_warnings_count": len(future_predictions.get("early_warnings", [])),
                # Intelligence Weights
                "critical_items_count": len(weighted_priorities.get("critical_items", [])),
                # Search Metadata
                "keywords_count": len(search_metadata.get("keywords", [])),
                "topics_count": len(search_metadata.get("topics", [])),
                # Decision Learning (Module 11)
                "learning_data_count": len(decision_learning.get("learning_data", [])),
                # Synthesis Analysis (Module 05)
                "success_patterns_count": len(synthesis_analysis.get("success_patterns", [])),
                "failure_patterns_count": len(synthesis_analysis.get("failure_patterns", [])),
                # Goals Tracking (Module 17)
                "goals_count": len(goals_tracking.get("goals", [])),
                # Templates & Regulations
                "regulations_count": len(regulations),
                "extracted_templates_count": len(extracted_templates),
                "goal_conflicts_count": len(goals_tracking.get("conflicts", [])),
                # Knowledge Extraction (Module 16)
                "knowledge_items_count": knowledge_extraction.get("total_extracted", 0),
                "knowledge_cases_count": len(knowledge_extraction.get("cases", [])),
                "knowledge_methodologies_count": len(knowledge_extraction.get("methodologies", [])),
                "knowledge_best_practices_count": len(knowledge_extraction.get("best_practices", [])),
                "knowledge_insights_count": len(knowledge_extraction.get("insights", []))
            },

            # LLM статистика
            "llm_stats": self.llm.get_stats()
        }

    async def _save_to_storage(self, results: Dict[str, Any]):
        """Сохранение результатов в хранилище"""
        if not self.storage:
            return

        try:
            # TODO: Реализовать сохранение в UnifiedStorage
            logger.info(f"💾 Saving results for meeting {results['meeting_id']}")
        except Exception as e:
            logger.error(f"❌ Storage save error: {e}")

    async def _reinforce_memory(
        self,
        meeting_id: str,
        agent_results: Dict[str, List[Dict[str, Any]]],
        transcript: str
    ) -> Dict[str, Any]:
        """
        Усиление памяти через Hebbian Learning и Frequency Tracking.

        Выполняет:
        1. Отслеживание частоты упоминаний сущностей
        2. Усиление связей между совместно упомянутыми сущностями
        3. Анализ эмоциональной значимости

        Args:
            meeting_id: ID встречи
            agent_results: Результаты агентов
            transcript: Транскрипт для анализа эмоций

        Returns:
            Статистика усиления памяти
        """
        stats = {
            "frequency_tracking": {},
            "hebbian_learning": {},
            "emotional_salience": {},
            "enabled": False,
        }

        try:
            from backend.core.config import flags

            # Проверяем включены ли флаги
            if not any([
                flags.enable_frequency_tracking,
                flags.enable_hebbian_learning,
                flags.enable_emotional_salience
            ]):
                return stats

            stats["enabled"] = True

            # Получаем или создаём GraphBuilder
            graph = self.graph_builder
            if not graph:
                logger.warning("⚠️ Memory reinforcement skipped: no graph_builder")
                return stats

            # 1. Frequency Tracking
            if flags.enable_frequency_tracking:
                from backend.core.memory import get_frequency_tracker
                tracker = get_frequency_tracker(graph)

                freq_stats = await tracker.track_meeting_entities(
                    meeting_id=meeting_id,
                    entities=agent_results.get("entities", []),
                    participants=agent_results.get("participants", [])
                )
                stats["frequency_tracking"] = freq_stats
                logger.info(f"📊 Frequency tracked: {freq_stats.get('entities_tracked', 0)} entities")

            # 2. Hebbian Learning
            # defer_cooccurrence: внешний пайплайн (knowledge_sync) создаёт узлы
            # сущностей ПОСЛЕ process_meeting и сам запускает co-occurrence с
            # реальными id узлов. Здесь сущности — это ещё сырые slug'и без узлов
            # в графе, поэтому ранний запуск давал 0 связей и спам «Nodes not
            # found». В таком режиме откладываем co-occurrence наружу.
            if flags.enable_hebbian_learning and not getattr(self, "defer_cooccurrence", False):
                from backend.core.memory import get_hebbian_learning
                hebbian = get_hebbian_learning(graph)

                hebb_stats = await hebbian.process_meeting_cooccurrences(
                    meeting_id=meeting_id,
                    entities=agent_results.get("entities", []),
                    participants=agent_results.get("participants", [])
                )
                stats["hebbian_learning"] = hebb_stats
                logger.info(f"🔗 Hebbian: {hebb_stats.get('connections_strengthened', 0)} connections")
            elif getattr(self, "defer_cooccurrence", False):
                logger.debug("🔗 Hebbian co-occurrence отложен на post-save (defer_cooccurrence)")

            # 3. Emotional Salience
            if flags.enable_emotional_salience:
                from backend.core.memory import get_emotional_analyzer
                analyzer = get_emotional_analyzer()

                # Анализируем транскрипт
                salience_result = analyzer.analyze_text(transcript[:5000])  # Ограничиваем размер
                stats["emotional_salience"] = {
                    "urgency_score": salience_result.get("urgency_score", 0),
                    "negative_sentiment": salience_result.get("negative_sentiment", 0),
                    "positive_sentiment": salience_result.get("positive_sentiment", 0),
                    "priority_level": salience_result.get("priority_level", "normal"),
                    "factors_count": len(salience_result.get("factors", [])),
                }

                # Обновляем emotional_salience для сущностей с высокой значимостью
                if salience_result.get("overall_salience", 0) > 0.5:
                    for entity in agent_results.get("entities", []):
                        entity_id = entity.get("entity_id") or entity.get("id")
                        if entity_id:
                            await graph.update_emotional_salience(
                                node_id=entity_id,
                                salience_delta=salience_result.get("overall_salience", 0) * 0.1
                            )

                logger.info(f"😊 Emotional salience: {salience_result.get('priority_level', 'normal')}")

            # Сохраняем граф
            await graph.save()

            return stats

        except ImportError as e:
            logger.debug(f"Memory reinforcement modules not available: {e}")
            return stats
        except Exception as e:
            logger.error(f"❌ Memory reinforcement error: {e}")
            return stats

    async def _validate_extracted_data(
        self,
        agent_results: Dict[str, List[Dict[str, Any]]],
        transcript: str,
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Валидация извлечённых данных.

        Проверяет:
        1. Релевантность для компании
        2. Фильтрует внешние данные
        3. Проверяет согласованность

        Args:
            agent_results: Результаты агентов
            transcript: Транскрипт для контекста
            meeting_context: Контекст встречи

        Returns:
            Результат валидации
        """
        result = {
            "enabled": False,
            "validation": {},
            "filtered_data": {},
            "quality_score": 1.0,
        }

        try:
            from backend.core.config import flags

            if not flags.enable_validation_agent:
                return result

            result["enabled"] = True

            # Получаем контекст компании если есть
            company_context = None
            if meeting_context and meeting_context.get("company_snapshot"):
                company_context = meeting_context.get("company_snapshot")

            # 1. Валидация сущностей, решений, задач
            validation = await self.validation_agent.validate_extracted_data(
                entities=agent_results.get("entities", []),
                decisions=agent_results.get("decisions", []),
                tasks=agent_results.get("tasks", []),
                company_context=company_context,
                transcript=transcript
            )
            result["validation"] = validation
            result["quality_score"] = validation.get("quality_score", 1.0)

            # 2. Фильтрация внешних данных (если включено)
            if flags.enable_external_data_filtering:
                filtered = await self.validation_agent.filter_external_data(
                    entities=agent_results.get("entities", []),
                    transcript=transcript
                )
                result["filtered_data"] = filtered

                # Логируем если много внешних данных
                external_count = len(filtered.get("external", []))
                if external_count > 0:
                    logger.info(f"🔍 Filtered {external_count} external entities")

            logger.info(f"✅ Validation complete: quality={result['quality_score']:.2f}")

            return result

        except ImportError as e:
            logger.debug(f"Validation agent not available: {e}")
            return result
        except Exception as e:
            logger.error(f"❌ Validation error: {e}")
            return result

    def _update_stats(self, results: Dict[str, Any], start_time: datetime):
        """Обновление статистики"""
        self.stats["meetings_processed"] += 1
        self.stats["total_entities_extracted"] += results["summary"]["total_entities"]

        # Обновляем среднее время обработки
        processing_time = results.get("processing_time_seconds", 0)
        n = self.stats["meetings_processed"]
        self.stats["avg_processing_time"] = (
            (self.stats["avg_processing_time"] * (n - 1) + processing_time) / n
        )

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику оркестратора"""
        return {
            "orchestrator": self.name,
            "version": self.version,
            "stats": self.stats,
            "agents": {
                name: agent.get_stats()
                for name, agent in self.agents.items()
            },
            "llm": self.llm.get_stats()
        }


# Singleton
_global_orchestrator: Optional[CaptureOrchestrator] = None


def get_capture_orchestrator() -> CaptureOrchestrator:
    """Получить глобальный экземпляр оркестратора"""
    global _global_orchestrator
    if _global_orchestrator is None:
        _global_orchestrator = CaptureOrchestrator()
    return _global_orchestrator


def reset_global_orchestrator():
    """Сбросить глобальный оркестратор"""
    global _global_orchestrator
    _global_orchestrator = None



