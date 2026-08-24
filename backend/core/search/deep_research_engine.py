# -*- coding: utf-8 -*-
"""
Deep Research Engine — 4-агентный исследовательский движок.

Архитектура:
1. STRATEGIST — анализирует вопрос, определяет план исследования
2. NAVIGATOR — ищет данные по плану (vector + graph + BM25 + snapshots)
3. RESEARCHER — анализирует найденное, проверяет противоречия, строит выводы
4. SYNTHESIZER — формирует структурированный отчёт с цитатами

Используется для сложных вопросов: "почему?", "как связаны?", "что изменилось?"
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResearchPlan:
    """План исследования от Strategist."""
    question: str = ""
    sub_questions: List[str] = field(default_factory=list)
    required_data_types: List[str] = field(default_factory=list)  # persons, projects, decisions, kpis...
    search_strategies: List[str] = field(default_factory=list)  # vector, graph, causal, temporal
    expected_answer_type: str = ""  # factoid, analytical, comparison, causal, temporal


@dataclass
class ResearchContext:
    """Контекст, собранный Navigator."""
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    snapshots: List[Dict[str, Any]] = field(default_factory=list)
    causal_chains: List[Dict[str, Any]] = field(default_factory=list)
    graph_paths: List[Dict[str, Any]] = field(default_factory=list)
    contradictions: List[str] = field(default_factory=list)
    total_sources: int = 0


@dataclass
class ResearchAnalysis:
    """Анализ от Researcher."""
    key_findings: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)  # [{fact, source, confidence}]
    contradictions: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)  # Что не нашли
    confidence: float = 0.0


@dataclass
class ResearchReport:
    """Финальный отчёт от Synthesizer."""
    answer: str = ""
    sections: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    research_steps: List[str] = field(default_factory=list)
    duration_ms: int = 0


class DeepResearchEngine:
    """
    4-агентный Deep Research Engine.

    Каждый агент — отдельный LLM вызов с специализированным промптом.
    Между агентами передаётся структурированный контекст.
    """

    def __init__(
        self,
        hybrid_search=None,
        llm_router=None,
        graph_builder=None,
    ):
        self.hybrid_search = hybrid_search
        self.llm = llm_router
        self.graph = graph_builder

    async def research(
        self,
        query: str,
        user_id: Optional[str] = None,
        extra_context: Optional[str] = None,
    ) -> ResearchReport:
        """
        Запустить полное исследование.

        Args:
            query: Исследовательский вопрос
            user_id: ID пользователя
            extra_context: Дополнительный контекст

        Returns:
            ResearchReport с ответом, источниками и шагами
        """
        start_time = time.time()
        steps = []

        # ═══════════════════════════════════════════════════════════
        # AGENT 1: STRATEGIST — Планирование исследования
        # ═══════════════════════════════════════════════════════════
        steps.append("🧠 Strategist: планирование исследования...")
        plan = await self._strategist(query, extra_context)
        steps.append(f"   План: {len(plan.sub_questions)} подвопросов, стратегии: {', '.join(plan.search_strategies)}")

        # ═══════════════════════════════════════════════════════════
        # AGENT 2: NAVIGATOR — Сбор данных по плану
        # ═══════════════════════════════════════════════════════════
        steps.append("🔍 Navigator: сбор данных...")
        context = await self._navigator(plan, user_id=user_id)
        steps.append(
            f"   Найдено: {context.total_sources} источников, "
            f"{len(context.snapshots)} снапшотов, "
            f"{len(context.causal_chains)} каузальных цепочек"
        )

        # ═══════════════════════════════════════════════════════════
        # AGENT 3: RESEARCHER — Анализ и верификация
        # ═══════════════════════════════════════════════════════════
        steps.append("🔬 Researcher: анализ данных...")
        analysis = await self._researcher(query, plan, context)
        steps.append(
            f"   Выводов: {len(analysis.key_findings)}, "
            f"противоречий: {len(analysis.contradictions)}, "
            f"пробелов: {len(analysis.gaps)}, "
            f"уверенность: {analysis.confidence:.0%}"
        )

        # ═══════════════════════════════════════════════════════════
        # AGENT 4: SYNTHESIZER — Финальный отчёт
        # ═══════════════════════════════════════════════════════════
        steps.append("✨ Synthesizer: генерация отчёта...")
        report = await self._synthesizer(query, plan, context, analysis)

        duration_ms = int((time.time() - start_time) * 1000)
        report.research_steps = steps
        report.duration_ms = duration_ms

        steps.append(f"✅ Исследование завершено за {duration_ms}ms")

        return report

    # ─── AGENT 1: STRATEGIST ──────────────────────────────────────

    async def _strategist(self, query: str, extra_context: Optional[str] = None) -> ResearchPlan:
        """Стратег: разбивает вопрос на подвопросы и определяет план поиска."""
        plan = ResearchPlan(question=query)

        if not self.llm:
            # Fallback без LLM
            plan.sub_questions = [query]
            plan.required_data_types = ["meetings", "decisions", "tasks", "persons", "projects"]
            plan.search_strategies = ["vector", "graph"]
            plan.expected_answer_type = "analytical"
            return plan

        prompt = f"""Ты стратег-аналитик. Разбей исследовательский вопрос на конкретные подвопросы
