# -*- coding: utf-8 -*-
"""
🧠 Deep Analysis Agent - Агент глубокого анализа.

Выполняет трёхэтапный анализ:
1. Предварительный анализ и планирование
2. Глубокий сбор контекстной информации
3. Синтез финального ответа

Адаптировано из tess_search/module_10_1_intelligent_search.
"""
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DeepAnalysisAgent:
    """
    Агент глубокого анализа для качественных аналитических ответов.

    Пример использования:
    ```python
    agent = DeepAnalysisAgent(graph_traverser, llm_router)

    result = await agent.perform_deep_analysis(
        query="Чем занимается наша компания?",
        context_data={
            "graph_results": [...],
            "vector_results": [...],
        }
    )

    logger.info(result["final_answer"])
    ```
    """

    def __init__(self, graph_traverser=None, llm_router=None):
        """
        Args:
            graph_traverser: GraphTraverser для поиска в графе
            llm_router: LLM Router для генерации ответов
        """
        self.traverser = graph_traverser
        self.llm = llm_router
        self.name = "DeepAnalysisAgent"

    async def perform_deep_analysis(
        self,
        query: str,
        context_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Выполняет трёхэтапный глубокий анализ.

        Args:
            query: Пользовательский запрос
            context_data: Собранные данные из других компонентов

        Returns:
            Результат глубокого анализа
        """
        try:
            logger.info(f"🧠 Deep Analysis: начинаю анализ запроса '{query[:50]}...'")

            # Этап 1: Предварительный анализ и планирование
            preliminary_analysis = await self._perform_preliminary_analysis(query, context_data)
            logger.info("   📋 Этап 1: план анализа создан")

            # Этап 2: Глубокое погружение в контекст
            deep_context = await self._gather_deep_context(query, preliminary_analysis, context_data)
            logger.info(f"   🔍 Этап 2: контекст собран ({len(deep_context.get('entity_details', {}))} сущностей)")

            # Этап 3: Синтез и формирование финального ответа
            final_analysis = await self._synthesize_final_answer(query, preliminary_analysis, deep_context)
            logger.info("   ✨ Этап 3: ответ синтезирован")

            return {
                "query": query,
                "analysis_stages": {
                    "preliminary": preliminary_analysis,
                    "deep_context": deep_context,
                    "final_synthesis": final_analysis
                },
                "final_answer": final_analysis.get("comprehensive_answer", ""),
                "confidence": final_analysis.get("confidence", 0.7),
                "sources": final_analysis.get("sources", []),
                "recommendations": final_analysis.get("recommendations", []),
                "key_findings": final_analysis.get("key_findings", []),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"❌ Ошибка глубокого анализа: {e}")
            return {
                "error": f"Ошибка глубокого анализа: {e!s}",
                "query": query,
                "final_answer": f"Произошла ошибка при анализе: {e!s}",
                "confidence": 0.0,
                "timestamp": datetime.now().isoformat()
            }

    async def _perform_preliminary_analysis(
        self,
        query: str,
        context_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Этап 1: Предварительный анализ запроса и данных.
        """
        if not self.llm:
            return self._create_default_preliminary(query, context_data)

        analysis_prompt = f"""Ты - эксперт-аналитик. Проанализируй запрос и составь план конкретного анализа.

ВАЖНО: Пользователь хочет ГОТОВЫЙ АНАЛИЗ с КОНКРЕТНЫМИ ДАННЫМИ, не советы.

Запрос: "{query}"

Доступные данные:
- Результаты поиска в графах: {len(context_data.get('nodes', []))} элементов
- Векторные результаты: {len(context_data.get('vector_results', []))} элементов

ОПРЕДЕЛИ ПЛАН АНАЛИЗА:
1. Тип анализа (отчет по эффективности, статус проекта, анализ компании)
2. Конкретные сущности для анализа
3. Какие КОНКРЕТНЫЕ МЕТРИКИ и ФАКТЫ нужно найти
4. Дополнительные поиски для получения данных

Ответь в формате JSON:
{{
    "query_type": "тип анализа",
    "key_entities": ["сущности для анализа"],
    "time_period": "период анализа",
    "analysis_aspects": ["метрики для поиска"],
    "additional_searches": ["дополнительные запросы"],
    "expected_outcome": "ожидаемый результат",
    "output_format": "формат вывода"
}}"""

        try:
            response = await self.llm.generate(
                prompt=analysis_prompt,
                temperature=0.2,
                max_tokens=1000
            )

            # Пытаемся распарсить JSON
            json_match = self._extract_json(response)
            if json_match:
                return json.loads(json_match)
        except Exception as e:
            logger.warning(f"LLM preliminary analysis failed: {e}")

        return self._create_default_preliminary(query, context_data)

    def _create_default_preliminary(
        self,
        query: str,
        context_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Создаёт план анализа по умолчанию."""
        query_lower = query.lower()

        # Определяем тип запроса
        if any(word in query_lower for word in ["компания", "организация", "занимается", "делает"]):
            query_type = "анализ деятельности компании"
            entities = ["компания", "проекты", "команда", "продукты"]
            searches = [
                "проекты компании активные",
                "команда сотрудники",
                "продукты услуги",
                "направления деятельности"
            ]
        elif any(word in query_lower for word in ["проект", "статус", "прогресс"]):
            query_type = "анализ проекта"
            entities = self._extract_entities_from_query(query)
            searches = [
                f"{query} статус результаты",
                f"{query} участники команда",
                f"{query} задачи прогресс"
            ]
        elif any(word in query_lower for word in ["сотрудник", "команда", "человек"]):
            query_type = "анализ команды"
            entities = self._extract_entities_from_query(query)
            searches = [
                "сотрудники роли",
                "команда проекты",
                "участники активность"
            ]
        else:
            query_type = "общий анализ"
            entities = self._extract_entities_from_query(query)
            searches = [
                f"{query} данные",
                f"{query} информация",
                "общая информация"
            ]

        return {
            "query_type": query_type,
            "key_entities": entities,
            "time_period": "все доступные данные",
            "analysis_aspects": [
                "ключевые факты",
                "участники и роли",
                "текущий статус",
                "результаты и достижения"
            ],
            "additional_searches": searches,
            "expected_outcome": f"Готовый анализ по запросу '{query}'",
            "output_format": "структурированный отчет"
        }

    async def _gather_deep_context(
        self,
        query: str,
        preliminary: Dict[str, Any],
        initial_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Этап 2: Глубокий сбор контекстной информации.
        """
        deep_context = {
            "entity_details": {},
            "relationship_analysis": {},
            "additional_findings": [],
            "aggregated_metrics": {}
        }

        # Собираем детальную информацию по каждой ключевой сущности
        if self.traverser:
            for entity in preliminary.get("key_entities", [])[:5]:  # Ограничиваем до 5
                entity_data = await self._analyze_entity_deeply(entity, initial_context)
                if entity_data:
                    deep_context["entity_details"][entity] = entity_data

        # Добавляем данные из начального контекста
        deep_context["initial_nodes"] = initial_context.get("nodes", [])
        deep_context["initial_vectors"] = initial_context.get("vector_results", [])

        # Агрегируем метрики
        deep_context["aggregated_metrics"] = self._aggregate_metrics(
            deep_context["entity_details"],
            initial_context
        )

        return deep_context

    async def _analyze_entity_deeply(
        self,
        entity: str,
        context_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Глубокий анализ конкретной сущности.
        """
        entity_lower = entity.lower()

        # Ищем данные об этой сущности в контексте
        related_nodes = []
        for node in context_data.get("nodes", []):
            node_text = str(node).lower()
            if entity_lower in node_text:
                related_nodes.append(node)

        for vr in context_data.get("vector_results", []):
            vr_text = str(vr).lower()
            if entity_lower in vr_text:
                related_nodes.append(vr)

        # Если есть traverser, делаем дополнительный поиск
        if self.traverser:
            try:
                # Поиск в графе
                graph_results = await self.traverser.find_nodes("Entity", {"name": entity}, limit=10)
                related_nodes.extend(graph_results)

                # Поиск людей
                people_results = await self.traverser.find_nodes("Person", {"name": entity}, limit=5)
                related_nodes.extend(people_results)
            except Exception as e:
                logger.warning(f"Graph search for {entity} failed: {e}")

        # Извлекаем метрики
        metrics = self._extract_entity_metrics(entity, related_nodes)

        return {
            "entity_name": entity,
            "related_data_count": len(related_nodes),
            "metrics": metrics,
            "sample_data": related_nodes[:5]  # Первые 5 примеров
        }

    def _extract_entity_metrics(
        self,
        entity: str,
        data: List[Dict]
    ) -> Dict[str, Any]:
        """Извлекает метрики из данных сущности."""
        metrics = {
            "total_mentions": len(data),
            "tasks_count": 0,
            "decisions_count": 0,
            "meetings_count": 0,
            "projects_count": 0
        }

        for item in data:
            item_text = str(item).lower()
            item_type = item.get("type", item.get("_label", "")).lower()

            if item_type == "task" or "задача" in item_text:
                metrics["tasks_count"] += 1
            if item_type == "decision" or "решение" in item_text:
                metrics["decisions_count"] += 1
            if item_type == "meeting" or "встреча" in item_text:
                metrics["meetings_count"] += 1
            if item_type == "project" or "проект" in item_text:
                metrics["projects_count"] += 1

        return metrics

    def _aggregate_metrics(
        self,
        entity_details: Dict[str, Any],
        initial_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Агрегирует метрики из всех источников."""
        aggregated = {
            "total_entities_analyzed": len(entity_details),
            "total_nodes_found": len(initial_context.get("nodes", [])),
            "total_vectors_found": len(initial_context.get("vector_results", [])),
            "tasks_total": 0,
            "decisions_total": 0,
            "meetings_total": 0,
            "projects_total": 0
        }

        for _entity, details in entity_details.items():
            metrics = details.get("metrics", {})
            aggregated["tasks_total"] += metrics.get("tasks_count", 0)
            aggregated["decisions_total"] += metrics.get("decisions_count", 0)
            aggregated["meetings_total"] += metrics.get("meetings_count", 0)
            aggregated["projects_total"] += metrics.get("projects_count", 0)

        return aggregated

    async def _synthesize_final_answer(
        self,
        query: str,
        preliminary: Dict[str, Any],
        deep_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Этап 3: Синтез финального подробного ответа.
        """
        if not self.llm:
            return self._create_simple_answer(query, preliminary, deep_context)

        # Форматируем данные для промпта
        entity_summary = self._format_entity_summary(deep_context.get("entity_details", {}))
        metrics_summary = self._format_metrics_summary(deep_context.get("aggregated_metrics", {}))
        nodes_summary = self._format_nodes_summary(deep_context.get("initial_nodes", []))

        synthesis_prompt = f"""ТЫ - ИСПОЛНИТЕЛЬНАЯ АНАЛИТИЧЕСКАЯ СИСТЕМА КОМПАНИИ "Tessbrain".

КРИТИЧЕСКИ ВАЖНАЯ ИНСТРУКЦИЯ:
🚫 ЗАПРЕЩЕНО писать "для создания отчета нужно", "необходимо определить", "следует проанализировать"
🚫 ЗАПРЕЩЕНО давать ИНСТРУКЦИИ пользователю
✅ ТЫ ДОЛЖЕН НЕМЕДЛЕННО ВЫПОЛНИТЬ ЗАПРОС И ДАТЬ ГОТОВЫЙ РЕЗУЛЬТАТ
✅ НАЧИНАЙ ОТВЕТ СРАЗУ С ФАКТОВ

ЗАПРОС ПОЛЬЗОВАТЕЛЯ: "{query}"

ТИП АНАЛИЗА: {preliminary.get('query_type', 'общий анализ')}

📊 СОБРАННЫЕ ДАННЫЕ ИЗ ГРАФА ЗНАНИЙ:

{entity_summary}

📈 МЕТРИКИ:
{metrics_summary}

📋 НАЙДЕННЫЕ ЭЛЕМЕНТЫ (встречи, задачи, решения, люди):
{nodes_summary}

ВАЖНО ДЛЯ ВОПРОСОВ О КОМПАНИИ:
- Если спрашивают "чем занимается компания" - анализируй ТЕМЫ встреч, РОЛИ людей, НАЗВАНИЯ задач
- Из ролей людей (например "Executive коуч", "Директор по образованию") можно понять направления деятельности
- Из тем встреч и задач можно понять текущие проекты и фокус компании
- Из упоминаний организаций (Сколково, банки и т.д.) можно понять партнёров и сферу

ПРАВИЛА ОТВЕТА:
1. Начни с конкретных фактов и выводов
2. Если данных мало - делай ВЫВОДЫ из косвенных данных (роли людей, темы встреч)
3. Структурируй ответ с заголовками (используй emoji для визуализации)
4. Укажи уровень уверенности в выводах

Создай JSON:
{{
    "comprehensive_answer": "Готовый подробный ответ на запрос с конкретными фактами",
    "key_findings": ["ключевая находка 1", "ключевая находка 2"],
    "confidence": 0.8,
    "sources": ["источник данных"],
    "recommendations": ["рекомендация 1"]
}}"""

        try:
            response = await self.llm.generate(
                prompt=synthesis_prompt,
                temperature=0.3,
                max_tokens=2000
            )

            json_match = self._extract_json(response)
            if json_match:
                return json.loads(json_match)

            # Если не JSON, используем весь ответ
            return {
                "comprehensive_answer": response,
                "key_findings": ["Анализ выполнен на основе доступных данных"],
                "confidence": 0.7,
                "sources": ["граф знаний", "векторный поиск"],
                "recommendations": []
            }

        except Exception as e:
            logger.warning(f"LLM synthesis failed: {e}")
            return self._create_simple_answer(query, preliminary, deep_context)

    def _create_simple_answer(
        self,
        query: str,
        preliminary: Dict[str, Any],
        deep_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Создаёт простой ответ без LLM."""
        metrics = deep_context.get("aggregated_metrics", {})
        entities = deep_context.get("entity_details", {})

        parts = [f"📊 **Результаты анализа по запросу:** {query}\n"]

        # Общая статистика
        parts.append("**Найдено данных:**")
        parts.append(f"- Узлов в графе: {metrics.get('total_nodes_found', 0)}")
        parts.append(f"- Векторных совпадений: {metrics.get('total_vectors_found', 0)}")
        parts.append(f"- Проанализировано сущностей: {metrics.get('total_entities_analyzed', 0)}")

        if metrics.get("tasks_total", 0) > 0:
            parts.append(f"- Задач: {metrics['tasks_total']}")
        if metrics.get("decisions_total", 0) > 0:
            parts.append(f"- Решений: {metrics['decisions_total']}")
        if metrics.get("projects_total", 0) > 0:
            parts.append(f"- Проектов: {metrics['projects_total']}")

        # Информация по сущностям
        if entities:
            parts.append("\n**Детали по сущностям:**")
            for name, details in list(entities.items())[:5]:
                count = details.get("related_data_count", 0)
                parts.append(f"- {name}: {count} связанных элементов")

        # Примеры данных
        nodes = deep_context.get("initial_nodes", [])
        if nodes:
            parts.append("\n**Примеры найденных данных:**")
            for node in nodes[:3]:
                node_name = node.get("name", node.get("title", "—"))
                node_type = node.get("type", node.get("_label", ""))
                if node_name:
                    parts.append(f"- [{node_type}] {node_name}")

        if metrics.get('total_nodes_found', 0) == 0 and metrics.get('total_vectors_found', 0) == 0:
            parts.append("\n⚠️ По данному запросу найдено мало данных. Возможно, нужно уточнить запрос или добавить больше данных в систему.")

        return {
            "comprehensive_answer": "\n".join(parts),
            "key_findings": [f"Найдено {metrics.get('total_nodes_found', 0)} узлов в графе"],
            "confidence": 0.6 if metrics.get('total_nodes_found', 0) > 0 else 0.3,
            "sources": ["граф знаний"],
            "recommendations": ["Добавьте больше данных для более точного анализа"]
        }

    def _format_entity_summary(self, entity_details: Dict[str, Any]) -> str:
        """Форматирует сводку по сущностям."""
        if not entity_details:
            return "Детальные данные по сущностям не найдены."

        parts = []
        for entity, details in entity_details.items():
            metrics = details.get("metrics", {})
            parts.append(f"**{entity}:**")
            parts.append(f"  - Упоминаний: {metrics.get('total_mentions', 0)}")
            parts.append(f"  - Задач: {metrics.get('tasks_count', 0)}")
            parts.append(f"  - Проектов: {metrics.get('projects_count', 0)}")

        return "\n".join(parts)

    def _format_metrics_summary(self, metrics: Dict[str, Any]) -> str:
        """Форматирует сводку метрик."""
        if not metrics:
            return "Метрики не собраны."

        parts = [
            f"- Всего узлов: {metrics.get('total_nodes_found', 0)}",
            f"- Всего векторов: {metrics.get('total_vectors_found', 0)}",
            f"- Задач: {metrics.get('tasks_total', 0)}",
            f"- Решений: {metrics.get('decisions_total', 0)}",
            f"- Проектов: {metrics.get('projects_total', 0)}"
        ]
        return "\n".join(parts)

    def _format_nodes_summary(self, nodes: List[Dict]) -> str:
        """Форматирует сводку по узлам."""
        if not nodes:
            return "Узлы не найдены."

        parts = []
        for node in nodes[:10]:
            name = node.get("name", node.get("title", node.get("summary", "—")))
            node_type = node.get("type", node.get("_label", "unknown"))
            if name:
                parts.append(f"- [{node_type}] {name[:50]}")

        if len(nodes) > 10:
            parts.append(f"... и ещё {len(nodes) - 10} элементов")

        return "\n".join(parts)

    def _extract_entities_from_query(self, query: str) -> List[str]:
        """Извлекает потенциальные сущности из запроса."""
        entities = []

        # Ищем слова с заглавной буквы
        words = query.split()
        for word in words:
            # Убираем знаки препинания
            clean_word = word.strip("?.,!:;\"'")
            if clean_word and clean_word[0].isupper() and len(clean_word) > 2:
                entities.append(clean_word)

        # Добавляем ключевые термины
        key_terms = ["проект", "задача", "команда", "компания", "продукт"]
        for term in key_terms:
            if term in query.lower() and term not in [e.lower() for e in entities]:
                entities.append(term)

        return entities[:5]

    def _extract_json(self, text: str) -> Optional[str]:
        """Извлекает JSON из текста."""
        import re
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            return json_match.group()
        return None

