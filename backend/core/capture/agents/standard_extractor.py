# -*- coding: utf-8 -*-
"""
Standard Extractor Agent - Извлечение стандартов и нормативов.

Специализируется на поиске:
- Стандартов (standards): принятые нормы качества и исполнения
- Нормативов (norms): количественные и качественные ориентиры
- SLA/SLO (service levels): уровни обслуживания
- Критериев приемки (acceptance criteria)
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class ExtractedStandard:
    """Извлечённый стандарт."""
    id: str = field(default_factory=lambda: str(uuid4()))

    # Тип: standard, norm, sla, criteria
    type: str = "standard"

    # Содержание
    title: str = ""
    description: str = ""

    # Детали
    threshold: str = ""  # Пороговое значение
    scope: str = ""  # Область применения
    mandatory: bool = True

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
            "threshold": self.threshold,
            "scope": self.scope,
            "mandatory": self.mandatory,
            "source_meeting_id": self.source_meeting_id,
            "source_quote": self.source_quote,
            "confidence": self.confidence,
            "extracted_at": self.extracted_at
        }


class StandardExtractorAgent:
    """
    Агент для фиксации стандартов качества и работы.
    """

    def __init__(self, llm_router=None):
        self.llm = llm_router
        logger.info("✅ StandardExtractorAgent initialized")

    async def extract_standards(
        self,
        transcript: str,
        meeting_id: str
    ) -> List[ExtractedStandard]:
        """
        Извлекает стандарты из транскрипта.
        """
        if not self.llm or not transcript:
            return []

        try:
            prompt = f"""Проанализируй транскрипт и найди СТАНДАРТЫ и НОРМАТИВЫ.

ТРАНСКРИПТ (фрагмент):
{transcript[:8000]}

ИЩИ:
1. **Standards**: Стандарты качества, кодирования, дизайна, обслуживания.
2. **Norms**: Ожидаемые нормы выработки или поведения.
3. **SLA/SLO**: Соглашения об уровне сервиса.
4. **Acceptance Criteria**: Критерии приемки работ.

ДЛЯ КАЖДОГО ЭЛЕМЕНТА:
- Тип (standard/norm/sla/criteria)
- Название
- Описание
- Порог (threshold) - если есть конкретное значение
- Обязательность (true/false)

Верни ТОЛЬКО валидный JSON массив:
[
  {{
    "type": "standard|norm|sla|criteria",
    "title": "Название стандарта",
    "description": "Суть требования",
    "threshold": "Значение (напр. < 200ms)",
    "scope": "Где применяется",
    "mandatory": true,
    "source_quote": "Цитата",
    "confidence": 0.0-1.0
  }}
]

ВАЖНО: source_quote обязателен и дословен — фрагмент, где стандарт/норма
названы. threshold и scope бери только из текста, не выдумывай значения. Нет
цитаты → не включай. Пустой массив лучше выдуманного норматива.

Если ничего нет - верни []"""

            system_prompt = ("Ты — инженер по качеству и стандартизации. Фиксируй "
                             "четкие требования и нормы СТРОГО из сказанного, без "
                             "домыслов о значениях.")

            response = await self.llm.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.1
            )

            return self._parse_response(response, meeting_id)

        except Exception as e:
            logger.error(f"❌ Standard extraction failed: {e}")
            return []

    def _parse_response(self, response: str, meeting_id: str) -> List[ExtractedStandard]:
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
                    results.append(ExtractedStandard(
                        type=item.get("type", "standard"),
                        title=item.get("title", ""),
                        description=item.get("description", ""),
                        threshold=item.get("threshold", ""),
                        scope=item.get("scope", ""),
                        mandatory=item.get("mandatory", True),
                        source_quote=item.get("source_quote", ""),
                        confidence=float(item.get("confidence", 0.8)),
                        source_meeting_id=meeting_id
                    ))

            return results

        except Exception as e:
            logger.warning(f"Failed to parse standard JSON: {e}")
            return []

