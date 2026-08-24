# -*- coding: utf-8 -*-
"""
Insight Extractor Agent - Извлечение инсайтов и озарений.

Специализируется на поиске:
- Инсайтов (insights): глубокое, неочевидное понимание ситуации
- Открытий (discoveries): новые факты и находки
- Гипотез (hypotheses): предположения, требующие проверки
- Трендов (trends): наблюдаемые закономерности
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class ExtractedInsight:
    """Извлечённый инсайт."""
    id: str = field(default_factory=lambda: str(uuid4()))

    # Тип: insight, discovery, hypothesis, trend
    type: str = "insight"

    # Содержание
    title: str = ""
    description: str = ""

    # Контекст
    implications: str = ""  # Последствия/выводы
    actionability: str = "medium"  # low, medium, high (насколько можно применить)

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
            "implications": self.implications,
            "actionability": self.actionability,
            "source_meeting_id": self.source_meeting_id,
            "source_quote": self.source_quote,
            "confidence": self.confidence,
            "extracted_at": self.extracted_at
        }


class InsightExtractorAgent:
    """
    Агент для поиска глубоких инсайтов.
    """

    def __init__(self, llm_router=None):
        self.llm = llm_router
        logger.info("✅ InsightExtractorAgent initialized")

    async def extract_insights(
        self,
        transcript: str,
        meeting_id: str
    ) -> List[ExtractedInsight]:
        """
        Извлекает инсайты из транскрипта.
        """
        if not self.llm or not transcript:
            return []

        try:
            prompt = f"""Проанализируй транскрипт и найди ИНСАЙТЫ и ОТКРЫТИЯ.

ТРАНСКРИПТ (фрагмент):
{transcript[:8000]}

ИЩИ:
1. **Insights**: "Ага-моменты", неочевидное понимание сути вещей.
2. **Discoveries**: Новые факты, о которых раньше не знали.
3. **Hypotheses**: Интересные предположения, которые команда хочет проверить.
4. **Patterns/Trends**: Замеченные закономерности.

ДЛЯ КАЖДОГО ЭЛЕМЕНТА:
- Тип (insight/discovery/hypothesis/trend)
- Суть (что поняли)
- Последствия (implications) - что это значит для бизнеса
- Actionability (low/medium/high) - можно ли на основе этого действовать

Верни ТОЛЬКО валидный JSON массив:
[
  {{
    "type": "insight|discovery|hypothesis|trend",
    "title": "Краткая суть",
    "description": "Детальное описание",
    "implications": "Что это значит для нас",
    "actionability": "low|medium|high",
    "source_quote": "Цитата",
    "confidence": 0.0-1.0
  }}
]

ВАЖНО:
- source_quote ОБЯЗАТЕЛЕН и дословен — фрагмент, из которого следует инсайт. Нет
  подходящей цитаты → это не инсайт, не включай.
- Инсайт должен вытекать из СКАЗАННОГО, а не из твоих общих знаний о рынке.
  implications — следствие названных фактов, без домыслов.
- Не выдавай банальность или пересказ за «ага-момент». Лучше пустой массив.

Если ничего нет - верни []"""

            system_prompt = ("Ты — стратег и исследователь. Ищи глубокие смыслы и "
                             "неочевидные связи СТРОГО в сказанном. Не добавляй "
                             "фактов извне, не выдумывай. Игнорируй банальности.")

            response = await self.llm.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.3
            )

            return self._parse_response(response, meeting_id)

        except Exception as e:
            logger.error(f"❌ Insight extraction failed: {e}")
            return []

    def _parse_response(self, response: str, meeting_id: str) -> List[ExtractedInsight]:
        """Парсит ответ LLM."""
        try:
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
                    results.append(ExtractedInsight(
                        type=item.get("type", "insight"),
                        title=item.get("title", ""),
                        description=item.get("description", ""),
                        implications=item.get("implications", ""),
                        actionability=item.get("actionability", "medium"),
                        source_quote=item.get("source_quote", ""),
                        confidence=float(item.get("confidence", 0.8)),
                        source_meeting_id=meeting_id
                    ))

            return results

        except Exception as e:
            logger.warning(f"Failed to parse insight JSON: {e}")
            return []

