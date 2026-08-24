# -*- coding: utf-8 -*-
"""
Session-based Agent Pipeline — замена AG2 GroupChat.

Детерминированный пайплайн агентов с сессионной памятью.
Каждый агент получает структурированные входные данные и возвращает результат.
Нет LLM-based speaker selection — порядок выполнения фиксирован.

Архитектура:
1. PLANNER — анализирует вопрос, составляет план поиска
2. RESEARCHER — выполняет поиск через Enhanced Search
3. CRITIC — оценивает качество найденных данных
4. SYNTHESIZER — формирует финальный ответ

Отличия от AG2:
- Детерминированный порядок (нет случайных петель)
- Сессионная память (предыдущие запросы влияют на текущий)
- Structured I/O (dataclasses вместо свободного текста)
- Нет зависимости от autogen
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
#  Data structures для межагентной коммуникации
# ═══════════════════════════════════════════════════════

@dataclass
class AgentPlan:
    """Результат работы Planner."""
    original_query: str = ""
    interpreted_query: str = ""       # Уточнённый запрос (с учётом сессии)
    search_queries: List[str] = field(default_factory=list)  # Подзапросы
    required_data: List[str] = field(default_factory=list)   # Типы данных для поиска
    complexity: str = "standard"      # simple, standard, complex
    approach: str = ""                # Краткое описание подхода


@dataclass
class AgentSearchResults:
    """Результаты поиска от Researcher."""
    answers: List[Dict[str, Any]] = field(default_factory=list)  # [{query, answer, confidence, sources}]
    raw_context: str = ""             # Объединённый контекст для синтеза
    total_sources: int = 0
    search_time_ms: int = 0


@dataclass
class AgentCritique:
    """Оценка от Critic."""
    quality_score: float = 0.0        # 0-10
    is_sufficient: bool = False       # Достаточно ли данных для ответа
    missing_data: List[str] = field(default_factory=list)      # Что не хватает
    additional_queries: List[str] = field(default_factory=list) # Доп. запросы
    feedback: str = ""


@dataclass
class AgentSessionContext:
    """Контекст сессии для передачи между агентами."""
    session_id: str = ""
    user_id: str = ""
    previous_queries: List[str] = field(default_factory=list)     # Предыдущие вопросы
    previous_answers: List[str] = field(default_factory=list)     # Предыдущие ответы (краткие)
    mentioned_entities: List[str] = field(default_factory=list)   # Упомянутые сущности
    active_topic: str = ""                                         # Текущая тема разговора


@dataclass
class PipelineResult:
    """Финальный результат пайплайна."""
    answer: str = ""
    confidence: float = 0.0
    sources: List[Dict[str, Any]] = field(default_factory=list)
    reasoning_steps: List[str] = field(default_factory=list)
    processing_time_ms: int = 0
    agent_trace: List[Dict[str, Any]] = field(default_factory=list)  # Для отладки


class SessionAgentPipeline:
    """
    Детерминированный пайплайн агентов с сессионной памятью.

    Использование:
        pipeline = SessionAgentPipeline(enhanced_search, llm_router)
        result = await pipeline.run(
            query="Чем занимается компания?",
            session_context=session_ctx,
            user_id="user123"
        )
    """

    def __init__(
        self,
        enhanced_search=None,
        llm=None,
        graph_builder=None,
        max_retries: int = 1    # Макс. кол-во повторных поисков по рекомендации Critic
    ):
        self.enhanced_search = enhanced_search
        self.llm = llm
        self.graph_builder = graph_builder
        self.max_retries = max_retries

    async def run(
        self,
        query: str,
        session_context: Optional[AgentSessionContext] = None,
        user_id: str = "",
        search_depth: str = "standard",
        extra_context: str = ""
    ) -> PipelineResult:
        """
        Запуск пайплайна.

        Args:
            query: Запрос пользователя
            session_context: Контекст сессии (предыдущие Q&A)
            user_id: ID пользователя
            search_depth: Глубина поиска
            extra_context: Дополнительный контекст

        Returns:
            PipelineResult с ответом и метаданными
        """
        start = time.time()
        steps = []
        trace = []

        if not session_context:
            session_context = AgentSessionContext(user_id=user_id)

        # ═══ STEP 1: PLANNER ═══
        steps.append("1. Planner: анализ вопроса и составление плана")
        plan = await self._run_planner(query, session_context, extra_context)
        trace.append({"agent": "planner", "output": {
            "interpreted_query": plan.interpreted_query,
            "search_queries": plan.search_queries,
            "complexity": plan.complexity
        }})
        steps.append(f"   Plan: {len(plan.search_queries)} подзапросов, сложность: {plan.complexity}")

        # ═══ STEP 2: RESEARCHER ═══
        # seen_queries: НЕ ищем один и тот же подзапрос дважды за прогон —
        # Critic часто рекомендует уже отработанные запросы; повтор полного
        # standard-поиска на идентичном тексте = чистый мусор (тот же вход →
        # тот же результат). Дедуп безопасен, качество не меняется.
        steps.append("2. Researcher: поиск по базе знаний")
        _seen_queries: set = set()
        search_results = await self._run_researcher(
            plan, user_id, search_depth, seen_queries=_seen_queries)
        trace.append({"agent": "researcher", "output": {
            "answers_count": len(search_results.answers),
            "total_sources": search_results.total_sources,
            "search_time_ms": search_results.search_time_ms
        }})
        steps.append(f"   Найдено: {search_results.total_sources} источников за {search_results.search_time_ms}ms")

        # ═══ STEP 3: CRITIC (optional retry) ═══
        retries_done = 0
        while retries_done < self.max_retries:
            steps.append(f"3. Critic: оценка качества (попытка {retries_done + 1})")
            critique = await self._run_critic(query, plan, search_results, session_context)
            trace.append({"agent": "critic", "output": {
                "quality_score": critique.quality_score,
                "is_sufficient": critique.is_sufficient,
                "missing_data": critique.missing_data
            }})
            steps.append(f"   Качество: {critique.quality_score}/10, достаточно: {critique.is_sufficient}")

            if critique.is_sufficient or not critique.additional_queries:
                break

            # Дополнительный поиск по рекомендации Critic
            steps.append(f"   Critic рекомендует доп. поиск: {critique.additional_queries}")
            additional_results = await self._run_researcher(
                AgentPlan(
                    original_query=query,
                    interpreted_query=query,
                    search_queries=critique.additional_queries,
                    complexity=plan.complexity
                ),
                user_id,
                search_depth,
                seen_queries=_seen_queries,
            )
            # Объединяем результаты
            search_results.answers.extend(additional_results.answers)
            search_results.raw_context += "\n\n" + additional_results.raw_context
            search_results.total_sources += additional_results.total_sources
            retries_done += 1

        # ═══ STEP 4: SYNTHESIZER ═══
        steps.append("4. Synthesizer: формирование ответа")
        answer = await self._run_synthesizer(query, plan, search_results, critique, session_context)

        processing_time = int((time.time() - start) * 1000)

        # Собираем источники из всех ответов
        all_sources = []
        seen_sources = set()
        for ans in search_results.answers:
            for src in ans.get("sources", []):
                src_key = src.get("name", "") or str(src)
                if src_key not in seen_sources:
                    seen_sources.add(src_key)
                    all_sources.append(src)

        return PipelineResult(
            answer=answer,
            confidence=critique.quality_score / 10.0 if critique else 0.5,
            sources=all_sources[:20],
            reasoning_steps=steps,
            processing_time_ms=processing_time,
            agent_trace=trace
        )

    # ═══════════════════════════════════════════════════════
    #  Agent implementations
    # ═══════════════════════════════════════════════════════

    async def _run_planner(
        self,
        query: str,
        session_ctx: AgentSessionContext,
        extra_context: str = ""
    ) -> AgentPlan:
        """Planner: анализирует вопрос с учётом сессионного контекста."""

        # Собираем контекст сессии
        session_info = ""
        if session_ctx.previous_queries:
            recent = session_ctx.previous_queries[-5:]
            session_info = "Предыдущие вопросы в этой сессии:\n"
            for i, q in enumerate(recent, 1):
                session_info += f"  {i}. {q}\n"
            if session_ctx.mentioned_entities:
                session_info += f"Упомянутые сущности: {', '.join(session_ctx.mentioned_entities[:10])}\n"
            if session_ctx.active_topic:
                session_info += f"Текущая тема: {session_ctx.active_topic}\n"

        prompt = f"""Ты — Planner (планировщик поиска по базе знаний компании).

