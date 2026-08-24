# -*- coding: utf-8 -*-
"""
Enhanced Search Orchestrator - Главный оркестратор умного поиска.

Объединяет лучшие практики из старой системы (Module 10.2) и новую архитектуру:
1. Query Understanding: Глубокий анализ интента
2. Hybrid Search: Vector + BM25 + Graph (через HybridSearchOrchestrator)
3. Verification: Проверка противоречий и фактов
4. Reflection: Оценка качества и автоматический retry
5. Context Assembly: Сборка богатого контекста с цитатами
6. Response Synthesis: Генерация точного ответа

Использование:
```python
orchestrator = EnhancedSearchOrchestrator(
    hybrid_search=hybrid_search,
    llm_router=llm_router
)
result = await orchestrator.search("какой статус у проекта X?")
```
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..llm.router import LLMRouter, ModelTier
from .graph_trace_builder import build_graph_trace
from .hybrid_search_orchestrator import HybridSearchOrchestrator, HybridSearchStrategy

logger = logging.getLogger(__name__)

# Сколько символов текста брать с одного источника при сборке контекста.
# 500 было слишком мало (огрызки транскриптов); 2000 даёт модели полноценный
# фрагмент. Общий размер всё равно ограничивает Context Window Guard по tier.
_PER_SOURCE_TEXT_LIMIT = 2000

# Deep dive: суммарный бюджет символов полных транскриптов на один глубокий
# проход (делится между выбранными встречами). ~40K симв ≈ ~11K токенов.
DEEP_DIVE_CONTEXT_BUDGET = 40000
# Порог уверенности, ниже которого предлагаем «погрузиться в транскрипты».
DEEP_DIVE_CONFIDENCE_THRESHOLD = 0.6

# Маркеры РЕШЕНЧЕСКОГО (динамического) запроса — для гейта wisdom-в-чате.
# Измеренный урок (ТЗ_AGI/INVENTORY_HONEST §5, HYPOTHESES_SPEC Gate-0): опыт/
# гипотезы помогают на динамических вопросах, на фактах чистый LLM сильнее.
_DECISION_MARKERS = (
    "стоит ли ", "нужно ли ", "выбрать", "выбор", "стратег", "риск",
    "решен", "масштаб", "нанимать", "нанять", "запус", "инвест", "расшир",
    "приоритет", "бюджет", "сократ", "оптимиз", "компромисс", "трейд", " vs ",
    " или ", " лучше ", "развив", "should", "whether", "vs ", "versus", " or ",
    "strategy", "risk", "decide", "decision", "trade-off", "tradeoff",
    "prioriti", "invest", "scale", "launch", "hire", "budget",
)


def _looks_like_decision_query(query: str) -> bool:
    """Эвристика: запрос похож на решенческий/динамический (а не факт-ретрив).
    Дёшево, без LLM. Требует ≥1 маркер решения И минимальную длину — иначе
    wisdom-инъекция считается шумом (факты лучше берёт чистый LLM)."""
    if not query:
        return False
    q = f" {query.lower().strip()} "
    if len(q) < 14:
        return False
    return any(m in q for m in _DECISION_MARKERS)


# Глобальный кэш названий встреч (meeting_id -> title)
_meeting_titles_cache: Dict[str, str] = {}

# Кэш загруженной route() из answer_mode_router (см. _load_answer_mode_route)
_answer_mode_route_cache: list = []


def _load_answer_mode_route():
    """Устойчивая загрузка route() из answer_mode_router.

    Прямой импорт `backend.core.think.answer_mode_router` тянет think/__init__,
    который жёстко импортирует autogen (AG2). В окружении без autogen это МОЛЧА
    отключало флагнутую фичу (выловлено e2e-тестом). Сам роутер — чистый stdlib,
    поэтому fallback: загрузка модуля по пути файла, минуя пакетный __init__.
    В полном проде (autogen установлен) работает первый путь — поведение прежнее.
    """
    if _answer_mode_route_cache:
        return _answer_mode_route_cache[0]
    route = None
    try:
        from backend.core.think.answer_mode_router import route as _r
        route = _r
    except Exception:
        try:
            import importlib.util as _ilu
            import os as _os
            import sys as _sys
            _p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                               "think", "answer_mode_router.py")
            _spec = _ilu.spec_from_file_location("_tessent_answer_mode_router", _p)
            _mod = _ilu.module_from_spec(_spec)
            # регистрация ДО exec_module обязательна: @dataclass смотрит
            # sys.modules[cls.__module__] (иначе AttributeError: NoneType)
            _sys.modules["_tessent_answer_mode_router"] = _mod
            _spec.loader.exec_module(_mod)
            route = _mod.route
        except Exception:
            logger.debug("answer_mode_router unavailable", exc_info=True)
    _answer_mode_route_cache.append(route)
    return route

@dataclass
class SearchResult:
    """Финальный результат поиска"""
    answer: str
    sources: List[Dict[str, Any]]
    confidence: float
    metadata: Dict[str, Any]
    reasoning_steps: List[str]
    processing_time_ms: int

class EnhancedSearchOrchestrator:
    """
    Усовершенствованный оркестратор поиска.
    Реализует паттерн "Adaptive Retrieval-Augmented Generation" (Adaptive RAG).
    """

    def __init__(
        self,
        hybrid_search: HybridSearchOrchestrator,
        llm_router: LLMRouter,
        verification_enabled: bool = True,
        reflection_enabled: bool = True,
        max_retries: int = 2
    ):
        self.hybrid_search = hybrid_search
        self.llm = llm_router
        self.verification_enabled = verification_enabled
        self.reflection_enabled = reflection_enabled
        self.max_retries = max_retries

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        search_depth: str = "standard",  # standard, deep, comprehensive
        extra_context: Optional[str] = None,  # Дополнительный контекст (например, из документов)
        model_tier: str = "standard"  # "standard" или "premium"
    ) -> SearchResult:
        """
        Выполнить умный поиск.

        Args:
            query: Запрос пользователя
            user_id: ID пользователя (для контекста и прав)
            filters: Дополнительные фильтры
            search_depth: Глубина поиска
            extra_context: Дополнительный контекст (например, из документов)
            model_tier: Уровень модели (standard/premium)

        Returns:
            SearchResult с ответом и источниками
        """
        start_time = datetime.now()
        steps = []
        self._extra_context = extra_context  # Сохраняем для использования в _synthesize_answer
        self._model_tier = model_tier  # Сохраняем tier модели

        # МЯГКИЙ ДЕДЛАЙН: тяжёлые ОПЦИОНАЛЬНЫЕ этапы (reasoning chains)
        # пропускаются, когда времени в обрез — лучше полный ответ за 90с
        # без гипотез, чем обрыв по таймауту 240с (golden-прогон: вопрос
        # про регламенты ел 200с). TESSENT_CHAT_SOFT_DEADLINE_S, деф. 150.
        import os as _os
        try:
            _soft_s = float(_os.getenv("TESSENT_CHAT_SOFT_DEADLINE_S", "150"))
        except (TypeError, ValueError):
            _soft_s = 150.0
        _soft_deadline_ts = start_time.timestamp() + _soft_s
        self._time_left = (lambda: _soft_deadline_ts
                           - datetime.now().timestamp())

        # Answer-mode router (за флагом enable_answer_mode_router). Оркестратор —
        # singleton, поэтому СБРАСЫВАЕМ режим на каждый запрос. Для вопросов про
        # динамику/историю/мультисессию/синтез включаем analysis-усиление синтеза
        # (см. _synthesize_answer). Прямые факты → factual (синтез прежний).
        # Замер: docs/ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md. Best-effort, не роняет поиск.
        self._answer_mode = None
        self._answer_mode_reason = None
        try:
            from backend.core.config.feature_flags import get_feature_flags
            if get_feature_flags().enable_answer_mode_router:
                _route = _load_answer_mode_route()
                if _route is not None:
                    _am = _route(query)
                    self._answer_mode = _am.mode
                    self._answer_mode_reason = _am.reason
                    if _am.mode == "analysis":
                        steps.append(f"🧭 Answer-mode: analysis ({_am.reason})")
        except Exception:
            logger.debug("answer-mode router skipped", exc_info=True)

        # Персонализация под пользователя (за флагом enable_chat_personalization).
        # get_personalized_prompt существовал, но НЕ вызывался нигде
        # (docs/CAPABILITY_READINESS.md, быстрая победа B4): стиль/детализация/
        # известные темы из user_profiles → инструкция в синтез. Сброс на каждый
        # запрос (singleton). Best-effort: профиль недоступен → синтез прежний.
        # Корпоративный словарь (enable_company_glossary, Glean-перенос №3):
        # «КП» в запросе → поиск дополнительно видит «коммерческое
        # предложение» (и наоборот). Запрос пользователя НЕ переписывается —
        # синтез видит исходную формулировку; расширение только для retrieval.
        try:
            from backend.core.config.feature_flags import get_feature_flags as _gf
            if user_id and _gf().enable_company_glossary:
                from backend.core.memory.company_glossary import (
                    glossary_for_user,
                )
                _exp = glossary_for_user(user_id).expand_query(query)
                if _exp["expanded"]:
                    query = _exp["query_expanded"]
                    steps.append("📖 Словарь компании: +"
                                 + ", ".join(_exp["additions"]))
        except Exception:
            logger.debug("glossary skipped", exc_info=True)

        self._personalization = None
        try:
            from backend.core.config.feature_flags import get_feature_flags as _gff
            if user_id and _gff().enable_chat_personalization:
                from backend.memory.user_profiles import get_user_profile_service
                _pp = await get_user_profile_service().get_personalized_prompt(user_id)
                if _pp and _pp.strip():
                    self._personalization = _pp.strip()
                    steps.append("👤 Персонализация: профиль пользователя применён")
                # Rule Book: регламенты компании (свёрнутые ночью из встреч) —
                # ассистент должен им следовать. Раньше rule_book не читался.
                try:
                    from backend.core.memory.rule_book import get_rule_book_service
                    _rb = get_rule_book_service().load(user_id).to_prompt()
                    if _rb and _rb.strip():
                        self._personalization = (
                            (self._personalization + "\n\n" if self._personalization else "")
                            + _rb.strip())
                        steps.append("📜 Rule Book: регламенты применены")
                except Exception:
                    logger.debug("rule book load skipped", exc_info=True)
        except Exception:
            logger.debug("chat personalization skipped", exc_info=True)

        # Wisdom→чат (за флагом wisdom_in_search_enabled): подмешиваем «опыт
        # прошлых решений» из BWE в extra_context → попадает в синтез без правки
        # промптов. ВАЖНО (измеренный урок ТЗ_AGI/INVENTORY_HONEST §5): wisdom
        # помогает на ДИНАМИЧЕСКИХ (решенческих) вопросах, а на ФАКТАХ чистый
        # LLM сильнее → гейтим на decision-запросы, не на длину. Graceful.
        try:
            from backend.config import settings as _settings
            if (
                getattr(_settings, "wisdom_in_search_enabled", False)
                and user_id
                and _looks_like_decision_query(query)
            ):
                from backend.core.wisdom.wisdom_engine import get_wisdom_engine
                _engine = await get_wisdom_engine()
                _brief = await _engine.build_wisdom_brief(user_id=user_id, situation=query)
                if _brief:
                    _wisdom_block = "\n\nОПЫТ ПРОШЛЫХ РЕШЕНИЙ КОМПАНИИ:\n" + "\n".join(_brief)
                    self._extra_context = (self._extra_context or "") + _wisdom_block
                    steps.append(f"🔮 Wisdom: {len(_brief)} пунктов опыта добавлено")
        except Exception as _e:
            logger.debug(f"wisdom-in-search skipped: {_e}")

        # ═══════════════════════════════════════════════════════════
        # QUICK MODE: Быстрый поиск без LLM-анализа и без reflection
        # ~3-5 сек, 1 LLM вызов (только synthesis), ~$0.001
        # ═══════════════════════════════════════════════════════════
        if search_depth == "quick":
            steps.append("⚡ QUICK: Быстрый поиск (без анализа, без reflection)")

            # Простой анализ без LLM — экономим ~$0.0005 и ~2 сек
            understanding = {
                "intent": "FACTOID",
                "complexity": "SIMPLE",
                "analysis_type": {"requires_search": True},
                "semantic_decomposition": {},
            }
            retrieval_plan = self._build_retrieval_plan(
                query=query,
                understanding=understanding,
                search_depth="quick",
                filters=filters,
            )
            understanding["_retrieval_plan"] = retrieval_plan
            steps.append(
                f"   🗂️ Retrieval plan: {', '.join(retrieval_plan.get('primary_keys', [])[:8])}"
            )

            # Hybrid Search с уменьшенным top_k
            search_results = await self.hybrid_search.search(
                query=query,
                strategy=HybridSearchStrategy.ADAPTIVE,
                top_k=15,
                filters=filters,
                collections=retrieval_plan.get("primary_collections"),
            )
            steps.append(f"   Найдено: {search_results.total_found} результатов")

            # Сборка контекста
            context = await self._assemble_context(search_results, query, understanding)

            # Synthesis — 1 LLM вызов
            steps.append("Генерация краткого ответа...")
            final_answer = await self._synthesize_answer(query, context, understanding, search_depth="quick", user_id=user_id)

            processing_time = int((datetime.now() - start_time).total_seconds() * 1000)
            quick_confidence = 0.7
            evidence_contract = self._build_evidence_contract(
                context=context,
                reasoning_steps=steps,
                confidence=quick_confidence,
                search_depth="quick",
            )
            graph_trace = await build_graph_trace(
                graph_builder=self.hybrid_search.graph_builder,
                sources=context.get("sources", []),
                reasoning_steps=evidence_contract.get("inferred_path", []),
                title="Retrieval provenance trace",
            )
            if graph_trace:
                evidence_contract["graph_trace"] = graph_trace
            steps.append(
                f"🧾 Evidence contract: direct={len(evidence_contract.get('direct_evidence', []))}, "
                f"ambiguities={len(evidence_contract.get('ambiguities', []))}"
            )
            return SearchResult(
                answer=final_answer,
                sources=context.get("sources", []),
                confidence=quick_confidence,
                metadata={
                    "search_depth": "quick",
                    "attempts": 1,
                    "strategy_used": search_results.strategy_used,
                    "total_found": search_results.total_found,
                    "retrieval_plan": {
                        "primary": retrieval_plan.get("primary_keys", []),
                        "fallback": retrieval_plan.get("fallback_keys", []),
                    },
                    "source_breakdown": context.get("source_breakdown", {}),
                    "evidence_contract": evidence_contract,
                    "graph_trace": graph_trace,
                    "answer_mode": getattr(self, "_answer_mode", None),
                },
                reasoning_steps=steps,
                processing_time_ms=processing_time
            )

        # ═══════════════════════════════════════════════════════════
        # STANDARD / DEEP MODE: Полный поиск с анализом и reflection
        # ═══════════════════════════════════════════════════════════

        # 1. Query Understanding (LLM) — Multi-layer Decomposition
        steps.append("🔍 Анализ запроса (multi-layer decomposition)...")
        understanding = await self._analyze_query(query)

        intent = understanding.get('intent', 'FACTOID')
        complexity = understanding.get('complexity', 'SIMPLE')
        analysis_type = understanding.get('analysis_type', {})
        temporal = understanding.get('temporal', {})
        fact_intent = understanding.get('fact_vs_intent', {})
        semantic = understanding.get('semantic_decomposition', {})

        steps.append(
            f"   Тип: {intent}, Сложность: {complexity}, "
            f"Действие: {semantic.get('action_status', '?')}, "
            f"Ищем: {fact_intent.get('seeking', '?')}"
        )

        # ═══════════════════════════════════════════════════════════
        # 1.5 SMART SEARCH QUERIES — формируем запросы на основе анализа
        # ═══════════════════════════════════════════════════════════
        search_queries = understanding.get("search_queries", [query])
        if not search_queries:
            search_queries = [query]
        # Всегда включаем оригинальный запрос
        if query not in search_queries:
            search_queries.insert(0, query)

        # Определяем параметры поиска на основе search_depth
        strategy = HybridSearchStrategy.ADAPTIVE
        if search_depth == "deep":
            top_k = 50
        elif search_depth == "comprehensive":
            top_k = 100
        else:
            top_k = 25

        retrieval_plan = self._build_retrieval_plan(
            query=query,
            understanding=understanding,
            search_depth=search_depth,
            filters=filters,
        )
        understanding["_retrieval_plan"] = retrieval_plan
        steps.append(
            f"🗂️ Retrieval plan: {', '.join(retrieval_plan.get('primary_keys', [])[:8])}"
        )

        # ═══════════════════════════════════════════════════════════
        # 2. TEMPORAL REASONING — поиск с учётом временных сдвигов
        # ═══════════════════════════════════════════════════════════
        temporal_context = ""

        if temporal.get("require_temporal_query") and temporal.get("actual_search_period"):
            actual_period = temporal["actual_search_period"]
            mentioned = temporal.get("mentioned_period", "")
            shift_reason = temporal.get("temporal_shift_reason", "")

            steps.append(f"🕐 Temporal Reasoning: запрос '{mentioned}' → ищем в '{actual_period}'")
            if shift_reason:
                steps.append(f"   Причина сдвига: {shift_reason}")

            # Если actual_search_period отличается от mentioned — добавляем уточняющие запросы
            if actual_period != mentioned and actual_period:
                search_queries.append(f"{semantic.get('object', query)} {actual_period}")
                steps.append(f"   Добавлен запрос: '{semantic.get('object', query)} {actual_period}'")

            # Используем graph temporal query если есть graph
            if self.hybrid_search.graph_builder and hasattr(self.hybrid_search.graph_builder, 'query_changes_between'):
                try:
                    # Определяем временные границы
                    from datetime import datetime as _dt

                    month_map = {
                        "январ": "01", "феврал": "02", "март": "03", "апрел": "04",
                        "ма": "05", "июн": "06", "июл": "07", "август": "08",
                        "сентябр": "09", "октябр": "10", "ноябр": "11", "декабр": "12",
                    }

                    period_lower = actual_period.lower()
                    year = _dt.now().year
                    month_num = None

                    for prefix, num in month_map.items():
                        if prefix in period_lower:
                            month_num = num
                            break

                    if month_num:
                        start_ts = f"{year}-{month_num}-01T00:00:00Z"
                        # Последний день месяца
                        if month_num == "12":
                            end_ts = f"{year + 1}-01-01T00:00:00Z"
                        else:
                            end_ts = f"{year}-{int(month_num) + 1:02d}-01T00:00:00Z"

                        changes = await self.hybrid_search.graph_builder.query_changes_between(
                            start=start_ts, end=end_ts
                        )

                        if changes:
                            temporal_parts = []
                            for change in changes[:10]:
                                rel_type = change.get("rel_type", "")
                                from_id = change.get("from_id", "")
                                to_id = change.get("to_id", "")
                                temporal_parts.append(f"  {from_id} —[{rel_type}]→ {to_id}")

                            temporal_context = f"\n\n=== ДАННЫЕ ЗА ПЕРИОД ({actual_period}) ===\n" + "\n".join(temporal_parts)
                            steps.append(f"   Найдено {len(changes)} изменений за период")
                except Exception as e:
                    logger.warning(f"Temporal query failed (non-critical): {e}")

        # ═══════════════════════════════════════════════════════════
        # 3. ENRICHMENT LAYERS (deep + standard для complex)
        # ═══════════════════════════════════════════════════════════
        causal_context = ""
        deep_enrichment_context = ""
        graph_traversal_context = ""
        pattern_context = ""
        analytical_context = ""

        is_deep = search_depth == "deep"
        is_complex = complexity in ("COMPLEX", "RESEARCH") or intent in ("ANALYTICAL", "CAUSAL", "PATTERN", "RECOMMENDATION", "SCENARIO", "AUDIT")

        # ─── 3.1 Каузальные цепочки (deep или causal intent) ──────
        if is_deep or analysis_type.get("requires_causal_reasoning"):
            import re
            causal_keywords = r"\b(почему|зачем|из-за\s+чего|причин[аы]|что\s+привело|что\s+мешает|блокирует)\b"
            if re.search(causal_keywords, query.lower()) or analysis_type.get("requires_causal_reasoning"):
                steps.append("🔗 Поиск каузальных цепочек...")
                try:
                    chains = await self.hybrid_search.causal_chain_search(
                        query=query, top_k=10, max_depth=3
                    )
                    if chains:
                        causal_parts = [chain["explanation"] for chain in chains[:5]]
                        causal_context = "\n\n=== КАУЗАЛЬНЫЕ ЦЕПОЧКИ ===\n" + "\n---\n".join(causal_parts)
                        steps.append(f"   Найдено {len(chains)} каузальных цепочек")
                except Exception as e:
                    logger.warning(f"Causal search failed: {e}")

        # ─── 3.2 Graph Traversal — обход связей для контекста ──────
        if is_deep or is_complex or analysis_type.get("requires_graph_traversal"):
            steps.append("🕸️ Graph Traversal: обход связей...")
            try:
                traversal_parts = await self._graph_multi_hop_traversal(query, understanding, max_hops=2)
                if traversal_parts:
                    graph_traversal_context = "\n\n=== СВЯЗИ И КОНТЕКСТ ИЗ ГРАФА ===\n" + "\n".join(traversal_parts)
                    steps.append(f"   Найдено {len(traversal_parts)} связанных контекстов")
            except Exception as e:
                logger.warning(f"Graph traversal failed: {e}")

        # ─── 3.3 Pattern Analysis — анализ паттернов ───────────────
        if is_deep or analysis_type.get("requires_pattern_analysis"):
            steps.append("📊 Анализ паттернов...")
            try:
                pattern_parts = await self._analyze_patterns(query, understanding)
                if pattern_parts:
                    pattern_context = "\n\n=== ПАТТЕРНЫ И ЗАКОНОМЕРНОСТИ ===\n" + "\n".join(pattern_parts)
                    steps.append(f"   Найдено {len(pattern_parts)} паттернов")
            except Exception as e:
                logger.warning(f"Pattern analysis failed: {e}")

        # ─── 3.4 Cross-Entity Analysis + KPI Aggregation ──────────
        if is_deep or analysis_type.get("requires_cross_entity") or analysis_type.get("requires_aggregation"):
            steps.append("🔄 Cross-Entity Analysis...")
            try:
                analytical_parts = await self._cross_entity_analysis(query, understanding)
                if analytical_parts:
                    analytical_context = "\n\n=== АНАЛИТИКА ПО СУЩНОСТЯМ ===\n" + "\n".join(analytical_parts)
                    steps.append(f"   Собрано {len(analytical_parts)} аналитических блоков")
            except Exception as e:
                logger.warning(f"Cross-entity analysis failed: {e}")

        # ─── 3.5 Обогащение снапшотами ─────────────────────────────
        if is_deep or is_complex:
            steps.append("📸 Обогащение снапшотами...")
            enrichment_parts = []
            try:
                from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
                snap_gen = get_enhanced_snapshot_generator(
                    self.hybrid_search.graph_builder, user_id=user_id)

                entities = understanding.get("entities", [])
                if isinstance(entities, list):
                    for ent in entities[:5]:
                        ent_name = ent if isinstance(ent, str) else ent.get("name", "")
                        ent_type = "" if isinstance(ent, str) else ent.get("type", "")

                        if ent_name:
                            try:
                                if ent_type in ("person", "сотрудник", "человек"):
                                    snap = await snap_gen.get_person_snapshot(ent_name)
                                    if snap:
                                        enrichment_parts.append(f"[Снапшот: {ent_name}]\n{snap.to_text(max_length=500)}")
                                elif ent_type in ("project", "проект"):
                                    snap = await snap_gen.get_project_snapshot(ent_name)
                                    if snap:
                                        enrichment_parts.append(f"[Снапшот: {ent_name}]\n{snap.to_text(max_length=500)}")
                                elif ent_type in ("product", "продукт"):
                                    snap = await snap_gen.get_product_snapshot(ent_name)
                                    if snap:
                                        enrichment_parts.append(f"[Снапшот: {ent_name}]\n{snap.to_text(max_length=500)}")
                                elif ent_type in ("department", "отдел"):
                                    snap = await snap_gen.get_department_snapshot(ent_name)
                                    if snap:
                                        enrichment_parts.append(f"[Снапшот: {ent_name}]\n{snap.to_text(max_length=500)}")
                                elif ent_type in ("team", "команда"):
                                    snap = await snap_gen.get_team_snapshot(ent_name)
                                    if snap:
                                        enrichment_parts.append(f"[Снапшот: {ent_name}]\n{snap.to_text(max_length=500)}")
                                else:
                                    # Тип не размечен — пробуем как человека (самый
                                    # частый кейс: «вклад Шитова/Белухина»). Имя
                                    # резолвится в person_id внутри снэпшот-генератора.
                                    snap = await snap_gen.get_person_snapshot(ent_name)
                                    if snap:
                                        enrichment_parts.append(f"[Снапшот: {ent_name}]\n{snap.to_text(max_length=500)}")
                            except Exception:
                                logger.debug("suppressed exception", exc_info=True)

                if enrichment_parts:
                    deep_enrichment_context = "\n\n=== СНАПШОТЫ СУЩНОСТЕЙ ===\n" + "\n---\n".join(enrichment_parts)
                    steps.append(f"   Загружено {len(enrichment_parts)} снапшотов")
            except Exception as e:
                logger.warning(f"Snapshot enrichment failed: {e}")

        # ─── 3.6 Consulting Analyzer — фреймворки уровня McKinsey ──
        consulting_context = ""
        if is_deep or intent in ("ANALYTICAL", "RECOMMENDATION", "PATTERN", "SCENARIO", "AUDIT"):
            steps.append("🎯 Consulting Analysis...")
            try:
                from backend.core.search.consulting_analyzer import get_consulting_analyzer
                analyzer = get_consulting_analyzer(
                    graph_builder=self.hybrid_search.graph_builder,
                    llm_router=self.llm
                )
                consulting_results = await analyzer.analyze(query)
                if consulting_results:
                    consulting_text = analyzer.format_results(consulting_results)
                    if consulting_text.strip():
                        consulting_context = "\n\n=== КОНСАЛТИНГОВЫЙ АНАЛИЗ ===\n" + consulting_text
                        total_findings = sum(len(r.findings) for r in consulting_results)
                        steps.append(f"   Проведено {len(consulting_results)} анализов, {total_findings} находок")
            except Exception as e:
                logger.warning(f"Consulting analysis failed: {e}")

        # ─── 3.7 Reasoning Chains — гипотезы, доказательства, опровержения ─
        reasoning_context = ""
        needs_reasoning = (
            is_deep
            or intent in ("ANALYTICAL", "CAUSAL", "RECOMMENDATION", "SCENARIO", "AUDIT")
            or analysis_type.get("requires_causal_reasoning")
        )
        if needs_reasoning and self._time_left() < 60:
            needs_reasoning = False
            steps.append("⏱ Reasoning Chains пропущены: мало времени до "
                         "дедлайна — отвечаем по собранному контексту")
            logger.warning("soft deadline: reasoning chains skipped "
                           f"({self._time_left():.0f}s left)")
        if needs_reasoning:
            steps.append("🧪 Reasoning Chains: гипотезы + доказательства...")
            try:
                from backend.core.search.reasoning_chains import get_reasoning_chain_engine
                reasoning_engine = get_reasoning_chain_engine(
                    hybrid_search=self.hybrid_search,
                    llm_router=self.llm,
                    graph_builder=self.hybrid_search.graph_builder
                )

                # Собираем уже найденный контекст как подсказку
                existing_hints = ""
                if consulting_context:
                    existing_hints += consulting_context[:1000]
                if pattern_context:
                    existing_hints += pattern_context[:500]

                reasoning_result = await reasoning_engine.reason(
                    query=query,
                    context=existing_hints or None,
                    max_hypotheses=3
                )

                if reasoning_result.hypotheses:
                    reasoning_context = reasoning_engine.format_for_context(reasoning_result)
                    supported = [h for h in reasoning_result.hypotheses if h.status == "supported"]
                    refuted = [h for h in reasoning_result.hypotheses if h.status == "refuted"]
                    steps.append(
                        f"   Гипотез: {len(reasoning_result.hypotheses)}, "
                        f"подтверждено: {len(supported)}, опровергнуто: {len(refuted)}, "
                        f"уверенность: {reasoning_result.overall_confidence:.0%}"
                    )
            except Exception as e:
                logger.warning(f"Reasoning chains failed: {e}")

        # Увеличиваем retries для deep/complex. Число раундов конфигурируемо:
        # TESSENT_DEEP_MAX_RETRIES (default 1 → до 2 раундов). RESEARCH-запрос
        # с 3 раундами (retries=2) на CPU-железе шёл 6+ минут (каждый раунд =
        # 3 подзапроса × vector+bm25+graph с удвоением top_k) — фронт отваливался
        # по таймауту раньше ответа. 2 раунда почти не теряют качества (в ответ
        # всё равно идёт ~16 источников); верни 2, если железо позволяет.
        import os as _os
        if is_deep:
            try:
                _deep_retries = int(_os.getenv("TESSENT_DEEP_MAX_RETRIES", "1"))
            except (TypeError, ValueError):
                _deep_retries = 1
            self.max_retries = max(0, _deep_retries)
        # Потолок top_k при эскалации — чтобы не раздувалось до 400 (каждый
        # раунд = 3 подзапроса × vector+bm25+graph по top_k). Default 100:
        # на 200 один RESEARCH-прогон занимал минуты на CPU.
        try:
            _deep_max_topk = int(_os.getenv("TESSENT_DEEP_MAX_TOPK", "100"))
        except (TypeError, ValueError):
            _deep_max_topk = 100

        # 4. Execution Loop (Search -> Reflect -> Retry)
        attempt = 1
        best_context = None
        best_quality = 0.0

        while attempt <= self.max_retries + 1:
            steps.append(f"🚀 Поиск (попытка {attempt}, top_k={top_k})...")
            current_collections = (
                retrieval_plan.get("primary_collections")
                if attempt == 1
                else retrieval_plan.get("fallback_collections")
            )
            current_collection_keys = (
                retrieval_plan.get("primary_keys")
                if attempt == 1
                else retrieval_plan.get("fallback_keys")
            )
            if current_collection_keys:
                steps.append(f"   Коллекции: {', '.join(current_collection_keys[:8])}")

            # 4.1 Multi-Query Hybrid Search — ищем по нескольким подзапросам
            all_results = None
            for sq in search_queries[:3]:
                sq_results = await self.hybrid_search.search(
                    query=sq,
                    strategy=strategy,
                    top_k=top_k,
                    filters=filters,
                    collections=current_collections,
                )
                if all_results is None:
                    all_results = sq_results
                else:
                    # Мержим результаты, исключая дубликаты
                    existing_ids = {r.doc_id for r in all_results.results}
                    for r in sq_results.results:
                        if r.doc_id not in existing_ids:
                            all_results.results.append(r)
                            existing_ids.add(r.doc_id)
                    all_results.total_found = len(all_results.results)

            search_results = all_results
            if len(search_queries) > 1:
                steps.append(f"   Multi-query: {len(search_queries)} запросов → {search_results.total_found} результатов")

            # 4.2 Context Assembly & Verification
            context = await self._assemble_context(search_results, query, understanding)

            # 4.3 Fact vs Intent Filtering — фильтруем результаты
            exclude_patterns = fact_intent.get("exclude_patterns", [])
            seeking = fact_intent.get("seeking", "BOTH")
            if exclude_patterns and seeking != "BOTH" and context.get("sources"):
                original_count = len(context["sources"])
                filtered_sources = []
                filtered_texts = []

                for source in context["sources"]:
                    text_lower = source.get("text", "").lower()
                    should_exclude = False
                    for pattern in exclude_patterns:
                        if pattern.lower() in text_lower:
                            should_exclude = True
                            break
                    if not should_exclude:
                        filtered_sources.append(source)
                        filtered_texts.append(f"[{source.get('type', '?')}] {source.get('title', '?')}\n{source.get('text', '')}")

                if filtered_sources:
                    context["sources"] = filtered_sources
                    context["text"] = "\n\n---\n\n".join(filtered_texts)
                    context["count"] = len(filtered_sources)

                    if original_count != len(filtered_sources):
                        steps.append(f"   🎯 Fact filter: {original_count} → {len(filtered_sources)} (исключены: {', '.join(exclude_patterns[:3])})")

            # 2.3 Reflection (Оценка качества)
            if self.reflection_enabled:
                quality_check = await self._evaluate_context_quality(
                    query, context, understanding
                )
                quality_score = quality_check.get("score", 0.0)
                steps.append(f"   Качество контекста: {quality_score}/10")

                if quality_score >= 7.0:
                    best_context = context
                    break

                # Если качество лучше предыдущего - сохраняем
                if quality_score > best_quality:
                    best_quality = quality_score
                    best_context = context

                # Если последняя попытка - выходим
                if attempt > self.max_retries:
                    break

                # Планируем следующую попытку (расширяем поиск)
                steps.append(f"   ⚠️ Недостаточно данных. Расширяем поиск ({quality_check.get('reason')})...")
                top_k = min(top_k * 2, _deep_max_topk)
                # Можно изменить стратегию, например, на семантическую если лексика не сработала
                if strategy == HybridSearchStrategy.LEXICAL_FIRST:
                    strategy = HybridSearchStrategy.SEMANTIC_FIRST

            else:
                best_context = context
                break

            attempt += 1

        best_context = best_context or {"sources": [], "text": "", "source_breakdown": {}}

        # 5. Response Synthesis
        # Объединяем ВСЕ дополнительные контексты
        enrichment_extra = ""
        if temporal_context:
            enrichment_extra += temporal_context
        if causal_context:
            enrichment_extra += causal_context
        if graph_traversal_context:
            enrichment_extra += graph_traversal_context
        if pattern_context:
            enrichment_extra += pattern_context
        if analytical_context:
            enrichment_extra += analytical_context
        if deep_enrichment_context:
            enrichment_extra += deep_enrichment_context
        if consulting_context:
            enrichment_extra += consulting_context
        if reasoning_context:
            enrichment_extra += reasoning_context

        if enrichment_extra:
            existing_extra = getattr(self, '_extra_context', '') or ''
            self._extra_context = existing_extra + enrichment_extra

        steps.append("Генерация ответа...")
        final_answer = await self._synthesize_answer(
            query, best_context, understanding, search_depth=search_depth, user_id=user_id
        )

        # 4. Final Metadata
        processing_time = int((datetime.now() - start_time).total_seconds() * 1000)
        result_confidence = best_quality / 10.0 if self.reflection_enabled else 0.8
        evidence_contract = self._build_evidence_contract(
            context=best_context,
            reasoning_steps=steps,
            confidence=result_confidence,
            search_depth=search_depth,
        )
        graph_trace = await build_graph_trace(
            graph_builder=self.hybrid_search.graph_builder,
            sources=best_context.get("sources", []),
            reasoning_steps=evidence_contract.get("inferred_path", []),
            title="Retrieval provenance trace",
        )
        if graph_trace:
            evidence_contract["graph_trace"] = graph_trace
        steps.append(
            f"🧾 Evidence contract: direct={len(evidence_contract.get('direct_evidence', []))}, "
            f"ambiguities={len(evidence_contract.get('ambiguities', []))}"
        )

        return SearchResult(
            answer=final_answer,
            sources=best_context.get("sources", []),
            confidence=result_confidence,
            metadata={
                "search_depth": search_depth,
                "attempts": attempt,
                "strategy_used": search_results.strategy_used,
                "total_found": search_results.total_found,
                "retrieval_plan": {
                    "primary": retrieval_plan.get("primary_keys", []),
                    "fallback": retrieval_plan.get("fallback_keys", []),
                },
                "source_breakdown": best_context.get("source_breakdown", {}),
                "evidence_contract": evidence_contract,
                "graph_trace": graph_trace,
                "answer_mode": getattr(self, "_answer_mode", None),
            },
            reasoning_steps=steps,
            processing_time_ms=processing_time
        )

    async def _analyze_query(self, query: str) -> Dict[str, Any]:
        """
        Multi-layer Query Decomposition.

        Глубокий анализ запроса на нескольких уровнях:
        1. Семантическая декомпозиция: объект, действие, метрика, контекст
        2. Временное рассуждение: когда искать, с учётом сдвигов (итоги мая → ищи в июне)
        3. Fact vs Intent: отделяем свершившиеся факты от планов
        4. Тип анализа: какой reasoning нужен (поиск, агрегация, паттерны, причинность)
        """
        system_prompt = """Ты эксперт по глубокому анализу поисковых запросов в корпоративной базе знаний.
