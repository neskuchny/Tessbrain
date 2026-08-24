# -*- coding: utf-8 -*-
"""
Graph Traverser - обход графа и выполнение запросов.

Выполняет шаги плана рассуждения:
- Запросы к графу (NetworkX / Neo4j)
- Векторный поиск
- Агрегация данных
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class GraphTraverser:
    """
    Обходчик графа знаний.

    Выполняет запросы к графу и векторной БД.

    Пример:
    ```python
    traverser = GraphTraverser(graph_builder, vector_indexer)

    # Найти узел по имени
    nodes = await traverser.find_nodes("Person", {"name": "Иван Петров"})

    # Получить связи узла
    relationships = await traverser.get_relationships(node_id)

    # Multi-hop traversal
    path = await traverser.traverse_path(start_node, ["ASSIGNED_TO", "CREATED_FROM"])
    ```
    """

    def __init__(self, graph_builder=None, vector_indexer=None):
        """
        Args:
            graph_builder: GraphBuilder instance
            vector_indexer: VectorIndexer instance
        """
        self.graph = graph_builder
        self.vectors = vector_indexer

    async def find_nodes(
        self,
        label: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        allowed_groups: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Найти узлы по метке и фильтрам.

        Args:
            label: Метка узла (Person, Task, Decision, etc.)
            filters: Фильтры по свойствам
            limit: Максимум результатов
            allowed_groups: Разрешенные группы доступа (RBAC)

        Returns:
            Список узлов
        """
        if not self.graph or not self.graph.connected:
            logger.warning("Graph not connected")
            return []

        # Используем метод графа
        nodes = await self.graph.find_nodes_by_label(label, limit=limit * 2)  # Берем больше для фильтрации

        # Фильтрация по access_group (RBAC)
        if allowed_groups:
            nodes = [n for n in nodes if n.get("access_group", "public") in allowed_groups or n.get("access_group") == "public"]

        # Применяем фильтры
        if filters:
            filtered = []
            for node in nodes:
                match = True
                for key, value in filters.items():
                    node_value = node.get(key, "")

                    # Поддержка частичного совпадения для строк
                    if isinstance(value, str) and isinstance(node_value, str):
                        if value.lower() not in node_value.lower():
                            match = False
                            break
                    elif node_value != value:
                        match = False
                        break

                if match:
                    filtered.append(node)

            return filtered[:limit]

        return nodes[:limit]

    async def find_node_by_name(
        self,
        name: str,
        label: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Найти узел по имени (fuzzy matching).

        Args:
            name: Имя для поиска
            label: Опциональная метка узла

        Returns:
            Узел или None
        """
        if not self.graph or not self.graph.connected:
            return None

        name_lower = name.lower().strip()

        # Если указана метка — ищем только в ней
        if label:
            nodes = await self.find_nodes(label, {"name": name}, limit=10)
            # Ищем лучшее совпадение
            for node in nodes:
                node_name = node.get("name", "").lower()
                if name_lower == node_name or name_lower in node_name or node_name in name_lower:
                    return node
            return nodes[0] if nodes else None

        # Иначе ищем по всем меткам
        for node_label in ["Person", "Entity", "Task", "Decision", "Meeting", "KPI"]:
            nodes = await self.find_nodes(node_label, {"name": name}, limit=10)
            for node in nodes:
                node_name = node.get("name", "").lower()
                if name_lower == node_name or name_lower in node_name or node_name in name_lower:
                    return node

        return None

    async def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        """
        Получить узел по ID.

        Args:
            node_id: ID узла

        Returns:
            Узел или None
        """
        if not self.graph:
            return None

        return await self.graph.get_node_by_id(node_id)

    async def get_relationships(
        self,
        node_id: str,
        direction: str = "both",
        rel_types: Optional[List[str]] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Получить связи узла.

        Args:
            node_id: ID узла
            direction: "incoming", "outgoing", или "both"
            rel_types: Фильтр по типам связей

        Returns:
            {"incoming": [...], "outgoing": [...]}
        """
        if not self.graph:
            return {"incoming": [], "outgoing": []}

        rels = await self.graph.get_node_relationships(node_id)

        # Фильтруем по типу
        if rel_types:
            rels["incoming"] = [
                r for r in rels["incoming"]
                if r.get("type", r.get("_type", "")) in rel_types
            ]
            rels["outgoing"] = [
                r for r in rels["outgoing"]
                if r.get("type", r.get("_type", "")) in rel_types
            ]

        # Фильтруем по направлению
        if direction == "incoming":
            rels["outgoing"] = []
        elif direction == "outgoing":
            rels["incoming"] = []

        return rels

    async def traverse_path(
        self,
        start_node_id: str,
        relationship_types: List[str],
        max_depth: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Обход графа по цепочке связей.

        Args:
            start_node_id: Начальный узел
            relationship_types: Типы связей для обхода
            max_depth: Максимальная глубина

        Returns:
            Путь в виде списка узлов
        """
        if not self.graph:
            return []

        path = []
        visited = set()
        current_nodes = [start_node_id]

        for depth in range(max_depth):
            if not current_nodes:
                break

            next_nodes = []

            for node_id in current_nodes:
                if node_id in visited:
                    continue

                visited.add(node_id)

                # Получаем узел
                node = await self.get_node(node_id)
                if node:
                    node["_depth"] = depth
                    path.append(node)

                # Получаем связи
                rels = await self.get_relationships(
                    node_id,
                    direction="outgoing",
                    rel_types=relationship_types
                )

                # Добавляем целевые узлы
                for rel in rels.get("outgoing", []):
                    target = rel.get("target")
                    if target and target not in visited:
                        next_nodes.append(target)

            current_nodes = next_nodes

        return path

    async def get_neighbors(
        self,
        node_id: str,
        hops: int = 1
    ) -> List[Dict[str, Any]]:
        """
        Получить соседей узла на заданном расстоянии.

        Args:
            node_id: ID узла
            hops: Количество переходов

        Returns:
            Список соседних узлов
        """
        if not self.graph:
            return []

        neighbors = []
        visited = {node_id}
        current_level = [node_id]

        for _ in range(hops):
            next_level = []

            for nid in current_level:
                rels = await self.get_relationships(nid)

                # Собираем все связанные узлы
                for rel in rels.get("outgoing", []):
                    target = rel.get("target")
                    if target and target not in visited:
                        visited.add(target)
                        next_level.append(target)

                for rel in rels.get("incoming", []):
                    source = rel.get("source")
                    if source and source not in visited:
                        visited.add(source)
                        next_level.append(source)

            # Получаем данные узлов
            for nid in next_level:
                node = await self.get_node(nid)
                if node:
                    neighbors.append(node)

            current_level = next_level

        return neighbors

    async def vector_search(
        self,
        query: str,
        collections: Optional[List[str]] = None,
        limit: int = 10,
        score_threshold: float = 0.5,
        filters: Optional[Dict[str, Any]] = None,
        allowed_groups: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Семантический поиск по векторной БД.

        Args:
            query: Текст запроса
            collections: Коллекции для поиска
            limit: Максимум результатов
            score_threshold: Минимальный порог
            filters: Фильтры по payload
            allowed_groups: Разрешенные группы доступа (RBAC)

        Returns:
            Список результатов
        """
        if not self.vectors or not self.vectors.connected:
            logger.warning("Vector indexer not connected")
            return []

        # Объединяем фильтры с access_group
        search_filters = filters.copy() if filters else {}

        # Для RBAC: ищем без фильтра, потом фильтруем (т.к. нужен OR для нескольких групп)
        results = await self.vectors.search(
            query=query,
            collections=collections,
            limit=limit * 2 if allowed_groups else limit,  # Берем больше для фильтрации
            score_threshold=score_threshold,
            filter_conditions=search_filters
        )

        # Фильтрация по access_group (RBAC)
        if allowed_groups:
            results = [r for r in results if r.get("access_group", "public") in allowed_groups or r.get("access_group") == "public"]
            results = results[:limit]

        return results

    async def hybrid_search(
        self,
        query: str,
        node_type: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Гибридный поиск: векторный + граф.

        Args:
            query: Текст запроса
            node_type: Тип узла для поиска в графе
            limit: Максимум результатов

        Returns:
            Объединённые результаты
        """
        results = []
        seen_ids = set()

        # 1. Векторный поиск
        vector_results = await self.vector_search(query, limit=limit)

        for vr in vector_results:
            result_id = vr.get("id", "")
            if result_id not in seen_ids:
                seen_ids.add(result_id)
                vr["_source"] = "vector"
                results.append(vr)

        # 2. Поиск по графу (по ключевым словам)
        keywords = query.lower().split()

        for keyword in keywords[:3]:  # Берём первые 3 слова
            if len(keyword) < 3:
                continue

            graph_nodes = await self.find_nodes(node_type, {"name": keyword}, limit=5)

            for node in graph_nodes:
                node_id = node.get("id", "")
                if node_id not in seen_ids:
                    seen_ids.add(node_id)
                    node["_source"] = "graph"
                    node["score"] = 0.7  # Базовый score для графа
                    results.append(node)

        # Сортируем по score
        results.sort(key=lambda x: x.get("score", 0), reverse=True)

        return results[:limit]

    async def aggregate_data(
        self,
        data: List[Dict[str, Any]],
        aggregate_type: str
    ) -> Dict[str, Any]:
        """
        Агрегировать данные.

        Args:
            data: Список данных для агрегации
            aggregate_type: Тип агрегации

        Returns:
            Агрегированные данные
        """
        if aggregate_type == "count":
            return {"count": len(data)}

        elif aggregate_type == "group_by_type":
            groups = {}
            for item in data:
                item_type = item.get("type", item.get("_label", "unknown"))
                if item_type not in groups:
                    groups[item_type] = []
                groups[item_type].append(item)
            return {"groups": groups, "group_counts": {k: len(v) for k, v in groups.items()}}

        elif aggregate_type == "project_summary":
            return self._aggregate_project_summary(data)

        elif aggregate_type == "person_summary":
            return self._aggregate_person_summary(data)

        elif aggregate_type == "anomaly_report":
            return self._aggregate_anomaly_report(data)

        elif aggregate_type == "overall_summary":
            return self._aggregate_overall_summary(data)

        else:
            return {"data": data, "count": len(data)}

    def _aggregate_project_summary(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Агрегация для проекта"""
        summary = {
            "tasks": [],
            "decisions": [],
            "participants": [],
            "meetings": [],
            "metrics": {}
        }

        for item in data:
            item_type = item.get("type", item.get("_label", ""))

            if item_type == "task" or item_type == "Task":
                summary["tasks"].append({
                    "title": item.get("title", ""),
                    "status": item.get("status", ""),
                    "priority": item.get("priority", ""),
                    "assignee": item.get("assignee", "")
                })
            elif item_type == "decision" or item_type == "Decision":
                summary["decisions"].append({
                    "summary": item.get("summary", item.get("decision_summary", "")),
                    "category": item.get("category", ""),
                    "urgency": item.get("urgency", "")
                })
            elif item_type == "participant" or item_type == "Person":
                summary["participants"].append({
                    "name": item.get("name", ""),
                    "role": item.get("role", "")
                })
            elif item_type == "meeting" or item_type == "Meeting":
                summary["meetings"].append({
                    "title": item.get("title", ""),
                    "date": item.get("date", "")
                })

        # Метрики
        summary["metrics"] = {
            "total_tasks": len(summary["tasks"]),
            "total_decisions": len(summary["decisions"]),
            "total_participants": len(summary["participants"]),
            "total_meetings": len(summary["meetings"]),
            "high_priority_tasks": len([t for t in summary["tasks"] if t.get("priority") == "high"]),
            "new_tasks": len([t for t in summary["tasks"] if t.get("status") == "new"])
        }

        return summary

    def _aggregate_person_summary(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Агрегация для человека"""
        summary = {
            "assigned_tasks": [],
            "made_decisions": [],
            "participated_meetings": [],
            "metrics": {}
        }

        for item in data:
            item_type = item.get("type", item.get("_label", ""))

            if item_type == "task" or item_type == "Task":
                summary["assigned_tasks"].append({
                    "title": item.get("title", ""),
                    "status": item.get("status", ""),
                    "priority": item.get("priority", "")
                })
            elif item_type == "decision" or item_type == "Decision":
                summary["made_decisions"].append({
                    "summary": item.get("summary", item.get("decision_summary", "")),
                    "category": item.get("category", "")
                })
            elif item_type == "meeting" or item_type == "Meeting":
                summary["participated_meetings"].append({
                    "title": item.get("title", ""),
                    "date": item.get("date", "")
                })

        summary["metrics"] = {
            "total_tasks": len(summary["assigned_tasks"]),
            "total_decisions": len(summary["made_decisions"]),
            "total_meetings": len(summary["participated_meetings"])
        }

        return summary

    def _aggregate_anomaly_report(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Агрегация аномалий"""
        anomalies = {
            "overdue_tasks": [],
            "stuck_tasks": [],
            "unassigned_tasks": [],
            "pending_decisions": [],
            "risks": []
        }

        for item in data:
            item_type = item.get("type", item.get("_label", ""))

            if item_type == "task" or item_type == "Task":
                # Проверяем аномалии задач
                if item.get("status") == "overdue":
                    anomalies["overdue_tasks"].append(item)
                elif item.get("status") == "stuck":
                    anomalies["stuck_tasks"].append(item)
                elif not item.get("assignee"):
                    anomalies["unassigned_tasks"].append(item)

            elif item_type == "decision" or item_type == "Decision":
                if item.get("status") == "pending":
                    anomalies["pending_decisions"].append(item)

        # Определяем риски
        if len(anomalies["overdue_tasks"]) > 3:
            anomalies["risks"].append({
                "type": "many_overdue",
                "severity": "high",
                "message": f"Много просроченных задач: {len(anomalies['overdue_tasks'])}"
            })

        if len(anomalies["unassigned_tasks"]) > 2:
            anomalies["risks"].append({
                "type": "unassigned_tasks",
                "severity": "medium",
                "message": f"Есть неназначенные задачи: {len(anomalies['unassigned_tasks'])}"
            })

        return anomalies

    def _aggregate_overall_summary(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Общая агрегация"""
        summary = {
            "statistics": {},
            "recent_meetings": [],
            "active_tasks": [],
            "key_decisions": []
        }

        for item in data:
            item_type = item.get("type", item.get("_label", ""))

            if item_type == "Meeting":
                summary["recent_meetings"].append({
                    "title": item.get("title", ""),
                    "date": item.get("date", "")
                })
            elif item_type == "Task":
                if item.get("status") in ("new", "in_progress"):
                    summary["active_tasks"].append({
                        "title": item.get("title", ""),
                        "priority": item.get("priority", "")
                    })
            elif item_type == "Decision":
                summary["key_decisions"].append({
                    "summary": item.get("summary", item.get("decision_summary", "")),
                    "urgency": item.get("urgency", "")
                })

        return summary

    def get_graph_info(self) -> Dict[str, Any]:
        """Получить информацию о графе"""
        if self.graph:
            return self.graph.get_graph_info()
        return {"error": "Graph not connected"}

    async def get_all_nodes_with_relationships(
        self,
        label: str,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Получить все узлы типа с их связями.

        Args:
            label: Метка узла
            limit: Максимум результатов

        Returns:
            Список узлов с их связями
        """
        if not self.graph or not self.graph.connected:
            return []

        nodes = await self.find_nodes(label, limit=limit)

        # Обогащаем каждый узел связями
        enriched = []
        for node in nodes:
            node_id = node.get("id", "")
            if node_id:
                rels = await self.get_relationships(node_id)
                node["_incoming_count"] = len(rels.get("incoming", []))
                node["_outgoing_count"] = len(rels.get("outgoing", []))
                node["_relationships"] = rels
            enriched.append(node)

        return enriched

    async def find_person_with_context(
        self,
        name: str
    ) -> Dict[str, Any]:
        """
        Найти человека и получить весь его контекст.

        Args:
            name: Имя человека

        Returns:
            Данные о человеке с задачами, решениями, встречами
        """
        result = {
            "person": None,
            "tasks": [],
            "decisions": [],
            "meetings": [],
            "entities": []
        }

        # Ищем человека
        person = await self.find_node_by_name(name, "Person")
        if not person:
            return result

        result["person"] = person
        person_id = person.get("id", "")

        # Получаем связи
        rels = await self.get_relationships(person_id)

        # Обрабатываем исходящие связи (Person -> X)
        for rel in rels.get("outgoing", []):
            rel_type = rel.get("type", "")
            target_id = rel.get("target", "")

            if target_id:
                target_node = await self.get_node(target_id)
                if target_node:
                    target_label = target_node.get("_label", "")

                    if target_label == "Task" or rel_type == "ASSIGNED_TO":
                        result["tasks"].append(target_node)
                    elif target_label == "Decision" or rel_type == "MADE_DECISION":
                        result["decisions"].append(target_node)
                    elif target_label == "Meeting" or rel_type == "PARTICIPATED_IN":
                        result["meetings"].append(target_node)
                    elif target_label == "Entity":
                        result["entities"].append(target_node)

        # Обрабатываем входящие связи (X -> Person)
        for rel in rels.get("incoming", []):
            rel_type = rel.get("type", "")
            source_id = rel.get("source", "")

            if source_id:
                source_node = await self.get_node(source_id)
                if source_node:
                    source_label = source_node.get("_label", "")

                    if source_label == "Task":
                        if source_node not in result["tasks"]:
                            result["tasks"].append(source_node)
                    elif source_label == "Meeting":
                        if source_node not in result["meetings"]:
                            result["meetings"].append(source_node)

        return result

    async def find_meeting_with_context(
        self,
        meeting_id: Optional[str] = None,
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Найти встречу и получить весь её контекст.

        Args:
            meeting_id: ID встречи
            title: Название встречи

        Returns:
            Данные о встрече с участниками, задачами, решениями
        """
        result = {
            "meeting": None,
            "participants": [],
            "tasks": [],
            "decisions": [],
            "entities": []
        }

        # Ищем встречу
        meeting = None
        if meeting_id:
            meeting = await self.get_node(meeting_id)
        elif title:
            meeting = await self.find_node_by_name(title, "Meeting")

        if not meeting:
            return result

        result["meeting"] = meeting
        meeting_node_id = meeting.get("id", "")

        # Получаем связи
        rels = await self.get_relationships(meeting_node_id)

        # Обрабатываем входящие связи (X -> Meeting)
        for rel in rels.get("incoming", []):
            rel_type = rel.get("type", "")
            source_id = rel.get("source", "")

            if source_id:
                source_node = await self.get_node(source_id)
                if source_node:
                    source_label = source_node.get("_label", "")

                    if source_label == "Person" or rel_type == "PARTICIPATED_IN":
                        result["participants"].append(source_node)
                    elif source_label == "Task" or rel_type == "CREATED_AT":
                        result["tasks"].append(source_node)
                    elif source_label == "Decision" or rel_type == "MADE_AT":
                        result["decisions"].append(source_node)

        # Обрабатываем исходящие связи (Meeting -> X)
        for rel in rels.get("outgoing", []):
            rel_type = rel.get("type", "")
            target_id = rel.get("target", "")

            if target_id:
                target_node = await self.get_node(target_id)
                if target_node:
                    target_label = target_node.get("_label", "")

                    if target_label == "Entity" or rel_type == "MENTIONS":
                        result["entities"].append(target_node)

        return result

    async def get_all_tasks_summary(self, limit: int = 100) -> Dict[str, Any]:
        """
        Получить сводку по всем задачам.

        Returns:
            Сводка по задачам с группировкой по статусу
        """
        tasks = await self.find_nodes("Task", limit=limit)

        summary = {
            "total": len(tasks),
            "by_status": {},
            "tasks": []
        }

        for task in tasks:
            status = task.get("status", "unknown")
            if status not in summary["by_status"]:
                summary["by_status"][status] = 0
            summary["by_status"][status] += 1

            summary["tasks"].append({
                "id": task.get("id", ""),
                "title": task.get("title", ""),
                "status": status,
                "assignee": task.get("assignee", ""),
                "deadline": task.get("deadline", "")
            })

        return summary

    async def get_all_people_summary(self, limit: int = 100) -> Dict[str, Any]:
        """
        Получить сводку по всем людям.

        Returns:
            Список людей с их активностью
        """
        people = await self.find_nodes("Person", limit=limit)

        summary = {
            "total": len(people),
            "people": []
        }

        for person in people:
            person_id = person.get("id", "")
            rels = await self.get_relationships(person_id)

            # Считаем связи
            meetings_count = sum(1 for r in rels.get("outgoing", []) if r.get("type") == "PARTICIPATED_IN")
            tasks_count = sum(1 for r in rels.get("outgoing", []) if r.get("type") == "ASSIGNED_TO")

            summary["people"].append({
                "id": person_id,
                "name": person.get("name", ""),
                "role": person.get("role", ""),
                "meetings_count": meetings_count,
                "tasks_count": tasks_count
            })

        return summary

