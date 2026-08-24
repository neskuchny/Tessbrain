# -*- coding: utf-8 -*-
"""
RegulationClassifierAgent — определяет, является ли контент регламентом.

Критерии настоящего регламента:
✅ Описывает ПРОЦЕСС или ПРОЦЕДУРУ (как делать что-то)
✅ Содержит ШАГИ или ЭТАПЫ
✅ Применимо МНОГОКРАТНО (не одноразовое действие)
✅ Имеет ОБУЧАЮЩУЮ ценность (можно научить других)

❌ НЕ регламент:
- Одноразовые задачи ("поправь слайд 5")
- Личные предпочтения ("мне нравится синий цвет")
- Обсуждение конкретного проекта без обобщения
- Простые замечания без методологии
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ContentType(Enum):
    """Тип контента по результатам классификации."""
    REGULATION = "regulation"           # Полноценный регламент/процедура
    GUIDELINE = "guideline"             # Рекомендации/гайдлайны
    BEST_PRACTICE = "best_practice"     # Лучшие практики
    CHECKLIST = "checklist"             # Чеклист
    TEMPLATE = "template"               # Шаблон
    SPECIFICATION = "specification"     # ТЗ/Спецификация
    NOT_REGULATION = "not_regulation"   # Не является регламентом


@dataclass
class ClassificationResult:
    """Результат классификации контента."""
    content_type: ContentType
    confidence: float  # 0.0 - 1.0
    is_regulation_worthy: bool  # Стоит ли генерировать документ
    reasoning: str
    topic: str
    keywords: List[str]
    estimated_document_size: str  # "small", "medium", "large"
    recommended_sections: List[str]


class RegulationClassifierAgent:
    """
    Агент классификации контента — определяет, стоит ли генерировать документ.

    Использует LLM для анализа контента и определения:
    1. Это регламент или просто обсуждение?
    2. Какой тип документа можно создать?
    3. Насколько полноценным будет документ?
    """

    def __init__(self, llm_router):
        self.llm = llm_router
        logger.info("✅ RegulationClassifierAgent initialized")

    async def classify(
        self,
        content: str,
        meeting_title: str = "",
        context: Optional[Dict[str, Any]] = None
    ) -> ClassificationResult:
        """
        Классифицировать контент встречи.

        Args:
            content: Текст транскрипта или резюме встречи
            meeting_title: Название встречи
            context: Дополнительный контекст

        Returns:
            ClassificationResult с типом контента и рекомендациями
        """
        system_prompt = """Ты — REGULATION CLASSIFIER AGENT. Твоя задача — определить,
содержит ли контент встречи материал для создания полноценного РЕГЛАМЕНТА или ДОКУМЕНТА.

## КРИТЕРИИ НАСТОЯЩЕГО РЕГЛАМЕНТА (все должны быть выполнены):

✅ **ПРОЦЕССНЫЙ** — описывает КАК делать что-то (процесс, процедуру, методологию)
✅ **СТРУКТУРИРОВАННЫЙ** — содержит шаги, этапы, последовательность действий
✅ **МНОГОКРАТНЫЙ** — применим повторно, не одноразовое действие
✅ **ОБУЧАЮЩИЙ** — можно использовать для обучения других сотрудников
✅ **ОБОБЩЁННЫЙ** — не привязан к конкретному проекту, а описывает общий подход

## ЭТО НЕ РЕГЛАМЕНТ:

❌ Одноразовые задачи: "поправь слайд 5", "отправь письмо клиенту"
❌ Личные предпочтения: "мне нравится такой стиль"
❌ Обсуждение конкретного проекта без методологии
❌ Простые замечания и правки
❌ Статусные апдейты: "мы сделали то-то"
❌ Вопросы без ответов

## ТИПЫ ДОКУМЕНТОВ:

1. **regulation** — Полноценный регламент (процедура с шагами)
2. **guideline** — Рекомендации (советы, но не строгие правила)
3. **best_practice** — Лучшие практики (примеры хорошего подхода)
4. **checklist** — Чеклист (список проверок)
5. **template** — Шаблон (структура для заполнения)
6. **specification** — ТЗ/Спецификация (требования к продукту)
7. **not_regulation** — НЕ подходит для документа

## ФОРМАТ ОТВЕТА (JSON):