и определи план поиска данных в корпоративной базе знаний.

ВОПРОС: {query}

{f'ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ: {extra_context}' if extra_context else ''}

Доступные типы данных: persons, projects, products, teams, departments,
decisions, tasks, kpis, meetings, risks, knowledge, contradictions, causal_chains

Доступные стратегии поиска: vector (семантический), graph (по связям),
causal (причинно-следственные цепочки), temporal (во времени), snapshot (снапшоты сущностей)

Ответь в JSON:
{{
    "sub_questions": ["подвопрос 1", "подвопрос 2", ...],
    "required_data_types": ["тип1", "тип2", ...],
    "search_strategies": ["стратегия1", "стратегия2", ...],
    "expected_answer_type": "factoid|analytical|comparison|causal|temporal"
}}"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты стратег исследований. Отвечай JSON."
            )
            plan.sub_questions = result.get("sub_questions", [query])
            plan.required_data_types = result.get("required_data_types", ["meetings", "decisions"])
            plan.search_strategies = result.get("search_strategies", ["vector", "graph"])
            plan.expected_answer_type = result.get("expected_answer_type", "analytical")
        except Exception as e:
            logger.warning(f"Strategist LLM failed: {e}")
            plan.sub_questions = [query]
            plan.required_data_types = ["meetings", "decisions", "tasks", "persons", "projects"]
            plan.search_strategies = ["vector", "graph"]
            plan.expected_answer_type = "analytical"

        return plan

    # ─── AGENT 2: NAVIGATOR ──────────────────────────────────────

    async def _navigator(self, plan: ResearchPlan, user_id: Optional[str] = None) -> ResearchContext:
        """Навигатор: ищет данные по плану стратега — использует все слои поиска."""
        context = ResearchContext()

        # 1. Поиск по каждому подвопросу (vector + BM25 + graph)
        for sub_q in plan.sub_questions[:5]:
            if self.hybrid_search:
                try:
                    results = await self.hybrid_search.search(
                        query=sub_q,
                        top_k=20,
                    )
                    for r in results.results:
                        context.search_results.append({
                            "query": sub_q,
                            "text": r.text,
                            "score": r.score,
                            "type": r.metadata.get("type", "unknown"),
                            "source": r.metadata.get("meeting_id", r.doc_id),
                        })
                except Exception as e:
                    logger.warning(f"Navigator search failed for '{sub_q}': {e}")

        # 2. Каузальные цепочки
        if "causal" in plan.search_strategies and self.hybrid_search:
            try:
                chains = await self.hybrid_search.causal_chain_search(
                    query=plan.question, top_k=10, max_depth=3
                )
                context.causal_chains = chains
            except Exception as e:
                logger.warning(f"Causal search failed: {e}")

        # 3. Graph traversal — обход связей
        if "graph" in plan.search_strategies and self.graph:
            try:
                # Используем метод traversal если доступен
                if hasattr(self.graph, 'search_nodes'):
                    seed_results = await self.graph.search_nodes(query=plan.question, limit=5)

                    for seed in seed_results[:3]:
                        seed_id = seed.get("id")
                        if not seed_id:
                            continue
                        try:
                            rels = await self.graph.get_node_relationships(seed_id)
                            seed_name = seed.get("name", seed_id)

                            for direction in ("outgoing", "incoming"):
                                for rel in rels.get(direction, [])[:8]:
                                    other_id = rel.get("target" if direction == "outgoing" else "source", "")
                                    if not other_id:
                                        continue
                                    other = await self.graph.get_node_by_id(other_id)
                                    if not other:
                                        continue

                                    context.graph_paths.append({
                                        "from": seed_name,
                                        "relation": rel.get("relation", rel.get("type", "RELATED")),
                                        "to": other.get("name", other_id),
                                        "to_type": other.get("_label", ""),
                                        "description": other.get("description", "")[:200],
                                    })
                        except Exception:
                            logger.debug("suppressed exception", exc_info=True)
            except Exception as e:
                logger.warning(f"Graph traversal failed: {e}")

        # 4. Снапшоты
        if "snapshot" in plan.search_strategies and self.graph:
            try:
                from backend.core.sleep.enhanced_snapshot import get_enhanced_snapshot_generator
                snap_gen = get_enhanced_snapshot_generator(self.graph, user_id=user_id)

                company = await snap_gen.get_company_snapshot()
                if company:
                    context.snapshots.append({
                        "type": "company",
                        "text": company.to_text(max_length=1500),
                    })
            except Exception as e:
                logger.warning(f"Snapshot loading failed: {e}")

        # 5. Паттерны и аналитика (для PATTERN/RECOMMENDATION запросов)
        if any(s in plan.search_strategies for s in ("pattern", "analytical")) and self.graph:
            try:
                # Заблокированные задачи
                tasks = await self.graph.find_nodes_by_label(label="Task", limit=100)
                blocked = [t for t in tasks if t.get("status") in ("blocked", "on_hold", "stuck")]
                if blocked:
                    for bt in blocked[:5]:
                        context.search_results.append({
                            "query": "blocked tasks",
                            "text": f"Заблокированная задача: {bt.get('name', bt.get('title', 'N/A'))} — {bt.get('description', '')}",
                            "score": 0.8,
                            "type": "Pattern:BlockedTask",
                            "source": "graph_analysis",
                        })

                # Риски
                risks = await self.graph.find_nodes_by_label(label="Risk", limit=30)
                for r in risks[:5]:
                    context.search_results.append({
                        "query": "risks",
                        "text": f"Риск: {r.get('description', r.get('name', 'N/A'))}. Вероятность: {r.get('probability', '?')}, Влияние: {r.get('impact', '?')}",
                        "score": 0.7,
                        "type": "Pattern:Risk",
                        "source": "graph_analysis",
                    })

                # Противоречия
                contradictions = await self.graph.find_nodes_by_label(label="Contradiction", limit=10)
                for c in contradictions[:3]:
                    context.contradictions.append(
                        f"БЫЛО: {c.get('existing_fact', '?')} → СТАЛО: {c.get('new_fact', '?')}"
                    )
            except Exception as e:
                logger.warning(f"Pattern analysis failed: {e}")

        # 6. Consulting-level analysis (для ANALYTICAL/RECOMMENDATION)
        if any(s in plan.search_strategies for s in ("pattern", "analytical", "snapshot")) and self.graph:
            try:
                from backend.core.search.consulting_analyzer import get_consulting_analyzer
                analyzer = get_consulting_analyzer(self.graph, self.llm)
                consulting_results = await analyzer.analyze(plan.question)

                for cr in consulting_results:
                    for finding in cr.findings[:5]:
                        context.search_results.append({
                            "query": f"consulting:{cr.framework}",
                            "text": finding,
                            "score": 0.85,
                            "type": f"Consulting:{cr.framework}",
                            "source": "consulting_analysis",
                        })
                    for rec in cr.recommendations[:3]:
                        context.search_results.append({
                            "query": f"recommendation:{cr.framework}",
                            "text": rec,
                            "score": 0.9,
                            "type": f"Recommendation:{cr.framework}",
                            "source": "consulting_analysis",
                        })
            except Exception as e:
                logger.warning(f"Consulting analysis failed: {e}")

        context.total_sources = len(context.search_results)
        return context

    # ─── AGENT 3: RESEARCHER ──────────────────────────────────────

    async def _researcher(
        self, query: str, plan: ResearchPlan, context: ResearchContext
    ) -> ResearchAnalysis:
        """
        Исследователь: анализирует данные, находит противоречия.

        Включает Reasoning Chains:
        1. Hypothesis Generation — генерация гипотез по данным
        2. Evidence Scoring — оценка силы доказательств
        3. Counter-argument Search — поиск опровержений
        """
        analysis = ResearchAnalysis()

        if not self.llm:
            # Без LLM — просто извлекаем факты
            analysis.key_findings = [r["text"][:200] for r in context.search_results[:5]]
            analysis.confidence = 0.5
            return analysis

        # ═══ Reasoning Chains — аналитическое мышление ═══
        reasoning_conclusion = ""
        reasoning_hypotheses_text = ""
        try:
            from backend.core.search.reasoning_chains import get_reasoning_chain_engine
            reasoning_engine = get_reasoning_chain_engine(
                hybrid_search=self.hybrid_search,
                llm_router=self.llm,
                graph_builder=self.graph
            )

            # Собираем текстовый контекст как подсказку для генерации гипотез
            hint = "\n".join([r["text"][:200] for r in context.search_results[:5]])

            reasoning_result = await reasoning_engine.reason(
                query=query,
                context=hint,
                max_hypotheses=3
            )

            if reasoning_result.conclusion:
                reasoning_conclusion = reasoning_result.conclusion

            # Добавляем reasoning в findings
            for h in reasoning_result.hypotheses:
                status = {"supported": "✅", "refuted": "❌", "inconclusive": "❓"}.get(h.status, "❓")
                reasoning_hypotheses_text += f"\n{status} Гипотеза: {h.text} [{h.confidence:.0%}]"
                for e in h.supporting_evidence[:2]:
                    reasoning_hypotheses_text += f"\n  + {e.text[:150]}"
                for e in h.counter_evidence[:2]:
                    reasoning_hypotheses_text += f"\n  - {e.text[:150]}"

        except Exception as e:
            logger.warning(f"Reasoning chains in researcher failed: {e}")

        # ═══ Формируем контекст для LLM ═══
        context_text = ""
        for i, r in enumerate(context.search_results[:15]):
            context_text += f"\n[Источник {i+1}] ({r['type']}): {r['text'][:300]}"

        if context.causal_chains:
            context_text += "\n\n=== КАУЗАЛЬНЫЕ ЦЕПОЧКИ ===\n"
            for chain in context.causal_chains[:3]:
                context_text += chain.get("explanation", "") + "\n"

        if context.graph_paths:
            context_text += "\n\n=== СВЯЗИ (ГРАФ) ===\n"
            for gp in context.graph_paths[:10]:
                context_text += f"  {gp['from']} —[{gp['relation']}]→ {gp['to']} ({gp['to_type']})"
                if gp.get('description'):
                    context_text += f": {gp['description']}"
                context_text += "\n"

        if context.snapshots:
            context_text += "\n\n=== СНАПШОТЫ ===\n"
            for snap in context.snapshots[:2]:
                context_text += snap["text"] + "\n"

        if context.contradictions:
            context_text += "\n\n=== ПРОТИВОРЕЧИЯ ===\n"
            for c in context.contradictions[:5]:
                context_text += f"  ⚠️ {c}\n"

        # Reasoning Chains
        if reasoning_hypotheses_text:
            context_text += f"\n\n=== ПРОВЕРКА ГИПОТЕЗ (Reasoning Chains) ===\n{reasoning_hypotheses_text}"
        if reasoning_conclusion:
            context_text += f"\n\n=== ВЫВОД REASONING ===\n{reasoning_conclusion}"

        prompt = f"""Ты аналитик-исследователь. Проанализируй собранные данные и ответь на вопрос.

ВОПРОС: {query}

ПОДВОПРОСЫ: {', '.join(plan.sub_questions)}

СОБРАННЫЕ ДАННЫЕ:
{context_text[:10000]}

Определи:
1. Ключевые выводы (факты, подтверждённые данными)
2. Противоречия (если одни источники говорят одно, другие — другое)
3. Пробелы (что не удалось найти)
4. Уверенность в ответе (0.0-1.0)

ВАЖНО: Учитывай результаты проверки гипотез — подтверждённые гипотезы (✅) имеют больший вес,
опровергнутые (❌) НЕ ДОЛЖНЫ входить в выводы. Неопределённые (❓) отметь как требующие проверки.

ТОЛЬКО ИЗ ДАННЫХ: key_findings и evidence.fact бери ТОЛЬКО из собранных данных
выше, не добавляй фактов из общих знаний и не выдумывай. evidence.source —
реальный источник из данных, не сочиняй. Нет данных по подвопросу → в gaps, а не
в выводы. Лучше честный пробел, чем выдуманный вывод.

Ответь JSON:
{{
    "key_findings": ["вывод 1", "вывод 2", ...],
    "evidence": [{{"fact": "факт", "source": "источник", "confidence": 0.9}}],
    "contradictions": ["противоречие 1", ...],
    "gaps": ["пробел 1", ...],
    "confidence": 0.8
}}"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты аналитик. Проверяй факты, ищи противоречия. Учитывай результаты проверки гипотез. Отвечай JSON."
            )
            analysis.key_findings = result.get("key_findings", [])
            analysis.evidence = result.get("evidence", [])
            analysis.contradictions = result.get("contradictions", [])
            analysis.gaps = result.get("gaps", [])
            analysis.confidence = float(result.get("confidence", 0.5))
        except Exception as e:
            logger.warning(f"Researcher LLM failed: {e}")
            analysis.key_findings = [r["text"][:200] for r in context.search_results[:5]]
            analysis.confidence = 0.4

        return analysis

    # ─── AGENT 4: SYNTHESIZER ────────────────────────────────────

    async def _synthesizer(
        self,
        query: str,
        plan: ResearchPlan,
        context: ResearchContext,
        analysis: ResearchAnalysis
    ) -> ResearchReport:
        """Синтезатор: формирует финальный отчёт."""
        report = ResearchReport()

        if not self.llm:
            # Без LLM — простой вывод
            parts = [f"# Исследование: {query}\n"]
            for f in analysis.key_findings[:5]:
                parts.append(f"- {f}")
            report.answer = "\n".join(parts)
            report.confidence = analysis.confidence
            return report

        # Собираем все данные для synthesis
        findings_text = "\n".join([f"- {f}" for f in analysis.key_findings])
        contradictions_text = "\n".join([f"- ⚠️ {c}" for c in analysis.contradictions]) if analysis.contradictions else "Нет"
        gaps_text = "\n".join([f"- ❓ {g}" for g in analysis.gaps]) if analysis.gaps else "Нет"

        evidence_text = ""
        for ev in analysis.evidence[:10]:
            evidence_text += f"\n  📊 {ev.get('fact', '')} (источник: {ev.get('source', 'N/A')}, уверенность: {ev.get('confidence', '?')})"

        prompt = f"""Ты эксперт по синтезу информации. Сформируй структурированный отчёт.

