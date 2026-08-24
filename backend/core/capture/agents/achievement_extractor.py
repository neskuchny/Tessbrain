# -*- coding: utf-8 -*-
"""
Achievement Extractor Agent - Извлечение достижений и провалов.

Специализируется на поиске:
- Достижений (achievements): успешные результаты, победы, выполненные цели
- Провалов (failures): неудачи, ошибки, невыполненные обязательства
- Уроков (lessons learned): выводы из опыта
- Метрик успеха (success metrics): количественные показатели
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class ExtractedAchievement:
    """Извлечённое достижение или провал."""
    id: str = field(default_factory=lambda: str(uuid4()))

    # Тип: achievement, failure, milestone
    type: str = "achievement"

    # Описание
    title: str = ""
    description: str = ""

    # Контекст
    impact: str = ""  # Влияние на бизнес/проект
    root_cause: str = ""  # Причина успеха или провала

    # Метрики
    metrics: List[str] = field(default_factory=list)

    # Участники
    contributors: List[str] = field(default_factory=list)

    # Источник
    source_meeting_id: Optional[str] = None
    source_quote: str = ""

    # Метаданные
    confidence: float = 0.8
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "description": self.description,
            "impact": self.impact,
            "root_cause": self.root_cause,
            "metrics": self.metrics,
            "contributors": self.contributors,
            "source_meeting_id": self.source_meeting_id,
            "source_quote": self.source_quote,
            "confidence": self.confidence,
            "extracted_at": self.extracted_at
        }


class AchievementExtractorAgent:
    """
    Агент для глубокого анализа достижений и провалов.
    """

    def __init__(self, llm_router=None):
        self.llm = llm_router
        logger.info("✅ AchievementExtractorAgent initialized")

    async def extract_achievements(
        self,
        meeting_data: Dict[str, Any],
        transcript: str
    ) -> List[ExtractedAchievement]:
        """
        Извлекает достижения и провалы из данных встречи.
        """
        if not self.llm or not transcript:
            return []

        try:
            # Подготовка контекста
            kpis = meeting_data.get("kpis", [])
            kpi_text = "\n".join([f"- {k.get('name')}: {k.get('value')}" for k in kpis if isinstance(k, dict)])

            prompt = f"""Проанализируй транскрипт встречи и найди значимые достижения и провалы.

ТРАНСКРИПТ (фрагмент):
{transcript[:8000]}

УПОМЯНУТЫЕ KPI:
{kpi_text}

НАЙДИ:
1. **Достижения (achievement)**: Успешные результаты, завершенные этапы, перевыполнение планов.
2. **Провалы (failure)**: Срывы сроков, ошибки, недостижение целей, проблемы качества.
3. **Вехи (milestone)**: Значимые пройденные этапы проекта.

ДЛЯ КАЖДОГО ЭЛЕМЕНТА ОПРЕДЕЛИ:
- Тип (achievement/failure/milestone)
- Суть (что произошло)
- Влияние (impact) на проект или бизнес
- Причину (root cause) успеха или неудачи
- Количественные метрики (если есть)
- Кто внёс вклад (имена/роли)

Верни ТОЛЬКО валидный JSON массив:
[
  {{
    "type": "achievement|failure|milestone",
    "title": "Краткий заголовок",
    "description": "Детальное описание",
    "impact": "Влияние на бизнес/проект",
    "root_cause": "Причина успеха/провала",
    "metrics": ["Метрика 1", "Метрика 2"],
    "contributors": ["Имя 1", "Роль 2"],
    "source_quote": "Цитата подтверждающая факт",
    "confidence": 0.0-1.0
  }}
]

ВАЖНО: source_quote обязателен и дословен — фраза, подтверждающая факт. Достижение
или провал должны быть реально названы; не выдумывай метрики результата и не
приписывай успех/провал конкретному человеку без опоры на текст.

Если ничего значимого нет - верни []"""

            system_prompt = ("Ты — аналитик эффективности бизнеса. Объективно "
                             "выявляй успехи и неудачи СТРОГО из сказанного, "
                             "игнорируя пустую похвалу или необоснованную критику; "
                             "не додумывай результаты, которых нет в тексте.")

            response = await self.llm.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.2
            )

            return self._parse_response(response, meeting_data.get("meeting_id"))

        except Exception as e:
            logger.error(f"❌ Achievement extraction failed: {e}")
            return []

    def _parse_response(self, response: str, meeting_id: str) -> List[ExtractedAchievement]:
        """Парсит ответ LLM."""
        try:
            # Чистка от markdown
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            response = response.strip()

            data = json.loads(response)
            results = []

            for item in data:
                if item.get("title"):
                    results.append(ExtractedAchievement(
                        type=item.get("type", "achievement"),
                        title=item.get("title", ""),
                        description=item.get("description", ""),
                        impact=item.get("impact", ""),
                        root_cause=item.get("root_cause", ""),
                        metrics=item.get("metrics", []),
                        contributors=item.get("contributors", []),
                        source_quote=item.get("source_quote", ""),
                        confidence=float(item.get("confidence", 0.8)),
                        source_meeting_id=meeting_id
                    ))

            return results

        except Exception as e:
            logger.warning(f"Failed to parse achievement JSON: {e}")
            return []