Твоя задача — ПОЛНОСТЬЮ РАЗОБРАТЬ запрос на составляющие для максимально точного поиска.

Определи следующее и верни JSON:

{
    "intent": "FACTOID | ANALYTICAL | SUMMARY | COMPARISON | AGGREGATION | CAUSAL | PATTERN | RECOMMENDATION | SCENARIO | AUDIT",
    "complexity": "SIMPLE | MEDIUM | COMPLEX | RESEARCH",

    "semantic_decomposition": {
        "object": "что именно ищем (например: 'договоры с клиентами', не просто 'договоры')",
        "object_qualifiers": ["уточнения объекта: с клиентами (не подрядчиками), реальные (не планы)"],
        "action": "действие/состояние (подписали = свершившееся, планируем = намерение)",
        "action_status": "COMPLETED | PLANNED | IN_PROGRESS | UNKNOWN",
        "metric": "что измеряем: количество, сумма, процент, null если не метрика",
        "subject": "кто выполняет (мы = компания, конкретный человек, отдел)"
    },

    "temporal": {
        "mentioned_period": "период упомянутый в запросе (в мае, за Q1, на прошлой неделе) или null",
        "actual_search_period": "реальный период поиска с учётом логики. Если спрашивают итоги мая — искать в данных июня, т.к. итоги обсуждаются ПОСЛЕ периода. Если null — искать везде",
        "temporal_shift_reason": "почему сдвигаем период или null",
        "require_temporal_query": true
    },

    "fact_vs_intent": {
        "seeking": "FACT | INTENT | BOTH",
        "explanation": "Если спрашивают 'сколько подписали' — ищем ФАКТ, а не план. Если 'сколько планируем' — ищем INTENT",
        "exclude_patterns": ["паттерны которые нужно ИСКЛЮЧИТЬ из результатов: 'планируем', 'хотим', 'если' для FACT-запросов"]
    },

    "analysis_type": {
        "requires_search": true,
        "requires_aggregation": false,
        "requires_pattern_analysis": false,
        "requires_causal_reasoning": false,
        "requires_cross_entity": false,
        "requires_graph_traversal": false,
        "reasoning_description": "краткое описание что нужно сделать для ответа"
    },

    "entities": [
        {"name": "имя сущности", "type": "person | project | product | department | team | kpi | task | decision"}
    ],

    "search_queries": ["точные подзапросы для поиска, переформулированные для лучшего нахождения"]
}

