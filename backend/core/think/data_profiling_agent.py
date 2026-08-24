# -*- coding: utf-8 -*-
"""
Data Profiling Agent - профилирование доступных данных перед поиском.

Анализирует граф и определяет:
- Какие типы данных доступны
- Какие проекты/встречи/люди есть
- Временные диапазоны данных
- Качество и полноту данных
"""
import logging
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class DataProfilingAgent:
    """
    Агент для профилирования доступных данных.

    Помогает системе понять, какие данные доступны для ответа на запрос,
    и оптимизировать стратегию поиска.
    """

    def __init__(self, graph_builder=None, vector_indexer=None):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            vector_indexer: VectorIndexer для доступа к векторному индексу
        """
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer
        self._profile_cache = None
        self._cache_timestamp = None
        self._cache_ttl = 300  # 5 минут

    async def profile_available_data(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Профилировать все доступные данные.

        Returns:
            Профиль данных с метриками и статистикой
        """
        # Проверяем кеш
        if not force_refresh and self._profile_cache and self._cache_timestamp:
            age = (datetime.now() - self._cache_timestamp).total_seconds()
            if age < self._cache_ttl:
                return self._profile_cache

        logger.info("📊 DataProfilingAgent: профилирование данных...")

        profile = {
            "timestamp": datetime.now().isoformat(),
            "graph": await self._profile_graph(),
            "vectors": await self._profile_vectors(),
            "data_quality": {},
            "recommendations": []
        }

        # Оцениваем качество данных
        profile["data_quality"] = self._assess_data_quality(profile)

        # Генерируем рекомендации
        profile["recommendations"] = self._generate_recommendations(profile)

        # Кешируем
        self._profile_cache = profile
        self._cache_timestamp = datetime.now()

        logger.info(f"   ✅ Профиль создан: {profile['graph']['total_nodes']} узлов, {profile['vectors']['total_vectors']} векторов")

        return profile

    async def _profile_graph(self) -> Dict[str, Any]:
        """Профилировать граф."""
        if not self.graph_builder or not self.graph_builder.nx_graph:
            return {
                "available": False,
                "total_nodes": 0,
                "total_edges": 0,
                "node_types": {},
                "projects": [],
                "people": [],
                "date_range": None
            }

        graph = self.graph_builder.nx_graph
        nodes = [dict(graph.nodes[n]) | {"id": n} for n in graph.nodes()]

        # Подсчёт по типам
        node_types = Counter(n.get("_label", "Unknown") for n in nodes)

        # Проекты
        projects = [
            {"id": n.get("id"), "name": n.get("name", "?")}
            for n in nodes if n.get("_label") == "Project"
        ]

        # Люди
        people = [
            {"id": n.get("id"), "name": n.get("name", "?"), "role": n.get("role", "")}
            for n in nodes if n.get("_label") == "Person"
        ]

        # Встречи с датами
        meetings = [n for n in nodes if n.get("_label") == "Meeting"]
        dates = []
        for m in meetings:
            date_str = m.get("date") or m.get("created_at", "")
            if date_str:
                try:
                    if "T" in str(date_str):
                        dates.append(date_str[:10])
                    else:
                        dates.append(str(date_str)[:10])
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        date_range = None
        if dates:
            dates_sorted = sorted(dates)
            date_range = {
                "earliest": dates_sorted[0],
                "latest": dates_sorted[-1],
                "count": len(dates)
            }

        # Задачи по статусам
        tasks = [n for n in nodes if n.get("_label") == "Task"]
        task_statuses = Counter(t.get("status", "unknown") for t in tasks)

        # Сущности по типам
        entities = [n for n in nodes if n.get("_label") == "Entity"]
        entity_types = Counter(e.get("entity_type", "unknown") for e in entities)

        return {
            "available": True,
            "total_nodes": graph.number_of_nodes(),
            "total_edges": graph.number_of_edges(),
            "node_types": dict(node_types),
            "projects": projects,
            "projects_count": len(projects),
            "people": people,
            "people_count": len(people),
            "meetings_count": len(meetings),
            "date_range": date_range,
            "task_statuses": dict(task_statuses),
            "entity_types": dict(entity_types),
            "decisions_count": node_types.get("Decision", 0),
            "kpis_count": node_types.get("KPI", 0),
        }

    async def _profile_vectors(self) -> Dict[str, Any]:
        """Профилировать векторный индекс."""
        if not self.vector_indexer:
            return {
                "available": False,
                "total_vectors": 0,
                "collections": {}
            }

        stats = self.vector_indexer.get_stats() if hasattr(self.vector_indexer, 'get_stats') else {}

        return {
            "available": True,
            "total_vectors": stats.get("total_vectors", len(getattr(self.vector_indexer, '_vectors', {}))),
            "backend": getattr(self.vector_indexer, 'backend', 'unknown'),
            "embedding_model": getattr(self.vector_indexer, 'embedding_model_name', 'unknown'),
        }

    def _assess_data_quality(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Оценить качество данных."""
        graph = profile.get("graph", {})
        vectors = profile.get("vectors", {})

        scores = {
            "completeness": 0,
            "richness": 0,
            "searchability": 0,
            "overall": 0
        }

        # Полнота (есть ли разные типы данных)
        node_types = graph.get("node_types", {})
        expected_types = ["Meeting", "Person", "Task", "Decision", "Entity", "Project"]
        present_types = sum(1 for t in expected_types if node_types.get(t, 0) > 0)
        scores["completeness"] = round(present_types / len(expected_types) * 100)

        # Богатство (количество связей на узел)
        total_nodes = graph.get("total_nodes", 0)
        total_edges = graph.get("total_edges", 0)
        if total_nodes > 0:
            avg_edges = total_edges / total_nodes
            scores["richness"] = min(100, round(avg_edges * 50))  # 2 связи = 100%

        # Поисковая готовность (векторы vs узлы)
        total_vectors = vectors.get("total_vectors", 0)
        if total_nodes > 0:
            coverage = total_vectors / total_nodes
            scores["searchability"] = min(100, round(coverage * 100))

        # Общий балл
        scores["overall"] = round(
            (scores["completeness"] * 0.3 +
             scores["richness"] * 0.3 +
             scores["searchability"] * 0.4)
        )

        return scores

    def _generate_recommendations(self, profile: Dict[str, Any]) -> List[str]:
        """Сгенерировать рекомендации по улучшению данных."""
        recommendations = []
        graph = profile.get("graph", {})
        vectors = profile.get("vectors", {})
        quality = profile.get("data_quality", {})

        # Проверяем покрытие векторами
        total_nodes = graph.get("total_nodes", 0)
        total_vectors = vectors.get("total_vectors", 0)
        if total_nodes > 0 and total_vectors < total_nodes * 0.8:
            recommendations.append(
                f"Рекомендуется пересобрать векторный индекс. "
                f"Покрытие: {total_vectors}/{total_nodes} ({round(total_vectors/total_nodes*100)}%)"
            )

        # Проверяем наличие ключевых типов
        node_types = graph.get("node_types", {})
        if node_types.get("Person", 0) == 0:
            recommendations.append("В графе нет информации о людях. Запустите обогащение данных.")
        if node_types.get("Task", 0) == 0:
            recommendations.append("В графе нет задач. Запустите обогащение данных.")
        if node_types.get("Decision", 0) == 0:
            recommendations.append("В графе нет решений. Запустите обогащение данных.")

        # Проверяем связность
        if quality.get("richness", 0) < 50:
            recommendations.append(
                "Низкая связность графа. Рекомендуется добавить больше связей между сущностями."
            )

        return recommendations

    async def get_data_availability_for_query(self, query: str, query_type: str | None = None) -> Dict[str, Any]:
        """
        Определить доступность данных для конкретного запроса.

        Args:
            query: Текст запроса
            query_type: Тип запроса (person_status, project_info, etc.)

        Returns:
            Информация о доступных данных для запроса
        """
        profile = await self.profile_available_data()
        graph = profile.get("graph", {})

        availability = {
            "can_answer": True,
            "confidence": "high",
            "available_data": [],
            "missing_data": [],
            "suggestions": []
        }

        # Анализируем запрос и проверяем доступность данных
        query_lower = query.lower()

        # Проверка на людей
        if any(word in query_lower for word in ["кто", "человек", "сотрудник", "участник", "команда"]):
            if graph.get("people_count", 0) > 0:
                availability["available_data"].append(f"Люди: {graph['people_count']}")
            else:
                availability["missing_data"].append("Информация о людях")
                availability["confidence"] = "low"

        # Проверка на проекты
        if any(word in query_lower for word in ["проект", "project", "работа", "задача"]):
            if graph.get("projects_count", 0) > 0:
                availability["available_data"].append(f"Проекты: {graph['projects_count']}")
            if graph.get("task_statuses"):
                availability["available_data"].append(f"Задачи: {sum(graph['task_statuses'].values())}")

        # Проверка на встречи
        if any(word in query_lower for word in ["встреча", "meeting", "обсужд", "совещание"]):
            if graph.get("meetings_count", 0) > 0:
                availability["available_data"].append(f"Встречи: {graph['meetings_count']}")
                if graph.get("date_range"):
                    dr = graph["date_range"]
                    availability["available_data"].append(f"Период: {dr['earliest']} - {dr['latest']}")

        # Проверка на решения
        if any(word in query_lower for word in ["решени", "decision", "постанов", "вывод"]):
            if graph.get("decisions_count", 0) > 0:
                availability["available_data"].append(f"Решения: {graph['decisions_count']}")
            else:
                availability["missing_data"].append("Информация о решениях")

        # Проверка на KPI/метрики
        if any(word in query_lower for word in ["kpi", "метрик", "показател", "результат", "цифр"]):
            if graph.get("kpis_count", 0) > 0:
                availability["available_data"].append(f"KPI: {graph['kpis_count']}")
            else:
                availability["missing_data"].append("KPI и метрики")

        # Определяем можем ли ответить
        if not availability["available_data"]:
            availability["can_answer"] = False
            availability["confidence"] = "none"
            availability["suggestions"].append("Данные для ответа на этот запрос отсутствуют")
        elif availability["missing_data"]:
            availability["confidence"] = "medium"
            availability["suggestions"].append(
                f"Ответ может быть неполным. Отсутствует: {', '.join(availability['missing_data'])}"
            )

        return availability

    def get_profile_summary(self) -> str:
        """Получить текстовое описание профиля данных."""
        if not self._profile_cache:
            return "Профиль данных не загружен. Вызовите profile_available_data()."

        profile = self._profile_cache
        graph = profile.get("graph", {})
        quality = profile.get("data_quality", {})

        summary = f"""📊 Профиль данных:
- Узлов: {graph.get('total_nodes', 0)}
- Связей: {graph.get('total_edges', 0)}
- Проектов: {graph.get('projects_count', 0)}
- Людей: {graph.get('people_count', 0)}
- Встреч: {graph.get('meetings_count', 0)}
- Задач: {sum(graph.get('task_statuses', {}).values())}
- Решений: {graph.get('decisions_count', 0)}
- KPI: {graph.get('kpis_count', 0)}

📈 Качество данных:
- Полнота: {quality.get('completeness', 0)}%
- Связность: {quality.get('richness', 0)}%
- Поисковая готовность: {quality.get('searchability', 0)}%
- Общий балл: {quality.get('overall', 0)}%
"""

        if profile.get("recommendations"):
            summary += "\n⚠️ Рекомендации:\n"
            for rec in profile["recommendations"]:
                summary += f"  - {rec}\n"

        return summary



