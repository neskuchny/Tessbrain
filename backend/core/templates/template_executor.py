# -*- coding: utf-8 -*-
"""
Template Executor - Выполнение шаблонов и цепочек агентов.

Основные функции:
- Выполнение шаблонов по триггерам
- Резолвинг переменных между шагами
- Обработка условий и ошибок
- Трекинг выполнения
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from backend.core.llm.usage_tracker import UsageContext

from .template_model import (
    ExecutionStatus,
    TemplateExecution,
    TemplateStep,
    WorkflowTemplate,
)

logger = logging.getLogger(__name__)


class VariableResolver:
    """
    Резолвер переменных в параметрах шагов.

    Поддерживает:
    - ${step_1.result} - результат шага
    - ${step_1.entity} - конкретное поле
    - ${input.param} - входной параметр
    """

    VARIABLE_PATTERN = re.compile(r'\$\{([^}]+)\}')

    def resolve(
        self,
        value: Any,
        step_results: Dict[str, Any],
        input_params: Dict[str, Any]
    ) -> Any:
        """
        Резолвит переменные в значении.
        """
        if isinstance(value, str):
            return self._resolve_string(value, step_results, input_params)
        elif isinstance(value, dict):
            return {k: self.resolve(v, step_results, input_params) for k, v in value.items()}
        elif isinstance(value, list):
            return [self.resolve(v, step_results, input_params) for v in value]
        return value

    def _resolve_string(
        self,
        text: str,
        step_results: Dict[str, Any],
        input_params: Dict[str, Any]
    ) -> Any:
        """
        Резолвит переменные в строке.
        """
        # Если вся строка - переменная, возвращаем значение напрямую
        full_match = re.fullmatch(r'\$\{([^}]+)\}', text)
        if full_match:
            return self._get_value(full_match.group(1), step_results, input_params)

        # Иначе заменяем переменные в строке
        def replacer(match):
            value = self._get_value(match.group(1), step_results, input_params)
            if value is None:
                return match.group(0)  # Оставляем как есть
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False)
            return str(value)

        return self.VARIABLE_PATTERN.sub(replacer, text)

    def _get_value(
        self,
        path: str,
        step_results: Dict[str, Any],
        input_params: Dict[str, Any]
    ) -> Any:
        """
        Получает значение по пути.

        Примеры путей:
        - step_1.result
        - step_1.entity
        - input.meeting_id
        """
        parts = path.split('.')

        if not parts:
            return None

        # Определяем источник
        source_name = parts[0]

        if source_name == "input":
            source = input_params
            parts = parts[1:]
        elif source_name.startswith("step_"):
            source = step_results.get(source_name, {})
            parts = parts[1:]
        else:
            # Пробуем как step_id напрямую
            source = step_results.get(source_name, {})
            parts = parts[1:]

        # Навигация по вложенным полям
        current = source
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if idx < len(current) else None
            else:
                return None

            if current is None:
                return None

        return current


class TemplateExecutor:
    """
    Выполняет шаблоны и цепочки агентов.
    """

    def __init__(
        self,
        llm_router=None,
        graph_builder=None,
        vector_indexer=None,
        agent_registry: Optional[Dict[str, Callable]] = None
    ):
        self.llm_router = llm_router
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer
        self.variable_resolver = VariableResolver()

        # Реестр агентов (имя -> callable)
        self.agent_registry: Dict[str, Any] = agent_registry or {}

        # История выполнений
        self.executions: Dict[str, TemplateExecution] = {}

        logger.info("✅ TemplateExecutor initialized")

    def register_agent(self, name: str, agent: Any):
        """Регистрирует агента."""
        self.agent_registry[name] = agent
        logger.debug(f"📝 Registered agent: {name}")

    async def execute(
        self,
        template: WorkflowTemplate,
        query: str,
        input_params: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        on_step_complete: Optional[Callable] = None
    ) -> TemplateExecution:
        """
        Выполняет шаблон.

        Args:
            template: Шаблон для выполнения
            query: Исходный запрос пользователя
            input_params: Входные параметры
            user_id: ID пользователя
            on_step_complete: Callback после каждого шага

        Returns:
            TemplateExecution с результатами
        """
        execution_id = str(uuid4())
        input_params = input_params or {}

        # Создаём запись выполнения
        execution = TemplateExecution(
            execution_id=execution_id,
            template_id=template.template_id,
            query=query,
            input_params=input_params,
            status=ExecutionStatus.RUNNING,
            user_id=user_id,
            started_at=datetime.now().isoformat()
        )

        self.executions[execution_id] = execution

        logger.info(f"🚀 Starting execution {execution_id} of template '{template.name}'")

        try:
            # Извлекаем параметры из триггера
            trigger_match = template.matches_query(query)
            if trigger_match and trigger_match.get("groups"):
                # Добавляем захваченные группы в input_params
                for i, group in enumerate(trigger_match["groups"]):
                    input_params[f"captured_{i+1}"] = group
                    if i == 0:
                        input_params["entity"] = group

            # Выполняем шаги
            step_results: Dict[str, Any] = {}

            sorted_steps = sorted(template.steps, key=lambda s: s.order)

            for step in sorted_steps:
                # Проверяем условие
                if step.condition and not self._evaluate_condition(step.condition, step_results, input_params):
                    logger.info(f"   ⏭️ Skipping step {step.step_id}: condition not met")
                    continue

                # Выполняем шаг
                step_start = datetime.now()

                try:
                    result = await self._execute_step(step, step_results, input_params)
                    step_results[step.step_id] = result

                    # Сохраняем под output_variable если указано
                    if step.output_variable:
                        step_results[step.output_variable] = result

                    step_duration = (datetime.now() - step_start).total_seconds() * 1000

                    logger.info(f"   ✅ Step {step.step_id} completed in {step_duration:.0f}ms")

                    # Callback
                    if on_step_complete:
                        await on_step_complete({
                            "step_id": step.step_id,
                            "agent": step.agent,
                            "action": step.action,
                            "status": "completed",
                            "duration_ms": step_duration,
                            "result_preview": str(result)[:200] if result else None
                        })

                except asyncio.TimeoutError:
                    logger.error(f"   ⏱️ Step {step.step_id} timed out")
                    step_results[step.step_id] = {"error": "timeout"}

                except Exception as e:
                    logger.error(f"   ❌ Step {step.step_id} failed: {e}")
                    step_results[step.step_id] = {"error": str(e)}

            # Формируем финальный результат
            execution.step_results = step_results
            execution.result = self._format_result(template, step_results)
            execution.result_data = step_results
            execution.status = ExecutionStatus.COMPLETED

        except Exception as e:
            logger.error(f"❌ Execution {execution_id} failed: {e}", exc_info=True)
            execution.status = ExecutionStatus.FAILED
            execution.error = str(e)

        finally:
            execution.completed_at = datetime.now().isoformat()
            if execution.started_at:
                started = datetime.fromisoformat(execution.started_at)
                completed = datetime.fromisoformat(execution.completed_at)
                execution.duration_ms = int((completed - started).total_seconds() * 1000)

            # Обновляем статистику шаблона
            template.usage_count += 1
            template.last_used_at = execution.completed_at

            logger.info(f"🏁 Execution {execution_id} finished: {execution.status.value}")

        return execution

    async def _execute_step(
        self,
        step: TemplateStep,
        step_results: Dict[str, Any],
        input_params: Dict[str, Any]
    ) -> Any:
        """
        Выполняет один шаг.
        """
        logger.info(f"   🔄 Executing step {step.step_id}: {step.agent}.{step.action}")

        # Резолвим параметры
        resolved_params = self.variable_resolver.resolve(
            step.params,
            step_results,
            input_params
        )

        # Получаем агента
        agent = self.agent_registry.get(step.agent)

        if not agent:
            # Пробуем выполнить через LLM напрямую
            return await self._execute_via_llm(step, resolved_params, step_results, input_params)

        # Получаем метод
        action_method = getattr(agent, step.action, None)

        if not action_method:
            logger.warning(f"   ⚠️ Agent {step.agent} has no action {step.action}")
            return await self._execute_via_llm(step, resolved_params, step_results, input_params)

        # Выполняем с таймаутом
        result = await asyncio.wait_for(
            action_method(**resolved_params),
            timeout=step.timeout_seconds
        )

        return result

    async def _execute_via_llm(
        self,
        step: TemplateStep,
        params: Dict[str, Any],
        step_results: Dict[str, Any],
        input_params: Dict[str, Any]
    ) -> Any:
        """
        Выполняет шаг через LLM если агент не найден.
        """
        if not self.llm_router:
            return {"error": f"Agent {step.agent} not found and no LLM router available"}

        # Формируем промпт
        prompt = f"""Выполни действие "{step.action}" агента "{step.agent}".

