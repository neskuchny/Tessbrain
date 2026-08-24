# -*- coding: utf-8 -*-
"""
ContentAggregatorAgent — собирает и структурирует контент из нескольких встреч.

Задачи:
1. Объединить контент из связанных встреч
2. Убрать дубликаты и противоречия
3. Выделить ключевые моменты
4. Собрать примеры и обоснования
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AggregatedContent:
    """Агрегированный контент для генерации документа."""
    topic: str
    summary: str
    key_points: List[str] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    best_practices: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    definitions: Dict[str, str] = field(default_factory=dict)
    tools_mentioned: List[str] = field(default_factory=list)
    people_involved: List[str] = field(default_factory=list)
    source_meetings: List[str] = field(default_factory=list)
    raw_quotes: List[Dict[str, str]] = field(default_factory=list)  # Цитаты для примеров


class ContentAggregatorAgent:
    """
    Агент агрегации контента — собирает информацию из нескольких встреч.
    """

    def __init__(self, llm_router):
        self.llm = llm_router
        logger.info("✅ ContentAggregatorAgent initialized")

    async def aggregate(
        self,
        meetings: List[Dict[str, Any]],
        topic: str,
        document_type: str = "regulation",
        model_tier=None,
    ) -> AggregatedContent:
        """
        Агрегировать контент из нескольких встреч.

        Args:
            meetings: Список встреч
            topic: Тема документа
            document_type: Тип документа (regulation, guideline, checklist, etc.)

        Returns:
            AggregatedContent с структурированной информацией
        """
        if not meetings:
            return AggregatedContent(topic=topic, summary="Нет данных")

        # Собрать весь контент
        all_content = self._prepare_content(meetings)

        # Извлечь структурированную информацию через LLM
        aggregated = await self._extract_structured_content(
            all_content, topic, document_type, model_tier=model_tier)

        # Добавить источники
        aggregated.source_meetings = [
            m.get("id", m.get("meeting_id", "unknown"))
            for m in meetings
        ]

        return aggregated

    def _prepare_content(self, meetings: List[Dict[str, Any]]) -> str:
        """Подготовить контент из всех встреч."""
        parts = []

        for i, meeting in enumerate(meetings, 1):
            title = meeting.get("title", f"Встреча {i}")
            content = meeting.get("transcription_text") or meeting.get("transcript") or meeting.get("summary", "")

            # Если контента слишком много, обрезаем (пока просто, потом можно умнее)
            if len(content) > 100000:
                content = content[:100000] + "... (обрезано)"

            parts.append(f"""
=== ВСТРЕЧА {i}: {title} ===
{content}
""")

        return "\n\n".join(parts)

    async def _extract_structured_content(
        self,
        content: str,
        topic: str,
        document_type: str,
        model_tier=None,
    ) -> AggregatedContent:
        """Извлечь структурированный контент через LLM."""

        system_prompt = f"""Ты — CONTENT AGGREGATOR AGENT. Твоя задача — извлечь и структурировать
информацию из встреч для создания документа типа "{document_type}" на тему "{topic}".

## ТВОЯ ЗАДАЧА:

1. **Извлечь ключевые моменты** — что важно знать по этой теме
2. **Выделить шаги/этапы** — если это процесс, какие шаги нужно выполнить
3. **Собрать примеры** — конкретные примеры из обсуждений
4. **Найти лучшие практики** — что рекомендуют делать
5. **Отметить предупреждения** — чего избегать, какие ошибки не допускать
6. **Собрать определения** — если упоминаются термины
7. **Выделить цитаты** — яркие высказывания для примеров в документе

## ВАЖНО:
- НЕ выдумывай информацию, только то что есть в контенте
- Если информации мало — так и скажи
- Сохраняй контекст и обоснования ("потому что...", "это важно так как...")
- Собирай РЕАЛЬНЫЕ примеры из обсуждений

## ФОРМАТ ОТВЕТА (JSON):

