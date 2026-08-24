# -*- coding: utf-8 -*-
"""
Intelligence Weights Agent - Приоритизация задач и решений

Анализирует:
1. Бизнес-критичность задач
2. Срочность и дедлайны
3. Зависимости и блокеры
4. Ресурсные ограничения
5. Стратегическую важность

Адаптировано из tess_old/module_13_intelligence_weights/
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class WeightedItem:
    """Взвешенный элемент"""
    id: str
    name: str
    item_type: str  # task/decision/project/risk
    priority_score: float  # 0.0-1.0
    urgency_score: float  # 0.0-1.0
    importance_score: float  # 0.0-1.0
    final_weight: float  # 0.0-1.0
    priority_level: str  # critical/high/medium/low
    factors: Dict[str, float] = field(default_factory=dict)
    reasoning: str = ""


class IntelligenceWeights:
    """
    Агент приоритизации задач и решений.

    Возможности:
    - Расчёт весов важности
    - Приоритизация задач
    - Выявление критических элементов
    - Рекомендации по фокусу
    """

    # Веса для различных факторов
    WEIGHT_FACTORS = {
        "deadline_proximity": 0.20,      # Близость дедлайна
        "business_impact": 0.25,         # Влияние на бизнес
        "strategic_alignment": 0.15,     # Соответствие стратегии
        "dependency_count": 0.10,        # Количество зависимостей
        "resource_availability": 0.10,   # Доступность ресурсов
        "stakeholder_pressure": 0.10,    # Давление стейкхолдеров
        "risk_level": 0.10               # Уровень риска
    }

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "intelligence_weights"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"⚖️ {self.name} v{self.version} initialized")

    def set_llm(self, llm_router):
        """Устанавливает LLM роутер"""
        self.llm = llm_router

    async def calculate_weights(
        self,
        tasks: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        projects: List[Dict[str, Any]],
        risks: Optional[List[Dict[str, Any]]] = None,
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Рассчитывает веса и приоритеты для всех элементов.

        Args:
            tasks: Список задач
            decisions: Список решений
            projects: Список проектов
            risks: Список рисков
            meeting_context: Контекст встречи

        Returns:
            Взвешенные элементы с приоритетами
        """
        start_time = datetime.now(timezone.utc)
        logger.info("⚖️ Starting intelligence weights calculation")

        result = {
            "weighted_tasks": [],
            "weighted_decisions": [],
            "weighted_projects": [],
            "weighted_risks": [],
            "priority_matrix": {},
            "critical_items": [],
            "focus_recommendations": [],
            "stats": {}
        }

        try:
            # 1. Взвешиваем задачи
            result["weighted_tasks"] = await self._weight_tasks(
                tasks, projects, meeting_context
            )

            # 2. Взвешиваем решения
            result["weighted_decisions"] = await self._weight_decisions(
                decisions, projects, meeting_context
            )

            # 3. Взвешиваем проекты
            result["weighted_projects"] = await self._weight_projects(
                projects, tasks, meeting_context
            )

            # 4. Взвешиваем риски
            if risks:
                result["weighted_risks"] = await self._weight_risks(
                    risks, projects
                )

            # 5. Строим матрицу приоритетов
            result["priority_matrix"] = self._build_priority_matrix(
                result["weighted_tasks"],
                result["weighted_decisions"],
                result["weighted_projects"],
                result["weighted_risks"]
            )

            # 6. Выявляем критические элементы
            result["critical_items"] = self._identify_critical_items(
                result["priority_matrix"]
            )

            # 7. Генерируем рекомендации по фокусу
            result["focus_recommendations"] = await self._generate_focus_recommendations(
                result["critical_items"],
                result["priority_matrix"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "tasks_weighted": len(result["weighted_tasks"]),
                "decisions_weighted": len(result["weighted_decisions"]),
                "projects_weighted": len(result["weighted_projects"]),
                "risks_weighted": len(result["weighted_risks"]),
                "critical_items_count": len(result["critical_items"])
            }

            logger.info(f"✅ Intelligence weights calculated in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Intelligence weights error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _weight_tasks(
        self,
        tasks: List[Dict[str, Any]],
        projects: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Взвешивает задачи"""
        weighted = []

        if not self.llm or not tasks:
            return weighted

        prompt = f"""
Оцени ПРИОРИТЕТ И ВАЖНОСТЬ каждой задачи.

ЗАДАЧИ:
{json.dumps(tasks[:20], ensure_ascii=False)[:3000]}

ПРОЕКТЫ (контекст):
{json.dumps(projects[:5], ensure_ascii=False)[:1000]}

Для каждой задачи оцени по шкале 0.0-1.0:

1. urgency_score - срочность (близость дедлайна, давление)
2. importance_score - важность (влияние на бизнес, стратегия)
3. complexity_score - сложность (ресурсы, зависимости)
4. risk_score - риск невыполнения
5. dependency_score - количество зависящих от неё элементов

Формат (JSON массив):
[
    {{
        "task_name": "название задачи",
        "assignee": "исполнитель",
        "urgency_score": 0.0-1.0,
        "importance_score": 0.0-1.0,
        "complexity_score": 0.0-1.0,
        "risk_score": 0.0-1.0,
        "dependency_score": 0.0-1.0,
        "final_weight": 0.0-1.0,
        "priority_level": "critical/high/medium/low",
        "reasoning": "почему такой приоритет",
        "blockers": ["что блокирует"],
        "accelerators": ["что может ускорить"]
    }}
]

final_weight = urgency * 0.3 + importance * 0.4 + risk * 0.2 + dependency * 0.1
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error weighting tasks: {e}")

        return weighted

    async def _weight_decisions(
        self,
        decisions: List[Dict[str, Any]],
        projects: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Взвешивает решения"""
        weighted = []

        if not self.llm or not decisions:
            return weighted

        prompt = f"""
Оцени ВАЖНОСТЬ И СРОЧНОСТЬ каждого решения.

РЕШЕНИЯ:
{json.dumps(decisions[:15], ensure_ascii=False)[:2500]}

ПРОЕКТЫ (контекст):
{json.dumps(projects[:5], ensure_ascii=False)[:1000]}

Для каждого решения оцени:

1. strategic_impact - стратегическое влияние
2. urgency - срочность принятия
3. reversibility - обратимость (0 = необратимо, 1 = легко отменить)
4. stakeholder_impact - влияние на стейкхолдеров
5. resource_impact - влияние на ресурсы

Формат (JSON массив):
[
    {{
        "decision": "суть решения",
        "decision_maker": "кто принял",
        "strategic_impact": 0.0-1.0,
        "urgency": 0.0-1.0,
        "reversibility": 0.0-1.0,
        "stakeholder_impact": 0.0-1.0,
        "resource_impact": 0.0-1.0,
        "final_weight": 0.0-1.0,
        "priority_level": "critical/high/medium/low",
        "reasoning": "почему такой приоритет",
        "implementation_urgency": "immediate/this_week/this_month/flexible",
        "dependencies": ["от чего зависит реализация"]
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error weighting decisions: {e}")

        return weighted

    async def _weight_projects(
        self,
        projects: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Взвешивает проекты"""
        weighted = []

        if not self.llm or not projects:
            return weighted

        prompt = f"""
Оцени ПРИОРИТЕТ И ВАЖНОСТЬ каждого проекта.

ПРОЕКТЫ:
{json.dumps(projects[:10], ensure_ascii=False)[:2000]}

СВЯЗАННЫЕ ЗАДАЧИ:
{json.dumps(tasks[:15], ensure_ascii=False)[:1500]}

Для каждого проекта оцени:

1. strategic_value - стратегическая ценность
2. revenue_impact - влияние на доход
3. resource_intensity - ресурсоёмкость
4. risk_level - уровень риска
5. timeline_pressure - давление сроков
6. stakeholder_visibility - видимость для стейкхолдеров

Формат (JSON массив):
[
    {{
        "project_name": "название проекта",
        "status": "текущий статус",
        "strategic_value": 0.0-1.0,
        "revenue_impact": 0.0-1.0,
        "resource_intensity": 0.0-1.0,
        "risk_level": 0.0-1.0,
        "timeline_pressure": 0.0-1.0,
        "stakeholder_visibility": 0.0-1.0,
        "final_weight": 0.0-1.0,
        "priority_level": "critical/high/medium/low",
        "reasoning": "почему такой приоритет",
        "attention_areas": ["на что обратить внимание"],
        "quick_wins": ["быстрые победы"]
    }}
]
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error weighting projects: {e}")

        return weighted

    async def _weight_risks(
        self,
        risks: List[Dict[str, Any]],
        projects: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Взвешивает риски"""
        weighted = []

        for risk in risks:
            if isinstance(risk, dict):
                probability = risk.get("probability", 0.5)
                impact_str = risk.get("impact", "medium")

                # Конвертируем impact в число
                impact_map = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}
                impact = impact_map.get(impact_str, 0.5)

                # Расчёт веса риска: probability * impact
                final_weight = probability * impact

                # Определяем уровень приоритета
                if final_weight > 0.7:
                    priority_level = "critical"
                elif final_weight > 0.5:
                    priority_level = "high"
                elif final_weight > 0.3:
                    priority_level = "medium"
                else:
                    priority_level = "low"

                weighted.append({
                    "risk_name": risk.get("risk_name", risk.get("name", "")),
                    "category": risk.get("category", ""),
                    "probability": probability,
                    "impact": impact_str,
                    "final_weight": round(final_weight, 2),
                    "priority_level": priority_level,
                    "mitigation_urgency": "immediate" if priority_level == "critical" else "planned"
                })

        # Сортируем по весу
        weighted.sort(key=lambda x: x.get("final_weight", 0), reverse=True)

        return weighted

    def _build_priority_matrix(
        self,
        tasks: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        projects: List[Dict[str, Any]],
        risks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Строит матрицу приоритетов"""
        matrix = {
            "critical": [],
            "high": [],
            "medium": [],
            "low": []
        }

        # Добавляем задачи
        for task in tasks:
            if isinstance(task, dict):
                priority = task.get("priority_level", "medium")
                matrix.get(priority, matrix["medium"]).append({
                    "type": "task",
                    "name": task.get("task_name", ""),
                    "weight": task.get("final_weight", 0),
                    "assignee": task.get("assignee", ""),
                    "reasoning": task.get("reasoning", "")
                })

        # Добавляем решения
        for decision in decisions:
            if isinstance(decision, dict):
                priority = decision.get("priority_level", "medium")
                matrix.get(priority, matrix["medium"]).append({
                    "type": "decision",
                    "name": decision.get("decision", ""),
                    "weight": decision.get("final_weight", 0),
                    "decision_maker": decision.get("decision_maker", ""),
                    "reasoning": decision.get("reasoning", "")
                })

        # Добавляем проекты
        for project in projects:
            if isinstance(project, dict):
                priority = project.get("priority_level", "medium")
                matrix.get(priority, matrix["medium"]).append({
                    "type": "project",
                    "name": project.get("project_name", ""),
                    "weight": project.get("final_weight", 0),
                    "status": project.get("status", ""),
                    "reasoning": project.get("reasoning", "")
                })

        # Добавляем риски
        for risk in risks:
            if isinstance(risk, dict):
                priority = risk.get("priority_level", "medium")
                matrix.get(priority, matrix["medium"]).append({
                    "type": "risk",
                    "name": risk.get("risk_name", ""),
                    "weight": risk.get("final_weight", 0),
                    "category": risk.get("category", ""),
                    "reasoning": f"Probability: {risk.get('probability', 0):.0%}, Impact: {risk.get('impact', 'medium')}"
                })

        # Сортируем каждую категорию по весу
        for priority in matrix:
            matrix[priority].sort(key=lambda x: x.get("weight", 0), reverse=True)

        return matrix

    def _identify_critical_items(
        self,
        priority_matrix: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Выявляет критические элементы"""
        critical = []

        # Все critical items
        critical.extend(priority_matrix.get("critical", []))

        # Топ high items
        high_items = priority_matrix.get("high", [])[:5]
        critical.extend(high_items)

        return critical

    async def _generate_focus_recommendations(
        self,
        critical_items: List[Dict[str, Any]],
        priority_matrix: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации по фокусу"""
        recommendations = []

        if not self.llm:
            # Базовые рекомендации без LLM
            if critical_items:
                recommendations.append({
                    "recommendation": "Сфокусируйтесь на критических элементах",
                    "items": [item.get("name") for item in critical_items[:5]],
                    "priority": "critical",
                    "rationale": "Эти элементы имеют наивысший приоритет"
                })
            return recommendations

        prompt = f"""
На основе матрицы приоритетов, сгенерируй РЕКОМЕНДАЦИИ ПО ФОКУСУ.

КРИТИЧЕСКИЕ ЭЛЕМЕНТЫ:
{json.dumps(critical_items[:10], ensure_ascii=False)[:1500]}

МАТРИЦА ПРИОРИТЕТОВ (summary):
- Critical: {len(priority_matrix.get('critical', []))} элементов
- High: {len(priority_matrix.get('high', []))} элементов
- Medium: {len(priority_matrix.get('medium', []))} элементов
- Low: {len(priority_matrix.get('low', []))} элементов

Создай рекомендации:

Формат (JSON массив):
[
    {{
        "recommendation": "конкретная рекомендация",
        "focus_area": "tasks/decisions/projects/risks",
        "priority": "critical/high/medium",
        "items": ["конкретные элементы для фокуса"],
        "rationale": "почему это важно",
        "time_allocation": "сколько времени уделить",
        "success_criteria": "как понять что сделано"
    }}
]

Дай 3-5 конкретных рекомендаций.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error generating focus recommendations: {e}")

        return recommendations

    def _parse_json_response(self, response: str) -> Any:
        """Парсит JSON из ответа LLM"""
        if not response:
            return None

        # Убираем markdown
        response = response.strip()
        if response.startswith('```json'):
            response = response[7:]
        if response.startswith('```'):
            response = response[3:]
        if response.endswith('```'):
            response = response[:-3]

        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'[\[\{].*[\]\}]', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        return None

    def get_top_priorities(
        self,
        weighted_result: Dict[str, Any],
        top_n: int = 10
    ) -> List[Dict[str, Any]]:
        """Получает топ приоритетов"""
        all_items = []

        # Собираем все элементы
        for task in weighted_result.get("weighted_tasks", []):
            all_items.append({
                "type": "task",
                "name": task.get("task_name", ""),
                "weight": task.get("final_weight", 0),
                "priority": task.get("priority_level", "medium"),
                "details": task
            })

        for decision in weighted_result.get("weighted_decisions", []):
            all_items.append({
                "type": "decision",
                "name": decision.get("decision", ""),
                "weight": decision.get("final_weight", 0),
                "priority": decision.get("priority_level", "medium"),
                "details": decision
            })

        for project in weighted_result.get("weighted_projects", []):
            all_items.append({
                "type": "project",
                "name": project.get("project_name", ""),
                "weight": project.get("final_weight", 0),
                "priority": project.get("priority_level", "medium"),
                "details": project
            })

        # Сортируем по весу
        all_items.sort(key=lambda x: x.get("weight", 0), reverse=True)

        return all_items[:top_n]