Твоя задача: разложить вопрос пользователя на конкретные поисковые запросы.

ВОПРОС ПОЛЬЗОВАТЕЛЯ: {query}

{session_info}

{f"ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ: {extra_context[:500]}" if extra_context else ""}

Верни JSON:
{{
    "interpreted_query": "уточнённый вопрос (с учётом контекста сессии, если есть)",
    "search_queries": ["запрос 1", "запрос 2", ...],
    "required_data": ["persons", "projects", "decisions", "meetings", "tasks"],
    "complexity": "simple|standard|complex",
    "approach": "краткое описание подхода к поиску"
}}

ПРАВИЛА:
- Если вопрос ссылается на предыдущие вопросы ("а что по Ивану?", "подробнее") — используй контекст сессии
- search_queries — это КОНКРЕТНЫЕ поисковые запросы для БД (не абстрактные шаги)
- Максимум 4 подзапроса
- Если вопрос простой — 1 запрос достаточно"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты аналитик. Отвечай ТОЛЬКО JSON.",
                temperature=0.2,
                max_tokens=1000
            )

            if isinstance(result, dict):
                return AgentPlan(
                    original_query=query,
                    interpreted_query=result.get("interpreted_query", query),
                    search_queries=result.get("search_queries", [query]),
                    required_data=result.get("required_data", []),
                    complexity=result.get("complexity", "standard"),
                    approach=result.get("approach", "")
                )
        except Exception as e:
            logger.warning(f"Planner LLM failed, using fallback: {e}")

        # Fallback: используем оригинальный запрос
        return AgentPlan(
            original_query=query,
            interpreted_query=query,
            search_queries=[query],
            complexity="standard"
        )

    async def _run_researcher(
        self,
        plan: AgentPlan,
        user_id: str,
        search_depth: str,
        seen_queries: set = None,
    ) -> AgentSearchResults:
        """Researcher: выполняет поиск через Enhanced Search.

        seen_queries — уже отработанные подзапросы (нормализ.): идентичный
        не ищем повторно (дедуп дубля от Critic'а, экономит полный
        standard-поиск; результат тот же)."""

        start = time.time()
        all_answers = []
        all_context_parts = []
        total_sources = 0
        if seen_queries is None:
            seen_queries = set()

        for sq in plan.search_queries[:4]:
            _norm_sq = " ".join(str(sq or "").lower().split())
            if not _norm_sq or _norm_sq in seen_queries:
                continue  # пустой или уже искали — чистый дедуп
            seen_queries.add(_norm_sq)
            try:
                result = await self.enhanced_search.search(
                    query=sq,
                    user_id=user_id,
                    search_depth=search_depth
                )

                answer_text = getattr(result, 'answer', str(result))
                sources = getattr(result, 'sources', [])
                confidence = getattr(result, 'confidence', 0.5)

                all_answers.append({
                    "query": sq,
                    "answer": answer_text,
                    "confidence": confidence,
                    "sources": sources[:10]
                })
                all_context_parts.append(f"### {sq}\n{answer_text}")
                total_sources += len(sources)

            except Exception as e:
                logger.warning(f"Researcher: search failed for '{sq[:50]}': {e}")
                all_answers.append({
                    "query": sq,
                    "answer": f"Ошибка поиска: {str(e)[:100]}",
                    "confidence": 0.0,
                    "sources": []
                })

        search_time = int((time.time() - start) * 1000)

        return AgentSearchResults(
            answers=all_answers,
            raw_context="\n\n".join(all_context_parts),
            total_sources=total_sources,
            search_time_ms=search_time
        )

    async def _run_critic(
        self,
        original_query: str,
        plan: AgentPlan,
        search_results: AgentSearchResults,
        session_ctx: AgentSessionContext
    ) -> AgentCritique:
        """Critic: оценивает качество найденных данных."""

        # Краткая сводка результатов
        results_summary = ""
        for ans in search_results.answers[:4]:
            answer_preview = ans.get("answer", "")[:200]
            results_summary += f"- Запрос: {ans.get('query', '')}\n  Ответ: {answer_preview}...\n  Уверенность: {ans.get('confidence', 0)}\n\n"

        prompt = f"""Ты — Critic (контролёр качества поиска).

ИСХОДНЫЙ ВОПРОС: {original_query}
ПЛАН: {plan.approach}

РЕЗУЛЬТАТЫ ПОИСКА:
{results_summary}

Оцени, достаточно ли данных для полноценного ответа на вопрос.

Верни JSON:
{{
    "quality_score": 7.5,
    "is_sufficient": true,
    "missing_data": ["что не хватает"],
    "additional_queries": ["доп. запрос если нужен"],
    "feedback": "краткая оценка"
}}

ПРАВИЛА:
- quality_score от 0 до 10
- is_sufficient=true если можно дать хоть какой-то полезный ответ (>=5/10)
- additional_queries — ТОЛЬКО если is_sufficient=false и данных критически мало
- Максимум 1 дополнительный запрос
- НЕ требуй идеальных данных — лучше частичный ответ, чем бесконечные поиски"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты контролёр качества. Отвечай ТОЛЬКО JSON.",
                temperature=0.2,
                max_tokens=500
            )

            if isinstance(result, dict):
                return AgentCritique(
                    quality_score=float(result.get("quality_score", 5.0)),
                    is_sufficient=bool(result.get("is_sufficient", True)),
                    missing_data=result.get("missing_data", []),
                    additional_queries=result.get("additional_queries", [])[:1],
                    feedback=result.get("feedback", "")
                )
        except Exception as e:
            logger.warning(f"Critic LLM failed, assuming sufficient: {e}")

        # Fallback: считаем что достаточно
        return AgentCritique(
            quality_score=5.0,
            is_sufficient=True,
            feedback="Critic unavailable, proceeding with available data"
        )

    async def _run_synthesizer(
        self,
        original_query: str,
        plan: AgentPlan,
        search_results: AgentSearchResults,
        critique: AgentCritique,
        session_ctx: AgentSessionContext
    ) -> str:
        """Synthesizer: формирует финальный ответ."""

        # Собираем контекст из всех ответов
        context_parts = []
        for ans in search_results.answers:
            if ans.get("answer") and ans.get("confidence", 0) > 0:
                context_parts.append(ans["answer"])

        full_context = "\n\n---\n\n".join(context_parts)

        # Контекст сессии для связности
        session_info = ""
        if session_ctx.previous_queries:
            session_info = "\nКонтекст разговора: пользователь ранее спрашивал: " + \
                "; ".join(session_ctx.previous_queries[-3:])

        # Обрезаем контекст до разумного размера
        max_ctx = 12000
        if len(full_context) > max_ctx:
            full_context = full_context[:int(max_ctx * 0.75)] + \
                "\n\n[...]\n\n" + full_context[-int(max_ctx * 0.2):]

        prompt = f"""Ты — аналитик компании. Дай подробный, структурированный ответ на вопрос.