ВОПРОС: {query}

КЛЮЧЕВЫЕ ВЫВОДЫ:
{findings_text}

ДОКАЗАТЕЛЬНАЯ БАЗА:
{evidence_text}

ПРОТИВОРЕЧИЯ:
{contradictions_text}

ПРОБЕЛЫ В ДАННЫХ:
{gaps_text}

УВЕРЕННОСТЬ АНАЛИТИКА: {analysis.confidence:.0%}

Напиши развёрнутый структурированный ответ в формате Markdown.
Используй разделы: Краткий ответ, Детальный анализ, Источники и доказательства,
Противоречия (если есть), Рекомендации.
Маркируй факты: 📊 (данные), 🤖 (предположения), 💬 (цитаты из встреч)."""

        try:
            result = await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты автор аналитических отчётов. Пиши структурировано, с цитатами и маркировкой.",
                max_tokens=4000,
            )
            report.answer = result
        except Exception as e:
            logger.warning(f"Synthesizer LLM failed: {e}")
            parts = [f"# Исследование: {query}\n"]
            for f in analysis.key_findings[:5]:
                parts.append(f"- {f}")
            if analysis.contradictions:
                parts.append("\n## Противоречия")
                for c in analysis.contradictions:
                    parts.append(f"- ⚠️ {c}")
            report.answer = "\n".join(parts)

        # Sources
        seen_sources = set()
        for r in context.search_results[:10]:
            src = r.get("source", "")
            if src and src not in seen_sources:
                seen_sources.add(src)
                report.sources.append({
                    "id": src,
                    "type": r.get("type", "unknown"),
                    "relevance": r.get("score", 0),
                })

        report.confidence = analysis.confidence
        return report


# Singleton
_deep_research_instance: Optional[DeepResearchEngine] = None


def get_deep_research_engine(
    hybrid_search=None,
    llm_router=None,
    graph_builder=None,
) -> DeepResearchEngine:
    """Получить экземпляр DeepResearchEngine."""
    global _deep_research_instance
    if _deep_research_instance is None:
        _deep_research_instance = DeepResearchEngine(
            hybrid_search=hybrid_search,
            llm_router=llm_router,
            graph_builder=graph_builder,
        )
    else:
        if hybrid_search:
            _deep_research_instance.hybrid_search = hybrid_search
        if llm_router:
            _deep_research_instance.llm = llm_router
        if graph_builder:
            _deep_research_instance.graph = graph_builder
    return _deep_research_instance
