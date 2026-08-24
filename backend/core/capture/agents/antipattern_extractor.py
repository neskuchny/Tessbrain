# -*- coding: utf-8 -*-
"""
Antipattern Extractor Agent - Извлечение антипаттернов и ошибок.

Специализируется на поиске:
- Антипаттернов (что НЕ надо делать)
- Известных проблем и "граблей" (pitfalls)
- Неэффективных практик (bad practices)
- Ошибочных убеждений (misconceptions)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class ExtractedAntipattern:
    """Извлечённый антипаттерн."""
    id: str = field(default_factory=lambda: str(uuid4()))

    # Тип: antipattern, pitfall, bad_practice
    type: str = "antipattern"

    # Содержание
    title: str = ""
    description: str = ""

    # Контекст
    consequences: str = ""  # Негативные последствия
    solution: str = ""  # Как делать правильно (если известно)

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
            "consequences": self.consequences,
            "solution": self.solution,
            "source_meeting_id": self.source_meeting_id,
            "source_quote": self.source_quote,
            "confidence": self.confidence,
            "extracted_at": self.extracted_at
        }


class AntipatternExtractorAgent:
    """
    Агент для выявления вредных практик и антипаттернов.
    """

    def __init__(self, llm_router=None):
        self.llm = llm_router
        logger.info("✅ AntipatternExtractorAgent initialized")

    async def extract_antipatterns(
        self,
        transcript: str,
        meeting_id: str
    ) -> List[ExtractedAntipattern]:
        """
        Извлекает антипаттерны из транскрипта.
        """
        if not self.llm or not transcript:
            return []

        try:
            prompt = f"""Проанализируй транскрипт и найди обсуждаемые АНТИПАТТЕРНЫ и ОШИБКИ.

ТРАНСКРИПТ (фрагмент):
{transcript[:8000]}

ИЩИ:
1. **Antipatterns**: Устойчивые неправильные способы решения проблем.
2. **Pitfalls ("Грабли")**: Скрытые опасности и неочевидные проблемы.
3. **Bad Practices**: Явно плохие или устаревшие методы работы.
4. **Common Mistakes**: Частые ошибки новичков или команд.

ДЛЯ КАЖДОГО ЭЛЕМЕНТА:
- Тип (antipattern/pitfall/bad_practice)
- Название (например "Microservice Spaghetti")
- Описание (в чем суть проблемы)
- Последствия (чем это грозит)
- Как надо правильно (если обсуждалось)

Верни ТОЛЬКО валидный JSON массив:
[
  {{
    "type": "antipattern|pitfall|bad_practice",
    "title": "Название антипаттерна",
    "description": "Суть проблемы",
    "consequences": "Негативные последствия",
    "solution": "Как избежать / как правильно",
    "source_quote": "Цитата",
    "confidence": 0.0-1.0
  }}
]

ВАЖНО: source_quote обязателен и дословен. Антипаттерн/ошибку фиксируй, только
если она реально обсуждалась в тексте; не приписывай ошибку конкретному человеку
без опоры на его слова и не выдумывай «как не надо» от себя.

Если ничего нет - верни []"""

            system_prompt = ("Ты — опытный аудитор и ментор. Предостерегай от "
                             "ошибок, выявляя обсуждения того, как делать НЕ надо, "
                             "СТРОГО по сказанному — без домыслов и обвинений.")

            response = await self.llm.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.2
            )

            return self._parse_response(response, meeting_id)

        except Exception as e:
            logger.error(f"❌ Antipattern extraction failed: {e}")
            return []

    def _parse_response(self, response: str, meeting_id: str) -> List[ExtractedAntipattern]:
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
                    results.append(ExtractedAntipattern(
                        type=item.get("type", "antipattern"),
                        title=item.get("title", ""),
                        description=item.get("description", ""),
                        consequences=item.get("consequences", ""),
                        solution=item.get("solution", ""),
                        source_quote=item.get("source_quote", ""),
                        confidence=float(item.get("confidence", 0.8)),
                        source_meeting_id=meeting_id
                    ))

            return results

        except Exception as e:
            logger.warning(f"Failed to parse antipattern JSON: {e}")
            return []

