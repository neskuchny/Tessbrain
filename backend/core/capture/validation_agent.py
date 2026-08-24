# -*- coding: utf-8 -*-
"""
Validation Agent - Валидация извлечённых данных.

Обеспечивает:
- Проверку релевантности данных для компании
- Фильтрацию внешних данных (чужие кейсы, обучение)
- Проверку согласованности с существующими данными
- Оценку качества извлечения

Использование:
```python
from backend.core.capture.validation_agent import ValidationAgent

validator = ValidationAgent(llm_router, graph_builder)

# Валидация извлечённых данных
result = await validator.validate_extracted_data(
    entities=extracted_entities,
    company_context=company_snapshot
)

# Фильтрация внешних данных
filtered = await validator.filter_external_data(
    entities=entities,
    transcript=transcript
)
```
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ValidationAgent:
    """
    Агент валидации извлечённых данных.

    Функции:
    1. Проверка релевантности для компании
    2. Фильтрация внешних данных
    3. Проверка согласованности
    4. Оценка качества
    """

    # Ключевые слова для определения внешних данных
    EXTERNAL_KEYWORDS = [
        # Чужие компании
        "их компания", "у них", "в той компании", "другая компания",
        "конкурент", "competitor", "их опыт", "их кейс",
        # Обучение
        "обучение", "тренинг", "training", "курс", "лекция",
        "преподаватель", "trainer", "спикер", "speaker",
        # Примеры
        "например", "к примеру", "допустим", "представьте",
        "гипотетически", "теоретически", "в теории",
    ]

    # Ключевые слова для определения внутренних данных
    INTERNAL_KEYWORDS = [
        "наш", "наша", "наше", "наши", "у нас", "мы",
        "наш проект", "наша команда", "наш клиент",
        "в нашей компании", "наш отдел",
    ]

    def __init__(self, llm_router=None, graph_builder=None):
        """
        Args:
            llm_router: LLM Router для глубокой валидации
            graph_builder: GraphBuilder для проверки согласованности
        """
        self.llm = llm_router
        self.graph = graph_builder

    async def validate_extracted_data(
        self,
        entities: List[Dict[str, Any]],
        decisions: Optional[List[Dict[str, Any]]] = None,
        tasks: Optional[List[Dict[str, Any]]] = None,
        company_context: Optional[str] = None,
        transcript: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Валидация извлечённых данных.

        Args:
            entities: Извлечённые сущности
            decisions: Извлечённые решения
            tasks: Извлечённые задачи
            company_context: Контекст компании (snapshot)
            transcript: Транскрипт для контекста

        Returns:
            Результат валидации с рекомендациями
        """
        result = {
            "is_valid": True,
            "entities": {"valid": [], "invalid": [], "uncertain": []},
            "decisions": {"valid": [], "invalid": [], "uncertain": []},
            "tasks": {"valid": [], "invalid": [], "uncertain": []},
            "issues": [],
            "recommendations": [],
            "quality_score": 1.0,
        }

        try:
            from backend.core.config import flags
            if not flags.enable_validation_agent:
                # Просто возвращаем всё как валидное
                result["entities"]["valid"] = entities or []
                result["decisions"]["valid"] = decisions or []
                result["tasks"]["valid"] = tasks or []
                return result
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)

        # 1. Валидация сущностей
        if entities:
            for entity in entities:
                validation = await self._validate_entity(entity, company_context, transcript)

                if validation["status"] == "valid":
                    result["entities"]["valid"].append(entity)
                elif validation["status"] == "invalid":
                    result["entities"]["invalid"].append({
                        **entity,
                        "validation_reason": validation["reason"]
                    })
                else:
                    result["entities"]["uncertain"].append({
                        **entity,
                        "validation_reason": validation["reason"]
                    })

        # 2. Валидация решений
        if decisions:
            for decision in decisions:
                validation = await self._validate_decision(decision, company_context)

                if validation["status"] == "valid":
                    result["decisions"]["valid"].append(decision)
                elif validation["status"] == "invalid":
                    result["decisions"]["invalid"].append({
                        **decision,
                        "validation_reason": validation["reason"]
                    })
                else:
                    result["decisions"]["uncertain"].append(decision)

        # 3. Валидация задач
        if tasks:
            for task in tasks:
                validation = await self._validate_task(task, company_context)

                if validation["status"] == "valid":
                    result["tasks"]["valid"].append(task)
                elif validation["status"] == "invalid":
                    result["tasks"]["invalid"].append({
                        **task,
                        "validation_reason": validation["reason"]
                    })
                else:
                    result["tasks"]["uncertain"].append(task)

        # 4. Вычисляем общий quality_score
        total = len(entities or []) + len(decisions or []) + len(tasks or [])
        valid = (
            len(result["entities"]["valid"]) +
            len(result["decisions"]["valid"]) +
            len(result["tasks"]["valid"])
        )

        if total > 0:
            result["quality_score"] = valid / total

        # 5. Определяем is_valid
        result["is_valid"] = result["quality_score"] >= 0.7

        # 6. Формируем рекомендации
        if result["entities"]["invalid"]:
            result["recommendations"].append(
                f"Удалено {len(result['entities']['invalid'])} нерелевантных сущностей"
            )

        if result["entities"]["uncertain"]:
            result["recommendations"].append(
                f"Требуется проверка {len(result['entities']['uncertain'])} сущностей"
            )

        logger.info(
            f"Validation complete: quality={result['quality_score']:.2f}, "
            f"valid={valid}/{total}"
        )

        return result

    async def _validate_entity(
        self,
        entity: Dict[str, Any],
        company_context: Optional[str],
        transcript: Optional[str]
    ) -> Dict[str, Any]:
        """Валидация одной сущности."""
        name = entity.get("name", "")
        entity_type = entity.get("entity_type", "")
        description = entity.get("description", "")

        # Быстрая проверка по ключевым словам
        text_to_check = f"{name} {description}".lower()

        # Проверка на внешние данные
        external_score = sum(
            1 for kw in self.EXTERNAL_KEYWORDS
            if kw.lower() in text_to_check
        )

        internal_score = sum(
            1 for kw in self.INTERNAL_KEYWORDS
            if kw.lower() in text_to_check
        )

        # Если явно внешние данные
        if external_score > internal_score and external_score >= 2:
            return {
                "status": "invalid",
                "reason": "Внешние данные (чужой кейс/обучение)"
            }

        # Проверка на слишком общие сущности
        generic_names = ["проект", "задача", "клиент", "система", "продукт"]
        if name.lower() in generic_names:
            return {
                "status": "uncertain",
                "reason": "Слишком общее название"
            }

        # Проверка на пустые/короткие имена
        if len(name) < 2:
            return {
                "status": "invalid",
                "reason": "Слишком короткое имя"
            }

        # Проверка согласованности с графом
        if self.graph and self.graph.use_networkx:
            existing = await self._check_graph_consistency(name, entity_type)
            if existing.get("conflict"):
                return {
                    "status": "uncertain",
                    "reason": f"Конфликт с существующими данными: {existing['conflict']}"
                }

        return {"status": "valid", "reason": ""}

    async def _validate_decision(
        self,
        decision: Dict[str, Any],
        company_context: Optional[str]
    ) -> Dict[str, Any]:
        """Валидация решения."""
        text = decision.get("decision", "") or decision.get("text", "")

        if len(text) < 10:
            return {
                "status": "invalid",
                "reason": "Слишком короткое описание решения"
            }

        # Проверка на внешние данные
        text_lower = text.lower()
        if any(kw.lower() in text_lower for kw in self.EXTERNAL_KEYWORDS[:6]):
            return {
                "status": "uncertain",
                "reason": "Возможно внешние данные"
            }

        return {"status": "valid", "reason": ""}

    async def _validate_task(
        self,
        task: Dict[str, Any],
        company_context: Optional[str]
    ) -> Dict[str, Any]:
        """Валидация задачи."""
        title = task.get("title", "") or task.get("name", "")

        if len(title) < 5:
            return {
                "status": "invalid",
                "reason": "Слишком короткое название задачи"
            }

        # Проверка на наличие исполнителя
        assignee = task.get("assignee", "") or task.get("responsible", "")
        if not assignee:
            return {
                "status": "uncertain",
                "reason": "Не указан исполнитель"
            }

        return {"status": "valid", "reason": ""}

    async def _check_graph_consistency(
        self,
        entity_name: str,
        entity_type: str
    ) -> Dict[str, Any]:
        """Проверка согласованности с графом."""
        result = {"exists": False, "conflict": None}

        if not self.graph or not self.graph.nx_graph:
            return result

        # Ищем существующую сущность
        for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
            if attrs.get("name", "").lower() == entity_name.lower():
                result["exists"] = True

                # Проверяем тип
                existing_type = attrs.get("entity_type", "")
                if existing_type and existing_type != entity_type:
                    result["conflict"] = f"Тип '{entity_type}' vs существующий '{existing_type}'"

                break

        return result

    async def filter_external_data(
        self,
        entities: List[Dict[str, Any]],
        transcript: str
    ) -> Dict[str, Any]:
        """
        Фильтрация внешних данных.

        Args:
            entities: Список сущностей
            transcript: Транскрипт для контекста

        Returns:
            Отфильтрованные данные
        """
        result = {
            "internal": [],
            "external": [],
            "uncertain": [],
        }

        try:
            from backend.core.config import flags
            if not flags.enable_external_data_filtering:
                result["internal"] = entities
                return result
        except ImportError:
            logger.debug("suppressed exception", exc_info=True)

        # Анализируем контекст транскрипта
        transcript_lower = transcript.lower()

        # Определяем общий контекст
        is_training_session = any(
            kw in transcript_lower
            for kw in ["обучение", "тренинг", "курс", "лекция", "семинар"]
        )

        is_external_case = any(
            kw in transcript_lower
            for kw in ["их компания", "у них", "конкурент", "другая компания"]
        )

        for entity in entities:
            name = entity.get("name", "").lower()
            description = entity.get("description", "").lower()

            # Если это явно обучающая сессия
            if is_training_session:
                # Проверяем связь с внутренними данными
                if any(kw in name or kw in description for kw in self.INTERNAL_KEYWORDS):
                    result["internal"].append(entity)
                else:
                    result["external"].append(entity)
                continue

            # Если обсуждают внешний кейс
            if is_external_case:
                # Большинство сущностей - внешние
                if any(kw in name or kw in description for kw in self.INTERNAL_KEYWORDS):
                    result["internal"].append(entity)
                else:
                    result["uncertain"].append(entity)
                continue

            # По умолчанию - внутренние
            result["internal"].append(entity)

        logger.info(
            f"External data filter: {len(result['internal'])} internal, "
            f"{len(result['external'])} external, "
            f"{len(result['uncertain'])} uncertain"
        )

        return result

    async def validate_specification(
        self,
        specification: Dict[str, Any],
        company_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Валидация технического задания.

        Args:
            specification: ТЗ для валидации
            company_context: Контекст компании

        Returns:
            Результат валидации
        """
        result = {
            "is_valid": True,
            "issues": [],
            "suggestions": [],
        }

        # Проверка обязательных полей
        required_fields = ["title", "description", "goals"]
        for field in required_fields:
            if not specification.get(field):
                result["issues"].append(f"Отсутствует обязательное поле: {field}")
                result["is_valid"] = False

        # Проверка длины описания
        description = specification.get("description", "")
        if len(description) < 50:
            result["issues"].append("Слишком короткое описание")
            result["is_valid"] = False

        # Проверка целей
        goals = specification.get("goals", [])
        if not goals:
            result["issues"].append("Не указаны цели")
            result["is_valid"] = False

        # Проверка плана выполнения
        execution_plan = specification.get("execution_plan", {})
        if not execution_plan.get("steps"):
            result["suggestions"].append("Рекомендуется добавить план выполнения")

        return result


# Singleton
_validator_instance: Optional[ValidationAgent] = None


def get_validation_agent(llm_router=None, graph_builder=None) -> ValidationAgent:
    """Получить экземпляр ValidationAgent."""
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = ValidationAgent(llm_router, graph_builder)
    return _validator_instance


