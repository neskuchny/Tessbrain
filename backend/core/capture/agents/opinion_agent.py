# -*- coding: utf-8 -*-
"""
TESSENT BRAIN - Opinion Agent
==============================

Извлекает мнения участников с глубоким анализом sentiment,
аргументации и мотивации.
"""
from typing import Any, Dict, Optional

from .base_agent import BaseExtractionAgent


class OpinionAgent(BaseExtractionAgent):
    """
    Агент извлечения мнений с продвинутым анализом.

    Возможности:
    - Многоуровневый sentiment анализ
    - Анализ аргументации и обоснований
    - Выявление скрытых мотиваций и интересов
    - Оценка влиятельности мнения
    """

    def get_entity_type(self) -> str:
        return "opinion"

    def get_instruction(self) -> str:
        return '''Ты - OPINION AGENT, эксперт по анализу человеческих мнений и мотиваций.

Твоя задача - извлечь ВСЕ мнения, точки зрения и оценки участников встречи.

ФОРМАТ ВЫВОДА - JSON массив:
```json
[
  {
    "opinion_id": "opinion_001",
    "author": "Имя автора мнения",
    "opinion_summary": "Краткая суть мнения (1-2 предложения)",
    "topic": "Тема/предмет мнения",
    "opinion_category": "strategic|tactical|personal|technical|process",
    "sentiment_analysis": {
      "primary_sentiment": "positive|negative|neutral|mixed",
      "intensity": 0.8,
      "emotional_undertones": ["уверенность", "энтузиазм"],
      "confidence_in_opinion": "high|medium|low"
    },
    "argumentation": {
      "reasoning_type": "data_based|experience_based|intuitive|emotional",
      "supporting_evidence": ["Приведенные доказательства"],
      "logical_structure": "strong|moderate|weak",
      "bias_indicators": ["Возможные предвзятости"]
    },
    "motivational_analysis": {
      "underlying_interests": ["Скрытые интересы"],
      "risk_concerns": ["Опасения и риски"],
      "opportunity_focus": ["Видимые возможности"],
      "personal_stake": "high|medium|low"
    },
    "influence_potential": {
      "persuasiveness": 0.7,
      "authority_level": "expert|experienced|peer|novice",
      "social_influence": "high|medium|low",
      "likely_supporters": ["Кто может поддержать это мнение"]
    },
    "business_implications": {
      "strategic_alignment": "aligned|neutral|misaligned",
      "resource_implications": ["Влияние на ресурсы"],
      "risk_assessment": "low|medium|high"
    },
    "quote": "Прямая цитата с мнением",
    "confidence_score": 0.85
  }
]
```

ВАЖНО — ТОЛЬКО РЕАЛЬНО ВЫСКАЗАННЫЕ МНЕНИЯ:
- Извлекай ВСЕ мнения, даже неявные, но каждое должно опираться на реплику из
  текста. Автора указывай, только если он назван.
- Отличай факты от мнений и мнение от намерения/решения.
- Эмоциональную окраску и подтекст оценивай по словам говорящего, не приписывай
  человеку чувств, которых в тексте нет.
- Не выдумывай мнения «за» участника, который об этом не говорил.
- Если мнений нет — верни `[]`.
'''

    def normalize(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализует извлеченное мнение"""
        if not isinstance(item, dict):
            return None

        # Проверяем обязательные поля
        if not item.get("author") and not item.get("opinion_summary"):
            return None

        # Базовая нормализация
        normalized = super().normalize(item)
        if not normalized:
            return None

        # Генерируем ID если нет
        if not normalized.get("opinion_id"):
            import hashlib
            content = f"{normalized.get('author', '')}_{normalized.get('opinion_summary', '')}"
            normalized["opinion_id"] = f"opinion_{hashlib.md5(content.encode()).hexdigest()[:8]}"

        # Нормализуем sentiment
        sentiment = normalized.get("sentiment_analysis", {})
        if isinstance(sentiment, dict):
            sentiment.setdefault("primary_sentiment", "neutral")
            sentiment.setdefault("intensity", 0.5)
            sentiment.setdefault("confidence_in_opinion", "medium")
        else:
            normalized["sentiment_analysis"] = {
                "primary_sentiment": "neutral",
                "intensity": 0.5,
                "confidence_in_opinion": "medium"
            }

        # Нормализуем категорию
        valid_categories = ["strategic", "tactical", "personal", "technical", "process"]
        if normalized.get("opinion_category") not in valid_categories:
            normalized["opinion_category"] = "tactical"

        return normalized


