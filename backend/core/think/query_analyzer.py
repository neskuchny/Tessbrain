# -*- coding: utf-8 -*-
"""
Query Analyzer - анализ и классификация запросов пользователя.

Определяет:
- Тип запроса (статус проекта, информация о человеке, поиск задач и т.д.)
- Извлекает сущности из запроса
- Определяет временной контекст
- Оценивает сложность (простой поиск vs multi-hop reasoning)
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class QueryType(Enum):
    """Типы запросов"""
    # Статусные запросы
    PROJECT_STATUS = "project_status"      # "Что с проектом X?"
    PERSON_STATUS = "person_status"        # "Чем занимается Иван?"
    TASK_STATUS = "task_status"            # "Какой статус задачи Y?"
    TEAM_STATUS = "team_status"            # "Как дела у команды Z?"

    # Поисковые запросы
    SEARCH_DECISIONS = "search_decisions"  # "Какие решения приняли?"
    SEARCH_TASKS = "search_tasks"          # "Какие задачи назначены?"
    SEARCH_PROBLEMS = "search_problems"    # "Какие проблемы есть?"
    SEARCH_MEETINGS = "search_meetings"    # "Какие встречи были?"

    # Аналитические запросы
    COMPARE = "compare"                    # "Сравни проекты A и B"
    TREND = "trend"                        # "Как меняется X со временем?"
    ANOMALY = "anomaly"                    # "Есть ли проблемы/аномалии?"
    SUMMARY = "summary"                    # "Дай общую картину"
    CHANGE = "change"                      # "Что изменилось?"
    AUDIT = "audit"                        # "Проведи аудит"
    DISCOVERY = "discovery"                # "Что мы упускаем?"

    # Связи и зависимости
    RELATIONSHIPS = "relationships"        # "Кто связан с X?"
    DEPENDENCIES = "dependencies"          # "От чего зависит Y?"
    IMPACT = "impact"                      # "На что повлияет Z?"

    # Рекомендации
    RECOMMENDATIONS = "recommendations"    # "Что порекомендуешь?"
    NEXT_STEPS = "next_steps"              # "Какие следующие шаги?"

    # Общий вопрос
    GENERAL = "general"                    # Всё остальное


class QueryComplexity(Enum):
    """Сложность запроса"""
    SIMPLE = "simple"          # Один hop, прямой поиск
    MODERATE = "moderate"      # 2-3 hops, несколько источников
    COMPLEX = "complex"        # 4+ hops, агрегация, reasoning


@dataclass
class QueryIntent:
    """Намерение пользователя из запроса"""
    query_type: QueryType
    complexity: QueryComplexity

    # Извлечённые сущности
    entities: List[Dict[str, Any]] = field(default_factory=list)

    # Временной контекст
    time_range: Optional[Dict[str, str]] = None

    # Фильтры
    filters: Dict[str, Any] = field(default_factory=dict)

    # Оригинальный запрос
    original_query: str = ""

    # Нормализованный запрос
    normalized_query: str = ""

    # Ключевые слова
    keywords: List[str] = field(default_factory=list)

    # Уверенность классификации
    confidence: float = 0.8

    # Метаданные
    metadata: Dict[str, Any] = field(default_factory=dict)


class QueryAnalyzer:
    """
    Анализатор запросов пользователя.

    Использует правила + LLM для классификации запросов.

    Пример:
    ```python
    analyzer = QueryAnalyzer(llm_router)

    intent = await analyzer.analyze("Что с проектом Tessbrain?")
    logger.info(intent.query_type)  # QueryType.PROJECT_STATUS
    logger.info(intent.entities)    # [{"type": "project", "name": "Tessbrain"}]
    ```
    """

    # Паттерны для быстрой классификации (без LLM)
    PATTERNS = {
        QueryType.PROJECT_STATUS: [
            r"что с проект\w*\s+(.+)",
            r"статус проект\w*\s+(.+)",
            r"как дела.*проект\w*\s+(.+)",
            r"project\s+(.+)\s+status",
        ],
        QueryType.PERSON_STATUS: [
            r"чем заним\w+\s+(.+)",
            r"что делает\s+(.+)",
            r"как дела у\s+(.+)",
            r"статус\s+(.+)",  # если не проект
        ],
        QueryType.TASK_STATUS: [
            r"статус задач\w*\s+(.+)",
            r"как.*задач\w*\s+(.+)",
            r"task\s+(.+)",
        ],
        QueryType.SEARCH_DECISIONS: [
            r"как\w*е?\s+решени\w+",
            r"что решили",
            r"принят\w+\s+решени\w+",
            r"decisions?",
        ],
        QueryType.SEARCH_TASKS: [
            r"как\w*е?\s+задач\w+",
            r"список задач",
            r"tasks?",
            r"что нужно сделать",
        ],
        QueryType.SEARCH_PROBLEMS: [
            r"как\w*е?\s+проблем\w+",
            r"есть ли проблем\w+",
            r"что не так",
            r"problems?",
            r"issues?",
        ],
        QueryType.SEARCH_MEETINGS: [
            r"как\w*е?\s+встреч\w+",
            r"meetings?",
            r"совещани\w+",
        ],
        QueryType.COMPARE: [
            r"сравни\w*\s+(.+)\s+и\s+(.+)",
            r"compare\s+(.+)\s+and\s+(.+)",
            r"разница между",
        ],
        QueryType.TREND: [
            r"как меня\w+",
            r"динамика",
            r"trend",
            r"со временем",
        ],
        QueryType.CHANGE: [
            r"что изменил\w+",
            r"что поменял\w+",
            r"какие изменени\w+",
            r"что нового",
            r"изменени\w+.*(?:за|с)\s+",
            r"что стало иначе",
        ],
        QueryType.ANOMALY: [
            r"аномали\w+",
            r"проблем\w+",
            r"риск\w+",
            r"anomal\w+",
        ],
        QueryType.AUDIT: [
            r"аудит",
            r"проведи аудит",
            r"провер\w+.*состояни\w+",
            r"review",
            r"проведи проверку",
            r"насколько .* хорошо",
        ],
        QueryType.DISCOVERY: [
            r"что мы не видим",
            r"что упуска\w+",
            r"найди скрыт\w+",
            r"выяви возможност\w+",
            r"обнаруж\w+ инсайт\w+",
            r"discover\w+",
        ],
        QueryType.SUMMARY: [
            r"общ\w+\s+картин\w+",
            r"summary",
            r"итог\w+",
            r"обзор",
        ],
        QueryType.RELATIONSHIPS: [
            r"кто связан",
            r"связи",
            r"relationships?",
            r"кто работает с",
        ],
        QueryType.DEPENDENCIES: [
            r"от чего зависит",
            r"зависимост\w+",
            r"dependenc\w+",
            r"блокир\w+",
        ],
        QueryType.RECOMMENDATIONS: [
            r"рекоменд\w+",
            r"посовет\w+",
            r"recommend\w+",
            r"suggest\w+",
        ],
        QueryType.NEXT_STEPS: [
            r"следующ\w+\s+шаг\w+",
            r"что дальше",
            r"next steps?",
            r"план\s+действий",
        ],
    }

    # Ключевые слова для извлечения сущностей
    ENTITY_INDICATORS = {
        "project": ["проект", "project", "система"],
        "person": ["человек", "сотрудник", "person", "кто"],
        "task": ["задача", "task", "тикет", "issue"],
        "meeting": ["встреча", "meeting", "совещание", "созвон"],
        "decision": ["решение", "decision"],
        "product": ["продукт", "product", "сервис", "platform", "платформа"],
        "department": ["отдел", "департамент", "department", "направление"],
        "team": ["команда", "team", "сквад"],
        "technology": ["технология", "technology", "фреймворк", "библиотека"],
    }

    # Временные индикаторы
    TIME_INDICATORS = {
        "today": ["сегодня", "today"],
        "yesterday": ["вчера", "yesterday"],
        "this_week": ["на этой неделе", "this week", "за неделю"],
        "last_week": ["на прошлой неделе", "last week"],
        "this_month": ["в этом месяце", "this month", "за месяц"],
        "last_month": ["в прошлом месяце", "last month"],
    }

    def __init__(self, llm_router=None):
        """
        Args:
            llm_router: LLM Router для сложных запросов (опционально)
        """
        self.llm = llm_router

    async def analyze(self, query: str) -> QueryIntent:
        """
        Анализировать запрос пользователя.

        Args:
            query: Текст запроса

        Returns:
            QueryIntent с результатами анализа
        """
        # Нормализуем запрос
        normalized = self._normalize_query(query)

        # 1. Быстрая классификация по паттернам
        query_type, pattern_confidence, pattern_entities = self._classify_by_patterns(normalized)

        # 2. Извлекаем сущности
        entities = self._extract_entities(
            query=normalized,
            pattern_entities=pattern_entities,
            original_query=query,
        )

        # 3. Определяем временной контекст
        time_range = self._extract_time_range(normalized)

        # 4. Извлекаем ключевые слова
        keywords = self._extract_keywords(normalized)

        # 5. Определяем сложность
        complexity = self._estimate_complexity(query_type, entities, normalized)

        # 6. Если уверенность низкая и есть LLM — уточняем
        if pattern_confidence < 0.7 and self.llm:
            llm_result = await self._classify_with_llm(query, normalized)
            if llm_result:
                query_type = llm_result.get("query_type", query_type)
                entities.extend(llm_result.get("entities", []))
                entities = self._deduplicate_entities(entities)
                pattern_confidence = llm_result.get("confidence", pattern_confidence)

        return QueryIntent(
            query_type=query_type,
            complexity=complexity,
            entities=entities,
            time_range=time_range,
            filters={},
            original_query=query,
            normalized_query=normalized,
            keywords=keywords,
            confidence=pattern_confidence,
            metadata={
                "analyzed_at": datetime.now(timezone.utc).isoformat()
            }
        )

    def _normalize_query(self, query: str) -> str:
        """Нормализовать запрос"""
        # Приводим к нижнему регистру
        normalized = query.lower().strip()

        # Убираем лишние пробелы
        normalized = re.sub(r'\s+', ' ', normalized)

        # Убираем знаки препинания в конце
        normalized = re.sub(r'[?!.]+$', '', normalized)

        return normalized

    def _classify_by_patterns(
        self,
        query: str
    ) -> Tuple[QueryType, float, List[Dict[str, Any]]]:
        """Классифицировать по паттернам"""
        best_type = QueryType.GENERAL
        best_confidence = 0.5
        extracted_entities = []

        for query_type, patterns in self.PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, query, re.IGNORECASE)
                if match:
                    # Нашли совпадение
                    confidence = 0.85

                    # Извлекаем группы из regex как сущности
                    if match.groups():
                        for group in match.groups():
                            if group:
                                extracted_entities.append({
                                    "type": self._infer_entity_type_for_query_type(query_type),
                                    "name": group.strip(),
                                    "source": "pattern"
                                })

                    if confidence > best_confidence:
                        best_confidence = confidence
                        best_type = query_type

        return best_type, best_confidence, extracted_entities

    def _extract_entities(
        self,
        query: str,
        pattern_entities: List[Dict[str, Any]],
        original_query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Извлечь сущности из запроса"""
        entities = list(pattern_entities)
        original_case_query = original_query or query

        # Ищем по индикаторам
        for entity_type, indicators in self.ENTITY_INDICATORS.items():
            for indicator in indicators:
                # Ищем паттерн "индикатор + название"
                pattern = rf'{indicator}\s+["\']?([^"\',.?!]+)["\']?'
                matches = re.findall(pattern, query, re.IGNORECASE)

                for match in matches:
                    entity_name = match.strip()
                    if entity_name and len(entity_name) > 1:
                        # Проверяем что такой сущности ещё нет
                        exists = any(
                            e.get("name", "").lower() == entity_name.lower()
                            for e in entities
                        )
                        if not exists:
                            entities.append({
                                "type": entity_type,
                                "name": entity_name,
                                "source": "indicator"
                            })

        # Ищем имена собственные (с большой буквы)
        # Используем оригинальный регистр, иначе все proper nouns теряются.
        proper_nouns = re.findall(r'\b([A-ZА-ЯЁ][a-zа-яё]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+)*)\b', original_case_query)
        for noun in proper_nouns:
            if len(noun) > 2:
                exists = any(
                    e.get("name", "").lower() == noun.lower()
                    for e in entities
                )
                if not exists:
                    entities.append({
                        "type": "unknown",
                        "name": noun,
                        "source": "proper_noun"
                    })

        return self._deduplicate_entities(entities)

    def _extract_time_range(self, query: str) -> Optional[Dict[str, str]]:
        """Извлечь временной контекст"""
        for time_key, indicators in self.TIME_INDICATORS.items():
            for indicator in indicators:
                if indicator in query:
                    return {"period": time_key}

        # Ищем конкретные даты
        date_pattern = r'(\d{1,2}[./]\d{1,2}[./]\d{2,4})'
        dates = re.findall(date_pattern, query)
        if dates:
            return {"dates": dates}

        return None

    def _extract_keywords(self, query: str) -> List[str]:
        """Извлечь ключевые слова"""
        # Стоп-слова
        stop_words = {
            "что", "как", "где", "когда", "кто", "какой", "какая", "какие",
            "с", "в", "на", "по", "для", "от", "до", "из", "у", "о", "об",
            "и", "а", "но", "или", "если", "то", "это", "есть", "был", "была",
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "can", "could", "should", "may", "might", "must",
            "with", "for", "on", "at", "by", "from", "to", "of", "in",
        }

        # Токенизируем
        words = re.findall(r'\b\w+\b', query.lower())

        # Фильтруем
        keywords = [
            w for w in words
            if w not in stop_words and len(w) > 2
        ]

        return keywords

    def _estimate_complexity(
        self,
        query_type: QueryType,
        entities: List[Dict[str, Any]],
        query: str
    ) -> QueryComplexity:
        """Оценить сложность запроса"""

        # Простые запросы
        simple_types = {
            QueryType.TASK_STATUS,
            QueryType.SEARCH_MEETINGS,
        }

        # Сложные запросы
        complex_types = {
            QueryType.COMPARE,
            QueryType.TREND,
            QueryType.CHANGE,
            QueryType.ANOMALY,
            QueryType.AUDIT,
            QueryType.DISCOVERY,
            QueryType.IMPACT,
            QueryType.RECOMMENDATIONS,
        }

        if query_type in simple_types:
            return QueryComplexity.SIMPLE

        if query_type in complex_types:
            return QueryComplexity.COMPLEX

        # Если много сущностей — сложнее
        if len(entities) > 2:
            return QueryComplexity.COMPLEX

        if len(entities) > 1:
            return QueryComplexity.MODERATE

        # Если длинный запрос — возможно сложнее
        if len(query.split()) > 10:
            return QueryComplexity.MODERATE

        return QueryComplexity.MODERATE

    async def _classify_with_llm(
        self,
        original_query: str,
        normalized_query: str
    ) -> Optional[Dict[str, Any]]:
        """Классифицировать с помощью LLM"""
        if not self.llm:
            return None

        prompt = f"""Проанализируй запрос пользователя и определи:
1. Тип запроса (один из: project_status, person_status, task_status, search_decisions, search_tasks, search_problems, compare, trend, change, anomaly, summary, audit, discovery, relationships, dependencies, recommendations, next_steps, general)
2. Сущности, упомянутые в запросе (проекты, люди, задачи, технологии)
3. Уверенность в классификации (0.0-1.0)

Запрос: "{original_query}"

Ответь в формате JSON:
{{
    "query_type": "тип_запроса",
    "entities": [
        {{"type": "project|person|task|technology", "name": "название"}}
    ],
    "confidence": 0.9
}}"""

        try:
            response = await self.llm.generate(
                prompt=prompt,
                temperature=0.1,
                max_tokens=500
            )

            # Парсим JSON из ответа
            import json

            # Ищем JSON в ответе
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                result = json.loads(json_match.group())

                # Конвертируем строку в QueryType
                type_str = result.get("query_type", "general")
                try:
                    result["query_type"] = QueryType(type_str)
                except ValueError:
                    result["query_type"] = QueryType.GENERAL

                return result

        except Exception as e:
            logger.warning(f"LLM classification failed: {e}")

        return None

    def _infer_entity_type_for_query_type(self, query_type: QueryType) -> str:
        """Подсказать тип целевой сущности по query type."""
        mapping = {
            QueryType.PROJECT_STATUS: "project",
            QueryType.PERSON_STATUS: "person",
            QueryType.TASK_STATUS: "task",
            QueryType.COMPARE: "unknown",
            QueryType.CHANGE: "unknown",
            QueryType.AUDIT: "unknown",
            QueryType.DISCOVERY: "unknown",
        }
        return mapping.get(query_type, "unknown")

    def _deduplicate_entities(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Удалить дубликаты сущностей, сохранив наиболее полезный type."""
        deduplicated: List[Dict[str, Any]] = []
        seen: Dict[str, int] = {}

        for entity in entities:
            name = str(entity.get("name", "")).strip()
            if not name:
                continue

            key = name.lower()
            entity_type = str(entity.get("type", "")).strip() or "unknown"

            if key not in seen:
                seen[key] = len(deduplicated)
                deduplicated.append({
                    **entity,
                    "name": name,
                    "type": entity_type,
                })
                continue

            existing = deduplicated[seen[key]]
            existing_type = existing.get("type", "unknown")
            if existing_type == "unknown" and entity_type != "unknown":
                existing["type"] = entity_type
            if not existing.get("source") and entity.get("source"):
                existing["source"] = entity["source"]

        return deduplicated

