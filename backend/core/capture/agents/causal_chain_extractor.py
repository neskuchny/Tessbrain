# -*- coding: utf-8 -*-
"""
TESSENT BRAIN — Causal Chain Extractor (Step 5)
=================================================

Извлекает причинно-следственные связи из транскриптов:
- Причина → Следствие (CAUSED_BY)
- Решение → Результат (LED_TO)
- Обоснование решений ("решили X потому что Y")

Создаёт связи в графе: CAUSED_BY, LED_TO

Tier 3+ (INTELLIGENCE)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CausalLink:
    """Одна причинно-следственная связь."""
    cause: str = ""           # Что стало причиной
    effect: str = ""          # Что стало следствием
    link_type: str = "caused_by"  # caused_by | led_to | resulted_in | because_of
    confidence: float = 0.7
    quote: str = ""           # Цитата из текста
    cause_entities: List[str] = field(default_factory=list)
    effect_entities: List[str] = field(default_factory=list)


class CausalChainExtractor:
    """
    Извлекает причинно-следственные цепочки из текста.

    Использует:
    1. Pattern-matching для русского языка (быстро, бесплатно)
    2. LLM extraction для неявных связей (точно, 1 вызов)
    """

    # Маркеры причинности для русского языка
    CAUSAL_PATTERNS_RU = [
        r"из-за\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"потому что\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"в результате\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"благодаря\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"(.{10,80}?)\s+привело к\s+(.{10,80})",
        r"(.{10,80}?)\s+поэтому\s+(.{10,80})",
        r"(.{10,80}?)\s+следовательно\s+(.{10,80})",
        r"вследствие\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"так как\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"по причине\s+(.{10,80}?)[,;.]\s*(.{10,80})",
        r"(.{10,80}?)\s+вызвало\s+(.{10,80})",
        r"(.{10,80}?)\s+стало причиной\s+(.{10,80})",
    ]

    def __init__(self, llm_client=None):
        self.llm = llm_client

    async def _get_llm(self):
        if self.llm:
            return self.llm
        try:
            from backend.core.llm.gemini_client import get_gemini_client
            self.llm = get_gemini_client()
            return self.llm
        except Exception:
            return None

    async def extract(
        self,
        transcript: str,
        meeting_id: str = "",
        meeting_context: Optional[Dict[str, Any]] = None,
    ) -> List[CausalLink]:
        """
        Извлечь причинно-следственные связи из транскрипта.

        Returns:
            Список CausalLink
        """
        if not transcript or len(transcript) < 100:
            return []

        links = []

        # 1. LLM extraction (основной метод)
        llm = await self._get_llm()
        if llm:
            try:
                llm_links = await self._extract_with_llm(transcript[:10000], llm)
                links.extend(llm_links)
            except Exception as e:
                logger.warning(f"Causal LLM extraction failed: {e}")

        # Дедупликация
        seen = set()
        unique_links = []
        for link in links:
            key = (link.cause[:50], link.effect[:50])
            if key not in seen:
                seen.add(key)
                unique_links.append(link)

        logger.info(f"🔗 Extracted {len(unique_links)} causal links from meeting {meeting_id}")
        return unique_links

    async def _extract_with_llm(self, text: str, llm) -> List[CausalLink]:
        """Извлечение через LLM."""
        prompt = f"""Найди в тексте встречи причинно-следственные связи.

Для каждой связи укажи:
- cause: что стало причиной (кратко)
- effect: что стало следствием (кратко)
- type: тип связи (caused_by, led_to, resulted_in, because_of)
- confidence: уверенность 0.0-1.0
- quote: краткая цитата из текста

Как разбирать (иллюстрация формата, НЕ данные для вывода):
- «Из-за задержки дизайна мы пропустили дедлайн» → cause и effect из этой фразы.
- «Перешли на новый фреймворк, потому что старый медленный» → причина и следствие
  из сказанного.

ВАЖНО: quote обязателен и дословен — фрагмент, где связь ЯВНО выражена (из-за,
потому что, привело к, поэтому). Не выводи причинность из совпадения по времени и
не додумывай связи, которых в тексте нет. Нет явной связи → не включай.

Текст:
{text}

Ответь СТРОГО в JSON:
[
  {{"cause": "...", "effect": "...", "type": "caused_by", "confidence": 0.8, "quote": "..."}},
  ...
]
Если причинно-следственных связей нет — верни пустой массив [].
Максимум 10 связей."""

        response = await llm.generate(prompt, temperature=0.1)
        data = self._parse_json(response)

        if not data or not isinstance(data, list):
            return []

        links = []
        for d in data[:10]:
            link = CausalLink(
                cause=d.get("cause", ""),
                effect=d.get("effect", ""),
                link_type=d.get("type", "caused_by"),
                confidence=float(d.get("confidence", 0.7)),
                quote=d.get("quote", ""),
            )
            if link.cause and link.effect:
                links.append(link)

        return links

    def _parse_json(self, response: str) -> Any:
        if not response:
            return None
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start:end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
