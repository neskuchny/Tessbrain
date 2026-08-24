# -*- coding: utf-8 -*-
"""
Reasoning Chains — аналитическое мышление поверх данных.

Превращает систему из "ищу текст" в "думаю как аналитик":

1. Hypothesis Generation — генерация гипотез по запросу, затем целенаправленный поиск
   подтверждений/опровержений каждой гипотезы
2. Evidence Scoring — оценка силы каждого доказательства (свежесть, уверенность,
   частота упоминаний, тип источника)
3. Counter-Argument Search — активный поиск опровержений для сделанных выводов,
   обеспечивает объективность анализа

Используется в:
- Enhanced Search (standard/deep mode) — для ANALYTICAL/RECOMMENDATION/CAUSAL intents
- Deep Research Engine — в Researcher агенте
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════

@dataclass
class Hypothesis:
    """Гипотеза, сгенерированная из запроса."""
    id: int = 0
    text: str = ""
    search_query: str = ""      # Запрос для проверки гипотезы
    counter_query: str = ""     # Запрос для поиска опровержения
    status: str = "pending"     # pending, supported, refuted, inconclusive
    supporting_evidence: List['Evidence'] = field(default_factory=list)
    counter_evidence: List['Evidence'] = field(default_factory=list)
    confidence: float = 0.0     # 0.0-1.0 итоговая уверенность


@dataclass
class Evidence:
    """Одно доказательство за или против гипотезы."""
    text: str = ""
    source: str = ""
    source_type: str = ""       # meeting, decision, task, kpi, risk, entity
    score: float = 0.0          # Evidence score (0.0-1.0)
    freshness: float = 0.0      # Свежесть (0.0-1.0, 1.0 = сегодня)
    frequency: float = 0.0      # Частота упоминаний нормализованная
    confidence: float = 0.0     # Уверенность источника
    direction: str = "supports" # supports | refutes | neutral


@dataclass
class ReasoningResult:
    """Результат reasoning chain."""
    hypotheses: List[Hypothesis] = field(default_factory=list)
    conclusion: str = ""
    overall_confidence: float = 0.0
    reasoning_steps: List[str] = field(default_factory=list)
    processing_time_ms: int = 0


class ReasoningChainEngine:
    """
    Движок аналитических reasoning chains.

    Паттерн работы:
    1. Query → LLM генерирует 2-4 гипотезы
    2. Каждая гипотеза → целенаправленный поиск (подтверждения + опровержения)
    3. Результаты → Evidence Scoring (оценка каждого доказательства)
    4. Counter-argument search → активный поиск опровержений
    5. Все доказательства → LLM формирует вывод с уровнем уверенности
    """

    def __init__(self, hybrid_search=None, llm_router=None, graph_builder=None):
        self.hybrid_search = hybrid_search
        self.llm = llm_router
        self.graph = graph_builder

    async def reason(
        self,
        query: str,
        context: Optional[str] = None,
        max_hypotheses: int = 3
    ) -> ReasoningResult:
        """
        Запустить полный reasoning chain.

        Args:
            query: Исследовательский вопрос
            context: Дополнительный контекст (уже найденные данные)
            max_hypotheses: Макс. количество гипотез
        """
        start = time.time()
        result = ReasoningResult()

        # ═══ STEP 1: Hypothesis Generation ═══
        result.reasoning_steps.append("🧪 Генерация гипотез...")
        hypotheses = await self._generate_hypotheses(query, context, max_hypotheses)
        result.hypotheses = hypotheses
        result.reasoning_steps.append(f"   Сгенерировано {len(hypotheses)} гипотез")

        # ═══ STEP 2: Evidence Search + Scoring ═══
        for h in hypotheses:
            result.reasoning_steps.append(f"🔍 Проверка гипотезы #{h.id}: {h.text[:80]}...")

            # 2a. Поиск подтверждений
            supporting = await self._search_evidence(h.search_query, "supports")
            h.supporting_evidence = supporting

            # 2b. Counter-argument search — поиск опровержений
            counter = await self._search_evidence(h.counter_query, "refutes")
            h.counter_evidence = counter

            # 2c. Evidence scoring + итоговая уверенность в гипотезе
            h.confidence = self._calculate_hypothesis_confidence(h)

            if h.confidence >= 0.7:
                h.status = "supported"
            elif h.confidence <= 0.3:
                h.status = "refuted"
            else:
                h.status = "inconclusive"

            result.reasoning_steps.append(
                f"   За: {len(supporting)}, Против: {len(counter)}, "
                f"Уверенность: {h.confidence:.0%}, Статус: {h.status}"
            )

        # ═══ STEP 3: Conclusion ═══
        result.reasoning_steps.append("📋 Формирование вывода...")
        result.conclusion = await self._synthesize_conclusion(query, hypotheses)

        # Overall confidence
        if hypotheses:
            [h for h in hypotheses if h.status == "supported"]
            result.overall_confidence = (
                sum(h.confidence for h in hypotheses) / len(hypotheses)
            )

        result.processing_time_ms = int((time.time() - start) * 1000)
        result.reasoning_steps.append(
            f"✅ Reasoning завершён за {result.processing_time_ms}ms, "
            f"уверенность: {result.overall_confidence:.0%}"
        )

        return result

    # ─── STEP 1: Hypothesis Generation ────────────────────────

    async def _generate_hypotheses(
        self, query: str, context: Optional[str], max_count: int
    ) -> List[Hypothesis]:
        """
        Генерация гипотез — LLM анализирует вопрос и предлагает
        возможные ответы, которые нужно проверить данными.
        """
        if not self.llm:
            # Fallback: одна гипотеза = сам запрос
            return [Hypothesis(
                id=1, text=query,
                search_query=query,
                counter_query=f"опровержение: {query}"
            )]

        ctx_hint = ""
        if context:
            ctx_hint = f"\n\nУже найденный контекст (используй для более точных гипотез):\n{context[:2000]}"

        prompt = f"""Ты аналитик. На основе вопроса сгенерируй {max_count} ГИПОТЕЗЫ —