Параметры:
{json.dumps(params, ensure_ascii=False, indent=2)}

Предыдущие результаты:
{json.dumps(step_results, ensure_ascii=False, indent=2)[:2000]}

Входные данные:
{json.dumps(input_params, ensure_ascii=False, indent=2)}

Верни результат в JSON формате."""

        try:
            # Трекинг использования LLM для workflow
            async with UsageContext(agent_mode="workflow", request_type=f"workflow_step_{step.action}"):
                response = await self.llm_router.generate(
                    prompt=prompt,
                    system_prompt=f"Ты - агент {step.agent}, выполняющий действие {step.action}."
                )

            # Пробуем распарсить JSON
            try:
                json_match = re.search(r'\{.*\}', response, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
            except json.JSONDecodeError:
                logger.debug("suppressed exception", exc_info=True)

            return {"response": response}

        except Exception as e:
            logger.error(f"   ❌ LLM execution failed: {e}")
            return {"error": str(e)}

    def _evaluate_condition(
        self,
        condition: str,
        step_results: Dict[str, Any],
        input_params: Dict[str, Any]
    ) -> bool:
        """
        Оценивает условие выполнения шага.

        Поддерживает простые условия:
        - ${step_1.success} == true
        - ${step_1.count} > 0
        - ${input.depth} == "deep"
        """
        try:
            # Резолвим переменные
            resolved = self.variable_resolver.resolve(condition, step_results, input_params)

            # Простая оценка
            if isinstance(resolved, bool):
                return resolved
            if isinstance(resolved, str):
                # Пробуем eval для простых выражений
                # ВНИМАНИЕ: в продакшене нужна более безопасная реализация
                return bool(eval(resolved, {"__builtins__": {}}, {}))

            return bool(resolved)

        except Exception as e:
            logger.warning(f"   ⚠️ Condition evaluation failed: {e}")
            return True  # По умолчанию выполняем шаг

    def _format_result(
        self,
        template: WorkflowTemplate,
        step_results: Dict[str, Any]
    ) -> str:
        """
        Форматирует финальный результат.
        """
        # Берём результат последнего шага
        if not template.steps:
            return ""

        last_step = sorted(template.steps, key=lambda s: s.order)[-1]
        last_result = step_results.get(last_step.step_id) or step_results.get(last_step.output_variable)

        if not last_result:
            return "Результат не получен"

        # Форматируем в зависимости от output_format
        if template.output_format == "json":
            return json.dumps(last_result, ensure_ascii=False, indent=2)

        elif template.output_format == "markdown":
            if isinstance(last_result, dict):
                # Пробуем извлечь текстовое поле
                for key in ["response", "result", "report", "summary", "text", "content"]:
                    if key in last_result:
                        return str(last_result[key])
                return json.dumps(last_result, ensure_ascii=False, indent=2)
            return str(last_result)

        elif template.output_format == "table":
            # TODO: форматирование в таблицу
            return str(last_result)

        return str(last_result)

    def get_execution(self, execution_id: str) -> Optional[TemplateExecution]:
        """Получает информацию о выполнении."""
        return self.executions.get(execution_id)

    def get_executions_for_template(self, template_id: str, limit: int = 10) -> List[TemplateExecution]:
        """Получает историю выполнений шаблона."""
        executions = [
            e for e in self.executions.values()
            if e.template_id == template_id
        ]
        return sorted(executions, key=lambda e: e.started_at or "", reverse=True)[:limit]


# ============================================
# Singleton
# ============================================

_executor: Optional[TemplateExecutor] = None


def get_template_executor(
    llm_router=None,
    graph_builder=None,
    vector_indexer=None
) -> TemplateExecutor:
    """Получает singleton экземпляр TemplateExecutor."""
    global _executor

    if _executor is None:
        _executor = TemplateExecutor(
            llm_router=llm_router,
            graph_builder=graph_builder,
            vector_indexer=vector_indexer
        )

    return _executor


