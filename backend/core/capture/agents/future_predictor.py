# -*- coding: utf-8 -*-
"""
Future Predictor Agent - Прогнозирование будущего

Анализирует:
1. Сценарии развития (оптимистичный/реалистичный/пессимистичный)
2. Вероятности исходов
3. Риски и early warnings
4. Прогнозы по проектам и задачам
5. Рекомендации по предотвращению рисков

Адаптировано из tess_old/module_09_future_prediction/
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Scenario:
    """Сценарий развития"""
    name: str
    scenario_type: str  # optimistic/realistic/pessimistic/critical
    probability: float  # 0.0-1.0
    description: str = ""
    key_events: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)
    timeline: str = ""


@dataclass
class Risk:
    """Риск"""
    name: str
    category: str  # operational/strategic/financial/reputational
    probability: float  # 0.0-1.0
    impact: str  # low/medium/high/critical
    early_warnings: List[str] = field(default_factory=list)
    mitigation: List[str] = field(default_factory=list)


@dataclass
class Prediction:
    """Прогноз"""
    subject: str  # project/task/person/team
    subject_name: str
    success_probability: float
    completion_date: Optional[str] = None
    confidence: float = 0.5
    key_factors: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


class FuturePredictor:
    """
    Агент прогнозирования будущего.

    Возможности:
    - Моделирование сценариев развития
    - Прогнозирование результатов проектов
    - Выявление рисков и early warnings
    - Генерация рекомендаций
    """

    def __init__(self, llm_router=None, graph_builder=None):
        self.name = "future_predictor"
        self.version = "1.0.0"
        self.llm = llm_router
        self.graph = graph_builder

        logger.info(f"🔮 {self.name} v{self.version} initialized")

    def set_llm(self, llm_router):
        """Устанавливает LLM роутер"""
        self.llm = llm_router

    async def predict(
        self,
        transcript: str,
        projects: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Полный прогноз будущего на основе данных встречи.

        Args:
            transcript: Транскрипт встречи
            projects: Список проектов
            tasks: Список задач
            decisions: Список решений
            meeting_context: Контекст встречи

        Returns:
            Комплексный прогноз
        """
        start_time = datetime.now(timezone.utc)
        logger.info("🔮 Starting future prediction")

        result = {
            "scenarios": [],
            "project_predictions": [],
            "task_predictions": [],
            "risks": [],
            "early_warnings": [],
            "opportunities": [],
            "recommendations": [],
            "stats": {}
        }

        try:
            # 1. Моделируем сценарии развития
            result["scenarios"] = await self._model_scenarios(
                transcript, projects, tasks, decisions, meeting_context
            )

            # 2. Прогнозируем результаты проектов
            result["project_predictions"] = await self._predict_project_outcomes(
                projects, tasks, decisions
            )

            # 3. Прогнозируем выполнение задач
            result["task_predictions"] = await self._predict_task_completion(
                tasks, meeting_context
            )

            # 4. Выявляем риски
            result["risks"] = await self._identify_risks(
                transcript, projects, tasks, decisions
            )

            # 5. Генерируем early warnings
            result["early_warnings"] = await self._generate_early_warnings(
                result["risks"],
                result["project_predictions"]
            )

            # 6. Выявляем возможности
            result["opportunities"] = await self._identify_opportunities(
                transcript, projects, meeting_context
            )

            # 7. Генерируем рекомендации
            result["recommendations"] = await self._generate_recommendations(
                result["scenarios"],
                result["risks"],
                result["opportunities"]
            )

            # Статистика
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            result["stats"] = {
                "duration_seconds": round(duration, 2),
                "scenarios_modeled": len(result["scenarios"]),
                "projects_predicted": len(result["project_predictions"]),
                "tasks_predicted": len(result["task_predictions"]),
                "risks_identified": len(result["risks"]),
                "warnings_generated": len(result["early_warnings"]),
                "opportunities_found": len(result["opportunities"]),
                "recommendations_made": len(result["recommendations"])
            }

            logger.info(f"✅ Future prediction complete in {duration:.2f}s")

        except Exception as e:
            logger.error(f"❌ Future prediction error: {e}", exc_info=True)
            result["error"] = str(e)

        return result

    async def _model_scenarios(
        self,
        transcript: str,
        projects: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Моделирует сценарии развития"""
        scenarios = []

        if not self.llm:
            logger.warning("LLM not available for scenario modeling")
            return scenarios

        prompt = f"""
Смоделируй СЦЕНАРИИ РАЗВИТИЯ на основе данных встречи.

ПРОЕКТЫ:
{json.dumps(projects[:5], ensure_ascii=False)[:1500]}

ЗАДАЧИ:
{json.dumps(tasks[:10], ensure_ascii=False)[:1500]}

РЕШЕНИЯ:
{json.dumps(decisions[:5], ensure_ascii=False)[:1000]}

ТРАНСКРИПТ (фрагмент):
{transcript[:2000]}

Создай 3-4 сценария развития:

1. ОПТИМИСТИЧНЫЙ - всё идёт хорошо
2. РЕАЛИСТИЧНЫЙ - наиболее вероятный
3. ПЕССИМИСТИЧНЫЙ - что может пойти не так
4. КРИТИЧЕСКИЙ - худший случай (если есть серьёзные риски)

Формат ответа (JSON массив):
[
    {{
        "name": "название сценария",
        "type": "optimistic/realistic/pessimistic/critical",
        "probability": 0.0-1.0,
        "description": "описание сценария",
        "time_horizon": "1 месяц / 3 месяца / 6 месяцев / 1 год",
        "key_events": ["ключевые события в этом сценарии"],
        "triggers": ["что может запустить этот сценарий"],
        "consequences": ["последствия для компании/проектов"],
        "affected_projects": ["затронутые проекты"],
        "affected_people": ["затронутые люди"],
        "decision_points": ["точки принятия решений"],
        "success_factors": ["факторы успеха в этом сценарии"],
        "failure_factors": ["факторы неудачи"],
        "recommended_actions": ["рекомендуемые действия"]
    }}
]

ВАЖНО — ПРОГНОЗ, ОПЁРТЫЙ НА СИГНАЛЫ ИЗ ВСТРЕЧИ:
- Каждый прогноз должен вытекать из конкретных фактов/трендов/рисков, названных
  в этой встрече. Указывай, на каком сигнале он основан. Нет основания в тексте —
  не выдумывай сценарий.
- Это гипотеза о будущем, а не факт: помечай неопределённость честными
  вероятностями (в сумме ~1.0), не завышай уверенность.
- Будь конкретным в описании событий, но не переходи в фантазию, оторванную от
  сказанного.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error modeling scenarios: {e}")

        return scenarios

    async def _predict_project_outcomes(
        self,
        projects: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Прогнозирует результаты проектов"""
        predictions = []

        if not self.llm or not projects:
            return predictions

        prompt = f"""
Спрогнозируй РЕЗУЛЬТАТЫ ПРОЕКТОВ.

ПРОЕКТЫ:
{json.dumps(projects[:10], ensure_ascii=False)[:2000]}

СВЯЗАННЫЕ ЗАДАЧИ:
{json.dumps(tasks[:15], ensure_ascii=False)[:1500]}

РЕШЕНИЯ:
{json.dumps(decisions[:10], ensure_ascii=False)[:1000]}

Для каждого проекта дай прогноз:

Формат (JSON массив):
[
    {{
        "project_name": "название проекта",
        "current_status": "текущий статус",
        "success_probability": 0.0-1.0,
        "confidence": 0.0-1.0,
        "predicted_completion": "прогнозируемая дата завершения",
        "delay_risk": 0.0-1.0,
        "quality_forecast": "low/medium/high",
        "budget_risk": "on_track/at_risk/over_budget",
        "key_success_factors": ["факторы успеха"],
        "key_risk_factors": ["факторы риска"],
        "bottlenecks": ["узкие места"],
        "dependencies": ["критические зависимости"],
        "team_readiness": "low/medium/high",
        "resource_adequacy": "insufficient/adequate/excess",
        "outcome_scenarios": [
            {{
                "scenario": "описание исхода",
                "probability": 0.0-1.0,
                "impact": "влияние на бизнес"
            }}
        ],
        "recommendations": ["рекомендации по улучшению прогноза"]
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
            logger.error(f"Error predicting project outcomes: {e}")

        return predictions

    async def _predict_task_completion(
        self,
        tasks: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Прогнозирует выполнение задач"""
        predictions = []

        if not self.llm or not tasks:
            return predictions

        # Ограничиваем количество задач
        tasks_to_predict = tasks[:15]

        prompt = f"""
Спрогнозируй ВЫПОЛНЕНИЕ ЗАДАЧ.

ЗАДАЧИ:
{json.dumps(tasks_to_predict, ensure_ascii=False)[:2500]}

Для каждой задачи дай прогноз:

Формат (JSON массив):
[
    {{
        "task": "название задачи",
        "assignee": "исполнитель",
        "deadline": "дедлайн если есть",
        "completion_probability": 0.0-1.0,
        "on_time_probability": 0.0-1.0,
        "predicted_completion": "прогнозируемая дата",
        "delay_days": 0,
        "blockers": ["блокирующие факторы"],
        "dependencies": ["зависимости"],
        "priority_score": 0.0-1.0,
        "effort_estimate": "low/medium/high",
        "risk_factors": ["факторы риска"],
        "success_factors": ["факторы успеха"]
    }}
]

Анализируй только задачи из входных данных.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error predicting task completion: {e}")

        return predictions

    async def _identify_risks(
        self,
        transcript: str,
        projects: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Выявляет риски"""
        risks = []

        if not self.llm:
            return risks

        prompt = f"""
Выяви РИСКИ на основе данных встречи.

ТРАНСКРИПТ (фрагмент):
{transcript[:2000]}

ПРОЕКТЫ:
{json.dumps(projects[:5], ensure_ascii=False)[:1000]}

ЗАДАЧИ:
{json.dumps(tasks[:10], ensure_ascii=False)[:1000]}

РЕШЕНИЯ:
{json.dumps(decisions[:5], ensure_ascii=False)[:500]}

Найди все потенциальные риски:

Формат (JSON массив):
[
    {{
        "risk_name": "название риска",
        "category": "operational/strategic/financial/reputational/technical/resource/timeline",
        "description": "описание риска",
        "probability": 0.0-1.0,
        "impact": "low/medium/high/critical",
        "affected_projects": ["затронутые проекты"],
        "affected_people": ["затронутые люди"],
        "root_causes": ["корневые причины"],
        "triggers": ["что может активировать риск"],
        "early_warning_signs": ["ранние признаки"],
        "timeline": "когда может проявиться",
        "mitigation_strategies": ["стратегии митигации"],
        "contingency_plans": ["планы на случай реализации"],
        "owner": "кто должен отслеживать",
        "monitoring_frequency": "daily/weekly/monthly"
    }}
]

Ищи как явные, так и скрытые риски.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error identifying risks: {e}")

        return risks

    async def _generate_early_warnings(
        self,
        risks: List[Dict[str, Any]],
        project_predictions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует early warnings"""
        warnings = []

        # Генерируем предупреждения на основе рисков
        for risk in risks:
            if isinstance(risk, dict):
                probability = risk.get("probability", 0)
                impact = risk.get("impact", "medium")

                # High probability or high impact risks
                if probability > 0.6 or impact in ["high", "critical"]:
                    warning = {
                        "type": "risk_warning",
                        "severity": "high" if impact == "critical" else "medium",
                        "title": f"⚠️ Риск: {risk.get('risk_name', 'Неизвестный риск')}",
                        "description": risk.get("description", ""),
                        "indicators": risk.get("early_warning_signs", []),
                        "recommended_action": risk.get("mitigation_strategies", ["Требуется внимание"])[0] if risk.get("mitigation_strategies") else "Требуется внимание",
                        "deadline": "Немедленно" if impact == "critical" else "В течение недели"
                    }
                    warnings.append(warning)

        # Генерируем предупреждения на основе прогнозов проектов
        for pred in project_predictions:
            if isinstance(pred, dict):
                success_prob = pred.get("success_probability", 1.0)
                delay_risk = pred.get("delay_risk", 0)

                if success_prob < 0.5:
                    warning = {
                        "type": "project_warning",
                        "severity": "high",
                        "title": f"🚨 Проект под угрозой: {pred.get('project_name', 'Неизвестный проект')}",
                        "description": f"Вероятность успеха: {success_prob:.0%}",
                        "indicators": pred.get("key_risk_factors", []),
                        "recommended_action": pred.get("recommendations", ["Требуется ревью проекта"])[0] if pred.get("recommendations") else "Требуется ревью проекта",
                        "deadline": "Срочно"
                    }
                    warnings.append(warning)
                elif delay_risk > 0.7:
                    warning = {
                        "type": "delay_warning",
                        "severity": "medium",
                        "title": f"⏰ Риск задержки: {pred.get('project_name', 'Неизвестный проект')}",
                        "description": f"Вероятность задержки: {delay_risk:.0%}",
                        "indicators": pred.get("bottlenecks", []),
                        "recommended_action": "Пересмотреть сроки и ресурсы",
                        "deadline": "В течение недели"
                    }
                    warnings.append(warning)

        return warnings

    async def _identify_opportunities(
        self,
        transcript: str,
        projects: List[Dict[str, Any]],
        meeting_context: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Выявляет возможности"""
        opportunities = []

        if not self.llm:
            return opportunities

        prompt = f"""
Выяви ВОЗМОЖНОСТИ И ПОЗИТИВНЫЕ ТРЕНДЫ на основе данных встречи.

ТРАНСКРИПТ (фрагмент):
{transcript[:2000]}

ПРОЕКТЫ:
{json.dumps(projects[:5], ensure_ascii=False)[:1000]}

Найди:
1. Новые возможности для роста
2. Позитивные тренды
3. Потенциальные синергии
4. Неиспользованный потенциал
5. Возможности оптимизации

Формат (JSON массив):
[
    {{
        "opportunity_name": "название возможности",
        "category": "growth/efficiency/innovation/synergy/market",
        "description": "описание возможности",
        "potential_value": "low/medium/high",
        "probability_of_success": 0.0-1.0,
        "time_to_realize": "short/medium/long term",
        "required_resources": ["необходимые ресурсы"],
        "key_enablers": ["что нужно для реализации"],
        "barriers": ["препятствия"],
        "related_projects": ["связанные проекты"],
        "recommended_first_steps": ["первые шаги"]
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
            logger.error(f"Error identifying opportunities: {e}")

        return opportunities

    async def _generate_recommendations(
        self,
        scenarios: List[Dict[str, Any]],
        risks: List[Dict[str, Any]],
        opportunities: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Генерирует рекомендации"""
        recommendations = []

        if not self.llm:
            return recommendations

        prompt = f"""
На основе сценариев, рисков и возможностей, сгенерируй СТРАТЕГИЧЕСКИЕ РЕКОМЕНДАЦИИ.

СЦЕНАРИИ:
{json.dumps(scenarios[:3], ensure_ascii=False)[:1500]}

РИСКИ:
{json.dumps(risks[:5], ensure_ascii=False)[:1000]}

ВОЗМОЖНОСТИ:
{json.dumps(opportunities[:5], ensure_ascii=False)[:1000]}

Создай практические рекомендации:

Формат (JSON массив):
[
    {{
        "recommendation": "конкретная рекомендация",
        "type": "risk_mitigation/opportunity_capture/scenario_preparation/optimization",
        "priority": "critical/high/medium/low",
        "rationale": "обоснование",
        "expected_outcome": "ожидаемый результат",
        "implementation_steps": ["шаги реализации"],
        "resources_needed": ["необходимые ресурсы"],
        "timeline": "сроки реализации",
        "success_metrics": ["метрики успеха"],
        "responsible": "кто должен отвечать",
        "dependencies": ["зависимости"]
    }}
]

Дай 5-8 конкретных рекомендаций.
"""

        try:
            response = await self.llm.generate(prompt)
            if response:
                parsed = self._parse_json_response(response)
                if isinstance(parsed, list):
                    return parsed
        except Exception as e:
            logger.error(f"Error generating recommendations: {e}")

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
            # Пытаемся найти JSON в тексте
            import re
            json_match = re.search(r'[\[\{].*[\]\}]', response, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        return None

    async def save_predictions_to_graph(
        self,
        predictions: Dict[str, Any],
        meeting_id: str
    ):
        """Сохраняет прогнозы в граф"""
        if not self.graph:
            return

        try:
            # Сохраняем сценарии
            for i, scenario in enumerate(predictions.get("scenarios", [])):
                if isinstance(scenario, dict):
                    scenario_id = f"scenario:{meeting_id}:{i}"
                    await self.graph.create_node(
                        node_id=scenario_id,
                        label="Scenario",
                        properties={
                            "name": scenario.get("name", ""),
                            "type": scenario.get("type", ""),
                            "probability": scenario.get("probability", 0),
                            "description": scenario.get("description", ""),
                            "meeting_id": meeting_id,
                            "created_at": datetime.now(timezone.utc).isoformat()
                        }
                    )

            # Сохраняем риски
            for i, risk in enumerate(predictions.get("risks", [])):
                if isinstance(risk, dict):
                    risk_id = f"risk:{meeting_id}:{i}"
                    await self.graph.create_node(
                        node_id=risk_id,
                        label="Risk",
                        properties={
                            "name": risk.get("risk_name", ""),
                            "category": risk.get("category", ""),
                            "probability": risk.get("probability", 0),
                            "impact": risk.get("impact", "medium"),
                            "description": risk.get("description", ""),
                            "meeting_id": meeting_id,
                            "created_at": datetime.now(timezone.utc).isoformat()
                        }
                    )

            # Сохраняем early warnings
            for i, warning in enumerate(predictions.get("early_warnings", [])):
                if isinstance(warning, dict):
                    warning_id = f"warning:{meeting_id}:{i}"
                    await self.graph.create_node(
                        node_id=warning_id,
                        label="EarlyWarning",
                        properties={
                            "title": warning.get("title", ""),
                            "type": warning.get("type", ""),
                            "severity": warning.get("severity", "medium"),
                            "description": warning.get("description", ""),
                            "meeting_id": meeting_id,
                            "created_at": datetime.now(timezone.utc).isoformat()
                        }
                    )

            logger.info(f"💾 Saved predictions to graph for meeting {meeting_id}")

        except Exception as e:
            logger.error(f"Error saving predictions to graph: {e}")


