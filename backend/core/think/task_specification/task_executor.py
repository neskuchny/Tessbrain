# -*- coding: utf-8 -*-
"""
Task Executor Agent - выполнение задач по ТЗ.

Выполняет:
- Разбиение на подзадачи
- Пошаговое выполнение
- Мониторинг прогресса
- Валидация результатов
"""

import json
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .base_agent import BaseTaskAgent


class TaskStatus(Enum):
    """Статусы задачи."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskExecutorAgent(BaseTaskAgent):
    """Агент выполнения задач по ТЗ."""

    def __init__(
        self,
        storage=None,
        llm_router=None,
        tool_registry: Optional[Dict[str, Callable]] = None
    ):
        super().__init__("TaskExecutor", storage, llm_router)
        self.tool_registry = tool_registry or {}
        self.execution_history = []

    def register_tool(self, name: str, func: Callable, description: str = ""):
        """Регистрация инструмента для выполнения."""
        self.tool_registry[name] = {
            "func": func,
            "description": description
        }
        self.log_info(f"Зарегистрирован инструмент: {name}")

    async def execute(
        self,
        specification: Dict[str, Any],
        task_id: str,
        execution_mode: str = "plan_only"  # plan_only | supervised | autonomous
    ) -> Dict[str, Any]:
        """
        Выполнение задачи по спецификации.

        Args:
            specification: Техническое задание
            task_id: ID задачи
            execution_mode: Режим выполнения

        Returns:
            Результат выполнения
        """
        self.log_start(f"Выполнение задачи: {task_id}, режим: {execution_mode}")

        # 1. Разбор спецификации
        execution_plan = self._parse_specification(specification)

        # 2. Генерация детального плана выполнения
        detailed_plan = await self._generate_detailed_plan(execution_plan)

        # 3. Валидация готовности
        readiness = await self._validate_readiness(detailed_plan)

        # 4. Выполнение (в зависимости от режима)
        if execution_mode == "plan_only":
            result = await self._create_execution_plan_only(detailed_plan, readiness)
        elif execution_mode == "supervised":
            result = await self._execute_supervised(detailed_plan, readiness)
        else:  # autonomous
            result = await self._execute_autonomous(detailed_plan, readiness)

        # 5. Формирование отчета
        execution_report = {
            "task_id": task_id,
            "specification_id": specification.get("specification_id"),
            "execution_mode": execution_mode,
            "detailed_plan": detailed_plan,
            "readiness_check": readiness,
            "execution_result": result,
            "summary": await self._generate_summary(result),
            "metadata": {
                "agent": self.name,
                "timestamp": self.get_timestamp(),
                "execution_history_count": len(self.execution_history)
            }
        }

        self.log_complete(f"Выполнение завершено: {result.get('status', 'unknown')}")
        return execution_report

    def _parse_specification(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Разбор спецификации для выполнения."""
        main_spec = spec.get("main_specification", {})
        exec_plan = spec.get("execution_plan", {})

        return {
            "deliverables": main_spec.get("deliverables", []),
            "requirements": main_spec.get("requirements", {}),
            "solution_approach": main_spec.get("solution_approach", {}),
            "phases": exec_plan.get("phases", []),
            "timeline": exec_plan.get("timeline", {}),
            "resources": exec_plan.get("resources", {}),
            "acceptance_criteria": spec.get("acceptance_criteria", {}),
            "risks": spec.get("risk_management", {}).get("risk_register", [])
        }

    async def _generate_detailed_plan(
        self,
        execution_plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Генерация детального плана выполнения."""

        system_prompt = """Ты — эксперт по выполнению проектов.
Создай детальный план выполнения с конкретными шагами.

Возвращай ТОЛЬКО валидный JSON без markdown-разметки."""

        plan_json = json.dumps(execution_plan, ensure_ascii=False, indent=2)
        tools_available = list(self.tool_registry.keys())

        prompt = f"""Создай детальный план выполнения и верни JSON:

ПЛАН ВЫПОЛНЕНИЯ:
{plan_json}

ДОСТУПНЫЕ ИНСТРУМЕНТЫ:
{json.dumps(tools_available, ensure_ascii=False)}

Верни JSON со структурой:
{{
    "execution_steps": [
        {{
            "step_id": "STEP-001",
            "phase_ref": "PHASE-1",
            "name": "название шага",
            "description": "детальное описание",
            "action_type": "manual|automated|hybrid",
            "tool_to_use": "название инструмента или null",
            "inputs": ["входные данные"],
            "outputs": ["выходные данные"],
            "validation": "как проверить результат",
            "estimated_duration_minutes": 30,
            "dependencies": ["STEP-XXX"],
            "rollback_action": "действие при ошибке"
        }}
    ],
    "execution_flow": {{
        "parallel_groups": [
            ["STEP-001", "STEP-002"]
        ],
        "sequential_order": ["STEP-003", "STEP-004"],
        "checkpoints": [
            {{
                "after_step": "STEP-002",
                "validation": "проверка"
            }}
        ]
    }},
    "automation_opportunities": [
        {{
            "step_id": "STEP-001",
            "automation_type": "full|partial",
            "implementation": "как автоматизировать"
        }}
    ],
    "human_intervention_points": [
        {{
            "after_step": "STEP-003",
            "reason": "причина",
            "decision_required": "какое решение нужно"
        }}
    ],
    "success_criteria": {{
        "step_level": ["критерии для каждого шага"],
        "overall": ["общие критерии успеха"]
    }}
}}"""

        return await self.generate_llm_response(prompt, system_prompt)

    async def _validate_readiness(
        self,
        detailed_plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Валидация готовности к выполнению."""
        readiness = {
            "is_ready": True,
            "blockers": [],
            "warnings": [],
            "tool_availability": {},
            "resource_availability": {}
        }

        # Проверка доступности инструментов
        for step in detailed_plan.get("execution_steps", []):
            tool = step.get("tool_to_use")
            if tool and tool not in self.tool_registry:
                readiness["tool_availability"][tool] = False
                if step.get("action_type") == "automated":
                    readiness["blockers"].append(
                        f"Инструмент '{tool}' не доступен для шага {step.get('step_id')}"
                    )
                    readiness["is_ready"] = False
                else:
                    readiness["warnings"].append(
                        f"Инструмент '{tool}' не доступен, шаг {step.get('step_id')} будет ручным"
                    )
            elif tool:
                readiness["tool_availability"][tool] = True

        # Проверка зависимостей
        all_step_ids = {s.get("step_id") for s in detailed_plan.get("execution_steps", [])}
        for step in detailed_plan.get("execution_steps", []):
            for dep in step.get("dependencies", []):
                if dep not in all_step_ids:
                    readiness["warnings"].append(
                        f"Зависимость '{dep}' для {step.get('step_id')} не найдена в плане"
                    )

        return readiness

    async def _create_execution_plan_only(
        self,
        detailed_plan: Dict[str, Any],
        readiness: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Создание плана без выполнения."""

        # Генерация инструкций для ручного выполнения
        instructions = await self._generate_manual_instructions(detailed_plan)

        return {
            "status": "plan_created",
            "execution_type": "manual",
            "instructions": instructions,
            "steps_count": len(detailed_plan.get("execution_steps", [])),
            "estimated_duration": self._calculate_total_duration(detailed_plan),
            "action_items": [
                {
                    "step_id": step.get("step_id"),
                    "action": step.get("name"),
                    "type": step.get("action_type"),
                    "duration_minutes": step.get("estimated_duration_minutes")
                }
                for step in detailed_plan.get("execution_steps", [])
            ]
        }

    async def _execute_supervised(
        self,
        detailed_plan: Dict[str, Any],
        readiness: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Выполнение с контролем (шаг за шагом)."""
        results = []

        for step in detailed_plan.get("execution_steps", []):
            step_result = {
                "step_id": step.get("step_id"),
                "name": step.get("name"),
                "status": TaskStatus.PENDING.value,
                "started_at": None,
                "completed_at": None,
                "output": None,
                "error": None
            }

            # Проверяем, можно ли автоматизировать
            tool = step.get("tool_to_use")
            if tool and tool in self.tool_registry and step.get("action_type") != "manual":
                # Автоматическое выполнение
                try:
                    step_result["started_at"] = self.get_timestamp()
                    step_result["status"] = TaskStatus.IN_PROGRESS.value

                    tool_func = self.tool_registry[tool]["func"]
                    # Вызываем инструмент
                    output = await self._safe_tool_call(tool_func, step)

                    step_result["output"] = output
                    step_result["status"] = TaskStatus.COMPLETED.value
                    step_result["completed_at"] = self.get_timestamp()

                except Exception as e:
                    step_result["status"] = TaskStatus.FAILED.value
                    step_result["error"] = str(e)
                    self.log_error(f"Ошибка в шаге {step.get('step_id')}: {e}")
            else:
                # Требуется ручное выполнение
                step_result["status"] = "requires_manual"
                step_result["manual_instructions"] = await self._generate_step_instructions(step)

            results.append(step_result)
            self.execution_history.append(step_result)

        completed = sum(1 for r in results if r["status"] == TaskStatus.COMPLETED.value)
        manual = sum(1 for r in results if r["status"] == "requires_manual")
        failed = sum(1 for r in results if r["status"] == TaskStatus.FAILED.value)

        return {
            "status": "partially_executed" if manual > 0 else "executed",
            "execution_type": "supervised",
            "steps_results": results,
            "summary": {
                "total": len(results),
                "completed": completed,
                "requires_manual": manual,
                "failed": failed
            }
        }

    async def _execute_autonomous(
        self,
        detailed_plan: Dict[str, Any],
        readiness: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Автономное выполнение (без подтверждений)."""

        if not readiness.get("is_ready"):
            return {
                "status": "blocked",
                "execution_type": "autonomous",
                "reason": "Не все условия готовности выполнены",
                "blockers": readiness.get("blockers", [])
            }

        # Выполнение всех шагов
        results = await self._execute_supervised(detailed_plan, readiness)
        results["execution_type"] = "autonomous"

        return results

    async def _safe_tool_call(
        self,
        tool_func: Callable,
        step: Dict[str, Any]
    ) -> Any:
        """Безопасный вызов инструмента."""
        try:
            # Подготавливаем входные данные
            inputs = step.get("inputs", [])

            # Вызываем функцию
            if callable(tool_func):
                import asyncio
                if asyncio.iscoroutinefunction(tool_func):
                    return await tool_func(*inputs)
                else:
                    return tool_func(*inputs)
            return None
        except Exception as e:
            raise Exception(f"Tool execution failed: {e}")

    async def _generate_manual_instructions(
        self,
        detailed_plan: Dict[str, Any]
    ) -> str:
        """Генерация инструкций для ручного выполнения."""

        system_prompt = """Создай понятные пошаговые инструкции для выполнения задачи.
Инструкции должны быть конкретными и actionable."""

        steps = detailed_plan.get("execution_steps", [])
        steps_json = json.dumps(steps, ensure_ascii=False, indent=2)

        prompt = f"""Создай пошаговые инструкции для выполнения:

ШАГИ:
{steps_json}

Формат: Markdown с нумерованными шагами и чеклистами."""

        result = await self.generate_llm_response(prompt, system_prompt)
        return result.get("raw_text", str(result))

    async def _generate_step_instructions(self, step: Dict[str, Any]) -> str:
        """Генерация инструкций для одного шага."""
        return f"""
## {step.get('step_id')}: {step.get('name')}

**Описание:** {step.get('description', 'N/A')}

**Входные данные:**
{chr(10).join('- ' + str(i) for i in step.get('inputs', []))}

**Ожидаемые результаты:**
{chr(10).join('- ' + str(o) for o in step.get('outputs', []))}

**Проверка:** {step.get('validation', 'N/A')}

**Примерное время:** {step.get('estimated_duration_minutes', 'N/A')} минут
"""

    def _calculate_total_duration(self, detailed_plan: Dict[str, Any]) -> int:
        """Расчет общей длительности."""
        total = 0
        for step in detailed_plan.get("execution_steps", []):
            total += step.get("estimated_duration_minutes", 0)
        return total

    async def _generate_summary(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Генерация итогового резюме."""
        status = result.get("status", "unknown")
        exec_type = result.get("execution_type", "unknown")

        summary_data = result.get("summary", {})

        return {
            "overall_status": status,
            "execution_type": exec_type,
            "completion_rate": self._calculate_completion_rate(summary_data),
            "next_actions": self._determine_next_actions(result),
            "recommendations": await self._generate_recommendations(result)
        }

    def _calculate_completion_rate(self, summary: Dict[str, Any]) -> float:
        """Расчет процента выполнения."""
        total = summary.get("total", 0)
        if total == 0:
            return 0.0
        completed = summary.get("completed", 0)
        return round((completed / total) * 100, 2)

    def _determine_next_actions(self, result: Dict[str, Any]) -> List[str]:
        """Определение следующих действий."""
        actions = []
        status = result.get("status", "")

        if status == "plan_created":
            actions.append("Начать выполнение по инструкциям")
            actions.append("Подготовить необходимые ресурсы")
        elif status == "partially_executed":
            actions.append("Выполнить оставшиеся ручные шаги")
            actions.append("Проверить результаты автоматических шагов")
        elif status == "executed":
            actions.append("Провести финальную валидацию")
            actions.append("Подготовить отчет о выполнении")
        elif status == "blocked":
            actions.append("Устранить блокеры")
            actions.append("Повторить проверку готовности")

        return actions

    async def _generate_recommendations(self, result: Dict[str, Any]) -> List[str]:
        """Генерация рекомендаций."""
        recommendations = []

        summary = result.get("summary", {})
        if summary.get("failed", 0) > 0:
            recommendations.append("Проанализировать причины неудач и обновить план")

        if summary.get("requires_manual", 0) > 0:
            recommendations.append("Рассмотреть возможность автоматизации ручных шагов")

        if result.get("status") == "executed":
            recommendations.append("Задокументировать успешные практики для будущих задач")

        return recommendations

