# -*- coding: utf-8 -*-
"""
Numerical Data Search Agent - специализированный агент для поиска числовых данных.

Ищет и извлекает:
- KPI и метрики
- Финансовые данные
- Статистику проектов
- Временные показатели
- Количественные результаты
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class NumericalDataSearchAgent:
    """
    Специализированный агент для поиска числовых данных.

    Оптимизирован для запросов типа:
    - "Какой бюджет проекта X?"
    - "Сколько задач выполнено?"
    - "Какие KPI по проекту?"
    - "Статистика за месяц"
    """

    # Паттерны для извлечения чисел
    NUMBER_PATTERNS = [
        r'\d+[.,]\d+',  # Десятичные
        r'\d+%',  # Проценты
        r'\d+\s*(руб|₽|usd|\$|€|тыс|млн|млрд)',  # Валюты
        r'\d+',  # Целые числа
    ]

    # Ключевые слова для типов данных
    DATA_TYPE_KEYWORDS = {
        "financial": ["бюджет", "стоимость", "цена", "расход", "доход", "выручка", "прибыль", "затрат"],
        "time": ["срок", "дней", "недель", "месяц", "часов", "время", "длительность", "период"],
        "count": ["количество", "число", "сколько", "всего", "итого", "штук"],
        "percentage": ["процент", "%", "доля", "часть", "соотношение"],
        "kpi": ["kpi", "показател", "метрик", "результат", "эффективность"],
    }

    def __init__(self, graph_builder=None, vector_indexer=None, llm_client=None):
        """
        Args:
            graph_builder: GraphBuilder для доступа к графу
            vector_indexer: VectorIndexer для векторного поиска
            llm_client: LLM клиент для интерпретации
        """
        self.graph_builder = graph_builder
        self.vector_indexer = vector_indexer
        self.llm_client = llm_client

    async def search(
        self,
        query: str,
        project_name: Optional[str] = None,
        data_types: Optional[List[str]] = None,
        date_range: Optional[Tuple[str, str]] = None
    ) -> Dict[str, Any]:
        """
        Поиск числовых данных по запросу.

        Args:
            query: Текст запроса
            project_name: Фильтр по проекту
            data_types: Типы данных для поиска (financial, time, count, etc.)
            date_range: Временной диапазон (start, end)

        Returns:
            Найденные числовые данные с контекстом
        """
        logger.info(f"🔢 NumericalDataSearchAgent: поиск '{query[:50]}...'")

        # Определяем типы данных из запроса если не указаны
        if not data_types:
            data_types = self._detect_data_types(query)

        results = {
            "query": query,
            "data_types": data_types,
            "kpis": [],
            "metrics": [],
            "aggregations": {},
            "raw_numbers": [],
            "context": []
        }

        # 1. Поиск KPI в графе
        kpis = await self._search_kpis(query, project_name)
        results["kpis"] = kpis

        # 2. Поиск числовых данных в задачах
        task_metrics = await self._search_task_metrics(query, project_name)
        results["metrics"].extend(task_metrics)

        # 3. Поиск в решениях (часто содержат числа)
        decision_numbers = await self._search_decision_numbers(query)
        results["raw_numbers"].extend(decision_numbers)

        # 4. Агрегации по графу
        aggregations = await self._compute_aggregations(query, project_name)
        results["aggregations"] = aggregations

        # 5. Векторный поиск для контекста
        if self.vector_indexer:
            context = await self._search_vector_context(query)
            results["context"] = context

        # 6. ЕДИНЫЕ МЕТРИКИ + датасеты (metric_registry / таблицы, CRM):
        # реальные типизированные цифры с планом/фактом и сверкой — раньше
        # глубокий поиск доставал числа только regex'ом по графу и вектором
        # (цифра как строка). user_id — из tenant-контекста. Never-raise.
        try:
            from backend.core.observability.tenant_context import (
                get_current_tenant,
            )
            uid = get_current_tenant()
            if uid:
                from backend.core.ontology.numbers_context import numbers_block
                nb = numbers_block(uid, query)
                for n in nb.get("numbers") or []:
                    results["metrics"].append({
                        "name": n.get("metric"),
                        "value": n.get("value"),
                        "unit": n.get("unit") or "",
                        "source": n.get("source"),
                        "verified": bool(n.get("is_verified")),
                    })
                if nb.get("numbers"):
                    logger.info(f"   📈 Из онтологии данных: "
                                f"{len(nb['numbers'])} метрик")
        except Exception as e:
            logger.debug(f"numbers_block in numerical search skipped: {e}")

        # Подсчитываем найденное
        total_found = (
            len(results["kpis"]) +
            len(results["metrics"]) +
            len(results["raw_numbers"]) +
            len(results["aggregations"])
        )

        logger.info(f"   ✅ Найдено: {total_found} числовых данных")

        return results

    def _detect_data_types(self, query: str) -> List[str]:
        """Определить типы данных из запроса."""
        query_lower = query.lower()
        detected = []

        for data_type, keywords in self.DATA_TYPE_KEYWORDS.items():
            if any(kw in query_lower for kw in keywords):
                detected.append(data_type)

        return detected or ["general"]

    async def _search_kpis(self, query: str, project_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Поиск KPI в графе."""
        if not self.graph_builder or not self.graph_builder.nx_graph:
            return []

        graph = self.graph_builder.nx_graph
        nodes = [dict(graph.nodes[n]) | {"id": n} for n in graph.nodes()]

        kpis = []
        query_lower = query.lower()

        for node in nodes:
            if node.get("_label") != "KPI":
                continue

            name = node.get("name", "").lower()
            description = node.get("description", "").lower()

            # Фильтр по проекту если указан
            if project_name:
                # Проверяем связи с проектом
                node_id = node.get("id")
                neighbors = list(graph.neighbors(node_id)) if node_id in graph else []
                project_match = any(
                    project_name.lower() in str(graph.nodes.get(n, {}).get("name", "")).lower()
                    for n in neighbors
                )
                if not project_match:
                    continue

            # Релевантность по запросу
            relevance = 0
            for word in query_lower.split():
                if len(word) > 2:
                    if word in name:
                        relevance += 2
                    if word in description:
                        relevance += 1

            if relevance > 0 or not query.strip():
                kpis.append({
                    "id": node.get("id"),
                    "name": node.get("name"),
                    "value": node.get("value"),
                    "unit": node.get("unit", ""),
                    "description": node.get("description", ""),
                    "relevance": relevance
                })

        # Сортируем по релевантности
        kpis.sort(key=lambda x: -x.get("relevance", 0))

        return kpis[:20]

    async def _search_task_metrics(self, query: str, project_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Поиск метрик из задач."""
        if not self.graph_builder or not self.graph_builder.nx_graph:
            return []

        graph = self.graph_builder.nx_graph
        nodes = [dict(graph.nodes[n]) | {"id": n} for n in graph.nodes()]

        tasks = [n for n in nodes if n.get("_label") == "Task"]

        # Статистика по статусам
        status_counts = {}
        for task in tasks:
            status = task.get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1

        metrics = []

        # Добавляем метрики
        if status_counts:
            total = sum(status_counts.values())
            for status, count in status_counts.items():
                metrics.append({
                    "type": "task_status",
                    "name": f"Задачи: {status}",
                    "value": count,
                    "unit": "шт",
                    "percentage": round(count / total * 100, 1) if total > 0 else 0
                })

            # Общее количество
            metrics.append({
                "type": "task_total",
                "name": "Всего задач",
                "value": total,
                "unit": "шт"
            })

        return metrics

    async def _search_decision_numbers(self, query: str) -> List[Dict[str, Any]]:
        """Извлечь числа из решений."""
        if not self.graph_builder or not self.graph_builder.nx_graph:
            return []

        graph = self.graph_builder.nx_graph
        nodes = [dict(graph.nodes[n]) | {"id": n} for n in graph.nodes()]

        decisions = [n for n in nodes if n.get("_label") == "Decision"]

        numbers = []

        for decision in decisions:
            text = f"{decision.get('summary', '')} {decision.get('description', '')}"

            # Извлекаем числа
            for pattern in self.NUMBER_PATTERNS:
                matches = re.findall(pattern, text, re.IGNORECASE)
                for match in matches:
                    numbers.append({
                        "value": match,
                        "source": "decision",
                        "context": decision.get("summary", "")[:100],
                        "decision_id": decision.get("id")
                    })

        return numbers[:30]

    async def _compute_aggregations(self, query: str, project_name: Optional[str] = None) -> Dict[str, Any]:
        """Вычислить агрегации по графу."""
        if not self.graph_builder or not self.graph_builder.nx_graph:
            return {}

        graph = self.graph_builder.nx_graph
        nodes = [dict(graph.nodes[n]) | {"id": n} for n in graph.nodes()]

        aggregations = {
            "total_nodes": graph.number_of_nodes(),
            "total_edges": graph.number_of_edges(),
        }

        # Подсчёт по типам
        from collections import Counter
        type_counts = Counter(n.get("_label", "Unknown") for n in nodes)
        aggregations["by_type"] = dict(type_counts)

        # Встречи по месяцам
        meetings = [n for n in nodes if n.get("_label") == "Meeting"]
        meetings_by_month = {}
        for m in meetings:
            date_str = m.get("date") or m.get("created_at", "")
            if date_str:
                try:
                    month = str(date_str)[:7]  # YYYY-MM
                    meetings_by_month[month] = meetings_by_month.get(month, 0) + 1
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)

        if meetings_by_month:
            aggregations["meetings_by_month"] = dict(sorted(meetings_by_month.items()))

        return aggregations

    async def _search_vector_context(self, query: str) -> List[Dict[str, Any]]:
        """Поиск контекста через векторный индекс."""
        if not self.vector_indexer:
            return []

        try:
            # Добавляем числовые ключевые слова к запросу
            enhanced_query = f"{query} числа метрики KPI показатели результаты"

            results = await self.vector_indexer.search(
                query=enhanced_query,
                limit=10
            )

            context = []
            for r in results:
                # Проверяем наличие чисел в результате
                text = str(r.get("metadata", {}))
                has_numbers = any(re.search(p, text) for p in self.NUMBER_PATTERNS)

                if has_numbers:
                    context.append({
                        "id": r.get("id"),
                        "label": r.get("metadata", {}).get("label"),
                        "score": r.get("score"),
                        "metadata": r.get("metadata", {})
                    })

            return context

        except Exception as e:
            logger.warning(f"⚠️ Ошибка векторного поиска: {e}")
            return []

    async def search_project_financial_data(
        self,
        project_name: str,
        data_types: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Поиск финансовых данных проекта.

        Args:
            project_name: Название проекта
            data_types: Типы данных (budget, cost, revenue, etc.)

        Returns:
            Финансовые данные проекта
        """
        query = f"финансовые данные бюджет стоимость проект {project_name}"

        results = await self.search(
            query=query,
            project_name=project_name,
            data_types=data_types or ["financial"]
        )

        # Структурируем финансовые данные
        financial = {
            "project": project_name,
            "budget": None,
            "spent": None,
            "remaining": None,
            "kpis": results.get("kpis", []),
            "raw_data": results.get("raw_numbers", [])
        }

        # Пробуем извлечь конкретные значения
        for kpi in results.get("kpis", []):
            name_lower = kpi.get("name", "").lower()
            if "бюджет" in name_lower:
                financial["budget"] = kpi.get("value")
            elif "расход" in name_lower or "затрат" in name_lower:
                financial["spent"] = kpi.get("value")

        return financial

    def format_results(self, results: Dict[str, Any]) -> str:
        """Форматировать результаты для отображения."""
        lines = ["📊 **Числовые данные:**\n"]

        # KPI
        if results.get("kpis"):
            lines.append("**KPI:**")
            for kpi in results["kpis"][:5]:
                value = kpi.get("value", "?")
                unit = kpi.get("unit", "")
                lines.append(f"  - {kpi.get('name')}: {value} {unit}")
            lines.append("")

        # Метрики
        if results.get("metrics"):
            lines.append("**Метрики:**")
            for metric in results["metrics"][:5]:
                value = metric.get("value", "?")
                unit = metric.get("unit", "")
                pct = metric.get("percentage")
                pct_str = f" ({pct}%)" if pct else ""
                lines.append(f"  - {metric.get('name')}: {value} {unit}{pct_str}")
            lines.append("")

        # Агрегации
        if results.get("aggregations"):
            agg = results["aggregations"]
            lines.append("**Статистика:**")
            if "by_type" in agg:
                for type_name, count in sorted(agg["by_type"].items(), key=lambda x: -x[1])[:5]:
                    lines.append(f"  - {type_name}: {count}")

        return "\n".join(lines)