ВАЖНО:
- "сколько договоров подписали в мае" → object="договоры с клиентами", action_status="COMPLETED", metric="количество", actual_search_period="июнь" (итоги обсуждают в следующем месяце)
- "каких не хватает сотрудников" → requires_pattern_analysis=true, requires_cross_entity=true (нужно анализировать задачи, проблемы, узкие места по отделам)
- "что можно оптимизировать с помощью ИИ" → intent="RECOMMENDATION", requires_pattern_analysis=true, requires_causal_reasoning=true"""

        try:
            result = await self.llm.generate_json(
                prompt=f"Запрос пользователя: {query}",
                system_prompt=system_prompt
            )

            # Гарантируем минимальные поля
            result.setdefault("intent", "FACTOID")
            result.setdefault("complexity", "SIMPLE")
            result.setdefault("semantic_decomposition", {})
            result.setdefault("temporal", {})
            result.setdefault("fact_vs_intent", {"seeking": "BOTH", "exclude_patterns": []})
            result.setdefault("analysis_type", {"requires_search": True})
            result.setdefault("entities", [])
            result.setdefault("search_queries", [query])

            logger.info(
                f"🧠 Query analysis: intent={result['intent']}, "
                f"complexity={result['complexity']}, "
                f"action_status={result.get('semantic_decomposition', {}).get('action_status', '?')}, "
                f"temporal_shift={result.get('temporal', {}).get('actual_search_period', 'none')}, "
                f"fact_seeking={result.get('fact_vs_intent', {}).get('seeking', '?')}, "
                f"needs_patterns={result.get('analysis_type', {}).get('requires_pattern_analysis', False)}"
            )

            return result
        except Exception as e:
            logger.error(f"Error analyzing query: {e}")
            return {
                "intent": "FACTOID",
                "complexity": "SIMPLE",
                "entities": [],
                "search_queries": [query],
                "semantic_decomposition": {},
                "temporal": {},
                "fact_vs_intent": {"seeking": "BOTH", "exclude_patterns": []},
                "analysis_type": {"requires_search": True},
            }

    async def _get_meeting_title(self, meeting_id: str) -> str:
        """Получить название встречи по ID (с кэшированием)"""
        global _meeting_titles_cache

        if not meeting_id:
            return ""

        # Проверяем кэш
        if meeting_id in _meeting_titles_cache:
            return _meeting_titles_cache[meeting_id]

        # Загружаем из базы данных
        try:
            from ...db.supabase_client import get_supabase_client
            supabase = get_supabase_client()
            meeting = await supabase.get_meeting_details(meeting_id, include_transcript=False)
            if meeting:
                title = meeting.get('title', '')
                _meeting_titles_cache[meeting_id] = title
                return title
        except Exception as e:
            logger.debug(f"Could not fetch meeting title for {meeting_id}: {e}")

        return ""

    async def _get_meeting_titles_batch(self, meeting_ids: list) -> None:
        """Загрузить названия встреч ОДНИМ запросом (PostgREST id=in.(...)).

        Раньше на этапе сборки контекста дёргали Supabase ПО ОДНОМУ разу на
        встречу через asyncio.gather — 60+ HTTP round-trip'ов к Supabase Cloud
        ≈ 15-20с, и это повторялось на КАЖДОМ раунде DEEP (эскалация top_k).
        Батчим: 1 запрос на до 100 id. Некэшируемые кладём пустыми, чтобы не
        дёргать повторно.
        """
        global _meeting_titles_cache
        ids = [m for m in meeting_ids if m and m not in _meeting_titles_cache]
        # meetings.id — uuid: не-UUID (docsource_…, chat:…) в in.(...) ронял
        # 400 «invalid input syntax for type uuid» ВЕСЬ чанк — терялись
        # заголовки всех 100 встреч из-за одного чужеродного id. Такие id —
        # не встречи, кэшируем пустыми ниже и в запрос не кладём.
        import uuid as _uuid

        def _is_uuid(v: str) -> bool:
            try:
                _uuid.UUID(str(v))
                return True
            except Exception:
                return False

        non_uuid = [m for m in ids if not _is_uuid(m)]
        ids = [m for m in ids if _is_uuid(m)]
        if non_uuid:
            logger.debug("meeting titles: %d не-UUID id пропущено (%s…)",
                         len(non_uuid), str(non_uuid[0])[:40])
        if not ids:
            for m in non_uuid:
                _meeting_titles_cache.setdefault(m, "")
            return
        try:
            from ...db.supabase_client import get_supabase_client
            supabase = get_supabase_client()
            CHUNK = 100
            for i in range(0, len(ids), CHUNK):
                chunk = ids[i:i + CHUNK]
                rows = await supabase._request(
                    "GET", "/rest/v1/meetings",
                    params={"id": f"in.({','.join(chunk)})", "select": "id,title"},
                )
                for row in (rows or []):
                    _meeting_titles_cache[row.get("id", "")] = row.get("title", "") or ""
        except Exception as e:
            logger.debug(f"batch meeting titles fetch failed: {e}")
        # Всё, что не вернулось (нет в Supabase / ошибка) — кэшируем пустым,
        # чтобы следующий раунд не пытался тянуть их снова.
        for m in ids + non_uuid:
            _meeting_titles_cache.setdefault(m, "")

    async def deep_dive_search(
        self,
        query: str,
        user_id: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        extra_context: Optional[str] = None,
        model_tier: str = "standard",
        max_meetings: int = 4,
    ) -> "SearchResult":
        """Глубокий проход: читаем ПОЛНЫЕ транскрипты самых релевантных встреч.

        Обычный search() работает по сниппетам (быстро/дёшево). Здесь — для
        вопросов-реконструкций, где сниппетов недостаточно. Порядок:
          1) обычный поиск → ранжированные кандидаты-встречи (meeting_id+score);
          2) LLM выбирает 1..max_meetings встреч, которые надо прочитать целиком
             («система сначала думает, что прочесть» — масштабируется на тысячи
             встреч, т.к. полнотекстово читаем только отобранные);
          3) грузим полные транскрипты выбранных встреч;
          4) синтез ответа по полным текстам.
        """
        start_time = datetime.now()
        steps: List[str] = ["🔬 Deep dive: подбираю встречи для полного чтения"]

        # 1) Базовый поиск даёт ранжированных кандидатов с meeting_id.
        base = await self.search(
            query, user_id=user_id, filters=filters,
            search_depth="standard", extra_context=extra_context,
            model_tier=model_tier,
        )

        # 2) Агрегируем встречи: meeting_id -> {title, score, snippet}.
        meetings: Dict[str, Dict[str, Any]] = {}
        for src in (base.sources or []):
            mid = src.get("meeting_id")
            if not mid:
                continue
            score = float(src.get("score") or 0.0)
            snippet = (src.get("text") or "").replace("\n", " ")[:300]
            cur = meetings.get(mid)
            if cur is None:
                meetings[mid] = {
                    "title": src.get("meeting_title") or src.get("title") or mid,
                    "score": score, "snippet": snippet,
                }
            else:
                cur["score"] = max(cur["score"], score)
                if not cur.get("snippet") and snippet:
                    cur["snippet"] = snippet

        if not meetings:
            base.metadata = {**(base.metadata or {}), "deep_dive": True, "meetings_read": []}
            base.reasoning_steps = steps + (base.reasoning_steps or []) + [
                "⚠️ Среди источников нет встреч с транскриптами — глубокое чтение невозможно"]
            return base

        ranked = sorted(meetings.items(), key=lambda kv: kv[1]["score"], reverse=True)[:12]

        # 3) LLM выбирает, какие встречи читать целиком.
        selected_ids = await self._select_meetings_for_deep_read(query, ranked, max_meetings)
        if not selected_ids:
            selected_ids = [mid for mid, _ in ranked[:max_meetings]]
        steps.append(f"📖 Выбрано встреч для полного чтения: {len(selected_ids)}")

        # 4) Грузим полные транскрипты выбранных встреч.
        from backend.db.supabase_client import get_supabase_client
        client = get_supabase_client()
        transcripts: List[Any] = []  # (title, meeting_id, text)
        for mid in selected_ids:
            try:
                text = await client.get_meeting_transcript(mid)
            except Exception as e:
                logger.warning("deep_dive: transcript fetch failed for %s: %s", mid, e)
                text = None
            if text and len(text.strip()) > 50:
                transcripts.append((meetings.get(mid, {}).get("title", mid), mid, text))

        if not transcripts:
            base.metadata = {**(base.metadata or {}), "deep_dive": True, "meetings_read": []}
            base.reasoning_steps = steps + (base.reasoning_steps or []) + [
                "⚠️ Транскрипты выбранных встреч пусты"]
            return base

        # 5) Синтез из полных транскриптов (бюджет на встречу).
        per_meeting = max(4000, int(DEEP_DIVE_CONTEXT_BUDGET / len(transcripts)))
        blocks = [f"=== ВСТРЕЧА: {title} ===\n{text[:per_meeting]}"
                  for (title, _mid, text) in transcripts]
        deep_context = "\n\n".join(blocks)

        system_prompt = (
            "Ты — аналитик. Тебе даны ПОЛНЫЕ транскрипты встреч. Восстанови по ним "
            "точную картину: кто, что и когда говорил/решал. Опирайся ТОЛЬКО на "
            "транскрипты, цитируй дословно где уместно и указывай встречу-источник. "
            "Если данных нет — честно скажи, не выдумывай."
        )
        user_prompt = (
            f"Вопрос: {query}\n\n"
            f"=== ПОЛНЫЕ ТРАНСКРИПТЫ ({len(transcripts)} встреч) ===\n{deep_context}\n\n"
            "Дай подробный, точный ответ со ссылками на встречи."
        )
        # deep dive по ПОЛНЫМ транскриптам — всегда тяжёлый анализ: система
        # специально прочитала целые встречи, отдавать их lite-модели — терять
        # смысл шага. Уровень premium (модель тенанта + Google-страховка).
        tier_enum = ModelTier.PREMIUM
        answer = await self._routed_generate(
            user_id, "search_deep_synthesis",
            user_prompt=user_prompt, system_prompt=system_prompt,
            temperature=0.2, max_tokens=4000, tier_enum=tier_enum,
        )

        processing_time = int((datetime.now() - start_time).total_seconds() * 1000)
        steps.append(f"✅ Глубокий ответ собран из {len(transcripts)} полных транскриптов")
        sources_out = [
            {"meeting_id": mid, "meeting_title": title,
             "type": "full_transcript", "chars": len(text)}
            for (title, mid, text) in transcripts
        ]
        return SearchResult(
            answer=answer,
            sources=sources_out,
            confidence=min(0.95, float(base.confidence or 0.5) + 0.2),
            metadata={**(base.metadata or {}), "deep_dive": True,
                      "meetings_read": [m for _t, m, _x in transcripts]},
            reasoning_steps=steps,
            processing_time_ms=processing_time,
        )

    async def _select_meetings_for_deep_read(
        self, query: str, ranked: List[Any], max_meetings: int
    ) -> List[str]:
        """LLM выбирает meeting_id'ы для полного чтения из кандидатов."""
        import re
        try:
            lines = []
            for i, (mid, info) in enumerate(ranked, 1):
                snip = (info.get("snippet") or "")[:200]
                lines.append(f"{i}. id={mid} | {info.get('title', '')} | {snip}")
            listing = "\n".join(lines)
            prompt = (
                f"Вопрос пользователя: {query}\n\n"
                f"Кандидаты-встречи (id, название, фрагмент):\n{listing}\n\n"
                f"Выбери от 1 до {max_meetings} встреч, чьи ПОЛНЫЕ транскрипты надо "
                "прочитать, чтобы максимально точно ответить. Верни ТОЛЬКО id "
                "выбранных встреч через запятую, без пояснений."
            )
            raw = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты выбираешь источники для глубокого чтения. Отвечай только списком id через запятую.",
                temperature=0.0, max_tokens=200, model_tier=ModelTier.STANDARD,
            )
            valid = {mid for mid, _ in ranked}
            chosen: List[str] = []
            for token in re.split(r"[,\s]+", (raw or "").strip()):
                t = token.strip().strip(".")
                if t in valid and t not in chosen:
                    chosen.append(t)
                if len(chosen) >= max_meetings:
                    break
            return chosen
        except Exception as e:
            logger.warning("deep_dive: meeting selection failed: %s", e)
            return []

    async def _assemble_context(
        self,
        search_results: Any,
        query: str,
        understanding: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Сборка контекста из результатов поиска.
        Включает проверку противоречий (Verification).
        """
        # Преобразуем результаты в единый формат
        candidate_sources = []

        logger.info(f"🔧 Assembling context from {len(search_results.results)} results")

        # Собираем уникальные meeting_id для batch-загрузки названий
        meeting_ids_to_fetch = set()
        for res in search_results.results:
            metadata = res.metadata or {}
            mid = metadata.get('meeting_id', metadata.get('source_meeting_id', ''))
            if mid and mid not in _meeting_titles_cache:
                meeting_ids_to_fetch.add(mid)

        # Загружаем названия встреч параллельно
        if meeting_ids_to_fetch:
            logger.info(f"📋 Fetching titles for {len(meeting_ids_to_fetch)} meetings (batch)")
            await self._get_meeting_titles_batch(list(meeting_ids_to_fetch))

        for i, res in enumerate(search_results.results):
            # Извлекаем тип и текст
            metadata = res.metadata or {}
            doc_type = metadata.get('type', metadata.get('doc_type', 'Unknown'))

            # Пробуем получить текст из разных источников (приоритет)
            text = None

            # 1. Прямо из результата
            if res.text and len(str(res.text).strip()) > 10:
                text = res.text

            # 2. Из metadata/payload - разные поля
            if not text:
                text_candidates = [
                    metadata.get('text'),
                    metadata.get('content'),
                    metadata.get('text_content'),  # Наше новое поле
                    metadata.get('summary'),
                    metadata.get('description'),
                    metadata.get('transcription_text'),
                    metadata.get('transcript'),
                ]
                for candidate in text_candidates:
                    if candidate and len(str(candidate).strip()) > 10:
                        text = str(candidate)
                        break

            # 3. Для узлов графа - собираем из свойств
            if not text and doc_type in ['Person', 'Task', 'Decision', 'Entity', 'KPI', 'Meeting']:
                parts = []
                name = metadata.get('name', metadata.get('title', ''))
                if name:
                    parts.append(f"Название: {name}")
                desc = metadata.get('description', metadata.get('details', ''))
                if desc:
                    parts.append(f"Описание: {desc}")
                role = metadata.get('role', '')
                if role:
                    parts.append(f"Роль: {role}")
                status = metadata.get('status', '')
                if status:
                    parts.append(f"Статус: {status}")
                assignee = metadata.get('assignee', '')
                if assignee:
                    parts.append(f"Исполнитель: {assignee}")

                if parts:
                    text = ". ".join(parts)

            # Формируем заголовок источника - пробуем разные поля
            title = (
                metadata.get('title') or
                metadata.get('name') or
                metadata.get('document_title') or
                metadata.get('meeting_title') or
                metadata.get('assignee') or  # Для задач
                (text[:50] + '...' if text and len(text) > 50 else text) or
                f'Документ {i+1}'
            )
            meeting_id = metadata.get('meeting_id', metadata.get('source_meeting_id', ''))
            # Получаем название встречи - пробуем разные поля, потом из кэша
            meeting_title = (
                metadata.get('meeting_title') or
                metadata.get('source_meeting_title') or
                metadata.get('meeting_name') or
                _meeting_titles_cache.get(meeting_id, '')  # Из кэша, загруженного выше
            )

            # Temporal resolver (за флагом answer_mode_router — self._answer_mode
            # выставлен только при включённом флаге). Резолвим относительные даты
            # («вчера/на прошлой неделе») против даты источника В КОДЕ и аннотируем
            # текст — модель не путает «дата − смещение» (замер: temporal 0.20→0.60,
            # docs/ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md). Недеструктивно: при отсутствии
            # выражения/даты текст байт-в-байт. Best-effort, не роняет сборку.
            if text and getattr(self, "_answer_mode", None):
                try:
                    from backend.core.search.temporal_resolver import (
                        anchor_from_metadata,
                    )
                    from backend.core.search.temporal_resolver import (
                        annotate as _annotate_dates,
                    )
                    _anchor = anchor_from_metadata(metadata)
                    if _anchor is not None:
                        text = _annotate_dates(text, _anchor)
                except Exception:
                    logger.debug("temporal resolver skipped", exc_info=True)

            if text and len(text.strip()) > 10:
                source_header = f"[{doc_type}] {title}"
                # Показываем название встречи вместо ID
                if meeting_title:
                    source_header += f" (встреча: {meeting_title})"
                elif meeting_id:
                    # Fallback на короткий ID только если нет названия
                    source_header += f" (ID: {meeting_id[:8]})"

                candidate_sources.append({
                    "id": res.doc_id,
                    # Раньше резали до 500 символов → модель видела лишь огрызки
                    # транскриптов (25–145K символов) и честно отвечала
                    # «фрагментарно, без полных протоколов». Поднято до 2000:
                    # источник содержательнее. Общий бюджет всё равно держит
                    # Context Window Guard (max_context_chars по tier ниже).
                    "text": text[:_PER_SOURCE_TEXT_LIMIT] if len(text) > _PER_SOURCE_TEXT_LIMIT else text,
                    "score": res.score,
                    "metadata": metadata,
                    "title": title,
                    "type": doc_type,
                    "meeting_id": meeting_id,
                    "meeting_title": meeting_title,
                    "retrieval_sources": list(getattr(res, "sources", []) or metadata.get("retrieval_sources", []) or []),
                    "vector_score": getattr(res, "vector_score", metadata.get("vector_score")),
                    "bm25_score": getattr(res, "bm25_score", metadata.get("bm25_score")),
                    "graph_score": getattr(res, "graph_score", metadata.get("graph_score")),
                    "_context_block": f"{source_header}\n{text}",
                })
            else:
                # Логируем только если это не связь (rel_)
                if not res.doc_id.startswith('rel_'):
                    logger.warning(f"⚠️ Empty text for result {i}: {res.doc_id}")

        # НОВОЕ: Учитываем веса узлов для приоритизации
        try:
            from backend.core.config import flags
            if flags.enable_graph_weights and candidate_sources:
                candidate_sources = self._apply_weight_scoring(candidate_sources)
                logger.info(f"⚖️ Applied weight scoring to {len(candidate_sources)} sources")
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)

        prioritized_sources = self._prioritize_context_sources(candidate_sources, understanding)
        combined_text = "\n\n---\n\n".join(
            item.get("_context_block", "")
            for item in prioritized_sources
            if item.get("_context_block")
        )
        source_breakdown: Dict[str, int] = {}
        for item in prioritized_sources:
            normalized_type = item.get("_normalized_type") or self._normalize_source_type(item)
            source_breakdown[normalized_type] = source_breakdown.get(normalized_type, 0) + 1

        sources = [
            {k: v for k, v in item.items() if not k.startswith("_")}
            for item in prioritized_sources
        ]
        logger.info(
            f"📋 Assembled {len(sources)} prioritized sources, total text: {len(combined_text)} chars, "
            f"mix={source_breakdown}"
        )

        # Verification: Проверка противоречий для числовых/фактических запросов
        if self.verification_enabled and understanding.get("intent") == "FACTOID":
            # Простая эвристика или LLM вызов для проверки
            pass

        return {
            "text": combined_text,
            "sources": sources,
            "count": len(sources),
            "source_breakdown": source_breakdown,
        }

    def _resolve_collection_name(self, collection_key: str) -> Optional[str]:
        """Получить реальное имя коллекции vector indexer по логическому ключу."""
        if collection_key == "documents":
            return "tessent_documents"
        if collection_key == "chunks":
            return "tessent_chunks"

        vector_indexer = getattr(self.hybrid_search, "vector_indexer", None)
        if not vector_indexer:
            return None

        collections = getattr(vector_indexer, "collections", {}) or {}
        return collections.get(collection_key)

    def _build_retrieval_plan(
        self,
        query: str,
        understanding: Dict[str, Any],
        search_depth: str,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Выбирает first-pass и fallback коллекции под тип вопроса.

        Идея простая: сначала ищем по самым плотным memory units, а уже потом
        расширяемся до сырых текстов и документов.
        """
        intent = str(understanding.get("intent", "FACTOID")).upper()
        complexity = str(understanding.get("complexity", "SIMPLE")).upper()
        analysis_type = understanding.get("analysis_type", {}) or {}
        semantic = understanding.get("semantic_decomposition", {}) or {}
        query_lower = query.lower()

        primary_keys: List[str] = []
        fallback_keys: List[str] = []

        def add_unique(target: List[str], keys: List[str]) -> None:
            for key in keys:
                if key not in target:
                    target.append(key)

        is_deep_query = (
            search_depth in ("deep", "comprehensive")
            or complexity in ("COMPLEX", "RESEARCH")
            or intent in {"CAUSAL", "PATTERN", "RECOMMENDATION", "SCENARIO", "AUDIT"}
            or analysis_type.get("requires_causal_reasoning")
            or analysis_type.get("requires_pattern_analysis")
            or analysis_type.get("requires_graph_traversal")
        )
        is_overview_query = (
            "обзор" in query_lower
            or "summary" in query_lower
            or semantic.get("action_status") in {"overview", "summary"}
        )

        if search_depth == "quick":
            add_unique(primary_keys, ["propositions", "snapshots", "kpis", "decisions", "tasks", "entities"])
        elif is_deep_query:
            add_unique(primary_keys, ["snapshots", "topics", "propositions", "decisions", "tasks", "kpis", "entities", "reports", "meetings"])
        elif intent == "AGGREGATION":
            add_unique(primary_keys, ["snapshots", "kpis", "propositions", "topics", "decisions", "tasks"])
        elif is_overview_query:
            add_unique(primary_keys, ["snapshots", "topics", "reports", "meetings", "propositions"])
        else:
            add_unique(primary_keys, ["snapshots", "propositions", "topics", "decisions", "tasks", "kpis", "entities", "meetings"])

        if filters and filters.get("document_id"):
            add_unique(primary_keys, ["documents", "chunks"])

        fallback_keys = list(primary_keys)
        add_unique(fallback_keys, ["reports", "meetings", "documents", "chunks"])

        primary_collections = [
            collection
            for key in primary_keys
            if (collection := self._resolve_collection_name(key))
        ]
        fallback_collections = [
            collection
            for key in fallback_keys
            if (collection := self._resolve_collection_name(key))
        ]

        if search_depth == "quick":
            source_priority = {
                "proposition": 0,
                "snapshot": 1,
                "kpi": 1,
                "decision": 2,
                "task": 2,
                "entity": 3,
                "topic": 4,
                "meeting": 5,
                "report": 5,
                "document": 6,
                "raw_chunk": 7,
                "unknown": 8,
            }
            max_per_type = {
                "proposition": 4,
                "snapshot": 2,
                "kpi": 2,
                "decision": 2,
                "task": 2,
                "entity": 2,
                "topic": 1,
                "meeting": 1,
                "document": 1,
                "raw_chunk": 1,
                "default": 1,
            }
            max_total_sources = 8
        elif is_deep_query:
            source_priority = {
                "snapshot": 0,
                "topic": 1,
                "proposition": 1,
                "decision": 2,
                "task": 2,
                "kpi": 2,
                "report": 2,
                "entity": 3,
                "meeting": 4,
                "document": 5,
                "raw_chunk": 6,
                "unknown": 7,
            }
            max_per_type = {
                "snapshot": 3,
                "topic": 4,
                "proposition": 6,
                "decision": 3,
                "task": 3,
                "kpi": 3,
                "report": 2,
                "entity": 2,
                "meeting": 2,
                "document": 2,
                "raw_chunk": 2,
                "default": 2,
            }
            max_total_sources = 16
        else:
            source_priority = {
                "snapshot": 0,
                "proposition": 1,
                "topic": 2,
                "decision": 2,
                "task": 2,
                "kpi": 2,
                "entity": 3,
                "meeting": 4,
                "report": 4,
                "document": 5,
                "raw_chunk": 6,
                "unknown": 7,
            }
            max_per_type = {
                "snapshot": 3,
                "proposition": 5,
                "topic": 3,
                "decision": 3,
                "task": 3,
                "kpi": 2,
                "entity": 2,
                "meeting": 2,
                "report": 2,
                "document": 2,
                "raw_chunk": 2,
                "default": 2,
            }
            max_total_sources = 12

        return {
            "primary_keys": primary_keys,
            "fallback_keys": fallback_keys,
            "primary_collections": primary_collections,
            "fallback_collections": fallback_collections,
            "source_priority": source_priority,
            "max_per_type": max_per_type,
            "max_total_sources": max_total_sources,
        }

    def _normalize_source_type(self, source: Dict[str, Any]) -> str:
        """Свести типы источников к нескольким retrieval-категориям."""
        raw_type = str(source.get("type") or source.get("metadata", {}).get("type", "")).lower()
        collection = str(source.get("metadata", {}).get("collection", "")).lower()

        if "snapshot" in raw_type or "snapshots" in collection:
            return "snapshot"
        if raw_type == "topic" or "topics" in collection:
            return "topic"
        if raw_type == "proposition" or "propositions" in collection:
            return "proposition"
        if raw_type == "decision":
            return "decision"
        if raw_type == "task":
            return "task"
        if raw_type == "kpi":
            return "kpi"
        if raw_type == "entity":
            return "entity"
        if raw_type == "meeting":
            return "meeting"
        if raw_type == "report" or "reports" in collection:
            return "report"
        if raw_type == "document":
            return "document"
        if raw_type in {"document_chunk", "transcript_chunk", "chunk"} or "chunks" in collection:
            return "raw_chunk"
        return "unknown"

    def _prioritize_context_sources(
        self,
        sources: List[Dict[str, Any]],
        understanding: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Ограничивает шум и поднимает структурные memory units выше raw evidence."""
        if not sources:
            return []

        retrieval_plan = understanding.get("_retrieval_plan", {}) or {}
        source_priority = retrieval_plan.get("source_priority", {})
        max_per_type = retrieval_plan.get("max_per_type", {})
        max_total_sources = retrieval_plan.get("max_total_sources", len(sources))

        for source in sources:
            normalized_type = self._normalize_source_type(source)
            source["_normalized_type"] = normalized_type
            source["evidence_role"] = self._determine_evidence_role(normalized_type, source)

        sorted_sources = sorted(
            sources,
            key=lambda item: (
                source_priority.get(item.get("_normalized_type", "unknown"), source_priority.get("unknown", 99)),
                -(item.get("adjusted_score", item.get("score", 0.0))),
            ),
        )

        selected: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {}
        seen_ids = set()

        for source in sorted_sources:
            source_id = source.get("id")
            if source_id in seen_ids:
                continue

            normalized_type = source.get("_normalized_type", "unknown")
            current_count = counts.get(normalized_type, 0)
            type_limit = max_per_type.get(normalized_type, max_per_type.get("default", max_total_sources))
            if current_count >= type_limit:
                continue

            selected.append(source)
            counts[normalized_type] = current_count + 1
            seen_ids.add(source_id)

            if len(selected) >= max_total_sources:
                break

        return selected if selected else sorted_sources[:max_total_sources]

    def _determine_evidence_role(self, normalized_type: str, source: Dict[str, Any]) -> str:
        """Классифицировать источник как direct/supporting evidence."""
        if normalized_type in {"raw_chunk", "document", "unknown"}:
            return "supporting"

        score = float(source.get("adjusted_score", source.get("score", 0.0)) or 0.0)
        if normalized_type in {"snapshot", "proposition", "decision", "task", "kpi", "entity", "meeting", "topic", "report"}:
            return "direct" if score >= 0.15 else "supporting"
        return "supporting"

    def _build_evidence_contract(
        self,
        context: Dict[str, Any],
        reasoning_steps: List[str],
        confidence: float,
        search_depth: str,
    ) -> Dict[str, Any]:
        """Собрать формальный evidence contract без лишнего шума."""
        sources = context.get("sources", []) or []
        breakdown = context.get("source_breakdown", {}) or {}

        direct_evidence = []
        supporting_evidence = []
        for source in sources:
            source_metadata = source.get("metadata", {}) or {}
            entry = {
                "id": source.get("id", ""),
                "type": source.get("type", ""),
                "normalized_type": source.get("_normalized_type") or self._normalize_source_type(source),
                "title": source.get("title") or source.get("name") or source.get("meeting_title") or "",
                "score": source.get("adjusted_score", source.get("score", 0.0)),
                "snippet": str(source.get("text", ""))[:240] if source.get("text") else "",
                "meeting_id": source.get("meeting_id", "") or source_metadata.get("meeting_id", ""),
                "meeting_title": source.get("meeting_title", "") or source_metadata.get("meeting_title", ""),
                "document_id": source.get("document_id", "") or source_metadata.get("document_id", ""),
                "entity_id": source.get("entity_id", "") or source_metadata.get("entity_id", "") or source.get("id", ""),
                "snapshot_type": source.get("snapshot_type", "") or source_metadata.get("snapshot_type", ""),
                "evidence_role": source.get("evidence_role", "supporting"),
                "retrieval_stage": source.get("retrieval_stage", "") or source_metadata.get("retrieval_stage", ""),
                "retrieval_sources": source.get("retrieval_sources", []) or source_metadata.get("retrieval_sources", []),
            }
            if entry["evidence_role"] == "direct" and len(direct_evidence) < 4:
                direct_evidence.append(entry)
            elif len(supporting_evidence) < 3:
                supporting_evidence.append(entry)

        if not direct_evidence:
            direct_evidence = supporting_evidence[:3]

        structured_types = {"snapshot", "proposition", "decision", "task", "kpi", "entity", "meeting", "topic", "report"}
        structured_source_count = sum(
            count for source_type, count in breakdown.items()
            if source_type in structured_types
        )

        ambiguities: List[str] = []
        if not structured_source_count:
            ambiguities.append("Структурные memory units не найдены, ответ опирается в основном на raw/document evidence.")
        if len(sources) < 2:
            ambiguities.append("Найдено меньше двух независимых источников, поэтому вывод может быть неполным.")
        if breakdown.get("raw_chunk", 0) > structured_source_count:
            ambiguities.append("Сырые чанки доминируют над структурной памятью, значит часть ответа основана на менее стабильном контексте.")
        if confidence < 0.6:
            ambiguities.append("Итоговая уверенность ниже целевого production-уровня.")

        inferred_path = []
        inference_keywords = (
            "Retrieval plan",
            "Temporal",
            "Graph Traversal",
            "Анализ паттернов",
            "Cross-Entity",
            "Fact filter",
            "контекст",
            "поиск",
            "поиска",
            "reflection",
            "retry",
        )
        for step in reasoning_steps:
            step_text = (step or "").strip()
            if not step_text:
                continue
            if any(keyword.lower() in step_text.lower() for keyword in inference_keywords):
                inferred_path.append(step_text)
            if len(inferred_path) >= 6:
                break
        if not inferred_path:
            inferred_path = [step.strip() for step in reasoning_steps[:4] if step.strip()]

        confidence_label = "high" if confidence >= 0.8 else "medium" if confidence >= 0.6 else "low"
        return {
            "direct_evidence": direct_evidence,
            "supporting_evidence": supporting_evidence[:3],
            "inferred_path": inferred_path,
            "ambiguities": ambiguities,
            "confidence_envelope": {
                "score": round(confidence, 3),
                "label": confidence_label,
                "search_depth": search_depth,
                "source_count": len(sources),
                "structured_source_count": structured_source_count,
                "source_breakdown": breakdown,
            },
        }

    async def _evaluate_context_quality(
        self,
        query: str,
        context: Dict[str, Any],
        understanding: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Reflection: Оценка достаточности контекста для ответа.
        """
        # Если контекст пустой - сразу 0
        if not context["sources"]:
            return {"score": 0.0, "reason": "No data found"}

        system_prompt = """Оцени по шкале 0-10, достаточно ли предоставленного контекста для ПОЛНОГО и ТОЧНОГО ответа на вопрос пользователя.
Если оценка ниже 7, укажи краткую причину (чего не хватает).
Верни JSON: {"score": float, "reason": str}"""

        user_prompt = f"Вопрос: {query}\n\nКонтекст:\n{context['text'][:3000]}..."

        try:
            return await self.llm.generate_json(
                prompt=user_prompt,
                system_prompt=system_prompt
            )
        except Exception:
            return {"score": 5.0, "reason": "Error in evaluation"}

    async def _routed_generate(self, user_id, workload, *, user_prompt,
                               system_prompt, temperature, max_tokens, tier_enum):
        """Синтез ответа с распределением по нагрузке (workload_policy).

        ЗА флагом enable_tiered_extraction → модель тенанта нужного уровня с
        3-слойной Google-страховкой. По умолчанию/при сбое — прежний
        self.llm.generate (поведение 1:1, тот же tier)."""
        async def _legacy(_p):
            return await self.llm.generate(
                prompt=user_prompt, system_prompt=system_prompt,
                temperature=temperature, max_tokens=max_tokens, model_tier=tier_enum)
        try:
            from backend.core.config import flags as _flags
            if user_id and getattr(_flags, "enable_tiered_extraction", False):
                from backend.core.llm.workload_policy import generate_for_workload
                combined = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt
                t = await generate_for_workload(user_id, workload, combined,
                                                fallback_generate=_legacy)
                if t:
                    return t
        except Exception as e:  # noqa: BLE001
            logger.debug("search workload route fallback: %s", e)
        return await _legacy(None)

    async def _synthesize_answer(
        self,
        query: str,
        context: Dict[str, Any],
        understanding: Dict[str, Any],
        search_depth: str = "standard",
        user_id: Optional[str] = None,
    ) -> str:
        """
        Генерация финального ответа с цитатами.
        """
        # Проверяем есть ли контекст из документов
        extra_context = getattr(self, '_extra_context', None)

        if not context["sources"] and not extra_context:
            return "К сожалению, я не нашел информации по вашему запросу в базе знаний."

        # Логируем для отладки
        logger.info(f"📝 Synthesizing answer for: {query}")
        logger.info(f"📊 Context sources: {len(context.get('sources', []))}")
        logger.info(f"📄 Context text length: {len(context.get('text', ''))}")
        if extra_context:
            logger.info(f"📎 Extra context length: {len(extra_context)}")

        # Формируем промпт с учётом типа анализа
        _understanding_intent = understanding.get("intent", "FACTOID")
        _analysis_type = understanding.get("analysis_type", {})
        _semantic = understanding.get("semantic_decomposition", {})
        _fact_intent = understanding.get("fact_vs_intent", {})

        # Дополнительные инструкции в зависимости от типа запроса
        intent_instructions = ""
        if _understanding_intent == "AGGREGATION":
            intent_instructions = """
СПЕЦЗАДАЧА — АГРЕГАЦИЯ:
- Подсчитай точные числа из данных (не выдумывай).
- Если есть KPI — покажи конкретные значения и тренды.
- Отделяй ФАКТЫ (что произошло) от ПЛАНОВ (что планировали).
"""
        elif _understanding_intent in ("CAUSAL", "PATTERN"):
            intent_instructions = """
СПЕЦЗАДАЧА — ПРИЧИННО-СЛЕДСТВЕННЫЙ АНАЛИЗ:
- Ищи причинно-следственные связи: ЧТО → ПОЧЕМУ → К ЧЕМУ ПРИВЕЛО.
- Используй каузальные цепочки и паттерны из контекста.
- Не просто перечисляй факты — ОБЪЯСНИ связи между ними.
- Если есть противоречия — укажи их и объясни.
"""
        elif _understanding_intent == "RECOMMENDATION":
            intent_instructions = """
СПЕЦЗАДАЧА — РЕКОМЕНДАЦИЯ:
- Проанализируй проблемы и паттерны из данных.
- Сформулируй КОНКРЕТНЫЕ рекомендации на основе найденных данных.
- Оцени приоритет каждой рекомендации.
- Укажи, на каких данных основаны твои выводы.
"""
        elif _understanding_intent == "SCENARIO":
            intent_instructions = """
СПЕЦЗАДАЧА — СЦЕНАРНЫЙ АНАЛИЗ (What-If):
- Проанализируй текущие данные (KPI, задачи, риски, решения).
- Опиши 2-3 сценария развития ситуации: оптимистичный, реалистичный, пессимистичный.
- Для каждого сценария укажи: условия, вероятные последствия, действия для подготовки.
- Обоснуй сценарии ДАННЫМИ из контекста.
"""
        elif _understanding_intent == "AUDIT":
            intent_instructions = """
СПЕЦЗАДАЧА — АУДИТ:
- Проведи всесторонний аудит запрашиваемой области.
- Используй данные из всех источников: встречи, задачи, KPI, риски, решения, паттерны.
- Структурируй ответ: Текущее состояние → Проблемы → Сильные стороны → Рекомендации.
- Будь объективен, не приукрашивай — это аудит.
"""

        fact_instructions = ""
        if _fact_intent.get("seeking") == "FACT":
            fact_instructions = f"""
ВАЖНО: Пользователь ищет СВЕРШИВШИЕСЯ ФАКТЫ, а не планы или намерения.
Отделяй "подписали 8 договоров" (факт) от "планируем подписать 10" (план).
Исключай из ответа: {', '.join(_fact_intent.get('exclude_patterns', []))}.
"""

        evidence_contract_instructions = ""
        if (
            search_depth != "quick"
            or _understanding_intent in ("ANALYTICAL", "CAUSAL", "PATTERN", "RECOMMENDATION", "SCENARIO", "AUDIT")
            or understanding.get("complexity") in ("COMPLEX", "RESEARCH")
        ):
            if getattr(self, "_answer_mode", None) == "analysis":
                # Q1 (аудит): режим анализа ниже УЖЕ требует отделять факты от
                # выводов и не выдумывать — не дублируем инструкцию для дешёвой
                # модели, оставляем только уникальную часть (блок неопределённостей).
                evidence_contract_instructions = """
- Если есть пробелы, противоречия или слабые места в данных — выдели их отдельным блоком "Неопределённости".
"""
            else:
                evidence_contract_instructions = """
ФОРМАТ ДОКАЗАТЕЛЬНОГО ОТВЕТА:
- Сначала дай основной вывод по вопросу.
- Затем кратко отдели ПРЯМЫЕ ОСНОВАНИЯ (что прямо следует из контекста) от ИНТЕРПРЕТАЦИИ (где ты делаешь вывод).
- Если есть пробелы, противоречия или слабые места в данных — выдели их отдельным блоком "Неопределённости".
- Не придумывай evidence: если прямых оснований мало, честно скажи об этом.
"""

        # СЕГОДНЯШНЯЯ ДАТА обязана быть в промпте: без неё модель выводит
        # «сегодня» из самой свежей встречи в выдаче и считает «последний
        # месяц» от неё (golden-прогон 02.07.2026: ответ «отсчёт от текущей
        # даты 15.04.2026» — по дате последней встречи, ошибка на 2.5 месяца).
        _today = datetime.now().strftime("%d.%m.%Y")
        system_prompt = f"""Ты - аналитик Tessbrain с глубоким пониманием смыслов корпоративных данных.

СЕГОДНЯ: {_today}. Все относительные периоды («последний месяц», «недавно»,
«сейчас») отсчитывай ОТ ЭТОЙ даты, а не от даты последней встречи в данных.
Если самые свежие данные заметно старше сегодняшней даты — честно отметь это.

ВАЖНЫЕ ИНСТРУКЦИИ:
1. Внимательно прочитай ВЕСЬ предоставленный контекст перед ответом.
2. Ответ должен быть ПОДРОБНЫМ и РАЗВЁРНУТЫМ - извлеки максимум полезной информации.
3. Используй конкретные факты, цифры, названия и имена из контекста.
4. Структурируй ответ с помощью списков и заголовков, если информации много.
5. Ссылайся на источники (например, "согласно данным встречи...", "по информации из графа...").
6. Если информация разрознена - собери её в единый СВЯЗНЫЙ ответ, покажи связи.
7. Отвечай на русском языке.
8. Если есть каузальные цепочки (причины → следствия) — используй их для объяснения.
9. Если есть данные о связях между сущностями (граф) — используй для обоснования.
10. Если есть паттерны — выдели закономерности и выводы.
{intent_instructions}
{fact_instructions}
{evidence_contract_instructions}
КРИТИЧЕСКИ ВАЖНО: Если в контексте есть информация по теме вопроса - ОБЯЗАТЕЛЬНО используй её для ответа, даже если она не полная. Лучше частичный ответ с реальными данными, чем отказ отвечать. ДУМАЙ над ответом, выводи СМЫСЛ из данных, а не просто цитируй.
"""

        # ═══ ANSWER-MODE: ANALYSIS ═══
        # Если answer_mode_router определил вопрос как динамика/история/мультисессия/
        # синтез — усиливаем синтез валидированной инструкцией (замер на ДЕШЁВОЙ
        # модели: multi-session 0.143→0.714, temporal +0.27, HotpotQA multi-hop +0.20;
        # docs/ru/BENCHMARK_PUBLIC_LOCOMO_HOTPOT.md). Семантика как у ANALYSIS_PROMPT в
        # backend/core/think/answer_mode_router.py — порт на язык продукта (ответ на
        # русском, инструкция #7). Баланс LoCoMo adversarial: компилируй, НО раздели
        # факты/выводы и не выдумывай при пустых данных. За флагом — иначе блок не добавляется.
        if getattr(self, "_answer_mode", None) == "analysis":
            system_prompt += """
═══ РЕЖИМ АНАЛИЗА (динамика / история / мультисессия / синтез) ═══
Вопрос требует НЕ простого lookup'а, а АНАЛИЗА во времени. Действуй так:
- СОЕДИНЯЙ информацию ИЗ РАЗНЫХ встреч/сессий/документов и дат — не отвечай по одному фрагменту.
- Рассуждай о ПОРЯДКЕ и ИЗМЕНЕНИИ во времени: как было РАНЬШЕ vs как СЕЙЧАС, где произошёл перелом/сдвиг. Опирайся на даты, выстраивай хронологию.
- КОМПИЛИРУЙ лучший обоснованный ответ из данных, даже если нет одной прямой фразы — не отказывайся преждевременно (например, «кого нанять / где гапы» — собери вывод из разрозненных сигналов).
- ЧЕСТНОСТЬ: чётко отделяй то, что ПРЯМО сказано в данных, от того, что ты ВЫВОДИШЬ. Факты — утверждай; выводы — помечай («вероятно», «судя по обсуждениям», «это указывает на»).
- РЕЗОЛЮЦИЯ ДАТ: у фрагментов есть даты. Относительные выражения («вчера», «на прошлой неделе», «в прошлом месяце», «прошлым летом», «в следующем месяце») отсчитывай ОТ ДАТЫ фрагмента — применяй смещение (вчера = дата−1 день; на прошлой неделе ≈ −7 дней; в прошлом месяце/году/летом = предыдущий период), вычисли АБСОЛЮТНУЮ дату, а не выводи дату фрагмента без изменений.
- «КАК СЕЙЧАС / последнее / текущее» — опирайся на САМЫЕ СВЕЖИЕ по дате фрагменты, не путай их со старыми (например, годичной давности).
- Если данных по сути НЕТ — так и скажи, не выдумывай факты/числа/мнения, которых в памяти нет.
- Заверши одним кратким итоговым выводом.
"""

        # ═══ ПЕРСОНАЛИЗАЦИЯ (за флагом enable_chat_personalization) ═══
        # Инструкция из профиля пользователя (стиль/детализация/известные темы).
        # Меняет ПОДАЧУ, не содержание. Флаг OFF / профиль пуст → блока нет.
        _pers = getattr(self, "_personalization", None)
        if _pers:
            system_prompt += (
                "\n═══ ПЕРСОНАЛИЗАЦИЯ ПОД ПОЛЬЗОВАТЕЛЯ ═══\n"
                f"{_pers}\n"
                "Персонализация меняет только ПОДАЧУ ответа — факты, цифры и "
                "честность про отсутствие данных не меняются.\n"
            )

        # ═══ CONTEXT WINDOW GUARD ═══
        # Адаптивное управление размером контекста для предотвращения переполнения
        full_context = context.get('text', '')

        if extra_context:
            full_context = f"=== КОНТЕКСТ ИЗ ДОКУМЕНТОВ ===\n{extra_context}\n\n=== КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ ===\n{full_context}"

        # Лимиты по tier модели (chars ~ tokens * 3.5 для русского)
        model_tier_current = getattr(self, '_model_tier', 'standard')
        if model_tier_current == 'premium':
            # Premium модели: больший контекст
            max_context_chars = 24000
        elif search_depth == "deep":
            max_context_chars = 18000
        elif search_depth == "standard":
            max_context_chars = 14000
        else:
            max_context_chars = 10000  # quick

        # Q1 (аудит): бюджет выше закладывал system prompt ~2000 символов, но с
        # блоками анализа/персонализации он растёт — вычитаем реальный перерасход
        # из бюджета контекста, чтобы суммарный запрос не переполнял окно модели.
        _sys_overhead = max(0, len(system_prompt) - 2000)
        max_context_chars = max(6000, max_context_chars - _sys_overhead)

        if len(full_context) > max_context_chars:
            # Умное обрезание: сохраняем начало (обычно важнее) и конец (последние результаты)
            head_size = int(max_context_chars * 0.7)  # 70% — начало
            tail_size = int(max_context_chars * 0.25)  # 25% — конец
            # 5% — пометка
            full_context = (
                full_context[:head_size] +
                f"\n\n[...пропущено {len(full_context) - head_size - tail_size} символов. "
                f"Показано начало и конец контекста...]\n\n" +
                full_context[-tail_size:]
            )
            logger.info(
                f"Context Window Guard: trimmed {len(context.get('text', ''))} -> {len(full_context)} chars "
                f"(budget: {max_context_chars}, depth: {search_depth})"
            )

        user_prompt = f"""Вопрос пользователя: {query}

=== НАЙДЕННЫЙ КОНТЕКСТ ===
{full_context}

Пожалуйста, дай подробный ответ на вопрос, используя информацию из контекста выше."""

        # Определяем tier модели
        model_tier = getattr(self, '_model_tier', 'standard')
        tier_enum = ModelTier.PREMIUM if model_tier == 'premium' else ModelTier.STANDARD

        logger.info(f"[LLM] Sending to LLM, prompt length: {len(user_prompt)}, model_tier: {model_tier}")

        # Для complex/deep — увеличиваем max_tokens
        _max_tokens = 2000
        if _understanding_intent in ("ANALYTICAL", "CAUSAL", "PATTERN", "RECOMMENDATION", "SCENARIO", "AUDIT"):
            _max_tokens = 4000
        if understanding.get("complexity") in ("COMPLEX", "RESEARCH"):
            _max_tokens = 4000

        # Распределение по нагрузке: обычный поиск/чат — быстрая модель, а
        # СИНТЕЗ ГЛУБОКОГО АНАЛИЗА (COMPLEX/RESEARCH после multi-query
        # исследования) — большая. Раньше deep-research отвечал той же
        # lite-моделью, что и болталка — глубина поиска пропадала в синтезе.
        _deep_analysis = (model_tier == "premium"
                          or understanding.get("complexity") in ("COMPLEX", "RESEARCH"))
        _wl = "search_deep_synthesis" if _deep_analysis else "search_standard"
        if _deep_analysis:
            tier_enum = ModelTier.PREMIUM
        answer = await self._routed_generate(
            user_id, _wl,
            user_prompt=user_prompt, system_prompt=system_prompt,
            temperature=0.3, max_tokens=_max_tokens, tier_enum=tier_enum,
        )

        logger.info(f"📥 LLM response length: {len(answer)}")

        return answer

    # ═══════════════════════════════════════════════════════════
    # LEVEL 2: Graph Traversal, Pattern Analysis, Cross-Entity
    # ═══════════════════════════════════════════════════════════

    async def _graph_multi_hop_traversal(
        self, query: str, understanding: Dict[str, Any], max_hops: int = 2
    ) -> List[str]:
        """
        Multi-hop graph traversal — обход связей на 2-3 уровня.

        Находит начальные ноды по запросу, затем обходит их связи,
        собирая контекст из связанных сущностей.

        Решает: "кто блокирует задачу X" → Task → BLOCKED_BY → Reason → Person
        """
        if not self.hybrid_search.graph_builder:
            return []

        graph = self.hybrid_search.graph_builder
        results = []

        # Извлекаем сущности из understanding
        entities = understanding.get("entities", [])

        # Также ищем начальные ноды по запросу
        seed_nodes = []

        # 1. Ноды из entities
        for ent in entities[:5]:
            ent_name = ent if isinstance(ent, str) else ent.get("name", "")
            if not ent_name:
                continue
            try:
                node = await graph.get_node_by_id(ent_name)
                if node:
                    seed_nodes.append(node)
                    continue

                # Поиск по имени
                search_results = await graph.search_nodes(query=ent_name, limit=3)
                for sr in search_results:
                    if sr.get("id"):
                        seed_nodes.append(sr)
                        break
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # 2. Если нет конкретных сущностей — ищем по ключевым словам запроса
        if not seed_nodes:
            try:
                search_results = await graph.search_nodes(query=query, limit=5)
                seed_nodes.extend(search_results)
            except Exception:
                logger.debug("suppressed exception", exc_info=True)

        # 3. Обходим связи каждой seed-ноды
        visited = set()

        for seed in seed_nodes[:5]:
            seed_id = seed.get("id")
            if not seed_id or seed_id in visited:
                continue
            visited.add(seed_id)

            seed_name = seed.get("name", seed.get("title", seed_id))
            seed_label = seed.get("_label", "")

            try:
                rels = await graph.get_node_relationships(seed_id)

                for direction in ("outgoing", "incoming"):
                    for rel in rels.get(direction, [])[:10]:
                        other_id = rel.get("target" if direction == "outgoing" else "source", "")
                        if not other_id or other_id in visited:
                            continue

                        other_node = await graph.get_node_by_id(other_id)
                        if not other_node:
                            continue

                        other_name = other_node.get("name", other_node.get("title", other_id))
                        other_label = other_node.get("_label", "")
                        rel_type = rel.get("relation", rel.get("type", "RELATED_TO"))
                        rel_desc = rel.get("description", "")

                        # Формируем контекст связи
                        if direction == "outgoing":
                            line = f"• {seed_name} ({seed_label}) —[{rel_type}]→ {other_name} ({other_label})"
                        else:
                            line = f"• {other_name} ({other_label}) —[{rel_type}]→ {seed_name} ({seed_label})"

                        if rel_desc:
                            line += f": {rel_desc}"

                        # Добавляем свойства связанной ноды
                        other_desc = other_node.get("description", other_node.get("summary", ""))
                        other_status = other_node.get("status", "")
                        if other_desc:
                            line += f"\n  Описание: {other_desc[:200]}"
                        if other_status:
                            line += f"\n  Статус: {other_status}"

                        results.append(line)
                        visited.add(other_id)

                        # Hop 2: обходим связи связанных нод (если max_hops > 1)
                        if max_hops > 1:
                            try:
                                hop2_rels = await graph.get_node_relationships(other_id)
                                for d2 in ("outgoing", "incoming"):
                                    for rel2 in hop2_rels.get(d2, [])[:5]:
                                        hop2_id = rel2.get("target" if d2 == "outgoing" else "source", "")
                                        if not hop2_id or hop2_id in visited:
                                            continue

                                        hop2_node = await graph.get_node_by_id(hop2_id)
                                        if not hop2_node:
                                            continue

                                        hop2_name = hop2_node.get("name", hop2_node.get("title", hop2_id))
                                        hop2_label = hop2_node.get("_label", "")
                                        rel2_type = rel2.get("relation", rel2.get("type", ""))

                                        line2 = f"  ↳ {other_name} —[{rel2_type}]→ {hop2_name} ({hop2_label})"
                                        hop2_status = hop2_node.get("status", "")
                                        if hop2_status:
                                            line2 += f" [{hop2_status}]"

                                        results.append(line2)
                                        visited.add(hop2_id)
                            except Exception:
                                logger.debug("suppressed exception", exc_info=True)
            except Exception as e:
                logger.warning(f"Traversal failed for {seed_id}: {e}")

        return results[:30]

    async def _analyze_patterns(
        self, query: str, understanding: Dict[str, Any]
    ) -> List[str]:
        """
        Анализ паттернов — ищет повторяющиеся проблемы, частые темы, узкие места.

        Решает: "каких не хватает сотрудников" → находит:
        - Задачи со статусом blocked (по каким причинам)
        - Темы, обсуждавшиеся на 3+ встречах без решения
        - Risk ноды с высоким impact
        - Contradiction ноды (конфликтующая информация)
        """
        if not self.hybrid_search.graph_builder:
            return []

        graph = self.hybrid_search.graph_builder
        results = []

        # 1. Заблокированные задачи — системные проблемы
        try:
            tasks = await graph.find_nodes_by_label(label="Task", limit=100)
            blocked_tasks = [t for t in tasks if t.get("status") in ("blocked", "on_hold", "stuck")]
            if blocked_tasks:
                results.append(f"🚧 Заблокированные задачи ({len(blocked_tasks)}):")
                for bt in blocked_tasks[:10]:
                    name = bt.get("name", bt.get("title", "N/A"))
                    assignee = bt.get("assignee", "не назначен")
                    reason = bt.get("blocked_reason", bt.get("description", ""))
                    results.append(f"  - {name} (исполнитель: {assignee})")
                    if reason:
                        results.append(f"    Причина: {reason[:200]}")
        except Exception as e:
            logger.warning(f"Pattern: blocked tasks failed: {e}")

        # 2. Риски с высоким impact
        try:
            risks = await graph.find_nodes_by_label(label="Risk", limit=50)
            high_risks = [r for r in risks if r.get("impact") in ("high", "critical", "высокий")]
            if high_risks:
                results.append(f"\n⚠️ Риски высокого уровня ({len(high_risks)}):")
                for hr in high_risks[:5]:
                    name = hr.get("name", hr.get("description", "N/A"))
                    probability = hr.get("probability", "?")
                    mitigation = hr.get("mitigation", "нет плана")
                    results.append(f"  - {name[:200]} (вероятность: {probability})")
                    results.append(f"    Митигация: {mitigation[:200]}")
        except Exception as e:
            logger.warning(f"Pattern: risks failed: {e}")

        # 3. Противоречия в данных
        try:
            contradictions = await graph.find_nodes_by_label(label="Contradiction", limit=20)
            if contradictions:
                results.append(f"\n🔀 Противоречия в данных ({len(contradictions)}):")
                for c in contradictions[:5]:
                    new_fact = c.get("new_fact", "")
                    existing = c.get("existing_fact", "")
                    results.append(f"  - БЫЛО: {existing[:150]}")
                    results.append(f"    СТАЛО: {new_fact[:150]}")
        except Exception as e:
            logger.warning(f"Pattern: contradictions failed: {e}")

        # 4. Часто упоминаемые ноды (по frequency_score) — горячие темы
        try:
            all_entities = await graph.find_nodes_by_label(label="Entity", limit=100)
            frequent = [e for e in all_entities if (e.get("frequency_score", 0) or 0) > 3]
            frequent.sort(key=lambda x: x.get("frequency_score", 0), reverse=True)
            if frequent:
                results.append("\n🔥 Горячие темы (часто упоминаемые):")
                for f in frequent[:10]:
                    name = f.get("name", "N/A")
                    freq = f.get("frequency_score", 0)
                    results.append(f"  - {name} (упоминаний: {freq})")
        except Exception as e:
            logger.warning(f"Pattern: frequency failed: {e}")

        # 5. Pattern ноды из графа
        try:
            patterns = await graph.find_nodes_by_label(label="Pattern", limit=20)
            if patterns:
                results.append(f"\n📈 Обнаруженные паттерны ({len(patterns)}):")
                for p in patterns[:5]:
                    name = p.get("name", p.get("description", "N/A"))
                    results.append(f"  - {name[:300]}")
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        return results

    async def _cross_entity_analysis(
        self, query: str, understanding: Dict[str, Any]
    ) -> List[str]:
        """
        Cross-Entity Analysis — связывание данных между сущностями.

        Включает:
        - KPI aggregation (суммы, тренды, сравнения)
        - Связи между отделами/проектами/людьми
        - Нагрузка на людей (кол-во задач, проектов)
        """
        if not self.hybrid_search.graph_builder:
            return []

        graph = self.hybrid_search.graph_builder
        results = []

        # 1. KPI Aggregation
        try:
            kpis = await graph.find_nodes_by_label(label="KPI", limit=100)
            if kpis:
                # Группируем по категориям
                by_category = {}
                for kpi in kpis:
                    cat = kpi.get("category", "Общие")
                    if cat not in by_category:
                        by_category[cat] = []
                    by_category[cat].append(kpi)

                results.append(f"📊 KPI ({len(kpis)} метрик в {len(by_category)} категориях):")
                for cat, items in list(by_category.items())[:5]:
                    results.append(f"\n  [{cat}]")
                    for item in items[:5]:
                        name = item.get("name", "N/A")
                        value = item.get("numeric_value", item.get("value", "?"))
                        unit = item.get("unit", "")
                        trend = item.get("trend", "")
                        trend_icon = "📈" if trend in ("up", "рост") else "📉" if trend in ("down", "падение") else "➡️"
                        results.append(f"    {trend_icon} {name}: {value} {unit}")
        except Exception as e:
            logger.warning(f"Cross-entity: KPI failed: {e}")

        # 2. Нагрузка на людей
        try:
            persons = await graph.find_nodes_by_label(label="Person", limit=50)
            person_workload = []

            for person in persons[:20]:
                pid = person.get("id")
                if not pid:
                    continue

                try:
                    rels = await graph.get_node_relationships(pid)

                    task_count = 0
                    project_count = 0
                    blocked_count = 0

                    for direction in ("outgoing", "incoming"):
                        for rel in rels.get(direction, []):
                            other_id = rel.get("target" if direction == "outgoing" else "source", "")
                            if not other_id:
                                continue
                            other = await graph.get_node_by_id(other_id)
                            if not other:
                                continue
                            label = other.get("_label", "")
                            if label == "Task":
                                task_count += 1
                                if other.get("status") in ("blocked", "stuck"):
                                    blocked_count += 1
                            elif label == "Project":
                                project_count += 1

                    if task_count > 0:
                        person_workload.append({
                            "name": person.get("name", "N/A"),
                            "tasks": task_count,
                            "projects": project_count,
                            "blocked": blocked_count,
                        })
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

            if person_workload:
                person_workload.sort(key=lambda x: x["tasks"], reverse=True)
                results.append("\n👥 Нагрузка на сотрудников:")
                for pw in person_workload[:10]:
                    blocked_str = f", ❌ заблокировано: {pw['blocked']}" if pw["blocked"] > 0 else ""
                    results.append(
                        f"  - {pw['name']}: {pw['tasks']} задач, {pw['projects']} проектов{blocked_str}"
                    )
        except Exception as e:
            logger.warning(f"Cross-entity: workload failed: {e}")

        # 3. Связи между проектами
        try:
            projects = await graph.find_nodes_by_label(label="Project", limit=30)
            if len(projects) > 1:
                project_connections = []
                for proj in projects[:10]:
                    proj_id = proj.get("id")
                    if not proj_id:
                        continue
                    try:
                        rels = await graph.get_node_relationships(proj_id)
                        dep_count = 0
                        for direction in ("outgoing", "incoming"):
                            for rel in rels.get(direction, []):
                                rel_type = rel.get("relation", rel.get("type", ""))
                                if rel_type in ("DEPENDS_ON", "BLOCKED_BY", "RELATED_TO"):
                                    dep_count += 1

                        project_connections.append({
                            "name": proj.get("name", "N/A"),
                            "status": proj.get("status", "?"),
                            "deps": dep_count,
                        })
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)

                if project_connections:
                    project_connections.sort(key=lambda x: x["deps"], reverse=True)
                    results.append("\n📁 Проекты и зависимости:")
                    for pc in project_connections[:8]:
                        results.append(f"  - {pc['name']} [{pc['status']}] — {pc['deps']} связей")
        except Exception as e:
            logger.warning(f"Cross-entity: projects failed: {e}")

        return results

    def _apply_weight_scoring(self, sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Применить веса узлов для улучшения ранжирования.

        Формула: adjusted_score = base_score × (1 + α×weight + β×freq + γ×emotion)
        где:
        - α = 0.10 (вес узла)
        - β = 0.05 (частота упоминаний)
        - γ = 0.15 (эмоциональная значимость)

        Args:
            sources: Список источников с базовым score

        Returns:
            Отсортированный список с adjusted_score
        """
        ALPHA = 0.10  # node_weight
        BETA = 0.05   # frequency_score
        GAMMA = 0.15  # emotional_salience

        for source in sources:
            base_score = source.get("score", 0.5)
            metadata = source.get("metadata", {})

            # Получаем веса из metadata
            node_weight = metadata.get("weight", 1.0)
            frequency_score = metadata.get("frequency_score", 0.0)
            emotional_salience = metadata.get("emotional_salience", 0.0)

            # Вычисляем adjusted_score
            multiplier = (
                1.0
                + ALPHA * (node_weight - 1.0)  # Нормализуем от 1.0
                + BETA * frequency_score
                + GAMMA * emotional_salience
            )

            source["adjusted_score"] = base_score * max(multiplier, 0.1)
            source["weight_factors"] = {
                "node_weight": node_weight,
                "frequency_score": frequency_score,
                "emotional_salience": emotional_salience,
                "multiplier": multiplier,
            }

        # Сортируем по adjusted_score
        sources.sort(key=lambda x: x.get("adjusted_score", x.get("score", 0)), reverse=True)

        return sources


# Singleton instance
_enhanced_orchestrator: Optional[EnhancedSearchOrchestrator] = None

def get_enhanced_search_orchestrator(
    hybrid_search: HybridSearchOrchestrator,
    llm_router: LLMRouter
) -> EnhancedSearchOrchestrator:
    global _enhanced_orchestrator
    if _enhanced_orchestrator is None:
        _enhanced_orchestrator = EnhancedSearchOrchestrator(
            hybrid_search=hybrid_search,
            llm_router=llm_router
        )
    return _enhanced_orchestrator