возможные ответы, которые нужно ПРОВЕРИТЬ данными из корпоративной базы знаний.

ВОПРОС: {query}
{ctx_hint}

Для каждой гипотезы определи:
- text: сама гипотеза (предполагаемый ответ)
- search_query: запрос для ПОИСКА ПОДТВЕРЖДЕНИЙ (конкретный, для поиска в базе встреч и решений)
- counter_query: запрос для ПОИСКА ОПРОВЕРЖЕНИЙ (ищем данные ПРОТИВ гипотезы)

ВАЖНО: гипотезы должны быть ПРОВЕРЯЕМЫЕ данными, а не абстрактные.

Пример для "каких сотрудников не хватает?":
- Гипотеза: "Не хватает разработчиков — задачи по разработке постоянно буксуют"
  search_query: "задачи разработка blocked проблемы задержки"
  counter_query: "разработка успешно завершена вовремя прогресс"

Ответь JSON:
{{"hypotheses": [
  {{"text": "...", "search_query": "...", "counter_query": "..."}},
  ...
]}}"""

        try:
            result = await self.llm.generate_json(
                prompt=prompt,
                system_prompt="Ты аналитик-гипотезогенератор. Отвечай JSON."
            )

            hypotheses = []
            for i, h_data in enumerate(result.get("hypotheses", [])[:max_count]):
                hypotheses.append(Hypothesis(
                    id=i + 1,
                    text=h_data.get("text", ""),
                    search_query=h_data.get("search_query", query),
                    counter_query=h_data.get("counter_query", f"не {query}"),
                ))

            return hypotheses if hypotheses else [
                Hypothesis(id=1, text=query, search_query=query, counter_query=f"опровержение: {query}")
            ]

        except Exception as e:
            logger.warning(f"Hypothesis generation failed: {e}")
            return [Hypothesis(id=1, text=query, search_query=query, counter_query=f"опровержение: {query}")]

    # ─── STEP 2: Evidence Search + Scoring ─────────────────────

    async def _search_evidence(self, search_query: str, direction: str) -> List[Evidence]:
        """Поиск доказательств (за или против)."""
        evidence_list = []

        if not self.hybrid_search:
            return evidence_list

        try:
            results = await self.hybrid_search.search(
                query=search_query,
                top_k=10,
            )

            for r in results.results[:8]:
                ev = Evidence(
                    text=r.text[:500] if r.text else "",
                    source=r.doc_id,
                    source_type=r.metadata.get("type", "unknown"),
                    direction=direction,
                )

                # Evidence Scoring
                ev.score = self._score_evidence(r, ev)

                if ev.score > 0.1 and ev.text:
                    evidence_list.append(ev)

        except Exception as e:
            logger.warning(f"Evidence search failed for '{search_query[:50]}': {e}")

        # Сортируем по score
        evidence_list.sort(key=lambda e: e.score, reverse=True)
        return evidence_list[:5]

    def _score_evidence(self, search_result: Any, evidence: Evidence) -> float:
        """
        Evidence Scoring — оценка силы доказательства.

        Факторы:
        1. Search relevance (base_score) — насколько результат релевантен запросу
        2. Source type weight — решения весят больше чем просто упоминания
        3. Frequency — часто упоминаемые факты надёжнее
        4. Node weight — веса из графа (частота, эмоциональная значимость)
        5. Freshness — свежие данные ценнее
        """
        metadata = search_result.metadata or {}

        # 1. Base relevance score (от hybrid search)
        base_score = search_result.score or 0.5

        # 2. Source type multiplier
        type_weights = {
            "Decision": 1.5,      # Решения — самые сильные доказательства
            "KPI": 1.4,           # Метрики — числовые факты
            "Task": 1.2,          # Задачи — конкретные действия
            "Risk": 1.3,          # Риски — важные предупреждения
            "Meeting": 1.0,       # Встречи — обсуждения
            "Person": 0.8,        # Персоны — косвенные
            "Entity": 0.9,        # Сущности — упоминания
            "Knowledge": 1.1,     # Знания — экспертиза
            "Contradiction": 1.3, # Противоречия — важные сигналы
            "Pattern": 1.2,       # Паттерны — закономерности
        }
        source_type = metadata.get("type", "")
        type_multiplier = type_weights.get(source_type, 1.0)

        # 3. Frequency
        freq = metadata.get("frequency_score", 0)
        freq_bonus = min(0.3, freq * 0.05)  # До +0.3 за частоту

        # 4. Node weight
        node_weight = metadata.get("weight", 1.0)
        weight_bonus = (node_weight - 1.0) * 0.1  # До +0.1

        # 5. Emotional salience (эмоционально значимые данные надёжнее для org анализа)
        emotion = metadata.get("emotional_salience", 0)
        emotion_bonus = emotion * 0.05

        # Итоговый score
        total = base_score * type_multiplier + freq_bonus + weight_bonus + emotion_bonus

        # Нормализуем 0-1
        total = max(0.0, min(1.0, total))

        # Сохраняем компоненты
        evidence.freshness = 0.5  # TODO: calculate from created_at
        evidence.frequency = min(1.0, freq / 10.0)
        evidence.confidence = total

        return total

    def _calculate_hypothesis_confidence(self, hypothesis: Hypothesis) -> float:
        """
        Вычислить итоговую уверенность в гипотезе.

        Формула:
        confidence = (sum(supporting_scores) - sum(counter_scores) * 0.8) / normalizer

        Counter-evidence имеет множитель 0.8 (не полный вес), потому что
        опровержения часто косвенные.
        """
        if not hypothesis.supporting_evidence and not hypothesis.counter_evidence:
            return 0.5  # Нет данных — неопределённость

        support_score = sum(e.score for e in hypothesis.supporting_evidence)
        counter_score = sum(e.score for e in hypothesis.counter_evidence) * 0.8

        # Количество источников тоже важно
        support_count = len(hypothesis.supporting_evidence)
        counter_count = len(hypothesis.counter_evidence)

        total_evidence = support_count + counter_count
        if total_evidence == 0:
            return 0.5

        # Базовая уверенность от scores
        score_diff = support_score - counter_score
        normalizer = support_score + counter_score
        if normalizer > 0:
            score_confidence = (score_diff / normalizer + 1.0) / 2.0  # Нормализуем в 0-1
        else:
            score_confidence = 0.5

        # Корректируем по количеству источников
        # Больше источников = выше уверенность (но с diminishing returns)
        count_factor = min(1.0, total_evidence / 5.0)

        # Итого: balance between score-based и count-based
        confidence = score_confidence * 0.7 + (support_count / max(total_evidence, 1)) * 0.3
        confidence *= count_factor  # Снижаем если мало данных

        return max(0.0, min(1.0, confidence))

    # ─── STEP 3: Conclusion Synthesis ──────────────────────────

    async def _synthesize_conclusion(
        self, query: str, hypotheses: List[Hypothesis]
    ) -> str:
        """Синтез вывода из всех гипотез и доказательств."""
        if not self.llm:
            # Fallback: простой вывод
            supported = [h for h in hypotheses if h.status == "supported"]
            if supported:
                return "\n".join([f"✅ {h.text} (уверенность: {h.confidence:.0%})" for h in supported])
            return "Недостаточно данных для уверенного вывода."

        # Формируем контекст для LLM
        hyp_texts = []
        for h in hypotheses:
            status_icon = {"supported": "✅", "refuted": "❌", "inconclusive": "❓"}.get(h.status, "❓")

            hyp_text = f"\n{status_icon} Гипотеза #{h.id}: {h.text}"
            hyp_text += f"\n   Статус: {h.status}, Уверенность: {h.confidence:.0%}"

            if h.supporting_evidence:
                hyp_text += f"\n   Подтверждения ({len(h.supporting_evidence)}):"
                for e in h.supporting_evidence[:3]:
                    hyp_text += f"\n     + [{e.source_type}] {e.text[:200]} (score: {e.score:.2f})"

            if h.counter_evidence:
                hyp_text += f"\n   Опровержения ({len(h.counter_evidence)}):"
                for e in h.counter_evidence[:3]:
                    hyp_text += f"\n     - [{e.source_type}] {e.text[:200]} (score: {e.score:.2f})"

            hyp_texts.append(hyp_text)

        prompt = f"""Ты аналитик. На основе проверки гипотез сформулируй ОБОСНОВАННЫЙ ВЫВОД.