```json
{
  "content_type": "regulation|guideline|best_practice|checklist|template|specification|not_regulation",
  "confidence": 0.0-1.0,
  "is_regulation_worthy": true/false,
  "reasoning": "Почему это является/не является регламентом",
  "topic": "Основная тема (например: 'Создание презентаций')",
  "keywords": ["ключевые", "слова", "темы"],
  "estimated_document_size": "small|medium|large",
  "recommended_sections": ["Введение", "Этапы процесса", "Примеры", "Чеклист"]
}
```

## ПРИМЕРЫ:

**Пример 1 — ЭТО РЕГЛАМЕНТ:**
Встреча: "Как делать презентации для клиентов"
Контент: "Сначала определяем цель презентации. Потом собираем данные о клиенте.
Структура: проблема-решение-результат. Используем корпоративный шаблон..."

→ content_type: "regulation", is_regulation_worthy: true

**Пример 2 — НЕ РЕГЛАМЕНТ:**
Встреча: "Статус по проекту X5"
Контент: "Вчера отправили презентацию. Надо поправить слайд 5, там ошибка в цифрах.
Клиент просил добавить график продаж."

→ content_type: "not_regulation", is_regulation_worthy: false
"""

        user_prompt = f"""Проанализируй контент встречи и определи, подходит ли он для создания документа.

## Название встречи:
{meeting_title}

## Контент:
{content[:8000]}

## Дополнительный контекст:
{context or "Нет"}

Ответь в формате JSON."""

        try:
            result = await self.llm.generate_json(
                prompt=user_prompt,
                system_prompt=system_prompt
            )

            if not result:
                return self._default_result("Не удалось получить ответ от LLM")

            return ClassificationResult(
                content_type=ContentType(result.get("content_type", "not_regulation")),
                confidence=float(result.get("confidence", 0.0)),
                is_regulation_worthy=result.get("is_regulation_worthy", False),
                reasoning=result.get("reasoning", ""),
                topic=result.get("topic", ""),
                keywords=result.get("keywords", []),
                estimated_document_size=result.get("estimated_document_size", "small"),
                recommended_sections=result.get("recommended_sections", [])
            )

        except Exception as e:
            logger.error(f"Classification failed: {e}")
            return self._default_result(str(e))

    async def classify_batch(
        self,
        meetings: List[Dict[str, Any]]
    ) -> List[ClassificationResult]:
        """
        Классифицировать несколько встреч.

        Args:
            meetings: Список встреч с полями title, transcript/summary

        Returns:
            Список ClassificationResult
        """
        results = []
        for meeting in meetings:
            content = meeting.get("transcription_text") or meeting.get("transcript") or meeting.get("summary", "")
            title = meeting.get("title", "")

            result = await self.classify(content, title)
            results.append(result)

        return results

    async def filter_regulation_worthy(
        self,
        meetings: List[Dict[str, Any]],
        min_confidence: float = 0.6
    ) -> List[Dict[str, Any]]:
        """
        Отфильтровать только те встречи, из которых можно создать документ.

        Args:
            meetings: Список встреч
            min_confidence: Минимальная уверенность для включения

        Returns:
            Отфильтрованный список встреч с добавленной классификацией
        """
        worthy_meetings = []

        for meeting in meetings:
            content = meeting.get("transcription_text") or meeting.get("transcript") or meeting.get("summary", "")
            title = meeting.get("title", "")

            classification = await self.classify(content, title)

            if classification.is_regulation_worthy and classification.confidence >= min_confidence:
                meeting["_classification"] = classification
                worthy_meetings.append(meeting)
                logger.info(f"✅ Meeting '{title}' is regulation-worthy: {classification.content_type.value}")
            else:
                logger.debug(f"❌ Meeting '{title}' is NOT regulation-worthy: {classification.reasoning}")

        return worthy_meetings

    def _default_result(self, error: str) -> ClassificationResult:
        """Результат по умолчанию при ошибке."""
        return ClassificationResult(
            content_type=ContentType.NOT_REGULATION,
            confidence=0.0,
            is_regulation_worthy=False,
            reasoning=f"Ошибка классификации: {error}",
            topic="",
            keywords=[],
            estimated_document_size="small",
            recommended_sections=[]
        )

