# -*- coding: utf-8 -*-
"""
Expert Knowledge Extractor Agent - Извлечение экспертных знаний.

Специализируется на поиске:
- Уникальных знаний и опыта (domain expertise)
- Технических деталей и нюансов (technical know-how)
- Архитектурных решений и их обоснований (architectural decisions)
- Профессиональных советов и рекомендаций
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class ExpertKnowledge:
    """Извлечённое экспертное знание."""
    id: str = field(default_factory=lambda: str(uuid4()))

    # Тип: domain, technical, process, strategic
    type: str = "domain"

    # Содержание
    title: str = ""
    content: str = ""

    # Эксперт
    expert_name: str = ""
    expert_role: str = ""

    # Контекст
    domain: str = ""  # Область знаний
    complexity: str = "medium"  # low, medium, high

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
            "content": self.content,
            "expert_name": self.expert_name,
            "expert_role": self.expert_role,
            "domain": self.domain,
            "complexity": self.complexity,
            "source_meeting_id": self.source_meeting_id,
            "source_quote": self.source_quote,
            "confidence": self.confidence,
            "extracted_at": self.extracted_at
        }


class ExpertKnowledgeExtractorAgent:
    """
    Агент для извлечения глубоких экспертных знаний.
    """

    def __init__(self, llm_router=None):
        self.llm = llm_router
        logger.info("✅ ExpertKnowledgeExtractorAgent initialized")

    async def extract_expert_knowledge(
        self,
        meeting_data: Dict[str, Any],
        transcript: str
    ) -> List[ExpertKnowledge]:
        """
        Извлекает экспертные знания из транскрипта.
        """
        if not self.llm or not transcript:
            return []

        try:
            # Получаем список участников для контекста
            participants = meeting_data.get("participants", [])
            participants_context = ", ".join([
                f"{p.get('name')} ({p.get('role', 'unknown')})"
                for p in participants if isinstance(p, dict) and p.get('name')
            ])

            prompt = f"""Проанализируй транскрипт встречи и извлеки ценные ЭКСПЕРТНЫЕ ЗНАНИЯ.

ТРАНСКРИПТ (фрагмент):
{transcript[:10000]}

УЧАСТНИКИ:
{participants_context}

ЗАДАЧА:
Найти уникальные знания, которыми делятся эксперты. Игнорируй общеизвестные факты и "воду".
Ищи:
1. **Domain Expertise**: Глубокое понимание предметной области.
2. **Technical Know-how**: Технические детали, трюки, нюансы реализации.
3. **Strategic Insight**: Стратегическое видение и обоснования.
4. **Process Wisdom**: Опыт оптимизации процессов.

ДЛЯ КАЖДОГО ЗНАНИЯ ОПРЕДЕЛИ:
- Тип (domain/technical/strategic/process)
- Заголовок (суть знания)
- Детальное содержание (само знание)
- Кто поделился (эксперт)
- Область (domain - например "Sales", "DevOps", "Management")
- Сложность (low/medium/high)

Верни ТОЛЬКО валидный JSON массив:
[
  {{
    "type": "domain|technical|strategic|process",
    "title": "Краткая суть",
    "content": "Детальное описание знания",
    "expert_name": "Имя эксперта",
    "domain": "Область знаний",
    "complexity": "low|medium|high",
    "source_quote": "Цитата",
    "confidence": 0.0-1.0
  }}
]

ВАЖНО: source_quote обязателен и дословен — фрагмент, где знание высказано.
Кристаллизуй то, что эксперт РЕАЛЬНО сказал; не дополняй своими знаниями по теме и
не приписывай экспертизу человеку без опоры на его слова.

Если экспертных знаний нет - верни []"""

            system_prompt = ("Ты — опытный knowledge engineer. Кристаллизуй неявные "
                             "знания экспертов из СКАЗАННОГО в явную структуру, не "
                             "добавляя фактов извне. Игнорируй тривиальное.")

            response = await self.llm.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.2
            )

            return self._parse_response(response, meeting_data.get("meeting_id"))

        except Exception as e:
            logger.error(f"❌ Expert knowledge extraction failed: {e}")
            return []

    def _parse_response(self, response: str, meeting_id: str) -> List[ExpertKnowledge]:
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
                if item.get("content"):
                    results.append(ExpertKnowledge(
                        type=item.get("type", "domain"),
                        title=item.get("title", ""),
                        content=item.get("content", ""),
                        expert_name=item.get("expert_name", ""),
                        domain=item.get("domain", ""),
                        complexity=item.get("complexity", "medium"),
                        source_quote=item.get("source_quote", ""),
                        confidence=float(item.get("confidence", 0.8)),
                        source_meeting_id=meeting_id
                    ))

            return results

        except Exception as e:
            logger.warning(f"Failed to parse expert knowledge JSON: {e}")
            return []

