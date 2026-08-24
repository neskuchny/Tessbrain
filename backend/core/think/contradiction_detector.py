# -*- coding: utf-8 -*-
"""
TESSENT BRAIN — Contradiction Detector (Step 5)
================================================

Обнаруживает противоречия между новыми и существующими фактами.

Типы противоречий:
1. DIRECT      — "бюджет 100M" vs "бюджет 200M"
2. TEMPORAL    — "Иван PM" (янв) vs "Мария PM" (фев) — НЕ противоречие!
3. SCOPE       — "проект успешен" (в целом) vs "провал" (один спринт)
4. PERSPECTIVE — "хороший результат" (PM) vs "плохой" (клиент)

При обнаружении создаёт узел Contradiction в графе.

Tier 3+ (INTELLIGENCE)
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class Contradiction:
    """Обнаруженное противоречие."""
    contradiction_id: str = ""
    new_fact: str = ""
    existing_fact: str = ""
    contradiction_type: str = "direct"  # direct | temporal | scope | perspective
    severity: str = "medium"            # low | medium | high
    resolution_suggestion: str = ""     # supersede | coexist | needs_verification
    new_source: str = ""                # meeting_id нового факта
    existing_source: str = ""           # meeting_id старого факта
    entity_id: str = ""                 # К какой сущности относится
    confidence: float = 0.7


class ContradictionDetector:
    """
    Обнаруживает противоречия при добавлении новых фактов в граф.

    Вызывается после extraction — проверяет новые KPI, решения, факты
    на противоречия с существующими данными.
    """

    def __init__(self, graph_builder=None, llm_client=None):
        self.graph = graph_builder
        self.llm = llm_client

    async def _get_llm(self):
        if self.llm:
            return self.llm
        try:
            from backend.core.llm.gemini_client import get_gemini_client
            self.llm = get_gemini_client()
            return self.llm
        except Exception:
            return None

    async def check_new_data(
        self,
        meeting_id: str,
        agent_results: Dict[str, Any],
        user_id: str = "",
    ) -> List[Contradiction]:
        """
        Проверить новые данные встречи на противоречия с существующими.

        Args:
            meeting_id: ID текущей встречи
            agent_results: Результаты агентов (decisions, tasks, kpis...)
            user_id: ID пользователя

        Returns:
            Список обнаруженных противоречий
        """
        contradictions = []

        if not self.graph:
            return contradictions

        # Проверяем KPI на противоречия (числовые значения)
        kpi_contradictions = await self._check_kpi_contradictions(
            agent_results.get("kpis", []), meeting_id
        )
        contradictions.extend(kpi_contradictions)

        # Проверяем решения (новое решение может противоречить старому)
        decision_contradictions = await self._check_decision_contradictions(
            agent_results.get("decisions", []), meeting_id
        )
        contradictions.extend(decision_contradictions)

        if contradictions:
            logger.info(f"⚠️ Found {len(contradictions)} contradictions in meeting {meeting_id}")

        return contradictions

    async def _check_kpi_contradictions(
        self,
        new_kpis: List[Dict],
        meeting_id: str,
    ) -> List[Contradiction]:
        """Проверить KPI на числовые противоречия."""
        contradictions = []

        if not self.graph or not self.graph.use_networkx:
            return contradictions

        try:
            # Собираем существующие KPI из графа
            existing_kpis = {}
            for node_id, data in self.graph.nx_graph.nodes(data=True):
                if data.get("_label") == "KPI" and data.get("name"):
                    existing_kpis[data["name"].lower()] = {
                        "node_id": node_id,
                        "value": data.get("current_value", ""),
                        "meeting_id": data.get("meeting_id", ""),
                    }

            for kpi in new_kpis:
                name = (kpi.get("name") or "").lower()
                value = kpi.get("current_value") or kpi.get("value", "")

                if name in existing_kpis:
                    old = existing_kpis[name]
                    old_value = old.get("value", "")

                    # Если значения существенно различаются
                    if old_value and value and str(old_value) != str(value):
                        contradictions.append(Contradiction(
                            contradiction_id=f"contradiction_{meeting_id}_{name}",
                            new_fact=f"KPI '{name}' = {value}",
                            existing_fact=f"KPI '{name}' = {old_value}",
                            contradiction_type="temporal",  # Скорее всего обновление
                            severity="low",
                            resolution_suggestion="supersede",  # Новое значение заменяет старое
                            new_source=meeting_id,
                            existing_source=old.get("meeting_id", ""),
                            entity_id=old.get("node_id", ""),
                        ))
        except Exception as e:
            logger.warning(f"KPI contradiction check failed: {e}")

        return contradictions

    async def _check_decision_contradictions(
        self,
        new_decisions: List[Dict],
        meeting_id: str,
    ) -> List[Contradiction]:
        """Проверить решения на противоречия с предыдущими."""
        contradictions = []

        if not self.graph or not self.graph.use_networkx:
            return contradictions

        llm = await self._get_llm()
        if not llm:
            return contradictions

        try:
            # Собираем существующие решения
            existing_decisions = []
            for node_id, data in self.graph.nx_graph.nodes(data=True):
                if data.get("_label") == "Decision" and data.get("summary"):
                    existing_decisions.append({
                        "node_id": node_id,
                        "summary": data["summary"],
                        "category": data.get("decision_category", ""),
                        "meeting_id": data.get("meeting_id", ""),
                    })

            if not existing_decisions or not new_decisions:
                return contradictions

            # Проверяем через LLM только если есть решения в тех же категориях
            for new_d in new_decisions:
                new_summary = new_d.get("decision_summary") or new_d.get("summary", "")
                new_category = new_d.get("decision_category") or new_d.get("category", "")

                if not new_summary:
                    continue

                # Ищем решения в похожих категориях
                similar = [d for d in existing_decisions
                           if d.get("category") == new_category and d.get("category")]

                if not similar:
                    continue

                # Берём последние 5 решений из этой категории
                similar_texts = "\n".join(
                    f"- {d['summary']}" for d in similar[-5:]
                )

                prompt = f"""Сравни новое решение с предыдущими. Есть ли противоречие?

Новое решение: {new_summary}

Предыдущие решения в категории "{new_category}":
{similar_texts}

Если есть противоречие — ответь JSON:
{{"contradiction": true, "type": "direct|temporal|scope", "explanation": "...", "severity": "low|medium|high"}}

Если нет — ответь:
{{"contradiction": false}}"""

                try:
                    response = await llm.generate(prompt, temperature=0.1)
                    result = self._parse_json(response)

                    if result and result.get("contradiction"):
                        contradictions.append(Contradiction(
                            contradiction_id=f"contradiction_{meeting_id}_{len(contradictions)}",
                            new_fact=new_summary,
                            existing_fact=similar_texts[:200],
                            contradiction_type=result.get("type", "direct"),
                            severity=result.get("severity", "medium"),
                            resolution_suggestion="needs_verification",
                            new_source=meeting_id,
                        ))
                except Exception:
                    pass  # Не критично

        except Exception as e:
            logger.warning(f"Decision contradiction check failed: {e}")

        return contradictions

    def _parse_json(self, response: str) -> Any:
        if not response:
            return None
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