```json
{{
  "summary": "Краткое резюме всего контента (2-3 предложения)",
  "key_points": [
    "Ключевой момент 1 с обоснованием",
    "Ключевой момент 2 с обоснованием"
  ],
  "steps": [
    {{
      "step_number": 1,
      "title": "Название шага",
      "description": "Подробное описание что делать",
      "tips": ["Совет 1", "Совет 2"],
      "example": "Пример если есть"
    }}
  ],
  "examples": [
    {{
      "title": "Название примера",
      "description": "Описание ситуации",
      "outcome": "Результат/вывод"
    }}
  ],
  "best_practices": [
    "Лучшая практика 1 с обоснованием почему",
    "Лучшая практика 2"
  ],
  "warnings": [
    "Чего избегать и почему",
    "Типичная ошибка"
  ],
  "definitions": {{
    "Термин": "Определение"
  }},
  "tools_mentioned": ["Инструмент 1", "Инструмент 2"],
  "people_involved": ["Имя 1", "Имя 2"],
  "raw_quotes": [
    {{
      "quote": "Точная цитата из обсуждения",
      "context": "Контекст цитаты",
      "speaker": "Кто сказал (если известно)"
    }}
  ]
}}
```
"""

        # Ограничиваем контент чтобы не превысить лимит токенов
        max_content_length = 15000
        truncated_content = content[:max_content_length]
        if len(content) > max_content_length:
            truncated_content += "\n\n[... контент обрезан ...]"

        user_prompt = f"""Тема документа: {topic}
Тип документа: {document_type}

## КОНТЕНТ ИЗ ВСТРЕЧ:

{truncated_content}

Извлеки структурированную информацию для создания документа."""

        try:
            _kw = {"model_tier": model_tier} if model_tier is not None else {}
            result = await self.llm.generate_json(
                prompt=user_prompt,
                system_prompt=system_prompt,
                **_kw,
            )

            if not result:
                return AggregatedContent(
                    topic=topic,
                    summary="Не удалось извлечь информацию"
                )

            return AggregatedContent(
                topic=topic,
                summary=result.get("summary", ""),
                key_points=result.get("key_points", []),
                steps=[
                    {
                        "step_number": s.get("step_number", i+1),
                        "title": s.get("title", ""),
                        "description": s.get("description", ""),
                        "tips": s.get("tips", []),
                        "example": s.get("example", "")
                    }
                    for i, s in enumerate(result.get("steps", []))
                ],
                examples=result.get("examples", []),
                best_practices=result.get("best_practices", []),
                warnings=result.get("warnings", []),
                definitions=result.get("definitions", {}),
                tools_mentioned=result.get("tools_mentioned", []),
                people_involved=result.get("people_involved", []),
                raw_quotes=result.get("raw_quotes", [])
            )

        except Exception as e:
            logger.error(f"Content aggregation failed: {e}")
            return AggregatedContent(
                topic=topic,
                summary=f"Ошибка агрегации: {e}"
            )

    async def enrich_with_context(
        self,
        aggregated: AggregatedContent,
        graph_context: Optional[Dict[str, Any]] = None
    ) -> AggregatedContent:
        """
        Обогатить агрегированный контент данными из графа знаний.

        Args:
            aggregated: Уже агрегированный контент
            graph_context: Контекст из графа знаний

        Returns:
            Обогащённый AggregatedContent
        """
        if not graph_context:
            return aggregated

        # Добавить связанные сущности из графа
        if "related_entities" in graph_context:
            for entity in graph_context["related_entities"]:
                if entity.get("type") == "tool":
                    if entity["name"] not in aggregated.tools_mentioned:
                        aggregated.tools_mentioned.append(entity["name"])
                elif entity.get("type") == "person":
                    if entity["name"] not in aggregated.people_involved:
                        aggregated.people_involved.append(entity["name"])

        # Добавить определения из графа
        if "definitions" in graph_context:
            for term, definition in graph_context["definitions"].items():
                if term not in aggregated.definitions:
                    aggregated.definitions[term] = definition

        return aggregated

