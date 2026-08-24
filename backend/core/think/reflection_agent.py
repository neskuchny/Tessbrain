# -*- coding: utf-8 -*-
"""
🔍 Reflection Agent - Агент рефлексии и оценки качества.

Оценивает качество результатов поиска и определяет,
достаточно ли данных для ответа на вопрос.

Адаптировано из tess_search/module_10_2_intelligent_navigation.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ReflectionAgent:
    """
    Агент рефлексии для оценки качества результатов поиска.

    Оценивает:
    - Достаточность данных
    - Качество результатов
    - Полноту ответа
    - Необходимость дополнительного поиска

    Пример использования:
    ```python
    agent = ReflectionAgent(llm_router)

    reflection = await agent.evaluate_results(
        query="Чем занимается компания?",
        context={"nodes": [...], "vector_results": [...]},
        understanding={"query_type": "company_analysis"}
    )

    if reflection["is_sufficient"]:
        # Достаточно данных для ответа
    else:
        # Нужен дополнительный поиск
        retry_strategy = reflection["retry_strategy"]
    ```
    """

    # Пороги качества
    QUALITY_THRESHOLDS = {
        "excellent": 9,
        "good": 7,
        "acceptable": 5,
        "poor": 3
    }

    # Минимальные требования для разных типов запросов
    MIN_REQUIREMENTS = {
        "company_analysis": {"min_nodes": 3, "min_vectors": 2},
        "project_status": {"min_nodes": 2, "min_vectors": 1},
        "person_status": {"min_nodes": 1, "min_vectors": 1},
        "search_tasks": {"min_nodes": 1, "min_vectors": 0},
        "general": {"min_nodes": 1, "min_vectors": 1}
    }

    def __init__(self, llm_router=None):
        """
        Args:
            llm_router: LLM Router для оценки качества
        """
        self.llm = llm_router
        self.name = "ReflectionAgent"

    async def evaluate_results(
        self,
        query: str,
        context: Dict[str, Any],
        understanding: Optional[Dict[str, Any]] = None,
        previous_attempts: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """
        Оценить качество результатов поиска.

        Args:
            query: Исходный запрос
            context: Собранный контекст (nodes, vector_results, etc.)
            understanding: Понимание запроса (query_type, entities)
            previous_attempts: Предыдущие попытки поиска

        Returns:
            Результат оценки с рекомендациями
        """
        logger.info(f"🔍 Reflection: оцениваю результаты для '{query[:50]}...'")

        # Базовые метрики
        nodes_count = len(context.get("nodes", []))
        vectors_count = len(context.get("vector_results", []))
        total_data = nodes_count + vectors_count

        # Определяем тип запроса
        query_type = self._determine_query_type(query, understanding)

        # Получаем минимальные требования
        requirements = self.MIN_REQUIREMENTS.get(query_type, self.MIN_REQUIREMENTS["general"])

        # Оцениваем качество
        quality_score = self._calculate_quality_score(
            nodes_count, vectors_count, requirements, context
        )

        # Оцениваем полноту
        completeness = self._calculate_completeness(
            context, query_type, requirements
        )

        # Определяем достаточность
        is_sufficient = self._is_sufficient(quality_score, completeness, requirements)

        # Определяем проблемы
        issues = self._identify_issues(
            nodes_count, vectors_count, requirements, context, query
        )

        # Формируем стратегию повторного поиска если нужно
        retry_strategy = None
        if not is_sufficient:
            retry_strategy = self._create_retry_strategy(
                query, issues, previous_attempts, context
            )

        result = {
            "query": query,
            "query_type": query_type,
            "quality_score": quality_score,
            "completeness": completeness,
            "is_sufficient": is_sufficient,
            "issues": issues,
            "retry_strategy": retry_strategy,
            "metrics": {
                "nodes_count": nodes_count,
                "vectors_count": vectors_count,
                "total_data": total_data,
                "min_nodes_required": requirements["min_nodes"],
                "min_vectors_required": requirements["min_vectors"]
            },
            "timestamp": datetime.now().isoformat()
        }

        logger.info(
            f"   📊 Качество: {quality_score}/10, "
            f"Полнота: {completeness:.0%}, "
            f"Достаточно: {is_sufficient}"
        )

        return result

    def _determine_query_type(
        self,
        query: str,
        understanding: Optional[Dict[str, Any]]
    ) -> str:
        """Определяет тип запроса."""
        if understanding and understanding.get("query_type"):
            return understanding["query_type"]

        query_lower = query.lower()

        if any(word in query_lower for word in ["компания", "организация", "занимается"]):
            return "company_analysis"
        elif any(word in query_lower for word in ["проект", "статус"]):
            return "project_status"
        elif any(word in query_lower for word in ["человек", "сотрудник", "кто"]):
            return "person_status"
        elif any(word in query_lower for word in ["задач", "task"]):
            return "search_tasks"

        return "general"

    def _calculate_quality_score(
        self,
        nodes_count: int,
        vectors_count: int,
        requirements: Dict[str, int],
        context: Dict[str, Any]
    ) -> int:
        """Вычисляет оценку качества от 1 до 10."""
        score = 1  # Минимальный балл

        # Оценка по количеству узлов
        min_nodes = requirements["min_nodes"]
        if nodes_count >= min_nodes * 3:
            score += 3
        elif nodes_count >= min_nodes * 2:
            score += 2
        elif nodes_count >= min_nodes:
            score += 1

        # Оценка по количеству векторов
        min_vectors = requirements["min_vectors"]
        if min_vectors > 0:
            if vectors_count >= min_vectors * 3:
                score += 3
            elif vectors_count >= min_vectors * 2:
                score += 2
            elif vectors_count >= min_vectors:
                score += 1
        else:
            score += 1  # Если векторы не требуются, даём балл

        # Оценка по разнообразию данных
        node_types = set()
        for node in context.get("nodes", []):
            node_type = node.get("type", node.get("_label", ""))
            if node_type:
                node_types.add(node_type)

        if len(node_types) >= 3:
            score += 2
        elif len(node_types) >= 2:
            score += 1

        # Оценка по наличию важных данных
        has_important_data = False
        for node in context.get("nodes", []):
            if node.get("importance") == "high" or node.get("priority") == "high":
                has_important_data = True
                break

        if has_important_data:
            score += 1

        return min(score, 10)

    def _calculate_completeness(
        self,
        context: Dict[str, Any],
        query_type: str,
        requirements: Dict[str, int]
    ) -> float:
        """Вычисляет полноту от 0 до 1."""
        nodes_count = len(context.get("nodes", []))
        vectors_count = len(context.get("vector_results", []))

        # Базовая полнота по количеству
        nodes_completeness = min(nodes_count / max(requirements["min_nodes"] * 2, 1), 1.0)

        if requirements["min_vectors"] > 0:
            vectors_completeness = min(vectors_count / max(requirements["min_vectors"] * 2, 1), 1.0)
            base_completeness = (nodes_completeness + vectors_completeness) / 2
        else:
            base_completeness = nodes_completeness

        # Бонус за разнообразие типов
        node_types = set()
        for node in context.get("nodes", []):
            node_type = node.get("type", node.get("_label", ""))
            if node_type:
                node_types.add(node_type)

        diversity_bonus = min(len(node_types) * 0.1, 0.3)

        return min(base_completeness + diversity_bonus, 1.0)

    def _is_sufficient(
        self,
        quality_score: int,
        completeness: float,
        requirements: Dict[str, int]
    ) -> bool:
        """Определяет достаточность данных."""
        # Достаточно если качество >= 5 и полнота >= 40%
        return quality_score >= 5 and completeness >= 0.4

    def _identify_issues(
        self,
        nodes_count: int,
        vectors_count: int,
        requirements: Dict[str, int],
        context: Dict[str, Any],
        query: str
    ) -> List[str]:
        """Выявляет проблемы с результатами."""
        issues = []

        if nodes_count < requirements["min_nodes"]:
            issues.append(f"Недостаточно узлов в графе ({nodes_count} < {requirements['min_nodes']})")

        if requirements["min_vectors"] > 0 and vectors_count < requirements["min_vectors"]:
            issues.append(f"Недостаточно векторных результатов ({vectors_count} < {requirements['min_vectors']})")

        if nodes_count == 0 and vectors_count == 0:
            issues.append("Данные не найдены ни в графе, ни в векторном поиске")

        # Проверяем разнообразие
        node_types = set()
        for node in context.get("nodes", []):
            node_type = node.get("type", node.get("_label", ""))
            if node_type:
                node_types.add(node_type)

        if len(node_types) < 2 and nodes_count > 0:
            issues.append("Низкое разнообразие типов данных")

        return issues

    def _create_retry_strategy(
        self,
        query: str,
        issues: List[str],
        previous_attempts: Optional[List[Dict]],
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Создаёт стратегию повторного поиска."""
        strategy = {
            "approach": "expand_search",
            "changes": {},
            "confidence": 0.6
        }

        # Определяем количество предыдущих попыток
        attempts_count = len(previous_attempts) if previous_attempts else 0

        if attempts_count == 0:
            # Первая неудачная попытка - расширяем поиск
            strategy["approach"] = "expand_search"
            strategy["changes"] = {
                "max_nodes": 100,
                "expand_search_depth": True,
                "use_vector_search": True
            }
            strategy["confidence"] = 0.7

        elif attempts_count == 1:
            # Вторая попытка - используем гибридный поиск
            strategy["approach"] = "hybrid_search"
            strategy["changes"] = {
                "use_vector_search": True,
                "disable_smart_filter": True,
                "max_nodes": 200
            }
            strategy["confidence"] = 0.5

        else:
            # Третья+ попытка - простой векторный fallback
            strategy["approach"] = "simple_vector_fallback"
            strategy["changes"] = {
                "action": "simple_vector_fallback",
                "top_k": 50
            }
            strategy["confidence"] = 0.3

        # Специфические изменения на основе проблем
        if "Данные не найдены" in str(issues):
            strategy["changes"]["use_fallback_search"] = True

        return strategy

    async def assess_answer_quality(
        self,
        query: str,
        answer: str,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Оценивает качество сгенерированного ответа.

        Args:
            query: Исходный запрос
            answer: Сгенерированный ответ
            context: Контекст, использованный для генерации

        Returns:
            Оценка качества ответа
        """
        assessment = {
            "answer_length": len(answer),
            "has_structure": False,
            "has_data": False,
            "is_actionable": False,
            "quality_score": 5
        }

        # Проверяем структуру
        if any(marker in answer for marker in ["**", "##", "- ", "• ", "1.", "📊", "✅"]):
            assessment["has_structure"] = True
            assessment["quality_score"] += 1

        # Проверяем наличие данных
        if any(char.isdigit() for char in answer):
            assessment["has_data"] = True
            assessment["quality_score"] += 1

        # Проверяем actionable контент
        if any(word in answer.lower() for word in ["рекомендую", "следует", "можно", "нужно"]):
            assessment["is_actionable"] = True
            assessment["quality_score"] += 1

        # Штраф за слишком короткий ответ
        if len(answer) < 100:
            assessment["quality_score"] -= 2

        # Штраф за инструкции вместо ответа
        if any(phrase in answer.lower() for phrase in ["для создания отчета", "необходимо определить"]):
            assessment["quality_score"] -= 3
            assessment["contains_instructions"] = True

        assessment["quality_score"] = max(1, min(10, assessment["quality_score"]))

        return assessment



