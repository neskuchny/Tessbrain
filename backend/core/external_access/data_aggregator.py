# -*- coding: utf-8 -*-
"""
Data Aggregator - Агрегация данных из различных источников.

Собирает данные из графа знаний, снапшотов и встреч для формирования ответа.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class AggregatedData:
    """Агрегированные данные."""
    sources: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    total_items: int = 0
    query_time_ms: int = 0


class DataAggregator:
    """
    Агрегатор данных для внешнего доступа.

    Источники:
    1. Граф знаний (сущности, связи)
    2. Снапшоты (компания, сотрудники, проекты)
    3. Встречи (открытые для подрядчика)
    4. Документы (публичные)
    """

    # Маппинг категорий к источникам
    CATEGORY_SOURCES = {
        "company_overview": ["company_snapshot", "graph:Company"],
        "product_description": ["graph:Product", "meetings:product"],
        "team_overview": ["graph:Person", "graph:Department"],
        "project_progress": ["project_snapshots", "graph:Project"],
        "risks_overview": ["graph:Risk", "meetings:risk"],
        "achievements": ["graph:Achievement", "company_snapshot"],
        "decisions": ["graph:Decision", "meetings:decision"],
        "metrics": ["company_snapshot", "graph:Metric"],
        "processes": ["graph:Process", "meetings:process"],
        "regulations": ["graph:Regulation"],
    }

    def __init__(self, graph_builder=None, snapshot_generator=None):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            snapshot_generator: EnhancedSnapshotGenerator для снапшотов
        """
        self.graph = graph_builder
        self.snapshot_gen = snapshot_generator

    async def aggregate(
        self,
        query: str,
        categories: Set[str],
        meeting_ids: Optional[List[str]] = None,
        user_id: Optional[str] = None
    ) -> AggregatedData:
        """
        Агрегировать данные по запросу.

        Args:
            query: Поисковый запрос
            categories: Запрошенные категории данных
            meeting_ids: ID встреч, открытых для подрядчика
            user_id: ID пользователя (владельца данных)

        Returns:
            Агрегированные данные
        """
        import time
        start_time = time.time()

        result = AggregatedData()

        for category in categories:
            try:
                category_data = await self._get_category_data(
                    category=category,
                    query=query,
                    meeting_ids=meeting_ids,
                    user_id=user_id
                )

                if category_data:
                    result.data[category] = category_data
                    result.total_items += self._count_items(category_data)

            except Exception as e:
                logger.error(f"Error aggregating category {category}: {e}")

        result.query_time_ms = int((time.time() - start_time) * 1000)
        result.sources = list(set(result.sources))

        return result

    async def _get_category_data(
        self,
        category: str,
        query: str,
        meeting_ids: Optional[List[str]],
        user_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Получить данные для категории."""

        if category == "company_overview":
            return await self._get_company_overview(user_id)

        elif category == "product_description":
            return await self._get_product_data(query, user_id)

        elif category == "team_overview":
            return await self._get_team_overview(user_id)

        elif category == "project_progress":
            return await self._get_project_data(user_id)

        elif category == "risks_overview":
            return await self._get_risks_data(user_id)

        elif category == "achievements":
            return await self._get_achievements(user_id)

        elif category == "decisions":
            return await self._get_decisions(meeting_ids, user_id)

        elif category == "metrics":
            return await self._get_metrics(user_id)

        else:
            # Общий поиск по графу
            return await self._search_graph(query, category, user_id)

    async def _get_company_overview(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить обзор компании."""
        if not self.snapshot_gen:
            return None

        try:
            snapshot = await self.snapshot_gen.get_company_snapshot()
            if snapshot:
                return {
                    "name": snapshot.name,
                    "mission": snapshot.mission,
                    "description": snapshot.description,
                    "industry": snapshot.industry,
                    "size": snapshot.size,
                    "strengths": snapshot.strengths[:5] if snapshot.strengths else [],
                    "technologies": snapshot.technologies[:10] if snapshot.technologies else [],
                }
        except Exception as e:
            logger.error(f"Error getting company overview: {e}")

        return None

    async def _get_product_data(self, query: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить данные о продуктах."""
        if not self.graph or not self.graph.use_networkx:
            return None

        products = []

        try:
            for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
                if attrs.get("_label") == "Product":
                    products.append({
                        "name": attrs.get("name", ""),
                        "description": attrs.get("description", ""),
                        "status": attrs.get("status", ""),
                        "features": attrs.get("features", []),
                    })

            return {"products": products[:10]}

        except Exception as e:
            logger.error(f"Error getting product data: {e}")

        return None

    async def _get_team_overview(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить обзор команды."""
        if not self.graph or not self.graph.use_networkx:
            return None

        try:
            departments = []
            people_count = 0
            roles = {}

            for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
                label = attrs.get("_label", "")

                if label == "Department":
                    departments.append({
                        "name": attrs.get("name", ""),
                        "description": attrs.get("description", ""),
                    })

                elif label == "Person":
                    people_count += 1
                    role = attrs.get("role", "Unknown")
                    roles[role] = roles.get(role, 0) + 1

            return {
                "total_people": people_count,
                "departments": departments[:10],
                "roles_distribution": dict(sorted(roles.items(), key=lambda x: -x[1])[:10])
            }

        except Exception as e:
            logger.error(f"Error getting team overview: {e}")

        return None

    async def _get_project_data(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить данные о проектах."""
        if not self.graph or not self.graph.use_networkx:
            return None

        try:
            projects = []

            for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
                if attrs.get("_label") == "Project":
                    projects.append({
                        "name": attrs.get("name", ""),
                        "description": attrs.get("description", ""),
                        "status": attrs.get("status", ""),
                        "progress": attrs.get("progress", 0),
                    })

            # Сортируем по прогрессу
            projects.sort(key=lambda p: p.get("progress", 0), reverse=True)

            return {"projects": projects[:20]}

        except Exception as e:
            logger.error(f"Error getting project data: {e}")

        return None

    async def _get_risks_data(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить данные о рисках."""
        if not self.graph or not self.graph.use_networkx:
            return None

        try:
            risks = []

            for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
                if attrs.get("_label") == "Risk":
                    risks.append({
                        "description": attrs.get("description", ""),
                        "severity": attrs.get("severity", "medium"),
                        "status": attrs.get("status", "open"),
                        "mitigation": attrs.get("mitigation", ""),
                    })

            # Сортируем по severity
            severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            risks.sort(key=lambda r: severity_order.get(r.get("severity", "medium"), 2))

            return {"risks": risks[:10]}

        except Exception as e:
            logger.error(f"Error getting risks data: {e}")

        return None

    async def _get_achievements(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить достижения."""
        if not self.graph or not self.graph.use_networkx:
            return None

        try:
            achievements = []

            for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
                if attrs.get("_label") == "Achievement":
                    achievements.append({
                        "title": attrs.get("title", attrs.get("name", "")),
                        "description": attrs.get("description", ""),
                        "date": attrs.get("date", ""),
                        "impact": attrs.get("impact", ""),
                    })

            return {"achievements": achievements[:10]}

        except Exception as e:
            logger.error(f"Error getting achievements: {e}")

        return None

    async def _get_decisions(
        self,
        meeting_ids: Optional[List[str]],
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Получить решения."""
        if not self.graph or not self.graph.use_networkx:
            return None

        try:
            decisions = []

            for _node_id, attrs in self.graph.nx_graph.nodes(data=True):
                if attrs.get("_label") == "Decision":
                    # Фильтруем по meeting_ids если указаны
                    decision_meeting = attrs.get("meeting_id", "")
                    if meeting_ids and decision_meeting not in meeting_ids:
                        continue

                    decisions.append({
                        "text": attrs.get("text", attrs.get("description", "")),
                        "date": attrs.get("date", ""),
                        "context": attrs.get("context", ""),
                    })

            return {"decisions": decisions[:20]}

        except Exception as e:
            logger.error(f"Error getting decisions: {e}")

        return None

    async def _get_metrics(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Получить метрики."""
        # Метрики обычно из снапшота компании
        if not self.snapshot_gen:
            return None

        try:
            snapshot = await self.snapshot_gen.get_company_snapshot()
            if snapshot and snapshot.current_state:
                return {
                    "status": snapshot.current_state.get("status", ""),
                    "key_metrics": snapshot.current_state.get("key_metrics", {}),
                }
        except Exception as e:
            logger.error(f"Error getting metrics: {e}")

        return None

    async def _search_graph(
        self,
        query: str,
        category: str,
        user_id: str
    ) -> Optional[Dict[str, Any]]:
        """Общий поиск по графу."""
        if not self.graph or not self.graph.use_networkx:
            return None

        try:
            results = []
            query_lower = query.lower()

            for node_id, attrs in self.graph.nx_graph.nodes(data=True):
                # Простой текстовый поиск
                name = attrs.get("name", "").lower()
                description = attrs.get("description", "").lower()

                if query_lower in name or query_lower in description:
                    results.append({
                        "id": node_id,
                        "type": attrs.get("_label", "Unknown"),
                        "name": attrs.get("name", ""),
                        "description": attrs.get("description", "")[:200],
                    })

            return {"search_results": results[:20]} if results else None

        except Exception as e:
            logger.error(f"Error searching graph: {e}")

        return None

    def _count_items(self, data: Any) -> int:
        """Подсчёт элементов в данных."""
        if isinstance(data, dict):
            return sum(self._count_items(v) for v in data.values())
        elif isinstance(data, list):
            return len(data)
        return 1


# Singleton
_data_aggregator: Optional[DataAggregator] = None


def get_data_aggregator(graph_builder=None, snapshot_generator=None) -> DataAggregator:
    """Получить экземпляр DataAggregator."""
    global _data_aggregator
    if _data_aggregator is None:
        _data_aggregator = DataAggregator(graph_builder, snapshot_generator)
    return _data_aggregator