ВОПРОС: {original_query}
{session_info}

ПЛАН АНАЛИЗА: {plan.approach}

СОБРАННЫЕ ДАННЫЕ:
{full_context}

ОЦЕНКА КАЧЕСТВА ДАННЫХ: {critique.quality_score}/10 — {critique.feedback}
{f"Отсутствующие данные: {', '.join(critique.missing_data)}" if critique.missing_data else ""}

ПРАВИЛА:
- Отвечай НА РУССКОМ языке
- Используй ТОЛЬКО данные из раздела "СОБРАННЫЕ ДАННЫЕ"
- НЕ придумывай факты
- Структурируй ответ (заголовки, списки)
- Если данных недостаточно — честно скажи что известно и чего не хватает
- Если вопрос связан с предыдущими — дай связный ответ"""

        try:
            answer = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты бизнес-аналитик, работающий с внутренней базой знаний компании.",
                temperature=0.3,
                max_tokens=4000
            )
            return answer
        except Exception as e:
            logger.error(f"Synthesizer LLM failed: {e}")
            # Fallback: возвращаем сырые данные
            if search_results.raw_context:
                return f"## Найденная информация\n\n{search_results.raw_context[:3000]}\n\n*Примечание: автоматическая обработка недоступна, показаны сырые данные.*"
            return "К сожалению, не удалось обработать результаты поиска. Попробуйте переформулировать вопрос."
