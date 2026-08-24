# -*- coding: utf-8 -*-
"""
Task Specification System Orchestrator - главный оркестратор системы ТЗ.

ГЛАВНЫЙ ПРИНЦИП: ТЗ создаётся на основе РЕАЛЬНЫХ данных компании!

Алгоритм:
1. Понять задачу - что нужно сделать
2. Определить какие данные нужны
3. Поискать данные в графах, векторах, снепшотах
4. Собрать контекст - факты, мнения, цифры
5. Создать ТЗ на основе реальных данных
6. Выполнить задачу (опционально)
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Импортируем новую data-driven систему
from .data_driven_orchestrator import DataDrivenTaskSystem

logger = logging.getLogger("task_spec.orchestrator")


class TaskSpecificationSystem:
    """
    Главный оркестратор системы генерации ТЗ и выполнения задач.

    КЛЮЧЕВОЙ ПРИНЦИП: Всё основано на РЕАЛЬНЫХ данных компании!

    Пример использования:
    ```python
    system = TaskSpecificationSystem(storage=storage, llm_router=llm_router)

    # Полный цикл: понять -> поискать -> собрать -> создать ТЗ
    result = await system.process_task(
        task_description="Написать финмодель для чатбота риелторов",
        execution_mode="plan_only"
    )

    # Результат содержит:
    # - found_data: что нашли в данных компании
    # - missing_data: чего не хватает
    # - assumptions: что пришлось предположить
    # - specification: ТЗ на основе реальных данных
    ```
    """

    def __init__(
        self,
        storage=None,
        llm_router=None,
        output_dir: str | None = None,
        tool_registry: Optional[Dict[str, Callable]] = None
    ):
        """
        Инициализация системы.

        Args:
            storage: Хранилище данных (векторное + граф)
            llm_router: Роутер для LLM запросов
            output_dir: Директория для сохранения ТЗ
            tool_registry: Реестр инструментов для выполнения
        """
        self.storage = storage
        self.llm_router = llm_router
        self.output_dir = Path(output_dir) if output_dir else Path("task_specifications")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Инициализация data-driven системы
        self.data_driven_system = DataDrivenTaskSystem(
            storage=storage,
            llm_router=llm_router,
            output_dir=str(self.output_dir)
        )

        # Реестр инструментов
        self.tool_registry = tool_registry or {}

        # История обработки
        self.processing_history: List[Dict[str, Any]] = []

        # НОВОЕ: Агентская память для обучения на прошлых ТЗ
        self._agent_memory_enabled = False
        self._init_agent_memory()

        logger.info("TaskSpecificationSystem инициализирован")

    def _init_agent_memory(self):
        """Инициализация агентской памяти."""
        try:
            from backend.core.config import flags
            if flags.enable_agent_memory:
                self._agent_memory_enabled = True
                logger.info("✅ Agent memory enabled for TaskSpec")
        except Exception as e:
            logger.debug(f"Agent memory not available: {e}")

    async def process_task(
        self,
        task_description: str,
        additional_context: Optional[Dict[str, Any]] = None,
        execution_mode: str = "plan_only",
        skip_execution: bool = False
    ) -> Dict[str, Any]:
        """
        Полный цикл обработки задачи на основе РЕАЛЬНЫХ данных.

        ЭТАПЫ:
        1. ПОНЯТЬ задачу - определить тип, нужные данные
        2. СПЛАНИРОВАТЬ поиск - где искать, какие запросы
        3. СОБРАТЬ данные - из графов, векторов, снепшотов
        4. ПРОАНАЛИЗИРОВАТЬ полноту - что нашли, чего не хватает
        5. СОБРАТЬ контекст - факты, мнения, цифры, предположения
        6. СОЗДАТЬ ТЗ - на основе реальных данных

        Args:
            task_description: Описание задачи
            additional_context: Дополнительный контекст
            execution_mode: Режим выполнения (plan_only|supervised|autonomous)
            skip_execution: Пропустить этап выполнения

        Returns:
            Результат с ТЗ на основе реальных данных:
            {
                "task_id": str,
                "status": "completed|failed",
                "found_data": {...},  # Что нашли
                "missing_data": [...],  # Чего не хватает
                "assumptions": [...],  # Что предположили
                "stages": {...},  # Результаты каждого этапа
                "file_paths": {...},  # Пути к файлам
                "metadata": {...}
            }
        """
        task_id = self._generate_task_id()
        start_time = datetime.now()

        logger.info("="*70)
        logger.info(f"🚀 НОВАЯ ЗАДАЧА: {task_description[:80]}...")
        logger.info(f"   ID: {task_id}")
        logger.info(f"   Режим: {execution_mode}")
        logger.info("="*70)

        try:
            # Используем data-driven систему
            mode = "full" if not skip_execution else "quick"

            # Аудит #5: ЗАМКНУТЬ цикл обучения. Раньше прошлые ТЗ сохранялись в
            # agent-memory, но НЕ переиспользовались. Теперь перед генерацией
            # подмешиваем похожие прошлые задачи в контекст (best-effort, за тем
            # же gate _agent_memory_enabled). Не роняет генерацию.
            ctx = dict(additional_context or {})
            try:
                similar = await self.get_similar_tasks_from_memory(
                    task_description, limit=3)
                if similar:
                    ctx["similar_past_tasks"] = similar
                    logger.info(f"   🧠 Подмешано похожих прошлых ТЗ: {len(similar)}")
            except Exception as e:
                logger.debug(f"similar-tasks recall skipped: {e}")

            result = await self.data_driven_system.process_task(
                task_description=task_description,
                additional_context=ctx,
                mode=mode
            )

            # Добавляем task_id
            result["task_id"] = task_id

            # Если нужно выполнение и есть план
            if not skip_execution and execution_mode != "plan_only":
                execution_result = await self._execute_from_spec(
                    result.get("stages", {}).get("result", {}),
                    task_id,
                    execution_mode
                )
                result["stages"]["execution"] = execution_result

            # Сохраняем в историю
            self.processing_history.append({
                "task_id": task_id,
                "task_description": task_description,
                "status": result.get("status", "unknown"),
                "timestamp": start_time.isoformat()
            })

            # НОВОЕ: Сохраняем в агентскую память для обучения
            await self._save_to_agent_memory(task_description, result)

            logger.info("="*70)
            logger.info(f"✅ ЗАДАЧА ЗАВЕРШЕНА: {result.get('status', 'unknown')}")
            logger.info(f"   Найдено данных: {len(result.get('found_data', {}))}")
            logger.info(f"   Пробелов: {len(result.get('missing_data', []))}")
            logger.info(f"   Предположений: {len(result.get('assumptions', []))}")
            logger.info("="*70)

            return result

        except Exception as e:
            logger.error(f"❌ Ошибка обработки: {e}")
            return {
                "task_id": task_id,
                "task_description": task_description,
                "status": "failed",
                "errors": [str(e)],
                "metadata": {
                    "started_at": start_time.isoformat(),
                    "error": str(e)
                }
            }

    async def _execute_from_spec(
        self,
        specification: Dict[str, Any],
        task_id: str,
        execution_mode: str
    ) -> Dict[str, Any]:
        """
        Выполнение задачи на основе сгенерированной спецификации.

        Args:
            specification: Спецификация задачи
            task_id: ID задачи
            execution_mode: Режим выполнения

        Returns:
            Результат выполнения
        """
        logger.info(f"⚡ Выполнение задачи: {task_id} [{execution_mode}]")

        sections = specification.get("sections", [])
        next_steps = specification.get("next_steps", [])

        execution_result = {
            "mode": execution_mode,
            "status": "plan_created",
            "plan": {
                "sections_count": len(sections),
                "next_steps": next_steps
            },
            "actions_taken": [],
            "results": []
        }

        if execution_mode == "plan_only":
            # Только план - не выполняем
            execution_result["status"] = "plan_created"
            execution_result["message"] = "План создан, выполнение не требуется"
            return execution_result

        # supervised/autonomous: подключаем РЕАЛЬНЫЙ TaskExecutorAgent
        # (аудит-фикс: раньше autonomous был TODO-заглушкой, а рабочий код —
        # в невызываемом классе). БЕЗОПАСНОСТЬ: и supervised, и autonomous в
        # TaskExecutorAgent реально ЗАПУСКАЮТ авто-инструменты (_safe_tool_call).
        # Поэтому ЛЮБОЕ выполнение — только за флагом enable_autonomous_execution.
        # При OFF деградируем в plan_only (детальный план, НОЛЬ выполнения).
        try:
            from backend.core.config.feature_flags import get_feature_flags
            allowed = get_feature_flags().enable_autonomous_execution
        except Exception:
            allowed = False
        eff_mode = execution_mode
        if not allowed:
            eff_mode = "plan_only"
            execution_result["downgraded_from"] = execution_mode
            execution_result["downgrade_reason"] = (
                "enable_autonomous_execution выключен — реальное выполнение "
                "инструментов запрещено по умолчанию (только детальный план)")

        try:
            from .task_executor import TaskExecutorAgent
            executor = TaskExecutorAgent(
                llm_router=self.llm_router,
                tool_registry=self.tool_registry,
            )
            report = await executor.execute(specification, task_id, eff_mode)
            # сохраняем пометку о деградации (если была) поверх отчёта
            if execution_result.get("downgraded_from"):
                report["downgraded_from"] = execution_result["downgraded_from"]
                report["downgrade_reason"] = execution_result.get("downgrade_reason")
            report.setdefault("mode", eff_mode)
            return report
        except Exception as e:
            logger.error(f"❌ Выполнение задачи провалено: {e}")
            execution_result["status"] = "execution_failed"
            execution_result["error"] = str(e)
            return execution_result

    def register_tool(self, name: str, func: Callable, description: str = ""):
        """
        Регистрация инструмента для выполнения задач.

        Args:
            name: Название инструмента
            func: Функция инструмента
            description: Описание
        """
        self.tool_registry[name] = {
            "func": func,
            "description": description
        }
        logger.info(f"Инструмент зарегистрирован: {name}")

    def _generate_task_id(self) -> str:
        """Генерация уникального ID задачи."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique = uuid.uuid4().hex[:8]
        return f"TASK_{timestamp}_{unique}"

    async def _save_to_agent_memory(
        self,
        task_description: str,
        result: Dict[str, Any]
    ):
        """Сохранить результат в агентскую память для обучения."""
        if not self._agent_memory_enabled:
            return

        try:
            from backend.core.memory import get_memgpt_manager

            memgpt = get_memgpt_manager()

            # Сохраняем успешные ТЗ для обучения
            if result.get("status") == "completed":
                spec = result.get("stages", {}).get("result", {})

                await memgpt.agent_remember(
                    agent_id="task_spec_generator",
                    content={
                        "task_description": task_description[:500],
                        "task_type": spec.get("task_type", "unknown"),
                        "domain": spec.get("domain", "unknown"),
                        "found_data_count": len(result.get("found_data", {})),
                        "missing_data_count": len(result.get("missing_data", [])),
                        "assumptions_count": len(result.get("assumptions", [])),
                        "success": True,
                    },
                    memory_type="episodic",
                    importance=0.8,
                    tags=["task_spec", "successful", spec.get("task_type", "unknown")]
                )
                logger.debug("💾 Saved TaskSpec to agent memory")

        except Exception as e:
            logger.debug(f"Could not save to agent memory: {e}")

    async def get_similar_tasks_from_memory(
        self,
        task_description: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Получить похожие задачи из памяти агента."""
        if not self._agent_memory_enabled:
            return []

        try:
            from backend.core.memory import get_memgpt_manager

            memgpt = get_memgpt_manager()

            memories = await memgpt.agent_recall(
                agent_id="task_spec_generator",
                query=task_description,
                memory_type="episodic",
                tags=["task_spec", "successful"],
                limit=limit
            )

            return [
                {
                    "task_description": m.get("content", {}).get("task_description", ""),
                    "task_type": m.get("content", {}).get("task_type", ""),
                    "domain": m.get("content", {}).get("domain", ""),
                }
                for m in memories
            ]

        except Exception as e:
            logger.debug(f"Could not recall from agent memory: {e}")
            return []

    def get_processing_history(self) -> List[Dict[str, Any]]:
        """Получить историю обработки."""
        return self.processing_history

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Получить статус задачи."""
        for item in self.processing_history:
            if item.get("task_id") == task_id:
                return item
        return None


# Функция для быстрого создания системы
def create_task_specification_system(
    storage=None,
    llm_router=None,
    output_dir: str | None = None,
    tools: Optional[Dict[str, Callable]] = None
) -> TaskSpecificationSystem:
    """
    Фабричная функция для создания системы ТЗ.

    Args:
        storage: Хранилище данных
        llm_router: Роутер LLM
        output_dir: Директория для файлов
        tools: Словарь инструментов

    Returns:
        Настроенная система
    """
    system = TaskSpecificationSystem(
        storage=storage,
        llm_router=llm_router,
        output_dir=output_dir,
        tool_registry=tools
    )
    return system