ВОПРОС: {query}

РЕЗУЛЬТАТЫ ПРОВЕРКИ ГИПОТЕЗ:
{"".join(hyp_texts)}

ПРАВИЛА:
1. Начни с КРАТКОГО ОТВЕТА (1-2 предложения)
2. Затем ОБОСНОВАНИЕ — почему именно такой вывод
3. Укажи подтверждённые гипотезы (✅) и опровергнутые (❌)
4. Если данных недостаточно — ЧЕСТНО СКАЖИ, что не хватает
5. Маркируй: 📊 (подтверждено данными), 🤖 (предположение), ⚠️ (есть опровержения)"""

        try:
            return await self.llm.generate(
                prompt=prompt,
                system_prompt="Ты аналитик. Делай выводы ТОЛЬКО на основе данных. Будь объективен.",
                max_tokens=1500,
            )
        except Exception as e:
            logger.warning(f"Conclusion synthesis failed: {e}")
            supported = [h for h in hypotheses if h.status == "supported"]
            if supported:
                return "\n".join([f"✅ {h.text} (уверенность: {h.confidence:.0%})" for h in supported])
            return "Недостаточно данных для уверенного вывода."

    def format_for_context(self, result: ReasoningResult) -> str:
        """Форматировать результат reasoning для включения в контекст поиска."""
        parts = ["\n=== АНАЛИТИЧЕСКИЕ РАССУЖДЕНИЯ (Reasoning Chain) ===\n"]

        for h in result.hypotheses:
            status_icon = {"supported": "✅", "refuted": "❌", "inconclusive": "❓"}.get(h.status, "❓")
            parts.append(f"{status_icon} Гипотеза: {h.text} [{h.confidence:.0%}]")

            for e in h.supporting_evidence[:2]:
                parts.append(f"  + {e.text[:150]}")
            for e in h.counter_evidence[:2]:
                parts.append(f"  - {e.text[:150]}")

        if result.conclusion:
            parts.append(f"\n📋 ВЫВОД:\n{result.conclusion}")

        return "\n".join(parts)


# Singleton
_reasoning_instance: Optional[ReasoningChainEngine] = None


def get_reasoning_chain_engine(
    hybrid_search=None, llm_router=None, graph_builder=None
) -> ReasoningChainEngine:
    """Получить экземпляр ReasoningChainEngine."""
    global _reasoning_instance
    if _reasoning_instance is None:
        _reasoning_instance = ReasoningChainEngine(hybrid_search, llm_router, graph_builder)
    else:
        if hybrid_search:
            _reasoning_instance.hybrid_search = hybrid_search
        if llm_router:
            _reasoning_instance.llm = llm_router
        if graph_builder:
            _reasoning_instance.graph = graph_builder
    return _reasoning_instance
